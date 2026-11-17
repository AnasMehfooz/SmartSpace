"""Settings table access with typed validation and defaults (SDD 5.3, 10.9)."""

from __future__ import annotations

import json
import threading
from typing import Annotated, Any, Callable, Literal

import psutil
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from app.db import Database, utcnow


def _default_threads() -> int:
    cores = psutil.cpu_count(logical=False) or psutil.cpu_count() or 2
    return max(1, cores // 2)


DEFAULTS: dict[str, Any] = {
    "language": "en",
    "quick_search_shortcut": "Alt+Space",
    "max_file_size_mb": 100,
    "max_pdf_pages": 500,
    "index_on_battery": False,
    "index_cpu_threads": _default_threads(),
    "summaries_enabled": True,  # Tier 0 overrides this to False unless the user set it
    "llm_idle_unload_min": 10,
    "llm_preload": False,
    "chunk_target_chars": 1600,
    "chunk_overlap_chars": 240,
    "search_top_k": 10,
    "rag_top_k": 6,
    "autostart": False,
}


class SettingsUpdate(BaseModel):
    """Partial update. Unknown keys and wrong types are rejected (422 VALIDATION_ERROR)."""

    model_config = ConfigDict(extra="forbid")

    language: Literal["en", "de"] | None = None
    quick_search_shortcut: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    max_file_size_mb: Annotated[StrictInt, Field(ge=1, le=4096)] | None = None
    max_pdf_pages: Annotated[StrictInt, Field(ge=1, le=10000)] | None = None
    index_on_battery: StrictBool | None = None
    index_cpu_threads: Annotated[StrictInt, Field(ge=1, le=64)] | None = None
    summaries_enabled: StrictBool | None = None
    llm_idle_unload_min: Annotated[StrictInt, Field(ge=0, le=1440)] | None = None
    llm_preload: StrictBool | None = None
    chunk_target_chars: Annotated[StrictInt, Field(ge=400, le=8000)] | None = None
    chunk_overlap_chars: Annotated[StrictInt, Field(ge=0, le=2000)] | None = None
    search_top_k: Annotated[StrictInt, Field(ge=1, le=100)] | None = None
    rag_top_k: Annotated[StrictInt, Field(ge=1, le=20)] | None = None
    autostart: StrictBool | None = None

    @model_validator(mode="after")
    def _overlap_smaller_than_target(self):
        if (
            self.chunk_target_chars is not None
            and self.chunk_overlap_chars is not None
            and self.chunk_overlap_chars >= self.chunk_target_chars
        ):
            raise ValueError("chunk_overlap_chars must be smaller than chunk_target_chars")
        return self


class SettingsService:
    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        self._stored: dict[str, Any] = {}
        self._dynamic_defaults: dict[str, Any] = {}
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        for row in db.query("SELECT key, value_json FROM settings"):
            try:
                self._stored[row["key"]] = json.loads(row["value_json"])
            except json.JSONDecodeError:
                continue

    def set_dynamic_default(self, key: str, value: Any) -> None:
        """Defaults that depend on hardware (e.g. summaries off on Tier 0)."""
        with self._lock:
            self._dynamic_defaults[key] = value

    def get(self, key: str) -> Any:
        with self._lock:
            if key in self._stored:
                return self._stored[key]
            if key in self._dynamic_defaults:
                return self._dynamic_defaults[key]
            return DEFAULTS[key]

    def all(self) -> dict[str, Any]:
        return {k: self.get(k) for k in DEFAULTS}

    def is_user_set(self, key: str) -> bool:
        with self._lock:
            return key in self._stored

    def update(self, patch: SettingsUpdate) -> dict[str, Any]:
        values = patch.model_dump(exclude_none=True)
        target = values.get("chunk_target_chars", self.get("chunk_target_chars"))
        overlap = values.get("chunk_overlap_chars", self.get("chunk_overlap_chars"))
        if overlap >= target:
            from app.errors import AppError

            raise AppError(
                "VALIDATION_ERROR", "chunk_overlap_chars must be smaller than chunk_target_chars"
            )
        now = utcnow()
        with self.db.write() as conn:
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
                    "updated_at = excluded.updated_at",
                    (key, json.dumps(value), now),
                )
        with self._lock:
            self._stored.update(values)
        for listener in list(self._listeners):
            try:
                listener(values)
            except Exception:
                pass
        return self.all()

    def on_change(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(fn)
