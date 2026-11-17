from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_ctx
from app.schemas import ExclusionIn, ExclusionOut, PurgeResultOut, WorkspaceIn, WorkspaceOut, WorkspaceStatsOut

router = APIRouter(prefix="/workspace", tags=["workspace"])


@router.get("", response_model=WorkspaceOut | None, summary="null or the workspace")
def get_workspace(ctx=Depends(get_ctx)):
    return ctx.workspace.get()


@router.put("", response_model=WorkspaceOut,
            summary="Set the root folder; 409 WORKSPACE_EXISTS for another path unless replace=true")
async def put_workspace(body: WorkspaceIn, ctx=Depends(get_ctx)):
    return await run_in_threadpool(ctx.workspace.set, body.root_path, body.replace)


@router.delete("", response_model=PurgeResultOut, summary="Unlink the workspace and delete the whole index")
async def delete_workspace(ctx=Depends(get_ctx)):
    ctx.workspace.require_root()
    items, freed = await run_in_threadpool(ctx.purge.unlink_workspace, "index", "workspace")
    return {"scope": "workspace", "items_deleted": items, "bytes_freed": freed}


@router.get("/stats", response_model=WorkspaceStatsOut)
def stats(ctx=Depends(get_ctx)):
    return ctx.workspace.stats()


@router.get("/exclusions", response_model=list[ExclusionOut])
def exclusions(ctx=Depends(get_ctx)):
    return ctx.workspace.exclusions()


@router.post("/exclusions", response_model=ExclusionOut, status_code=201,
             summary="Add a glob (relative to the root); matching files are removed from the index")
async def add_exclusion(body: ExclusionIn, ctx=Depends(get_ctx)):
    return await run_in_threadpool(ctx.workspace.add_exclusion, body.pattern)


@router.delete("/exclusions/{exclusion_id}", status_code=204)
def delete_exclusion(exclusion_id: int, ctx=Depends(get_ctx)):
    ctx.workspace.delete_exclusion(exclusion_id)
