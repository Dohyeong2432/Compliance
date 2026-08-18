from agent.contract_review import (
    CLAUSE_REVIEW_PROMPT_TEMPLATE,
    CONTRACT_REVIEW_MAX_TOOL_ITERATIONS,
    ClauseReview,
    review_contract,
)
from agent.harness import TOOL_LIMIT_EXCEEDED_MESSAGE, ComplianceAgent
from agent.llm_client import LLMResponse, ScriptedLLMClient, ToolCall
from agent.sso import SessionContext
from knowledge.embedder import HashEmbedder
from knowledge.graph_store import NetworkXGraphStore
from knowledge.retriever import HybridRetriever
from knowledge.vector_store import InMemoryVectorStore


def _final_response(text: str) -> LLMResponse:
    return LLMResponse(text=text, tool_call=None, raw=[{"type": "text", "text": text}])


def _tool_call_response(call_id: str, query: str) -> LLMResponse:
    return LLMResponse(
        text=None,
        tool_call=ToolCall(id=call_id, name="search_knowledge", arguments={"query": query}),
        raw=[{"type": "tool_use", "id": call_id, "name": "search_knowledge", "input": {"query": query}}],
    )


def _build_agent(script: list[LLMResponse], max_tool_iterations: int | None = None) -> ComplianceAgent:
    graph_store = NetworkXGraphStore()
    retriever = HybridRetriever(HashEmbedder(), InMemoryVectorStore(), graph_store)
    session = SessionContext(user_id="u1", dept="RETAIL")
    kwargs = {} if max_tool_iterations is None else {"max_tool_iterations": max_tool_iterations}
    return ComplianceAgent(ScriptedLLMClient(script), retriever, graph_store, session, **kwargs)


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


def test_review_contract_prompt_nudges_precedent_search():
    """계약검토 선례 DB(source_types=["precedent"])를 검색해보라는 안내가
    프롬프트에 포함돼야 LLM이 이 소스의 존재를 인지하고 활용할 수 있다."""
    text = "제1조(목적) 이 계약은 목적을 정한다."
    script = [_final_response("문제 없음")]
    agent = _build_agent(script)

    review_contract(text, agent)

    sent_prompt = agent.llm_client.calls[0]["messages"][0]["content"]
    assert 'source_types=["precedent"]' in sent_prompt


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


def test_clause_review_prompt_template_embeds_label_text_and_checklist():
    prompt = CLAUSE_REVIEW_PROMPT_TEMPLATE.format(
        label="제1조(목적)", text="이 계약은 목적을 정한다.", checklist="- 체크리스트 항목"
    )
    assert "제1조(목적)" in prompt
    assert "이 계약은 목적을 정한다." in prompt
    assert "- 체크리스트 항목" in prompt
    assert "위험도:" in prompt  # 구조화 필드 요구가 템플릿에 남아있는지


def test_review_contract_embeds_clause_type_checklist_in_prompt():
    """손해배상 조항이면 손해배상 체크리스트(민법 제398조 관련 문구)가
    실제로 LLM에 보내는 프롬프트에 들어가야 한다."""
    text = "제1조(손해배상) 위탁업무 수행 중 발생한 손해는 배상한다."
    script = [_final_response("문제 없음")]
    agent = _build_agent(script)

    review_contract(text, agent)

    sent_prompt = agent.llm_client.calls[0]["messages"][0]["content"]
    assert "민법 제398조" in sent_prompt


def test_review_contract_uses_default_checklist_for_unrecognized_clause_type():
    text = "제1조(정의) 이 계약에서 사용하는 용어의 정의는 다음과 같다."
    script = [_final_response("문제 없음")]
    agent = _build_agent(script)

    review_contract(text, agent)

    sent_prompt = agent.llm_client.calls[0]["messages"][0]["content"]
    assert "권리·의무가 대등하게" in sent_prompt


def test_contract_review_max_tool_iterations_allows_more_than_default_four():
    """계약서 검토 경로는 CONTRACT_REVIEW_MAX_TOOL_ITERATIONS(8)로 생성된
    에이전트를 쓰므로, 4회를 넘는 도구 호출도 TOOL_LIMIT_EXCEEDED_MESSAGE
    없이 끝까지 처리돼야 한다."""
    assert CONTRACT_REVIEW_MAX_TOOL_ITERATIONS > 4

    text = "제1조(손해배상) 손해배상 조항."
    script = [_tool_call_response(f"tc{i}", "손해배상") for i in range(5)] + [_final_response("검토 완료")]
    agent = _build_agent(script, max_tool_iterations=CONTRACT_REVIEW_MAX_TOOL_ITERATIONS)

    reviews = review_contract(text, agent)

    assert reviews[0].result.answer == "검토 완료"
    assert reviews[0].result.answer != TOOL_LIMIT_EXCEEDED_MESSAGE


def test_default_max_tool_iterations_would_have_failed_the_same_scenario():
    """위 테스트가 CONTRACT_REVIEW_MAX_TOOL_ITERATIONS 덕분에 통과한다는 걸
    확인하기 위한 대조군 -- 기본 4회 한도라면 같은 시나리오가 한도 초과
    메시지로 끝나야 한다."""
    text = "제1조(손해배상) 손해배상 조항."
    script = [_tool_call_response(f"tc{i}", "손해배상") for i in range(5)] + [_final_response("검토 완료")]
    agent = _build_agent(script)  # 기본값(4) 사용

    reviews = review_contract(text, agent)

    assert reviews[0].result.answer == TOOL_LIMIT_EXCEEDED_MESSAGE
