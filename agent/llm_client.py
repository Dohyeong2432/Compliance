"""LLM client abstraction.

The harness talks to an LLMClient interface, not to a specific vendor SDK,
so the tool-calling loop, RBAC enforcement, and citation verification can
all be unit tested with ScriptedLLMClient — no network, no API key.
AnthropicLLMClient is the production implementation.
"""

from __future__ import annotations

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
