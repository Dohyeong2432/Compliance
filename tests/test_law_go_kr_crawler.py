import json
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest
from selenium.common.exceptions import TimeoutException

import crawlers.law_go_kr as law_go_kr
from crawlers.law_go_kr import (
    click_row_by_number,
    crawl_watchlist_items,
    crawl_watchlist_items_incremental,
    get_last_page_number,
    get_page_range,
    law_detail_to_items,
    move_to_home,
    open_law_detail_by_name,
    open_law_or_reg_detail_by_name,
    parse_law_body_html,
    wait_for_detail_page,
)
from crawlers.law_watchlist import LAW_WATCHLIST
from ontology.schema import RelationType

FIXTURE = Path(__file__).parent / "fixtures" / "law_go_kr_body.html"


def _load_meta():
    html = FIXTURE.read_text(encoding="utf-8")
    return parse_law_body_html(html)


def test_parses_law_metadata_from_hidden_inputs():
    meta = _load_meta()
    assert meta["law_id"] == "011359"
    assert meta["law_name"] == "전기통신금융사기 피해 방지 및 피해금 환급에 관한 특별법"
    assert meta["revision_seq"] == "283199"
    assert meta["promulgation_no"] == "21320"
    assert meta["promulgation_date"] == "2026-02-03"
    assert meta["effective_date"] == "2026-08-04"


def test_finds_all_articles_by_anchor():
    meta = _load_meta()
    jo_keys = [(a["jo_no"], a["jo_br_no"]) for a in meta["articles"]]
    assert jo_keys == [("1", "0"), ("2", "0"), ("2", "2"), ("2", "3"), ("18", "0")]


def test_article_with_upcoming_amendment_has_both_versions():
    meta = _load_meta()
    art1 = next(a for a in meta["articles"] if a["jo_no"] == "1")

    assert art1["title"] == "제1조(목적)"
    assert "금융회사의 피해 방지 책임" in art1["body"]
    assert "피해금 환급을 위하여 사기이용계좌의 채권소멸절차" in art1["body"]

    assert art1["upcoming"] is not None
    assert art1["upcoming"]["effective_date"] == "2026-10-01"
    assert "금융회사등의 피해 방지 책임" in art1["upcoming"]["body"]
    assert "피해자산 환급을 위하여 사기이용계좌등의 채권소멸절차" in art1["upcoming"]["body"]


def test_article_without_upcoming_amendment_has_none():
    meta = _load_meta()
    art3 = next(a for a in meta["articles"] if a["jo_no"] == "2" and a["jo_br_no"] == "3")
    assert art3["title"] == "제2조의3(국제협력)"
    assert art3["upcoming"] is None


def test_article_title_extracted_for_both_current_and_upcoming():
    meta = _load_meta()
    art18 = next(a for a in meta["articles"] if a["jo_no"] == "18")
    assert art18["title"] == "제18조(과태료)"
    assert art18["upcoming"]["title"] == "제18조(과태료)"


def test_addenda_parsed_with_and_without_sub_provisions():
    meta = _load_meta()
    assert len(meta["addenda"]) == 2
    first, second = meta["addenda"]
    assert "공포 후 6개월이 경과한 날부터 시행한다" in first["body"]
    assert "제2조(경과조치)" in first["body"]  # collapsed sub-article pulled in
    assert "공포 후 6개월이 경과한 날부터 시행한다" in second["body"]


def test_law_detail_to_items_current_version_shape():
    meta = _load_meta()
    items = law_detail_to_items(meta, source_url="https://www.law.go.kr/example")

    current_j1 = next(i for i in items if i["id"] == "011359-1-0@2026-08-04")
    assert current_j1["title"] == "전기통신금융사기 피해 방지 및 피해금 환급에 관한 특별법 제1조(목적)"
    assert current_j1["effective_date"] == "2026-08-04"
    assert current_j1["superseded_date"] == "2026-10-01"  # closed off by the upcoming version
    assert current_j1["source_url"] == "https://www.law.go.kr/example"
    assert "supersedes" not in current_j1


