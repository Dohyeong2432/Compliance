import io
import shutil
import subprocess
import time

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "test-secret-key-that-is-long-enough-1234"


def _make_token(dept: str = "RETAIL") -> str:
    return jwt.encode(
        {"sub": "u1", "dept": dept, "iat": int(time.time()), "exp": int(time.time()) + 3600},
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "memory")
    monkeypatch.setenv("GRAPH_STORE_BACKEND", "memory")
    monkeypatch.setenv("EMBEDDER_BACKEND", "hash")
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("SSO_JWT_ALGORITHM", raising=False)
    monkeypatch.delenv("SSO_JWT_SECRET", raising=False)
    # Point every source connector at an empty/nonexistent tmp dir and turn
    # off the background sync loop -- otherwise every TestClient startup
    # would shell out to pandoc/catdoc/pdftotext against the real
    # data/raw/* staging folders (slow, and not what these tests are for).
    monkeypatch.setenv("REGULATION_DOCS_DIR", str(tmp_path / "no-regulation"))
    monkeypatch.setenv("REVIEW_DOCS_DIR", str(tmp_path / "no-review"))
    monkeypatch.setenv("FAQ_DOCS_DIR", str(tmp_path / "no-faq"))
    monkeypatch.setenv("PRECEDENT_DOCS_DIR", str(tmp_path / "no-precedent"))
    monkeypatch.setenv("SYNC_STATE_PATH", str(tmp_path / "sync_state.json"))
    monkeypatch.setenv("SYNC_INTERVAL_SECONDS", "0")
    monkeypatch.delenv("LAW_CRAWLER", raising=False)
    monkeypatch.delenv("INTERPRETATION_CRAWLER", raising=False)
    monkeypatch.delenv("CASE_CRAWLER", raising=False)


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
    # Without an explicit charset, Windows PowerShell 5.1's Invoke-RestMethod
    # mis-decodes non-ASCII (e.g. Korean) response bytes -- see UTF8JSONResponse.
    assert response.headers["content-type"] == "application/json; charset=utf-8"


def test_health_endpoint(api_env):
    import api.main as main_module
    import importlib

    importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.fixture
def require_pandoc():
    if shutil.which("pandoc") is None:
        pytest.skip("pandoc not installed")


