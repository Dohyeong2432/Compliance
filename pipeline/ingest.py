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
from pipeline.citation_extraction import extract_citation_relations
from pipeline.connectors.base import RawDocument, SourceConnector
from pipeline.masking import mask_pii

# 이 타입들만 본문에서 법령 인용("「OO법」 제N조" 등)을 자동 스캔해 CITES
# 관계를 만든다 -- LAW/REGULATION 자체는 인용의 "대상"이지 "출처"가
# 아니므로 제외한다 (법이 다른 법을 인용하는 경우도 있지만 지금 범위 밖).
_AUTO_CITATION_SOURCE_TYPES = frozenset(
    {EntityType.INTERPRETATION, EntityType.CASE, EntityType.REVIEW, EntityType.FAQ}
)


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

    The cache key also folds in the embedder's class/model/dimension (see
    _embed_identity below), not just the text content -- switching
    GEMINI_EMBED_MODEL (e.g. 001 -> 002) or GEMINI_EMBED_DIMENSION mid-project
    must not let an unchanged document quietly keep serving a vector from the
    old model out of the cache, since vectors from two different models
    aren't comparable in the same vector space. Changing the embedder
    automatically busts the whole cache.
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
        # 캐시 히트 판정에 이 값을 텍스트와 함께 해시해 넣는다 -- GEMINI_EMBED_MODEL을
        # 001에서 002로 바꾸는 것처럼 임베더의 모델/차원을 바꾸면, 내용이 안 바뀐
        # 문서라도 예전 모델로 만든 벡터가 캐시에서 그대로 재사용돼 새 모델 벡터와
        # 섞여버린다(둘은 서로 다른 벡터공간이라 코사인 유사도 비교 자체가 무의미해짐).
        # 모델/차원이 바뀌면 이 식별자도 바뀌어서 모든 캐시가 자동으로 무효화된다.
        self._embed_identity = f"{type(embedder).__name__}:{getattr(embedder, 'model', '')}:{getattr(embedder, 'dimension', '')}"

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

        hashes = [
            hashlib.sha256(f"{self._embed_identity}\n{text}".encode("utf-8")).hexdigest() for text in texts
        ]
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
            if entity.type in _AUTO_CITATION_SOURCE_TYPES:
                citation_text = f"{entity.title}\n{entity.body}"
                for relation_type, target_id in extract_citation_relations(citation_text, self.graph_store):
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