def test_law_detail_to_items_upcoming_version_supersedes_current():
    meta = _load_meta()
    items = law_detail_to_items(meta)

    upcoming_j1 = next(i for i in items if i["id"] == "011359-1-0@2026-10-01")
    assert upcoming_j1["effective_date"] == "2026-10-01"
    assert "superseded_date" not in upcoming_j1
    assert upcoming_j1["supersedes"] == "law:011359-1-0@2026-08-04"


def test_law_detail_to_items_no_upcoming_version_for_stable_article():
    meta = _load_meta()
    items = law_detail_to_items(meta)

    ids = {i["id"] for i in items}
    assert "011359-2-3@2026-08-04" in ids
    assert not any(i["id"].startswith("011359-2-3@") and i["id"] != "011359-2-3@2026-08-04" for i in items)


def test_law_detail_to_items_includes_addenda():
    meta = _load_meta()
    items = law_detail_to_items(meta)
    addendum_items = [i for i in items if i["id"].startswith("011359-ar-")]
    assert len(addendum_items) == 2
    assert all(i["title"] == "전기통신금융사기 피해 방지 및 피해금 환급에 관한 특별법 부칙" for i in addendum_items)


def test_law_detail_to_items_feeds_law_connector_end_to_end():
    """전체 파이프라인: 파싱 -> item 변환 -> LawConnector -> RawDocument."""
    from pipeline.connectors.law import LawConnector

    meta = _load_meta()
    items = law_detail_to_items(meta)
    connector = LawConnector(fetch_items=lambda: items)

    docs = connector.fetch()

    assert connector.errors == []
    assert len(docs) == len(items)
    upcoming_j1 = next(d for d in docs if d.external_id == "011359-1-0@2026-10-01")
    assert upcoming_j1.relations == [(RelationType.SUPERSEDES, "law:011359-1-0@2026-08-04")]


# ---------------------------------------------------------------------------
# click_row_by_number / wait_for_detail_page: 실제 Selenium/브라우저 없이,
# 참고 코드(click_law_row)에서 그대로 옮겨온 핵심 로직 자체를 검증한다.
# ---------------------------------------------------------------------------

def _make_tr(number_text: str):
    number_td = MagicMock()
    number_td.text = number_text
    name_td = MagicMock()
    link = MagicMock()
    name_td.find_element.return_value = link
    tr = MagicMock()
    tr.find_elements.return_value = [number_td, name_td]
    return tr, link


def test_click_row_by_number_clicks_the_matching_row_link():
    browser = MagicMock()
    tr1, link1 = _make_tr("1")
    tr2, link2 = _make_tr("2")
    browser.find_elements.return_value = [tr1, tr2]

    click_row_by_number(browser, 2)

    link1.click.assert_not_called()
    link2.click.assert_called_once()


def test_click_row_by_number_ignores_rows_without_cells():
    empty_tr = MagicMock()
    empty_tr.find_elements.return_value = []
    tr1, link1 = _make_tr("1")
    browser = MagicMock()
    browser.find_elements.return_value = [empty_tr, tr1]

    click_row_by_number(browser, 1)

    link1.click.assert_called_once()


def test_click_row_by_number_raises_when_not_found():
    browser = MagicMock()
    tr1, _ = _make_tr("1")
    browser.find_elements.return_value = [tr1]

    with pytest.raises(RuntimeError):
        click_row_by_number(browser, 99)


def test_wait_for_detail_page_succeeds_when_h2_matches_ignoring_non_korean_chars():
    browser = MagicMock()
    h2 = MagicMock()
    h2.text = "전기통신금융사기 피해 방지 및 피해금 환급에 관한 특별법(제21320호)"
    browser.find_elements.return_value = [h2]

    wait_for_detail_page(browser, "전기통신금융사기 피해 방지 및 피해금 환급에 관한 특별법", timeout=1)


def test_wait_for_detail_page_times_out_without_match():
    browser = MagicMock()
    h2 = MagicMock()
    h2.text = "다른 법령명"
    browser.find_elements.return_value = [h2]

    with pytest.raises(TimeoutException):
        wait_for_detail_page(browser, "찾는 법령명", timeout=1)


# ---------------------------------------------------------------------------
# law_watchlist.LAW_WATCHLIST / crawl_watchlist_items
# ---------------------------------------------------------------------------

