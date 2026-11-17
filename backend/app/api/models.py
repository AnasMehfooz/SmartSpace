from __future__ import annotations

import threading

from fastapi import APIRouter, Body, Depends, Request
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_ctx, sse_response
from app.errors import AppError
from app.logging_setup import get_logger
from app.schemas import (ActivateIn, ActivateOut, DownloadQueuedOut, LlmStatusOut, ModelOut, RecommendationsOut)

router = APIRouter(prefix="/models", tags=["models"])
log = get_logger("api.models")


@router.get("/catalog", response_model=list[ModelOut])
def catalog(ctx=Depends(get_ctx)):
    return ctx.models.catalog_view()


@router.get("/recommendations", response_model=RecommendationsOut)
async def recommendations(ctx=Depends(get_ctx)):
    return await run_in_threadpool(ctx.models.recommendations)


@router.get("/downloads/events", summary="SSE: queued, progress, completed, failed, cancelled")
async def download_events(request: Request, ctx=Depends(get_ctx)):
    async def gen():
        for mid, snap in ctx.downloads.progress_snapshot().items():
            yield ("progress" if snap.get("state") == "downloading" else "queued"), snap
        async for item in ctx.bus.subscribe("downloads"):
            yield item

    return sse_response(gen(), request)


@router.get("/llm/status", response_model=LlmStatusOut)
def llm_status(ctx=Depends(get_ctx)):
    return ctx.llm.status()


@router.post("/llm/start", response_model=LlmStatusOut, status_code=202,
             summary="Start llama-server with the active LLM (poll /models/llm/status)")
def llm_start(ctx=Depends(get_ctx)):
    if ctx.models.active("llm") is None:
        raise AppError("MODEL_NOT_INSTALLED", "Install and activate a language model first")

    def run():
        try:
            ctx.llm.ensure_ready()
        except Exception:
            log.warning("llm_start_failed")

    if ctx.llm.state != "ready":
        ctx.llm.state = "starting"
        threading.Thread(target=run, name="llm-start", daemon=True).start()
    return ctx.llm.status()


@router.post("/llm/stop", response_model=LlmStatusOut)
async def llm_stop(ctx=Depends(get_ctx)):
    await run_in_threadpool(ctx.llm.stop)
    return ctx.llm.status()


@router.post("/{model_id}/download", response_model=DownloadQueuedOut, status_code=202)
def download(model_id: str, ctx=Depends(get_ctx)):
    return {"download_id": ctx.downloads.enqueue(model_id), "model_id": model_id}


@router.post("/{model_id}/cancel")
def cancel(model_id: str, ctx=Depends(get_ctx)):
    return {"cancelled": ctx.downloads.cancel(model_id)}


@router.post("/{model_id}/activate", response_model=ActivateOut,
             summary="Activate for its role; embedding changes need confirm_reindex (else 409 REINDEX_REQUIRED)")
async def activate(model_id: str, body: ActivateIn | None = Body(default=None), ctx=Depends(get_ctx)):
    confirm = bool(body and body.confirm_reindex)
    return await run_in_threadpool(ctx.models.activate, model_id, confirm)


@router.delete("/{model_id}", summary="Delete model files (not the active model)")
async def delete(model_id: str, ctx=Depends(get_ctx)):
    return await run_in_threadpool(ctx.models.delete, model_id)
