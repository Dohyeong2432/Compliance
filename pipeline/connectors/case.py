"""금융감독원 제재정보공개시스템 연동 스텁.

실 연동에 필요한 것:
  - 금감원 제재정보공개(https://www.fss.or.kr) 정기 수집 방식 확정 (공개 API 부재로
    승인된 스크래핑 또는 정기 게시 자료 다운로드 필요)
  - 제재사례 -> 위반 법령/규정 매핑 규칙 (VIOLATES 관계 생성용)
"""

from __future__ import annotations

from ontology.schema import EntityType
from pipeline.connectors.base import RawDocument, SourceConnector


class CaseConnector(SourceConnector):
    entity_type = EntityType.CASE

    def __init__(self, documents: list[RawDocument] | None = None):
        self._documents = documents

    def fetch(self) -> list[RawDocument]:
        if self._documents is not None:
            return self._documents
        raise NotImplementedError(
            "CaseConnector 실 연동 미구현: 금감원 제재정보공개시스템 수집 방식 확정이 필요합니다."
        )