def test_watchlist_has_164_unique_nonempty_entries():
    assert len(LAW_WATCHLIST) == 164
    assert len(set(LAW_WATCHLIST)) == 164
    assert all(isinstance(name, str) and name.strip() for name in LAW_WATCHLIST)


def test_open_law_or_reg_detail_by_name_tries_law_then_falls_back_to_reg(monkeypatch):
    calls = []

    def fake_open(browser, site_category, name):
        calls.append(site_category)
        if site_category == "law":
            raise RuntimeError("not in law listing")
        return None

    monkeypatch.setattr(law_go_kr, "open_law_detail_by_name", fake_open)

    result = open_law_or_reg_detail_by_name(MagicMock(), "자금세탁방지및공중협박자금조달금지에관한업무규정")

    assert calls == ["law", "reg"]
    assert result == "reg"


def test_open_law_or_reg_detail_by_name_returns_law_without_trying_reg(monkeypatch):
    calls = []

    def fake_open(browser, site_category, name):
        calls.append(site_category)

    monkeypatch.setattr(law_go_kr, "open_law_detail_by_name", fake_open)

    result = open_law_or_reg_detail_by_name(MagicMock(), "개인정보보호법")

    assert calls == ["law"]
    assert result == "law"


def test_open_law_or_reg_detail_by_name_raises_when_neither_has_it(monkeypatch):
    def fake_open(browser, site_category, name):
        raise RuntimeError("not found")

    monkeypatch.setattr(law_go_kr, "open_law_detail_by_name", fake_open)

    with pytest.raises(RuntimeError):
        open_law_or_reg_detail_by_name(MagicMock(), "존재하지않는법")


def test_crawl_watchlist_items_skips_failures_and_continues(monkeypatch):
    def fake_open_or_reg(browser, name):
        if name == "실패하는법":
            raise RuntimeError("boom")
        return "law"

    monkeypatch.setattr(law_go_kr, "open_law_or_reg_detail_by_name", fake_open_or_reg)
    monkeypatch.setattr(law_go_kr, "get_body_content_html", lambda browser: "<html></html>")
    monkeypatch.setattr(law_go_kr, "parse_law_body_html", lambda html: {"law_id": "x", "articles": [], "addenda": []})
    monkeypatch.setattr(
        law_go_kr, "law_detail_to_items", lambda meta, source_url="": [{"id": f"item-{source_url}"}]
    )

    browser = MagicMock()
    browser.current_url = "https://example"
    items = crawl_watchlist_items(browser=browser, names=["실패하는법", "성공하는법"])

    assert items == [{"id": "item-https://example"}]
    browser.quit.assert_not_called()  # caller-supplied browser must not be closed by us


def test_crawl_watchlist_items_defaults_to_law_watchlist(monkeypatch):
    seen_names = []

    def fake_open_or_reg(browser, name):
        seen_names.append(name)
        raise RuntimeError("skip everything, just record names")

    monkeypatch.setattr(law_go_kr, "open_law_or_reg_detail_by_name", fake_open_or_reg)

    crawl_watchlist_items(browser=MagicMock())

    assert seen_names == LAW_WATCHLIST


# ---------------------------------------------------------------------------
# _watchlist_date_lookup / crawl_watchlist_items_incremental
# ---------------------------------------------------------------------------

def _listing_html(name_col: str, rows: list[tuple[str, str]], other_date_col: str = "공포일자") -> str:
    """_parse_listing_table이 파싱할 수 있는 최소한의 목록 테이블 HTML.

    other_date_col(공포일자/발령일자)과 시행일자를 둘 다 넣어, 실제 페이지처럼
    두 날짜가 같이 나오는 상황을 흉내낸다 -- _watchlist_date_lookup은 이제
    시행일자만 읽으므로, other_date_col 값은 "다른 컬럼도 있지만 무시된다"는
    걸 보여주는 용도로 시행일자와 다른 값을 넣어둔다."""
    trs = "\n".join(
        f"<tr><td>{i + 1}</td><td>{name}</td><td>9999.12.31.</td><td>{date}</td></tr>"
        for i, (name, date) in enumerate(rows)
    )
    return f"""
    <table>
    <tr><th scope="col">번호</th><th scope="col">{name_col}</th><th scope="col">{other_date_col}</th><th scope="col">시행일자</th></tr>
    {trs}
    </table>
    """


