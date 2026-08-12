"""Periodic re-ingestion so the knowledge stores stay in sync with their
sources without a human re-running a script by hand.

GraphStore.add_entity / VectorStore.upsert are both upsert-by-id, so simply
re-running ingest_documents() on a source's *current* fetch() result already
picks up additions and edits for free. The one thing a blind re-ingest can't
do is notice a document that used to exist and now doesn't (a file deleted
from data/raw/*, a row dropped from a crawler's feed) -- that entity would
stay retrievable forever, which is exactly the kind of silently-wrong answer
this whole project exists to prevent. IngestSyncer tracks the last-seen id
set per connector and explicitly deletes whatever dropped out.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from knowledge.graph_store import GraphStore
from knowledge.vector_store import VectorStore
from pipeline.connectors.base import SourceConnector
from pipeline.ingest import IngestPipeline

logger = logging.getLogger("compliance_agent.sync")


@dataclass
class ConnectorSyncResult:
    name: str
    ingested: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)
    ok: bool = True


@dataclass
class SyncReport:
    results: list[ConnectorSyncResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)


class IngestSyncer:
    """Re-runs each registered connector's fetch() and reconciles the result
    into graph_store/vector_store: upsert covers adds/edits, and an explicit
    delete covers ids that dropped out of a source since the last sync.

    state_path, if given, persists the last-seen id set per connector to
    disk (JSON) so restarts don't lose deletion tracking -- otherwise a file
    removed from disk while the process was down would never be detected as
    removed, since the in-memory baseline would restart empty and treat
    whatever's on disk now as "new", not "still there". This matters for the
    persistent backends (Chroma/Kuzu); in-memory backends lose everything on
    restart anyway so it's moot there. One known gap even with state_path:
    if a document was deleted from its source *before* state was ever
    persisted for it (e.g. first run after switching this on), it can't be
    retroactively detected -- only reconciled against the store's actual
    contents, which this class doesn't introspect.
    """

    def __init__(
        self,
        pipeline: IngestPipeline,
        graph_store: GraphStore,
        vector_store: VectorStore,
        connectors: dict[str, SourceConnector],
        state_path: str | Path | None = None,
    ):
        self.pipeline = pipeline
        self.graph_store = graph_store
        self.vector_store = vector_store
        self.connectors = connectors
        self.state_path = Path(state_path) if state_path else None
        self._known_ids: dict[str, set[str]] = self._load_state()

    def _load_state(self) -> dict[str, set[str]]:
        if self.state_path is None or not self.state_path.exists():
            return {name: set() for name in self.connectors}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("동기화 상태 파일을 읽지 못했습니다: %s (빈 상태로 시작)", self.state_path)
            raw = {}
        return {name: set(raw.get(name, [])) for name in self.connectors}

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps({name: sorted(ids) for name, ids in self._known_ids.items()}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("동기화 상태 파일을 저장하지 못했습니다: %s", self.state_path)

    def sync_once(self) -> SyncReport:
        report = SyncReport()
        for name, connector in self.connectors.items():
            result = ConnectorSyncResult(name=name)
            try:
                documents = connector.fetch()
            except Exception as exc:  # noqa: BLE001 -- 크롤러/파서 실패는 해당 소스만 건너뛰고 계속
                logger.exception("소스 '%s' fetch 실패", name)
                result.ok = False
                result.errors.append(str(exc))
                report.results.append(result)
                continue

            current_ids = {doc.entity_id for doc in documents}
            removed_ids = self._known_ids.get(name, set()) - current_ids
            for entity_id in removed_ids:
                self.graph_store.delete_entity(entity_id)
                self.vector_store.delete(entity_id)
            result.removed = len(removed_ids)

            result.ingested = self.pipeline.ingest_documents(documents)

            connector_errors = getattr(connector, "errors", None)
            if connector_errors:
                result.errors.extend(f"{item}: {reason}" for item, reason in connector_errors)

            self._known_ids[name] = current_ids
            report.results.append(result)

        self._save_state()
        return report

    async def run_forever(self, interval_seconds: float) -> None:
        """Sync every interval_seconds until the enclosing task is cancelled."""
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                report = await asyncio.to_thread(self.sync_once)
            except Exception:  # noqa: BLE001 -- one bad cycle must not kill the loop
                logger.exception("주기적 재색인 중 예상치 못한 오류")
                continue
            for r in report.results:
                logger.info(
                    "[sync] %s: ingested=%d removed=%d errors=%d",
                    r.name, r.ingested, r.removed, len(r.errors),
                )
