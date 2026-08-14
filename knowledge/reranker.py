"""Cross-encoder reranking of retrieval candidates.

벡터 검색과 BM25는 각각 "질의 벡터 vs 문서 벡터", "용어 빈도"만 보고 점수를
매긴다 -- 질의와 문서를 같이 읽고 판단하지 않는다. 그래서 후보를 20~30개
모아 그대로 LLM에 넣으면 상당수가 노이즈이고, 정작 정답 문서는 그 사이에
묻힌다. 리랭커는 (질의, 문서) 쌍을 함께 입력받아 관련성을 다시 매기는
별도 모델이라 이 구간의 정확도를 크게 끌어올린다.

기본값은 NoOpReranker다 -- 외부 API 호출이 늘면 지연·비용도 늘기 때문에,
켜는 것은 배포 환경의 선택으로 남긴다(RERANKER_BACKEND=voyage). 꺼져 있어도
검색은 기존과 동일하게 동작한다.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence


@dataclass
class RerankedItem:
    index: int    # 입력 documents 리스트에서의 원래 위치
    score: float


class Reranker(ABC):
    @abstractmethod
    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankedItem]:
        """관련성 내림차순으로 최대 top_n개를 (원래 인덱스, 점수)로 반환."""
        ...


class NoOpReranker(Reranker):
    """리랭킹 없이 입력 순서를 그대로 유지한 채 top_n개로 자르기만 한다.
    리랭커를 끈 배포에서 HybridRetriever가 분기 없이 동일한 코드 경로를
    타도록 하기 위한 기본 구현 (점수는 상위일수록 크게, 순서 정보만 보존)."""

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankedItem]:
        return [RerankedItem(i, float(len(documents) - i)) for i in range(min(len(documents), top_n))]


class VoyageReranker(Reranker):
    """Voyage AI rerank API 래퍼.

    임베딩과 같은 VOYAGE_API_KEY를 쓰지만 별개의 엔드포인트/과금 대상이다.
    _parse_response는 SDK 객체와 dict 응답을 모두 받아들이도록 해 두어
    네트워크 없이 단위 테스트할 수 있다 (VoyageEmbedder와 같은 방식).
    """

    def __init__(self, api_key: str | None = None, model: str = "rerank-2.5"):
        self.api_key = api_key or os.environ.get("VOYAGE_API_KEY")
        if not self.api_key:
            raise RuntimeError("VOYAGE_API_KEY is not set; cannot construct VoyageReranker")
        self.model = model
        self._client = self._build_client()

    def _build_client(self):
        import voyageai  # optional dependency, only needed for live calls

        return voyageai.Client(api_key=self.api_key)

    @staticmethod
    def _parse_response(response) -> list[RerankedItem]:
        results = getattr(response, "results", None)
        if results is None and isinstance(response, dict):
            results = response.get("results")
        if results is None:
            raise ValueError("Voyage rerank response is missing 'results'")

        items: list[RerankedItem] = []
        for result in results:
            if isinstance(result, dict):
                items.append(RerankedItem(int(result["index"]), float(result["relevance_score"])))
            else:
                items.append(RerankedItem(int(result.index), float(result.relevance_score)))
        items.sort(key=lambda item: item.score, reverse=True)
        return items

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankedItem]:
        if not documents:
            return []
        response = self._client.rerank(
            query=query, documents=list(documents), model=self.model, top_k=min(top_n, len(documents))
        )
        return self._parse_response(response)