def test_watchlist_date_lookup_prefers_law_over_reg_and_picks_latest_date(monkeypatch):
    """전체 목록을 한 번씩 훑는 대신, 이제 이름별로 law.go.kr 검색(query=)
    결과를 하나씩 확인한다 -- move_to_home이 호출될 때마다 그 site_category/
    query에 맞는 검색 결과 HTML을 browser.page_source에 채워 넣어 흉내낸다.

    시행일자를 기준으로 비교한다는 걸 확실히 하려고, 각 행의 공포일자/
    발령일자는 일부러 시행일자와 동떨어진 값(9999.12.31.)으로 채워뒀다 --
    그 값이 결과에 나오면 시행일자 대신 공포일자/발령일자를 읽고 있다는 뜻."""
    law_results = {
        "개인정보보호법": _listing_html("법령명", [("개인정보보호법", "2023.01.01."), ("개인정보보호법", "2024.06.15.")]),
        "은행법": _listing_html("법령명", [("은행법", "2022.03.01.")]),
        "자금세탁방지및공중협박자금조달금지에관한업무규정": _listing_html("법령명", []),
        "존재하지않는법": _listing_html("법령명", []),
    }
    reg_results = {
        "개인정보보호법": _listing_html("행정규칙명", [("개인정보보호법", "2099.01.01.")], other_date_col="발령일자"),
        "은행법": _listing_html("행정규칙명", [], other_date_col="발령일자"),
        "자금세탁방지및공중협박자금조달금지에관한업무규정": _listing_html(
            "행정규칙명", [("자금세탁방지및공중협박자금조달금지에관한업무규정", "2021.05.05.")], other_date_col="발령일자"
        ),
        "존재하지않는법": _listing_html("행정규칙명", [], other_date_col="발령일자"),
    }

    browser = MagicMock()

    def fake_move_to_home(b, site_category, query=""):
        results = law_results if site_category == "law" else reg_results
        b.page_source = results.get(query, "<table></table>")

    monkeypatch.setattr(law_go_kr, "move_to_home", fake_move_to_home)

    result = law_go_kr._watchlist_date_lookup(
        browser, ["개인정보보호법", "은행법", "자금세탁방지및공중협박자금조달금지에관한업무규정", "존재하지않는법"]
    )

    assert result["개인정보보호법"] == "2024-06-15"  # law의 최신 날짜, reg 값(2099)은 law에서 이미 찾아서 무시됨
    assert result["은행법"] == "2022-03-01"
    assert result["자금세탁방지및공중협박자금조달금지에관한업무규정"] == "2021-05-05"  # law엔 없고 reg에만 있음
    assert "존재하지않는법" not in result


def test_crawl_watchlist_items_incremental_first_run_crawls_everything_and_saves_state(monkeypatch, tmp_path):
    names = ["개인정보보호법", "은행법"]
    monkeypatch.setattr(law_go_kr, "_watchlist_date_lookup", lambda browser, names: {
        "개인정보보호법": "2024-01-01", "은행법": "2024-02-02"
    })
    item_by_name = {
        "개인정보보호법": [{"id": "priv-1"}],
        "은행법": [{"id": "bank-1"}],
    }
    browser = MagicMock()

    def fake_open_or_reg(b, name):
        browser.current_url = name
        return "law"

    monkeypatch.setattr(law_go_kr, "open_law_or_reg_detail_by_name", fake_open_or_reg)
    monkeypatch.setattr(law_go_kr, "get_body_content_html", lambda b: "<html></html>")
    monkeypatch.setattr(law_go_kr, "parse_law_body_html", lambda html: {"effective_date": "2024-01-01"})
    monkeypatch.setattr(law_go_kr, "law_detail_to_items", lambda meta, source_url="": item_by_name[source_url])

    state_path = tmp_path / "law_crawl_state.json"
    items = crawl_watchlist_items_incremental(browser=browser, names=names, state_path=state_path)

    assert items == [{"id": "priv-1"}, {"id": "bank-1"}]
    assert state_path.exists()
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["개인정보보호법"] == {"date": "2024-01-01", "items": [{"id": "priv-1"}]}
    assert saved["은행법"] == {"date": "2024-02-02", "items": [{"id": "bank-1"}]}


