"""Tool schema and dispatch for the agent's single retrieval tool.

The dispatcher is the RBAC choke point on the tool-calling side: dept comes
only from the SessionContext bound when the dispatcher is constructed, never
from the tool call's own arguments. Even if the LLM (or a prompt injected
into retrieved document text) tries to slip a 'dept' argument into the tool
call, it is simply not read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent.sso import SessionContext
from knowledge.retriever import HybridRetriever, RetrievedDocument

SEARCH_KNOWLEDGE_TOOL_SCHEMA = {
    "name": "search_knowledge",
    "description": (
        "그룹 준법감시 지식베이스(법령/유권해석/제재사례/사내규정/검토서/FAQ)에서 "
        "질의와 관련된 문서를 검색합니다. 접근 권한(RBAC)은 세션에 고정되어 있으며 "
        "이 도구를 통해 우회할 수 없습니다. 과거 시점 기준으로 유효했던 법령/규정을 "
        "물어볼 때는 as_of를 지정하세요."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "검색할 자연어 질의"},
            "as_of": {
                "type": "string",
                "description": "질의 기준 시점 (YYYY-MM-DD). 생략 시 오늘 날짜 기준.",
            },
            "top_k": {"type": "integer", "description": "반환할 최대 문서 수 (기본 6)"},
        },
        "required": ["query"],
    },
}


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict[str, Any]
    result_ids: list[str]


class ToolDispatcher:
    def __init__(self, retriever: HybridRetriever, session: SessionContext):
        self.retriever = retriever
        self.session = session

    def dispatch(self, name: str, arguments: dict[str, Any]) -> tuple[list[RetrievedDocument], ToolCallRecord]:
        if name != "search_knowledge":
            raise ValueError(f"Unknown tool: {name}")

        query = str(arguments.get("query", "")).strip()
        top_k = int(arguments.get("top_k") or 6)
        raw_as_of = arguments.get("as_of")
        as_of = datetime.strptime(raw_as_of, "%Y-%m-%d").date() if raw_as_of else None

        documents = self.retriever.retrieve(
            query, dept=self.session.dept, as_of=as_of, top_k=top_k
        )
        record = ToolCallRecord(
            name=name,
            arguments={"query": query, "as_of": raw_as_of, "top_k": top_k},
            result_ids=[d.entity.id for d in documents],
        )
        return documents, record


def format_documents_for_llm(documents: list[RetrievedDocument]) -> str:
    if not documents:
        return "검색 결과가 없습니다. 이 사실을 사용자에게 명확히 알리고, 답을 지어내지 마세요."

    blocks = []
    for doc in documents:
        entity = doc.entity
        blocks.append(
            f"<document id=\"{entity.id}\" type=\"{entity.type.value}\" "
            f"title=\"{entity.title}\" effective_date=\"{entity.effective_date or ''}\">\n"
            f"{entity.body}\n"
            f"</document>"
        )
    return (
        "다음은 검색된 문서입니다. 답변에서 이 문서의 내용을 사용할 때는 반드시 "
        "해당 document id를 [[CITE:id]] 형식으로 표기하세요. 이 목록에 없는 id는 "
        "인용하지 마세요.\n\n" + "\n\n".join(blocks)
    )
