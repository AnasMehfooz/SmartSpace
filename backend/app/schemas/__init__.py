"""Pydantic request/response models. They define the OpenAPI contract the frontend generates
TypeScript types from (`openapi-typescript http://127.0.0.1:8765/openapi.json`)."""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.services.settings import SettingsUpdate

T = TypeVar("T")

__all__ = ["SettingsUpdate"]


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorOut(BaseModel):
    error: ErrorDetail


# ---- system -------------------------------------------------------------------------------------
class HealthOut(BaseModel):
    status: str
    ready: bool
    version: str
    db_ok: bool
    llm_state: str


class SystemInfoOut(BaseModel):
    version: str
    data_dir: str
    db_size_bytes: int
    vectors_size_bytes: int
    models_size_bytes: int
    python: str
    platform: str
    logs_dir: str
    uptime_s: int


class GpuOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    vendor: str
    vram_mb: int | None = None
    vram_free_mb: int | None = None
    driver: str | None = None
    backend: str | None = None
    integrated: bool = False
    usable: bool = False


class BatteryOut(BaseModel):
    present: bool
    percent: int | None = None
    plugged: bool = True


class HardwareOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    os: str | None = None
    arch: str | None = None
    cpu_name: str | None = None
    cpu_cores_physical: int | None = None
    cpu_cores_logical: int | None = None
    cpu_avx2: bool = True
    cpu_avx512: bool = False
    ram_total_mb: int | None = None
    ram_available_mb: int | None = None
    gpus: list[GpuOut] = []
    disk_free_mb: int | None = None
    battery: BatteryOut | None = None
    tier: int
    scanned_at: str | None = None


# ---- models -------------------------------------------------------------------------------------
class DownloadProgress(BaseModel):
    model_config = ConfigDict(extra="allow")
    model_id: str
    state: Literal["downloading", "queued"]
    bytes_done: int | None = None
    bytes_total: int | None = None
    speed_bps: int | None = None
    eta_s: int | None = None
    position: int | None = None


class ModelOut(BaseModel):
    id: str
    role: Literal["embedding", "llm", "ocr"]
    display_name: str
    file_name: str | None = None
    quant: str | None = None
    size_bytes: int
    dim: int | None = None
    ctx_cap: int | None = None
    min_ram_gb: float | None = None
    min_vram_gb: float | None = None
    tiers: list[int]
    languages: list[str] = []
    license: str | None = None
    bundled: bool = False
    status: Literal["not_installed", "downloading", "installed", "failed"]
    is_active: bool
    downloaded_at: str | None = None
    recommended: bool
    fits_hardware: bool
    download: DownloadProgress | None = None


class Alternative(BaseModel):
    id: str
    role: str
    fits_hardware: bool
    note: str | None = None


class RecommendedSet(BaseModel):
    embedding: str | None = None
    llm: str | None = None
    ocr: str | None = None


class RecommendationsOut(BaseModel):
    tier: int
    ctx_cap: int
    recommended: RecommendedSet
    alternatives: list[Alternative]
    warnings: list[str]


class DownloadQueuedOut(BaseModel):
    download_id: str
    model_id: str


class ActivateIn(BaseModel):
    confirm_reindex: bool = False


class ActivateOut(BaseModel):
    id: str
    role: str
    is_active: bool
    reindex: bool


class LlmStatusOut(BaseModel):
    state: Literal["stopped", "starting", "ready", "error"]
    model_id: str | None = None
    ctx: int | None = None
    gpu_layers: int | None = None
    backend: str | None = None
    device: str | None = None
    vram_used_mb: int | None = None
    error: str | None = None
    external: bool = False


# ---- workspace ----------------------------------------------------------------------------------
class WorkspaceIn(BaseModel):
    root_path: str = Field(min_length=1, max_length=1024)
    replace: bool = False


class WorkspaceCounts(BaseModel):
    files: int
    indexed: int
    pending: int
    skipped: int
    errors: int


class WorkspaceOut(BaseModel):
    root_path: str
    status: str
    created_at: str
    last_scan_at: str | None = None
    exists: bool = True
    counts: WorkspaceCounts
    warnings: list[str] = []


class WorkspaceStatsOut(BaseModel):
    files_total: int
    by_status: dict[str, int]
    by_ext: dict[str, int]
    by_kind: dict[str, int]
    chunks: int
    summaries: int
    size_bytes: int


class ExclusionIn(BaseModel):
    pattern: str = Field(min_length=1, max_length=200)


class ExclusionOut(BaseModel):
    id: int
    pattern: str
    builtin: bool
    files_removed: int | None = None


class PurgeResultOut(BaseModel):
    scope: str | None = None
    items_deleted: int
    bytes_freed: int


