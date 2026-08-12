"""Ingest orchestration: RawDocument (from any connector) -> ontology
Entity/Relation -> graph store + vector store.

REVIEW-type documents are always masked here, regardless of whether the
originating connector already did so — this is the one place ingest can't
be skipped by a connector bug.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from knowledge.embedder import Embedder, Vector
from knowledge.graph_store import GraphStore
from knowledge.vector_store import VectorRecord, VectorStore
from ontology.schema import Entity, EntityType, Relation
from pipeline.connectors.base import RawDocument, SourceConnector
from pipeline.masking import mask_pii


class IngestPipeline:
    """embed_cache_path, if given, skips re-calling the embedder for a
    document whose exact embedded text ("{title}\\n{body}") is unchanged
    since the last ingest -- the embedder is an external, often
    rate-limited/paid API call (Voyage/Gemini), and re-syncing periodically
    re-submits every document from every source every cycle regardless of
    whether its content actually changed (see pipeline/sync.py). Documents
    are still re-added to graph_store/vector_store every cycle either way --
    those are cheap local upserts, and an in-memory VectorStore has nothing
    to reuse across a process restart even when the embedding itself was
    cached.
    """

    def __init__(
        self,
        embedder: Embedder,
        vector_store: VectorStore,
        graph_store: GraphStore,
        embed_cache_path: str | Path | None = None,
    ):
        self.embedder = embedder
        self.vector_store = vector_store
        self.graph_store = graph_store
        self.embed_cache_path = Path(embed_cache_path) if embed_cache_path else None
        self._embed_cache: dict[str, dict[str, Any]] = self._load_embed_cache()

    def _load_embed_cache(self) -> dict[str, dict[str, Any]]:
        if self.embed_cache_path is None or not self.embed_cache_path.exists():
            return {}
        try:
            return json.loads(self.embed_cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_embed_cache(self) -> None:
        if self.embed_cache_path is None:
            return
        self.embed_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.embed_cache_path.write_text(json.dumps(self._embed_cache, ensure_ascii=False), encoding="utf-8")

    def _embed_with_cache(self, entities: list[Entity], texts: list[str]) -> list[Vector]:
        if self.embed_cache_path is None:
            return self.embedder.embed(texts)

        hashes = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts]
        vectors: list[Vector | None] = [None] * len(entities)
        stale_indices = [
            i
            for i, entity in enumerate(entities)
            if self._embed_cache.get(entity.id, {}).get("hash") != hashes[i]
        ]
        for i, entity in enumerate(entities):
            if i not in stale_indices:
                vectors[i] = self._embed_cache[entity.id]["vector"]

        if stale_indices:
            fresh_vectors = self.embedder.embed([texts[i] for i in stale_indices])
            for i, vector in zip(stale_indices, fresh_vectors):
                vectors[i] = vector
                self._embed_cache[entities[i].id] = {"hash": hashes[i], "vector": vector}
            self._save_embed_cache()

        return vectors  # type: ignore[return-value]  -- every slot filled above

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

        vectors = self._embed_with_cache(entities, [f"{e.title}\n{e.body}" for e in entities])

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
