from datetime import date

from agent.audit import AuditLogger
from agent.harness import TOOL_LIMIT_EXCEEDED_MESSAGE, ComplianceAgent
from agent.llm_client import LLMResponse, ScriptedLLMClient, ToolCall
from agent.sso import SessionContext
from knowledge.embedder import HashEmbedder
from knowledge.graph_store import NetworkXGraphStore
from knowledge.retriever import HybridRetriever
from knowledge.vector_store import InMemoryVectorStore, VectorRecord
from ontology.schema import Entity, EntityType

EMB = HashEmbedder()


def _build_retriever_and_graph():
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    law = Entity("law:1", EntityType.LAW, "적합성 원칙", "고령투자자 보호 조항", effective_date=date(2023, 1, 1))
    graph_store.add_entity(law)
    vector_store.upsert(
        [VectorRecord(law.id, EMB.embed_one(law.title + law.body), law.body, effective_date=law.effective_date)]
    )
    return HybridRetriever(EMB, vector_store, graph_store), graph_store


def _tool_call_response(call_id: str, query: str) -> LLMResponse:
    return LLMResponse(
        text=None,
        tool_call=ToolCall(id=call_id, name="search_knowledge", arguments={"query": query}),
        raw=[{"type": "tool_use", "id": call_id, "name": "search_knowledge", "input": {"query": query}}],
    )


def _final_response(text: str) -> LLMResponse:
    return LLMResponse(text=text, tool_call=None, raw=[{"type": "text", "text": text}])


def test_full_turn_verifies_valid_citation_and_flags_hallucination(tmp_path):
    retriever, graph_store = _build_retriever_and_graph()
    session = SessionContext(user_id="u1", dept="RETAIL")
    script = [
        _tool_call_response("tc1", "적합성 원칙"),
        _final_response("적합성 원칙은 이렇습니다 [[CITE:law:1]]. 또한 [[CITE:fake]]도 참고."),
    ]
    llm = ScriptedLLMClient(script)
    logger = AuditLogger(str(tmp_path / "audit.jsonl"))
    agent = ComplianceAgent(llm, retriever, graph_store, session, audit_logger=logger)

    result = agent.ask("적합성 원칙이 뭐야?")

    assert "law:1" in result.verified_citations
    assert "fake" in result.rejected_citations
    assert "[1]" in result.answer
    assert (tmp_path / "audit.jsonl").exists()


def test_tool_iteration_limit_produces_safe_fallback_message():
    retriever, graph_store = _build_retriever_and_graph()
    session = SessionContext(user_id="u1", dept="RETAIL")
    # Always returns a tool call, never a final answer -- should hit the cap.
    script = [_tool_call_response(f"tc{i}", "적합성 원칙") for i in range(10)]
    llm = ScriptedLLMClient(script)
    agent = ComplianceAgent(llm, retriever, graph_store, session, max_tool_iterations=2)

    result = agent.ask("적합성 원칙이 뭐야?")

    assert result.answer == TOOL_LIMIT_EXCEEDED_MESSAGE
    assert len(result.tool_calls) == 2


def test_dispatcher_enforces_rbac_even_if_llm_asks_for_other_dept_docs():
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    ib_doc = Entity("review:1", EntityType.REVIEW, "IB 문서", "IB 전용 내용", allowed_depts=("IB",))
    graph_store.add_entity(ib_doc)
    vector_store.upsert(
        [VectorRecord(ib_doc.id, EMB.embed_one(ib_doc.title + ib_doc.body), ib_doc.body, allowed_depts=ib_doc.allowed_depts)]
    )
    retriever = HybridRetriever(EMB, vector_store, graph_store)

    session = SessionContext(user_id="u1", dept="RETAIL")
    script = [
        _tool_call_response("tc1", "IB 문서"),
        _final_response("검색 결과가 없어 답변드릴 수 없습니다."),
    ]
    agent = ComplianceAgent(ScriptedLLMClient(script), retriever, graph_store, session)

    result = agent.ask("IB 전용 문서를 보여줘")

    assert result.tool_calls[0].result_ids == []
