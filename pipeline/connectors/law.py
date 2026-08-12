"""국가법령정보센터(law.go.kr) 등에서 실제 크롤링한 법령 데이터를 색인하는 커넥터.

이 클래스는 크롤링 자체(HTTP 호출, XML/HTML 파싱, OC 인증키 처리 등)를 하지
않는다 -- 그건 호출 측이 작성한 fetch_items 콜백의 책임이다. 여기서는 그
콜백이 반환한 dict를 온톨로지(RawDocument/Relation)로 변환하는 것만 담당한다.
dict 스키마는 pipeline/connectors/crawler_base.py 참고.

item에 "supersedes": "law:<이전 버전 external_id>" (또는 그 리스트)를 채우면
신법 -> 구법 SUPERSEDES 관계가 자동 생성되어 HybridRetriever의 시계열 판정에
바로 반영된다.

사용 예:
    def crawl_law_items() -> list[dict]:
        # law.go.kr Open API 호출 + XML 파싱은 여기서 직접 구현
        ...

    connector = LawConnector(fetch_items=crawl_law_items)
    pipeline.ingest_connector(connector)

documents=[...]를 넘기면(dev/test 전용) 크롤러 없이 고정된 RawDocument 목록을
그대로 반환한다 -- seed_data/seed.py가 이 경로를 쓴다.
"""

from __future__ import annotations

from typing import Any

from ontology.schema import EntityType, RelationType
from pipeline.connectors.crawler_base import CrawledSourceConnector


class LawConnector(CrawledSourceConnector):
    entity_type = EntityType.LAW

    def _convenience_relations(self, item: dict[str, Any]) -> list[tuple[RelationType, str]]:
        targets = item.get("supersedes") or []
        if isinstance(targets, str):
            targets = [targets]
        return [(RelationType.SUPERSEDES, target_id) for target_id in targets]
