"""금융위원회/금융감독원 유권해석(질의회신)을 실제 크롤링한 데이터를 색인하는 커넥터.

크롤링 자체(사이트 접근, HTML 파싱, 페이지네이션)는 여기서 하지 않는다 --
공개 API가 없는 게시판형 자료이므로 어차피 사이트별 구현이 필요하고, 그건
호출 측이 작성한 fetch_items 콜백의 책임이다. 여기서는 그 콜백이 반환한
dict를 온톨로지(RawDocument/Relation)로 변환하는 것만 담당한다. dict 스키마는
pipeline/connectors/crawler_base.py 참고.

item에 "interprets": "law:<조항 id>" 또는 "regulation:<규정 id>" (또는 그
리스트)를 채우면 INTERPRETS 관계가 자동 생성되어, 해당 법령/규정 조회 시
1-hop 확장으로 관련 유권해석이 함께 노출된다.

사용 예:
    def crawl_interpretation_items() -> list[dict]:
        ...  # 금융위/금감원 질의회신 게시판 크롤링

    connector = InterpretationConnector(fetch_items=crawl_interpretation_items)
    pipeline.ingest_connector(connector)

documents=[...]를 넘기면(dev/test 전용) 크롤러 없이 고정된 RawDocument 목록을
그대로 반환한다 -- seed_data/seed.py가 이 경로를 쓴다.
"""

from __future__ import annotations

from typing import Any

from ontology.schema import EntityType, RelationType
from pipeline.connectors.crawler_base import CrawledSourceConnector


class InterpretationConnector(CrawledSourceConnector):
    entity_type = EntityType.INTERPRETATION

    def _convenience_relations(self, item: dict[str, Any]) -> list[tuple[RelationType, str]]:
        targets = item.get("interprets") or []
        if isinstance(targets, str):
            targets = [targets]
        return [(RelationType.INTERPRETS, target_id) for target_id in targets]
