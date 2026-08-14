from knowledge.graph_store import NetworkXGraphStore
from ontology.schema import Entity, EntityType, RelationType
from pipeline.citation_extraction import _find_law_citations, extract_citation_relations


def test_find_law_citations_bracketed_form():
    citations = _find_law_citations("「금융지주회사법」 제47조에 따라 위탁할 수 있다.")
    assert citations == [("금융지주회사법", "47", None)]


def test_find_law_citations_bare_form_without_brackets():
    citations = _find_law_citations("금융지주회사법 제15조는 업무 범위를 규정한다.")
    assert citations == [("금융지주회사법", "15", None)]


def test_find_law_citations_article_branch_number():
    citations = _find_law_citations("「자본시장법」 제9조의2에서 정의한다.")
    assert citations == [("자본시장법", "9", "2")]


def test_find_law_citations_same_law_reference_resolves_to_last_law_name():
    text = "「금융지주회사법」 제47조에 따라 위탁하고, 동법 제15조도 함께 검토했다."
    citations = _find_law_citations(text)
    assert citations == [("금융지주회사법", "47", None), ("금융지주회사법", "15", None)]


def test_find_law_citations_same_law_reference_with_no_prior_law_name_is_dropped():
    """"동법"이 나오기 전에 명시적 법령명이 한 번도 안 나왔으면(예: 문서
    앞부분이 잘려서 전달됐거나) 뭘 가리키는지 알 수 없으니 그냥 버려야
    한다 -- 엉뚱한 법에 잘못 연결하는 것보다 안전하다."""
    citations = _find_law_citations("동법 제15조도 함께 검토했다.")
    assert citations == []


def test_find_law_citations_no_citation_returns_empty():
    assert _find_law_citations("업무위탁과 관련된 일반적인 검토의견입니다.") == []


def test_extract_citation_relations_matches_existing_law_article():
    graph_store = NetworkXGraphStore()
    graph_store.add_entity(
        Entity(id="law:009374-47-0", type=EntityType.LAW, title="금융지주회사법 제47조(자회사등 사이의 업무위탁)", body="본문")
    )

    relations = extract_citation_relations("「금융지주회사법」 제47조에 따라 위탁했다.", graph_store)

    assert relations == [(RelationType.CITES, "law:009374-47-0")]


def test_extract_citation_relations_exact_article_only_not_similar_numbers():
    """"제2조" 인용이 "제20조"에는 안 걸려야 한다 -- retriever의 조문 직접
    매칭과 동일한 정확도 요구사항."""
    graph_store = NetworkXGraphStore()
    graph_store.add_entity(Entity(id="law:1-2-0", type=EntityType.LAW, title="금융지주회사법 제2조(정의)", body="본문"))
    graph_store.add_entity(Entity(id="law:1-20-0", type=EntityType.LAW, title="금융지주회사법 제20조(승인)", body="본문"))

    relations = extract_citation_relations("「금융지주회사법」 제2조에서 정의한다.", graph_store)

    assert relations == [(RelationType.CITES, "law:1-2-0")]


def test_extract_citation_relations_no_match_when_law_not_yet_indexed():
    """인용 대상 법령이 아직 크롤링 안 됐으면 그냥 건너뛴다 -- 나중에
    크롤링되면 다음 sync 사이클에 add_relation이 다시 시도되어 연결된다
    (add_relation은 멱등이라 매 사이클 재시도해도 안전함)."""
    graph_store = NetworkXGraphStore()
    relations = extract_citation_relations("「아직 없는 법」 제1조에 따라...", graph_store)
    assert relations == []


def test_extract_citation_relations_ignores_non_law_regulation_entities():
    """title이 우연히 같은 문자열을 담고 있어도(예: 다른 유권해석 제목),
    LAW/REGULATION이 아니면 인용 대상으로 잡으면 안 된다."""
    graph_store = NetworkXGraphStore()
    graph_store.add_entity(
        Entity(
            id="interpretation:x1",
            type=EntityType.INTERPRETATION,
            title="금융지주회사법 제47조 관련 유권해석 모음",
            body="본문",
        )
    )

    relations = extract_citation_relations("「금융지주회사법」 제47조에 따라...", graph_store)

    assert relations == []


def test_extract_citation_relations_dedupes_repeated_citation_of_same_article():
    graph_store = NetworkXGraphStore()
    graph_store.add_entity(
        Entity(id="law:009374-47-0", type=EntityType.LAW, title="금융지주회사법 제47조(자회사등 사이의 업무위탁)", body="본문")
    )
    text = "「금융지주회사법」 제47조에 따라... 이하 제47조의 취지를 다시 설명하면..."

    relations = extract_citation_relations(text, graph_store)

    assert relations == [(RelationType.CITES, "law:009374-47-0")]
