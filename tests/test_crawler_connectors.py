from datetime import date

import pytest

from ontology.schema import EntityType, RelationType
from pipeline.connectors.base import RawDocument
from pipeline.connectors.case import CaseConnector
from pipeline.connectors.interpretation import InterpretationConnector
from pipeline.connectors.law import LawConnector


def test_documents_dev_mode_returns_fixed_list_unchanged():
    docs = [RawDocument(external_id="1", entity_type=EntityType.LAW, title="t", body="b")]
    connector = LawConnector(documents=docs)
    assert connector.fetch() is docs


def test_no_documents_and_no_fetch_items_raises():
    connector = LawConnector()
    with pytest.raises(NotImplementedError):
        connector.fetch()


def test_fetch_items_maps_dict_to_raw_document():
    def crawl():
        return [
            {
                "id": "capital-markets-act-46",
                "title": "자본시장법 제46조",
                "body": "적합성 원칙 조문 본문",
                "effective_date": "2023-07-01",
                "source_url": "https://law.go.kr/foo",
            }
        ]

    connector = LawConnector(fetch_items=crawl)
    docs = connector.fetch()

    assert len(docs) == 1
    doc = docs[0]
    assert doc.external_id == "capital-markets-act-46"
    assert doc.entity_type == EntityType.LAW
    assert doc.title == "자본시장법 제46조"
    assert doc.effective_date == date(2023, 7, 1)
    assert doc.source == "https://law.go.kr/foo"
    assert doc.entity_id == "law:capital-markets-act-46"
    assert connector.errors == []


def test_law_supersedes_convenience_key_creates_relation():
    def crawl():
        return [
            {
                "id": "v2",
                "title": "신법",
                "body": "본문",
                "supersedes": "law:v1",
            }
        ]

    connector = LawConnector(fetch_items=crawl)
    doc = connector.fetch()[0]
    assert doc.relations == [(RelationType.SUPERSEDES, "law:v1")]


def test_law_supersedes_accepts_list_of_targets():
    def crawl():
        return [{"id": "v3", "title": "t", "body": "b", "supersedes": ["law:v1", "law:v2"]}]

    connector = LawConnector(fetch_items=crawl)
    doc = connector.fetch()[0]
    assert set(doc.relations) == {(RelationType.SUPERSEDES, "law:v1"), (RelationType.SUPERSEDES, "law:v2")}


def test_generic_relations_key_is_also_applied():
    def crawl():
        return [{"id": "v1", "title": "t", "body": "b", "relations": [["cites", "regulation:9"]]}]

    connector = LawConnector(fetch_items=crawl)
    doc = connector.fetch()[0]
    assert doc.relations == [(RelationType.CITES, "regulation:9")]


def test_interpretation_interprets_convenience_key():
    def crawl():
        return [{"id": "i1", "title": "회신", "body": "본문", "interprets": "law:capital-markets-act-46"}]

    connector = InterpretationConnector(fetch_items=crawl)
    doc = connector.fetch()[0]
    assert doc.entity_type == EntityType.INTERPRETATION
    assert doc.relations == [(RelationType.INTERPRETS, "law:capital-markets-act-46")]


def test_case_violates_convenience_key():
    def crawl():
        return [{"id": "c1", "title": "제재", "body": "본문", "violates": "regulation:67"}]

    connector = CaseConnector(fetch_items=crawl)
    doc = connector.fetch()[0]
    assert doc.entity_type == EntityType.CASE
    assert doc.relations == [(RelationType.VIOLATES, "regulation:67")]


def test_item_missing_required_field_is_collected_as_error_not_raised():
    def crawl():
        return [{"id": "bad", "title": "제목만 있고 본문 없음"}]

    connector = LawConnector(fetch_items=crawl)
    docs = connector.fetch()

    assert docs == []
    assert len(connector.errors) == 1
    assert connector.errors[0][0] == {"id": "bad", "title": "제목만 있고 본문 없음"}


def test_partial_batch_failure_does_not_block_other_items():
    def crawl():
        return [
            {"id": "ok", "title": "정상", "body": "본문"},
            {"id": "bad", "title": "본문 없음"},
        ]

    connector = LawConnector(fetch_items=crawl)
    docs = connector.fetch()

    assert [d.external_id for d in docs] == ["ok"]
    assert len(connector.errors) == 1