# ---- files --------------------------------------------------------------------------------------
class TreeNode(BaseModel):
    name: str
    rel_path: str
    type: Literal["dir", "file"]
    file_id: int | None = None
    status: str | None = None
    skip_reason: str | None = None
    child_count: int | None = None
    file_count: int | None = None
    indexed_count: int | None = None


class FileListItem(BaseModel):
    id: int
    rel_path: str
    name: str
    ext: str
    kind: str
    size_bytes: int
    mtime: str | None
    status: str
    skip_reason: str | None = None
    error_message: str | None = None
    summary_state: str
    indexed_at: str | None = None
    doc_type: str | None = None


class SummaryOut(BaseModel):
    text: str
    doc_type: str | None = None
    model_id: str
    generated_at: str
    prompt_version: str | None = None


class TagOut(BaseModel):
    name: str
    source: Literal["ai", "user"]


class FileDetailOut(BaseModel):
    id: int
    rel_path: str
    abs_path: str
    name: str
    ext: str
    kind: str
    size_bytes: int
    mtime: str | None
    language: str | None = None
    page_count: int | None = None
    status: str
    skip_reason: str | None = None
    summary_state: str
    summary: SummaryOut | None = None
    tags: list[TagOut]
    chunk_count: int
    error_message: str | None = None
    indexed_at: str | None = None


class ChunkOut(BaseModel):
    id: int
    chunk_index: int
    source: str
    page_start: int | None = None
    page_end: int | None = None
    text: str
    char_count: int
    embedding_model: str


class TagsIn(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=50)


class AcceptedOut(BaseModel):
    accepted: bool = True
    detail: str | None = None


# ---- index --------------------------------------------------------------------------------------
class IndexStatusOut(BaseModel):
    state: Literal["idle", "scanning", "indexing", "paused"]
    total: int
    indexed: int
    pending: int
    processing: int
    skipped: int
    errors: int
    summaries_done: int
    summaries_pending: int
    summaries_failed: int = 0
    eta_s: int | None = None
    paused_reason: str | None = None
    embedding_model: str | None = None


class IndexErrorItem(BaseModel):
    id: int
    rel_path: str
    name: str
    status: str
    skip_reason: str | None = None
    error_message: str | None = None
    updated_at: str


# ---- search -------------------------------------------------------------------------------------
class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ext: list[str] | None = None
    kind: Literal["document", "image", "other"] | None = None
    folder: str | None = None
    tags: list[str] | None = None
    doc_type: str | None = None
    date_from: str | None = Field(default=None, description="ISO date (YYYY-MM-DD) or datetime, on file mtime")
    date_to: str | None = None


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    mode: Literal["full", "quick"] = "full"
    filters: SearchFilters | None = None
    limit: int | None = Field(default=None, ge=1, le=100)


class Snippet(BaseModel):
    chunk_id: int
    page: int | None = None
    source: str | None = None
    text: str = Field(description="HTML-escaped text; matched terms wrapped in <mark>")


class SearchResult(BaseModel):
    file_id: int
    rel_path: str
    name: str
    ext: str
    kind: str
    score: float
    snippet: Snippet | None = None
    size_bytes: int | None = None
    mtime: str | None = None
    status: str | None = None
    summary: str | None = None
    doc_type: str | None = None
    tags: list[str] | None = None


class SearchOut(BaseModel):
    query: str
    mode: str
    took_ms: int
    results: list[SearchResult]


# ---- chat ---------------------------------------------------------------------------------------
class ChatSessionIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ChatRenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ChatMessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    filters: SearchFilters | None = None


class ChatSessionOut(BaseModel):
    id: int
    title: str
    created_at: str
    updated_at: str
    message_count: int = 0


class Citation(BaseModel):
    n: int
    file_id: int
    chunk_id: int
    page: int | None = None
    name: str
    rel_path: str | None = None
    score: float | None = None


class ChatMessageOut(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    content: str
    citations: list[Citation] = []
    model_id: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    created_at: str


class ChatSessionDetailOut(BaseModel):
    id: int
    title: str
    created_at: str
    updated_at: str
    messages: list[ChatMessageOut]
    generating: bool = False


class StopOut(BaseModel):
    stopped: bool


# ---- privacy ------------------------------------------------------------------------------------
class PrivacyOverviewOut(BaseModel):
    workspace_root: str | None
    files_total: int
    files_indexed: int
    chunks: int
    vectors: int
    summaries: int
    tags: int
    chat_sessions: int
    chat_messages: int
    db_size_bytes: int
    vectors_size_bytes: int
    models_size_bytes: int
    logs_size_bytes: int
    data_dir: str
    last_purge_at: str | None = None


class PurgeIn(BaseModel):
    scope: Literal["index", "file", "chats", "models", "everything"]
    file_ids: list[int] = Field(default_factory=list)
    confirm: bool = False


class AuditItem(BaseModel):
    id: int
    scope: str
    target: str | None = None
    items_deleted: int
    bytes_freed: int | None = None
    performed_at: str
