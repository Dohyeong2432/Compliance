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

import re
from dataclasses import dataclass
from datetime import date

from knowledge.embedder import Embedder
from knowledge.graph_store import GraphStore
from knowledge.vector_store import VectorStore
from ontology.schema import Entity

# "제N조"/"제N조의M" 같은 조문 인용을 찾는다. 이런 질의는 그 자체로는 벡터
# 임베딩이 붙잡을 만한 의미 정보가 거의 없어서(실사용에서 확인: top_k를
# 20까지 늘려도 "제2조가 뭐야" 같은 질의가 상위권에 안 듦), title 문자열
# 직접 매칭으로 보강한다. law_go_kr.py가 만드는 title은 항상 "법령명
# 제N조(제목)"/"법령명 제N조의M(제목)" 형태로, 공백 없이 붙어 있다.
_ARTICLE_CITATION = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?")


def _article_citation_needles(query: str) -> list[str]:
    needles: list[str] = []
    for match in _ARTICLE_CITATION.finditer(query):
        jo_no, jo_br = match.group(1), match.group(2)
        needle = f"제{jo_no}조의{jo_br}" if jo_br else f"제{jo_no}조"
        if needle not in needles:
            needles.append(needle)
    return needles


@dataclass
class RetrievedDocument:
    entity: Entity
    score: float
    reason: str  # "vector_match" | "citation_match" | "1hop_expansion"


class HybridRetriever:
    def __init__(
        self,
        embedder: Embedder,
        vector_store: VectorStore,
        graph_store: GraphStore,
        expansion_limit: int = 3,
        citation_match_limit: int = 10,
    ):
        self.embedder = embedder
        self.vector_store = vector_store
        self.graph_store = graph_store
        self.expansion_limit = expansion_limit
        self.citation_match_limit = citation_match_limit

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

        documents: list[RetrievedDocument] = []
        seen_ids: set[str] = set()

        # 조문 인용("제N조") 직접 매칭을 벡터 검색보다 먼저, 우선적으로 채운다
        # -- 정확한 조문 번호 질의는 벡터 유사도로 안정적으로 못 찾는 게
        # 실사용에서 확인됐다(_ARTICLE_CITATION 주석 참고).
        for needle in _article_citation_needles(query):
            matched = self.graph_store.find_entities_by_title_substring(needle)
            for entity in matched[: self.citation_match_limit]:
                resolved = self.graph_store.resolve_effective_version(entity.id, as_of)
                if resolved is None or not resolved.is_visible_to(dept) or resolved.id in seen_ids:
                    continue
                seen_ids.add(resolved.id)
                documents.append(RetrievedDocument(resolved, 1.0, "citation_match"))

        query_vector = self.embedder.embed_query(query)
        candidates = self.vector_store.search(query_vector, top_k=top_k, dept=dept, as_of=as_of)

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
