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
# 목록 -> 상세 페이지 이동 (미검증)
# ---------------------------------------------------------------------------

def fetch_law_body_html(browser: webdriver.Chrome, listing_row: dict[str, Any]) -> str:
    """listing_row(crawl_listing()이 반환한 한 행, "법령명__href"/"법령명__onclick"
    포함)로 상세 페이지에 접근해 "본문" 버튼까지 눌러 #bodyContent가 채워진
    뒤의 outerHTML을 반환.

    주의: parse_law_body_html()과 달리 이 함수는 실제 사이트에서 실행해
    검증하지 못했습니다 -- __onclick/__href 값이 실제로 어떻게 상세 페이지로
    이어지는지, "본문" 버튼(#bdyBtnKO)을 눌러야 하는지 아니면 목록 클릭만으로
    이미 본문이 뜨는지는 crawl_listing() 결과를 직접 실행해봐야 확정됩니다.
    """
    href = listing_row.get("법령명__href") or listing_row.get("행정규칙명__href")
    onclick = listing_row.get("법령명__onclick") or listing_row.get("행정규칙명__onclick")

    if href:
        base = browser.current_url.split("?")[0].rsplit("/", 1)[0]
        browser.get(href if href.startswith("http") else f"{base}/{href.lstrip('/')}")
    elif onclick:
        browser.execute_script(onclick.removesuffix("return false;"))
    else:
        raise ValueError("listing_row에 상세 페이지로 갈 href/onclick이 없습니다")

    WebDriverWait(browser, 10).until(
        lambda b: b.execute_script("return document.readyState") == "complete"
    )
    try:
        browser.find_element(By.ID, "bdyBtnKO").click()
    except Exception:
        pass  # 이미 본문이 기본 표시되는 화면일 수 있음

    WebDriverWait(browser, 10).until(
        lambda b: len(b.find_elements(By.CSS_SELECTOR, "#bodyContent .pgroup")) > 0
    )
    return browser.find_element(By.ID, "bodyContentTOP").get_attribute("outerHTML")


def crawl_law_items(browser: webdriver.Chrome | None = None, from_date: date | None = None) -> list[dict[str, Any]]:
    """LAW_CRAWLER=crawlers.law_go_kr:crawl_law_items 로 연결되는 진입점.

    browser를 넘기지 않으면 새로 띄우고 끝에 닫습니다. fetch_law_body_html()이
    아직 미검증이므로, 처음 시도할 때는 소량(예: from_date로 최근 며칠)으로
    먼저 결과를 확인해보는 걸 권장합니다.
    """
    owns_browser = browser is None
    browser = browser or get_browser()
    try:
        items: list[dict[str, Any]] = []
        for _, row in crawl_listing(browser, "law", stop_before=from_date).iterrows():
            html = fetch_law_body_html(browser, row.to_dict())
            meta = parse_law_body_html(html)
            items.extend(law_detail_to_items(meta, source_url=browser.current_url))
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