def test_crawl_watchlist_items_incremental_reuses_cache_when_date_unchanged(monkeypatch, tmp_path):
    state_path = tmp_path / "law_crawl_state.json"
    state_path.write_text(
        json.dumps({"개인정보보호법": {"date": "2024-01-01", "items": [{"id": "priv-1"}]}}), encoding="utf-8"
    )
    monkeypatch.setattr(law_go_kr, "_watchlist_date_lookup", lambda browser, names: {"개인정보보호법": "2024-01-01"})

    detail_crawl_calls = []
    monkeypatch.setattr(
        law_go_kr, "open_law_or_reg_detail_by_name", lambda b, name: detail_crawl_calls.append(name)
    )

    items = crawl_watchlist_items_incremental(
        browser=MagicMock(), names=["개인정보보호법"], state_path=state_path
    )

    assert items == [{"id": "priv-1"}]
    assert detail_crawl_calls == []  # 날짜가 같으니 상세 페이지를 다시 열지 않아야 함


def test_crawl_watchlist_items_incremental_recrawls_when_date_changed(monkeypatch, tmp_path):
    state_path = tmp_path / "law_crawl_state.json"
    state_path.write_text(
        json.dumps({"개인정보보호법": {"date": "2024-01-01", "items": [{"id": "priv-old"}]}}), encoding="utf-8"
    )
    monkeypatch.setattr(law_go_kr, "_watchlist_date_lookup", lambda browser, names: {"개인정보보호법": "2024-05-05"})
    monkeypatch.setattr(law_go_kr, "open_law_or_reg_detail_by_name", lambda b, name: "law")
    monkeypatch.setattr(law_go_kr, "get_body_content_html", lambda b: "<html></html>")
    monkeypatch.setattr(law_go_kr, "parse_law_body_html", lambda html: {"effective_date": "2024-05-05"})
    monkeypatch.setattr(law_go_kr, "law_detail_to_items", lambda meta, source_url="": [{"id": "priv-new"}])

    items = crawl_watchlist_items_incremental(
        browser=MagicMock(), names=["개인정보보호법"], state_path=state_path
    )

    assert items == [{"id": "priv-new"}]
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["개인정보보호법"] == {"date": "2024-05-05", "items": [{"id": "priv-new"}]}


def test_crawl_watchlist_items_incremental_falls_back_to_cache_on_detail_crawl_failure(monkeypatch, tmp_path):
    state_path = tmp_path / "law_crawl_state.json"
    state_path.write_text(
        json.dumps({"개인정보보호법": {"date": "2024-01-01", "items": [{"id": "priv-cached"}]}}), encoding="utf-8"
    )
    # 날짜가 바뀌어서 재크롤링을 시도하지만 실패하는 상황
    monkeypatch.setattr(law_go_kr, "_watchlist_date_lookup", lambda browser, names: {"개인정보보호법": "2024-05-05"})

    def fail(b, name):
        raise RuntimeError("사이트 접속 실패")

    monkeypatch.setattr(law_go_kr, "open_law_or_reg_detail_by_name", fail)

    items = crawl_watchlist_items_incremental(
        browser=MagicMock(), names=["개인정보보호법"], state_path=state_path
    )

    assert items == [{"id": "priv-cached"}]  # 실패해도 지난 결과를 잃지 않아야 함


def test_crawl_watchlist_items_incremental_drops_item_when_no_cache_and_crawl_fails(monkeypatch, tmp_path):
    state_path = tmp_path / "law_crawl_state.json"  # 상태 파일 없음(첫 실행) + 크롤링 실패
    monkeypatch.setattr(law_go_kr, "_watchlist_date_lookup", lambda browser, names: {})

    def fail(b, name):
        raise RuntimeError("찾을 수 없음")

    monkeypatch.setattr(law_go_kr, "open_law_or_reg_detail_by_name", fail)

    items = crawl_watchlist_items_incremental(
        browser=MagicMock(), names=["존재하지않는법"], state_path=state_path
    )

    assert items == []


