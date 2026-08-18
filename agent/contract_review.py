"""계약서 초안을 조항 단위로 순회하며 ComplianceAgent에게 검토를 맡긴다.

계약서는 지식 그래프에 색인할 대상(ontology.EntityType)이 아니라 그때그때
검토하고 버리는 일회성 입력이다. 그래서 새 LLM 오케스트레이션을 만들지
않고, 조항마다 이미 검증된 agent.harness.ComplianceAgent.ask()를 그대로
호출한다 -- 도구 호출 루프, RBAC, 인용 검증([[CITE:id]] -> 각주+참고문서
목록), 감사 로그가 전부 조항 단위로 자동 재사용된다.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.contract_checklist import checklist_for_label
from agent.harness import AgentTurnResult, ComplianceAgent
from pipeline.korean_article_parser import split_into_articles

# 자유형식 지시("문제 있는지 검토하세요")만 주면 조항마다 답변 분량·형식이
# 들쭉날쭉해진다. 4개 필드를 강제해 검토 결과를 조항 간에 비교 가능하게
# 만들고, {checklist}로 조항 유형별 확인 사항을 함께 넣어 검토 깊이를
# 일정 수준 이상으로 끌어올린다.
CLAUSE_REVIEW_PROMPT_TEMPLATE = """아래는 검토 대상 계약서의 한 조항입니다. 관련 법령·사내규정을 검색해 \
이 조항을 검토하고, 반드시 아래 형식으로 답변하세요.

위험도: [상/중/하]
문제 조항: [문제 없으면 "해당 없음"]
근거: [관련 법령/사규, [[CITE:id]] 포함]
수정 제안: [문제 없으면 "수정 불요", 있으면 구체적 대안 문구]

이 조항 유형에서 특히 확인할 사항:
{checklist}

필요하면 source_types=["precedent"]로 유사한 과거 계약검토 사례도 검색해 참고하십시오.

[{label}]
{text}"""

# 계약서 조항은 여러 법령이 얽힌 손해배상/면책 조항처럼 근거 법령 확정 +
# 유권해석/제재사례 추가 조회가 한 조항에서 두 차례 이상 필요할 수 있다.
# /chat의 기본 4회(agent.harness.MAX_TOOL_ITERATIONS)로는 부족할 수 있어
# 계약서 검토 경로에서만 두 배로 올린다.
CONTRACT_REVIEW_MAX_TOOL_ITERATIONS = 8

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
        prompt = CLAUSE_REVIEW_PROMPT_TEMPLATE.format(
            label=label, text=clause_text, checklist=checklist_for_label(label)
        )
        result = agent.ask(prompt)
        reviews.append(ClauseReview(label=label, original_text=clause_text, result=result))

    return reviews
