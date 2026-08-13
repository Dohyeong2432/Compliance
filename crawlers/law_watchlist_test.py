"""테스트용 진입점: 실제 운영 watchlist(law_watchlist.LAW_WATCHLIST, 164개)를
건드리지 않고, 이름을 직접 정해준 소수의 법령/행정규칙만 색인해보고 싶을 때
쓴다.

.env의 LAW_CRAWLER를 이걸로 돌리면 됨:
    LAW_CRAWLER=crawlers.law_watchlist_test:crawl_single_law_test
테스트가 끝나면 LAW_CRAWLER를
    LAW_CRAWLER=crawlers.law_go_kr:crawl_watchlist_items_incremental
로 되돌리면 원래 164개 watchlist로 돌아간다 -- law_watchlist.py 자체는
전혀 수정하지 않으므로 되돌리는 걸 잊어도 원본 목록은 안전하다.
"""

from __future__ import annotations

from typing import Any

from crawlers.law_go_kr import crawl_watchlist_items_incremental

TEST_NAMES: list[str] = ["금융지주회사법"]


def crawl_single_law_test() -> list[dict[str, Any]]:
    return crawl_watchlist_items_incremental(names=TEST_NAMES)
