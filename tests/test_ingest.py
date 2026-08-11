from datetime import date

from knowledge.embedder import HashEmbedder
from knowledge.graph_store import NetworkXGraphStore
from knowledge.vector_store import InMemoryVectorStore
from ontology.schema import EntityType, RelationType
from pipeline.connectors.base import RawDocument
from pipeline.ingest import IngestPipeline


def test_review_body_is_masked_on_ingest():
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    pipeline = IngestPipeline(HashEmbedder(), vector_store, graph_store)

    doc = RawDocument(
        external_id="r1",
        entity_type=EntityType.REVIEW,
        title="검토서",
        body="고객 연락처 010-1234-5678 확인",
        allowed_depts=("IB",),
    )
    pipeline.ingest_documents([doc])

    entity = graph_store.get_entity("review:r1")
    assert "010-1234-5678" not in entity.body


def test_non_review_body_is_not_masked():
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    pipeline = IngestPipeline(HashEmbedder(), vector_store, graph_store)

    doc = RawDocument(
        external_id="l1",
        entity_type=EntityType.LAW,
        title="법령",
        body="연락처 010-1234-5678 (실제로는 조문 예시일 뿐 마스킹 대상 아님)",
    )
    pipeline.ingest_documents([doc])

    entity = graph_store.get_entity("law:l1")
    assert "010-1234-5678" in entity.body


def test_relations_are_created_in_graph():
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    pipeline = IngestPipeline(HashEmbedder(), vector_store, graph_store)

    docs = [
        RawDocument(external_id="v1", entity_type=EntityType.LAW, title="구법", body="구법 본문"),
        RawDocument(
            external_id="v2",
            entity_type=EntityType.LAW,
            title="신법",
            body="신법 본문",
            relations=[(RelationType.SUPERSEDES, "law:v1")],
        ),
    ]
    pipeline.ingest_documents(docs)

    rels = graph_store.relations_from("law:v2", RelationType.SUPERSEDES)
    assert [r.target_id for r in rels] == ["law:v1"]


def test_ingest_populates_vector_store():
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    pipeline = IngestPipeline(HashEmbedder(), vector_store, graph_store)

    doc = RawDocument(external_id="f1", entity_type=EntityType.FAQ, title="FAQ 제목", body="FAQ 본문 내용")
    pipeline.ingest_documents([doc])

    embedder = HashEmbedder()
    results = vector_store.search(embedder.embed_one("FAQ 제목 FAQ 본문 내용"), top_k=5, dept="ANY")
    assert "faq:f1" in {m.entity_id for m in results}


def test_ingest_empty_list_is_a_noop():
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    pipeline = IngestPipeline(HashEmbedder(), vector_store, graph_store)
    assert pipeline.ingest_documents([]) == 0
