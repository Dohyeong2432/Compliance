"""Hybrid retrieval: lexical + vector recall, cross-checked against the graph.

Pipeline, in order:
  1. Article-citation direct match ("제N조") against entity titles — a pure
     number reference carries almost no semantic signal for an embedder, so
     this path is what makes exact article lookups reliable. Never reranked
     away: if the user named an article, that article is in the answer.
  2. Candidate recall from two independent channels:
       - BM25 lexical search (knowledge/lexical.py) — exact term matching,
         which dense vectors are weak at and legal text depends on.
       - Vector similarity search (already dept/date pre-filtered).
     The two score scales aren't comparable, so they're merged by Reciprocal
     Rank Fusion rather than by raw score.
  3. Graph existence check — a hit for an id the graph store doesn't know
     about is discarded as noise. This is the main anti-hallucination
     mechanism: nothing that isn't a real, ingested entity can be cited.
  4. Time resolution — the matched id is swapped for whichever version in
     its SUPERSEDES chain was actually in force at the query's as_of date.
  5. RBAC hard filter, re-applied after step 4 because the resolved version
     may not be the same entity the vector store already filtered.
  6. Reranking (knowledge/reranker.py) of the fused candidate pool. Recall
     channels score query and document separately; a reranker reads them
     together, which is what actually cuts the noise a 20-30 candidate pool
     would otherwise dump into the LLM's context.
  7. 1-hop graph expansion from the surviving documents, for related cases/
     interpretations/FAQs that give the agent grounding context even when
     they didn't independently score high enough on recall.
  8. Authority ordering — results are presented 법령 > 사내규정 > 유권해석 >
     제재사례 > 검토서 > FAQ (ontology.schema.AUTHORITY_RANK), so relevance
     decides *what* is retrieved and normative weight decides *what order the
     LLM reads it in*. Ranking a highly-similar internal review above the
     statute it comments on is a compliance risk, not just a ranking quirk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from knowledge.embedder import Embedder
from knowledge.graph_store import GraphStore
from knowledge.lexical import LexicalIndex
from knowledge.reranker import NoOpReranker, Reranker
from knowledge.vector_store import VectorStore
from ontology.schema import Entity, EntityType, authority_rank

# "제N조"/"제N조의M" 같은 조문 인용을 찾는다. 이런 질의는 그 자체로는 벡터
# 임베딩이 붙잡을 만한 의미 정보가 거의 없어서(실사용에서 확인: top_k를
# 20까지 늘려도 "제2조가 뭐야" 같은 질의가 상위권에 안 듦), title 문자열
# 직접 매칭으로 보강한다. law_go_kr.py가 만드는 title은 항상 "법령명
# 제N조(제목)"/"법령명 제N조의M(제목)" 형태로, 공백 없이 붙어 있다.
_ARTICLE_CITATION = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?")

# Reciprocal Rank Fusion 상수. BM25 점수와 코사인 유사도는 스케일이 전혀
# 달라 직접 더할 수 없으므로, 점수 대신 각 채널에서의 "순위"만 써서 합친다.
# 60은 RRF 원 논문 이래 사실상 표준값으로, 상위권 순위차를 과하게 벌리지
# 않으면서 하위권 기여를 완만하게 줄인다.
_RRF_K = 60


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
    reason: str  # "citation_match" | "lexical_match" | "vector_match" | "1hop_expansion"


class HybridRetriever:
    def __init__(
        self,
        embedder: Embedder,
        vector_store: VectorStore,
        graph_store: GraphStore,
        expansion_limit: int = 3,
        citation_match_limit: int = 10,
        lexical_index: LexicalIndex | None = None,
        reranker: Reranker | None = None,
    ):
        self.embedder = embedder
        self.vector_store = vector_store
        self.graph_store = graph_store
        self.expansion_limit = expansion_limit
        self.citation_match_limit = citation_match_limit
        self.lexical_index = lexical_index
        # 리랭커를 안 붙인 배포에서도 분기 없이 같은 경로를 타도록 기본값을
        # NoOp으로 둔다 -- 이 경우 문서 순서는 RRF 융합 순위 그대로다.
        self.reranker = reranker or NoOpReranker()

    def _resolve(
        self,
        entity_id: str,
        dept: str,
        as_of: date,
        entity_types: tuple[EntityType, ...] | None,
    ) -> Entity | None:
        """모든 검색 경로가 공유하는 단일 관문: 그래프 실재 확인 -> 시점
        버전 확정 -> RBAC -> 소스 타입 필터. 경로가 늘어도 권한 판정 지점은
        여기 하나뿐이어야 한다."""
        if not self.graph_store.has_entity(entity_id):
            return None  # vector index drifted ahead of the graph; don't trust it
        resolved = self.graph_store.resolve_effective_version(entity_id, as_of)
        if resolved is None:
            return None
        if not resolved.is_visible_to(dept):
            return None
        if entity_types is not None and resolved.type not in entity_types:
            return None
        return resolved

    def retrieve(
        self,
        query: str,
        dept: str,
        as_of: date | None = None,
        top_k: int = 6,
        entity_types: tuple[EntityType, ...] | None = None,
    ) -> list[RetrievedDocument]:
        if not dept:
            raise ValueError("dept is required for retrieval (RBAC cannot be skipped)")
        as_of = as_of or date.today()

        documents: list[RetrievedDocument] = []
        seen_ids: set[str] = set()

        # 1) 조문 인용("제N조") 직접 매칭을 먼저, 그리고 리랭킹 대상에서 제외해
        # 보호한다 -- 사용자가 조문 번호를 특정해 물었으면 그 조문은 무조건
        # 답변 근거에 들어가야 한다.
        for needle in _article_citation_needles(query):
            matched = self.graph_store.find_entities_by_title_substring(needle)
            for entity in matched[: self.citation_match_limit]:
                resolved = self._resolve(entity.id, dept, as_of, entity_types)
                if resolved is None or resolved.id in seen_ids:
                    continue
                seen_ids.add(resolved.id)
                documents.append(RetrievedDocument(resolved, 1.0, "citation_match"))

        # 2) 어휘(BM25) + 벡터 두 채널의 후보를 RRF로 융합
        pool = self._recall_candidates(query, dept, as_of, top_k, entity_types, seen_ids)

        # 3) 융합된 후보 풀을 리랭킹해 상위 top_k만 남긴다
        if pool:
            texts = [f"{doc.entity.title}\n{doc.entity.body}" for doc in pool]
            for item in self.reranker.rerank(query, texts, top_k):
                candidate = pool[item.index]
                seen_ids.add(candidate.entity.id)
                documents.append(RetrievedDocument(candidate.entity, item.score, candidate.reason))

        # 4) 살아남은 문서에서 1-hop 확장 (근거 보강)
        for doc in list(documents):
            related = self.graph_store.expand_related(doc.entity.id, limit=self.expansion_limit)
            for entity in related:
                if entity.id in seen_ids:
                    continue
                if not entity.is_visible_to(dept):
                    continue
                if not entity.is_effective_at(as_of):
                    continue
                if entity_types is not None and entity.type not in entity_types:
                    continue
                seen_ids.add(entity.id)
                documents.append(RetrievedDocument(entity, 0.0, "1hop_expansion"))

        # 5) 권위 위계 순으로 재배열 (같은 위계 안에서는 관련성 순)
        documents.sort(key=lambda doc: (authority_rank(doc.entity.type), -doc.score))
        return documents

    def _recall_candidates(
        self,
        query: str,
        dept: str,
        as_of: date,
        top_k: int,
        entity_types: tuple[EntityType, ...] | None,
        already_seen: set[str],
    ) -> list[RetrievedDocument]:
        """BM25와 벡터 검색 결과를 각 채널 내 순위 기반(RRF)으로 합쳐,
        점수 스케일이 다른 두 채널을 공정하게 섞은 후보 목록을 만든다."""
        fused_scores: dict[str, float] = {}
        candidates: dict[str, RetrievedDocument] = {}

        def absorb(entity_id: str, rank: int, reason: str) -> None:
            resolved = self._resolve(entity_id, dept, as_of, entity_types)
            if resolved is None or resolved.id in already_seen:
                return
            fused_scores[resolved.id] = fused_scores.get(resolved.id, 0.0) + 1.0 / (_RRF_K + rank)
            # 두 채널 모두에 걸린 문서는 먼저 등록된 reason을 유지한다 --
            # 어휘 검색을 먼저 흡수하므로 정확 용어 일치가 표시상 우선한다.
            candidates.setdefault(resolved.id, RetrievedDocument(resolved, 0.0, reason))

        if self.lexical_index is not None:
            for rank, match in enumerate(
                self.lexical_index.search(query, top_k=top_k, entity_types=entity_types), start=1
            ):
                absorb(match.entity_id, rank, "lexical_match")

        query_vector = self.embedder.embed_query(query)
        vector_matches = self.vector_store.search(
            query_vector, top_k=top_k, dept=dept, as_of=as_of, entity_types=entity_types
        )
        for rank, match in enumerate(vector_matches, start=1):
            absorb(match.entity_id, rank, "vector_match")

        return sorted(candidates.values(), key=lambda doc: fused_scores[doc.entity.id], reverse=True)
