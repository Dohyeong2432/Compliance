import tempfile
from datetime import date

import pytest

from knowledge.embedder import HashEmbedder
from knowledge.vector_store import ChromaVectorStore, InMemoryVectorStore, VectorRecord, VectorStore

EMB = HashEmbedder()


def _make_memory() -> VectorStore:
    return InMemoryVectorStore()


def _make_chroma() -> VectorStore:
    pytest.importorskip("chromadb")
    return ChromaVectorStore(tempfile.mkdtemp())


@pytest.fixture(params=["memory", "chroma"])
def vector_store(request) -> VectorStore:
    if request.param == "memory":
        return _make_memory()
    return _make_chroma()


def test_search_returns_dept_visible_records_only(vector_store):
    q = EMB.embed_one("고령투자자 보호 기준")
    vector_store.upsert(
        [
            VectorRecord("all1", EMB.embed_one("고령투자자 보호 기준 신법"), "text", allowed_depts=("ALL",)),
            VectorRecord("ib1", EMB.embed_one("고령투자자 보호 기준 IB 전용"), "text", allowed_depts=("IB",)),
        ]
    )
    retail_results = {m.entity_id for m in vector_store.search(q, top_k=5, dept="RETAIL")}
    ib_results = {m.entity_id for m in vector_store.search(q, top_k=5, dept="IB")}

    assert "ib1" not in retail_results
    assert "ib1" in ib_results
    assert "all1" in retail_results
    assert "all1" in ib_results


def test_search_excludes_records_outside_effective_window(vector_store):
    q = EMB.embed_one("적합성 원칙 기준")
    vector_store.upsert(
        [
            VectorRecord(
                "old",
                EMB.embed_one("적합성 원칙 기준 구법"),
                "text",
                effective_date=date(2020, 1, 1),
                superseded_date=date(2023, 1, 1),
            ),
            VectorRecord(
                "new",
                EMB.embed_one("적합성 원칙 기준 신법"),
                "text",
                effective_date=date(2023, 1, 1),
            ),
        ]
    )
    at_2021 = {m.entity_id for m in vector_store.search(q, top_k=5, dept="RETAIL", as_of=date(2021, 1, 1))}
    at_2024 = {m.entity_id for m in vector_store.search(q, top_k=5, dept="RETAIL", as_of=date(2024, 1, 1))}

    assert at_2021 == {"old"}
    assert at_2024 == {"new"}


def test_search_respects_top_k(vector_store):
    q = EMB.embed_one("공통 검색어 테스트")
    vector_store.upsert(
        [VectorRecord(f"doc{i}", EMB.embed_one(f"공통 검색어 테스트 문서 {i}"), "text") for i in range(10)]
    )
    results = vector_store.search(q, top_k=3, dept="RETAIL")
    assert len(results) <= 3


def test_delete_removes_record_from_search_results(vector_store):
    q = EMB.embed_one("삭제 대상 문서 테스트")
    vector_store.upsert([VectorRecord("doc1", EMB.embed_one("삭제 대상 문서 테스트"), "text")])
    assert "doc1" in {m.entity_id for m in vector_store.search(q, top_k=5, dept="ANY")}

    vector_store.delete("doc1")

    assert "doc1" not in {m.entity_id for m in vector_store.search(q, top_k=5, dept="ANY")}


def test_delete_missing_id_is_a_noop(vector_store):
    vector_store.delete("nope")  # must not raise


def test_upsert_overwrites_existing_record(vector_store):
    vector_store.upsert([VectorRecord("doc1", EMB.embed_one("첫 버전 텍스트"), "text", allowed_depts=("IB",))])
    vector_store.upsert([VectorRecord("doc1", EMB.embed_one("두번째 버전 텍스트"), "text", allowed_depts=("ALL",))])

    q = EMB.embed_one("두번째 버전 텍스트")
    results = vector_store.search(q, top_k=5, dept="RETAIL")
    assert "doc1" in {m.entity_id for m in results}


def test_entity_types_filter_restricts_search_to_requested_sources(vector_store):
    """소스 타입은 entity_id 접두사에서 복원하므로 레코드 스키마 변경이나
    기존 색인 마이그레이션 없이 필터가 동작해야 한다."""
    q = EMB.embed_one("업무위탁 관련 문서")
    vector_store.upsert([VectorRecord("law:1", EMB.embed_one("업무위탁 관련 문서"), "text")])
    vector_store.upsert([VectorRecord("review:1", EMB.embed_one("업무위탁 관련 문서"), "text")])

    from ontology.schema import EntityType

    results = vector_store.search(q, top_k=5, dept="ANY", entity_types=(EntityType.LAW,))

    assert {m.entity_id for m in results} == {"law:1"}


def test_entity_types_none_searches_every_source(vector_store):
    q = EMB.embed_one("업무위탁 관련 문서")
    vector_store.upsert([VectorRecord("law:1", EMB.embed_one("업무위탁 관련 문서"), "text")])
    vector_store.upsert([VectorRecord("review:1", EMB.embed_one("업무위탁 관련 문서"), "text")])

    results = vector_store.search(q, top_k=5, dept="ANY", entity_types=None)

    assert {m.entity_id for m in results} == {"law:1", "review:1"}
