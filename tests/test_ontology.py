from datetime import date

from ontology.schema import ALL_DEPARTMENTS, Entity, EntityType


def make_entity(**overrides) -> Entity:
    defaults = dict(id="e1", type=EntityType.LAW, title="t", body="b")
    defaults.update(overrides)
    return Entity(**defaults)


def test_is_effective_at_with_no_dates_is_always_effective():
    entity = make_entity()
    assert entity.is_effective_at(date(1999, 1, 1))
    assert entity.is_effective_at(date(2999, 1, 1))


def test_is_effective_at_before_effective_date_is_false():
    entity = make_entity(effective_date=date(2023, 1, 1))
    assert not entity.is_effective_at(date(2022, 12, 31))
    assert entity.is_effective_at(date(2023, 1, 1))


def test_is_effective_at_on_or_after_superseded_date_is_false():
    entity = make_entity(effective_date=date(2020, 1, 1), superseded_date=date(2023, 1, 1))
    assert entity.is_effective_at(date(2022, 12, 31))
    assert not entity.is_effective_at(date(2023, 1, 1))
    assert not entity.is_effective_at(date(2024, 1, 1))


def test_is_visible_to_all_departments_marker():
    entity = make_entity(allowed_depts=(ALL_DEPARTMENTS,))
    assert entity.is_visible_to("RETAIL")
    assert entity.is_visible_to("IB")


def test_is_visible_to_scoped_department():
    entity = make_entity(allowed_depts=("IB",))
    assert entity.is_visible_to("IB")
    assert not entity.is_visible_to("RETAIL")


def test_authority_rank_orders_binding_norms_above_internal_references():
    from ontology.schema import EntityType, authority_rank

    ranks = [
        authority_rank(EntityType.LAW),
        authority_rank(EntityType.REGULATION),
        authority_rank(EntityType.INTERPRETATION),
        authority_rank(EntityType.CASE),
        authority_rank(EntityType.REVIEW),
        authority_rank(EntityType.FAQ),
    ]
    assert ranks == sorted(ranks), "권위 위계가 법령→사내규정→유권해석→제재사례→검토서→FAQ 순이어야 한다"
    assert len(set(ranks)) == len(ranks), "동순위가 있으면 정렬 결과가 비결정적이 된다"


def test_every_entity_type_has_an_authority_label():
    from ontology.schema import AUTHORITY_LABEL, EntityType

    assert set(AUTHORITY_LABEL) == set(EntityType)


def test_entity_type_from_id_recovers_type_from_prefix():
    from ontology.schema import EntityType, entity_type_from_id

    assert entity_type_from_id("law:009374-47-0@2023-09-14") == EntityType.LAW
    assert entity_type_from_id("review:abc") == EntityType.REVIEW


def test_entity_type_from_id_returns_none_for_unparseable_ids():
    from ontology.schema import entity_type_from_id

    assert entity_type_from_id("prefixless") is None
    assert entity_type_from_id("unknowntype:1") is None
