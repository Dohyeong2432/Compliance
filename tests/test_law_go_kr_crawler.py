from pathlib import Path
from unittest.mock import MagicMock

import pytest
from selenium.common.exceptions import TimeoutException

import crawlers.law_go_kr as law_go_kr
from crawlers.law_go_kr import (
    click_row_by_number,
    crawl_watchlist_items,
    law_detail_to_items,
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
