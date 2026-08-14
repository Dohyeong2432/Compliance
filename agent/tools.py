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
from ontology.schema import AUTHORITY_LABEL, EntityType

SEARCH_KNOWLEDGE_TOOL_SCHEMA = {
    "name": "search_knowledge",
    "description": (
        "그룹 준법감시 지식베이스(법령/유권해석/제재사례/사내규정/검토서/FAQ)에서 "
        "질의와 관련된 문서를 검색합니다. 접근 권한(RBAC)은 세션에 고정되어 있으며 "
        "이 도구를 통해 우회할 수 없습니다. 과거 시점 기준으로 유효했던 법령/규정을 "
        "물어볼 때는 as_of를 지정하세요. 한 번에 모든 소스를 훑기보다, source_types로 "
        "소스를 좁혀 여러 번 나눠 검색하면 근거를 더 깊이 확보할 수 있습니다."
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
            "source_types": {
                "type": "array",
                "items": {"type": "string", "enum": [t.value for t in EntityType]},
                "description": (
                    "검색 대상 소스를 한정합니다 (생략 시 전체). "
                    "law=법령, regulation=사내규정, interpretation=유권해석, "
                    "case=제재사례, review=내부검토서, faq=FAQ. "
                    "예: 근거 법령을 먼저 확정한 뒤 [\"interpretation\"]으로 좁혀 "
                    "해당 조항의 해석사례만 다시 검색."
                ),
            },
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
        raw_source_types = arguments.get("source_types")
        entity_types = _parse_source_types(raw_source_types)

        documents = self.retriever.retrieve(
            query, dept=self.session.dept, as_of=as_of, top_k=top_k, entity_types=entity_types
        )
        record = ToolCallRecord(
            name=name,
            arguments={
                "query": query,
                "as_of": raw_as_of,
                "top_k": top_k,
                "source_types": [t.value for t in entity_types] if entity_types else None,
            },
            result_ids=[d.entity.id for d in documents],
        )
        return documents, record


def _parse_source_types(raw: Any) -> tuple[EntityType, ...] | None:
    """LLM이 넘긴 source_types를 EntityType 튜플로 변환한다.

    알 수 없는 값은 조용히 버린다 -- 오타 하나로 턴 전체를 실패시키는 대신
    남은 유효한 타입으로 검색하는 편이 낫고, 유효한 값이 하나도 없으면
    필터를 걸지 않은 것(None = 전체 검색)과 같이 취급한다. 이 필터는 검색
    범위를 좁히기만 할 뿐 RBAC과는 무관하므로, 파싱이 느슨해도 권한이
    새지 않는다(권한 판정은 HybridRetriever._resolve 한 곳뿐).
    """
    if not isinstance(raw, (list, tuple)):
        return None
    parsed: list[EntityType] = []
    for value in raw:
        try:
            entity_type = EntityType(str(value).strip().lower())
        except ValueError:
            continue
        if entity_type not in parsed:
            parsed.append(entity_type)
    return tuple(parsed) or None


def format_documents_for_llm(documents: list[RetrievedDocument]) -> str:
    if not documents:
        return "검색 결과가 없습니다. 이 사실을 사용자에게 명확히 알리고, 답을 지어내지 마세요."

    blocks = []
    for doc in documents:
        entity = doc.entity
        # authority 속성으로 규범적 층위를 문서마다 명시한다 -- 검토서(내부
        # 의견)와 법령(강행규범)이 같은 무게로 인용되면 답변 자체가 위험해지고,
        # 시스템 프롬프트의 일반 지침만으로는 그 구분이 지켜진다고 볼 수 없다.
        blocks.append(
            f"<document id=\"{entity.id}\" type=\"{entity.type.value}\" "
            f"authority=\"{AUTHORITY_LABEL.get(entity.type, entity.type.value)}\" "
            f"title=\"{entity.title}\" effective_date=\"{entity.effective_date or ''}\">\n"
            f"{entity.body}\n"
            f"</document>"
        )
    return (
        "다음은 검색된 문서입니다(규범적 권위가 높은 순으로 정렬됨). 답변에서 이 문서의 "
        "내용을 사용할 때는 반드시 해당 document id를 [[CITE:id]] 형식으로 표기하세요. "
        "이 목록에 없는 id는 인용하지 마세요. 각 문서의 authority 속성이 그 문서의 "
        "규범적 지위를 나타내므로, 결론은 상위 규범(법령·사내규정)을 근거로 삼고 "
        "하위 자료(검토서·FAQ)는 참고 의견으로만 인용하세요.\n\n" + "\n\n".join(blocks)
    )
