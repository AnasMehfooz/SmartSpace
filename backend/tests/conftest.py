from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Callable

import pytest

from app.config import Config, app_dir

TOKEN = "test-token"

TEST_CATALOG = {
    "version": 1,
    "tier_ctx_cap": {"0": 4096, "1": 4096, "2": 6144, "3": 8192, "4": 8192},
    "tier_defaults": {str(t): {"embedding": "emb-test", "llm": "llm-test"} for t in range(5)},
    "models": [
        {"id": "emb-test", "role": "embedding", "display_name": "Hash embedder (test)", "engine": "hash",
         "bundled": True, "dim": 384, "size_bytes": 1, "tiers": [0, 1, 2, 3, 4], "files": []},
        {"id": "emb-test2", "role": "embedding", "display_name": "Hash embedder 2 (test)", "engine": "hash",
         "bundled": True, "dim": 256, "size_bytes": 1, "tiers": [0, 1, 2, 3, 4], "files": []},
        {"id": "ocr-rapid", "role": "ocr", "display_name": "RapidOCR", "engine": "rapidocr", "bundled": True,
         "size_bytes": 1, "tiers": [0, 1, 2, 3, 4], "files": []},
        {"id": "llm-test", "role": "llm", "display_name": "Stub LLM (test)", "bundled": True, "size_bytes": 1,
         "n_layers": 4, "n_kv_heads": 2, "head_dim": 64, "max_ctx": 8192, "ctx_cap": 4096, "tiers": [0, 1, 2, 3, 4],
         "files": []},
        {"id": "dl-test", "role": "embedding", "display_name": "Download test", "engine": "hash", "dim": 64,
         "size_bytes": 0, "tiers": [0, 1, 2, 3, 4],
         "files": [{"path": "weights.bin", "size_bytes": 0, "sha256": "0" * 64,
                    "url": "https://huggingface.co/test/resolve/main/weights.bin"}]},
    ],
}


def make_config(base: Path, llm_url: str | None = None, catalog: dict | None = None) -> Config:
    res = base / "resources"
    res.mkdir(parents=True, exist_ok=True)
    (res / "models_catalog.json").write_text(json.dumps(catalog or TEST_CATALOG), encoding="utf-8")
    shutil.copy(app_dir() / "download_allowlist.json", res / "download_allowlist.json")
    return Config(port=0, token=TOKEN, data_dir=base / "data", llama_port=0, resource_dir=res, log_level="INFO",
                  dev=False, llm_external_url=llm_url, console_log=False)


def wait_for(fn: Callable[[], bool], timeout: float = 120, interval: float = 0.25) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False


def auth(client) -> None:
    client.headers["Authorization"] = f"Bearer {TOKEN}"


def parse_sse(lines) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    ev = None
    for line in lines:
        line = line.rstrip("\r\n") if isinstance(line, str) else line.decode().rstrip("\r\n")
        if line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:") and ev:
            events.append((ev, json.loads(line[5:].strip())))
    return events


@pytest.fixture
def config(tmp_path) -> Config:
    return make_config(tmp_path)
