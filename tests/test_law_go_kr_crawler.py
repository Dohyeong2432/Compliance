from pathlib import Path

from crawlers.law_go_kr import law_detail_to_items, parse_law_body_html
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
