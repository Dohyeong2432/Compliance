from datetime import date

from knowledge.embedder import HashEmbedder
from knowledge.graph_store import NetworkXGraphStore
from knowledge.vector_store import InMemoryVectorStore
from ontology.schema import EntityType, RelationType
from pipeline.connectors.base import RawDocument
from pipeline.ingest import IngestPipeline


class CountingEmbedder(HashEmbedder):
    """Wraps HashEmbedder but records exactly which texts were actually
    sent through embed() -- used to assert the embed cache is skipping
    calls, not just to get a working vector back."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.embedded_texts: list[str] = []

    def embed(self, texts):
        self.embedded_texts.extend(texts)
        return super().embed(texts)


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


def _doc(body="법령 본문"):
    return RawDocument(external_id="l1", entity_type=EntityType.LAW, title="법령", body=body)


def test_embed_cache_skips_embedder_call_for_unchanged_document(tmp_path):
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    embedder = CountingEmbedder()
    pipeline = IngestPipeline(embedder, vector_store, graph_store, embed_cache_path=tmp_path / "cache.json")

    pipeline.ingest_documents([_doc()])
    assert embedder.embedded_texts == ["법령\n법령 본문"]

    pipeline.ingest_documents([_doc()])  # 내용 동일 -- 두 번째 호출은 임베딩 API를 다시 부르면 안 됨
    assert embedder.embedded_texts == ["법령\n법령 본문"]  # 여전히 1번만


def test_embed_cache_recomputes_when_content_changes(tmp_path):
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    embedder = CountingEmbedder()
    pipeline = IngestPipeline(embedder, vector_store, graph_store, embed_cache_path=tmp_path / "cache.json")

    pipeline.ingest_documents([_doc(body="구 본문")])
    pipeline.ingest_documents([_doc(body="개정된 본문")])

    assert embedder.embedded_texts == ["법령\n구 본문", "법령\n개정된 본문"]


def test_embed_cache_persists_across_pipeline_instances(tmp_path):
    cache_path = tmp_path / "cache.json"
    embedder1 = CountingEmbedder()
    pipeline1 = IngestPipeline(embedder1, InMemoryVectorStore(), NetworkXGraphStore(), embed_cache_path=cache_path)
    pipeline1.ingest_documents([_doc()])
    assert embedder1.embedded_texts == ["법령\n법령 본문"]

    # 새 프로세스에서 다시 뜬 것처럼, 완전히 새 인스턴스(빈 vector/graph store)로 같은 캐시 파일을 가리킴
    embedder2 = CountingEmbedder()
    pipeline2 = IngestPipeline(embedder2, InMemoryVectorStore(), NetworkXGraphStore(), embed_cache_path=cache_path)
    pipeline2.ingest_documents([_doc()])

    assert embedder2.embedded_texts == []  # 디스크에 저장된 캐시를 재사용, 다시 임베딩 안 함


def test_embed_cache_hit_still_upserts_vector_store():
    """임베딩 자체는 건너뛰어도, 벡터 스토어엔 매번 다시 넣어야 한다 --
    memory 백엔드는 재시작하면 비어 있으므로 upsert까지 건너뛰면 검색이 안 됨."""
    embedder = CountingEmbedder()
    vector_store = InMemoryVectorStore()
    pipeline = IngestPipeline(embedder, vector_store, NetworkXGraphStore(), embed_cache_path=None)
    pipeline.ingest_documents([_doc()])

    fresh_vector_store = InMemoryVectorStore()
    pipeline2 = IngestPipeline(embedder, fresh_vector_store, NetworkXGraphStore(), embed_cache_path=None)
    pipeline2.ingest_documents([_doc()])

    results = fresh_vector_store.search(embedder.embed_one("법령 법령 본문"), top_k=5, dept="ANY")
    assert "law:l1" in {m.entity_id for m in results}


def test_no_embed_cache_path_means_every_ingest_recomputes():
    embedder = CountingEmbedder()
    pipeline = IngestPipeline(embedder, InMemoryVectorStore(), NetworkXGraphStore(), embed_cache_path=None)

    pipeline.ingest_documents([_doc()])
    pipeline.ingest_documents([_doc()])

    assert embedder.embedded_texts == ["법령\n법령 본문", "법령\n법령 본문"]
