from datetime import date

from agent.sso import SessionContext
from agent.tools import ToolDispatcher, format_documents_for_llm
from knowledge.embedder import HashEmbedder
from knowledge.graph_store import NetworkXGraphStore
from knowledge.retriever import HybridRetriever
from knowledge.vector_store import InMemoryVectorStore, VectorRecord
from ontology.schema import Entity, EntityType

EMB = HashEmbedder()


def _build_retriever():
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    ib_doc = Entity("review:1", EntityType.REVIEW, "IB 전용 문서", "IB 전용 본문", allowed_depts=("IB",))
    graph_store.add_entity(ib_doc)
    vector_store.upsert(
        [VectorRecord(ib_doc.id, EMB.embed_one(ib_doc.title + ib_doc.body), ib_doc.body, allowed_depts=ib_doc.allowed_depts)]
    )
    return HybridRetriever(EMB, vector_store, graph_store)


def test_dispatcher_ignores_dept_argument_from_tool_call():
    """Even if the tool-call arguments try to smuggle a dept override, the
    dispatcher must only ever use the session's own dept."""
    retriever = _build_retriever()
    session = SessionContext(user_id="u1", dept="RETAIL")
    dispatcher = ToolDispatcher(retriever, session)

    documents, record = dispatcher.dispatch(
        "search_knowledge", {"query": "IB 전용 문서", "dept": "IB"}
    )

    assert documents == []
    assert record.result_ids == []


def test_dispatcher_honors_session_dept_that_has_access():
    retriever = _build_retriever()
    session = SessionContext(user_id="u1", dept="IB")
    dispatcher = ToolDispatcher(retriever, session)

    documents, record = dispatcher.dispatch("search_knowledge", {"query": "IB 전용 문서"})

    assert record.result_ids == ["review:1"]
    assert documents[0].entity.id == "review:1"


def test_dispatcher_parses_as_of_date():
    retriever = _build_retriever()
    session = SessionContext(user_id="u1", dept="IB")
    dispatcher = ToolDispatcher(retriever, session)

    _, record = dispatcher.dispatch(
        "search_knowledge", {"query": "IB 전용 문서", "as_of": "2024-01-01"}
    )
    assert record.arguments["as_of"] == "2024-01-01"


def test_dispatcher_rejects_unknown_tool():
    retriever = _build_retriever()
    session = SessionContext(user_id="u1", dept="IB")
    dispatcher = ToolDispatcher(retriever, session)

    import pytest

    with pytest.raises(ValueError):
        dispatcher.dispatch("delete_everything", {})


def test_format_documents_empty_tells_llm_not_to_fabricate():
    text = format_documents_for_llm([])
    assert "없습니다" in text
    assert "지어내지" in text


def test_format_documents_includes_ids_for_citation():
    retriever = _build_retriever()
    session = SessionContext(user_id="u1", dept="IB")
    documents, _ = ToolDispatcher(retriever, session).dispatch("search_knowledge", {"query": "IB 전용 문서"})
    text = format_documents_for_llm(documents)
    assert 'id="review:1"' in text


# ---------------------------------------------------------------------------
# 권위 위계 라벨 / source_types 필터
# ---------------------------------------------------------------------------


def _mixed_retriever():
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    for entity in (
        Entity("law:1", EntityType.LAW, "업무위탁 법령", "업무위탁 조항 본문"),
        Entity("review:2", EntityType.REVIEW, "업무위탁 검토서", "업무위탁 검토의견 본문"),
    ):
        graph_store.add_entity(entity)
        vector_store.upsert([VectorRecord(entity.id, EMB.embed_one(entity.title + entity.body), entity.body)])
    return HybridRetriever(EMB, vector_store, graph_store)


def test_format_documents_labels_each_document_with_its_normative_authority():
    """LLM이 층위를 알고 답하려면 문서 블록 자체에 권위가 실려야 한다 --
    시스템 프롬프트의 일반 지침만으로는 지켜진다고 볼 수 없다."""
    session = SessionContext(user_id="u1", dept="RETAIL")
    documents, _ = ToolDispatcher(_mixed_retriever(), session).dispatch(
        "search_knowledge", {"query": "업무위탁"}
    )
    text = format_documents_for_llm(documents)

    assert 'authority="법령 (강행규범)"' in text
    assert 'authority="내부 검토서 (참고 의견, 구속력 없음)"' in text


def test_dispatcher_passes_source_types_filter_through_to_retrieval():
    session = SessionContext(user_id="u1", dept="RETAIL")
    dispatcher = ToolDispatcher(_mixed_retriever(), session)

    _, record = dispatcher.dispatch(
        "search_knowledge", {"query": "업무위탁", "source_types": ["law"]}
    )

    assert record.result_ids == ["law:1"]
    assert record.arguments["source_types"] == ["law"]


def test_dispatcher_without_source_types_searches_every_source():
    session = SessionContext(user_id="u1", dept="RETAIL")
    dispatcher = ToolDispatcher(_mixed_retriever(), session)

    _, record = dispatcher.dispatch("search_knowledge", {"query": "업무위탁"})

    assert set(record.result_ids) == {"law:1", "review:2"}
    assert record.arguments["source_types"] is None


def test_dispatcher_drops_unknown_source_types_instead_of_failing_the_turn():
    """LLM이 오타 난 소스명을 하나 섞어 보냈다고 턴 전체를 실패시키는 것보다,
    남은 유효한 타입으로 검색하는 편이 낫다."""
    session = SessionContext(user_id="u1", dept="RETAIL")
    dispatcher = ToolDispatcher(_mixed_retriever(), session)

    _, record = dispatcher.dispatch(
        "search_knowledge", {"query": "업무위탁", "source_types": ["law", "판례", ""]}
    )

    assert record.arguments["source_types"] == ["law"]
    assert record.result_ids == ["law:1"]


def test_dispatcher_treats_all_invalid_source_types_as_no_filter():
    session = SessionContext(user_id="u1", dept="RETAIL")
    dispatcher = ToolDispatcher(_mixed_retriever(), session)

    _, record = dispatcher.dispatch(
        "search_knowledge", {"query": "업무위탁", "source_types": ["존재하지않는타입"]}
    )

    assert record.arguments["source_types"] is None
    assert set(record.result_ids) == {"law:1", "review:2"}


def test_dispatcher_ignores_non_list_source_types():
    session = SessionContext(user_id="u1", dept="RETAIL")
    dispatcher = ToolDispatcher(_mixed_retriever(), session)

    _, record = dispatcher.dispatch("search_knowledge", {"query": "업무위탁", "source_types": "law"})

    assert record.arguments["source_types"] is None
