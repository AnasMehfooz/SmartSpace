from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from starlette.concurrency import run_in_threadpool

from app.api.deps import Paging, get_ctx
from app.schemas import AcceptedOut, ChunkOut, FileDetailOut, FileListItem, Page, TagsIn, TreeNode

router = APIRouter(prefix="/files", tags=["files"])


@router.get("/tree", response_model=list[TreeNode], summary="Lazy folder listing (direct children of `parent`)")
def tree(parent: str | None = Query(None, description="Relative folder; empty = root"), ctx=Depends(get_ctx)):
    return ctx.files.tree(parent)


@router.get("", response_model=Page[FileListItem], summary="Flat, filtered, paginated file list")
def list_files(
    folder: str | None = None,
    ext: str | None = Query(None, description="Comma separated, e.g. pdf,docx"),
    kind: str | None = None,
    status: str | None = Query(None, description="Comma separated statuses"),
    tag: str | None = None,
    doc_type: str | None = None,
    q_name: str | None = Query(None, max_length=200),
    sort: Literal["name", "mtime", "size", "indexed_at", "path"] = "name",
    order: Literal["asc", "desc"] = "asc",
    paging: Paging = Depends(),
    ctx=Depends(get_ctx),
):
    return ctx.files.list(folder=folder, ext=ext, kind=kind, status=status, tag=tag, doc_type=doc_type,
                          q_name=q_name, sort=sort, order=order, limit=paging.limit, offset=paging.offset)


@router.get("/{file_id}", response_model=FileDetailOut)
def detail(file_id: int, ctx=Depends(get_ctx)):
    return ctx.files.detail(file_id)


@router.get("/{file_id}/chunks", response_model=Page[ChunkOut], summary="Extracted text chunks (debug/preview)")
def chunks(file_id: int, limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0), ctx=Depends(get_ctx)):
    return ctx.files.chunks(file_id, limit, offset)


@router.get("/{file_id}/preview", response_class=Response,
            responses={200: {"content": {"image/jpeg": {}}}},
            summary="Downscaled JPEG (max 1024 px) for images and PDF pages")
async def preview(file_id: int, page: int = Query(1, ge=1), ctx=Depends(get_ctx)):
    data = await run_in_threadpool(ctx.files.preview, file_id, page)
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})


@router.post("/{file_id}/reindex", response_model=AcceptedOut, status_code=202)
def reindex(file_id: int, ctx=Depends(get_ctx)):
    ctx.files.require(file_id)
    ctx.indexer.reindex_file(file_id)
    return {"accepted": True}


@router.put("/{file_id}/tags", response_model=FileDetailOut, summary="Replace the user tags of a file")
def put_tags(file_id: int, body: TagsIn, ctx=Depends(get_ctx)):
    return ctx.files.set_user_tags(file_id, body.tags)


@router.delete("/{file_id}/index", response_model=FileDetailOut,
               summary="Remove this file's derived data; the row becomes skipped/excluded")
async def delete_index(file_id: int, ctx=Depends(get_ctx)):
    ctx.files.require(file_id)
    await run_in_threadpool(ctx.indexer.remove_file_index, file_id)
    return ctx.files.detail(file_id)