# ---------------------------------------------------------------------------
# move_to_home / get_last_page_number: query 파라미터로 law.go.kr 자체 검색
# 결과에 바로 들어가는지 -- 실사용해보니 검색 없이 전체 목록을 1페이지부터
# 넘기는 방식은 감당 못 할 정도로 느려서, open_law_detail_by_name()이 검색
# 결과 안에서만 페이지를 넘기도록 바꿨다.
# ---------------------------------------------------------------------------

def _stub_ready_state(browser):
    browser.execute_script.return_value = "complete"


def test_move_to_home_without_query_hits_plain_listing_url():
    browser = MagicMock()
    _stub_ready_state(browser)

    move_to_home(browser, "law")

    browser.get.assert_called_once_with(law_go_kr.get_url("law"))


def test_move_to_home_with_query_appends_url_encoded_search_term():
    browser = MagicMock()
    _stub_ready_state(browser)

    move_to_home(browser, "law", query="은행법")

    called_url = browser.get.call_args[0][0]
    assert called_url.startswith(law_go_kr.get_url("law"))
    assert called_url.endswith("%EC%9D%80%ED%96%89%EB%B2%95")  # quote("은행법")


def test_get_last_page_number_resets_to_the_same_query_not_the_full_listing(monkeypatch):
    """get_last_page_number는 마지막 페이지 번호를 읽은 뒤 목록 맨 앞으로
    되돌아가는데, query 없이 되돌아가면 검색으로 좁혀둔 결과를 잃고 전체
    목록으로 빠져버린다 -- 반드시 같은 query로 되돌아가야 한다."""
    browser = MagicMock()
    browser.find_elements.return_value = []  # "마지막으로" 버튼도, 페이지 번호도 없는 단순 케이스
    _stub_ready_state(browser)

    calls = []
    monkeypatch.setattr(law_go_kr, "move_to_home", lambda b, site, query="": calls.append(query))

    get_last_page_number(browser, "law", query="은행법")

    assert calls == ["은행법"]


# ---------------------------------------------------------------------------
# _parse_listing_table의 _upcoming 플래그 / open_law_detail_by_name의 현행
# 버전 우선 선택: 같은 법령명이 "시행 예정" 개정 + "현행" 버전 두 행으로
# 검색 결과에 함께 나오는 실제 사례(자본시장과 금융투자업에 관한 법률)를
# 실제 검색 결과 HTML로 확인해서 반영한 것.
# ---------------------------------------------------------------------------

_LISTING_HTML_WITH_UPCOMING_AND_CURRENT_ROWS = """
<table>
<tr><th scope="col">번호</th><th scope="col">법령명</th><th scope="col">공포일자</th></tr>
<tr>
  <td>4</td>
  <td>
    <a href="#" onclick="lsViewWideAll('285957','20261113','liBgcolor3',$(this),'2','0','Y','81');">
      <span class="tx">4. <img src="/LSW/images/common/bul_list1.gif" alt="앞으로 시행될 법령" style="padding-right:3px;">&nbsp;자본시장과 금융투자업에 관한 법률</span>
      <span class="tx2">[시행 2026. 11. 13.] [법률 제21647호, 2026. 5. 12., 일부개정]</span>
    </a>
  </td>
  <td>2026.05.12</td>
</tr>
<tr>
  <td>5</td>
  <td>
    <a href="#" onclick="lsViewWideAll('283193','20260804','liBgcolor4',$(this),'3','0','Y','81');">
      <span class="tx">5. &nbsp;자본시장과 금융투자업에 관한 법률</span>
      <span class="tx2">[시행 2026. 8. 4.] [법률 제21324호, 2026. 2. 3., 일부개정]</span>
    </a>
  </td>
  <td>2026.02.03</td>
</tr>
</table>
"""


def test_parse_listing_table_flags_upcoming_rows_via_alt_image():
    from bs4 import BeautifulSoup as bs

    table_df = law_go_kr._parse_listing_table(bs(_LISTING_HTML_WITH_UPCOMING_AND_CURRENT_ROWS, "html.parser"))

    upcoming_row = table_df[table_df["번호"] == "4"].iloc[0]
    current_row = table_df[table_df["번호"] == "5"].iloc[0]
    assert bool(upcoming_row["_upcoming"]) is True
    assert bool(current_row["_upcoming"]) is False


