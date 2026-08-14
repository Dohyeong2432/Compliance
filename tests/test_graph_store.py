import tempfile
from datetime import date
from pathlib import Path

import pytest

from knowledge.graph_store import GraphStore, NetworkXGraphStore
from ontology.schema import Entity, EntityType, Relation, RelationType


def _make_networkx() -> GraphStore:
    return NetworkXGraphStore()


def _make_kuzu() -> GraphStore:
    kuzu = pytest.importorskip("kuzu")
    del kuzu
    from knowledge.graph_store import KuzuGraphStore

    db_path = str(Path(tempfile.mkdtemp()) / "graph.kuzu")
    return KuzuGraphStore(db_path)


@pytest.fixture(params=["networkx", "kuzu"])
def graph_store(request) -> GraphStore:
    if request.param == "networkx":
        return _make_networkx()
    return _make_kuzu()


def _law(entity_id, effective_date=None, superseded_date=None) -> Entity:
    return Entity(
        id=entity_id,
        type=EntityType.LAW,
        title=entity_id,
        body=f"body of {entity_id}",
        effective_date=effective_date,
        superseded_date=superseded_date,
    )


def test_get_entity_missing_returns_none(graph_store):
    assert graph_store.get_entity("nope") is None
    assert graph_store.has_entity("nope") is False


def test_add_and_get_entity_roundtrip(graph_store):
    graph_store.add_entity(_law("law:1", date(2020, 1, 1)))
    got = graph_store.get_entity("law:1")
    assert got is not None
    assert got.id == "law:1"
    assert got.effective_date == date(2020, 1, 1)
    assert graph_store.has_entity("law:1") is True


def test_relations_from_and_to_filter_by_type(graph_store):
    graph_store.add_entity(_law("case:1"))
    graph_store.add_entity(_law("law:1"))
    graph_store.add_relation(Relation("case:1", RelationType.VIOLATES, "law:1"))
    graph_store.add_relation(Relation("case:1", RelationType.CITES, "law:1"))

    violates = graph_store.relations_from("case:1", RelationType.VIOLATES)
    assert [r.target_id for r in violates] == ["law:1"]

    all_out = graph_store.relations_from("case:1")
    assert {r.type for r in all_out} == {RelationType.VIOLATES, RelationType.CITES}

    incoming = graph_store.relations_to("law:1", RelationType.VIOLATES)
    assert [r.source_id for r in incoming] == ["case:1"]


def test_supersede_chain_orders_oldest_first(graph_store):
    graph_store.add_entity(_law("law:v1", date(2020, 1, 1), date(2022, 1, 1)))
    graph_store.add_entity(_law("law:v2", date(2022, 1, 1), date(2024, 1, 1)))
    graph_store.add_entity(_law("law:v3", date(2024, 1, 1)))
    graph_store.add_relation(Relation("law:v2", RelationType.SUPERSEDES, "law:v1"))
    graph_store.add_relation(Relation("law:v3", RelationType.SUPERSEDES, "law:v2"))

    chain = graph_store.supersede_chain("law:v1")
    assert [e.id for e in chain] == ["law:v1", "law:v2", "law:v3"]

    chain_from_middle = graph_store.supersede_chain("law:v2")
    assert [e.id for e in chain_from_middle] == ["law:v1", "law:v2", "law:v3"]


def test_resolve_effective_version_picks_version_valid_at_as_of(graph_store):
    graph_store.add_entity(_law("law:v1", date(2020, 1, 1), date(2022, 1, 1)))
    graph_store.add_entity(_law("law:v2", date(2022, 1, 1), date(2024, 1, 1)))
    graph_store.add_entity(_law("law:v3", date(2024, 1, 1)))
    graph_store.add_relation(Relation("law:v2", RelationType.SUPERSEDES, "law:v1"))
    graph_store.add_relation(Relation("law:v3", RelationType.SUPERSEDES, "law:v2"))

    assert graph_store.resolve_effective_version("law:v3", date(2021, 6, 1)).id == "law:v1"
    assert graph_store.resolve_effective_version("law:v1", date(2023, 6, 1)).id == "law:v2"
    assert graph_store.resolve_effective_version("law:v1", date(2025, 1, 1)).id == "law:v3"


def test_resolve_effective_version_before_earliest_falls_back_to_none_or_earliest(graph_store):
    graph_store.add_entity(_law("law:v1", date(2020, 1, 1)))
    resolved = graph_store.resolve_effective_version("law:v1", date(2019, 1, 1))
    assert resolved is None


def test_resolve_effective_version_unknown_entity_returns_none(graph_store):
    assert graph_store.resolve_effective_version("nope", date(2024, 1, 1)) is None


def test_expand_related_respects_limit_and_excludes_self(graph_store):
    graph_store.add_entity(_law("law:1"))
    for i in range(5):
        graph_store.add_entity(_law(f"case:{i}"))
        graph_store.add_relation(Relation(f"case:{i}", RelationType.VIOLATES, "law:1"))

    related = graph_store.expand_related("law:1", limit=3)
    assert len(related) == 3
    assert all(e.id != "law:1" for e in related)


def test_delete_entity_removes_it(graph_store):
    graph_store.add_entity(_law("law:1"))
    assert graph_store.has_entity("law:1") is True

    graph_store.delete_entity("law:1")

    assert graph_store.has_entity("law:1") is False
    assert graph_store.get_entity("law:1") is None


def test_delete_entity_missing_id_is_a_noop(graph_store):
    graph_store.delete_entity("nope")  # must not raise
    assert graph_store.has_entity("nope") is False


def test_delete_entity_also_drops_its_relations(graph_store):
    graph_store.add_entity(_law("case:1"))
    graph_store.add_entity(_law("law:1"))
    graph_store.add_relation(Relation("case:1", RelationType.VIOLATES, "law:1"))

    graph_store.delete_entity("case:1")

    assert graph_store.relations_to("law:1", RelationType.VIOLATES) == []


def test_find_entities_by_title_substring_matches_exact_article_only(graph_store):
    """"제2조"로 검색하면 "제20조"(부분문자열 아님)는 걸리지 않고 "제2조"만
    걸려야 한다 -- 조문 인용 검색의 정확도가 여기 달려있다."""
    graph_store.add_entity(
        Entity(id="law:1-0", type=EntityType.LAW, title="금융지주회사법 제1조(목적)", body="본문")
    )
    graph_store.add_entity(
        Entity(id="law:2-0", type=EntityType.LAW, title="금융지주회사법 제2조(정의)", body="본문")
    )
    graph_store.add_entity(
        Entity(id="law:20-0", type=EntityType.LAW, title="금융지주회사법 제20조(승인)", body="본문")
    )

    matches = graph_store.find_entities_by_title_substring("제2조")

    assert {e.id for e in matches} == {"law:2-0"}


def test_find_entities_by_title_substring_no_match_returns_empty(graph_store):
    graph_store.add_entity(_law("law:1"))
    assert graph_store.find_entities_by_title_substring("전혀 없는 문구") == []


def test_expand_related_defaults_exclude_supersedes(graph_store):
    graph_store.add_entity(_law("law:v1", date(2020, 1, 1), date(2022, 1, 1)))
    graph_store.add_entity(_law("law:v2", date(2022, 1, 1)))
    graph_store.add_relation(Relation("law:v2", RelationType.SUPERSEDES, "law:v1"))

    related = graph_store.expand_related("law:v1")
    assert related == []
