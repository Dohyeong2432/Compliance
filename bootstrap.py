"""Assembles the knowledge/agent backends from environment variables.

Defaults to fully in-memory, dependency-light backends so the app can run
with zero configuration for local development and tests. Set
VECTOR_STORE_BACKEND=chroma / GRAPH_STORE_BACKEND=kuzu / EMBEDDER_BACKEND=voyage
to switch to the persistent/production backends.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()  # no-op if no .env file is present; never overrides already-set env vars
except ImportError:
    pass

from agent.audit import AuditLogger
from agent.sso import SSOConfig
from knowledge.embedder import Embedder, HashEmbedder, VoyageEmbedder
from knowledge.graph_store import GraphStore, KuzuGraphStore, NetworkXGraphStore
from knowledge.retriever import HybridRetriever
from knowledge.vector_store import ChromaVectorStore, InMemoryVectorStore, VectorStore


@dataclass
class AppComponents:
    embedder: Embedder
    vector_store: VectorStore
    graph_store: GraphStore
    retriever: HybridRetriever
    audit_logger: AuditLogger
    sso_config: SSOConfig | None


def _build_embedder() -> Embedder:
    backend = os.environ.get("EMBEDDER_BACKEND", "hash").lower()
    if backend == "hash":
        return HashEmbedder()
    if backend == "voyage":
        return VoyageEmbedder(model=os.environ.get("VOYAGE_MODEL", "voyage-3"))
    raise RuntimeError(f"Unknown EMBEDDER_BACKEND: {backend}")


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


def build_components() -> AppComponents:
    embedder = _build_embedder()
    vector_store = _build_vector_store()
    graph_store = _build_graph_store()
    retriever = HybridRetriever(embedder, vector_store, graph_store)
    audit_logger = AuditLogger(os.environ.get("AUDIT_LOG_PATH", "./data/audit.jsonl"))
    sso_config = SSOConfig.from_env()
    return AppComponents(
        embedder=embedder,
        vector_store=vector_store,
        graph_store=graph_store,
        retriever=retriever,
        audit_logger=audit_logger,
        sso_config=sso_config,
    )
