"""API contract: auth, error format, validation, workspace rules, CORS, search-only chat."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import TOKEN, auth, parse_sse


@pytest.fixture
def client(config):
    with TestClient(create_app(config)) as c:
        yield c


def test_health_needs_no_auth(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["ready"] is True and body["db_ok"] is True
    assert body["llm_state"] == "stopped"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}])
def test_auth_required(client, headers):
    r = client.get("/api/v1/settings", headers=headers)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


def test_query_token_only_for_event_streams(client):
    assert client.get(f"/api/v1/settings?token={TOKEN}").status_code == 401


def test_error_shapes(client):
    auth(client)
    cases = [("/api/v1/files/999", "FILE_NOT_FOUND", 404), ("/api/v1/chat/sessions/999", "SESSION_NOT_FOUND", 404)]
    for url, code, status in cases:
        r = client.get(url)
        assert r.status_code == status and r.json()["error"]["code"] == code
    r = client.post("/api/v1/models/nope/download")
    assert r.status_code == 404 and r.json()["error"]["code"] == "MODEL_NOT_FOUND"
    r = client.get("/api/v1/workspace/stats")
    assert r.status_code == 409 and r.json()["error"]["code"] == "WORKSPACE_NOT_SET"
    r = client.post("/api/v1/search", json={"query": ""})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_settings_validation(client):
    auth(client)
    assert client.put("/api/v1/settings", json={"search_top_k": "ten"}).status_code == 422
    assert client.put("/api/v1/settings", json={"unknown_key": 1}).status_code == 422
    assert client.put("/api/v1/settings", json={"index_on_battery": 1}).status_code == 422
    r = client.put("/api/v1/settings", json={"chunk_target_chars": 800, "chunk_overlap_chars": 900})
    assert r.status_code == 422
    r = client.put("/api/v1/settings", json={"language": "de", "rag_top_k": 4})
    assert r.status_code == 200 and r.json()["language"] == "de" and r.json()["rag_top_k"] == 4
    assert client.get("/api/v1/settings").json()["rag_top_k"] == 4


def test_workspace_rules(client, tmp_path):
    auth(client)
    assert client.get("/api/v1/workspace").json() is None
    r = client.put("/api/v1/workspace", json={"root_path": str(tmp_path / "missing")})
    assert r.status_code == 400 and r.json()["error"]["code"] == "WORKSPACE_PATH_INVALID"
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert client.put("/api/v1/workspace", json={"root_path": str(a)}).status_code == 200
    r = client.put("/api/v1/workspace", json={"root_path": str(b)})
    assert r.status_code == 409 and r.json()["error"]["code"] == "WORKSPACE_EXISTS"
    r = client.put("/api/v1/workspace", json={"root_path": str(b), "replace": True})
    assert r.status_code == 200 and r.json()["root_path"] == str(b.resolve())
    # indexing needs an embedding model
    r = client.post("/api/v1/index/start")
    assert r.status_code == 409 and r.json()["error"]["code"] == "MODEL_NOT_INSTALLED"
    assert client.delete("/api/v1/workspace").status_code == 200
    assert client.get("/api/v1/workspace").json() is None


def test_purge_requires_confirm(client):
    auth(client)
    r = client.post("/api/v1/privacy/purge", json={"scope": "chats"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "CONFIRM_REQUIRED"
    r = client.post("/api/v1/privacy/purge", json={"scope": "chats", "confirm": True})
    assert r.status_code == 200
    assert client.get("/api/v1/privacy/audit").json()["items"][0]["scope"] == "chats"


def test_cors_allows_only_tauri_and_dev_origins(client):
    ok = client.options("/api/v1/settings", headers={"Origin": "http://localhost:5173",
                                                       "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    bad = client.options("/api/v1/settings", headers={"Origin": "https://evil.example",
                                                        "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in bad.headers


def test_catalog_and_recommendations(client):
    auth(client)
    models = client.get("/api/v1/models/catalog").json()
    ids = {m["id"]: m for m in models}
    assert ids["ocr-rapid"]["status"] == "installed" and ids["ocr-rapid"]["is_active"]
    rec = client.get("/api/v1/models/recommendations").json()
    assert 0 <= rec["tier"] <= 4 and rec["recommended"]["embedding"] == "emb-test"
    assert client.get("/api/v1/models/llm/status").json()["state"] == "stopped"
    r = client.post("/api/v1/models/emb-test/activate")
    assert r.status_code == 200 and r.json()["is_active"]
    r = client.delete("/api/v1/models/emb-test")
    assert r.status_code == 409 and r.json()["error"]["code"] == "MODEL_ACTIVE"


def test_chat_without_llm_is_search_only(client):
    auth(client)
    sid = client.post("/api/v1/chat/sessions", json={}).json()["id"]
    with client.stream("POST", f"/api/v1/chat/sessions/{sid}/messages", json={"content": "Where is my contract?"}) as r:
        assert r.status_code == 200
        events = parse_sse(r.iter_lines())
    names = [e for e, _ in events]
    assert names[0] == "meta" and names[-1] == "done" and "files" in names
    assert events[0][1]["search_only"] is True
    session = client.get(f"/api/v1/chat/sessions/{sid}").json()
    assert session["title"] == "Where is my contract?"
    assert [m["role"] for m in session["messages"]] == ["user", "assistant"]
    assert "language model" in session["messages"][1]["content"]
    r = client.post(f"/api/v1/chat/sessions/{sid}/messages", json={"content": "   "})
    assert r.status_code == 422


def test_openapi_lists_the_contract(client):
    paths = client.get("/openapi.json", headers={"Authorization": f"Bearer {TOKEN}"}).json()["paths"]
    for p in ["/api/v1/health", "/api/v1/search", "/api/v1/chat/sessions/{session_id}/messages", "/api/v1/files/tree",
              "/api/v1/index/events", "/api/v1/models/downloads/events", "/api/v1/privacy/purge", "/api/v1/settings",
              "/api/v1/workspace/exclusions", "/api/v1/models/{model_id}/activate", "/api/v1/system/hardware"]:
        assert p in paths, p


def test_hardware_reports_best_gpu(client):
    auth(client)
    hw = client.get("/api/v1/system/hardware").json()
    assert "best_gpu" in hw
    usable = [g for g in hw["gpus"] if g["usable"]]
    if usable:
        assert hw["best_gpu"]["vram_mb"] == max(g["vram_mb"] or 0 for g in usable)
    else:
        assert hw["best_gpu"] is None
