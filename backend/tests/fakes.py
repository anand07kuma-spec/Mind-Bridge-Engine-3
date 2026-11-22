"""Deterministic, offline test doubles for image provider tests."""

from __future__ import annotations

import base64
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.image_providers.base import (
    GeneratedImage,
    ImageGenerationRequest,
    ImageProvider,
    ProviderCapabilities,
)
from app.image_providers.http import HttpResponse

# A valid 1x1 transparent PNG. Deterministic and tiny, so tests never need an
# external network call or an image-processing dependency.
_TRANSPARENT_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)


class FakeImageProvider(ImageProvider):
    """In-memory provider used by unit and future pipeline tests."""

    def __init__(
        self,
        *,
        name: str = "fake",
        model: str = "fake-image-1",
        data: bytes | None = None,
        error: Exception | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self._data = data if data is not None else _TRANSPARENT_PNG
        self._error = error
        self._metadata = dict(metadata or {})
        self.calls: list[ImageGenerationRequest] = []
        self._call_lock = threading.Lock()

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            available=True,
            models=(self.model,),
            default_model=self.model,
            supported_sizes=("1024x1024",),
            supports_quality=True,
            max_width=1024,
            max_height=1024,
            notes="Deterministic test provider; never performs network calls.",
        )

    def generate(self, request: ImageGenerationRequest) -> GeneratedImage:
        with self._call_lock:
            self.calls.append(request)
        if self._error is not None:
            raise self._error
        return GeneratedImage(
            data=self._data,
            mime_type="image/png",
            extension="png",
            width=1,
            height=1,
            provider=self.name,
            model=self.model,
            metadata={"fake": True, **self._metadata},
        )


@dataclass
class RecordedHttpCall:
    url: str
    headers: Mapping[str, str]
    body: bytes
    timeout: float


@dataclass
class FakeHttpTransport:
    """Deterministic HTTP transport for image provider adapter tests."""

    response: HttpResponse | None = None
    error: BaseException | None = None
    calls: list[RecordedHttpCall] = field(default_factory=list)

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> HttpResponse:
        self.calls.append(
            RecordedHttpCall(
                url=url,
                headers=dict(headers),
                body=body,
                timeout=timeout,
            )
        )
        if self.error is not None:
            raise self.error
        if self.response is None:
            raise AssertionError("FakeHttpTransport has no response configured")
        return self.response
