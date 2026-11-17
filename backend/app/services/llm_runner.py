"""llama-server process manager (SDD 4.1 step 5, 9.3).

Starts lazily on the first LLM request, picks the binary (CUDA > Vulkan > CPU), computes -ngl from
free VRAM, falls back to half the layers and then CPU-only, and unloads after idle time.
On Windows the child is put in a kill-on-close Job Object so it never outlives the backend.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

import httpx

from app.config import backend_root
from app.errors import AppError
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from app.context import AppContext
    from app.services.catalog import CatalogModel

log = get_logger("llm")

IS_WINDOWS = sys.platform == "win32"
EXE = "llama-server.exe" if IS_WINDOWS else "llama-server"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
HEALTH_TIMEOUT_S = 60
OOM_MARKERS = ("out of memory", "failed to allocate", "erroroutofdevicememory", "cudamalloc failed",
               "unable to allocate", "vk::outofdevicememory")


# ---- formulas (unit tested) -----------------------------------------------------------------

def kv_cache_mb(ctx: int, n_layers: int, n_kv_heads: int, head_dim: int) -> float:
    """ctx * n_layers * 2 (K and V) * n_kv_heads * head_dim * 2 bytes (f16) / 1e6."""
    return ctx * n_layers * 2 * n_kv_heads * head_dim * 2 / 1e6


def compute_gpu_layers(vram_free_mb: float, model_size_mb: float, n_layers: int, kv_mb: float) -> int:
    """SDD 9.3: 99 (all) if it fits with 25% headroom, else a proportional share, minimum 0."""
    if vram_free_mb <= 0 or model_size_mb <= 0:
        return 0
    if vram_free_mb >= model_size_mb * 1.25 + kv_mb:
        return 99
    n = math.floor(n_layers * (vram_free_mb - kv_mb - 300) / model_size_mb)
    return max(0, min(n, n_layers))


def fallback_layers(ngl: int, n_layers: int) -> list[int]:
    """Start attempt, then half the GPU layers, then CPU only (deduplicated, in order)."""
    first = ngl
    half = (n_layers if ngl >= 99 else ngl) // 2
    out: list[int] = []
    for v in (first, half, 0):
        if v not in out:
            out.append(v)
    return out


_DEVICE_RE = re.compile(r"^\s*(\S+):\s+(.*?)\s+\((\d+)\s*MiB,\s*(\d+)\s*MiB free\)")


def parse_devices(text: str) -> list[dict[str, Any]]:
    devs = []
    for line in text.splitlines():
        m = _DEVICE_RE.match(line)
        if m:
            devs.append({"name": m.group(1), "description": m.group(2), "total_mb": int(m.group(3)),
                         "free_mb": int(m.group(4))})
    return devs


# ---- Windows job object ---------------------------------------------------------------------

class _JobObject:
    def __init__(self) -> None:
        self.handle = None
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IOC(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("R", "W", "O", "RT", "WT", "OT")]

            class EXT(ctypes.Structure):
                _fields_ = [("Basic", BASIC), ("Io", IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            h = k32.CreateJobObjectW(None, None)
            info = EXT()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if h and k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
                self.handle = h
                self._k32 = k32
        except Exception:
            self.handle = None

    def assign(self, proc: subprocess.Popen) -> None:
        if self.handle is None:
            return
        try:
            self._k32.AssignProcessToJobObject(self.handle, int(proc._handle))  # type: ignore[attr-defined]
        except Exception:
            pass


# ---- server manager -------------------------------------------------------------------------

class LlamaServer:
    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        self.state = "stopped"  # stopped | starting | ready | error
        self.model_id: str | None = None
        self.ctx_size: int | None = None
        self.gpu_layers: int | None = None
        self.backend: str | None = None
        self.device: str | None = None
        self.error: str | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._active = 0
        self._active_lock = threading.Lock()
        self._last_used = time.monotonic()
        self._stop_evt = threading.Event()
        self._job = _JobObject()
        self._devices_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self.external_url = ctx.cfg.llm_external_url
        self.log_path = ctx.cfg.logs_dir / "llama-server.log"
        self._idle_thread = threading.Thread(target=self._idle_loop, name="llm-idle", daemon=True)

    # ---- lifecycle --------------------------------------------------------------------------
    def start_background(self) -> None:
        self._idle_thread.start()

    def shutdown(self) -> None:
        self._stop_evt.set()
        self.stop()

    @property
    def base_url(self) -> str:
        if self.external_url:
            return self.external_url.rstrip("/")
        return f"http://127.0.0.1:{self.ctx.cfg.llama_port}"

    def status(self) -> dict[str, Any]:
        vram_used = None
        m = self.ctx.catalog.get(self.model_id) if self.model_id else None
        if self.state == "ready" and m is not None and self.gpu_layers:
            n_layers = int(m.get("n_layers") or 32)
            share = 1.0 if self.gpu_layers >= n_layers else self.gpu_layers / n_layers
            kv = kv_cache_mb(self.ctx_size or 0, n_layers, int(m.get("n_kv_heads") or 8), int(m.get("head_dim") or 128))
            vram_used = int(m.size_bytes / 2**20 * share + kv)
        return {
            "state": self.state,
            "model_id": self.model_id,
            "ctx": self.ctx_size,
            "gpu_layers": self.gpu_layers,
            "backend": self.backend,
            "device": self.device,
            "vram_used_mb": vram_used,
            "error": self.error,
            "external": bool(self.external_url),
        }

    def _alive(self) -> bool:
        if self.external_url:
            return True
        return self._proc is not None and self._proc.poll() is None

    def touch(self) -> None:
        self._last_used = time.monotonic()

    @contextmanager
    def use(self) -> Iterator[None]:
        with self._active_lock:
            self._active += 1
        self.touch()
        try:
            yield
        finally:
            with self._active_lock:
                self._active -= 1
            self.touch()

    def begin_use(self) -> None:
        with self._active_lock:
            self._active += 1
        self.touch()

    def end_use(self) -> None:
        with self._active_lock:
            self._active -= 1
        self.touch()

    def ensure_ready(self) -> None:
        """Start lazily and block until healthy. Raises AppError on failure."""
        m = self.ctx.models.active("llm")
        if m is None:
            raise AppError("MODEL_NOT_INSTALLED", "Install a language model to get answers")
        with self._lock:
            if self.state == "ready" and self.model_id == m.id and self._alive():
                self.touch()
                return
            self._start_locked(m)

    def stop(self) -> None:
        with self._lock:
            self._kill()
            if self.state != "error":
                self.state = "stopped"
            self.model_id = None if not self.external_url else self.model_id

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        log.info("llama_server_stopped")

    def _idle_loop(self) -> None:
        while not self._stop_evt.wait(15):
            try:
                minutes = int(self.ctx.settings.get("llm_idle_unload_min"))
                if minutes <= 0 or self.external_url or self.state != "ready":
                    continue
                with self._active_lock:
                    busy = self._active > 0
                if not busy and time.monotonic() - self._last_used > minutes * 60:
                    log.info("llama_server_idle_unload", idle_min=minutes)
                    self.stop()
            except Exception:
                log.exception("llm_idle_loop_error")

    # ---- binary / device selection ----------------------------------------------------------
    def find_binaries(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        cfg = self.ctx.cfg
        if cfg.llama_server_path and Path(cfg.llama_server_path).exists():
            found["custom"] = Path(cfg.llama_server_path)
        bases = [cfg.resource_dir / "bin" / "llama", backend_root() / "bin" / "llama",
                 Path(sys.executable).resolve().parent / "llama"]
        for base in bases:
            for variant in ("cuda", "vulkan", "cpu"):
                p = base / variant / EXE
                if p.exists():
                    found.setdefault(variant, p)
        sidecar = Path(sys.executable).resolve().parent / EXE  # Tauri externalBin next to the backend
        if sidecar.exists():
            found.setdefault("sidecar", sidecar)
        on_path = shutil.which("llama-server")
        if on_path:
            found.setdefault("path", Path(on_path))
        return found

    def choose_binary(self) -> tuple[Path, str]:
        found = self.find_binaries()
        gpu = self.ctx.hardware.best_gpu()
        if "custom" in found:
            return found["custom"], "auto" if gpu else "cpu"
        if gpu and gpu.get("vendor") == "nvidia" and "cuda" in found:
            return found["cuda"], "cuda"
        if gpu and "vulkan" in found:
            return found["vulkan"], "vulkan"
        for key in ("sidecar", "path"):
            if key in found:
                return found[key], "auto" if gpu else "cpu"
        if "cpu" in found:
            return found["cpu"], "cpu"
        if "vulkan" in found:
            return found["vulkan"], "vulkan"
        raise AppError(
            "LLM_START_FAILED",
            "llama-server was not found. Run scripts/fetch_llama_server.ps1 or set SS_LLAMA_SERVER.",
        )

    def list_devices(self, exe: Path) -> list[dict[str, Any]]:
        key = str(exe)
        cached = self._devices_cache.get(key)
        if cached and time.monotonic() - cached[0] < 30:
            return cached[1]
        try:
            res = subprocess.run([str(exe), "--list-devices"], capture_output=True, text=True, timeout=30,
                                 cwd=str(exe.parent), creationflags=CREATE_NO_WINDOW)
            devs = parse_devices((res.stdout or "") + "\n" + (res.stderr or ""))
        except Exception:
            devs = []
        self._devices_cache[key] = (time.monotonic(), devs)
        return devs

    def pick_device(self, devs: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Prefer devices matching a usable (discrete) GPU from the hardware profile; integrated GPUs
        report large shared memory in Vulkan and must not win on free memory alone."""
        if not devs:
            return None
        profile = self.ctx.hardware.get(scan_if_missing=False) or {}
        usable = [str(g.get("name", "")).lower() for g in profile.get("gpus", []) if g.get("usable")]
        preferred = [d for d in devs if any(n and (n in d["description"].lower() or d["description"].lower() in n)
                                            for n in usable)]
        if not preferred:
            integrated = ("uhd", "iris", "radeon(tm) graphics", "radeon graphics", "llvmpipe", "microsoft")
            preferred = [d for d in devs if not any(k in d["description"].lower() for k in integrated)]
        return max(preferred or devs, key=lambda d: d["free_mb"])

    # ---- start ------------------------------------------------------------------------------
    def _log_tail(self, offset: int = 0, max_chars: int = 3000) -> str:
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(max(offset, 0))
                data = fh.read()
            return data.decode("utf-8", errors="replace")[-max_chars:]
        except OSError:
            return ""

    def _start_locked(self, m: "CatalogModel") -> None:
        self._kill()
        self.error = None
        self.state = "starting"
        if self.external_url:
            if self._wait_healthy(None, 30):
                self.state, self.model_id, self.backend = "ready", m.id, "external"
                self.ctx_size = int(m.get("ctx_cap") or 4096)
                self.touch()
                return
            self.state, self.error = "error", "External LLM server is not reachable"
            raise AppError("LLM_NOT_READY", self.error)

        gguf = self.ctx.models.primary_file(m)
        if gguf is None or not gguf.exists():
            self.state = "error"
            raise AppError("MODEL_NOT_INSTALLED", f"Model file for {m.id} is missing")
        exe, backend = self.choose_binary()
        tier = self.ctx.hardware.tier
        ctx_size = min(self.ctx.catalog.tier_ctx_cap.get(tier, 4096), int(m.get("ctx_cap") or 4096),
                       int(m.get("max_ctx") or 32768))
        threads = max(1, self.ctx.hardware_cores())
        n_layers = int(m.get("n_layers") or 32)
        kv = kv_cache_mb(ctx_size, n_layers, int(m.get("n_kv_heads") or 8), int(m.get("head_dim") or 128))
        model_mb = m.size_bytes / 2**20

        device = None
        vram_free = 0.0
        if backend != "cpu":
            devs = self.list_devices(exe)
            best = self.pick_device(devs)
            if best is not None:
                vram_free = float(best["free_mb"])
                if len(devs) > 1:
                    device = best["name"]  # e.g. keep layers off a small second GPU / the iGPU
            else:
                vram_free = float(self.ctx.hardware.live_vram_free_mb() or 0)
        ngl = compute_gpu_layers(vram_free, model_mb, n_layers, kv) if backend != "cpu" else 0
        cpu_exe = self.find_binaries().get("cpu")

        last_error = "unknown error"
        for layers in fallback_layers(ngl, n_layers):
            use_exe = exe if layers > 0 or cpu_exe is None else cpu_exe
            use_backend = backend if layers > 0 else "cpu"
            ok, err = self._spawn(use_exe, gguf, ctx_size, layers, threads, device if layers > 0 else None)
            if ok:
                self.state, self.model_id, self.ctx_size = "ready", m.id, ctx_size
                self.gpu_layers, self.backend, self.device = layers, use_backend, device if layers > 0 else None
                self.touch()
                log.info("llama_server_ready", model_id=m.id, ctx=ctx_size, gpu_layers=layers, backend=use_backend)
                return
            last_error = err
            log.warning("llama_server_attempt_failed", gpu_layers=layers, error=err)
        self.state = "error"
        self.error = last_error
        raise AppError("LLM_START_FAILED", "The language model could not be started",
                       details={"error": last_error, "log_tail": self._log_tail()[-2000:]})

    def _wait_healthy(self, proc: subprocess.Popen | None, timeout_s: float, log_offset: int = 0) -> bool | str:
        deadline = time.monotonic() + timeout_s
        with httpx.Client(timeout=2.0) as client:
            while time.monotonic() < deadline:
                if proc is not None and proc.poll() is not None:
                    return False
                try:
                    r = client.get(f"{self.base_url}/health")
                    if r.status_code == 200:
                        return True
                except httpx.HTTPError:
                    pass
                if proc is not None:
                    tail = self._log_tail(log_offset, 20000).lower()
                    if any(mk in tail for mk in OOM_MARKERS):
                        return "oom"
                time.sleep(0.5)
        return False

    def _spawn(self, exe: Path, gguf: Path, ctx_size: int, layers: int, threads: int,
               device: str | None) -> tuple[bool, str]:
        cmd = [str(exe), "-m", str(gguf), "--host", "127.0.0.1", "--port", str(self.ctx.cfg.llama_port),
               "-c", str(ctx_size), "-ngl", str(layers), "-t", str(threads), "--parallel", "1", "--no-webui"]
        if device:
            cmd += ["--device", device]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        offset = self.log_path.stat().st_size if self.log_path.exists() else 0
        logf = open(self.log_path, "ab")
        logf.write(f"\n==== start gpu_layers={layers} ctx={ctx_size} device={device} ====\n".encode())
        logf.flush()
        try:
            proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    cwd=str(exe.parent), creationflags=CREATE_NO_WINDOW)
        except OSError as e:
            logf.close()
            return False, f"spawn failed: {e.__class__.__name__}"
        finally:
            pass
        self._job.assign(proc)
        self._proc = proc
        result = self._wait_healthy(proc, HEALTH_TIMEOUT_S, offset)
        logf.close()
        if result is True:
            return True, ""
        self._kill()
        if result == "oom":
            return False, "out of memory"
        if proc.returncode is not None and proc.returncode != 0:
            return False, f"exited with code {proc.returncode}"
        return False, "did not become healthy in time"
