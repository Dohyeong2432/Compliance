"""계약서 초안을 조항 단위로 순회하며 ComplianceAgent에게 검토를 맡긴다.

계약서는 지식 그래프에 색인할 대상(ontology.EntityType)이 아니라 그때그때
검토하고 버리는 일회성 입력이다. 그래서 새 LLM 오케스트레이션을 만들지
않고, 조항마다 이미 검증된 agent.harness.ComplianceAgent.ask()를 그대로
호출한다 -- 도구 호출 루프, RBAC, 인용 검증([[CITE:id]] -> 각주+참고문서
목록), 감사 로그가 전부 조항 단위로 자동 재사용된다.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.harness import AgentTurnResult, ComplianceAgent
from pipeline.korean_article_parser import split_into_articles

CLAUSE_REVIEW_PROMPT_TEMPLATE = """아래는 검토 대상 계약서의 한 조항입니다. 관련 법령·사내규정을 검색해 \
이 조항에 법규 위반 소지, 사내규정과의 불일치, 불리한 조건이 없는지 검토하세요. \
문제가 없다면 그렇다고 명시하세요.

[{label}]
{text}"""

_WHOLE_DOCUMENT_LABEL = "전체 본문"


@dataclass
class ClauseReview:
    label: str
    original_text: str
    result: AgentTurnResult


def review_contract(text: str, agent: ComplianceAgent) -> list[ClauseReview]:
    """계약서 본문을 조항 단위로 나눠 각각 agent.ask()로 검토받는다.

    "제N조(제목)" 구조가 하나도 없는 계약서(영문 계약, 단순 번호 목록 등)는
    split_into_articles가 빈 리스트를 반환하므로, 전체 본문을 단일 조항으로
    취급해 통째로 검토한다 -- LocalFileRegulationConnector가 조문 구조 없는
    사규 파일을 파일 전체 단위로 색인하는 것과 같은 폴백이다."""
    clauses = split_into_articles(text)
    if not clauses:
        clauses = [(_WHOLE_DOCUMENT_LABEL, text.strip())]

    reviews = []
    for label, clause_text in clauses:
        prompt = CLAUSE_REVIEW_PROMPT_TEMPLATE.format(label=label, text=clause_text)
        result = agent.ask(prompt)
        reviews.append(ClauseReview(label=label, original_text=clause_text, result=result))

    return reviews
