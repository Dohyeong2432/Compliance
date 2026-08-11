"""Knowledge graph backends.

GraphStore is deliberately thin: subclasses implement CRUD primitives only
(add/get entity, add relation, list relations in each direction). Every
retrieval-relevant behavior — walking a SUPERSEDES chain to the version
valid at a point in time, and 1-hop expansion to related cases/
interpretations — is shared logic built once on top of those primitives, so
NetworkXGraphStore and KuzuGraphStore can never drift apart on semantics.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Iterable

from ontology.schema import Entity, RelationType, Relation


class GraphStore(ABC):
    @abstractmethod
    def add_entity(self, entity: Entity) -> None:
        ...

    @abstractmethod
    def add_relation(self, relation: Relation) -> None:
        ...

    @abstractmethod
    def get_entity(self, entity_id: str) -> Entity | None:
        ...

    @abstractmethod
    def has_entity(self, entity_id: str) -> bool:
        ...

    @abstractmethod
    def relations_from(self, entity_id: str, type: RelationType | None = None) -> list[Relation]:
        ...

    @abstractmethod
    def relations_to(self, entity_id: str, type: RelationType | None = None) -> list[Relation]:
        ...

    # ---- shared logic built on the primitives above ----

    def supersede_chain(self, entity_id: str) -> list[Entity]:
        """All versions linked (transitively, either direction) by
        SUPERSEDES to entity_id, oldest first."""
        if not self.has_entity(entity_id):
            return []
        visited: set[str] = set()
        frontier = [entity_id]
        while frontier:
            current = frontier.pop()
            if current in visited:
                continue
            visited.add(current)
            for rel in self.relations_from(current, RelationType.SUPERSEDES):
                if rel.target_id not in visited:
                    frontier.append(rel.target_id)
            for rel in self.relations_to(current, RelationType.SUPERSEDES):
                if rel.source_id not in visited:
                    frontier.append(rel.source_id)
        chain = [self.get_entity(eid) for eid in visited]
        entities = [e for e in chain if e is not None]
        entities.sort(key=lambda e: e.effective_date or date.min)
        return entities

    def resolve_effective_version(self, entity_id: str, as_of: date | None) -> Entity | None:
        """Find whichever version in entity_id's supersede chain was valid
        at `as_of` (defaults to today). This is what lets a query about a
        past date land on the law/regulation text that actually applied
        then, not whatever is current now."""
        entity = self.get_entity(entity_id)
        if entity is None:
            return None
        if as_of is None:
            as_of = date.today()
        chain = self.supersede_chain(entity_id) or [entity]
        for version in chain:
            if version.is_effective_at(as_of):
                return version
        candidates = [v for v in chain if v.effective_date is not None and v.effective_date <= as_of]
        if candidates:
            return max(candidates, key=lambda v: v.effective_date)
        return None

    def expand_related(
        self,
        entity_id: str,
        types: Iterable[RelationType] | None = None,
        limit: int = 5,
    ) -> list[Entity]:
        """1-hop expansion to related cases/interpretations/FAQs for extra
        grounding context, in either relation direction."""
        if types is None:
            types = (
                RelationType.INTERPRETS,
                RelationType.VIOLATES,
                RelationType.CITES,
                RelationType.RELATED_TO,
                RelationType.ANSWERED_BY,
            )
        type_set = set(types)
        neighbor_ids: list[str] = []
        for rel in self.relations_from(entity_id):
            if rel.type in type_set:
                neighbor_ids.append(rel.target_id)
        for rel in self.relations_to(entity_id):
            if rel.type in type_set:
                neighbor_ids.append(rel.source_id)

        seen: set[str] = set()
        results: list[Entity] = []
        for neighbor_id in neighbor_ids:
            if neighbor_id in seen or neighbor_id == entity_id:
                continue
            seen.add(neighbor_id)
            entity = self.get_entity(neighbor_id)
            if entity is not None:
                results.append(entity)
            if len(results) >= limit:
                break
        return results


class NetworkXGraphStore(GraphStore):
    """In-process graph store backed by networkx. No persistence."""

    def __init__(self):
        import networkx as nx

        self._graph = nx.MultiDiGraph()

    def add_entity(self, entity: Entity) -> None:
        self._graph.add_node(entity.id, entity=entity)

    def add_relation(self, relation: Relation) -> None:
        self._graph.add_edge(
            relation.source_id,
            relation.target_id,
            key=relation.type.value,
            type=relation.type,
            metadata=relation.metadata,
        )

    def get_entity(self, entity_id: str) -> Entity | None:
        node = self._graph.nodes.get(entity_id)
        return node["entity"] if node else None

    def has_entity(self, entity_id: str) -> bool:
        return entity_id in self._graph.nodes

    def relations_from(self, entity_id: str, type: RelationType | None = None) -> list[Relation]:
        if entity_id not in self._graph:
            return []
        out = []
        for _, target, data in self._graph.out_edges(entity_id, data=True):
            if type is None or data["type"] == type:
                out.append(Relation(entity_id, data["type"], target, data.get("metadata", {})))
        return out

    def relations_to(self, entity_id: str, type: RelationType | None = None) -> list[Relation]:
        if entity_id not in self._graph:
            return []
        out = []
        for source, _, data in self._graph.in_edges(entity_id, data=True):
            if type is None or data["type"] == type:
                out.append(Relation(source, data["type"], entity_id, data.get("metadata", {})))
        return out


class KuzuGraphStore(GraphStore):
    """Embedded (server-less) Kuzu-backed graph store, persisted on disk."""

    def __init__(self, db_path: str):
        import kuzu

        self._db = kuzu.Database(db_path)
        self._conn = kuzu.Connection(self._db)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        for ddl in (
            """
            CREATE NODE TABLE IF NOT EXISTS Entity(
                id STRING,
                type STRING,
                title STRING,
                body STRING,
                effective_date DATE,
                superseded_date DATE,
                allowed_depts STRING,
                source STRING,
                PRIMARY KEY (id)
            )
            """,
            "CREATE REL TABLE IF NOT EXISTS RelatesTo(FROM Entity TO Entity, type STRING)",
        ):
            self._conn.execute(ddl)

    def add_entity(self, entity: Entity) -> None:
        self._conn.execute(
            """
            MERGE (e:Entity {id: $id})
            SET e.type = $type, e.title = $title, e.body = $body,
                e.effective_date = $effective_date, e.superseded_date = $superseded_date,
                e.allowed_depts = $allowed_depts, e.source = $source
            """,
            parameters={
                "id": entity.id,
                "type": entity.type.value,
                "title": entity.title,
                "body": entity.body,
                "effective_date": entity.effective_date,
                "superseded_date": entity.superseded_date,
                "allowed_depts": ",".join(entity.allowed_depts),
                "source": entity.source,
            },
        )

    def add_relation(self, relation: Relation) -> None:
        self._conn.execute(
            """
            MERGE (a:Entity {id: $source_id})
            MERGE (b:Entity {id: $target_id})
            MERGE (a)-[r:RelatesTo {type: $type}]->(b)
            """,
            parameters={
                "source_id": relation.source_id,
                "target_id": relation.target_id,
                "type": relation.type.value,
            },
        )

    @staticmethod
    def _as_date(value) -> date | None:
        if value is None:
            return None
        if hasattr(value, "date") and not isinstance(value, date):
            return value.date()
        return value

    def _row_to_entity(self, row: list) -> Entity:
        from ontology.schema import EntityType

        (entity_id, type_, title, body, effective_date, superseded_date, allowed_depts, source) = row
        depts = tuple(allowed_depts.split(",")) if allowed_depts else ("ALL",)
        return Entity(
            id=entity_id,
            type=EntityType(type_),
            title=title,
            body=body,
            effective_date=self._as_date(effective_date),
            superseded_date=self._as_date(superseded_date),
            allowed_depts=depts,
            source=source or "",
        )

    def get_entity(self, entity_id: str) -> Entity | None:
        result = self._conn.execute(
            "MATCH (e:Entity {id: $id}) RETURN e.id, e.type, e.title, e.body, "
            "e.effective_date, e.superseded_date, e.allowed_depts, e.source",
            parameters={"id": entity_id},
        )
        if not result.has_next():
            return None
        return self._row_to_entity(result.get_next())

    def has_entity(self, entity_id: str) -> bool:
        result = self._conn.execute(
            "MATCH (e:Entity {id: $id}) RETURN e.id", parameters={"id": entity_id}
        )
        return result.has_next()

    def relations_from(self, entity_id: str, type: RelationType | None = None) -> list[Relation]:
        if type is not None:
            query = (
                "MATCH (a:Entity {id: $id})-[r:RelatesTo {type: $type}]->(b:Entity) "
                "RETURN r.type, b.id"
            )
            params = {"id": entity_id, "type": type.value}
        else:
            query = "MATCH (a:Entity {id: $id})-[r:RelatesTo]->(b:Entity) RETURN r.type, b.id"
            params = {"id": entity_id}
        result = self._conn.execute(query, parameters=params)
        out = []
        while result.has_next():
            rel_type, target_id = result.get_next()
            out.append(Relation(entity_id, RelationType(rel_type), target_id))
        return out

    def relations_to(self, entity_id: str, type: RelationType | None = None) -> list[Relation]:
        if type is not None:
            query = (
                "MATCH (a:Entity)-[r:RelatesTo {type: $type}]->(b:Entity {id: $id}) "
                "RETURN r.type, a.id"
            )
            params = {"id": entity_id, "type": type.value}
        else:
            query = "MATCH (a:Entity)-[r:RelatesTo]->(b:Entity {id: $id}) RETURN r.type, a.id"
            params = {"id": entity_id}
        result = self._conn.execute(query, parameters=params)
        out = []
        while result.has_next():
            rel_type, source_id = result.get_next()
            out.append(Relation(source_id, RelationType(rel_type), entity_id))
        return out
