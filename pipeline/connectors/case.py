"""금융감독원 제재정보공개시스템 등을 실제 크롤링한 제재사례 데이터를 색인하는 커넥터.

크롤링 자체(사이트 접근, 게시판 파싱)는 여기서 하지 않는다 -- 공개 API가
없으므로 사이트별 구현이 필요하고, 그건 호출 측이 작성한 fetch_items 콜백의
책임이다. 여기서는 그 콜백이 반환한 dict를 온톨로지(RawDocument/Relation)로
변환하는 것만 담당한다. dict 스키마는 pipeline/connectors/crawler_base.py 참고.

item에 "violates": "law:<조항 id>" 또는 "regulation:<규정 id>" (또는 그
리스트)를 채우면 VIOLATES 관계가 자동 생성되어, 해당 법령/규정 조회 시
1-hop 확장으로 관련 제재사례가 함께 노출된다 (환각 방지 근거 강화).

사용 예:
    def crawl_case_items() -> list[dict]:
        ...  # 금감원 제재정보공개시스템 크롤링

    connector = CaseConnector(fetch_items=crawl_case_items)
    pipeline.ingest_connector(connector)

documents=[...]를 넘기면(dev/test 전용) 크롤러 없이 고정된 RawDocument 목록을
그대로 반환한다 -- seed_data/seed.py가 이 경로를 쓴다.
"""

from __future__ import annotations

from typing import Any

from ontology.schema import EntityType, RelationType
from pipeline.connectors.crawler_base import CrawledSourceConnector


class CaseConnector(CrawledSourceConnector):
    entity_type = EntityType.CASE

    def _convenience_relations(self, item: dict[str, Any]) -> list[tuple[RelationType, str]]:
        targets = item.get("violates") or []
        if isinstance(targets, str):
            targets = [targets]
        return [(RelationType.VIOLATES, target_id) for target_id in targets]
