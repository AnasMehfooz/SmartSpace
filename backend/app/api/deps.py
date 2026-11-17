from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, AsyncIterator

from fastapi import Query, Request
from sse_starlette.sse import EventSourceResponse

from app.errors import AppError

if TYPE_CHECKING:
    from app.context import AppContext


def get_ctx(request: Request) -> "AppContext":
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None or not ctx.ready:
        raise AppError("INTERNAL_ERROR", "Backend is starting", status=503)
    return ctx


class Paging:
    def __init__(self, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        self.limit = limit
        self.offset = offset


def sse_response(gen: AsyncIterator[tuple[str, Any]], request: Request) -> EventSourceResponse:
    async def events():
        async for event, data in gen:
            if await request.is_disconnected():
                break
            yield {"event": event, "data": json.dumps(data, ensure_ascii=False)}

    return EventSourceResponse(events(), ping=15, headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
