"""금융위원회/금융감독원 유권해석(질의회신) 연동 스텁.

실 연동에 필요한 것:
  - 금융위 법령해석 회신 게시판 및 금감원 질의응답 시스템 접근(공개 API 없는
    항목은 승인된 크롤링 또는 정기 다운로드 필요)
  - 회신 문서 -> 관련 법령 조항 매핑 규칙 (INTERPRETS 관계 생성용)
"""

from __future__ import annotations

from ontology.schema import EntityType
from pipeline.connectors.base import RawDocument, SourceConnector


class InterpretationConnector(SourceConnector):
    entity_type = EntityType.INTERPRETATION

    def __init__(self, documents: list[RawDocument] | None = None):
        self._documents = documents

    def fetch(self) -> list[RawDocument]:
        if self._documents is not None:
            return self._documents
        raise NotImplementedError(
            "InterpretationConnector 실 연동 미구현: 금융위/금감원 질의회신 접근 승인이 필요합니다."
        )
