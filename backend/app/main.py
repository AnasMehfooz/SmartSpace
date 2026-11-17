"""FastAPI app factory and lifespan (SDD 4.1).

Dev:  uv run uvicorn app.main:app --port 8765   (with SS_TOKEN=dev, SS_DEV=1)
Prod: python -m app  (Tauri passes SS_PORT, SS_TOKEN, SS_DATA_DIR, SS_LLAMA_PORT, SS_RESOURCE_DIR)
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer
from starlette.concurrency import run_in_threadpool

from app.api import ROUTERS
from app.config import API_PREFIX, VERSION, Config, load_config
from app.errors import install_error_handlers
from app.logging_setup import get_logger
from app.security import TokenAuthMiddleware

log = get_logger("main")

CORS_ORIGINS = [
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


class RequestLogMiddleware:
    """Request id + timing. Logs method, path (never the query string), status and duration."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = uuid.uuid4().hex[:12]
        scope.setdefault("state", {})["request_id"] = rid
        t0 = time.perf_counter()
        status = {"code": 500}

        async def _send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", rid.encode()))
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            path = scope.get("path", "")
            if not path.endswith("/health"):
                log.info("request", request_id=rid, method=scope.get("method"), path=path, status=status["code"],
                         duration_ms=int((time.perf_counter() - t0) * 1000))


def _default_exit() -> None:
    signal.raise_signal(signal.SIGINT)


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from app.context import AppContext

        loop = asyncio.get_running_loop()
        ctx = await run_in_threadpool(AppContext, cfg, loop)
        app.state.ctx = ctx
        ctx.start()
        log.info("backend_started", port=cfg.port, version=VERSION, data_dir=str(cfg.data_dir), dev=cfg.dev)
        if cfg.token_generated:
            print(f"[smartspace] SS_TOKEN not set; generated token for this run: {cfg.token}", file=sys.stderr)
        try:
            yield
        finally:
            await run_in_threadpool(ctx.shutdown)

    app = FastAPI(
        title="SMART SPACE backend",
        version=VERSION,
        description="Local-first AI file manager API. All endpoints except GET /api/v1/health need "
                    "`Authorization: Bearer <SS_TOKEN>`. Errors: `{error: {code, message, details}}`.",
        lifespan=lifespan,
        docs_url="/docs" if cfg.dev else None,
        redoc_url=None,
        openapi_url="/openapi.json",
        dependencies=[Security(HTTPBearer(auto_error=False))],  # documents the scheme; middleware enforces it
    )
    app.state.cfg = cfg
    app.state.request_exit = _default_exit
    install_error_handlers(app)
    exempt = {"/docs", "/openapi.json", "/docs/oauth2-redirect"} if cfg.dev else set()
    app.add_middleware(TokenAuthMiddleware, token=cfg.token, exempt_paths=exempt)
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-request-id"],
    )
    for r in ROUTERS:
        app.include_router(r, prefix=API_PREFIX)
    return app


app = create_app()
