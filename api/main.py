"""FastAPI deployment layer.

The /chat endpoint is fail-closed on identity: if SSO isn't configured, or
the bearer token doesn't verify, the request never reaches the agent. dept
is derived exclusively from the verified token (see agent.sso) and handed
to ComplianceAgent, which is the only thing constructed per-request — the
knowledge backends are built once at startup and shared.

Startup runs an initial ingest sync by default (so the app doesn't come up
empty) and, if SYNC_INTERVAL_SECONDS > 0, spawns a background task that
re-syncs every source connector on that interval for the lifetime of the
process — see pipeline/sync.py for what "sync" means here (upsert
added/changed docs, delete ones that disappeared from their source).

SYNC_ON_STARTUP=false skips that initial sync entirely, for deployments where
crawling/embedding is deliberately moved out of the request-serving process
(see scripts/sync.py, run separately via cron/systemd timer) and the app
should just start serving from whatever is already in the persistent
backends. Only makes sense with VECTOR_STORE_BACKEND=chroma and
GRAPH_STORE_BACKEND=kuzu — the in-memory backends have nothing to serve from
if nothing populates them in this process. LexicalIndex(persist_path=...) in
bootstrap.py is what closes the remaining gap: without it, the BM25 channel
would still come up empty every restart even with SYNC_ON_STARTUP=false,
since it doesn't live in either persistent backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import jwt
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from agent.contract_docx import build_review_document
from agent.contract_review import CONTRACT_REVIEW_MAX_TOOL_ITERATIONS, review_contract
from agent.harness import MAX_TOOL_ITERATIONS, ComplianceAgent
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


SYNC_ON_STARTUP = os.environ.get("SYNC_ON_STARTUP", "true").lower() != "false"


@asynccontextmanager
async def lifespan(app: FastAPI):
    components = build_components()
    app.state.components = components
    app.state.llm_client = None

    if SYNC_ON_STARTUP:
        report = await asyncio.to_thread(components.syncer.sync_once)
        for r in report.results:
            logger.info(
                "[startup-sync] %s: ingested=%d removed=%d errors=%d",
                r.name, r.ingested, r.removed, len(r.errors),
            )
    else:
        logger.info(
            "[startup-sync] SYNC_ON_STARTUP=false -- 시작 시 재색인을 건너뜁니다. "
            "영속 백엔드(및 LEXICAL_INDEX_PATH)에 이미 있는 데이터로 서빙을 시작합니다."
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

# --- POC 전용 대화형 UI (기본 비활성) ---
# 로그인 화면 없이 바로 질문/답변을 주고받는 로컬 데모용 페이지. SSO의
# fail-closed 원칙(agent/sso.py)은 건드리지 않는다 -- /poc/token은 그 원칙을
# 우회하는 게 아니라, make_token.py와 동일한 방식으로 .env에 이미 설정된
# HS256 시크릿으로 정상적인 서명 토큰을 서버가 대신 발급해 줄 뿐이다. 이
# 엔드포인트가 살아있으면 인증 없이 누구나 POC_DEPT 권한 토큰을 받아갈 수
# 있으므로, 반드시 로컬 개발 환경에서만 켠다 -- 공유/운영 환경에서 켜두면
# 그 자체로 인증 우회 통로가 된다.
POC_UI_ENABLED = os.environ.get("POC_UI_ENABLED", "").lower() == "true"
POC_DEPT = os.environ.get("POC_DEPT", "compliance")
_POC_HTML_PATH = Path(__file__).resolve().parent.parent / "poc" / "chat.html"

# /chat의 도구 호출 반복 한도. 기본값은 agent.harness.MAX_TOOL_ITERATIONS(4) 그대로
# 두되, 환경변수로 올릴 수 있게 한다 -- /contract-review가 이미
# CONTRACT_REVIEW_MAX_TOOL_ITERATIONS(8)로 "여러 법령이 얽혀 한 번의 왕복으로
# 안 끝나는" 경우를 검증해 뒀으므로, /chat에서도 같은 성격의 다단계 조사가
# 필요하면 코드 수정 없이 CHAT_MAX_TOOL_ITERATIONS만 올리면 된다. 반복은
# 병렬이 아니라 순차 LLM 왕복이라 값을 올릴수록 최악의 경우 응답 지연과
# 누적 토큰 비용이 함께 늘어난다는 점을 감안해서 정할 것.
CHAT_MAX_TOOL_ITERATIONS = int(os.environ.get("CHAT_MAX_TOOL_ITERATIONS") or MAX_TOOL_ITERATIONS)


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


def _build_agent(
    session: SessionContext, components: AppComponents, max_tool_iterations: int = MAX_TOOL_ITERATIONS
) -> ComplianceAgent:
    """/chat과 /contract-review이 공유하는 요청별 ComplianceAgent 생성.

    max_tool_iterations는 /chat에서는 CHAT_MAX_TOOL_ITERATIONS(기본값은
    agent.harness.MAX_TOOL_ITERATIONS=4)를, /contract-review에서는
    CONTRACT_REVIEW_MAX_TOOL_ITERATIONS(8)를 넘긴다 -- 계약 조항은 여러
    법령이 얽혀 근거 조사가 한 번의 왕복으로 안 끝나는 경우가 있어 애초에
    더 높게 잡았고, /chat도 다단계 조사가 필요한 배포 환경이면 코드 수정
    없이 CHAT_MAX_TOOL_ITERATIONS로 올릴 수 있다 -- 다만 반복은 순차 LLM
    왕복이라, 값을 올릴수록 최악의 경우 응답 지연과 누적 토큰 비용이 함께
    커진다."""
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
        max_tool_iterations=max_tool_iterations,
    )


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, authorization: str | None = Header(default=None)) -> ChatResponse:
    components: AppComponents = app.state.components
    session = _authenticate(authorization, components)
    agent = _build_agent(session, components, max_tool_iterations=CHAT_MAX_TOOL_ITERATIONS)

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

    agent = _build_agent(session, components, max_tool_iterations=CONTRACT_REVIEW_MAX_TOOL_ITERATIONS)
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


if POC_UI_ENABLED:

    @app.get("/poc")
    def poc_ui() -> FileResponse:
        return FileResponse(_POC_HTML_PATH, media_type="text/html; charset=utf-8")

    @app.get("/poc/token")
    def poc_token() -> dict:
        """POC 페이지가 로드될 때마다 호출해 짧게 사는 토큰을 새로 받는다.
        POC_DEPT 하나로 고정 -- 부서 선택 UI 없이 바로 질문하는 게 이
        페이지의 목적이므로, 다른 부서로 테스트하려면 POC_DEPT를 바꿔
        서버를 재시작한다."""
        components: AppComponents = app.state.components
        sso_config = components.sso_config
        if sso_config is None or sso_config.algorithm != "HS256":
            raise HTTPException(
                status_code=501,
                detail=(
                    "POC 토큰 발급에는 로컬 개발용 HS256 SSO 설정이 필요합니다. "
                    ".env에 SSO_JWT_ALGORITHM=HS256, SSO_JWT_SECRET=<임의의 문자열>을 "
                    "설정하세요 (make_token.py와 동일한 요구사항)."
                ),
            )
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {"sub": "poc-user", "dept": POC_DEPT, "iat": now, "exp": now + timedelta(hours=4)},
            sso_config.hs256_secret,
            algorithm="HS256",
        )
        return {"token": token, "dept": POC_DEPT}
