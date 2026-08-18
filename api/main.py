"""FastAPI deployment layer.

The /chat endpoint is fail-closed on identity: if SSO isn't configured, or
the bearer token doesn't verify, the request never reaches the agent. dept
is derived exclusively from the verified token (see agent.sso) and handed
to ComplianceAgent, which is the only thing constructed per-request — the
knowledge backends are built once at startup and shared.

Startup also runs an initial ingest sync (so the app doesn't come up empty)
and, if SYNC_INTERVAL_SECONDS > 0, spawns a background task that re-syncs
every source connector on that interval for the lifetime of the process —
see pipeline/sync.py for what "sync" means here (upsert added/changed docs,
delete ones that disappeared from their source).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from agent.contract_docx import build_review_document
from agent.contract_review import review_contract
from agent.harness import ComplianceAgent
from agent.llm_client import AnthropicLLMClient, GeminiLLMClient, LLMClient
from agent.sso import SessionContext, SSOAuthError, SSOConfigError, build_session_context
from bootstrap import AppComponents, build_components
from pipeline.connectors.local_file import _SUPPORTED_SUFFIXES, UnparsableDocumentError, _extract_text

logger = logging.getLogger("compliance_agent")


class UTF8JSONResponse(JSONResponse):
    """Starlette's default JSONResponse sends "Content-Type: application/json"
    with no charset param. RFC 8259 makes UTF-8 the mandatory default for JSON
    without one, but Windows PowerShell 5.1's Invoke-RestMethod (built on the
    legacy HttpWebRequest, not HttpClient) doesn't follow that default and
    guesses a single-byte encoding instead -- silently mangling every non-ASCII
    (e.g. Korean) character in the response before it even reaches the
    console, no client-side console/encoding fix can undo that. Declaring the
    charset explicitly fixes it for that client and is harmless for every
    other client that already assumed UTF-8.
    """

    media_type = "application/json; charset=utf-8"


@asynccontextmanager
async def lifespan(app: FastAPI):
    components = build_components()
    app.state.components = components
    app.state.llm_client = None

    report = await asyncio.to_thread(components.syncer.sync_once)
    for r in report.results:
        logger.info(
            "[startup-sync] %s: ingested=%d removed=%d errors=%d",
            r.name, r.ingested, r.removed, len(r.errors),
        )

    sync_task: asyncio.Task | None = None
    if components.sync_interval_seconds > 0:
        sync_task = asyncio.create_task(components.syncer.run_forever(components.sync_interval_seconds))

    yield

    if sync_task is not None:
        sync_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sync_task


app = FastAPI(title="Group AI Compliance Agent", lifespan=lifespan, default_response_class=UTF8JSONResponse)


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    answer: str
    verified_citations: list[str]
    rejected_citations: list[str]


def _build_llm_client() -> LLMClient:
    backend = os.environ.get("LLM_BACKEND", "anthropic").lower()
    if backend == "anthropic":
        return AnthropicLLMClient()
    if backend == "gemini":
        return GeminiLLMClient(
            model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
            # 채팅 응답 중 429(쿼터)/503(모델 과부하)를 만나면 잠깐 대기 후
            # 재시도 -- GEMINI_EMBED_RATE_LIMIT_*와 동일한 목적이나, 임베딩과
            # 채팅은 별도 쿼터/장애이므로 별도 env var로 뺀다.
            rate_limit_max_retries=int(os.environ.get("GEMINI_CHAT_RATE_LIMIT_MAX_RETRIES", "5")),
            rate_limit_backoff_seconds=float(os.environ.get("GEMINI_CHAT_RATE_LIMIT_BACKOFF_SECONDS", "60")),
        )
    raise RuntimeError(f"Unknown LLM_BACKEND: {backend}")


def _get_llm_client() -> LLMClient:
    if app.state.llm_client is None:
        app.state.llm_client = _build_llm_client()
    return app.state.llm_client


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    return authorization.split(" ", 1)[1].strip()


def _authenticate(authorization: str | None, components: AppComponents) -> SessionContext:
    """/chat, /admin/resync, /contract-review이 공유하는 fail-closed SSO 게이트."""
    if components.sso_config is None:
        raise HTTPException(status_code=501, detail="SSO is not configured on this server")

    token = _extract_bearer_token(authorization)
    try:
        return build_session_context(token, components.sso_config)
    except SSOAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except SSOConfigError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc


def _build_agent(session: SessionContext, components: AppComponents) -> ComplianceAgent:
    """/chat과 /contract-review이 공유하는 요청별 ComplianceAgent 생성."""
    try:
        llm_client = _get_llm_client()
    except Exception as exc:  # optional dependency / missing API key
        logger.exception("Failed to construct LLM client")
        raise HTTPException(status_code=500, detail="LLM backend unavailable") from exc

    return ComplianceAgent(
        llm_client=llm_client,
        retriever=components.retriever,
        graph_store=components.graph_store,
        session=session,
        audit_logger=components.audit_logger,
    )


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, authorization: str | None = Header(default=None)) -> ChatResponse:
    components: AppComponents = app.state.components
    session = _authenticate(authorization, components)
    agent = _build_agent(session, components)

    result = agent.ask(request.message)
    return ChatResponse(
        answer=result.answer,
        verified_citations=result.verified_citations,
        rejected_citations=result.rejected_citations,
    )


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@app.post("/contract-review")
async def contract_review(
    file: UploadFile = File(...), authorization: str | None = Header(default=None)
) -> Response:
    """계약서 초안(.docx/.doc/.pdf)을 업로드받아 조항 단위로 검토한 뒤 검토의견서
    .docx를 돌려준다. /chat과 달리 여러 차례(조항 수만큼)의 agent.ask() 호출이
    순차로 필요해 응답까지 시간이 걸릴 수 있다 -- 동기 처리이며 별도 job 큐는 없다."""
    components: AppComponents = app.state.components
    session = _authenticate(authorization, components)

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"지원하지 않는 파일 형식입니다: {suffix or '(없음)'} "
            f"(지원 형식: {', '.join(_SUPPORTED_SUFFIXES)})",
        )

    content = await file.read()
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(content)
        tmp.flush()
        try:
            text = _extract_text(Path(tmp.name))
        except UnparsableDocumentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    agent = _build_agent(session, components)
    clause_reviews = review_contract(text, agent)
    document = build_review_document(file.filename or "계약서", session, clause_reviews)

    buffer = io.BytesIO()
    document.save(buffer)

    download_name = f"검토의견서_{Path(file.filename or 'contract').stem}.docx"
    return Response(
        content=buffer.getvalue(),
        media_type=DOCX_MEDIA_TYPE,
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"contract_review.docx\"; "
                f"filename*=UTF-8''{quote(download_name)}"
            )
        },
    )


@app.post("/admin/resync")
def resync(authorization: str | None = Header(default=None)) -> dict:
    """Triggers an immediate ingest sync instead of waiting for the next
    SYNC_INTERVAL_SECONDS tick -- e.g. right after uploading a new sagyu file.
    Same fail-closed SSO gate as /chat (any authenticated session, no
    additional role check yet -- see AGENT/README notes on remaining work)."""
    components: AppComponents = app.state.components
    _authenticate(authorization, components)

    report = components.syncer.sync_once()
    return {
        "ok": report.ok,
        "results": [
            {"source": r.name, "ingested": r.ingested, "removed": r.removed, "errors": r.errors, "ok": r.ok}
            for r in report.results
        ],
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
