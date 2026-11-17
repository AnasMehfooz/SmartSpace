"""Application context: builds and wires every service, handles startup recovery and shutdown."""

from __future__ import annotations

import asyncio
import os
import threading
import time

import psutil

from app.config import Config
from app.db import Database, migrate
from app.logging_setup import get_logger, setup_logging, teardown_logging
from app.services.catalog import Catalog
from app.services.chat import ChatService
from app.services.downloads import DownloadManager
from app.services.embedder import EmbedderManager
from app.services.events import EventBus
from app.services.files import FilesService
from app.services.hardware import HardwareService
from app.services.indexer import PRIORITY_CHANGED, IndexService
from app.services.llm_client import InteractiveGate, LLMClient
from app.services.llm_runner import LlamaServer
from app.services.model_manager import ModelManager
from app.services.purge import PurgeService
from app.services.search import SearchService
from app.services.settings import SettingsService
from app.services.summarizer import Summarizer
from app.services.vectorstore import VectorStore
from app.services.watcher import WatcherService
from app.services.workspace import WorkspaceService

log = get_logger("context")


class AppContext:
    def __init__(self, cfg: Config, loop: asyncio.AbstractEventLoop | None = None):
        self.cfg = cfg
        self.started_at = time.time()
        self.ready = False
        cfg.ensure_dirs()
        self.log_file = setup_logging(cfg.logs_dir, cfg.log_level, cfg.console_log)
        # No telemetry and no hub lookups: models are loaded from local folders only.
        for k, v in (("HF_HUB_OFFLINE", "1"), ("HF_HUB_DISABLE_TELEMETRY", "1"), ("DO_NOT_TRACK", "1"),
                     ("TRANSFORMERS_OFFLINE", "1")):
            os.environ.setdefault(k, v)

        self.bus = EventBus(loop)
        self.db = Database(cfg.db_path)
        migrate(self.db)
        self._recover_after_crash()
        self.settings = SettingsService(self.db)
        self.catalog = Catalog.load(cfg.catalog_path)
        self.hardware = HardwareService(self)
        if self.hardware.get(scan_if_missing=False):
            self.hardware._apply_tier_defaults(self.hardware.tier)
        self.vectors = VectorStore(cfg.vectors_dir)
        self.downloads = DownloadManager(self)
        self.models = ModelManager(self)
        self.embedders = EmbedderManager(self)
        self.gate = InteractiveGate()
        self.llm = LlamaServer(self)
        self.llm_client = LLMClient(self)
        self.workspace = WorkspaceService(self)
        self.indexer = IndexService(self)
        self.summarizer = Summarizer(self)
        self.watcher = WatcherService(self)
        self.search = SearchService(self)
        self.files = FilesService(self)
        self.chat = ChatService(self)
        self.purge = PurgeService(self)

    def hardware_cores(self) -> int:
        return psutil.cpu_count(logical=False) or psutil.cpu_count() or 2

    def _recover_after_crash(self) -> None:
        """SDD 6.5: any running job becomes queued, any processing file becomes pending."""
        with self.db.write() as conn:
            conn.execute("UPDATE jobs SET state = 'queued', started_at = NULL WHERE state = 'running'")
            conn.execute("UPDATE files SET status = 'pending' WHERE status = 'processing'")

    def start(self) -> None:
        self.downloads.start()
        self.llm.start_background()
        self.indexer.start()
        threading.Thread(target=self._background_init, name="startup", daemon=True).start()
        self.ready = True

    def _background_init(self) -> None:
        try:
            if self.hardware.get(scan_if_missing=False) is None:
                self.hardware.scan()
        except Exception:
            log.exception("hardware_scan_failed")
        try:
            self.embedders.warmup()
        except Exception:
            log.exception("embedder_warmup_failed")
        try:
            if self.workspace.root() is not None and self.workspace.was_scanned():
                self.watcher.start()
                self.indexer.start_full_scan(PRIORITY_CHANGED)  # catch up on changes while the app was closed
        except Exception:
            log.exception("startup_scan_failed")
        try:
            self.indexer.queue_missing_summaries()  # also regenerates summaries from older prompt versions
        except Exception:
            log.exception("summary_requeue_failed")
        try:
            if self.settings.get("llm_preload") and self.models.active("llm") is not None:
                self.llm.ensure_ready()
        except Exception:
            log.exception("llm_preload_failed")

    def shutdown(self) -> None:
        self.ready = False
        for name, fn in (("watcher", self.watcher.stop), ("indexer", self.indexer.stop),
                         ("downloads", self.downloads.stop), ("llm", self.llm.shutdown)):
            try:
                fn()
            except Exception:
                log.exception("shutdown_step_failed", step=name)
        try:
            self.db.checkpoint()
        except Exception:
            pass
        self.db.close()
        log.info("backend_stopped")
        teardown_logging()