def test_startup_sync_ingests_regulation_docs_dir(api_env, monkeypatch, tmp_path, require_pandoc):
    reg_dir = tmp_path / "regulation"
    reg_dir.mkdir()
    md = tmp_path / "source.md"
    md.write_text("테스트 규정\n\n본문 내용", encoding="utf-8")
    subprocess.run(["pandoc", str(md), "-o", str(reg_dir / "1. 테스트 규정.docx")], check=True)
    monkeypatch.setenv("REGULATION_DOCS_DIR", str(reg_dir))
    monkeypatch.setenv("SSO_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("SSO_JWT_SECRET", SECRET)

    import api.main as main_module
    import importlib

    importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        # startup sync already ran by the time the context manager returns
        assert main_module.app.state.components.graph_store.has_entity("regulation:1")
        del client  # unused, just need the context open through startup


def test_startup_sync_ingests_precedent_docs_dir(api_env, monkeypatch, tmp_path, require_pandoc):
    prec_dir = tmp_path / "precedent"
    prec_dir.mkdir()
    md = tmp_path / "source.md"
    md.write_text("업무위탁계약 검토 사례\n\n본문 내용", encoding="utf-8")
    subprocess.run(["pandoc", str(md), "-o", str(prec_dir / "1. 사례.docx")], check=True)
    monkeypatch.setenv("PRECEDENT_DOCS_DIR", str(prec_dir))
    monkeypatch.setenv("SSO_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("SSO_JWT_SECRET", SECRET)

    import api.main as main_module
    import importlib

    importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        assert main_module.app.state.components.graph_store.has_entity("precedent:1")
        del client


def test_admin_resync_requires_auth(api_env):
    import api.main as main_module
    import importlib

    importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        response = client.post("/admin/resync")
    assert response.status_code == 501  # no SSO configured in this fixture by default


def test_admin_resync_rejects_missing_token_when_sso_configured(api_env, monkeypatch):
    monkeypatch.setenv("SSO_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("SSO_JWT_SECRET", SECRET)
    import api.main as main_module
    import importlib

    importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        response = client.post("/admin/resync")
    assert response.status_code == 401


def test_admin_resync_picks_up_file_added_after_startup(api_env, monkeypatch, tmp_path, require_pandoc):
    reg_dir = tmp_path / "regulation"
    reg_dir.mkdir()
    monkeypatch.setenv("REGULATION_DOCS_DIR", str(reg_dir))
    monkeypatch.setenv("SSO_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("SSO_JWT_SECRET", SECRET)

    import api.main as main_module
    import importlib

    importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        assert main_module.app.state.components.graph_store.has_entity("regulation:1") is False

        md = tmp_path / "source.md"
        md.write_text("나중에 올라온 규정\n\n본문 내용", encoding="utf-8")
        subprocess.run(["pandoc", str(md), "-o", str(reg_dir / "1. 나중에 올라온 규정.docx")], check=True)

        response = client.post("/admin/resync", headers={"Authorization": f"Bearer {_make_token()}"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    regulation_result = next(r for r in body["results"] if r["source"] == "regulation")
    assert regulation_result["ingested"] == 1
    assert main_module.app.state.components.graph_store.has_entity("regulation:1") is True


# ---------------------------------------------------------------------------
# /contract-review
# ---------------------------------------------------------------------------


def test_contract_review_fails_closed_without_sso_config(api_env):
    import api.main as main_module
    import importlib

    importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        response = client.post("/contract-review", files={"file": ("계약서.docx", b"dummy", "application/octet-stream")})
    assert response.status_code == 501


def test_contract_review_rejects_missing_authorization_header(api_env, monkeypatch):
    monkeypatch.setenv("SSO_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("SSO_JWT_SECRET", SECRET)
    import api.main as main_module
    import importlib

    importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        response = client.post("/contract-review", files={"file": ("계약서.docx", b"dummy", "application/octet-stream")})
    assert response.status_code == 401


def test_contract_review_rejects_unsupported_file_extension(api_env, monkeypatch):
    monkeypatch.setenv("SSO_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("SSO_JWT_SECRET", SECRET)
    import api.main as main_module
    import importlib

    importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        response = client.post(
            "/contract-review",
            files={"file": ("계약서.txt", b"dummy", "text/plain")},
            headers={"Authorization": f"Bearer {_make_token()}"},
        )
    assert response.status_code == 400


def test_contract_review_happy_path_returns_docx(api_env, monkeypatch, tmp_path, require_pandoc):
    monkeypatch.setenv("SSO_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("SSO_JWT_SECRET", SECRET)
    import api.main as main_module
    import importlib

    importlib.reload(main_module)

    from agent.llm_client import LLMResponse, ScriptedLLMClient

    md = tmp_path / "contract.md"
    md.write_text("제1조(목적) 이 계약은 위수탁 업무 범위를 정한다.", encoding="utf-8")
    contract_docx = tmp_path / "계약서.docx"
    subprocess.run(["pandoc", str(md), "-o", str(contract_docx)], check=True)

    with TestClient(main_module.app) as client:
        script = [LLMResponse(text="법규 위반 소지가 없습니다.", tool_call=None, raw=[{"type": "text", "text": "..."}])]
        main_module.app.state.llm_client = ScriptedLLMClient(script)

        with open(contract_docx, "rb") as f:
            response = client.post(
                "/contract-review",
                files={
                    "file": (
                        "계약서.docx",
                        f.read(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
                headers={"Authorization": f"Bearer {_make_token()}"},
            )

    assert response.status_code == 200
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert "filename*=UTF-8''" in response.headers["content-disposition"]

    from docx import Document

    document = Document(io.BytesIO(response.content))
    texts = [p.text for p in document.paragraphs]
    assert any("제1조" in t for t in texts)
    assert "법규 위반 소지가 없습니다." in texts
