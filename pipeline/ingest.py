"""Ingest orchestration: RawDocument (from any connector) -> ontology
Entity/Relation -> graph store + vector store.

REVIEW-type documents are always masked here, regardless of whether the
originating connector already did so — this is the one place ingest can't
be skipped by a connector bug.
"""

from __future__ import annotations

from knowledge.embedder import Embedder
from knowledge.graph_store import GraphStore
from knowledge.vector_store import VectorRecord, VectorStore
from ontology.schema import Entity, EntityType, Relation
from pipeline.connectors.base import RawDocument, SourceConnector
from pipeline.masking import mask_pii


class IngestPipeline:
    def __init__(self, embedder: Embedder, vector_store: VectorStore, graph_store: GraphStore):
        self.embedder = embedder
        self.vector_store = vector_store
        self.graph_store = graph_store

    def ingest_connector(self, connector: SourceConnector) -> int:
        return self.ingest_documents(connector.fetch())

    def ingest_documents(self, documents: list[RawDocument]) -> int:
        if not documents:
            return 0

        entities: list[Entity] = []
        for doc in documents:
            body = mask_pii(doc.body) if doc.entity_type == EntityType.REVIEW else doc.body
            entities.append(
                Entity(
                    id=doc.entity_id,
                    type=doc.entity_type,
                    title=doc.title,
                    body=body,
                    effective_date=doc.effective_date,
                    superseded_date=doc.superseded_date,
                    allowed_depts=doc.allowed_depts,
                    source=doc.source,
                    metadata=doc.metadata,
                )
            )

        vectors = self.embedder.embed([f"{e.title}\n{e.body}" for e in entities])

        for entity in entities:
            self.graph_store.add_entity(entity)

        for doc, entity in zip(documents, entities):
            for relation_type, target_id in doc.relations:
                self.graph_store.add_relation(Relation(entity.id, relation_type, target_id))

        self.vector_store.upsert(
            [
                VectorRecord(
                    entity_id=entity.id,
                    vector=vector,
                    text=entity.body,
                    allowed_depts=entity.allowed_depts,
                    effective_date=entity.effective_date,
                    superseded_date=entity.superseded_date,
                )
                for entity, vector in zip(entities, vectors)
            ]
        )

        return len(entities)
