"""Automatic law-article citation extraction for cross-source linking.

유권해석/제재사례/검토서/FAQ 문서를 사람이 일일이 "이건 제47조에 관한
거야"라고 라벨링할 수는 없다 -- 문서가 수천 건이면 비현실적이다. 대신 한국
법률 문서는 관행적으로 「금융지주회사법」 제47조, 동법 제15조 같은 식으로
관련 조항을 본문에 명시적으로 인용한다. 이 모듈은 그 인용을 정규식으로
찾아 이미 색인된 법령/규정 조항 entity와 자동으로 매칭해 CITES 관계를
만든다.

pipeline/ingest.py가 매 ingest 사이클마다 이 함수를 호출하고,
GraphStore.add_relation()은 멱등(같은 source/type/target 재호출 시
중복 안 생김)이라 -- 인용 대상 법령이 인용하는 문서보다 나중에 크롤링돼도
다음 sync 사이클에 자동으로 연결된다. 순서를 신경 쓸 필요가 없다.
"""

from __future__ import annotations

import re

from knowledge.graph_store import GraphStore
from ontology.schema import EntityType, RelationType

# 세 가지 인용 형태를 하나의 패턴으로 잡는다:
#   1. 「법령명」 제N조                 -- 가장 명확한 형태
#   2. 법령명(법/법률/시행령/시행규칙/규정/준칙으로 끝남) 제N조  -- 괄호 없는 형태
#   3. 동법/같은 법 제N조                -- 직전에 언급된 법령명을 그대로 이어받음
_CITATION = re.compile(
    r"(?:"
    r"「(?P<bracketed>[^」]{2,40})」"
    r"|(?P<bare>[가-힣]{2,30}(?:법률|시행령|시행규칙|법|규정|준칙))"
    r"|(?P<same>동법|같은\s*법)"
    r")"
    r"\s*제\s*(?P<jo>\d+)\s*조(?:\s*의\s*(?P<jo_br>\d+))?"
)


def _find_law_citations(text: str) -> list[tuple[str, str, str | None]]:
    """텍스트에서 (법령명, 조번호, 조가지번호)를 등장 순서대로 추출한다.
    "동법"/"같은 법"은 그 앞에 마지막으로 등장한 명시적 법령명으로 치환한다
    (앞에 명시적 법령명이 아직 없었다면 그 등장은 그냥 버린다)."""
    citations: list[tuple[str, str, str | None]] = []
    last_law_name: str | None = None
    for m in _CITATION.finditer(text):
        law_name = m.group("bracketed") or m.group("bare")
        if law_name:
            last_law_name = law_name
        elif m.group("same"):
            law_name = last_law_name
        if law_name:
            citations.append((law_name, m.group("jo"), m.group("jo_br")))
    return citations


def extract_citation_relations(text: str, graph_store: GraphStore) -> list[tuple[RelationType, str]]:
    """본문에서 찾은 법령 인용을 이미 색인된 LAW/REGULATION entity에 매칭해
    (RelationType.CITES, entity_id) 목록을 반환한다.

    law_go_kr.py가 만드는 조문 title은 항상 "법령명 제N조(제목)" 형태라
    "법령명 제N조"를 title 부분문자열로 찾으면 된다(retriever.py의
    _article_citation_needles와 같은 규칙). 일치하는 entity가 없으면(법령이
    아직 안 크롤링됐거나, 인용문의 법령명 표기가 실제 title과 미묘하게
    다르거나) 그 인용은 그냥 조용히 건너뛴다 -- 자동 보조 기능이라 재현율이
    100%일 필요는 없고, 잘못 연결하는 것보다 놓치는 게 안전하다."""
    relations: list[tuple[RelationType, str]] = []
    seen_targets: set[str] = set()
    for law_name, jo_no, jo_br_no in _find_law_citations(text):
        needle = f"{law_name} 제{jo_no}조" + (f"의{jo_br_no}" if jo_br_no else "")
        for entity in graph_store.find_entities_by_title_substring(needle):
            if entity.type not in (EntityType.LAW, EntityType.REGULATION):
                continue
            if entity.id in seen_targets:
                continue
            seen_targets.add(entity.id)
            relations.append((RelationType.CITES, entity.id))
    return relations
