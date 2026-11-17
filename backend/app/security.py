"""Bearer-token auth as a pure ASGI middleware (SDD 4.3).

Every request needs `Authorization: Bearer <SS_TOKEN>` except GET /health. Streaming endpoints
(`.../events`) also accept `?token=` because EventSource cannot send headers.
"""

from __future__ import annotations

import hmac
import json
from urllib.parse import parse_qs

from app.config import API_PREFIX
from app.errors import error_body


class TokenAuthMiddleware:
    def __init__(self, app, token: str, exempt_paths: set[str] | None = None):
        self.app = app
        self._token = token.encode("utf-8")
        self.exempt = {f"{API_PREFIX}/health"} | (exempt_paths or set())

    def _valid(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        return hmac.compare_digest(candidate.encode("utf-8"), self._token)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        method = scope.get("method", "GET")
        path = scope.get("path", "")
        if method == "OPTIONS" or path in self.exempt:
            return await self.app(scope, receive, send)

        token = None
        for key, value in scope.get("headers", []):
            if key == b"authorization":
                raw = value.decode("latin-1")
                if raw[:7].lower() == "bearer ":
                    token = raw[7:].strip()
                break
        if token is None and (path.endswith("/events") or scope["type"] == "websocket"):
            qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            token = (qs.get("token") or [None])[0]

        if self._valid(token):
            return await self.app(scope, receive, send)

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        body = json.dumps(error_body("UNAUTHORIZED", "Missing or invalid token")).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
