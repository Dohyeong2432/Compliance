import time

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "test-secret-key-that-is-long-enough-1234"


@pytest.fixture
def api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "memory")
    monkeypatch.setenv("GRAPH_STORE_BACKEND", "memory")
    monkeypatch.setenv("EMBEDDER_BACKEND", "hash")
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("SSO_JWT_ALGORITHM", raising=False)
    monkeypatch.delenv("SSO_JWT_SECRET", raising=False)


def test_chat_fails_closed_without_sso_config(api_env):
    import api.main as main_module
    import importlib

    importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        response = client.post("/chat", json={"message": "hi"})
    assert response.status_code == 501


def test_chat_rejects_missing_authorization_header(api_env, monkeypatch):
    monkeypatch.setenv("SSO_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("SSO_JWT_SECRET", SECRET)
    import api.main as main_module
    import importlib

    importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        response = client.post("/chat", json={"message": "hi"})
    assert response.status_code == 401


def test_chat_rejects_invalid_token(api_env, monkeypatch):
    monkeypatch.setenv("SSO_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("SSO_JWT_SECRET", SECRET)
    import api.main as main_module
    import importlib

    importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        response = client.post("/chat", json={"message": "hi"}, headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_chat_happy_path_verifies_citation(api_env, monkeypatch):
    monkeypatch.setenv("SSO_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("SSO_JWT_SECRET", SECRET)
    import api.main as main_module
    import importlib

    importlib.reload(main_module)

    from agent.llm_client import LLMResponse, ScriptedLLMClient, ToolCall
    from knowledge.vector_store import VectorRecord
    from ontology.schema import Entity, EntityType

    with TestClient(main_module.app) as client:
        components = main_module.app.state.components
        entity = Entity("law:1", EntityType.LAW, "테스트 법령", "테스트 본문 내용")
        components.graph_store.add_entity(entity)
        components.vector_store.upsert(
            [VectorRecord("law:1", components.embedder.embed_one("테스트 법령 테스트 본문 내용"), "테스트 본문 내용")]
        )

        script = [
            LLMResponse(
                text=None,
                tool_call=ToolCall(id="tc1", name="search_knowledge", arguments={"query": "테스트"}),
                raw=[{"type": "tool_use", "id": "tc1", "name": "search_knowledge", "input": {"query": "테스트"}}],
            ),
            LLMResponse(text="테스트 결과입니다 [[CITE:law:1]]", tool_call=None, raw=[{"type": "text", "text": "..."}]),
        ]
        main_module.app.state.llm_client = ScriptedLLMClient(script)

        token = jwt.encode(
            {"sub": "u1", "dept": "RETAIL", "iat": int(time.time()), "exp": int(time.time()) + 3600},
            SECRET,
            algorithm="HS256",
        )
        response = client.post(
            "/chat", json={"message": "테스트 질문"}, headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 200
    body = response.json()
    assert "law:1" in body["verified_citations"]
    assert "[1]" in body["answer"]


def test_health_endpoint(api_env):
    import api.main as main_module
    import importlib

    importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
