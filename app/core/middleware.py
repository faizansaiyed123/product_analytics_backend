"""Pure-ASGI middlewares: request id + access log + metrics, body-size limit, security headers, CORS."""

from __future__ import annotations

import logging
import time
import uuid

from prometheus_client import Counter, Histogram
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings
from app.core.logging import request_id_var

logger = logging.getLogger("app.access")

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram("http_request_duration_seconds", "HTTP latency", ["method", "route"])

PUBLIC_INGEST_PATHS = {"/api/v1/events", "/api/v1/events/batch"}


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        rid = headers.get(b"x-request-id", b"").decode()[:64] or uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid
        token = request_id_var.set(rid)
        start = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", rid.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            HTTP_REQUESTS.labels(scope["method"], route, str(status["code"])).inc()
            HTTP_LATENCY.labels(scope["method"], route).observe(elapsed)
            if route not in ("/health", "/ready", "/metrics"):
                logger.info(
                    "request",
                    extra={
                        "method": scope["method"],
                        "path": scope["path"],
                        "status": status["code"],
                        "duration_ms": round(elapsed * 1000, 1),
                    },
                )
            request_id_var.reset(token)


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = get_settings().max_body_bytes
        headers = dict(scope["headers"])
        cl = headers.get(b"content-length")
        if cl and cl.isdigit() and int(cl) > limit:
            await _json_413(send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=413, detail="Request body too large")
            return message

        await self.app(scope, limited_receive, send)


async def _json_413(send: Send) -> None:
    body = b'{"error":{"code":"PAYLOAD_TOO_LARGE","message":"Request body too large"}}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        s = get_settings()
        path = scope["path"]
        docs = path.startswith(("/docs", "/redoc"))

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                h = message.setdefault("headers", [])
                h.extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                        (b"cross-origin-resource-policy", b"cross-origin"),
                    ]
                )
                if not docs:
                    h.append((b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"))
                if s.cookie_secure:
                    h.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                if path.startswith("/api/") and not any(k == b"cache-control" for k, _ in h):
                    h.append((b"cache-control", b"no-store"))
            await send(message)

        await self.app(scope, receive, send_wrapper)


class CORSMiddleware:
    """Two policies: public ingestion endpoints (any origin, no credentials) and the dashboard API
    (allow-listed origins with credentials)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        origin = headers.get("origin")
        if not origin:
            await self.app(scope, receive, send)
            return
        method = scope["method"]
        path = scope["path"]
        preflight = method == "OPTIONS" and "access-control-request-method" in headers
        public = path in PUBLIC_INGEST_PATHS and (method in ("POST", "OPTIONS"))
        cors: list[tuple[bytes, bytes]] = []
        if public:
            cors = [
                (b"access-control-allow-origin", b"*"),
                (b"access-control-allow-methods", b"POST, OPTIONS"),
                (b"access-control-allow-headers", b"Content-Type, Authorization, X-API-Key"),
                (b"access-control-max-age", b"86400"),
            ]
        elif origin in get_settings().cors_origin_list:
            cors = [
                (b"access-control-allow-origin", origin.encode()),
                (b"access-control-allow-credentials", b"true"),
                (b"access-control-allow-methods", b"GET, POST, PUT, PATCH, DELETE, OPTIONS"),
                (b"access-control-allow-headers", b"Content-Type, X-CSRF-Token, X-Request-ID"),
                (
                    b"access-control-expose-headers",
                    b"X-Request-ID, X-RateLimit-Limit, X-RateLimit-Remaining, Retry-After",
                ),
                (b"vary", b"Origin"),
                (b"access-control-max-age", b"600"),
            ]
        if preflight:
            await send(
                {
                    "type": "http.response.start",
                    "status": 204 if cors else 403,
                    "headers": cors + [(b"content-length", b"0")],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and cors:
                message.setdefault("headers", []).extend(cors)
            await send(message)

        await self.app(scope, receive, send_wrapper)
