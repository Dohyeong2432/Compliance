import json
from pathlib import Path

from knowledge.embedder import HashEmbedder
from knowledge.graph_store import NetworkXGraphStore
from knowledge.vector_store import InMemoryVectorStore
from ontology.schema import EntityType
from pipeline.connectors.base import RawDocument, SourceConnector
from pipeline.ingest import IngestPipeline
from pipeline.sync import IngestSyncer


class FakeConnector(SourceConnector):
    """Test double whose fetch() result can be changed between calls, to
    simulate a source that has been edited/removed on disk or upstream."""

    entity_type = EntityType.LAW

    def __init__(self, documents: list[RawDocument]):
        self.documents = documents
        self.errors: list[tuple[str, str]] = []

    def fetch(self) -> list[RawDocument]:
        return self.documents


class RaisingConnector(SourceConnector):
    entity_type = EntityType.CASE

    def fetch(self) -> list[RawDocument]:
        raise RuntimeError("crawler is down")


def _law_doc(external_id: str, title: str = "t") -> RawDocument:
    return RawDocument(external_id=external_id, entity_type=EntityType.LAW, title=title, body="본문")


def _make_syncer(connectors: dict[str, SourceConnector], state_path=None) -> tuple[IngestSyncer, NetworkXGraphStore, InMemoryVectorStore]:
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    pipeline = IngestPipeline(HashEmbedder(), vector_store, graph_store)
    syncer = IngestSyncer(pipeline, graph_store, vector_store, connectors, state_path=state_path)
    return syncer, graph_store, vector_store


def test_sync_once_ingests_documents():
    connector = FakeConnector([_law_doc("1"), _law_doc("2")])
    syncer, graph_store, _ = _make_syncer({"law": connector})

    report = syncer.sync_once()

    assert report.ok is True
    assert report.results[0].ingested == 2
    assert report.results[0].removed == 0
    assert graph_store.has_entity("law:1")
    assert graph_store.has_entity("law:2")


def test_sync_once_is_idempotent_on_repeat_run():
    connector = FakeConnector([_law_doc("1")])
    syncer, graph_store, _ = _make_syncer({"law": connector})

    syncer.sync_once()
    report = syncer.sync_once()

    assert report.results[0].ingested == 1
    assert report.results[0].removed == 0
    assert graph_store.has_entity("law:1")


def test_sync_once_deletes_documents_that_disappeared():
    connector = FakeConnector([_law_doc("1"), _law_doc("2")])
    syncer, graph_store, vector_store = _make_syncer({"law": connector})
    syncer.sync_once()
    assert graph_store.has_entity("law:2")

    connector.documents = [_law_doc("1")]  # "2" was removed from the source
    report = syncer.sync_once()

    assert report.results[0].removed == 1
    assert graph_store.has_entity("law:1") is True
    assert graph_store.has_entity("law:2") is False
    assert vector_store.search(HashEmbedder().embed_one("t 본문"), top_k=10, dept="ANY")
    assert "law:2" not in {
        m.entity_id for m in vector_store.search(HashEmbedder().embed_one("t 본문"), top_k=10, dept="ANY")
    }


def test_sync_once_does_not_delete_on_first_run():
    # An empty starting baseline must never be read as "everything was removed".
    connector = FakeConnector([_law_doc("1")])
    syncer, graph_store, _ = _make_syncer({"law": connector})

    report = syncer.sync_once()

    assert report.results[0].removed == 0
    assert graph_store.has_entity("law:1")


def test_one_connector_failing_does_not_block_others():
    ok_connector = FakeConnector([_law_doc("1")])
    syncer, graph_store, _ = _make_syncer({"law": ok_connector, "case": RaisingConnector()})

    report = syncer.sync_once()

    by_name = {r.name: r for r in report.results}
    assert by_name["case"].ok is False
    assert "crawler is down" in by_name["case"].errors[0]
    assert by_name["law"].ok is True
    assert graph_store.has_entity("law:1")
    assert report.ok is False  # overall report reflects the failing source


