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
    """목록 테이블 한 페이지를 텍스트로 파싱 (원본 get_page_table_info와 동일).

    상세 페이지로 넘어가는 데는 href/onclick이 필요 없다는 게 확인됐다 --
    실제로 동작하는 참고 코드(click_law_row)는 "번호" 컬럼 값으로 그 행을
    다시 찾아 안의 <a>를 살아있는 DOM에서 직접 클릭한다. 그래서 "번호"
    컬럼은 (다른 곳과 달리) 여기서 버리지 않는다 -- click_row_by_number가
    쓴다."""
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
    """law_name(한글만 비교)과 일치하는 항목을 목록에서 찾아 상세(본문) 페이지를
    연다. 참고 코드가 실제로 쓰는 방식과 동일하게 페이지를 넘기며 찾다가,
    찾은 바로 그 페이지에서 클릭까지 이어서 한다. 동명이인이 있으면(제목이
    같은 옛 버전 등) 번호가 가장 큰(=가장 최근) 행을 선택한다."""
    normalized_target = re.sub(r"[^가-힣]", "", law_name).strip()
    name_col, date_col = get_column_name(site_category)
    move_to_home(browser, site_category)
    time.sleep(0.5)

    last_page_number = get_last_page_number(browser, site_category) or 1
    prev_names: pd.Series | None = None

    for range_start, range_end in get_page_range(last_page_number):
        for page in range(range_start, range_end + 1):
            _goto_page_with_retry(browser, page)
            time.sleep(0.5)

            table_df = _wait_for_fresh_table(browser, prev_names, page, name_col)
            if not table_df.empty and name_col in table_df:
                normalized_col = table_df[name_col].map(lambda x: re.sub(r"[^가-힣]", "", str(x)).strip())
                matches = table_df[normalized_col == normalized_target]
                if not matches.empty:
                    row_number = int(matches["번호"].astype(int).max())
                    click_row_by_number(browser, row_number)
                    wait_for_detail_page(browser, law_name)
                    time.sleep(0.5)
                    return
                prev_names = table_df[name_col]

        if range_end == last_page_number:
            break
        click_next_page(browser)
        time.sleep(0.5)

    raise RuntimeError(f"'{law_name}'을(를) 목록에서 찾지 못했습니다")


def fetch_law_item_by_name(browser: webdriver.Chrome, site_category: str, law_name: str) -> list[dict[str, Any]]:
    """law_name으로 목록에서 찾아 열고, 본문을 파싱해 LawConnector가 기대하는
    list[dict]로 변환. open_law_detail_by_name()/parse_law_body_html()/
    law_detail_to_items()를 엮은 것 -- 처음 검증해볼 때 쓰기 좋은, 법령
    하나짜리 진입점."""
    open_law_detail_by_name(browser, site_category, law_name)
    html = get_body_content_html(browser)
    meta = parse_law_body_html(html)
    return law_detail_to_items(meta, source_url=browser.current_url)


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
