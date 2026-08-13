from types import SimpleNamespace

import pytest

from agent.llm_client import GeminiLLMClient, ToolCall

SEARCH_TOOL = {
    "name": "search_knowledge",
    "description": "검색",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}


def _make_client():
    client = GeminiLLMClient.__new__(GeminiLLMClient)
    client.model = "gemini-2.5-flash"
    client.max_output_tokens = 2048
    return client


def test_gemini_llm_client_requires_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        GeminiLLMClient(api_key=None)


def test_build_tools_converts_anthropic_schema_to_function_declarations():
    client = _make_client()
    tools = client._build_tools([SEARCH_TOOL])
    assert tools == [
        {
            "function_declarations": [
                {
                    "name": "search_knowledge",
                    "description": "검색",
                    "parameters": SEARCH_TOOL["input_schema"],
                }
            ]
        }
    ]


def test_build_contents_first_user_turn_is_plain_text():
    client = _make_client()
    contents = client._build_contents([{"role": "user", "content": "질문입니다"}])
    assert contents == [{"role": "user", "parts": [{"text": "질문입니다"}]}]


def test_build_contents_round_trips_assistant_function_call_and_tool_result():
    client = _make_client()
    messages = [
        {"role": "user", "content": "자금세탁방지 관련 법령 알려줘"},
        {
            "role": "assistant",
            "content": {
                "text": None,
                "function_call": {"id": "call-1", "name": "search_knowledge", "args": {"query": "자금세탁방지"}},
            },
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call-1", "content": "검색 결과 문서 본문"},
            ],
        },
    ]

    contents = client._build_contents(messages)

    assert contents[0] == {"role": "user", "parts": [{"text": "자금세탁방지 관련 법령 알려줘"}]}
    assert contents[1] == {
        "role": "model",
        "parts": [{"function_call": {"name": "search_knowledge", "args": {"query": "자금세탁방지"}}}],
    }
    assert contents[2] == {
        "role": "user",
        "parts": [
            {
                "function_response": {
                    "name": "search_knowledge",
                    "response": {"result": "검색 결과 문서 본문"},
                }
            }
        ],
    }


def test_parse_response_extracts_function_call_as_tool_call():
    client = _make_client()
    response = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            function_call=SimpleNamespace(name="search_knowledge", args={"query": "은행법"}),
                            text=None,
                        )
                    ]
                )
            )
        ]
    )

    result = client._parse_response(response)

    assert result.text is None
    assert isinstance(result.tool_call, ToolCall)
    assert result.tool_call.name == "search_knowledge"
    assert result.tool_call.arguments == {"query": "은행법"}
    assert result.raw["function_call"]["name"] == "search_knowledge"
    assert result.raw["function_call"]["id"] == result.tool_call.id


def test_parse_response_captures_thought_signature_on_function_call_part():
    """Gemini 3.x(thinking) 모델은 함수 호출 part에 thought_signature를 함께
    돌려주고, 이걸 다음 턴에 그대로 되돌려주지 않으면 400 INVALID_ARGUMENT를
    낸다 -- 응답을 파싱할 때부터 놓치지 않고 잡아둬야 한다."""
    client = _make_client()
    response = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            function_call=SimpleNamespace(name="search_knowledge", args={"query": "은행법"}),
                            text=None,
                            thought_signature=b"opaque-signature-bytes",
                        )
                    ]
                )
            )
        ]
    )

    result = client._parse_response(response)

    assert result.raw["function_call"]["thought_signature"] == b"opaque-signature-bytes"


def test_build_contents_replays_thought_signature_on_function_call_part():
    client = _make_client()
    messages = [
        {"role": "user", "content": "질문"},
        {
            "role": "assistant",
            "content": {
                "text": None,
                "function_call": {
                    "id": "call-1",
                    "name": "search_knowledge",
                    "args": {"query": "은행법"},
                    "thought_signature": b"opaque-signature-bytes",
                },
            },
        },
    ]

    contents = client._build_contents(messages)

    assert contents[1]["parts"][0] == {
        "function_call": {"name": "search_knowledge", "args": {"query": "은행법"}},
        "thought_signature": b"opaque-signature-bytes",
    }


def test_parse_response_extracts_plain_text_when_no_function_call():
    client = _make_client()
    response = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[SimpleNamespace(function_call=None, text="답변입니다 [[CITE:doc-1]]")]
                )
            )
        ]
    )

    result = client._parse_response(response)

    assert result.text == "답변입니다 [[CITE:doc-1]]"
    assert result.tool_call is None
    assert result.raw == {"text": "답변입니다 [[CITE:doc-1]]", "function_call": None}
