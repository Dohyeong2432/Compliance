from agent.contract_review import CLAUSE_REVIEW_PROMPT_TEMPLATE, ClauseReview, review_contract
from agent.harness import ComplianceAgent
from agent.llm_client import LLMResponse, ScriptedLLMClient
from agent.sso import SessionContext
from knowledge.embedder import HashEmbedder
from knowledge.graph_store import NetworkXGraphStore
from knowledge.retriever import HybridRetriever
from knowledge.vector_store import InMemoryVectorStore


def _final_response(text: str) -> LLMResponse:
    return LLMResponse(text=text, tool_call=None, raw=[{"type": "text", "text": text}])


def _build_agent(script: list[LLMResponse]) -> ComplianceAgent:
    graph_store = NetworkXGraphStore()
    retriever = HybridRetriever(HashEmbedder(), InMemoryVectorStore(), graph_store)
    session = SessionContext(user_id="u1", dept="RETAIL")
    return ComplianceAgent(ScriptedLLMClient(script), retriever, graph_store, session)


def test_review_contract_calls_agent_once_per_clause():
    text = (
        "제1조(목적) 이 계약은 위수탁 업무 범위를 정한다.\n\n"
        "제2조(위탁업무의 범위) 위탁업무는 다음과 같다.\n\n"
        "제3조(계약기간) 이 계약의 유효기간은 1년으로 한다."
    )
    script = [_final_response("문제 없음"), _final_response("문제 없음"), _final_response("문제 없음")]
    agent = _build_agent(script)

    reviews = review_contract(text, agent)

    assert [r.label for r in reviews] == ["제1조(목적)", "제2조(위탁업무의 범위)", "제3조(계약기간)"]
    assert all(isinstance(r, ClauseReview) for r in reviews)
    assert all(r.result.answer == "문제 없음" for r in reviews)


def test_review_contract_includes_clause_label_and_text_in_prompt():
    """agent.ask()로 넘어가는 질의에 조항 라벨과 원문이 그대로 포함돼야
    LLM이 "이 조항"이 뭔지 알고 검토할 수 있다."""
    text = "제1조(목적) 이 계약은 목적을 정한다."
    script = [_final_response("문제 없음")]
    agent = _build_agent(script)

    review_contract(text, agent)

    sent_prompt = agent.llm_client.calls[0]["messages"][0]["content"]
    assert "제1조(목적)" in sent_prompt
    assert "이 계약은 목적을 정한다." in sent_prompt


def test_review_contract_falls_back_to_whole_document_without_article_headings():
    """"제N조(제목)" 구조가 없는 계약서(영문 계약, 단순 번호 목록 등)는 통째로
    한 건으로 검토돼야 한다 -- 사규 파서와 같은 폴백."""
    text = "This Agreement is made between Party A and Party B.\n1. Scope.\n2. Term."
    script = [_final_response("검토 완료")]
    agent = _build_agent(script)

    reviews = review_contract(text, agent)

    assert len(reviews) == 1
    assert reviews[0].label == "전체 본문"
    assert reviews[0].original_text == text


def test_clause_review_prompt_template_embeds_label_and_text():
    prompt = CLAUSE_REVIEW_PROMPT_TEMPLATE.format(label="제1조(목적)", text="이 계약은 목적을 정한다.")
    assert "제1조(목적)" in prompt
    assert "이 계약은 목적을 정한다." in prompt
