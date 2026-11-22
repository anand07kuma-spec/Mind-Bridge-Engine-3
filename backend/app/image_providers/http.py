"""Shared HTTP transport for image provider adapters.

Adapters receive an injectable transport, so construction never performs I/O and
tests can supply deterministic responses. This module contains no provider
credentials, prompt construction, or response parsing.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from app.image_providers.base import ImageProviderResponseError

MAX_HTTP_RESPONSE_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class HttpResponse:
    """Minimal transport response used by adapters and test doubles."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    """Injectable HTTP transport; construction must never perform I/O."""

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> HttpResponse: ...


class UrllibHttpTransport:
    """Standard-library transport that only performs I/O inside ``post``."""

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url,
            data=body,
            headers=dict(headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
                if len(payload) > MAX_HTTP_RESPONSE_BYTES:
                    raise ImageProviderResponseError(
                        "Image provider response exceeded the transport limit."
                    )
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                return HttpResponse(
                    status_code=int(status),
                    headers={key: value for key, value in response.headers.items()},
                    body=payload,
                )
        except urllib.error.HTTPError as exc:
            payload = exc.read(MAX_HTTP_RESPONSE_BYTES + 1)
            if len(payload) > MAX_HTTP_RESPONSE_BYTES:
                payload = b""
            return HttpResponse(
                status_code=int(exc.code),
                headers={key: value for key, value in (exc.headers or {}).items()},
                body=payload,
            )


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Return a numeric Retry-After value when the header is present."""
    for key, value in headers.items():
        if key.lower() != "retry-after":
            continue
        try:
            seconds = float(value.strip())
        except (TypeError, ValueError):
            return None
        return seconds if seconds >= 0 else None
    return None
