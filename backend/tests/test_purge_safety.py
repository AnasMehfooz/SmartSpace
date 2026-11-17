"""SDD 10.8 / 13: no purge scope may delete anything under the workspace root.

All file-removal primitives are wrapped; every scope runs on an indexed workspace; the test asserts
that no removal call received a path inside the root and that every source file is byte-identical.
"""

import hashlib
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.paths import is_within
from tests.conftest import auth, make_config, wait_for


def _snapshot(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def indexed(tmp_path):
    root = tmp_path / "workspace"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "contract.txt").write_text("Rental contract: rent 950 EUR per month.", encoding="utf-8")
    (root / "docs" / "notes.md").write_text("# Notes\nBuy milk and eggs.", encoding="utf-8")
    (root / "keep.txt").write_text("Never delete me.", encoding="utf-8")
    with TestClient(create_app(make_config(tmp_path / "app"))) as c:
        auth(c)
        c.post("/api/v1/models/emb-test/activate")
        assert c.put("/api/v1/workspace", json={"root_path": str(root)}).status_code == 200
        assert c.post("/api/v1/index/start").status_code == 202
        assert wait_for(lambda: c.get("/api/v1/index/status").json()["indexed"] == 3, timeout=60)
        sid = c.post("/api/v1/chat/sessions", json={}).json()["id"]
        with c.stream("POST", f"/api/v1/chat/sessions/{sid}/messages", json={"content": "rent?"}) as r:
            list(r.iter_lines())
        yield c, root


def test_purge_never_touches_workspace(indexed, monkeypatch):
    client, root = indexed
    before = _snapshot(root)
    calls: list[str] = []

    def spy(fn):
        def wrapper(path, *a, **kw):
            calls.append(os.fspath(path))
            return fn(path, *a, **kw)
        return wrapper

    monkeypatch.setattr(os, "remove", spy(os.remove))
    monkeypatch.setattr(os, "unlink", spy(os.unlink))
    monkeypatch.setattr(os, "rmdir", spy(os.rmdir))
    monkeypatch.setattr(shutil, "rmtree", spy(shutil.rmtree))
    orig_unlink = Path.unlink
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **kw: (calls.append(str(self)), orig_unlink(self, *a, **kw))[1])

    for body in ({"scope": "file", "file_ids": [1], "confirm": True}, {"scope": "chats", "confirm": True},
                 {"scope": "index", "confirm": True}, {"scope": "models", "confirm": True},
                 {"scope": "everything", "confirm": True}):
        r = client.post("/api/v1/privacy/purge", json=body)
        assert r.status_code == 200, r.text

    offending = [c for c in calls if is_within(c, root)]
    assert offending == []
    assert _snapshot(root) == before
    overview = client.get("/api/v1/privacy/overview").json()
    assert overview["chunks"] == 0 and overview["vectors"] == 0 and overview["chat_sessions"] == 0
    assert overview["workspace_root"] is None


def test_safe_delete_refuses_outside_data_dir(indexed):
    from app.services.purge import safe_rmtree, safe_unlink

    client, root = indexed
    ctx = client.app.state.ctx
    for target in (root / "keep.txt", root, ctx.cfg.data_dir, ctx.cfg.data_dir.parent):
        with pytest.raises(PermissionError):
            safe_unlink(ctx, target)
        with pytest.raises(PermissionError):
            safe_rmtree(ctx, target)
    assert (root / "keep.txt").exists()
