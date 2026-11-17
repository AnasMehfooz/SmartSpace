from __future__ import annotations

import asyncio
import platform
import sys
import time

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_ctx
from app.config import VERSION
from app.schemas import HardwareOut, HealthOut, SystemInfoOut

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthOut, summary="Liveness/readiness (no auth)")
def health(request: Request):
    ctx = getattr(request.app.state, "ctx", None)
    db_ok = False
    if ctx is not None:
        try:
            db_ok = ctx.db.scalar("SELECT 1") == 1
        except Exception:
            db_ok = False
    return {
        "status": "ok",
        "ready": bool(ctx and ctx.ready and db_ok),
        "version": VERSION,
        "db_ok": db_ok,
        "llm_state": ctx.llm.state if ctx else "stopped",
    }


@router.post("/system/shutdown", summary="Graceful stop (flush DB, stop llama-server and watcher)")
async def shutdown(request: Request):
    loop = asyncio.get_running_loop()
    loop.call_later(0.2, request.app.state.request_exit)
    return {"stopping": True}


@router.get("/system/info", response_model=SystemInfoOut)
def info(ctx=Depends(get_ctx)):
    return {
        "version": VERSION,
        "data_dir": str(ctx.cfg.data_dir),
        "db_size_bytes": ctx.db.size_bytes(),
        "vectors_size_bytes": ctx.vectors.size_bytes(),
        "models_size_bytes": ctx.models.models_size_bytes(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "logs_dir": str(ctx.cfg.logs_dir),
        "uptime_s": int(time.time() - ctx.started_at),
    }


@router.get("/system/hardware", response_model=HardwareOut, summary="Stored hardware profile (scans if none)")
async def hardware(ctx=Depends(get_ctx)):
    return await run_in_threadpool(ctx.hardware.get)


@router.post("/system/hardware/scan", response_model=HardwareOut)
async def hardware_scan(ctx=Depends(get_ctx)):
    return await run_in_threadpool(ctx.hardware.scan)
