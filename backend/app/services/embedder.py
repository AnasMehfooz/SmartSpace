"""Embeddings via fastembed (ONNX Runtime), loaded from the local model folder only (SDD 2.4, 9.4)."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import numpy as np
import xxhash

from app.logging_setup import get_logger
from app.services.catalog import CatalogModel

if TYPE_CHECKING:
    from app.context import AppContext

log = get_logger("embedder")

BATCH_SIZE = 32


def l2_normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    if v.ndim == 1:
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else v
    n = np.linalg.norm(v, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return v / n


class Embedder(Protocol):
    model_id: str
    dim: int

    def embed_passages(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


class FastEmbedder:
    def __init__(self, m: CatalogModel, model_dir: Path, threads: int):
        from fastembed import TextEmbedding

        self.model_id = m.id
        self.dim = int(m.dim or 0)
        self.query_prefix = m.get("query_prefix", "") or ""
        self.passage_prefix = m.get("passage_prefix", "") or ""
        self._lock = threading.Lock()
        self._model = TextEmbedding(
            model_name=m.get("fastembed_name"),
            specific_model_path=str(model_dir),
            cache_dir=str(model_dir),
            threads=threads,
            local_files_only=True,
        )

    def _embed(self, texts: list[str]) -> np.ndarray:
        with self._lock:
            vecs = list(self._model.embed(texts, batch_size=BATCH_SIZE))
        return l2_normalize(np.vstack(vecs)) if vecs else np.zeros((0, self.dim), np.float32)

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        return self._embed([self.passage_prefix + t for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([self.query_prefix + text])[0]


class HashEmbedder:
    """Deterministic bag-of-words + char-trigram hashing embedder. No model files; used by tests
    (SDD 15.1 "tiny test embedding model") and as a dev stand-in."""

    _word = re.compile(r"\w+", re.UNICODE)

    def __init__(self, model_id: str = "emb-test", dim: int = 384):
        self.model_id = model_id
        self.dim = dim

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for w in self._word.findall(text.lower()):
            h = xxhash.xxh64_intdigest(w.encode("utf-8"))
            v[h % self.dim] += 1.0
            padded = f"#{w}#"
            for i in range(len(padded) - 2):
                h3 = xxhash.xxh64_intdigest(padded[i : i + 3].encode("utf-8"))
                v[h3 % self.dim] += 0.25
        return v

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), np.float32)
        return l2_normalize(np.vstack([self._vec(t) for t in texts]))

    def embed_query(self, text: str) -> np.ndarray:
        return l2_normalize(self._vec(text))


class EmbedderManager:
    """Holds the embedder for the active embedding model; loads lazily and reloads on change."""

    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        self._lock = threading.Lock()
        self._embedder: Embedder | None = None
        self._loaded_id: str | None = None
        self.last_error: str | None = None

    def active_model_id(self) -> str | None:
        return self.ctx.models.active_id("embedding")

    def get(self) -> Embedder | None:
        m = self.ctx.models.active("embedding")
        if m is None:
            return None
        with self._lock:
            if self._embedder is not None and self._loaded_id == m.id:
                return self._embedder
            try:
                self._embedder = self._load(m)
                self._loaded_id = m.id
                self.last_error = None
                log.info("embedder_loaded", model_id=m.id)
            except Exception as e:
                self._embedder, self._loaded_id = None, None
                self.last_error = f"{e.__class__.__name__}: {e}"[:300]
                log.exception("embedder_load_failed", model_id=m.id)
                return None
            return self._embedder

    def _load(self, m: CatalogModel) -> Embedder:
        engine = m.get("engine", "fastembed")
        if engine == "hash":
            return HashEmbedder(m.id, int(m.dim or 384))
        threads = max(1, (self.ctx.hardware_cores() or 2) // 2)
        return FastEmbedder(m, self.ctx.models.model_dir(m), threads)

    def reload(self) -> None:
        with self._lock:
            self._embedder, self._loaded_id = None, None

    def warmup(self) -> None:
        emb = self.get()
        if emb is not None:
            emb.embed_query("warmup")
