"""FastAPI deployment layer.

The /chat endpoint is fail-closed on identity: if SSO isn't configured, or
the bearer token doesn't verify, the request never reaches the agent. dept
is derived exclusively from the verified token (see agent.sso) and handed
to ComplianceAgent, which is the only thing constructed per-request — the
knowledge backends are built once at startup and shared.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from agent.harness import ComplianceAgent
from agent.llm_client import AnthropicLLMClient, LLMClient
from agent.sso import SSOAuthError, SSOConfigError, build_session_context
from bootstrap import AppComponents, build_components

logger = logging.getLogger("compliance_agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.components = build_components()
    app.state.llm_client = None
    yield


app = FastAPI(title="Group AI Compliance Agent", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    answer: str
    verified_citations: list[str]
    rejected_citations: list[str]


def _get_llm_client() -> LLMClient:
    if app.state.llm_client is None:
        app.state.llm_client = AnthropicLLMClient()
    return app.state.llm_client


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    return authorization.split(" ", 1)[1].strip()


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, authorization: str | None = Header(default=None)) -> ChatResponse:
    components: AppComponents = app.state.components

    if components.sso_config is None:
        raise HTTPException(status_code=501, detail="SSO is not configured on this server")

    token = _extract_bearer_token(authorization)
    try:
        session = build_session_context(token, components.sso_config)
    except SSOAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except SSOConfigError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc

    try:
        llm_client = _get_llm_client()
    except Exception as exc:  # optional dependency / missing API key
        logger.exception("Failed to construct LLM client")
        raise HTTPException(status_code=500, detail="LLM backend unavailable") from exc

    agent = ComplianceAgent(
        llm_client=llm_client,
        retriever=components.retriever,
        graph_store=components.graph_store,
        session=session,
        audit_logger=components.audit_logger,
    )
    result = agent.ask(request.message)
    return ChatResponse(
        answer=result.answer,
        verified_citations=result.verified_citations,
        rejected_citations=result.rejected_citations,
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
