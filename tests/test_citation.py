from agent.citation import CitationGuard, UNVERIFIED_MARKER
from knowledge.graph_store import NetworkXGraphStore
from ontology.schema import Entity, EntityType


def _graph_with(entity_id: str) -> NetworkXGraphStore:
    store = NetworkXGraphStore()
    store.add_entity(Entity(entity_id, EntityType.LAW, "제목", "본문"))
    return store


def test_valid_citation_becomes_footnote():
    guard = CitationGuard(_graph_with("law:1"))
    result = guard.apply("적합성 원칙이 적용됩니다 [[CITE:law:1]].", retrieved_ids={"law:1"})

    assert "[1]" in result.text
    assert "law:1" in result.verified_ids
    assert result.rejected_ids == []
    assert "참고 문서" in result.text


def test_hallucinated_citation_is_flagged_not_dropped_or_trusted():
    guard = CitationGuard(NetworkXGraphStore())
    result = guard.apply("근거가 있습니다 [[CITE:made_up]].", retrieved_ids=set())

    assert UNVERIFIED_MARKER in result.text
    assert "made_up" not in result.text.split(UNVERIFIED_MARKER)[0]  # marker replaced the raw tag
    assert result.rejected_ids == ["made_up"]
    assert result.verified_ids == []


def test_citation_not_retrieved_this_turn_is_rejected_even_if_in_graph():
    """An id that genuinely exists in the graph but wasn't part of this
    turn's retrieval must still be rejected -- otherwise the LLM could cite
    arbitrary graph ids it merely guesses, defeating the point of grounding
    citations in what was actually retrieved and shown to it."""
    guard = CitationGuard(_graph_with("law:1"))
    result = guard.apply("[[CITE:law:1]]", retrieved_ids=set())

    assert result.rejected_ids == ["law:1"]
    assert result.verified_ids == []


def test_repeated_citation_reuses_same_footnote_number():
    guard = CitationGuard(_graph_with("law:1"))
    result = guard.apply("첫 언급 [[CITE:law:1]]. 두번째 언급 [[CITE:law:1]].", retrieved_ids={"law:1"})

    body = result.text.split("---")[0]
    assert body.count("[1]") == 2
    assert result.verified_ids == ["law:1"]


def test_no_citations_leaves_text_unchanged_and_no_footer():
    guard = CitationGuard(NetworkXGraphStore())
    result = guard.apply("일반적인 안내 문구입니다.", retrieved_ids=set())

    assert result.text == "일반적인 안내 문구입니다."
    assert "참고 문서" not in result.text
