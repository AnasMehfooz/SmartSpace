"""Uniform error format: {"error": {"code", "message", "details"}} (SDD 10, 10.10)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging_setup import get_logger

log = get_logger("errors")

ERROR_STATUS: dict[str, int] = {
    "UNAUTHORIZED": 401,
    "VALIDATION_ERROR": 422,
    "FILE_NOT_FOUND": 404,
    "SESSION_NOT_FOUND": 404,
    "MODEL_NOT_FOUND": 404,
    "NOT_FOUND": 404,
    "METHOD_NOT_ALLOWED": 405,
    "WORKSPACE_NOT_SET": 409,
    "WORKSPACE_EXISTS": 409,
    "WORKSPACE_PATH_INVALID": 400,
    "REINDEX_REQUIRED": 409,
    "MODEL_NOT_INSTALLED": 409,
    "MODEL_ACTIVE": 409,
    "DOWNLOAD_FAILED": 502,
    "DISK_FULL": 507,
    "LLM_NOT_READY": 503,
    "LLM_START_FAILED": 500,
    "INDEX_BUSY": 409,
    "CONFIRM_REQUIRED": 400,
    "INTERNAL_ERROR": 500,
}


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or ERROR_STATUS.get(code, 400)
        self.details = details or {}


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def error_response(
    code: str, message: str, status: int, details: dict[str, Any] | None = None
) -> JSONResponse:
    return JSONResponse(error_body(code, message, details), status_code=status)


def _clean_validation_errors(errors: list[dict]) -> list[dict]:
    out = []
    for e in errors:
        out.append(
            {
                "loc": [str(x) for x in e.get("loc", ())],
                "msg": str(e.get("msg", "")),
                "type": str(e.get("type", "")),
            }
        )
    return out


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return error_response(exc.code, exc.message, exc.status, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        return error_response(
            "VALIDATION_ERROR",
            "Request body or query is invalid",
            422,
            {"errors": _clean_validation_errors(exc.errors())},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 401: "UNAUTHORIZED"}.get(
            exc.status_code, "VALIDATION_ERROR" if exc.status_code < 500 else "INTERNAL_ERROR"
        )
        return error_response(code, str(exc.detail), exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12]
        log.exception("unhandled_error", request_id=request_id, path=request.url.path)
        return error_response(
            "INTERNAL_ERROR",
            "Unexpected error",
            500,
            {"request_id": request_id},
        )
