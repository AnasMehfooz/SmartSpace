"""Runtime configuration: paths, ports, token. Everything comes from SS_* env vars (SDD 4.1, 14)."""

from __future__ import annotations

import os
import secrets
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app import __version__

VERSION = __version__
API_PREFIX = "/api/v1"
APP_NAME = "SmartSpace"


def app_dir() -> Path:
    """Directory of the `app` package (holds prompts, migrations, catalog)."""
    if getattr(sys, "frozen", False):  # PyInstaller one-folder build
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "app"
    return Path(__file__).resolve().parent


def backend_root() -> Path:
    """`backend/` in dev, the folder of the executable when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return app_dir().parent


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / APP_NAME


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    port: int
    token: str
    data_dir: Path
    llama_port: int
    resource_dir: Path
    log_level: str = "INFO"
    dev: bool = False
    # SS_LLAMA_SERVER: explicit path to a llama-server executable (else auto-discovered).
    llama_server_path: Path | None = None
    # SS_LLM_EXTERNAL_URL: use an already running OpenAI-compatible server instead of spawning one.
    llm_external_url: str | None = None
    token_generated: bool = False
    console_log: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "smartspace.db"

    @property
    def vectors_dir(self) -> Path:
        return self.data_dir / "vectors"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def prompts_dir(self) -> Path:
        return app_dir() / "prompts"

    def resource_file(self, name: str) -> Path:
        """Look in SS_RESOURCE_DIR first (Tauri resources), then the bundled package copy."""
        candidate = self.resource_dir / name
        if candidate.exists():
            return candidate
        return app_dir() / name

    @property
    def catalog_path(self) -> Path:
        return self.resource_file("models_catalog.json")

    @property
    def allowlist_path(self) -> Path:
        return self.resource_file("download_allowlist.json")

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.vectors_dir, self.models_dir, self.logs_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_config(**overrides) -> Config:
    """Build the config from the environment. Keyword overrides win (used by tests)."""
    env = os.environ
    token = env.get("SS_TOKEN") or ""
    generated = False
    if not token:
        token = secrets.token_urlsafe(32)
        generated = True

    llama_server = env.get("SS_LLAMA_SERVER")
    values: dict = {
        "port": int(env.get("SS_PORT") or 8765),
        "token": token,
        "data_dir": Path(env["SS_DATA_DIR"]) if env.get("SS_DATA_DIR") else default_data_dir(),
        "llama_port": int(env.get("SS_LLAMA_PORT") or 0) or free_port(),
        "resource_dir": Path(env["SS_RESOURCE_DIR"]) if env.get("SS_RESOURCE_DIR") else app_dir(),
        "log_level": (env.get("SS_LOG_LEVEL") or "INFO").upper(),
        "dev": _env_bool("SS_DEV"),
        "llama_server_path": Path(llama_server) if llama_server else None,
        "llm_external_url": env.get("SS_LLM_EXTERNAL_URL") or None,
        "token_generated": generated,
    }
    values.update(overrides)
    return Config(**values)
