"""한국 법령/사규/계약서에 공통적인 "제N조(제목)" 조문 구조를 파싱한다.

원래 pipeline/connectors/local_file.py의 사규(REGULATION) 색인 전용
로직이었으나, 계약서 조항 분리(agent/contract_review.py)도 정확히 같은
구조(제N조(제목) 헤딩, 부칙은 번호 재시작)를 다뤄야 해서 공유 모듈로
분리했다. 두 용도가 같은 정규식을 쓰면, 한쪽에서 실제 문서로 검증해 튜닝한
내용(예: 괄호 제목 필수 조건)이 다른 쪽에도 자동으로 적용된다.
"""

from __future__ import annotations

import re

# 조문 헤딩은 실제 사규 파일에서 줄 맨 앞의 "제N조(제목)" 형태로 나타난다.
# 괄호 제목을 필수로 요구하는 이유: catdoc/pdftotext는 고정 폭으로 줄을
# 바꾸는데, "...법\n제47조제4항에 따른..."처럼 문장 중간의 타 법령 인용이
# 우연히 줄 맨 앞에 놓이는 경우가 실제 사규 파일에서 다수 확인됐다(제목
# 없는 "제47조"). 반면 진짜 조문 헤딩은 예외 없이 "제N조(제목)"로 제목이
# 붙어 있으므로, 괄호를 필수 조건으로 두면 이런 줄바꿈 오탐을 걸러낸다.
# "제N조의M" 가지번호도 지원.
_ARTICLE_HEADING = re.compile(
    r"^제\s*(?P<no>\d+)\s*조(?:\s*의\s*(?P<sub>\d+))?\s*\((?P<heading>[^)]{0,60})\)",
    re.MULTILINE,
)
# 부칙은 항상 "제1조(시행일)"부터 번호를 다시 매겨 시작하므로("64. 업무위탁운용지침"
# 실물 파일에서 확인됨), 조문 헤딩 매칭 대상에서 통째로 제외하고 마지막 조문 뒤에
# 그대로 덧붙인다.
_ADDENDUM_HEADING = re.compile(r"^부\s*칙", re.MULTILINE)


def split_into_articles(text: str) -> list[tuple[str, str]]:
    """본문을 (조문 라벨, 조문 본문) 목록으로 분리한다. 조문 헤딩이 하나도
    없으면(정관 별표, 조직도 첨부, 비정형 계약서 등 조문 구조가 아닌 문서)
    빈 리스트를 반환하며, 호출부는 문서 전체를 하나로 취급하는 기존 방식으로
    폴백해야 한다. 재현율이 100%일 필요는 없다 -- 놓친 조문은 문서 전체
    단위 처리였던 이전 상태와 다를 바 없이 여전히 검색/검토는 되고, 세밀한
    조문 단위 매칭만 못 받을 뿐이다."""
    addendum_match = _ADDENDUM_HEADING.search(text)
    body = text[: addendum_match.start()] if addendum_match else text
    addendum = text[addendum_match.start():].strip() if addendum_match else ""

    matches = list(_ARTICLE_HEADING.finditer(body))
    if not matches:
        return []

    articles = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        jo = f"제{m.group('no')}조" + (f"의{m.group('sub')}" if m.group("sub") else "")
        heading = (m.group("heading") or "").strip()
        label = f"{jo}({heading})" if heading else jo
        articles.append((label, body[m.start():end].strip()))

    if addendum:
        last_label, last_body = articles[-1]
        articles[-1] = (last_label, f"{last_body}\n\n{addendum}")

    return articles
