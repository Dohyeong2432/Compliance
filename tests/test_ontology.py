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