def test_ingest_failure_for_one_connector_does_not_block_others_or_raise():
    """실사용 재현: 임베딩 API 실패(쿼터 초과 등)가 sync_once() 밖으로 새어
    나가면 안 된다 -- lifespan()이 sync_once()를 그대로 await하고 있어서
    (api/main.py), 여기서 안 잡아주면 서버 시작 자체가 죽는다(재현된
    트레이스백: google.genai...ClientError: 429 RESOURCE_EXHAUSTED ->
    "Application startup failed. Exiting."). fetch() 실패와 마찬가지로
    ingest_documents() 실패도 해당 소스만 건너뛰고 나머지는 계속 성공해야
    한다."""

    class FlakyEmbedder(HashEmbedder):
        def embed(self, texts):
            if any("BOOM" in t for t in texts):
                raise RuntimeError("429 RESOURCE_EXHAUSTED")
            return super().embed(texts)

    ok_connector = FakeConnector([_law_doc("1")])
    failing_connector = FakeConnector([_law_doc("2", title="BOOM")])

    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    pipeline = IngestPipeline(FlakyEmbedder(), vector_store, graph_store)
    syncer = IngestSyncer(pipeline, graph_store, vector_store, {"law": ok_connector, "reg": failing_connector})

    report = syncer.sync_once()  # must not raise

    by_name = {r.name: r for r in report.results}
    assert by_name["reg"].ok is False
    assert "429" in by_name["reg"].errors[0]
    assert by_name["law"].ok is True
    assert graph_store.has_entity("law:1")
    assert graph_store.has_entity("law:2") is False
    assert report.ok is False


def test_connector_errors_attribute_is_surfaced_in_report():
    connector = FakeConnector([_law_doc("1")])
    connector.errors = [("bad-file.docx", "parse failed")]
    syncer, _, _ = _make_syncer({"law": connector})

    report = syncer.sync_once()

    assert "bad-file.docx: parse failed" in report.results[0].errors


def test_state_persists_across_syncer_instances(tmp_path):
    state_path = tmp_path / "sync_state.json"
    connector = FakeConnector([_law_doc("1"), _law_doc("2")])
    syncer, graph_store, _ = _make_syncer({"law": connector}, state_path=state_path)
    syncer.sync_once()
    assert json.loads(state_path.read_text())["law"] == ["law:1", "law:2"]

    # Simulate a process restart: a brand-new syncer instance, same state file,
    # backed by a *different* (also fresh) graph/vector store -- but the
    # connector's source now only has "1".
    graph_store2 = NetworkXGraphStore()
    vector_store2 = InMemoryVectorStore()
    pipeline2 = IngestPipeline(HashEmbedder(), vector_store2, graph_store2)
    connector.documents = [_law_doc("1")]
    syncer2 = IngestSyncer(pipeline2, graph_store2, vector_store2, {"law": connector}, state_path=state_path)

    report = syncer2.sync_once()

    assert report.results[0].removed == 1  # restart still remembered "2" existed before


def test_removed_document_is_dropped_from_the_lexical_index_too():
    """소스에서 사라진 문서는 그래프/벡터뿐 아니라 어휘 색인에서도 지워져야
    한다 -- 한 곳이라도 남으면 삭제된 문서가 계속 검색된다."""
    from knowledge.lexical import LexicalIndex

    lexical_index = LexicalIndex()
    graph_store = NetworkXGraphStore()
    vector_store = InMemoryVectorStore()
    pipeline = IngestPipeline(HashEmbedder(), vector_store, graph_store, lexical_index=lexical_index)
    connector = FakeConnector([_law_doc("1", title="업무위탁 조항"), _law_doc("2", title="겸직 조항")])
    syncer = IngestSyncer(pipeline, graph_store, vector_store, {"law": connector})

    syncer.sync_once()
    assert [m.entity_id for m in lexical_index.search("겸직")] == ["law:2"]

    connector.documents = [_law_doc("1", title="업무위탁 조항")]  # "2"가 소스에서 사라짐
    syncer.sync_once()

    assert lexical_index.search("겸직") == []
