from datetime import date

from knowledge.embedder import HashEmbedder
from knowledge.graph_store import NetworkXGraphStore
from knowledge.retriever import HybridRetriever
from knowledge.vector_store import InMemoryVectorStore
from pipeline.ingest import IngestPipeline
from seed_data.seed import LAW_NEW_ID, LAW_OLD_ID, REVIEW_ID, seed_all


def _build_seeded_retriever():
    embedder = HashEmbedder()
    vector_store = InMemoryVectorStore()
    graph_store = NetworkXGraphStore()
    pipeline = IngestPipeline(embedder, vector_store, graph_store)
    ingested = seed_all(pipeline)
    return HybridRetriever(embedder, vector_store, graph_store), graph_store, ingested


def test_seed_ingests_all_six_source_types():
    _, _, ingested = _build_seeded_retriever()
    assert ingested == 7  # 2 law versions + regulation + interpretation + case + review + faq


def test_seed_review_document_is_masked():
    _, graph_store, _ = _build_seeded_retriever()
    review = graph_store.get_entity(REVIEW_ID)
    assert "010-9876-5432" not in review.body
    assert "111-222-333444" not in review.body


def test_seed_rbac_scenario_ib_only_review():
    retriever, _, _ = _build_seeded_retriever()
    retail_docs = {d.entity.id for d in retriever.retrieve("랩상품 검토서", dept="RETAIL", as_of=date(2024, 6, 1))}
    ib_docs = {d.entity.id for d in retriever.retrieve("랩상품 검토서", dept="IB", as_of=date(2024, 6, 1))}

    assert REVIEW_ID not in retail_docs
    assert REVIEW_ID in ib_docs


def test_seed_time_awareness_scenario():
    retriever, _, _ = _build_seeded_retriever()
    past = {d.entity.id for d in retriever.retrieve("적합성 원칙", dept="RETAIL", as_of=date(2021, 1, 1))}
    present = {d.entity.id for d in retriever.retrieve("적합성 원칙", dept="RETAIL", as_of=date(2024, 1, 1))}

    assert LAW_OLD_ID in past and LAW_NEW_ID not in past
    assert LAW_NEW_ID in present and LAW_OLD_ID not in present


def test_seed_case_reachable_when_effective_at_query_time():
    """The 2022 sanction case only became relevant context once it existed
    -- querying about the law's 2021 version must NOT pull in a case that
    postdates that query, but querying in the present should."""
    retriever, _, _ = _build_seeded_retriever()

    past_ids = {d.entity.id for d in retriever.retrieve("적합성 원칙 조항", dept="RETAIL", as_of=date(2021, 1, 1))}
    assert "case:2022-unsuitable-recommendation-sanction" not in past_ids

    present_ids = {d.entity.id for d in retriever.retrieve("적합성 원칙 조항", dept="RETAIL", as_of=date(2024, 1, 1))}
    assert "case:2022-unsuitable-recommendation-sanction" in present_ids