# 실제 검색 결과 페이지 전체 HTML로 확인된 버그: "소관부처 상세설정" 필터
# 팝업의 <table>(화면엔 항상 display:none으로 숨겨져 있지만 검색어/결과와
# 무관하게 DOM엔 항상 먼저 나온다)이 실제 결과 테이블보다 앞에 나온다.
_LISTING_HTML_WITH_DECOY_FILTER_POPUP_TABLE_FIRST = """
<div id="divCptOfi" style="display:none;">
<table class="table1">
<tr><th scope="col">부처</th><th scope="col">청</th><th scope="col">위원회</th><th scope="col">기타</th></tr>
<tr><td>고용노동부</td><td>검찰청</td><td>공정거래위원회</td><td>감사원</td></tr>
</table>
</div>
<table>
<tr><th scope="col">번호</th><th scope="col">법령명</th><th scope="col">공포일자</th></tr>
<tr><td>1</td><td>금융지주회사법</td><td>2023.09.14.</td></tr>
</table>
"""


def test_parse_listing_table_skips_decoy_filter_popup_table():
    """page_source.find("table")로 첫 번째 <table>을 그냥 집으면 필터 팝업
    테이블을 파싱하게 되어 검색어와 무관하게 매번 "법령명" 컬럼이 없는
    빈 결과가 된다 -- "번호"가 첫 컬럼인 진짜 결과 테이블을 골라야 한다."""
    from bs4 import BeautifulSoup as bs

    table_df = law_go_kr._parse_listing_table(bs(_LISTING_HTML_WITH_DECOY_FILTER_POPUP_TABLE_FIRST, "html.parser"))

    assert "법령명" in table_df.columns
    assert table_df.iloc[0]["법령명"] == "금융지주회사법"


# 실제 "금융투자업규정" 행정규칙(reg) 검색 결과 페이지에서 확인: law 검색
# 페이지와 달리 여기서는 필터 팝업(div#divCptOfi)이 실제 결과 테이블보다
# DOM에서 "뒤에" 나온다(페이지마다 순서가 다를 수 있다는 뜻) -- 그래도
# "번호" 컬럼 매칭 방식은 순서와 무관하게 동작해야 한다.
_LISTING_HTML_REG_WITH_DECOY_FILTER_POPUP_TABLE_AFTER = """
<table summary="행정규칙 검색결과 목록으로 항목은 번호, 법령명, 법령종류, 발령번호, 발령일자, 제정·개정구분, 기관명입니다">
<caption>행정규칙 검색결과 목록</caption>
<thead><tr>
<th scope="col">번호</th><th scope="col">행정규칙명</th><th scope="col">행정규칙종류</th>
<th scope="col">발령번호</th><th scope="col">발령일자</th><th scope="col">시행일자</th>
<th scope="col">제정·개정구분</th><th scope="col">소관부처</th>
</tr></thead>
<tbody>
<tr><td>1</td><td class="tl"><a href="#AJAX"><strong>금융</strong><strong>투자업</strong><strong>규정</strong></a></td>
<td><p>금융위원회고시</p></td><td><p>제2026-28호</p></td><td>2026. 7. 8.</td><td>2026. 7. 8.</td>
<td>일부개정</td><td class="tl"><p>금융위원회</p></td></tr>
<tr><td>2</td><td class="tl"><a href="#AJAX"><strong>금융</strong><strong>투자업</strong><strong>규정</strong>시행세칙</a></td>
<td><p>금융감독원세칙</p></td><td>-</td><td>2026. 7. 6.</td><td>2026. 7. 8.</td>
<td>일부개정</td><td class="tl"><p>금융감독원</p></td></tr>
</tbody>
</table>
<div id="divCptOfi" style="display:none;">
<table class="table1">
<tr><th scope="col">부처</th><th scope="col">청</th><th scope="col">위원회</th><th scope="col">기타</th></tr>
<tr><td>금융위원회</td><td>-</td><td>-</td><td>-</td></tr>
</table>
</div>
"""


