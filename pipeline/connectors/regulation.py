"""사내 규정/지침 관리시스템 연동 스텁.

실 연동에 필요한 것:
  - 사내 규정관리시스템(EDMS 등) 서비스 계정 및 API 접근 권한
  - 규정 개정 이력 -> SUPERSEDES 체인 매핑
  - 규정별 소관 부서 -> allowed_depts 매핑 규칙 (전사 공통 규정은 ALL)
"""

from __future__ import annotations

from ontology.schema import EntityType
from pipeline.connectors.base import RawDocument, SourceConnector


class RegulationConnector(SourceConnector):
    entity_type = EntityType.REGULATION

    def __init__(self, documents: list[RawDocument] | None = None):
        self._documents = documents

    def fetch(self) -> list[RawDocument]:
        if self._documents is not None:
            return self._documents
        raise NotImplementedError(
            "RegulationConnector 실 연동 미구현: 사내 규정관리시스템(EDMS) 접근 권한이 필요합니다."
        )
