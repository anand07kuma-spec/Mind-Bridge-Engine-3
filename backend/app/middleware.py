"""ASGI middleware: request correlation, access logging and body limits."""

from __future__ import annotations

import json
import logging
import re
import time
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability import LOGGER_NAME, request_id_var

REQUEST_ID_HEADER = b"x-request-id"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

logger = logging.getLogger(LOGGER_NAME)


def _header(scope: Scope, name: str) -> str | None:
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == target:
            return value.decode("latin-1")
    return None


class RequestContextMiddleware:
    """Accept or generate ``X-Request-ID``, echo it and log one access event."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = _header(scope, "x-request-id")
        request_id = incoming if incoming and _REQUEST_ID_RE.fullmatch(incoming) else uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500
        failure: Exception | None = None

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != REQUEST_ID_HEADER
                ]
                headers.append((REQUEST_ID_HEADER, request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception as exc:  # re-raised after logging
            failure = exc
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            extra = {
                "request_id": request_id,
                "method": scope.get("method"),
                "path": scope.get("path"),
                "duration_ms": duration_ms,
            }
            if failure is not None:
                logger.error("http.unhandled", exc_info=failure, extra={**extra, "status": 500})
            else:
                event = "http.error" if status_code >= 400 else "http.request"
                level = logging.ERROR if status_code >= 500 else logging.WARNING if status_code >= 400 else logging.INFO
                logger.log(level, event, extra={**extra, "status": status_code})
            request_id_var.reset(token)


class BodySizeLimitMiddleware:
    """Reject request bodies larger than ``max_bytes`` with HTTP 413.

    The body is buffered so chunked requests are measured too; Engine 3 only
    accepts small JSON payloads, so this keeps parsing bounded.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _header(scope, "content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await self._reject(send)
                    return
            except ValueError:
                pass

        body = bytearray()
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await self._reject(send)
                return
            more_body = bool(message.get("more_body", False))

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            # Delegate so downstream disconnect listeners keep the real
            # blocking semantics of ``receive`` instead of seeing an instant
            # disconnect.
            return await receive()

        await self.app(scope, replay, send)

    async def _reject(self, send: Send) -> None:
        payload = json.dumps(
            {"detail": f"Request body exceeds the {self.max_bytes}-byte limit."}
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
