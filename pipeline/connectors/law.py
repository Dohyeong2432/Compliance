"""국가법령정보센터(law.go.kr) Open API 연동 스텁.

실 연동에 필요한 것:
  - law.go.kr Open API 사용자 인증키(OC) 발급 (https://open.law.go.kr)
  - 대상 법령 화이트리스트 (자본시장법, 금융소비자보호법, 특정금융정보법 등)
  - 개정 이력 API(효력범위 등)로 신구법 SUPERSEDES 체인 자동 구성

documents가 주어지면(개발/테스트용) 해당 목록을 그대로 반환하고, 그렇지 않으면
실 연동이 아직 구현되지 않았음을 명시적으로 알린다.
"""

from __future__ import annotations

from ontology.schema import EntityType
from pipeline.connectors.base import RawDocument, SourceConnector


class LawConnector(SourceConnector):
    entity_type = EntityType.LAW

    def __init__(self, documents: list[RawDocument] | None = None, oc_key: str | None = None):
        self._documents = documents
        self.oc_key = oc_key

    def fetch(self) -> list[RawDocument]:
        if self._documents is not None:
            return self._documents
        raise NotImplementedError(
            "LawConnector 실 연동 미구현: 국가법령정보센터 OC 인증키가 필요합니다. "
            "https://open.law.go.kr 에서 발급 후 oc_key로 전달하세요."
        )
