"""law.go.kr(국가법령정보센터) 법령/행정규칙 크롤러.

원본은 업로드해주신 Selenium 기반 목록 크롤링 코드(법령_개정안_안내/py_files/
common_functions.py, table_update.py 중 "사이트 크롤링" 부분)입니다. 이 파일은
그걸 다음 기준으로 정리한 것입니다:
  - 메일 발송/hwp 다운로드 등 이후 처리 로직 제거 (원본 주석에 이미 표시됨)
  - get_updated_table()/get_total_table_info()의 90% 중복 코드를 crawl_listing()
    하나로 통합 (증분 수집은 stop_before로, 최초 전체 수집은 stop_before=None으로)
  - "이전 테이블과 동일하면 무한 재시도"였던 대기 루프에 재시도 횟수 상한을 둠
    (원본은 크롤링 랙이 영구적이면 그대로 무한 루프에 빠짐)
  - xlsx/parquet 저장을 함수 밖으로 분리 -- crawl_law_items()는 매 SYNC_INTERVAL_
    SECONDS마다 자동 호출될 수 있으므로, 파일 저장 같은 부수효과를 안에 두면 안 됨
  - 목록 각 행에서 상세 페이지로 가는 <a href>/onclick을 원문 그대로 추가 수집
    (원본은 법령명/공포일자 텍스트만 뽑고 상세 페이지로 갈 방법을 아예 남기지
    않아서, 본문을 이어서 가져올 수가 없었음)

## 아직 못 채운 부분 (본문/제개정사항/신구조문대비표)

목록(법령명/공포일자) 수집은 원본 코드가 검증된 그대로라 정리만 했지만, 본문·
제개정사항·신구조문대비표는 상세 페이지의 실제 HTML 구조를 봐야 정확한 선택자를
쓸 수 있어서 비워뒀습니다(fetch_law_detail 참고). 준법감시 데이터에서 셀렉터를
잘못 짚으면 "조용히 빈 본문/엉뚱한 본문이 색인되는" 실패가 나는데, 이건 이
프로젝트 전체가 막으려는 실패 모드라 추측으로 채우지 않았습니다. 아래 중 하나를
공유해주시면 이어서 구현합니다:
  1. 법령 본문 조회 페이지, 행정규칙 본문 조회 페이지, 신구조문대비표 페이지의
     HTML(뷰 소스) -- 이 파일의 스크래핑 방식을 그대로 유지하고 싶다면.
  2. law.go.kr Open API(open.law.go.kr) OC 인증키 -- 있다면 스크래핑보다 이 쪽이
     훨씬 안정적입니다. lawService.do가 조문 본문을 XML로 바로 주고, 신구조문
     대비 정보도 API로 제공되는 걸로 알고 있어서(정확한 엔드포인트는 발급받은
     문서로 재확인 필요), Selenium 없이 requests만으로 끝낼 수 있습니다.

pipeline.connectors.law.LawConnector(fetch_items=...)에 연결하려면:
    LAW_CRAWLER=crawlers.law_go_kr:crawl_law_items
행정규칙도 같은 EntityType.LAW로 색인합니다(자체 REGULATION은 "사내" 규정
전용이라 행정규칙과는 다른 범주라고 판단했습니다 -- 다르게 나누고 싶으면
알려주세요).
"""

from __future__ import annotations

import re
import time
from datetime import date
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup as bs
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
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


def get_browser() -> webdriver.Chrome:
    return webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()))


def get_column_name(site_category: str) -> tuple[str, str]:
    return _COLUMN_NAMES[site_category]


def get_url(site_category: str) -> str:
    return _URLS[site_category]


def move_to_home(browser: webdriver.Chrome, site_category: str) -> None:
    browser.get(get_url(site_category))
    WebDriverWait(browser, 10).until(
        lambda b: b.execute_script("return document.readyState") == "complete"
    )


def get_last_page_number(browser: webdriver.Chrome, site_category: str) -> int | None:
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

    move_to_home(browser, site_category)
    time.sleep(0.5)
    return last_page_number


def get_page_range(last_page_number: int) -> list[tuple[int, int]]:
    """한 화면에 노출되는 5개 단위 페이지 구간 목록."""
    ranges = []
    for start in range(1, last_page_number, 5):
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


def _parse_listing_table(page_source: bs) -> pd.DataFrame:
    """목록 테이블 한 페이지를 파싱. 텍스트뿐 아니라, 각 셀 안에 <a>가 있으면
    그 href/onclick도 "<컬럼명>__href" / "<컬럼명>__onclick"으로 원문 그대로
    함께 담는다 -- 상세 페이지로 가는 유일한 단서라 텍스트만 뽑으면 버려진다."""
    table_tag = page_source.find("table")
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
            anchor = td.find("a")
            if anchor is not None:
                if anchor.get("href"):
                    row[f"{col_name}__href"] = anchor["href"]
                if anchor.get("onclick"):
                    row[f"{col_name}__onclick"] = anchor["onclick"]
        if row:
            rows.append(row)
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
) -> pd.DataFrame:
    """페이지 전환 직후에도 이전 페이지 DOM이 잠깐 남아있는 경우가 있어(크롤링
    랙), 이름 목록이 이전과 달라질 때까지 짧게 재확인한다. 원본은 이 대기에
    상한이 없어 랙이 영구적이면 무한 루프에 빠졌다."""
    for _ in range(retries):
        table_df = _parse_listing_table(bs(browser.page_source, "html.parser"))
        if page == 1 or prev_names is None or name_col not in table_df or not prev_names.equals(table_df[name_col]):
            return table_df
        time.sleep(0.3)
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
# 상세 페이지(본문/제개정사항/신구조문대비표) -- 실제 HTML을 봐야 구현 가능
# ---------------------------------------------------------------------------

def fetch_law_detail(browser: webdriver.Chrome, listing_row: dict[str, Any]) -> dict[str, Any]:
    """listing_row(crawl_listing()이 반환한 한 행, __href/__onclick 포함)로
    상세 페이지에 접근해 본문/제개정이유/신구조문대비표를 파싱해 반환.

    TODO: law.go.kr 법령/행정규칙 본문 조회 페이지, 신구조문대비표 페이지의
    실제 HTML을 받으면 구현합니다. 지금은 목록 행 원본만 그대로 돌려줍니다.
    """
    raise NotImplementedError(
        "상세 페이지 파싱 미구현 -- law.go.kr 본문 조회/신구조문대비표 페이지의 "
        "HTML을 공유해주시면 구현하겠습니다. 그 전까지는 crawl_listing()의 결과"
        "(법령명/공포일자 + __href/__onclick)만 사용할 수 있습니다."
    )


def crawl_law_items(from_date: date | None = None) -> list[dict[str, Any]]:
    """LAW_CRAWLER=crawlers.law_go_kr:crawl_law_items 로 연결되는 진입점.

    pipeline.connectors.crawler_base.CrawledSourceConnector가 기대하는
    {"id", "title", "body", "effective_date", ...} 스키마를 아직 만들 수
    없습니다(본문이 없으므로) -- fetch_law_detail이 채워지면 완성됩니다.
    """
    raise NotImplementedError(
        "본문 파싱이 아직 없어 LawConnector에 바로 연결할 수 없습니다. "
        "지금 당장 목록만 확인해보려면 이 모듈의 crawl_listing()을 직접 호출하세요."
    )


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
