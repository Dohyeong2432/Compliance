"""The agent turn loop: tool-calling LLM + RBAC-locked retrieval + citation
verification + audit logging, wired together.

Nothing here ever reads a department from anywhere but the SessionContext
passed into ComplianceAgent.__init__ (itself only constructible via
agent.sso.build_session_context from a verified JWT). The LLM can ask for
whatever it wants in a tool call; ToolDispatcher ignores any dept it tries
to pass. The LLM can emit any [[CITE:id]] marker it wants; CitationGuard
only honors ids that were actually retrieved and still resolve in the
graph. Hallucination and privilege escalation are structural constraints
here, not prompt instructions the model could be talked out of.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.audit import AuditLogger
from agent.citation import CitationGuard
from agent.llm_client import LLMClient
from agent.prompts import SYSTEM_PROMPT
from agent.sso import SessionContext
from agent.tools import SEARCH_KNOWLEDGE_TOOL_SCHEMA, ToolCallRecord, ToolDispatcher, format_documents_for_llm
from knowledge.graph_store import GraphStore
from knowledge.retriever import HybridRetriever

MAX_TOOL_ITERATIONS = 4
TOOL_LIMIT_EXCEEDED_MESSAGE = (
    "요청 처리에 필요한 도구 호출 한도를 초과했습니다. 질의를 더 구체적으로 나눠서 다시 시도해 주세요."
)


@dataclass
class AgentTurnResult:
    answer: str
    verified_citations: list[str] = field(default_factory=list)
    rejected_citations: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)


class ComplianceAgent:
    def __init__(
        self,
        llm_client: LLMClient,
        retriever: HybridRetriever,
        graph_store: GraphStore,
        session: SessionContext,
        audit_logger: AuditLogger | None = None,
        max_tool_iterations: int = MAX_TOOL_ITERATIONS,
    ):
        self.llm_client = llm_client
        self.dispatcher = ToolDispatcher(retriever, session)
        self.citation_guard = CitationGuard(graph_store)
        self.session = session
        self.audit_logger = audit_logger
        self.max_tool_iterations = max_tool_iterations

    def ask(self, user_message: str) -> AgentTurnResult:
        messages: list[dict] = [{"role": "user", "content": user_message}]
        retrieved_ids: set[str] = set()
        tool_calls: list[ToolCallRecord] = []
        final_text = TOOL_LIMIT_EXCEEDED_MESSAGE

        for _ in range(self.max_tool_iterations):
            response = self.llm_client.generate(SYSTEM_PROMPT, messages, [SEARCH_KNOWLEDGE_TOOL_SCHEMA])

            if response.tool_call is None:
                final_text = response.text or ""
                break

            documents, record = self.dispatcher.dispatch(
                response.tool_call.name, response.tool_call.arguments
            )
            tool_calls.append(record)
            retrieved_ids.update(record.result_ids)

            messages.append({"role": "assistant", "content": response.raw})
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": response.tool_call.id,
                            "content": format_documents_for_llm(documents),
                        }
                    ],
                }
            )

        citation_result = self.citation_guard.apply(final_text, retrieved_ids)

        if self.audit_logger is not None:
            self.audit_logger.log(
                session=self.session,
                user_message=user_message,
                tool_calls=tool_calls,
                verified=citation_result.verified_ids,
                rejected=citation_result.rejected_ids,
                answer=citation_result.text,
            )

        return AgentTurnResult(
            answer=citation_result.text,
            verified_citations=citation_result.verified_ids,
            rejected_citations=citation_result.rejected_ids,
            tool_calls=tool_calls,
        )
