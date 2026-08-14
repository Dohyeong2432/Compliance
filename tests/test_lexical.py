from knowledge.lexical import LexicalIndex, tokenize
from ontology.schema import EntityType


def test_tokenize_emits_whole_word_and_bigrams_for_hangul():
    """어절 전체는 정확 일치용(IDF 높음), 바이그램은 부분 일치 흡수용 --
    둘 다 있어야 바이그램만 쓸 때 생기는 오탐을 IDF가 눌러줄 수 있다."""
    tokens = tokenize("이해상충")
    assert "이해상충" in tokens
    assert {"이해", "해상", "상충"} <= set(tokens)


def test_tokenize_keeps_alphanumeric_tokens_whole():
    tokens = tokenize("RBAC 47")
    assert "rbac" in tokens  # 소문자 정규화
    assert "47" in tokens


def test_tokenize_is_case_insensitive():
    assert tokenize("Compliance") == tokenize("COMPLIANCE")


def test_search_finds_document_containing_the_exact_term():
    index = LexicalIndex()
    index.index("law:1", "자회사등 사이의 업무위탁에 관한 조항")
    index.index("law:2", "고령투자자 보호 기준에 관한 조항")

    matches = index.search("업무위탁", top_k=5)

    assert [m.entity_id for m in matches][:1] == ["law:1"]


def test_exact_term_document_outranks_partial_bigram_overlap():
    """바이그램만 겹치는 문서("해상보험"의 '해상')보다 어절이 통째로 일치하는
    문서가 위로 와야 한다 -- 바이그램 색인의 알려진 오탐을 어절 term의 IDF가
    제압하는지 확인."""
    index = LexicalIndex()
    index.index("law:exact", "이해상충 방지체계 운영에 관한 사항")
    index.index("law:partial", "해상보험 및 해상운송 관련 사항")

    matches = index.search("이해상충", top_k=5)

    assert matches[0].entity_id == "law:exact"


def test_search_returns_empty_when_index_is_empty():
    assert LexicalIndex().search("아무 질의") == []


def test_search_returns_empty_when_query_has_no_indexable_token():
    index = LexicalIndex()
    index.index("law:1", "본문")
    assert index.search("!!! ???") == []


def test_delete_removes_document_from_results():
    index = LexicalIndex()
    index.index("law:1", "업무위탁 관련 조항")
    assert index.search("업무위탁")

    index.delete("law:1")

    assert index.search("업무위탁") == []
    assert len(index) == 0


def test_delete_of_unknown_id_is_a_noop():
    index = LexicalIndex()
    index.delete("law:nope")  # must not raise
    assert len(index) == 0


def test_reindexing_same_id_replaces_old_content():
    """같은 id로 다시 색인하면 옛 본문의 posting이 남아 있으면 안 된다 --
    sync가 매 사이클 전체 문서를 재색인하므로 이게 새면 색인이 계속 부푼다."""
    index = LexicalIndex()
    index.index("law:1", "업무위탁 관련 조항")
    index.index("law:1", "겸직 제한 관련 조항")

    assert index.search("업무위탁") == []
    assert [m.entity_id for m in index.search("겸직")] == ["law:1"]
    assert len(index) == 1


def test_entity_types_filter_restricts_results_to_requested_sources():
    index = LexicalIndex()
    index.index("law:1", "업무위탁 관련 조항")
    index.index("review:1", "업무위탁 관련 내부 검토의견")

    law_only = index.search("업무위탁", top_k=5, entity_types=(EntityType.LAW,))

    assert [m.entity_id for m in law_only] == ["law:1"]


def test_entity_types_none_searches_everything():
    index = LexicalIndex()
    index.index("law:1", "업무위탁 관련 조항")
    index.index("review:1", "업무위탁 관련 내부 검토의견")

    matches = index.search("업무위탁", top_k=5, entity_types=None)

    assert {m.entity_id for m in matches} == {"law:1", "review:1"}


def test_search_respects_top_k():
    index = LexicalIndex()
    for i in range(5):
        index.index(f"law:{i}", "업무위탁 관련 조항")

    assert len(index.search("업무위탁", top_k=2)) == 2
