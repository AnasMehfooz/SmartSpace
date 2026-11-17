"""Hardware profiler (SDD 9.1) and tier rules (SDD 9.2)."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import threading
from typing import TYPE_CHECKING, Any

import psutil

from app.db import utcnow
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from app.context import AppContext

log = get_logger("hardware")

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# ---- detection ------------------------------------------------------------------------------

def _cpu_info() -> dict[str, Any]:
    try:
        import cpuinfo

        info = cpuinfo.get_cpu_info()
        flags = set(info.get("flags", []))
        return {
            "cpu_name": info.get("brand_raw") or platform.processor() or "unknown",
            "cpu_avx2": "avx2" in flags,
            "cpu_avx512": any(f.startswith("avx512") for f in flags),
        }
    except Exception as e:  # pragma: no cover - depends on host
        log.warning("cpuinfo_failed", error=e.__class__.__name__)
        machine = platform.machine().lower()
        return {
            "cpu_name": platform.processor() or "unknown",
            "cpu_avx2": machine in {"amd64", "x86_64"},
            "cpu_avx512": False,
        }


def _nvidia_gpus() -> list[dict[str, Any]]:
    try:
        import pynvml

        pynvml.nvmlInit()
    except Exception:
        return []
    gpus = []
    try:
        driver = pynvml.nvmlSystemGetDriverVersion()
        driver = driver.decode() if isinstance(driver, bytes) else str(driver)
        for i in range(pynvml.nvmlDeviceGetCount()):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(h)
            name = name.decode() if isinstance(name, bytes) else str(name)
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            gpus.append(
                {
                    "name": name,
                    "vendor": "nvidia",
                    "vram_mb": int(mem.total // 2**20),
                    "vram_free_mb": int(mem.free // 2**20),
                    "driver": driver,
                    "backend": "cuda",
                    "integrated": False,
                    "vram_exact": True,
                }
            )
    except Exception as e:  # pragma: no cover
        log.warning("nvml_failed", error=e.__class__.__name__)
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass
    return gpus


def _windows_registry_vram() -> dict[str, int]:
    """Real 64-bit VRAM sizes from the display class registry key (WMI caps at 4 GB)."""
    out: dict[str, int] = {}
    try:
        import winreg

        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as key:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(key, sub) as sk:
                        desc = winreg.QueryValueEx(sk, "DriverDesc")[0]
                        size = winreg.QueryValueEx(sk, "HardwareInformation.qwMemorySize")[0]
                        if isinstance(size, bytes):
                            size = int.from_bytes(size, "little")
                        out[str(desc)] = int(size) // 2**20
                except OSError:
                    continue
    except Exception:
        pass
    return out


def _windows_other_gpus(known_names: set[str]) -> list[dict[str, Any]]:
    cmd = [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,AdapterRAM,DriverVersion,AdapterCompatibility | ConvertTo-Json -Compress",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW)
        data = json.loads(res.stdout or "[]")
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    reg = _windows_registry_vram()
    gpus = []
    for d in data:
        name = str(d.get("Name") or "").strip()
        if not name or name in known_names:
            continue
        compat = str(d.get("AdapterCompatibility") or "")
        low = f"{name} {compat}".lower()
        if "microsoft" in low or "basic display" in low or "remote" in low or "virtual" in low:
            continue
        if "nvidia" in low:
            vendor = "nvidia"
        elif "amd" in low or "radeon" in low or "advanced micro" in low:
            vendor = "amd"
        elif "intel" in low:
            vendor = "intel"
        else:
            vendor = "other"
        wmi_ram = d.get("AdapterRAM") or 0
        try:
            wmi_mb = int(wmi_ram) // 2**20 if int(wmi_ram) > 0 else 0
        except (TypeError, ValueError):
            wmi_mb = 0
        vram_mb = reg.get(name, wmi_mb)
        integrated = (vendor == "intel" and "arc" not in low) or (
            vendor == "amd" and vram_mb < 2048 and ("graphics" in low or "vega" in low)
        )
        gpus.append(
            {
                "name": name,
                "vendor": vendor,
                "vram_mb": vram_mb,
                "vram_free_mb": None,
                "driver": d.get("DriverVersion"),
                "backend": "vulkan",
                "integrated": integrated,
                "vram_exact": name in reg,
            }
        )
    return gpus


def _mac_gpus(ram_total_mb: int) -> list[dict[str, Any]]:
    if platform.machine().lower() != "arm64":
        return []
    return [
        {
            "name": "Apple Silicon GPU",
            "vendor": "apple",
            "vram_mb": int(ram_total_mb * 0.66),  # unified memory usable by Metal
            "vram_free_mb": None,
            "driver": None,
            "backend": "metal",
            "integrated": False,
            "vram_exact": False,
        }
    ]


def _linux_gpus() -> list[dict[str, Any]]:
    gpus = []
    try:
        res = subprocess.run(["lspci"], capture_output=True, text=True, timeout=10)
        for line in res.stdout.splitlines():
            if "VGA" in line or "3D controller" in line:
                name = line.split(":", 2)[-1].strip()
                low = name.lower()
                vendor = "amd" if ("amd" in low or "ati" in low) else "intel" if "intel" in low else "other"
                if "nvidia" in low:
                    continue  # covered by NVML
                gpus.append(
                    {"name": name, "vendor": vendor, "vram_mb": 0, "vram_free_mb": None, "driver": None,
                     "backend": "vulkan", "integrated": vendor == "intel", "vram_exact": False}
                )
    except Exception:
        pass
    return gpus


def detect_gpus(ram_total_mb: int) -> list[dict[str, Any]]:
    gpus = _nvidia_gpus()
    if sys.platform == "win32":
        gpus += _windows_other_gpus({g["name"] for g in gpus})
    elif sys.platform == "darwin":
        gpus += _mac_gpus(ram_total_mb)
    else:
        gpus += _linux_gpus()
    for g in gpus:
        g["usable"] = (not g.get("integrated")) and (g.get("vram_mb") or 0) >= 2048
    return gpus


def battery_status() -> dict[str, Any]:
    try:
        b = psutil.sensors_battery()
    except Exception:
        b = None
    if b is None:
        return {"present": False, "percent": None, "plugged": True}
    return {"present": True, "percent": round(b.percent), "plugged": bool(b.power_plugged)}


def _os_name() -> str:
    if sys.platform == "win32":
        ver = platform.version()
        try:
            build = int(ver.split(".")[-1])
        except ValueError:
            build = 0
        return f"Windows {'11' if build >= 22000 else platform.release()} ({ver})"
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0]}"
    return f"{platform.system()} {platform.release()}"


# ---- tier rules -----------------------------------------------------------------------------

def compute_tier(ram_total_mb: int, avx2: bool, gpus: list[dict[str, Any]]) -> int:
    """SDD 9.2. Sizes are rounded to nominal GB (a "16 GB" laptop reports ~15.7 GB)."""
    ram = round(ram_total_mb / 1024)
    vram = round(max((g.get("vram_mb") or 0 for g in gpus if g.get("usable")), default=0) / 1024)
    if ram < 8 or not avx2:
        return 0
    if vram >= 12:
        return 4
    if vram >= 6:
        return 3
    if ram >= 24:
        return 3  # CPU-only with plenty of RAM
    if ram >= 16 and vram >= 4:
        return 2
    return 1


def scan_hardware(data_dir) -> dict[str, Any]:
    vm = psutil.virtual_memory()
    ram_total_mb = int(vm.total // 2**20)
    cpu = _cpu_info()
    gpus = detect_gpus(ram_total_mb)
    try:
        disk_free_mb = int(shutil.disk_usage(data_dir).free // 2**20)
    except OSError:
        disk_free_mb = None
    profile = {
        "os": _os_name(),
        "arch": platform.machine(),
        "cpu_name": cpu["cpu_name"],
        "cpu_cores_physical": psutil.cpu_count(logical=False) or 1,
        "cpu_cores_logical": psutil.cpu_count(logical=True) or 1,
        "cpu_avx2": bool(cpu["cpu_avx2"]),
        "cpu_avx512": bool(cpu["cpu_avx512"]),
        "ram_total_mb": ram_total_mb,
        "ram_available_mb": int(vm.available // 2**20),
        "gpus": gpus,
        "disk_free_mb": disk_free_mb,
        "battery": battery_status(),
        "scanned_at": utcnow(),
    }
    profile["tier"] = compute_tier(ram_total_mb, profile["cpu_avx2"], gpus)
    return profile


class HardwareService:
    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        self._lock = threading.Lock()
        self._profile: dict[str, Any] | None = self._load()

    def _load(self) -> dict[str, Any] | None:
        row = self.ctx.db.query_one("SELECT * FROM hardware_profile WHERE id = 1")
        if row is None:
            return None
        d = dict(row)
        d.pop("id", None)
        d["gpus"] = json.loads(d.pop("gpus_json") or "[]")
        d["cpu_avx2"] = bool(d["cpu_avx2"])
        d.setdefault("cpu_avx512", False)
        d.setdefault("arch", platform.machine())
        return d

    def _store(self, p: dict[str, Any]) -> None:
        with self.ctx.db.write() as conn:
            conn.execute(
                "INSERT INTO hardware_profile (id, os, cpu_name, cpu_cores_physical, cpu_cores_logical, "
                "cpu_avx2, ram_total_mb, ram_available_mb, gpus_json, disk_free_mb, tier, scanned_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "os=excluded.os, cpu_name=excluded.cpu_name, cpu_cores_physical=excluded.cpu_cores_physical, "
                "cpu_cores_logical=excluded.cpu_cores_logical, cpu_avx2=excluded.cpu_avx2, "
                "ram_total_mb=excluded.ram_total_mb, ram_available_mb=excluded.ram_available_mb, "
                "gpus_json=excluded.gpus_json, disk_free_mb=excluded.disk_free_mb, tier=excluded.tier, "
                "scanned_at=excluded.scanned_at",
                (
                    p["os"], p["cpu_name"], p["cpu_cores_physical"], p["cpu_cores_logical"],
                    int(p["cpu_avx2"]), p["ram_total_mb"], p["ram_available_mb"], json.dumps(p["gpus"]),
                    p["disk_free_mb"], p["tier"], p["scanned_at"],
                ),
            )

    def scan(self, only_if_missing: bool = False) -> dict[str, Any]:
        with self._lock:  # concurrent callers wait for one scan instead of running their own
            if not (only_if_missing and self._profile is not None):
                profile = scan_hardware(self.ctx.cfg.data_dir)
                self._store(profile)
                self._profile = profile
                log.info("hardware_scanned", tier=profile["tier"], gpus=len(profile["gpus"]))
                self._apply_tier_defaults(profile["tier"])
        return self.get()

    def get(self, scan_if_missing: bool = True) -> dict[str, Any] | None:
        if self._profile is None and scan_if_missing:
            return self.scan(only_if_missing=True)
        if self._profile is None:
            return None
        p = dict(self._profile)
        # live values
        p["battery"] = battery_status()
        try:
            p["ram_available_mb"] = int(psutil.virtual_memory().available // 2**20)
            p["disk_free_mb"] = int(shutil.disk_usage(self.ctx.cfg.data_dir).free // 2**20)
        except OSError:
            pass
        return p

    @property
    def tier(self) -> int:
        p = self._profile or self.get()
        return int(p["tier"]) if p else 1

    def _apply_tier_defaults(self, tier: int) -> None:
        self.ctx.settings.set_dynamic_default("summaries_enabled", tier != 0)

    def best_gpu(self) -> dict[str, Any] | None:
        p = self._profile or self.get()
        usable = [g for g in (p or {}).get("gpus", []) if g.get("usable")]
        if not usable:
            return None
        return max(usable, key=lambda g: g.get("vram_mb") or 0)

    def live_vram_free_mb(self) -> int | None:
        """Free VRAM on the best GPU right now (NVML), else the stored total."""
        best = self.best_gpu()
        if best is None:
            return None
        if best["vendor"] == "nvidia":
            for g in _nvidia_gpus():
                if g["name"] == best["name"]:
                    return g["vram_free_mb"]
        return best.get("vram_mb")
