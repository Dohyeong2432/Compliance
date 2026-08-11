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
