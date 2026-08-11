"""Vector similarity backends.

Both stores apply the same two hard filters before ranking: RBAC (a record
whose allowed_depts doesn't include the caller's dept is invisible, full
stop) and time (a record isn't returned as being valid before its
effective_date or on/after its superseded_date). This is belt-and-suspenders
with the checks HybridRetriever repeats against the graph store — a vector
match should never be able to leak a document past RBAC even if the graph
cross-check were skipped.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from ontology.schema import ALL_DEPARTMENTS
from knowledge.embedder import Vector

# Calibrated for HashEmbedder's score distribution (cosine similarity over a
# 256-dim hashed n-gram space). MUST be re-tuned when switching to a real
# semantic embedder such as VoyageEmbedder — its score distribution is not
# comparable to the hashing trick's.
DEFAULT_MIN_SCORE = 0.05


@dataclass
class VectorRecord:
    entity_id: str
    vector: Vector
    text: str
    allowed_depts: tuple[str, ...] = (ALL_DEPARTMENTS,)
    effective_date: date | None = None
    superseded_date: date | None = None


@dataclass
class ScoredMatch:
    entity_id: str
    score: float


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _dept_visible(allowed_depts: Sequence[str], dept: str | None) -> bool:
    if dept is None or ALL_DEPARTMENTS in allowed_depts:
        return True
    return dept in allowed_depts


def _effective_at(effective_date: date | None, superseded_date: date | None, as_of: date | None) -> bool:
    if as_of is None:
        return True
    if effective_date is not None and as_of < effective_date:
        return False
    if superseded_date is not None and as_of >= superseded_date:
        return False
    return True


class VectorStore(ABC):
    @abstractmethod
    def upsert(self, records: Sequence[VectorRecord]) -> None:
        ...

    @abstractmethod
    def search(
        self,
        query_vector: Vector,
        top_k: int = 10,
        dept: str | None = None,
        as_of: date | None = None,
    ) -> list[ScoredMatch]:
        ...


class InMemoryVectorStore(VectorStore):
    def __init__(self, min_score: float = DEFAULT_MIN_SCORE):
        self._records: dict[str, VectorRecord] = {}
        self.min_score = min_score

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        for record in records:
            self._records[record.entity_id] = record

    def search(
        self,
        query_vector: Vector,
        top_k: int = 10,
        dept: str | None = None,
        as_of: date | None = None,
    ) -> list[ScoredMatch]:
        matches: list[ScoredMatch] = []
        for record in self._records.values():
            if not _dept_visible(record.allowed_depts, dept):
                continue
            if not _effective_at(record.effective_date, record.superseded_date, as_of):
                continue
            score = _cosine(query_vector, record.vector)
            if score < self.min_score:
                continue
            matches.append(ScoredMatch(record.entity_id, score))
        matches.sort(key=lambda m: m.score, reverse=True)
        return matches[:top_k]


class ChromaVectorStore(VectorStore):
    """Embedded (local, serverless) Chroma-backed vector store.

    Chroma's metadata filters only support scalar equality/comparison, not
    "value in list", so RBAC/date filtering here is done the same way as
    InMemoryVectorStore: fetch a broad candidate set from Chroma, then apply
    the same Python-side filters before truncating to top_k. This keeps
    behavior identical between backends.
    """

    def __init__(self, persist_directory: str, collection_name: str = "compliance_knowledge", min_score: float = DEFAULT_MIN_SCORE):
        import chromadb  # optional dependency

        self.min_score = min_score
        self._client = chromadb.PersistentClient(path=persist_directory)
        self._collection = self._client.get_or_create_collection(
            name=collection_name, metadata={"hnsw:space": "cosine"}
        )

    @staticmethod
    def _to_metadata(record: VectorRecord) -> dict:
        return {
            "allowed_depts": ",".join(record.allowed_depts),
            "effective_date": record.effective_date.isoformat() if record.effective_date else "",
            "superseded_date": record.superseded_date.isoformat() if record.superseded_date else "",
        }

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        if not records:
            return
        self._collection.upsert(
            ids=[r.entity_id for r in records],
            embeddings=[r.vector for r in records],
            documents=[r.text for r in records],
            metadatas=[self._to_metadata(r) for r in records],
        )

    def search(
        self,
        query_vector: Vector,
        top_k: int = 10,
        dept: str | None = None,
        as_of: date | None = None,
    ) -> list[ScoredMatch]:
        fetch_n = max(top_k * 4, 20)
        result = self._collection.query(query_embeddings=[query_vector], n_results=fetch_n)
        ids = result.get("ids", [[]])[0]
        distances = result.get("distances", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]

        matches: list[ScoredMatch] = []
        for entity_id, distance, metadata in zip(ids, distances, metadatas):
            score = 1.0 - distance  # cosine distance -> cosine similarity
            if score < self.min_score:
                continue
            allowed_depts = tuple(metadata.get("allowed_depts", ALL_DEPARTMENTS).split(","))
            if not _dept_visible(allowed_depts, dept):
                continue
            eff = date.fromisoformat(metadata["effective_date"]) if metadata.get("effective_date") else None
            sup = date.fromisoformat(metadata["superseded_date"]) if metadata.get("superseded_date") else None
            if not _effective_at(eff, sup, as_of):
                continue
            matches.append(ScoredMatch(entity_id, score))

        matches.sort(key=lambda m: m.score, reverse=True)
        return matches[:top_k]
