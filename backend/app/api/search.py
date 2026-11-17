from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_ctx
from app.schemas import SearchIn, SearchOut

router = APIRouter(tags=["search"])


@router.post("/search", response_model=SearchOut, response_model_exclude_unset=True,
             summary="Hybrid search. mode=quick: top 6 files, short snippet, no summary. Queries are not stored.")
async def search(body: SearchIn, ctx=Depends(get_ctx)):
    return await run_in_threadpool(ctx.search.search, body.query, body.filters, body.limit, body.mode)
