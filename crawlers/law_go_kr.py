"""law.go.kr(국가법령정보센터) 법령/행정규칙 크롤러.

세 벌의 참고 코드를 이 한 파일로 정리했습니다:
  - common_functions.py / table_update.py: 목록(법령명/공포일자) 크롤링.
    get_updated_table()/get_total_table_info()의 90% 중복 코드를
    crawl_listing() 하나로 통합했고(증분은 stop_before로, 전체는 None으로),
    "이전 테이블과 동일하면 무한 재시도"였던 대기 루프에 재시도 상한을 둔
    것 정도만 다릅니다(원본은 랙이 영구적이면 무한 루프).
  - get_information.py의 click_law_row(): 목록 -> 본문 페이지 이동. href/
    onclick을 저장해뒀다가 재생하는 게 아니라, "번호" 컬럼으로 그 행을 다시
    찾아 안의 <a>를 살아있는 DOM에서 바로 클릭하는 방식이라는 걸 이 코드로
    확인했습니다 -- click_row_by_number/wait_for_detail_page/
    open_law_detail_by_name이 이걸 그대로 옮긴 것입니다.
  - 메일 발송/hwp 다운로드/xlwings 엑셀 연동 등 이후 처리 로직은 가져오지
    않았습니다(이 프로젝트의 RAG 파이프라인과 무관).

## 본문 파싱

parse_law_body_html()은 참고 코드에는 없던 부분으로, 공유받은 실제 "본문"
버튼 결과 HTML을 직접 분석해 작성했습니다. 시행 예정 개정이 걸린 법령은
조문마다 현행 텍스트와 시행예정 텍스트가 함께 나온다는 걸 발견해, 이를
SUPERSEDES 관계로 자동 연결합니다 (tests/test_law_go_kr_crawler.py,
tests/fixtures/law_go_kr_body.html 참고).

## 아직 실제 사이트에서 못 돌려본 부분

click_row_by_number/wait_for_detail_page 자체는 참고 코드에서 검증된
로직을 그대로 옮긴 것이라 신뢰도가 높지만, crawl_law_items()가 "목록 페이지의
모든 행을 순서대로 열었다가 같은 페이지로 돌아와 다음 행을 여는" 반복은 참고
코드에 없던 확장이라(참고 코드는 이름을 아는 법령 하나를 찾아 여는 용도) 아직
미검증입니다. 이름을 아는 법령 하나로 먼저 확인하고 싶으면
fetch_law_item_by_name()을 직접 호출하세요.

pipeline.connectors.law.LawConnector(fetch_items=...)에 연결하려면:
    LAW_CRAWLER=crawlers.law_go_kr:crawl_watchlist_items_incremental
(law_watchlist.LAW_WATCHLIST 164개만, 시행일자가 안 바뀐 건 상세
페이지를 다시 열지 않는 증분 버전 -- 매 사이클 164개를 전부 여는
crawl_watchlist_items()보다 이쪽을 기본으로 권장합니다. 전체 법령/행정규칙을
훑는 crawl_law_items()도 있지만 아직 실사이트 미검증입니다.)
행정규칙도 같은 EntityType.LAW로 색인합니다(자체 REGULATION은 "사내" 규정
전용이라 행정규칙과는 다른 범주라고 판단했습니다 -- 다르게 나누고 싶으면
알려주세요).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

# ChromeDriverManager().install()이 사용 중인 Chrome 버전에 맞는 드라이버를
# 찾으려고 googlechromelabs.github.io에 HTTPS로 접속하는데, 사내망처럼
# TLS를 중간에서 검사(SSL 인터셉션)하는 환경에서는 이 요청의 인증서 검증이
# 실패한다(SSLCertVerificationError: unable to get local issuer certificate).
# 원본 common_functions.py도 정확히 이 문제를 이 env var로 우회하고
# 있었다 -- import 시점에 설정해야 ChromeDriverManager가 이 값을 읽는다.
os.environ.setdefault("WDM_SSL_VERIFY", "0")

import pandas as pd
from bs4 import BeautifulSoup as bs
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

_URLS = {
    "law": "https://www.law.go.kr/lsSc.do?menuId=1&subMenuId=23&tabMenuId=121&query=",
    "reg": "https://www.law.go.kr/admRulSc.do?menuId=5&subMenuId=45&tabMenuId=203&query=",
}
_COLUMN_NAMES = {
    "law": ("법령명", "공포일자"),
    "reg": ("행정규칙명", "발령일자"),
}

# watchlist 증분 크롤링(_watchlist_date_lookup)에서 "바뀌었는지" 판단할 때 쓰는
# 컬럼. 공포일자/발령일자는 "발표된 날", 시행일자는 "실제로 적용되는 날"이라
# 실무 안전을 위해 시행일자를 기준으로 삼는다 -- 목록 테이블엔 공포/발령과
# 동시에 시행일자도 이미 나와 있어서(law/reg 양쪽 실제 검색 결과 HTML로 확인),
# 시행일자를 기준으로 바꿔도 변경 감지가 공포일자 기준보다 늦어지지 않는다.
# crawl_listing()/crawl_law_items()의 페이지 넘김 중단 로직은 그대로
# 공포일자/발령일자를 쓴다 -- 그쪽은 목록이 공포일자/발령일자 내림차순으로
# 정렬되어 있다는 전제로 "날짜가 오래되면 그만 넘긴다"고 판단하는데, 시행일자는
# 공포일자와 정렬 순서가 어긋날 수 있어(예: 먼저 공포된 개정이 나중에 공포된
# 개정보다 시행일이 늦을 수 있음) 그 전제를 깨뜨릴 위험이 있다.
_EFFECTIVE_DATE_COLUMN = "시행일자"


def get_browser() -> webdriver.Chrome:
    return webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()))


def get_column_name(site_category: str) -> tuple[str, str]:
    return _COLUMN_NAMES[site_category]


def get_url(site_category: str) -> str:
    return _URLS[site_category]


def move_to_home(browser: webdriver.Chrome, site_category: str, query: str = "") -> None:
    """목록 홈으로 이동. query를 주면 law.go.kr 자체 검색으로 필터링된 결과
    목록으로 바로 들어간다 -- 전체 목록(수십~수백 페이지)을 처음부터 넘기며
    이름을 찾는 대신, 검색으로 좁혀진(보통 1~수 페이지) 목록만 넘기면 되므로
    open_law_detail_by_name()처럼 이름 하나를 찾을 때 훨씬 빠르다."""
    browser.get(get_url(site_category) + quote(query))
    WebDriverWait(browser, 10).until(
        lambda b: b.execute_script("return document.readyState") == "complete"
    )


def get_last_page_number(browser: webdriver.Chrome, site_category: str, query: str = "") -> int | None:
    for tag in browser.find_elements(By.TAG_NAME, "img"):
        if tag.get_property("alt").strip() == "마지막으로":
            try:
                tag.click()
                break
            except Exception:
                continue
    time.sleep(1)

    last_page_number = None
    for tag in browser.find_elements(By.CLASS_NAME, "on"):
        if re.search(r"^\d{1,5}$", tag.text):
            last_page_number = int(tag.text)
            break

    move_to_home(browser, site_category, query)
    time.sleep(0.5)
    return last_page_number


def get_page_range(last_page_number: int) -> list[tuple[int, int]]:
    """한 화면에 노출되는 5개 단위 페이지 구간 목록.

    range()의 stop은 last_page_number + 1이어야 한다 -- last_page_number를
    그대로 stop으로 쓰면(원래 코드) last_page_number가 정확히 새 5개 구간의
    시작점과 같아지는 값(1, 6, 11, ...)에서 그 마지막 구간이 통째로
    빠진다. 특히 last_page_number == 1(검색으로 결과가 딱 1페이지로
    좁혀졌을 때, 즉 open_law_detail_by_name의 query= 검색 결과에서 흔한
    경우)이면 결과가 아예 빈 리스트가 되어 그 유일한 페이지조차 한 번도
    확인하지 못하고 "찾지 못했다"고 끝나버렸다."""
    ranges = []
    for start in range(1, last_page_number + 1, 5):
        end = min(start + 4, last_page_number)
        ranges.append((start, end))
    return ranges


def move_to_page(browser: webdriver.Chrome, page_num: int) -> None:
    for page_tag in browser.find_elements(By.CLASS_NAME, "paging"):
        if not page_tag.text:
            continue
        for tag in page_tag.find_elements(By.TAG_NAME, "li"):
            if int(tag.text) == page_num:
                tag.click()
                WebDriverWait(browser, 10).until(
                    lambda b: b.execute_script("return document.readyState") == "complete"
                )
                return


def click_next_page(browser: webdriver.Chrome) -> None:
    for tag in browser.find_elements(By.TAG_NAME, "img"):
        if tag.get_property("alt").strip() == "다음으로":
            try:
                tag.click()
                return
            except Exception:
                continue


_UPCOMING_ALT = "앞으로 시행될 법령"


class _NotInListingError(RuntimeError):
    """검색 결과 목록에 이 이름이 아예 없다("이 site_category엔 없다")는
    것만 나타내는 전용 예외. open_law_or_reg_detail_by_name()이 law -> reg로
    넘어갈 때 이것만 삼켜야 한다. click_row_by_number()가 행을 못 찾거나
    (실사용에서 확인됨: 목록 테이블이 display:none인 뷰에 있으면 Selenium의
    .text가 항상 빈 문자열이라 못 찾는다), 페이지 이동/갱신이 실패하는 등
    "목록엔 있는데 다른 이유로 실패"한 경우까지 일반 RuntimeError로 뭉뚱그려
    같이 삼키면, 진짜 원인이 "어디에서도 찾지 못했다"는 오해성 메시지에
    덮여버린다."""


def _parse_listing_table(page_source: bs) -> pd.DataFrame:
    """목록 테이블 한 페이지를 텍스트로 파싱 (원본 get_page_table_info와 동일).

    상세 페이지로 넘어가는 데는 href/onclick이 필요 없다는 게 확인됐다 --
    실제로 동작하는 참고 코드(click_law_row)는 "번호" 컬럼 값으로 그 행을
    다시 찾아 안의 <a>를 살아있는 DOM에서 직접 클릭한다. 그래서 "번호"
    컬럼은 (다른 곳과 달리) 여기서 버리지 않는다 -- click_row_by_number가
    쓴다.

    같은 법령이 개정될 때마다 목록에 별도 행으로 쌓이는데(예: 자본시장법
    검색 시 현행 버전 + 이미 공포됐지만 아직 시행 전인 개정 버전이 둘 다
    "정확히 일치"하는 행으로 나옴), law.go.kr는 아직 시행 전인 행에만
    `<img alt="앞으로 시행될 법령">`을 붙여서 구분해준다(실제 검색 결과
    HTML로 확인됨). open_law_detail_by_name()이 이 "_upcoming" 플래그로
    현행 버전만 골라 클릭할 수 있도록 행마다 같이 뽑아둔다.

    첫 번째 <table>을 그냥 집으면 안 된다 -- 실제 검색 결과 페이지 전체
    HTML로 확인해보니, 검색 결과 목록("법령 검색결과 목록" 캡션, 컬럼이
    "번호/법령명/공포일자/...")보다 "소관부처 상세설정" 필터 팝업의
    <table>(컬럼이 "부처/청/위원회/기타")이 DOM에 항상 먼저 나온다 --
    화면엔 display:none으로 숨겨져 있을 뿐 검색어/결과 유무와 무관하게
    항상 그 자리에 렌더링된다. page_source.find("table")로 첫 번째
    <table>을 집으면 매번 이 필터 팝업을 파싱하게 되어 "법령명"/"공포일자"
    컬럼이 없으니 date_col not in table_df가 되고, 결국 검색어와 무관하게
    "찾지 못했다"로 끝난다(실사용에서 재현된 버그). 실제 결과 테이블은
    첫 컬럼이 항상 "번호"라는 확실한 구분점이 있어(필터 팝업 테이블엔
    없음) 그걸로 올바른 테이블을 골라낸다."""
    table_tag = None
    for candidate in page_source.find_all("table"):
        first_th = candidate.find("th", attrs={"scope": "col"})
        if first_th is not None and first_th.text.strip() == "번호":
            table_tag = candidate
            break
    if table_tag is None:
        return pd.DataFrame()

    columns = [th.text.strip() for th in table_tag.find_all("th", attrs={"scope": "col"}) if th.text.strip()]

    rows: list[dict[str, Any]] = []
    for tr in table_tag.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        row: dict[str, Any] = {}
        for col_name, td in zip(columns, cells):
            text = td.text.strip()
            if text:
                row[col_name] = text
        if row:
            row["_upcoming"] = tr.find("img", attrs={"alt": _UPCOMING_ALT}) is not None
            rows.append(row)
    return pd.DataFrame(rows)


_LEFT_LIST_ROW_ID = re.compile(r"^liBgcolor\d+$")
_LEFT_LIST_EFYD = re.compile(r"\[시행\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\]")


def _parse_left_listing(page_source: bs, name_col: str) -> pd.DataFrame:
    """검색 결과 페이지의 기본(좌측, #listDiv) 목록 뷰를 파싱한다.

    law.go.kr 검색 결과 페이지는 같은 결과를 두 벌로 렌더링한다: 기본으로
    화면에 보이는 #listDiv > ul.left_list_bx의 <li> 목록과, <table> 기반의
    "와이드" 뷰(#WideListDIV)다. 실제 HTML로 조상 체인을 추적해보니
    #WideListDIV는 style="display: none;"이었다 -- Selenium 브라우저가
    "와이드 보기"를 켠 적 없는 세션(크롤러가 매번 새로 띄우는 브라우저가
    정확히 이 상태)에서는 항상 이 상태다. _parse_listing_table()(테이블
    기반)이 찾는 "번호" 헤더 테이블이 바로 이 안에 있다 -- BeautifulSoup은
    CSS 가시성을 신경 안 써서 파싱 자체는 문제없이 됐지만, click_row_by_
    number()가 Selenium으로 그 안의 <tr>를 다시 찾으려 하면 숨겨진 요소의
    .text가 항상 빈 문자열이라 못 찾았다 -- "찾지 못했다" 오탐의 진짜
    원인이었다(실사용 HTML로 확인). 그래서 실제로 클릭 가능한 이 좌측
    목록을 대신 파싱한다.

    각 행(<li id="liBgcolorN">)의 <a title="법령명\\n[시행 ...] [...]">에서
    첫 줄(법령명)을 이름으로, title 안의 "[시행 YYYY. M. D.]"를 시행일자로
    뽑는다. law.go.kr는 아직 시행 전인 개정 버전도 같은 이름으로 별도 행에
    나올 수 있는데, 이 뷰에는 (테이블 뷰의 `<img alt="앞으로 시행될
    법령">`같은) 명시적인 표시가 없어서, 대신 뽑아낸 시행일자를 오늘 날짜와
    비교해 "_upcoming"을 직접 계산한다."""
    list_div = page_source.find(id="listDiv")
    if list_div is None:
        return pd.DataFrame()

    today = date.today()
    rows: list[dict[str, Any]] = []
    for index, li in enumerate(list_div.find_all("li", id=_LEFT_LIST_ROW_ID)):
        anchor = li.find("a", recursive=False)
        title = anchor.get("title") if anchor is not None else None
        if not title:
            continue
        name = title.split("\n", 1)[0].strip()

        span_tx2 = anchor.find("span", class_="tx2")
        efyd_match = _LEFT_LIST_EFYD.search(span_tx2.get_text()) if span_tx2 is not None else None
        effective_date = None
        upcoming = False
        if efyd_match is not None:
            year, month, day = (int(g) for g in efyd_match.groups())
            effective_date = f"{year:04d}.{month:02d}.{day:02d}."
            upcoming = date(year, month, day) > today

        rows.append({
            "번호": str(index + 1),
            "_li_id": li["id"],
            name_col: name,
            _EFFECTIVE_DATE_COLUMN: effective_date,
            "_upcoming": upcoming,
        })
    return pd.DataFrame(rows)


def _goto_page_with_retry(browser: webdriver.Chrome, page_num: int, retries: int = 5) -> None:
    for _ in range(retries):
        try:
            move_to_page(browser, page_num)
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"{page_num} 페이지로 이동 실패 (재시도 {retries}회 초과)")


def _wait_for_fresh_table(
    browser: webdriver.Chrome,
    prev_names: pd.Series | None,
    page: int,
    name_col: str,
    retries: int = 20,
    parse_fn=_parse_listing_table,
) -> pd.DataFrame:
    """페이지 전환 직후에도 이전 페이지 DOM이 잠깐 남아있는 경우가 있어(크롤링
    랙), 이름 목록이 이전과 달라질 때까지 짧게 재확인한다. 원본은 이 대기에
    상한이 없어 랙이 영구적이면 무한 루프에 빠졌다.

    page==1이거나 prev_names가 없으면(이 목록을 처음 읽는 경우) "이전과
    다르면 새로 고쳐졌다"고 판단할 기준이 없어, 예전엔 그 즉시 파싱 결과를
    그대로 반환했다. 그런데 실사용에서 확인해보니 law.go.kr 검색 결과
    페이지는 document.readyState가 "complete"가 된 뒤에도(정적 골격만 로드된
    시점) 실제 결과가 AJAX로 나중에 채워진다 -- 실제로 존재하는 법령
    ("금융지주회사법")을 검색해도 이 시점에 즉시 읽으면 빈 목록으로 나와
    "찾지 못했다"로 끝나는 게 재현됐다. 그래서 이 경우엔 테이블이 채워질
    때까지(또는 재시도가 다 될 때까지) 짧게 재확인한다 -- 다만 검색 결과가
    정말로 0건인 것도 정상적인 결과이지 오류가 아니므로, 재시도를 다 써도
    예외를 던지지 않고 그때까지 읽은 결과(빈 테이블일 수도 있음)를 그대로
    반환한다.

    parse_fn: page_source(bs)를 받아 DataFrame을 반환하는 파서. 기본은
    (레거시 crawl_listing()/crawl_law_items()가 쓰는) 테이블 기반
    _parse_listing_table이고, 실제 클릭 가능한 좌측 목록을 읽어야 하는
    open_law_detail_by_name()/_watchlist_date_lookup()은 _parse_left_listing을
    넘겨 쓴다."""
    no_baseline = page == 1 or prev_names is None
    table_df = pd.DataFrame()
    for _ in range(retries):
        table_df = parse_fn(bs(browser.page_source, "html.parser"))
        if no_baseline:
            if not table_df.empty:
                return table_df
        elif name_col not in table_df or not prev_names.equals(table_df[name_col]):
            return table_df
        time.sleep(0.3)
    if no_baseline:
        return table_df
    raise RuntimeError("페이지 갱신 대기 시간 초과 (동일한 목록이 반복 감지됨)")


def crawl_listing(
    browser: webdriver.Chrome,
    site_category: str,
    stop_before: date | None = None,
    oldest_allowed_year: int = 2000,
) -> pd.DataFrame:
    """법령/행정규칙 목록을 페이지 순서대로 수집.

    stop_before가 주어지면 그 날짜보다 오래된 항목만 남은 페이지에서 멈춘다
    (증분 수집용, 원본의 get_updated_table). None이면 oldest_allowed_year보다
    오래된 항목이 나올 때까지 전체를 수집한다(최초 구축용, 원본의
    get_total_table_info).
    """
    name_col, date_col = get_column_name(site_category)
    move_to_home(browser, site_category)
    time.sleep(0.5)

    last_page_number = get_last_page_number(browser, site_category)
    if not last_page_number:
        return pd.DataFrame()

    collected: list[pd.DataFrame] = []
    prev_names: pd.Series | None = None

    for range_start, range_end in get_page_range(last_page_number):
        for page in range(range_start, range_end + 1):
            _goto_page_with_retry(browser, page)
            time.sleep(0.5)

            table_df = _wait_for_fresh_table(browser, prev_names, page, name_col)
            if table_df.empty or date_col not in table_df:
                continue

            dates = pd.to_datetime(table_df[date_col], errors="coerce")
            newest = dates.max()

            if stop_before is not None and pd.notna(newest) and newest.date() < stop_before:
                return _finalize(collected)
            if stop_before is None and pd.notna(newest) and newest.year < oldest_allowed_year:
                return _finalize(collected)

            collected.append(table_df)
            prev_names = table_df[name_col] if name_col in table_df else None

        if range_end == last_page_number:
            break
        click_next_page(browser)
        time.sleep(0.5)

    return _finalize(collected)


def _finalize(table_list: list[pd.DataFrame]) -> pd.DataFrame:
    if not table_list:
        return pd.DataFrame()
    df = pd.concat(table_list, ignore_index=True).drop_duplicates()
    return df.drop(columns="번호", errors="ignore")


# ---------------------------------------------------------------------------
# 본문 조회 페이지(#bodyContent) 파싱
#
# "본문" 버튼(id=bdyBtnKO)을 누르면 AJAX로 #bodyContent가 채워지는데, 시행
# 예정인 개정이 걸려 있는 법령은 조문마다 두 벌이 함께 표시된다:
#   1. <div class="pgroup"><div class="lawcon">...</div></div>
#      -- 현재 시행 중인 조문 텍스트
#   2. (있다면 바로 뒤에) <div class="pgroup babl">...</div>
#      -- 아직 시행 전인 개정 조문 텍스트. 바뀐 부분은
#         style="color: rgb(255, 0, 0)"로 표시되고, 하단에
#         "[시행일: YYYY. M. D.] 제N조" 각주로 그 시행일이 붙는다.
# 이 두 벌을 각각 독립된 시계열 버전으로 뽑아, 아래 law_detail_to_items()가
# RawDocument의 effective_date/superseded_date/supersedes로 그대로 연결한다
# -- ontology/schema.py의 SUPERSEDES 체인이 원래 이걸 위해 만들어진 장치다.
# ---------------------------------------------------------------------------

_JO_ANCHOR = re.compile(r"^J(\d+):(\d+)$")
_EFYD_FOOTNOTE = re.compile(r"\[시행일:\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\]")


def _input_value(soup: bs, input_id: str) -> str:
    tag = soup.find("input", id=input_id)
    return tag.get("value", "") if tag is not None else ""


def _yyyymmdd_to_iso(value: str) -> str | None:
    if not value or len(value) != 8 or not value.isdigit():
        return None
    return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"


def _extract_pgroup_text(container) -> tuple[str, str]:
    """container(현행 조문이면 <div class="pgroup">, 시행예정 조문이면
    <div class="pgroup babl">)에서 (본문 전체 텍스트, 조문 제목)을 뽑는다.
    제목은 두 경우 모두 class="bl" 요소 안에 있다 -- 현행은
    <span class="bl"><label>제목</label></span>, 시행예정은
    <span class="bl">제목</span> 뿐이라 구조가 다르지만 class="bl"의
    get_text()로 통일해서 처리한다."""
    root = container.find("div", class_="lawcon") or container
    title_el = root.find(class_="bl")
    title = title_el.get_text(strip=True) if title_el else ""
    lines = [p.get_text(" ", strip=True) for p in root.find_all("p")]
    return "\n".join(line for line in lines if line), title


def _parse_article(anchor, pgroup) -> dict[str, Any]:
    jo_no, jo_br_no = _JO_ANCHOR.match(anchor["name"]).groups()
    body, title = _extract_pgroup_text(pgroup)

    upcoming = None
    sibling = pgroup.find_next_sibling()
    if sibling is not None and "babl" in (sibling.get("class") or []):
        upcoming_body, upcoming_title = _extract_pgroup_text(sibling)
        m = _EFYD_FOOTNOTE.search(upcoming_body)
        upcoming_effective = f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}" if m else None
        upcoming = {"title": upcoming_title or title, "body": upcoming_body, "effective_date": upcoming_effective}

    return {"jo_no": jo_no, "jo_br_no": jo_br_no, "title": title, "body": body, "upcoming": upcoming}


def _parse_addenda(soup: bs) -> list[dict[str, Any]]:
    ar_div = soup.find("div", id="arDivArea")
    if ar_div is None:
        return []
    addenda = []
    for anchor in ar_div.find_all("a", attrs={"name": True}):
        name = anchor["name"]
        if name == "arArea" or not name.startswith("J") or ":" in name:
            continue  # 조문 앵커("J1:0")와 구분 -- 부칙 앵커는 콜론이 없다
        pgroup = anchor.find_next_sibling("div", class_="pgroup")
        if pgroup is None:
            continue
        header = pgroup.find("p", class_="pty3")
        lines = [header.get_text(" ", strip=True)] if header else []
        lines += [
            p.get_text(" ", strip=True)
            for p in pgroup.find_all("p", class_=re.compile(r"^pty3_dep"))
        ]
        addenda.append({"id": name[1:], "body": "\n".join(line for line in lines if line)})
    return addenda


def parse_law_body_html(html: str) -> dict[str, Any]:
    """"본문" 버튼을 누른 뒤 렌더링된 #bodyContent(또는 그 상위 #bodyContentTOP)의
    outerHTML을 파싱해 법령 메타데이터 + 조문별 텍스트(현행/시행예정) + 부칙을
    반환한다. 순수 파싱 함수라 Selenium/네트워크 없이 단위 테스트 가능
    (tests/fixtures/law_go_kr_body.html, tests/test_law_go_kr_crawler.py 참고).
    """
    soup = bs(html, "html.parser")

    meta = {
        "law_id": _input_value(soup, "lsId"),
        "law_name": _input_value(soup, "lsNm"),
        "revision_seq": _input_value(soup, "lsiSeq"),
        "promulgation_no": _input_value(soup, "ancNo"),
        "promulgation_date": _yyyymmdd_to_iso(_input_value(soup, "ancYd")),
        "effective_date": _yyyymmdd_to_iso(_input_value(soup, "efYd")),
    }

    articles = []
    for anchor in soup.find_all("a", attrs={"name": _JO_ANCHOR}):
        pgroup = anchor.find_next_sibling("div", class_="pgroup")
        if pgroup is not None:
            articles.append(_parse_article(anchor, pgroup))
    meta["articles"] = articles
    meta["addenda"] = _parse_addenda(soup)
    return meta


def law_detail_to_items(meta: dict[str, Any], source_url: str = "") -> list[dict[str, Any]]:
    """parse_law_body_html()의 결과를 LawConnector(fetch_items=...)가 기대하는
    list[dict] 스키마로 변환. 조문 하나당 최소 1개(현행), 시행 예정 개정이
    걸려 있으면 2개(현행 + 시행예정, SUPERSEDES로 연결) 아이템을 만든다."""
    law_id, law_name, effective_date = meta["law_id"], meta["law_name"], meta["effective_date"]
    items: list[dict[str, Any]] = []

    for art in meta["articles"]:
        base = f"{law_id}-{art['jo_no']}-{art['jo_br_no']}"
        upcoming = art.get("upcoming")
        upcoming_effective = upcoming["effective_date"] if upcoming else None

        items.append({
            "id": f"{base}@{effective_date}",
            "title": f"{law_name} {art['title']}".strip(),
            "body": art["body"],
            "effective_date": effective_date,
            "superseded_date": upcoming_effective,
            "source_url": source_url,
        })

        if upcoming and upcoming_effective:
            items.append({
                "id": f"{base}@{upcoming_effective}",
                "title": f"{law_name} {upcoming['title']}".strip(),
                "body": upcoming["body"],
                "effective_date": upcoming_effective,
                "source_url": source_url,
                "supersedes": f"law:{base}@{effective_date}",
            })

    for addendum in meta["addenda"]:
        items.append({
            "id": f"{law_id}-ar-{addendum['id']}",
            "title": f"{law_name} 부칙",
            "body": addendum["body"],
            "source_url": source_url,
        })

    return items


# ---------------------------------------------------------------------------
# 목록 -> 본문 페이지 이동
#
# 실제로 동작 중인 참고 코드(get_information.py의 click_law_row)를 그대로
# 옮긴 것. 핵심은 href/onclick을 저장해뒀다가 나중에 재생하는 게 아니라,
# 목록 페이지에 "번호" 컬럼으로 그 행을 다시 찾아 그 안의 <a>를 살아있는
# DOM에서 바로 클릭하는 것 -- law.go.kr의 "번호"는 그 페이지 안에서만
# 유효한 순번이라, 반드시 그 행이 보이는 페이지를 이미 띄운 상태에서
# 호출해야 한다(다른 페이지로 이동한 뒤에는 재사용 불가).
# ---------------------------------------------------------------------------

def click_row_by_number(browser: webdriver.Chrome, row_number: int) -> None:
    """현재 목록 페이지에서 "번호" 컬럼 값이 row_number인 행을 찾아, 그 행의
    두 번째 셀(법령명/행정규칙명 컬럼)에 있는 링크를 클릭한다."""
    for tr in browser.find_elements(By.TAG_NAME, "tr"):
        cells = tr.find_elements(By.TAG_NAME, "td")
        if cells and cells[0].text.strip() == str(row_number):
            cells[1].find_element(By.TAG_NAME, "a").click()
            return
    raise RuntimeError(f"번호 {row_number}에 해당하는 행을 현재 페이지에서 찾을 수 없습니다")


def click_left_list_row(browser: webdriver.Chrome, li_id: str) -> None:
    """#listDiv 좌측 목록에서 li_id(_parse_left_listing이 뽑은 <li id="...">
    값, 예: "liBgcolor0")에 해당하는 행의 <a>를 클릭한다.

    law.go.kr는 이 id를 #WideListDIV 내부의 숨겨진 사본에도 그대로 중복해서
    쓴다(잘못된 마크업이지만 실제 HTML로 확인됨). By.ID 조회는 문서에 먼저
    나오는 요소를 찾는데 #listDiv가 항상 #WideListDIV보다 앞에 나오므로,
    이 id로 찾으면 항상 보이는(#listDiv 쪽) 행이 선택된다."""
    try:
        li = browser.find_element(By.ID, li_id)
    except NoSuchElementException as exc:
        raise RuntimeError(f"'{li_id}'에 해당하는 행을 현재 페이지에서 찾을 수 없습니다") from exc
    li.find_element(By.TAG_NAME, "a").click()


def wait_for_detail_page(browser: webdriver.Chrome, expected_name: str, timeout: int = 15) -> None:
    """상세 페이지 로드 대기: <h2> 텍스트(한글만 남기고 비교)가 expected_name으로
    시작할 때까지 폴링. 원본은 이 대기에 상한이 없어 페이지가 영영 안 뜨면
    무한 루프에 빠졌다 -- WebDriverWait로 시간 제한을 둔 것 외에 한 가지를 더
    고쳤다: 원본은 정확히 일치(==)를 봤는데, 실제 본문 페이지의 <h2>는
    "법령명 ( 약칭: ... )"처럼 약칭이 뒤에 붙어서 온다는 걸 이번에 받은 실제
    HTML로 확인했다 -- 약칭이 있는 법령은 원본 방식대로면 절대 매치가 안 되어
    무한 대기에 빠졌을 것이다. 그래서 정확히 일치 대신 "약칭이 뒤에 붙어도
    괜찮도록" 접두어 일치로 바꿨다."""
    normalized_expected = re.sub(r"[^가-힣]", "", expected_name).strip()

    def _loaded(b: webdriver.Chrome) -> bool:
        return any(
            re.sub(r"[^가-힣]", "", h2.text).strip().startswith(normalized_expected)
            for h2 in b.find_elements(By.TAG_NAME, "h2")
        )

    WebDriverWait(browser, timeout).until(_loaded)


def get_body_content_html(browser: webdriver.Chrome) -> str:
    return browser.find_element(By.ID, "bodyContentTOP").get_attribute("outerHTML")


def open_law_detail_by_name(browser: webdriver.Chrome, site_category: str, law_name: str) -> None:
    """law_name(한글만 비교)과 일치하는 항목을 찾아 상세(본문) 페이지를 연다.

    law.go.kr 자체 검색(query=)으로 먼저 좁힌 결과 목록에서 찾는다 -- 전체
    목록을 1페이지부터 끝까지 넘기며 찾는 것은 실사용해보니 감당하기 힘들
    정도로 느렸다(법령 목록이 최신순 정렬이라, 오래되고 개정이 뜸한 법령일수록
    한참 뒤 페이지에 있어 수십~수백 페이지를 넘겨야 했음). 검색 결과 안에서는
    기존과 동일하게 페이지를 넘기며 정확히 일치하는 이름을 찾는다.

    같은 이름으로 여러 행이 나올 수 있다(현행 버전 + 이미 공포됐지만 아직
    시행 전인 개정 버전) -- `_upcoming` 플래그로 아직 시행 전인 행은 제외하고
    현행 버전을 우선 선택한다(실제 검색 결과에서 확인된 동작). 그러고도
    여러 행이 남으면(동명이인 등) 번호가 가장 큰 행을 선택한다.

    행 클릭은 _parse_left_listing()이 파싱하는 #listDiv 좌측 목록(li_id)
    기준이다 -- <table> 기반 뷰(#WideListDIV)는 display:none이라 Selenium
    으로 그 안의 행을 다시 찾아 클릭할 수 없다는 게 실사용 HTML로 확인됐다
    (click_left_list_row 참고).
    """
    normalized_target = re.sub(r"[^가-힣]", "", law_name).strip()
    name_col, _ = get_column_name(site_category)
    move_to_home(browser, site_category, query=law_name)
    time.sleep(0.5)

    last_page_number = get_last_page_number(browser, site_category, query=law_name) or 1
    prev_names: pd.Series | None = None

    for range_start, range_end in get_page_range(last_page_number):
        for page in range(range_start, range_end + 1):
            _goto_page_with_retry(browser, page)
            time.sleep(0.5)

            table_df = _wait_for_fresh_table(
                browser, prev_names, page, name_col, parse_fn=lambda soup: _parse_left_listing(soup, name_col)
            )
            if not table_df.empty and name_col in table_df:
                normalized_col = table_df[name_col].map(lambda x: re.sub(r"[^가-힣]", "", str(x)).strip())
                matches = table_df[normalized_col == normalized_target]
                if not matches.empty:
                    if "_upcoming" in matches:
                        current_matches = matches[~matches["_upcoming"]]
                        if not current_matches.empty:
                            matches = current_matches
                    best_row = matches.loc[matches["번호"].astype(int).idxmax()]
                    click_left_list_row(browser, best_row["_li_id"])
                    wait_for_detail_page(browser, law_name)
                    time.sleep(0.5)
                    return
                prev_names = table_df[name_col]

        if range_end == last_page_number:
            break
        click_next_page(browser)
        time.sleep(0.5)

    raise _NotInListingError(f"'{law_name}'을(를) 목록에서 찾지 못했습니다")


def fetch_law_item_by_name(browser: webdriver.Chrome, site_category: str, law_name: str) -> list[dict[str, Any]]:
    """law_name으로 목록에서 찾아 열고, 본문을 파싱해 LawConnector가 기대하는
    list[dict]로 변환. open_law_detail_by_name()/parse_law_body_html()/
    law_detail_to_items()를 엮은 것 -- 처음 검증해볼 때 쓰기 좋은, 법령
    하나짜리 진입점."""
    open_law_detail_by_name(browser, site_category, law_name)
    html = get_body_content_html(browser)
    meta = parse_law_body_html(html)
    return law_detail_to_items(meta, source_url=browser.current_url)


# ---------------------------------------------------------------------------
# 관리 목록(law_watchlist.LAW_WATCHLIST) 기반 크롤링
#
# law.go.kr 전체를 크롤링하는 crawl_law_items() 대신, 실제 업무에 쓰는
# 법령/행정규칙만 정해두고 그것만 찾아 색인한다. 목록에는 law.go.kr에서
# 법령/행정규칙 어느 쪽인지 구분이 없으므로(원본 법규리스트.xlsx도 마찬가지),
# 'law'에서 먼저 찾고 없으면 'reg'에서 찾는다 -- 원본 get_update_df와
# 동일한 방식(law/reg 두 목록 모두에서 이름을 대조).
# ---------------------------------------------------------------------------

def open_law_or_reg_detail_by_name(browser: webdriver.Chrome, name: str) -> str:
    """name을 'law'(법령)에서 먼저 찾고, 없으면 'reg'(행정규칙)에서 찾아 상세
    페이지를 연다. 찾아서 연 쪽의 site_category를 반환.

    _NotInListingError("이 site_category 목록엔 없음")만 삼키고 다음
    site_category로 넘어간다 -- 그 외 RuntimeError(클릭 실패, 페이지 이동
    실패 등 "목록엔 있는데 다른 이유로 실패")까지 같이 삼키면 실사용에서
    실제로 겪은 것처럼 진짜 원인이 감춰지고 "어디에서도 찾지 못했다"는
    오해성 메시지만 남는다 -- 그런 실패는 그대로 위로 전파해야 한다."""
    for site_category in ("law", "reg"):
        try:
            open_law_detail_by_name(browser, site_category, name)
            return site_category
        except _NotInListingError:
            continue
    raise RuntimeError(f"'{name}'을(를) 법령/행정규칙 목록 어디에서도 찾지 못했습니다")


def crawl_watchlist_items(
    browser: webdriver.Chrome | None = None, names: list[str] | None = None
) -> list[dict[str, Any]]:
    """LAW_CRAWLER=crawlers.law_go_kr:crawl_watchlist_items 로 연결되는 진입점.

    names를 지정하지 않으면 law_watchlist.LAW_WATCHLIST(사용자가 관리하는
    법규리스트.xlsx에서 옮긴 목록)를 사용한다. 목록에 있는 항목 하나를
    못 찾거나 파싱에 실패해도 전체가 죽지 않고 그 항목만 건너뛴다 -- 나머지
    163개를 위해 실패한 1개 때문에 SYNC_INTERVAL_SECONDS 주기 전체가 비는
    일은 없어야 하므로.
    """
    from crawlers.law_watchlist import LAW_WATCHLIST

    names = names if names is not None else LAW_WATCHLIST
    owns_browser = browser is None
    browser = browser or get_browser()
    try:
        items: list[dict[str, Any]] = []
        for name in names:
            try:
                site_category = open_law_or_reg_detail_by_name(browser, name)
                html = get_body_content_html(browser)
                meta = parse_law_body_html(html)
                items.extend(law_detail_to_items(meta, source_url=browser.current_url))
            except Exception:
                logging.getLogger(__name__).exception("watchlist 항목 '%s' 크롤링 실패, 건너뜀", name)
        return items
    finally:
        if owns_browser:
            browser.quit()


# ---------------------------------------------------------------------------
# 증분 크롤링: 상세 페이지를 열기 전에, _watchlist_date_lookup()으로 시행일자가
# 지난번과 같은지부터 저렴하게 확인한다.
#
# 상세 페이지(본문 파싱)를 여는 게 이 크롤러에서 제일 비싼 부분이다 --
# crawl_watchlist_items()는 164개를 매번 전부 연다. 반면 목록 테이블에는
# 상세 페이지를 열지 않고도 시행일자가 이미 나와 있으므로, 그 날짜가
# 지난번과 같으면 본문이 안 바뀌었다고 보고 상세 페이지를 건너뛴다.
#
# 주의: pipeline.sync.IngestSyncer는 매 사이클 fetch_items()가 반환한
# id 전체를 "현재 상태"로 보고, 거기 없는 id는 "소스에서 사라졌다"고
# 판단해 그래프/벡터 스토어에서 삭제한다. 그래서 이 함수는 안 바뀐
# 법령도 (상세 페이지를 다시 열지 않을 뿐) 반드시 결과에 포함해야 한다 --
# 지난번에 파싱해둔 결과를 상태 파일에 캐시해뒀다가 그대로 재사용하는
# 이유가 이것이다. 바뀐 것만 반환하면 다음 사이클에 나머지가 전부
# "사라진 문서"로 오인되어 삭제된다.
# ---------------------------------------------------------------------------


def _watchlist_date_lookup(browser: webdriver.Chrome, names: list[str]) -> dict[str, str]:
    """watchlist 이름별 현재 시행일자를 상세 페이지 없이 확인한다.

    처음엔 law -> reg 순으로 전체 목록을 한 번씩 훑어서 한 번의 순회로
    모든 이름의 날짜를 확인했는데, 이건 law.go.kr에 등록된 전체 건수(수천~
    수만 건)를 이름 개수와 무관하게 매번 다 넘겨야 해서 실사용해보니
    open_law_detail_by_name()에서와 같은 이유로 감당하기 힘들었다(watchlist가
    164개뿐이라도, 그 164개를 위해 훨씬 많은 전체 목록을 다 훑는 건 배보다
    배꼽이 컸음). open_law_detail_by_name()과 동일하게 이름별로 law.go.kr
    자체 검색(query=)을 써서, 상세 페이지는 열지 않고 검색 결과 첫 페이지의
    시행일자만 저렴하게 읽는다(_EFFECTIVE_DATE_COLUMN 주석 참고 -- 공포일자/
    발령일자 대신 시행일자를 기준으로 삼는 이유). law -> reg 순으로 시도하고
    (law에서 이미 찾았으면 reg는 보지 않음), 여러 건이 매치되면(개정 이력 등)
    가장 최근 날짜를 취한다.
    """
    normalized_targets = {re.sub(r"[^가-힣]", "", n).strip(): n for n in names}
    found: dict[str, str] = {}

    for site_category in ("law", "reg"):
        name_col, _ = get_column_name(site_category)
        for normalized, original_name in normalized_targets.items():
            if original_name in found:
                continue

            move_to_home(browser, site_category, query=original_name)
            time.sleep(0.5)
            # page=1, prev_names=None -> AJAX로 결과가 채워질 때까지 짧게
            # 재확인만 하고, 재시도가 다 돼도 예외 없이 빈 테이블을 반환한다
            # (_wait_for_fresh_table 참고 -- open_law_detail_by_name과 동일한
            # 경합을 여기서도 겪는다). #WideListDIV(테이블 뷰)는 display:none
            # 이라 실제 화면엔 좌측 목록(#listDiv)만 보이므로 그쪽을 읽는다
            # (open_law_detail_by_name과 동일한 이유 -- _parse_left_listing
            # 참고).
            table_df = _wait_for_fresh_table(
                browser, None, 1, name_col, parse_fn=lambda soup: _parse_left_listing(soup, name_col)
            )
            if table_df.empty or name_col not in table_df or _EFFECTIVE_DATE_COLUMN not in table_df:
                continue

            normalized_col = table_df[name_col].map(lambda x: re.sub(r"[^가-힣]", "", str(x)).strip())
            matches = table_df[normalized_col == normalized]
            if matches.empty:
                continue
            dates = pd.to_datetime(matches[_EFFECTIVE_DATE_COLUMN], errors="coerce").dropna()
            if not dates.empty:
                found[original_name] = dates.max().strftime("%Y-%m-%d")

    return found


def _load_watchlist_state(state_path: Path) -> dict[str, dict[str, Any]]:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logging.getLogger(__name__).warning("법령 크롤 상태 파일을 읽지 못했습니다: %s (빈 상태로 시작)", state_path)
        return {}


def _save_watchlist_state(state_path: Path, state: dict[str, dict[str, Any]]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def crawl_watchlist_items_incremental(
    browser: webdriver.Chrome | None = None,
    names: list[str] | None = None,
    state_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """LAW_CRAWLER=crawlers.law_go_kr:crawl_watchlist_items_incremental 로
    연결되는, crawl_watchlist_items()의 증분 버전. 반환 스키마는 동일하다.

    이름마다 무조건 상세 페이지를 여는 대신, _watchlist_date_lookup()으로
    시행일자가 지난번(state_path에 저장된 값)과 같은지 먼저 본다.
    같으면 상세 페이지를 열지 않고 지난번 파싱 결과를 그대로 재사용하고,
    다르거나(개정/공포) 처음 보는 이름이면 그때만 상세 페이지를 연다.
    상세 페이지 크롤링이 실패해도 지난번 캐시가 있으면 그걸 대신 반환한다
    (실패했다고 결과에서 빠지면 다음 사이클에 "사라진 문서"로 오인 삭제됨).
    """
    from crawlers.law_watchlist import LAW_WATCHLIST

    names = names if names is not None else LAW_WATCHLIST
    state_file = Path(state_path or os.environ.get("LAW_CRAWL_STATE_PATH", "./data/law_crawl_state.json"))
    state = _load_watchlist_state(state_file)
    logger = logging.getLogger(__name__)

    owns_browser = browser is None
    browser = browser or get_browser()
    try:
        current_dates = _watchlist_date_lookup(browser, names)

        items: list[dict[str, Any]] = []
        for name in names:
            cached = state.get(name)
            current_date = current_dates.get(name)

            if cached is not None and current_date is not None and cached.get("date") == current_date:
                items.extend(cached["items"])
                continue

            try:
                open_law_or_reg_detail_by_name(browser, name)
                html = get_body_content_html(browser)
                meta = parse_law_body_html(html)
                new_items = law_detail_to_items(meta, source_url=browser.current_url)
                items.extend(new_items)
                state[name] = {"date": current_date or meta.get("effective_date"), "items": new_items}
            except Exception:
                logger.exception("watchlist 항목 '%s' 크롤링 실패", name)
                if cached is not None:
                    items.extend(cached["items"])

        _save_watchlist_state(state_file, state)
        return items
    finally:
        if owns_browser:
            browser.quit()


def crawl_law_items(browser: webdriver.Chrome | None = None, from_date: date | None = None) -> list[dict[str, Any]]:
    """LAW_CRAWLER=crawlers.law_go_kr:crawl_law_items 로 연결되는 진입점.

    click_row_by_number/wait_for_detail_page는 참고 코드에서 검증된 대로
    포팅한 것이지만, 목록 페이지의 "모든" 행을 순서대로 열었다가 같은
    페이지로 돌아와 다음 행을 여는 이 반복 자체는 참고 코드에 없던
    확장이라 미검증입니다(참고 코드는 이름을 알고 있는 법령 하나를 찾아
    여는 용도였습니다 -- 그 흐름만 쓰고 싶다면 fetch_law_item_by_name을
    직접 호출하세요). 처음 돌릴 때는 from_date로 범위를 좁혀 확인해보길
    권합니다.
    """
    owns_browser = browser is None
    browser = browser or get_browser()
    try:
        name_col, date_col = get_column_name("law")
        move_to_home(browser, "law")
        time.sleep(0.5)
        last_page_number = get_last_page_number(browser, "law") or 1

        items: list[dict[str, Any]] = []
        prev_names: pd.Series | None = None

        for range_start, range_end in get_page_range(last_page_number):
            for page in range(range_start, range_end + 1):
                _goto_page_with_retry(browser, page)
                time.sleep(0.5)

                table_df = _wait_for_fresh_table(browser, prev_names, page, name_col)
                if table_df.empty or date_col not in table_df:
                    continue

                dates = pd.to_datetime(table_df[date_col], errors="coerce")
                if from_date is not None and pd.notna(dates.max()) and dates.max().date() < from_date:
                    return items

                for row_number, law_name in zip(table_df["번호"], table_df[name_col]):
                    click_row_by_number(browser, int(row_number))
                    wait_for_detail_page(browser, law_name)
                    html = get_body_content_html(browser)
                    items.extend(law_detail_to_items(parse_law_body_html(html), source_url=browser.current_url))
                    # 같은 목록 페이지로 복귀해 다음 행을 이어서 연다.
                    _goto_page_with_retry(browser, page)
                    time.sleep(0.5)

                prev_names = table_df[name_col]

            if range_end == last_page_number:
                break
            click_next_page(browser)
            time.sleep(0.5)

        return items
    finally:
        if owns_browser:
            browser.quit()


if __name__ == "__main__":
    # 수동 실행용 예시: 법령/행정규칙 전체 목록을 한 번 수집해 화면에 건수만 출력.
    # (원본의 xlsx/parquet 저장은 이 크롤러가 자동 재색인 루프에서 매번 호출될
    # 수 있어 함수 밖, 즉 여기 스크립트 실행부로만 남겨뒀습니다.)
    for category in ("law", "reg"):
        drv = get_browser()
        try:
            df = crawl_listing(drv, category)
        finally:
            drv.quit()
        print(f"{category}: {len(df)}건 수집")
        today = date.today().strftime("%y%m%d")
        df.to_excel(f"./{today}_latest_{category}.xlsx", index=False)
