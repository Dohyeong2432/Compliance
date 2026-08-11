"""내부 검토서(Chinese-wall 대상 문서) 연동 스텁.

실 연동에 필요한 것:
  - 사내 EDMS 검토서함 접근 권한 (부서별 문서함 구조 확인 필요)
  - 문서함 -> allowed_depts 매핑 규칙 (예: IB 검토서함 -> allowed_depts=("IB",))
  - 반드시 pipeline.masking.mask_pii를 거친 뒤 색인해야 함 — 이 커넥터가 반환하는
    RawDocument.body는 아직 마스킹되지 않은 원문이며, 마스킹은 ingest.py의 공통
    단계에서 REVIEW 타입에 대해 일괄 적용된다.
"""

from __future__ import annotations

from ontology.schema import EntityType
from pipeline.connectors.base import RawDocument, SourceConnector


class ReviewConnector(SourceConnector):
    entity_type = EntityType.REVIEW

    def __init__(self, documents: list[RawDocument] | None = None):
        self._documents = documents

    def fetch(self) -> list[RawDocument]:
        if self._documents is not None:
            return self._documents
        raise NotImplementedError(
            "ReviewConnector 실 연동 미구현: 사내 EDMS 검토서함 접근 권한이 필요합니다."
        )
