"""Provider-independent interface for AI image generation.

Nothing in the application depends on a specific vendor. A provider adapter only
has to implement :class:`ImageProvider`; scene orchestration selects exactly one
configured provider and never imports vendor-specific code.

Reference-image workflows remain representable through
``ImageGenerationRequest.reference_image_paths`` and
``ProviderCapabilities.supports_reference_images``, but no adapter is active in
this phase.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ImageGenerationRequest:
    """Normalized input passed to every image provider."""

    prompt: str
    scene_id: str
    revision: int = 0
    width: int = 1024
    height: int = 1024
    quality: str = "standard"
    style: str | None = None
    reference_image_paths: tuple[Path, ...] = ()
    timeout_seconds: float = 60.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("prompt must not be empty")
        if not self.scene_id.strip():
            raise ValueError("scene_id must not be empty")
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("width and height must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not self.quality.strip():
            raise ValueError("quality must not be empty")


@dataclass(frozen=True)
class GeneratedImage:
    """Normalized provider output; persistence belongs to the asset manager."""

    data: bytes
    mime_type: str
    extension: str
    width: int | None = None
    height: int | None = None
    thumbnail: bytes | None = None
    provider: str = ""
    model: str = ""
    revised_prompt: str | None = None
    provider_request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("image data must not be empty")
        mime_type = self.mime_type.strip().lower()
        if not mime_type.startswith("image/"):
            raise ValueError("mime_type must be an image/* media type")
        extension = self.extension.strip().lstrip(".").lower()
        if not extension:
            raise ValueError("extension must not be empty")
        if self.width is not None and self.width <= 0:
            raise ValueError("width must be positive when provided")
        if self.height is not None and self.height <= 0:
            raise ValueError("height must be positive when provided")
        if self.thumbnail is not None and not self.thumbnail:
            raise ValueError("thumbnail must not be empty when provided")
        object.__setattr__(self, "mime_type", mime_type)
        object.__setattr__(self, "extension", extension)


@dataclass(frozen=True)
class ProviderCapabilities:
    """Static description of a provider; constructing it performs no I/O."""

    name: str
    available: bool
    models: tuple[str, ...] = ()
    default_model: str = ""
    supported_sizes: tuple[str, ...] = ()
    supports_quality: bool = False
    supports_reference_images: bool = False
    max_width: int = 1024
    max_height: int = 1024
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("provider name must not be empty")
        if self.max_width <= 0 or self.max_height <= 0:
            raise ValueError("max_width and max_height must be positive")


class ImageProviderError(RuntimeError):
    """Base class for safe, provider-agnostic image generation failures.

    Messages must never contain API keys, prompts, or raw provider response
    bodies. Operational metadata is exposed separately for logging and retry
    decisions.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        model: str | None = None,
        retryable: bool = False,
        provider_request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.retryable = retryable
        self.provider_request_id = provider_request_id
        self.retry_after = retry_after


class ImageProviderConfigurationError(ImageProviderError):
    """Raised when provider configuration is unknown or invalid."""


class ImageProviderNotConfiguredError(ImageProviderError):
    """Raised when AI image generation was requested without an active provider."""


class ImageProviderAuthError(ImageProviderError):
    """Raised when the configured provider rejects its credentials."""


class ImageProviderRateLimitError(ImageProviderError):
    """Raised when the provider asks the caller to slow down."""


class ImageProviderTimeoutError(ImageProviderError):
    """Raised when the provider does not answer within the configured timeout."""


class ImageProviderContentPolicyError(ImageProviderError):
    """Raised when the provider blocks the prompt or the generated result."""


class ImageProviderResponseError(ImageProviderError):
    """Raised when the provider returns an unusable or malformed response."""


class ImageProviderUnavailableError(ImageProviderError):
    """Raised when the provider is temporarily unavailable."""


class ImageProvider(ABC):
    """Contract every image-generation backend must satisfy."""

    name: str = "abstract"
    model: str = ""

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Describe the provider without performing any network call."""

    @abstractmethod
    def generate(self, request: ImageGenerationRequest) -> GeneratedImage:
        """Produce raw image bytes; persistence belongs to the asset manager."""


class UnavailableImageProvider(ImageProvider):
    """Explicit placeholder that fails loudly instead of faking output."""

    def __init__(
        self,
        name: str = "unavailable",
        model: str = "",
        reason: str | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.reason = reason or (
            "No image provider configured. SVG generation is fully functional."
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            available=False,
            models=(self.model,) if self.model else (),
            default_model=self.model,
            notes=self.reason,
        )

    def generate(self, request: ImageGenerationRequest) -> GeneratedImage:
        raise ImageProviderNotConfiguredError(
            self.reason,
            provider=self.name,
            model=self.model or None,
        )


_REGISTRY: dict[str, ImageProvider] = {}


def register_provider(provider: ImageProvider) -> None:
    _REGISTRY[provider.name] = provider


def get_provider(name: str) -> ImageProvider:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ImageProviderConfigurationError(
            f"Unknown image provider '{name}'",
            provider=name,
        ) from exc


def available_providers() -> list[str]:
    return sorted(_REGISTRY)
