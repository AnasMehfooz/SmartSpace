from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import Paging, get_ctx, sse_response
from app.errors import AppError
from app.schemas import AcceptedOut, IndexErrorItem, IndexStatusOut, Page

router = APIRouter(prefix="/index", tags=["index"])


@router.post("/start", response_model=AcceptedOut, status_code=202, summary="Full scan + queue all pending files")
def start(ctx=Depends(get_ctx)):
    ctx.workspace.require_root()
    if ctx.models.active("embedding") is None:
        raise AppError("MODEL_NOT_INSTALLED", "Download an embedding model before indexing")
    ctx.indexer.resume("user")
    ctx.watcher.start()
    started = ctx.indexer.start_full_scan()
    return {"accepted": True, "detail": None if started else "scan already running; another pass is queued"}


@router.post("/pause", response_model=IndexStatusOut, summary="Pause (running jobs finish)")
def pause(ctx=Depends(get_ctx)):
    ctx.indexer.pause("user")
    return ctx.indexer.status()


@router.post("/resume", response_model=IndexStatusOut)
def resume(ctx=Depends(get_ctx)):
    ctx.indexer.resume("user")
    return ctx.indexer.status()


@router.post("/retry-failed", response_model=AcceptedOut, status_code=202)
def retry_failed(ctx=Depends(get_ctx)):
    n = ctx.indexer.retry_failed()
    return {"accepted": True, "detail": f"{n} item(s) re-queued"}


@router.get("/status", response_model=IndexStatusOut)
def status(ctx=Depends(get_ctx)):
    return ctx.indexer.status()


@router.get("/errors", response_model=Page[IndexErrorItem], summary="Files in error or skipped, with reason")
def errors(paging: Paging = Depends(), include_skipped: bool = True, ctx=Depends(get_ctx)):
    statuses = ("error", "skipped") if include_skipped else ("error",)
    qs = ",".join("?" * len(statuses))
    rows = ctx.db.query(
        f"SELECT id, rel_path, name, status, skip_reason, error_message, updated_at FROM files WHERE status IN ({qs}) "
        f"ORDER BY CASE status WHEN 'error' THEN 0 ELSE 1 END, updated_at DESC LIMIT ? OFFSET ?",
        (*statuses, paging.limit, paging.offset))
    total = ctx.db.scalar(f"SELECT COUNT(*) FROM files WHERE status IN ({qs})", statuses) or 0
    return {"items": [dict(r) for r in rows], "total": total, "limit": paging.limit, "offset": paging.offset}


@router.get("/events", summary="SSE: state_changed, progress, file_indexed, file_error, file_skipped, "
                               "file_removed, summary_done, summary_failed")
async def events(request: Request, ctx=Depends(get_ctx)):
    async def gen():
        st = ctx.indexer.status()
        yield "state_changed", {"state": st["state"], "paused_reason": st["paused_reason"]}
        yield "progress", {k: st[k] for k in ("indexed", "total", "eta_s", "pending", "processing", "skipped",
                                              "errors", "summaries_done", "summaries_pending")}
        async for item in ctx.bus.subscribe("index"):
            yield item

    return sse_response(gen(), request)
