"""LanceDB vector store: one table per embedding model, `chunks_<model_id>` (SDD 5.2)."""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import lancedb
import numpy as np
import pyarrow as pa

from app.logging_setup import get_logger

log = get_logger("vectors")

INDEX_THRESHOLD = 5000
NPROBES = 20


def _schema(dim: int) -> pa.Schema:
    return pa.schema(
        [
            pa.field("chunk_id", pa.int64()),
            pa.field("file_id", pa.int64()),
            pa.field("vector", pa.list_(pa.float32(), dim)),
        ]
    )


def _ids_sql(ids: list[int]) -> str:
    return ",".join(str(int(i)) for i in ids)


class VectorStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(self.path), read_consistency_interval=timedelta(0))
        self._tables: dict[str, object] = {}
        self._lock = threading.RLock()

    @staticmethod
    def table_name(model_id: str) -> str:
        return f"chunks_{model_id}"

    def _table_names(self) -> list[str]:
        res = self._db.list_tables()
        names = getattr(res, "tables", res)
        return list(names)

    def _open(self, model_id: str, dim: int | None = None, create: bool = False):
        name = self.table_name(model_id)
        with self._lock:
            t = self._tables.get(name)
            if t is not None:
                return t
            if name in self._table_names():
                t = self._db.open_table(name)
            elif create and dim:
                t = self._db.create_table(name, schema=_schema(dim), exist_ok=True)
            else:
                return None
            self._tables[name] = t
            return t

    def replace_files(
        self, model_id: str, dim: int, file_ids: list[int], chunk_ids: list[int], chunk_file_ids: list[int],
        vectors: np.ndarray,
    ) -> None:
        """Delete all vectors of `file_ids`, then add the new ones."""
        with self._lock:
            t = self._open(model_id, dim, create=True)
            if file_ids:
                t.delete(f"file_id IN ({_ids_sql(file_ids)})")
            if chunk_ids:
                data = pa.table(
                    {
                        "chunk_id": pa.array(chunk_ids, pa.int64()),
                        "file_id": pa.array(chunk_file_ids, pa.int64()),
                        "vector": pa.FixedSizeListArray.from_arrays(
                            pa.array(np.asarray(vectors, np.float32).reshape(-1), pa.float32()), dim
                        ),
                    },
                    schema=_schema(dim),
                )
                t.add(data)

    def add(self, model_id: str, dim: int, chunk_ids: list[int], file_ids: list[int], vectors: np.ndarray) -> None:
        self.replace_files(model_id, dim, [], chunk_ids, file_ids, vectors)

    def delete_chunks(self, model_id: str, chunk_ids: list[int]) -> None:
        if not chunk_ids:
            return
        with self._lock:
            t = self._open(model_id)
            if t is not None:
                t.delete(f"chunk_id IN ({_ids_sql(chunk_ids)})")

    def delete_files(self, file_ids: list[int], model_id: str | None = None) -> None:
        """Delete by file id from one model table, or from every table."""
        if not file_ids:
            return
        with self._lock:
            names = [self.table_name(model_id)] if model_id else self._table_names()
            for name in names:
                if not name.startswith("chunks_"):
                    continue
                t = self._open(name[len("chunks_"):])
                if t is not None:
                    t.delete(f"file_id IN ({_ids_sql(file_ids)})")

    def search(
        self, model_id: str, vector: np.ndarray, k: int = 40, file_ids: list[int] | None = None
    ) -> list[tuple[int, int, float]]:
        """Top-k by cosine distance -> [(chunk_id, file_id, distance)]."""
        t = self._open(model_id)
        if t is None:
            return []
        q = t.search(np.asarray(vector, np.float32), vector_column_name="vector").distance_type("cosine")
        if file_ids is not None:
            if not file_ids:
                return []
            q = q.where(f"file_id IN ({_ids_sql(file_ids)})", prefilter=True)
        q = q.limit(k).select(["chunk_id", "file_id", "_distance"])
        try:
            q = q.nprobes(NPROBES)
        except Exception:
            pass
        tbl = q.to_arrow()
        if tbl.num_rows == 0:
            return []
        cids = tbl.column("chunk_id").to_pylist()
        fids = tbl.column("file_id").to_pylist()
        dists = tbl.column("_distance").to_pylist()
        return list(zip(cids, fids, dists))

    def count(self, model_id: str | None = None) -> int:
        total = 0
        names = [self.table_name(model_id)] if model_id else self._table_names()
        for name in names:
            if not name.startswith("chunks_"):
                continue
            t = self._open(name[len("chunks_"):])
            if t is not None:
                total += t.count_rows()
        return total

    def optimize(self, model_id: str) -> None:
        """Compact fragments, drop old versions, build the ANN index once the table is large."""
        with self._lock:
            t = self._open(model_id)
            if t is None:
                return
            try:
                if t.count_rows() > INDEX_THRESHOLD and not t.list_indices():
                    t.create_index(metric="cosine", vector_column_name="vector", index_type="IVF_PQ")
                    log.info("vector_index_created", model_id=model_id)
                # keep a few minutes of old versions so concurrent searches never read a removed version
                t.optimize(cleanup_older_than=timedelta(minutes=5))
            except Exception:
                log.exception("vector_optimize_failed", model_id=model_id)

    def drop_model(self, model_id: str) -> None:
        name = self.table_name(model_id)
        with self._lock:
            self._tables.pop(name, None)
            if name in self._table_names():
                self._db.drop_table(name)

    def drop_all(self) -> int:
        with self._lock:
            names = [n for n in self._table_names() if n.startswith("chunks_")]
            for n in names:
                self._tables.pop(n, None)
                self._db.drop_table(n)
            return len(names)

    def size_bytes(self) -> int:
        if not self.path.exists():
            return 0
        return sum(p.stat().st_size for p in self.path.rglob("*") if p.is_file())
