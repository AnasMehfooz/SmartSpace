from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import get_ctx
from app.schemas import SettingsUpdate

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=dict[str, Any])
def get_settings(ctx=Depends(get_ctx)):
    return ctx.settings.all()


@router.put("", response_model=dict[str, Any], summary="Partial update; unknown keys or wrong types -> 422")
def put_settings(body: SettingsUpdate, ctx=Depends(get_ctx)):
    return ctx.settings.update(body)
