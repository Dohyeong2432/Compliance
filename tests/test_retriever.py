from datetime import date

import pytest

from knowledge.embedder import HashEmbedder
from knowledge.graph_store import NetworkXGraphStore
from knowledge.retriever import HybridRetriever
from knowledge.vector_store import InMemoryVectorStore, VectorRecord
from ontology.schema import Entity, EntityType, Relation, RelationType

EMB = HashEmbedder()


def _seed(graph_store, vector_store):
    law_old = Entity(
        "law:old", EntityType.LAW, "노인 보호 구법", "구법 본문 내용",
        effective_date=date(2020, 1, 1), superseded_date=date(2023, 1, 1),
    )
    law_new = Entity(
        "law:new", EntityType.LAW, "노인 보호 신법", "신법 본문 강화 내용",
        effective_date=date(2023, 1, 1),
    )
    case = Entity("case:1", EntityType.CASE, "제재사례 A", "노인 보호 위반 제재사례", allowed_depts=("ALL",))
    ib_review = Entity("review:1", EntityType.REVIEW, "IB 전용 검토서", "노인 보호 관련 IB 검토", allowed_depts=("IB",))

    for entity in (law_old, law_new, case, ib_review):
        graph_store.add_entity(entity)
        vector_store.upsert(
            [
                VectorRecord(
                    entity.id,
                    EMB.embed_one(entity.title + entity.body),
                    entity.body,
                    allowed_depts=entity.allowed_depts,
                    effective_date=entity.effective_date,
                    superseded_date=entity.superseded_date,
                )
            ]
        )

    graph_store.add_relation(Relation("law:new", RelationType.SUPERSEDES, "law:old"))
    graph_store.add_relation(Relation("case:1", RelationType.VIOLATES, "law:new"))

    # A vector hit with no backing graph entity at all -- must never surface.
    vector_store.upsert(
        [VectorRecord("ghost:1", EMB.embed_one("노인 보호 유령 문서 허위 정보"), "ghost text")]
    )

    return law_old, law_new, case, ib_review


@pytest.fixture
def retriever():
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    _seed(graph_store, vector_store)
    return HybridRetriever(EMB, vector_store, graph_store), graph_store, vector_store


def test_ghost_vector_hit_never_surfaces(retriever):
    hr, _, _ = retriever
    docs = hr.retrieve("노인 보호 유령 문서", dept="RETAIL", as_of=date(2024, 1, 1))
    assert "ghost:1" not in {d.entity.id for d in docs}


def test_time_resolution_swaps_to_version_valid_at_as_of(retriever):
    hr, _, _ = retriever
    past = hr.retrieve("노인 보호 기준", dept="RETAIL", as_of=date(2021, 6, 1))
    present = hr.retrieve("노인 보호 기준", dept="RETAIL", as_of=date(2024, 6, 1))

    assert "law:old" in {d.entity.id for d in past}
    assert "law:new" not in {d.entity.id for d in past}
    assert "law:new" in {d.entity.id for d in present}
    assert "law:old" not in {d.entity.id for d in present}


def test_rbac_hides_ib_scoped_review_from_other_depts(retriever):
    hr, _, _ = retriever
    retail_docs = hr.retrieve("IB 검토서 노인 보호", dept="RETAIL", as_of=date(2024, 1, 1))
    ib_docs = hr.retrieve("IB 검토서 노인 보호", dept="IB", as_of=date(2024, 1, 1))

    assert "review:1" not in {d.entity.id for d in retail_docs}
    assert "review:1" in {d.entity.id for d in ib_docs}


def test_dept_is_required(retriever):
    hr, _, _ = retriever
    with pytest.raises(ValueError):
        hr.retrieve("아무 질의", dept="")


def test_expansion_pulls_in_related_case(retriever):
    hr, _, _ = retriever
    docs = hr.retrieve("노인 보호 신법 조항", dept="RETAIL", as_of=date(2024, 1, 1), top_k=1)
    ids = {d.entity.id for d in docs}
    assert "law:new" in ids
    assert "case:1" in ids