def test_parse_listing_table_skips_decoy_filter_popup_table_when_popup_comes_after():
    """행정규칙(reg) 검색 결과 실제 HTML로 확인: 필터 팝업이 결과 테이블보다
    뒤에 나오는 경우도 있다 -- "첫 번째 테이블이 항상 팝업"이라고 가정하면
    안 되고, "번호" 헤더로 골라야 순서와 무관하게 항상 맞다."""
    from bs4 import BeautifulSoup as bs

    table_df = law_go_kr._parse_listing_table(
        bs(_LISTING_HTML_REG_WITH_DECOY_FILTER_POPUP_TABLE_AFTER, "html.parser")
    )

    assert "행정규칙명" in table_df.columns
    assert list(table_df["행정규칙명"]) == ["금융투자업규정", "금융투자업규정시행세칙"]
    assert list(table_df["발령일자"]) == ["2026. 7. 8.", "2026. 7. 6."]


def test_open_law_detail_by_name_prefers_non_upcoming_row_when_names_match(monkeypatch):
    """실제 검색 결과처럼 같은 법령명이 두 행(시행 예정 + 현행)으로 나올 때,
    시행 예정(_upcoming=True) 대신 현행 쪽을 클릭해야 한다."""
    df = pd.DataFrame(
        {
            "번호": ["4", "5"],
            "법령명": ["자본시장과 금융투자업에 관한 법률", "자본시장과 금융투자업에 관한 법률"],
            "_upcoming": [True, False],
        }
    )

    monkeypatch.setattr(law_go_kr, "move_to_home", lambda *a, **k: None)
    monkeypatch.setattr(law_go_kr, "get_last_page_number", lambda *a, **k: 1)
    monkeypatch.setattr(law_go_kr, "_goto_page_with_retry", lambda *a, **k: None)
    monkeypatch.setattr(law_go_kr, "_wait_for_fresh_table", lambda *a, **k: df)
    monkeypatch.setattr(law_go_kr, "wait_for_detail_page", lambda *a, **k: None)

    clicked = []
    monkeypatch.setattr(law_go_kr, "click_row_by_number", lambda browser, n: clicked.append(n))

    open_law_detail_by_name(MagicMock(), "law", "자본시장과 금융투자업에 관한 법률")

    assert clicked == [5]  # 현행(번호 5)을 골라야지 시행예정(번호 4)이 아님


def test_open_law_detail_by_name_clicks_only_match_when_none_are_upcoming(monkeypatch):
    df = pd.DataFrame({"번호": ["1"], "법령명": ["은행법"], "_upcoming": [False]})

    monkeypatch.setattr(law_go_kr, "move_to_home", lambda *a, **k: None)
    monkeypatch.setattr(law_go_kr, "get_last_page_number", lambda *a, **k: 1)
    monkeypatch.setattr(law_go_kr, "_goto_page_with_retry", lambda *a, **k: None)
    monkeypatch.setattr(law_go_kr, "_wait_for_fresh_table", lambda *a, **k: df)
    monkeypatch.setattr(law_go_kr, "wait_for_detail_page", lambda *a, **k: None)

    clicked = []
    monkeypatch.setattr(law_go_kr, "click_row_by_number", lambda browser, n: clicked.append(n))

    open_law_detail_by_name(MagicMock(), "law", "은행법")

    assert clicked == [1]


# ---------------------------------------------------------------------------
# get_page_range: last_page_number이 1, 6, 11, ...(새 5개 구간의 시작점과
# 같은 값)일 때 그 마지막 페이지가 통째로 누락되던 경계값 버그.
# ---------------------------------------------------------------------------

def test_get_page_range_covers_a_single_page_result():
    """query= 검색 결과가 1페이지짜리일 때 실제로 재현된 버그: 예전 코드는
    이 경우 빈 리스트를 반환해서 그 유일한 페이지조차 확인하지 못했다."""
    assert get_page_range(1) == [(1, 1)]


def test_get_page_range_covers_exact_multiples_of_five():
    assert get_page_range(5) == [(1, 5)]
    assert get_page_range(10) == [(1, 5), (6, 10)]


def test_get_page_range_covers_one_past_a_multiple_of_five():
    assert get_page_range(6) == [(1, 5), (6, 6)]
    assert get_page_range(11) == [(1, 5), (6, 10), (11, 11)]


def test_get_page_range_covers_arbitrary_counts():
    assert get_page_range(7) == [(1, 5), (6, 7)]
    assert get_page_range(23) == [(1, 5), (6, 10), (11, 15), (16, 20), (21, 23)]
