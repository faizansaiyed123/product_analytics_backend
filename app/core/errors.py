"""Consistent API errors: {"error": {"code": ..., "message": ..., "details": ...}}."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.errors")


class APIError(Exception):
    status_code = 400
    code = "BAD_REQUEST"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.details = details
        self.headers = headers


class NotFound(APIError):
    status_code = 404
    code = "NOT_FOUND"

    def __init__(self, message: str = "Resource not found", **kw: Any) -> None:
        super().__init__(message, **kw)


class Unauthorized(APIError):
    status_code = 401
    code = "UNAUTHORIZED"

    def __init__(self, message: str = "Authentication required", **kw: Any) -> None:
        super().__init__(message, **kw)


class Forbidden(APIError):
    status_code = 403
    code = "FORBIDDEN"

    def __init__(self, message: str = "You do not have permission to do this", **kw: Any) -> None:
        super().__init__(message, **kw)


class Conflict(APIError):
    status_code = 409
    code = "CONFLICT"


class RateLimited(APIError):
    status_code = 429
    code = "RATE_LIMITED"


def error_body(code: str, message: str, details: Any = None, request_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    if request_id:
        body["request_id"] = request_id
    return {"error": body}


def _rid(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def install_error_handlers(app: FastAPI, *, debug: bool = False) -> None:
    @app.exception_handler(APIError)
    async def api_error(request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            error_body(exc.code, exc.message, exc.details, _rid(request)),
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": [str(p) for p in e.get("loc", ())], "message": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        first = errors[0] if errors else {"loc": [], "message": "Invalid request"}
        loc = ".".join(first["loc"][1:] or first["loc"])
        message = f"{loc}: {first['message']}" if loc else first["message"]
        return JSONResponse(error_body("VALIDATION_ERROR", message, errors, _rid(request)), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            401: "UNAUTHORIZED",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
            413: "PAYLOAD_TOO_LARGE",
        }.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(
            error_body(code, str(exc.detail), None, _rid(request)),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception")
        message = f"{type(exc).__name__}: {exc}" if debug else "Internal server error"
        return JSONResponse(error_body("INTERNAL_ERROR", message, None, _rid(request)), status_code=500)
