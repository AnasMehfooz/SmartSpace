from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import Paging, get_ctx, sse_response
from app.schemas import (ChatMessageIn, ChatRenameIn, ChatSessionDetailOut, ChatSessionIn, ChatSessionOut, Page,
                         StopOut)

router = APIRouter(prefix="/chat", tags=["chat"])


@router.get("/sessions", response_model=Page[ChatSessionOut])
def list_sessions(paging: Paging = Depends(), ctx=Depends(get_ctx)):
    return ctx.chat.list_sessions(paging.limit, paging.offset)


@router.post("/sessions", response_model=ChatSessionOut, status_code=201)
def create_session(body: ChatSessionIn | None = None, ctx=Depends(get_ctx)):
    return ctx.chat.create_session(body.title if body else None)


@router.delete("/sessions", summary="Delete all sessions")
def delete_all(ctx=Depends(get_ctx)):
    return {"deleted": ctx.chat.delete_all()}


@router.get("/sessions/{session_id}", response_model=ChatSessionDetailOut)
def get_session(session_id: int, ctx=Depends(get_ctx)):
    return ctx.chat.get_session(session_id)


@router.patch("/sessions/{session_id}", response_model=ChatSessionDetailOut)
def rename(session_id: int, body: ChatRenameIn, ctx=Depends(get_ctx)):
    return ctx.chat.rename(session_id, body.title)


@router.delete("/sessions/{session_id}", status_code=204)
def delete(session_id: int, ctx=Depends(get_ctx)):
    ctx.chat.delete(session_id)


@router.post(
    "/sessions/{session_id}/messages",
    summary="Ask a question. SSE stream: meta, files, token*, sources, done (or error)",
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def post_message(session_id: int, body: ChatMessageIn, request: Request, ctx=Depends(get_ctx)):
    ctx.chat.validate_new_message(session_id, body.content)
    return sse_response(ctx.chat.stream_answer(session_id, body.content, body.filters), request)


@router.post("/sessions/{session_id}/stop", response_model=StopOut, summary="Abort the current generation")
async def stop(session_id: int, ctx=Depends(get_ctx)):
    ctx.chat.require_session(session_id)
    return {"stopped": ctx.chat.stop(session_id)}
