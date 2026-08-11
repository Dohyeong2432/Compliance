"""준법감시부 FAQ 연동 스텁.

실 연동에 필요한 것:
  - 사내 위키/포털의 준법감시부 FAQ 게시판 API 또는 정기 export 방식 확정
  - FAQ -> 근거 법령/규정/해석 매핑 규칙 (ANSWERED_BY 관계 생성용)
"""

from __future__ import annotations

from ontology.schema import EntityType
from pipeline.connectors.base import RawDocument, SourceConnector


class FaqConnector(SourceConnector):
    entity_type = EntityType.FAQ

    def __init__(self, documents: list[RawDocument] | None = None):
        self._documents = documents

    def fetch(self) -> list[RawDocument]:
        if self._documents is not None:
            return self._documents
        raise NotImplementedError(
            "FaqConnector 실 연동 미구현: 사내 위키/포털 FAQ 게시판 접근 방식 확정이 필요합니다."
        )
