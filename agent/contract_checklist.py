"""계약 조항 유형별 검토 체크리스트.

손해배상/해지/관할/비밀유지/면책/업무위탁 등 조항 유형마다 실무에서 확인하는
전형적 위험 포인트가 다른데, 일반론("법규 위반 소지가 있는지 검토하세요")만
주면 LLM이 무엇을 봐야 할지 스스로 정하게 되어 검토 깊이가 조항마다
들쭉날쭉해진다. 이 모듈은 조항 라벨의 괄호 제목(예: "제15조(손해배상)"의
"손해배상")을 키워드로 매칭해 해당 유형의 체크리스트를 골라준다 -- 별도 LLM
호출 없이 이미 split_into_articles()가 만들어주는 라벨만으로 분류한다.
"""

from __future__ import annotations

import re

_HEADING_PATTERN = re.compile(r"\(([^)]*)\)\s*$")

_DAMAGES_CHECKLIST = """\
- 손해배상액의 예정인지 위약벌인지 구분이 명확한가
- 배상액이 실손해에 비해 과도해 민법 제398조 감액 대상이 될 소지는 없는가
- 배상 상한(cap)이 있는가, 없다면 무제한 배상 리스크는 없는가
- 고의·중과실에 대한 배상까지 배제·제한하는 조항이 섞여 있어 무효 소지는 없는가"""

_TERMINATION_CHECKLIST = """\
- 해지 사유가 구체적이고 명확한가(포괄적·자의적 해지 사유는 없는가)
- 해지 통지 기간이 합리적인가
- 자동갱신 조항이 있다면 상대방의 갱신 거부권이 실질적으로 보장되는가
- 해지 시 정산·원상회복 의무가 명시되어 있는가"""

_JURISDICTION_CHECKLIST = """\
- 전속적 관할합의가 일방에게 현저히 불리하지 않은가(약관법 제14조 불공정 약관 소지)
- 준거법이 국내법인지, 그렇지 않다면 특수한 국제사법 이슈는 없는가
- 중재 조항이 있다면 중재기관·절차가 명확한가"""

_CONFIDENTIALITY_CHECKLIST = """\
- 비밀정보의 범위가 지나치게 넓거나 모호하지 않은가
- 비밀유지 존속기간이 합리적인가(계약 종료 후 영구 존속은 과도할 수 있음)
- 법령상 공개의무(감독당국 제출 등)에 대한 예외가 명시되어 있는가"""

_LIABILITY_CHECKLIST = """\
- 고의·중과실까지 면책하는 조항은 없는가(약관법상 무효 소지)
- 면책 범위가 포괄적이어서 실질적으로 책임을 회피하는 구조는 아닌가
- 불가항력 사유의 범위가 합리적인가"""

_OUTSOURCING_CHECKLIST = """\
- 금융지주회사법 제47조상 승인/보고 대상 업무인지 확인이 필요한가
- 재위탁 허용 여부와 그 제한 조건이 명시되어 있는가
- 수탁자에 대한 관리·감독 책임이 명확한가"""

_DEFAULT_CHECKLIST = """\
- 계약 당사자의 권리·의무가 대등하게 규정되어 있는가
- 관련 법령·사내규정과 상충하는 표현은 없는가
- 용어 정의가 모호해 해석 분쟁 소지가 있는 부분은 없는가"""

# 먼저 매칭되는 항목이 채택되므로, 더 구체적인(범위가 좁은) 유형을 앞에 둔다.
_CLAUSE_TYPE_CHECKLISTS: list[tuple[tuple[str, ...], str]] = [
    (("손해배상", "위약금", "위약벌", "지연손해", "배상액"), _DAMAGES_CHECKLIST),
    (("해지", "해제", "계약기간", "갱신", "존속기간"), _TERMINATION_CHECKLIST),
    (("관할", "준거법", "분쟁해결", "중재"), _JURISDICTION_CHECKLIST),
    (("비밀유지", "기밀", "보안"), _CONFIDENTIALITY_CHECKLIST),
    (("면책", "책임제한", "불가항력"), _LIABILITY_CHECKLIST),
    (("위탁", "수탁", "재위탁"), _OUTSOURCING_CHECKLIST),
]


def checklist_for_label(label: str) -> str:
    """조항 라벨(예: "제15조(손해배상)")의 괄호 제목에서 유형을 추정해 해당
    체크리스트를 반환한다. 괄호 제목이 없거나(예: split_into_articles가
    빈 리스트를 반환했을 때의 "전체 본문" 폴백 라벨) 알려진 유형 키워드와
    매칭되지 않으면 범용 체크리스트로 폴백한다."""
    match = _HEADING_PATTERN.search(label)
    heading = match.group(1) if match else ""

    for keywords, checklist in _CLAUSE_TYPE_CHECKLISTS:
        if any(keyword in heading for keyword in keywords):
            return checklist

    return _DEFAULT_CHECKLIST
