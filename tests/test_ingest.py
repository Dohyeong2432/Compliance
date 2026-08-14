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


def test_ingest_auto_extracts_law_citation_relations_from_interpretation_body():
    """유권해석 본문이 「법령명」 제N조를 언급하면, 사람이 relations를 직접
    채우지 않아도 이미 색인된 해당 조항 entity로 CITES 관계가 자동 생성돼야
    한다 -- 수천 건의 유권해석에 일일이 법조항 id를 매핑해줄 수 없다는
    현실적 제약 때문에 만들어진 기능."""
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    pipeline = IngestPipeline(HashEmbedder(), vector_store, graph_store)

    law_doc = RawDocument(
        external_id="009374-47-0",
        entity_type=EntityType.LAW,
        title="금융지주회사법 제47조(자회사등 사이의 업무위탁)",
        body="자회사등은 업무의 일부를 다른 자회사등에게 위탁할 수 있다.",
    )
    interpretation_doc = RawDocument(
        external_id="i1",
        entity_type=EntityType.INTERPRETATION,
        title="업무위탁 관련 질의회신",
        body="「금융지주회사법」 제47조에 따라 위탁이 가능한지 질의한 사안입니다.",
    )
    pipeline.ingest_documents([law_doc, interpretation_doc])

    rels = graph_store.relations_from("interpretation:i1", RelationType.CITES)
    assert [r.target_id for r in rels] == ["law:009374-47-0"]


def test_ingest_auto_citation_extraction_only_applies_to_citing_source_types():
    """LAW/REGULATION 문서 자체는 인용 스캔 대상이 아니다(인용의 '대상'이지
    '출처'가 아님) -- 법령 본문에 다른 법 조항이 언급돼도 자동으로 CITES가
    생기면 안 된다."""
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    pipeline = IngestPipeline(HashEmbedder(), vector_store, graph_store)

    docs = [
        RawDocument(external_id="756-0", entity_type=EntityType.LAW, title="민법 제756조", body="사용자책임 본문"),
        RawDocument(
            external_id="47-0",
            entity_type=EntityType.LAW,
            title="금융지주회사법 제47조(자회사등 사이의 업무위탁)",
            body="위탁받은 자회사등이 손해를 끼친 경우 「민법」 제756조가 준용된다.",
        ),
    ]
    pipeline.ingest_documents(docs)

    assert graph_store.relations_from("law:47-0", RelationType.CITES) == []


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


def test_embed_cache_invalidated_when_model_changes(tmp_path):
    """GEMINI_EMBED_MODEL을 001에서 002로 바꾸는 것처럼 임베더 자체를
    바꾸면, 내용이 그대로인 문서도 예전 모델로 만든 벡터를 캐시에서 그냥
    재사용하면 안 된다 -- 서로 다른 모델의 벡터는 같은 벡터공간이 아니라
    코사인 유사도 비교가 무의미해진다."""
    cache_path = tmp_path / "cache.json"

    class ModelA(CountingEmbedder):
        model = "gemini-embedding-001"

    class ModelB(CountingEmbedder):
        model = "gemini-embedding-002"

    embedder_a = ModelA()
    pipeline_a = IngestPipeline(embedder_a, InMemoryVectorStore(), NetworkXGraphStore(), embed_cache_path=cache_path)
    pipeline_a.ingest_documents([_doc()])
    assert embedder_a.embedded_texts == ["법령\n법령 본문"]

    embedder_b = ModelB()
    pipeline_b = IngestPipeline(embedder_b, InMemoryVectorStore(), NetworkXGraphStore(), embed_cache_path=cache_path)
    pipeline_b.ingest_documents([_doc()])  # 내용은 동일하지만 모델이 바뀌었으니 다시 임베딩해야 함

    assert embedder_b.embedded_texts == ["법령\n법령 본문"]


def test_no_embed_cache_path_means_every_ingest_recomputes():
    embedder = CountingEmbedder()
    pipeline = IngestPipeline(embedder, InMemoryVectorStore(), NetworkXGraphStore(), embed_cache_path=None)

    pipeline.ingest_documents([_doc()])
    pipeline.ingest_documents([_doc()])

    assert embedder.embedded_texts == ["법령\n법령 본문", "법령\n법령 본문"]


def test_ingest_populates_lexical_index_when_one_is_attached():
    from knowledge.lexical import LexicalIndex

    lexical_index = LexicalIndex()
    pipeline = IngestPipeline(
        HashEmbedder(), InMemoryVectorStore(), NetworkXGraphStore(), lexical_index=lexical_index
    )
    pipeline.ingest_documents(
        [RawDocument(external_id="1", entity_type=EntityType.LAW, title="업무위탁 조항", body="본문")]
    )

    assert [m.entity_id for m in lexical_index.search("업무위탁")] == ["law:1"]


def test_ingest_without_lexical_index_still_works():
    """어휘 색인은 선택 요소다 -- 안 붙여도 기존 경로가 그대로 동작해야 한다."""
    pipeline = IngestPipeline(HashEmbedder(), InMemoryVectorStore(), NetworkXGraphStore())
    assert pipeline.ingest_documents([_doc()]) == 1


def test_lexical_index_reflects_updated_body_on_reingest():
    """sync가 매 사이클 전체 문서를 재색인하므로, 본문이 바뀌면 옛 용어가
    색인에 남아 있으면 안 된다."""
    from knowledge.lexical import LexicalIndex

    lexical_index = LexicalIndex()
    pipeline = IngestPipeline(
        HashEmbedder(), InMemoryVectorStore(), NetworkXGraphStore(), lexical_index=lexical_index
    )
    pipeline.ingest_documents([_doc(body="업무위탁 관련 내용")])
    pipeline.ingest_documents([_doc(body="겸직 제한 관련 내용")])

    assert lexical_index.search("업무위탁") == []
    assert [m.entity_id for m in lexical_index.search("겸직")] == ["law:l1"]
