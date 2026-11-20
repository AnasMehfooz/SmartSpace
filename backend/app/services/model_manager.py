"""Model registry: catalog <-> models table, recommendations, activation, deletion (SDD 9, 10.2)."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.db import utcnow
from app.errors import AppError
from app.logging_setup import get_logger
from app.services.catalog import CatalogModel

if TYPE_CHECKING:
    from app.context import AppContext

log = get_logger("models")


class ModelManager:
    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        self.catalog = ctx.catalog
        self._lock = threading.RLock()
        self.sync()

    # ---- paths ----------------------------------------------------------------------------
    def model_dir(self, m: CatalogModel) -> Path:
        return self.ctx.cfg.models_dir / m.role / m.id

    def primary_file(self, m: CatalogModel) -> Path | None:
        name = m.file_name
        return self.model_dir(m) / name if name else None

    def files_present(self, m: CatalogModel) -> bool:
        if m.bundled:
            return True
        if not m.files:
            return False
        d = self.model_dir(m)
        for f in m.files:
            p = d / f.path
            if not p.exists() or p.stat().st_size != f.size_bytes:
                return False
        return True

    # ---- table sync -----------------------------------------------------------------------
    def sync(self) -> None:
        """Reconcile the models table with the catalog and the files on disk."""
        with self._lock, self.ctx.db.write() as conn:
            for m in self.catalog.models.values():
                on_disk = self.files_present(m)
                row = conn.execute("SELECT status, is_active FROM models WHERE id = ?", (m.id,)).fetchone()
                sha = m.files[0].sha256 if len(m.files) == 1 else None
                if row is None:
                    conn.execute(
                        "INSERT INTO models (id, role, display_name, file_name, quant, size_bytes, sha256, dim, "
                        "status, is_active, downloaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                        (
                            m.id, m.role, m.display_name, m.file_name, m.get("quant"), m.size_bytes, sha, m.dim,
                            "installed" if on_disk else "not_installed", utcnow() if on_disk and not m.bundled else None,
                        ),
                    )
                    continue
                status = row["status"]
                if on_disk:
                    status = "installed"
                elif status in ("installed", "downloading"):
                    status = "not_installed"
                conn.execute(
                    "UPDATE models SET role=?, display_name=?, file_name=?, quant=?, size_bytes=?, sha256=?, dim=?, "
                    "status=?, is_active = CASE WHEN ? = 'installed' THEN is_active ELSE 0 END WHERE id=?",
                    (m.role, m.display_name, m.file_name, m.get("quant"), m.size_bytes, sha, m.dim, status, status, m.id),
                )
            ids = list(self.catalog.models)
            if ids:
                conn.execute(f"DELETE FROM models WHERE id NOT IN ({','.join('?' * len(ids))})", ids)
            # OCR is bundled and always active.
            for m in self.catalog.by_role("ocr"):
                conn.execute("UPDATE models SET is_active = 1 WHERE id = ?", (m.id,))
                break
            # at most one active per role
            for role in ("embedding", "llm", "ocr"):
                rows = conn.execute(
                    "SELECT id FROM models WHERE role = ? AND is_active = 1 ORDER BY id", (role,)
                ).fetchall()
                for r in rows[1:]:
                    conn.execute("UPDATE models SET is_active = 0 WHERE id = ?", (r["id"],))

    # ---- queries --------------------------------------------------------------------------
    def row(self, model_id: str) -> dict[str, Any] | None:
        r = self.ctx.db.query_one("SELECT * FROM models WHERE id = ?", (model_id,))
        return dict(r) if r else None

    def active_id(self, role: str) -> str | None:
        return self.ctx.db.scalar(
            "SELECT id FROM models WHERE role = ? AND is_active = 1 AND status = 'installed'", (role,)
        )

    def active(self, role: str) -> CatalogModel | None:
        mid = self.active_id(role)
        return self.catalog.get(mid) if mid else None

    def is_installed(self, model_id: str) -> bool:
        return self.ctx.db.scalar("SELECT status FROM models WHERE id = ?", (model_id,)) == "installed"

    def get_or_404(self, model_id: str) -> CatalogModel:
        m = self.catalog.get(model_id)
        if m is None:
            raise AppError("MODEL_NOT_FOUND", f"Model {model_id} does not exist")
        return m

    def set_status(self, model_id: str, status: str) -> None:
        with self.ctx.db.write() as conn:
            if status == "installed":
                conn.execute(
                    "UPDATE models SET status = ?, downloaded_at = ? WHERE id = ?", (status, utcnow(), model_id)
                )
            else:
                conn.execute(
                    "UPDATE models SET status = ?, is_active = CASE WHEN ? = 'installed' THEN is_active ELSE 0 END "
                    "WHERE id = ?",
                    (status, status, model_id),
                )

    # ---- recommendations ------------------------------------------------------------------
    def fits_hardware(self, m: CatalogModel, tier: int) -> bool:
        return tier >= m.min_tier

    def recommendations(self) -> dict[str, Any]:
        hw = self.ctx.hardware.get() or {}
        tier = int(hw.get("tier", 1))
        rec = self.catalog.defaults_for_tier(tier)
        statuses = {r["id"]: r["status"] for r in self.ctx.db.query("SELECT id, status FROM models")}
        alternatives = []
        # Every other embedding and language model is offered, also ones above this PC's tier
        # (fits_hardware false + a note): the user can always choose, the recommendation is only a default.
        for m in self.catalog.models.values():
            if m.role == "ocr" or rec.get(m.role) == m.id:
                continue
            fits = self.fits_hardware(m, tier)
            note = None
            if m.role == "llm" and tier == 2 and m.id == "llm-7b":
                note = "slower (partial GPU offload)"
                fits = True
            elif not fits:
                note = "may be slow or crash"
            elif m.role == "embedding" and tier >= 3 and m.id != rec.get("embedding"):
                note = "alternative"
            status = statuses.get(m.id, "not_installed")
            local_copy = self.ctx.local_models.location(m) if status != "installed" else None
            if status == "installed":
                note = "already installed" if not note else f"already installed; {note}"
            elif local_copy:
                note = "found on this PC, no download needed" if not note else f"found on this PC; {note}"
            alternatives.append({"id": m.id, "role": m.role, "display_name": m.display_name,
                                 "size_bytes": m.size_bytes, "fits_hardware": fits, "note": note,
                                 "status": status, "local_copy": local_copy})
        alternatives.sort(key=lambda a: (a["role"] != "embedding", a["status"] != "installed",
                                         a["local_copy"] is None, not a["fits_hardware"], a["size_bytes"]))

        warnings: list[str] = []
        disk_free_mb = hw.get("disk_free_mb")
        need_bytes = 0
        for role in ("embedding", "llm"):
            mid = rec.get(role)
            m = self.catalog.get(mid) if mid else None
            if m and not self.is_installed(m.id):
                need_bytes += m.size_bytes
        if disk_free_mb is not None and need_bytes and disk_free_mb * 2**20 < need_bytes * 1.5:
            warnings.append(
                f"Low disk space: the recommended models need about {need_bytes * 1.5 / 2**30:.1f} GB free."
            )
        battery = hw.get("battery") or {}
        if battery.get("present") and not battery.get("plugged") and not self.ctx.settings.get("index_on_battery"):
            warnings.append("On battery: indexing is paused.")
        if not hw.get("cpu_avx2", True):
            warnings.append("This CPU has no AVX2: AI features will be slow.")
        avail = hw.get("ram_available_mb")
        if avail is not None and avail < 2048:
            warnings.append(f"Only {avail / 1024:.1f} GB of RAM is free: close other apps for faster indexing and chat.")
        if tier == 0:
            warnings.append("Summaries are off by default on this hardware; search still works.")
        return {
            "tier": tier,
            "ctx_cap": self.catalog.tier_ctx_cap.get(tier, 4096),
            "recommended": {"embedding": rec.get("embedding"), "llm": rec.get("llm"), "ocr": rec.get("ocr")},
            "alternatives": alternatives,
            "warnings": warnings,
        }

    def catalog_view(self) -> list[dict[str, Any]]:
        tier = self.ctx.hardware.tier
        rec = self.catalog.defaults_for_tier(tier)
        rows = {r["id"]: dict(r) for r in self.ctx.db.query("SELECT * FROM models")}
        progress = self.ctx.downloads.progress_snapshot()
        out = []
        for m in self.catalog.models.values():
            r = rows.get(m.id, {})
            out.append(
                {
                    "id": m.id,
                    "role": m.role,
                    "display_name": m.display_name,
                    "file_name": m.file_name,
                    "quant": m.get("quant"),
                    "size_bytes": m.size_bytes,
                    "dim": m.dim,
                    "ctx_cap": m.get("ctx_cap"),
                    "min_ram_gb": m.get("min_ram_gb"),
                    "min_vram_gb": m.get("min_vram_gb"),
                    "tiers": m.tiers,
                    "languages": m.get("languages", []),
                    "license": m.get("license"),
                    "bundled": m.bundled,
                    "status": r.get("status", "not_installed"),
                    "is_active": bool(r.get("is_active")),
                    "downloaded_at": r.get("downloaded_at"),
                    "recommended": rec.get(m.role) == m.id,
                    "fits_hardware": self.fits_hardware(m, tier),
                    "download": progress.get(m.id),
                    "local_copy": (self.ctx.local_models.location(m)
                                   if r.get("status", "not_installed") != "installed" and not m.bundled else None),
                }
            )
        return out

    # ---- activation / deletion ------------------------------------------------------------
    def activate(self, model_id: str, confirm_reindex: bool = False) -> dict[str, Any]:
        m = self.get_or_404(model_id)
        if not self.is_installed(model_id):
            raise AppError("MODEL_NOT_INSTALLED", f"Model {model_id} is not installed")
        current = self.active_id(m.role)
        if current == model_id:
            return {"id": model_id, "role": m.role, "is_active": True, "reindex": False}

        reindex = False
        if m.role == "embedding":
            has_index = bool(self.ctx.db.scalar("SELECT 1 FROM chunks LIMIT 1"))
            if has_index:
                if not confirm_reindex:
                    raise AppError(
                        "REINDEX_REQUIRED",
                        "Changing the embedding model requires a full re-index",
                        details={"current": current, "requested": model_id},
                    )
                if self.ctx.indexer.is_busy():
                    raise AppError("INDEX_BUSY", "Pause or finish indexing before switching the embedding model")
                reindex = True

        with self._lock, self.ctx.db.write() as conn:
            conn.execute("UPDATE models SET is_active = 0 WHERE role = ?", (m.role,))
            conn.execute("UPDATE models SET is_active = 1 WHERE id = ?", (model_id,))
        log.info("model_activated", model_id=model_id, role=m.role)

        if m.role == "embedding":
            self.ctx.embedders.reload()
            if reindex:
                old = current
                self.ctx.indexer.reindex_all(reason="embedding_changed")
                if old:
                    self.ctx.vectors.drop_model(old)
        elif m.role == "llm":
            self.ctx.llm.stop()  # next request starts the new model
            if self.ctx.settings.get("summaries_enabled"):
                self.ctx.indexer.queue_missing_summaries()
        return {"id": model_id, "role": m.role, "is_active": True, "reindex": reindex}

    def auto_activate(self, model_id: str) -> None:
        """After a download: activate if nothing is active for that role (no re-index needed)."""
        m = self.catalog.get(model_id)
        if m is None or self.active_id(m.role):
            return
        if m.role == "embedding" and self.ctx.db.scalar(
            "SELECT 1 FROM chunks WHERE embedding_model != ? LIMIT 1", (model_id,)
        ):
            # an old index from another model exists: re-embed everything with the new one
            with self.ctx.db.write() as conn:
                conn.execute("UPDATE models SET is_active = 1 WHERE id = ?", (model_id,))
            self.ctx.embedders.reload()
            self.ctx.indexer.reindex_all(reason="embedding_changed")
            return
        with self.ctx.db.write() as conn:
            conn.execute("UPDATE models SET is_active = 1 WHERE id = ?", (model_id,))
        if m.role == "embedding":
            self.ctx.embedders.reload()
        elif m.role == "llm" and self.ctx.settings.get("summaries_enabled"):
            self.ctx.indexer.queue_missing_summaries()

    def delete(self, model_id: str) -> dict[str, Any]:
        m = self.get_or_404(model_id)
        if m.bundled:
            raise AppError("MODEL_ACTIVE", "Bundled models cannot be deleted")
        if self.active_id(m.role) == model_id:
            raise AppError("MODEL_ACTIVE", "The active model cannot be deleted; activate another one first")
        if self.ctx.downloads.is_downloading(model_id):
            self.ctx.downloads.cancel(model_id)
        freed = self.remove_files(m)
        self.set_status(model_id, "not_installed")
        return {"id": model_id, "bytes_freed": freed}

    def remove_files(self, m: CatalogModel) -> int:
        """Delete a model's folder (inside the data dir only) and its partial downloads."""
        from app.services.purge import safe_rmtree, safe_unlink

        freed = 0
        d = self.model_dir(m)
        if d.exists():
            freed += sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
            safe_rmtree(self.ctx, d)
        for part in self.ctx.cfg.tmp_dir.glob(f"{m.id}__*.part"):
            freed += part.stat().st_size
            safe_unlink(self.ctx, part)
        return freed

    def models_size_bytes(self) -> int:
        d = self.ctx.cfg.models_dir
        if not d.exists():
            return 0
        return sum(p.stat().st_size for p in d.rglob("*") if p.is_file())

    def disk_free_bytes(self) -> int:
        return shutil.disk_usage(self.ctx.cfg.data_dir).free
