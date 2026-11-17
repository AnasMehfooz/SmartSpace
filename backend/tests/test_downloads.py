"""Model download manager: allowlist, Range resume, SHA-256 verification (SDD 9.4)."""

import copy
import hashlib

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.downloads import DownloadError
from tests.conftest import TEST_CATALOG, auth, make_config, wait_for

PAYLOAD = bytes(range(256)) * 4096  # 1 MiB


class FakeResponse:
    def __init__(self, data: bytes, range_header: str | None):
        start = int(range_header.split("=")[1].rstrip("-")) if range_header else 0
        self.status_code = 206 if start else 200
        self._data = data[start:]

    def iter_bytes(self, n):
        for i in range(0, len(self._data), 64 * 1024):
            yield self._data[i : i + 64 * 1024]

    def close(self):
        pass


@pytest.fixture
def ctx_client(tmp_path):
    cat = copy.deepcopy(TEST_CATALOG)
    dl = next(m for m in cat["models"] if m["id"] == "dl-test")
    dl["files"][0].update(size_bytes=len(PAYLOAD), sha256=hashlib.sha256(PAYLOAD).hexdigest())
    dl["size_bytes"] = len(PAYLOAD)
    with TestClient(create_app(make_config(tmp_path, catalog=cat))) as c:
        auth(c)
        yield c.app.state.ctx, c


def test_host_allowlist(ctx_client):
    ctx, _ = ctx_client
    dm = ctx.downloads
    dm._check_host("https://huggingface.co/Qwen/x/resolve/main/a.gguf")
    dm._check_host("https://us.aws.cdn.hf.co/xet-bridge-us/abc")
    for bad in ("http://huggingface.co/a", "https://evil.example/a", "https://huggingface.co.evil.example/a",
                "https://hf.co.evil.example/a"):
        with pytest.raises(DownloadError):
            dm._check_host(bad)


def test_download_resume_and_verify(ctx_client, monkeypatch):
    ctx, client = ctx_client
    seen_ranges = []

    def fake_open(_client, url, headers):
        ctx.downloads._check_host(url)
        seen_ranges.append(headers.get("Range"))
        return FakeResponse(PAYLOAD, headers.get("Range"))

    monkeypatch.setattr(ctx.downloads, "_open", fake_open)
    # simulate an interrupted earlier download: first 300 KB already in tmp/
    part = ctx.cfg.tmp_dir / "dl-test__weights.bin.part"
    part.write_bytes(PAYLOAD[:300_000])
    r = client.post("/api/v1/models/dl-test/download")
    assert r.status_code == 202 and r.json()["download_id"]
    assert wait_for(lambda: ctx.models.is_installed("dl-test"), timeout=20)
    assert seen_ranges == ["bytes=300000-"]
    dest = ctx.models.model_dir(ctx.catalog.get("dl-test")) / "weights.bin"
    assert dest.read_bytes() == PAYLOAD and not part.exists()
    # first embedding model downloaded -> auto-activated
    assert ctx.models.active_id("embedding") == "dl-test"


def test_checksum_mismatch_fails_and_drops_part(ctx_client, monkeypatch):
    ctx, client = ctx_client
    monkeypatch.setattr(ctx.downloads, "_open", lambda _c, url, headers: FakeResponse(b"x" * len(PAYLOAD), None))
    client.post("/api/v1/models/dl-test/download")
    assert wait_for(lambda: ctx.models.row("dl-test")["status"] == "failed", timeout=20)
    assert not list(ctx.cfg.tmp_dir.glob("dl-test__*.part"))
    assert not ctx.models.is_installed("dl-test")


def test_cancel_queued_download(ctx_client, monkeypatch):
    ctx, client = ctx_client
    import threading

    gate = threading.Event()

    def slow_open(_client, url, headers):
        gate.wait(5)
        return FakeResponse(PAYLOAD, None)

    monkeypatch.setattr(ctx.downloads, "_open", slow_open)
    client.post("/api/v1/models/dl-test/download")
    assert client.post("/api/v1/models/dl-test/cancel").json()["cancelled"] is True
    gate.set()
    assert wait_for(lambda: ctx.models.row("dl-test")["status"] == "not_installed", timeout=20)
