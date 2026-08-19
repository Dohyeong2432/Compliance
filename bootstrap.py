"""Assembles the knowledge/agent backends from environment variables.

Defaults to fully in-memory, dependency-light backends so the app can run
with zero configuration for local development and tests. Set
VECTOR_STORE_BACKEND=chroma / GRAPH_STORE_BACKEND=kuzu /
EMBEDDER_BACKEND=voyage|gemini to switch to the persistent/production
backends. LLM_BACKEND (anthropic|gemini) is read separately, in
api/main.py, since the LLM client is constructed per-process there rather
than as part of AppComponents.
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

try:
    from dotenv import load_dotenv

    load_dotenv()  # no-op if no .env file is present; never overrides already-set env vars
except ImportError:
    pass

from agent.audit import AuditLogger
from agent.sso import SSOConfig
from knowledge.embedder import Embedder, GeminiEmbedder, HashEmbedder, VoyageEmbedder
from knowledge.graph_store import GraphStore, KuzuGraphStore, NetworkXGraphStore
from knowledge.lexical import LexicalIndex
from knowledge.reranker import NoOpReranker, Reranker, VoyageReranker
from knowledge.retriever import HybridRetriever
from knowledge.vector_store import ChromaVectorStore, InMemoryVectorStore, VectorStore
from pipeline.connectors.base import SourceConnector
from pipeline.connectors.case import CaseConnector
from pipeline.connectors.faq import LocalFileFaqConnector
from pipeline.connectors.interpretation import InterpretationConnector
from pipeline.connectors.law import LawConnector
from pipeline.connectors.local_file import LocalFileRegulationConnector
from pipeline.connectors.precedent import LocalFilePrecedentConnector
from pipeline.connectors.review import LocalFileReviewConnector
from pipeline.ingest import IngestPipeline
from pipeline.sync import IngestSyncer

logger = logging.getLogger("compliance_agent.bootstrap")


@dataclass
class AppComponents:
    embedder: Embedder
    vector_store: VectorStore
    graph_store: GraphStore
    retriever: HybridRetriever
    audit_logger: AuditLogger
    sso_config: SSOConfig | None
    syncer: IngestSyncer
    sync_interval_seconds: float


def _build_embedder() -> Embedder:
    backend = os.environ.get("EMBEDDER_BACKEND", "hash").lower()
    if backend == "hash":
        return HashEmbedder()
    if backend == "voyage":
        return VoyageEmbedder(model=os.environ.get("VOYAGE_MODEL", "voyage-3"))
    if backend == "gemini":
        return GeminiEmbedder(
            model=os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-001"),
            dimension=int(os.environ.get("GEMINI_EMBED_DIMENSION", "768")),
            batch_size=int(os.environ.get("GEMINI_EMBED_BATCH_SIZE", "10")),
            # 무료 등급 분당 쿼터 초과(429) 시 재시도 대기 -- 유료 플랜으로
            # 넘어가면 쿼터가 커져 이 경로를 사실상 안 타게 되지만(429가 안
            # 나면 그냥 안 기다림), 재시도 횟수/대기 시간을 코드 수정 없이
            # 바꿀 수 있도록 env로 뺐다. 재시도 자체를 끄고 싶으면
            # GEMINI_EMBED_RATE_LIMIT_MAX_RETRIES=1로 설정.
            rate_limit_max_retries=int(os.environ.get("GEMINI_EMBED_RATE_LIMIT_MAX_RETRIES", "5")),
            rate_limit_backoff_seconds=float(os.environ.get("GEMINI_EMBED_RATE_LIMIT_BACKOFF_SECONDS", "60")),
        )
    raise RuntimeError(f"Unknown EMBEDDER_BACKEND: {backend}")


def _build_reranker() -> Reranker:
    """리랭커는 기본 비활성(none)이다 -- 검색 정확도 향상 폭은 크지만 질의마다
    외부 API 호출이 한 번 더 붙어 지연·비용이 늘기 때문에, 켜는 것은 배포
    환경의 선택으로 남긴다. 끄더라도 검색 경로 자체는 동일하게 동작한다."""
    backend = os.environ.get("RERANKER_BACKEND", "none").lower()
    if backend in ("none", ""):
        return NoOpReranker()
    if backend == "voyage":
        return VoyageReranker(model=os.environ.get("VOYAGE_RERANK_MODEL", "rerank-2.5"))
    raise RuntimeError(f"Unknown RERANKER_BACKEND: {backend}")


def _build_vector_store() -> VectorStore:
    backend = os.environ.get("VECTOR_STORE_BACKEND", "memory").lower()
    if backend == "memory":
        return InMemoryVectorStore()
    if backend == "chroma":
        persist_dir = os.environ.get("CHROMA_PERSIST_DIR", "./data/chroma")
        return ChromaVectorStore(persist_dir)
    raise RuntimeError(f"Unknown VECTOR_STORE_BACKEND: {backend}")


def _build_graph_store() -> GraphStore:
    backend = os.environ.get("GRAPH_STORE_BACKEND", "memory").lower()
    if backend == "memory":
        return NetworkXGraphStore()
    if backend == "kuzu":
        db_path = os.environ.get("KUZU_DB_PATH", "./data/graph.kuzu")
        return KuzuGraphStore(db_path)
    raise RuntimeError(f"Unknown GRAPH_STORE_BACKEND: {backend}")


def _import_callable(path: str) -> Callable[[], list[dict[str, Any]]]:
    """Resolves a "package.module:function" string to the function object.

    Used for LAW_CRAWLER / INTERPRETATION_CRAWLER / CASE_CRAWLER -- these
    point at a zero-arg function (written and owned by whoever operates this
    deployment) that does the actual crawling and returns list[dict]. See
    pipeline/connectors/crawler_base.py for the expected dict shape.
    """
    module_path, sep, attr = path.partition(":")
    if not sep or not attr:
        raise RuntimeError(f"Invalid crawler import path {path!r} -- expected 'package.module:function'")
    module = importlib.import_module(module_path)
    fn = getattr(module, attr)
    if not callable(fn):
        raise RuntimeError(f"{path!r} does not point at a callable")
    return fn


def _build_connectors() -> dict[str, SourceConnector]:
    """Registers every source this deployment currently knows how to read.

    REGULATION/REVIEW/FAQ/PRECEDENT are always registered as local-file
    connectors (pointed at a docs directory that may simply not exist yet --
    fetch() returns [] in that case rather than erroring). LAW/INTERPRETATION/
    CASE are crawler-backed and only registered once their *_CRAWLER env var
    points at an actual importable function -- there's deliberately no
    built-in scraping logic for law.go.kr / 금융위·금감원 질의회신 /
    금감원 제재정보공개 baked into this codebase (see each connector's
    module docstring for why), so until that env var is set, those three
    sources are simply absent from sync rather than raising on every cycle.

    PRECEDENT(계약검토 선례)는 다른 셋과 달리 "완료·승인된 사례만 이 폴더에
    올린다"는 사람의 큐레이션에 의존한다 -- 자동 검토 결과를 자동으로
    채워넣는 경로는 없다(agent/contract_review.py 참고).
    """
    connectors: dict[str, SourceConnector] = {
        "regulation": LocalFileRegulationConnector(os.environ.get("REGULATION_DOCS_DIR", "./data/raw/regulation")),
        "review": LocalFileReviewConnector(os.environ.get("REVIEW_DOCS_DIR", "./data/raw/review")),
        "faq": LocalFileFaqConnector(os.environ.get("FAQ_DOCS_DIR", "./data/raw/faq")),
        "precedent": LocalFilePrecedentConnector(os.environ.get("PRECEDENT_DOCS_DIR", "./data/raw/precedent")),
    }

    crawler_connectors = {
        "law": ("LAW_CRAWLER", LawConnector),
        "interpretation": ("INTERPRETATION_CRAWLER", InterpretationConnector),
        "case": ("CASE_CRAWLER", CaseConnector),
    }
    for name, (env_var, connector_cls) in crawler_connectors.items():
        import_path = os.environ.get(env_var)
        if not import_path:
            continue
        try:
            fetch_items = _import_callable(import_path)
        except (ImportError, AttributeError, RuntimeError):
            logger.exception("%s=%s 로딩 실패 -- %s 커넥터를 등록하지 않습니다.", env_var, import_path, name)
            continue
        connectors[name] = connector_cls(fetch_items=fetch_items)

    return connectors


def build_components() -> AppComponents:
    embedder = _build_embedder()
    vector_store = _build_vector_store()
    graph_store = _build_graph_store()
    # BM25 어휘 색인은 기본적으로 메모리 상주지만, LEXICAL_INDEX_PATH를 주면
    # sync 사이클 끝에 디스크로 저장되고 재시작 시 복원된다(knowledge/lexical.py
    # 참고) -- SYNC_ON_STARTUP=false로 시작 시 재색인을 건너뛸 때 이 색인만
    # 매번 비어버리는 걸 막기 위해 필요하다. 아래 IngestPipeline과
    # HybridRetriever가 같은 인스턴스를 공유해야 색인한 것을 검색할 수 있다.
    lexical_index = LexicalIndex(persist_path=os.environ.get("LEXICAL_INDEX_PATH", "./data/lexical_index.json"))
    retriever = HybridRetriever(
        embedder,
        vector_store,
        graph_store,
        lexical_index=lexical_index,
        reranker=_build_reranker(),
    )
    audit_logger = AuditLogger(os.environ.get("AUDIT_LOG_PATH", "./data/audit.jsonl"))
    sso_config = SSOConfig.from_env()

    pipeline = IngestPipeline(
        embedder,
        vector_store,
        graph_store,
        embed_cache_path=os.environ.get("EMBED_CACHE_PATH", "./data/embed_cache.json"),
        lexical_index=lexical_index,
    )
    syncer = IngestSyncer(
        pipeline=pipeline,
        graph_store=graph_store,
        vector_store=vector_store,
        connectors=_build_connectors(),
        state_path=os.environ.get("SYNC_STATE_PATH", "./data/sync_state.json"),
    )
    sync_interval_seconds = float(os.environ.get("SYNC_INTERVAL_SECONDS", "1800") or 0)

    return AppComponents(
        embedder=embedder,
        vector_store=vector_store,
        graph_store=graph_store,
        retriever=retriever,
        audit_logger=audit_logger,
        sso_config=sso_config,
        syncer=syncer,
        sync_interval_seconds=sync_interval_seconds,
    )
