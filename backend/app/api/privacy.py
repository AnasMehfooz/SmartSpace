from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from app.api.deps import Paging, get_ctx
from app.schemas import AuditItem, Page, PrivacyOverviewOut, PurgeIn, PurgeResultOut

router = APIRouter(prefix="/privacy", tags=["privacy"])


@router.get("/overview", response_model=PrivacyOverviewOut, summary="Everything the app has stored")
def overview(ctx=Depends(get_ctx)):
    return ctx.purge.overview()


@router.post("/purge", response_model=PurgeResultOut,
             summary="Delete app-created data only (never your files). confirm must be true")
async def purge(body: PurgeIn, ctx=Depends(get_ctx)):
    return await run_in_threadpool(ctx.purge.purge, body.scope, body.file_ids, body.confirm)


@router.get("/audit", response_model=Page[AuditItem])
def audit(paging: Paging = Depends(), ctx=Depends(get_ctx)):
    return ctx.purge.audit(paging.limit, paging.offset)


@router.get("/export", summary="JSON of summaries, tags and doc types (no file content)")
def export(ctx=Depends(get_ctx)):
    return ctx.purge.export()
