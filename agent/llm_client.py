"""LLM client abstraction.

The harness talks to an LLMClient interface, not to a specific vendor SDK,
so the tool-calling loop, RBAC enforcement, and citation verification can
all be unit tested with ScriptedLLMClient — no network, no API key.
AnthropicLLMClient is the production implementation.
"""

from __future__ import annotations

import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    text: str | None
    tool_call: ToolCall | None
    raw: Any = None  # assistant-turn content blocks, replayed verbatim on the next call


class LLMClient(ABC):
    @abstractmethod
    def generate(self, system_prompt: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        ...


class ScriptedLLMClient(LLMClient):
    """Replays a fixed sequence of responses. For tests only."""

    def __init__(self, script: list[LLMResponse]):
        self._script = list(script)
        self.calls: list[dict] = []

    def generate(self, system_prompt: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        self.calls.append({"system_prompt": system_prompt, "messages": list(messages), "tools": tools})
        if not self._script:
            raise RuntimeError("ScriptedLLMClient script exhausted")
        return self._script.pop(0)


class AnthropicLLMClient(LLMClient):
    """Wraps the Anthropic Messages API tool-use flow for a single step."""

    def __init__(self, model: str = "claude-sonnet-5", api_key: str | None = None, max_tokens: int = 2048):
        import anthropic  # optional dependency, only needed for live calls

        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = model
        self.max_tokens = max_tokens

    def generate(self, system_prompt: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system_prompt,
            messages=messages,
            tools=tools,
        )
        tool_call = None
        text_parts = []
        for block in response.content:
            if block.type == "tool_use":
                tool_call = ToolCall(id=block.id, name=block.name, arguments=block.input)
            elif block.type == "text":
                text_parts.append(block.text)
        return LLMResponse(text="\n".join(text_parts) or None, tool_call=tool_call, raw=response.content)


class GeminiLLMClient(LLMClient):
    """Wraps the Gemini API (google-genai SDK) tool-use flow for a single step.

    ComplianceAgent.ask() (harness.py) builds its `messages` history in
    Anthropic's content-block shape no matter which LLMClient is plugged
    in -- this client rebuilds a Gemini `contents` list from that shape on
    every call instead of requiring the harness itself to be vendor-aware.
    `LLMResponse.raw` stores just enough of the Gemini function-call info
    (its name, to resolve a later tool_result's tool_use_id back to a
    function_response) to make that reconstruction possible; Gemini itself
    has no id concept for a function call, so a random one is minted here
    purely for that internal correlation.

    contents/tools/config are built as plain dicts (matching the *Dict
    TypedDict aliases the SDK accepts alongside its pydantic model types)
    so the request-shaping logic can be unit tested without google-genai
    installed -- only __init__ and generate() touch the actual SDK client.
    """

    def __init__(self, model: str = "gemini-3.6-flash", api_key: str | None = None, max_output_tokens: int = 2048):
        api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set; cannot construct GeminiLLMClient")

        from google import genai  # optional dependency, only needed for live calls

        self._client = genai.Client(api_key=api_key)
        self.model = model
        self.max_output_tokens = max_output_tokens

    def _build_tools(self, tools: list[dict]) -> list[dict]:
        return [
            {
                "function_declarations": [
                    {"name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"]}
                    for t in tools
                ]
            }
        ]

    def _build_contents(self, messages: list[dict]) -> list[dict]:
        contents = []
        id_to_name: dict[str, str] = {}
        for message in messages:
            role = message["role"]
            content = message["content"]
            if role == "user" and isinstance(content, str):
                contents.append({"role": "user", "parts": [{"text": content}]})
            elif role == "assistant":
                parts = []
                if content.get("text"):
                    parts.append({"text": content["text"]})
                function_call = content.get("function_call")
                if function_call:
                    id_to_name[function_call["id"]] = function_call["name"]
                    parts.append({"function_call": {"name": function_call["name"], "args": function_call["args"]}})
                contents.append({"role": "model", "parts": parts})
            else:  # role == "user" with a list of tool_result blocks appended by the harness
                parts = [
                    {
                        "function_response": {
                            "name": id_to_name[block["tool_use_id"]],
                            "response": {"result": block["content"]},
                        }
                    }
                    for block in content
                ]
                contents.append({"role": "user", "parts": parts})
        return contents

    def _parse_response(self, response) -> LLMResponse:
        parts = response.candidates[0].content.parts or []
        text_chunks = []
        function_call = None
        for part in parts:
            call = getattr(part, "function_call", None)
            if call is not None:
                function_call = {"id": uuid.uuid4().hex, "name": call.name, "args": dict(call.args or {})}
            elif getattr(part, "text", None):
                text_chunks.append(part.text)
        text = "\n".join(text_chunks) or None
        tool_call = (
            ToolCall(id=function_call["id"], name=function_call["name"], arguments=function_call["args"])
            if function_call
            else None
        )
        return LLMResponse(text=text, tool_call=tool_call, raw={"text": text, "function_call": function_call})

    def generate(self, system_prompt: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        response = self._client.models.generate_content(
            model=self.model,
            contents=self._build_contents(messages),
            config={
                "system_instruction": system_prompt,
                "tools": self._build_tools(tools),
                "max_output_tokens": self.max_output_tokens,
            },
        )
        return self._parse_response(response)
