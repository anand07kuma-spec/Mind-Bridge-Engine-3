"""Structured logging and request correlation for Engine 3.

Logs are JSON objects on stdout. They intentionally carry only operational
metadata (request id, method, path, status, scene id, revision, cache hit and
warning counts) — never request bodies, instruction text, titles, labels or
credentials.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

LOGGER_NAME = "mindbridge.engine3"
API_LOGGER_NAME = f"{LOGGER_NAME}.api"

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_STRUCTURED_FIELDS = (
    "method",
    "path",
    "status",
    "duration_ms",
    "scene_id",
    "revision",
    "cache_hit",
    "warning_count",
)


def current_request_id() -> str:
    return request_id_var.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "request_id": getattr(record, "request_id", current_request_id()),
        }
        for key in _STRUCTURED_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    """Attach one JSON handler to the Engine 3 logger (idempotent)."""
    global _configured
    if _configured:
        return
    logger = logging.getLogger(LOGGER_NAME)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = True
    # The access middleware already emits a structured request event; suppress
    # uvicorn's duplicate plain-text access line.
    logging.getLogger("uvicorn.access").disabled = True
    _configured = True
