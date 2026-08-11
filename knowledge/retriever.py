"""Hybrid retrieval: vector recall cross-checked against the graph.

Pipeline, in order:
  1. Vector store returns top-K candidates (already dept/date pre-filtered).
  2. Graph existence check — a vector hit for an id the graph store doesn't
     know about is discarded as noise. This is the main anti-hallucination
     mechanism: nothing that isn't a real, ingested entity can be cited.
  3. Time resolution — the matched id is swapped for whichever version in
     its SUPERSEDES chain was actually in force at the query's as_of date.
  4. RBAC hard filter, re-applied after step 3 because the resolved version
     may not be the same entity the vector store already filtered.
  5. 1-hop graph expansion from the surviving documents, for related cases/
     interpretations/FAQs that give the agent grounding context even when
     they didn't independently score high enough on the vector search.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from knowledge.embedder import Embedder
from knowledge.graph_store import GraphStore
from knowledge.vector_store import VectorStore
from ontology.schema import Entity


@dataclass
class RetrievedDocument:
    entity: Entity
    score: float
    reason: str  # "vector_match" | "1hop_expansion"


class HybridRetriever:
    def __init__(
        self,
        embedder: Embedder,
        vector_store: VectorStore,
        graph_store: GraphStore,
        expansion_limit: int = 3,
    ):
        self.embedder = embedder
        self.vector_store = vector_store
        self.graph_store = graph_store
        self.expansion_limit = expansion_limit

    def retrieve(
        self,
        query: str,
        dept: str,
        as_of: date | None = None,
        top_k: int = 6,
    ) -> list[RetrievedDocument]:
        if not dept:
            raise ValueError("dept is required for retrieval (RBAC cannot be skipped)")
        as_of = as_of or date.today()

        query_vector = self.embedder.embed_one(query)
        candidates = self.vector_store.search(query_vector, top_k=top_k, dept=dept, as_of=as_of)

        documents: list[RetrievedDocument] = []
        seen_ids: set[str] = set()

        for match in candidates:
            if not self.graph_store.has_entity(match.entity_id):
                continue  # vector index drifted ahead of the graph; don't trust it
            resolved = self.graph_store.resolve_effective_version(match.entity_id, as_of)
            if resolved is None:
                continue
            if not resolved.is_visible_to(dept):
                continue
            if resolved.id in seen_ids:
                continue
            seen_ids.add(resolved.id)
            documents.append(RetrievedDocument(resolved, match.score, "vector_match"))

        for doc in list(documents):
            related = self.graph_store.expand_related(doc.entity.id, limit=self.expansion_limit)
            for entity in related:
                if entity.id in seen_ids:
                    continue
                if not entity.is_visible_to(dept):
                    continue
                if not entity.is_effective_at(as_of):
                    continue
                seen_ids.add(entity.id)
                documents.append(RetrievedDocument(entity, 0.0, "1hop_expansion"))

        return documents
