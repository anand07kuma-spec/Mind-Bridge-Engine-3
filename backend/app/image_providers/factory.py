"""Explicit selection of exactly one image provider.

The factory only reads configuration and constructs adapter objects. It never
performs network I/O, so a misconfigured or keyless image provider cannot delay
or break SVG rendering. Both the OpenAI and Gemini adapters are active; the
provider named by ``MIND_BRIDGE_IMAGE_PROVIDER`` is the only one constructed.
"""

from __future__ import annotations

from app.config import Settings
from app.image_providers.base import (
    ImageProvider,
    ImageProviderConfigurationError,
    UnavailableImageProvider,
)
from app.image_providers.gemini import GeminiImageProvider
from app.image_providers.openai import OpenAIImageProvider

SUPPORTED_PROVIDERS: tuple[str, ...] = ("none", "openai", "gemini")

_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-image-1",
    "gemini": "gemini-2.5-flash-image",
}

_API_KEY_NAMES: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def build_image_provider(settings: Settings) -> ImageProvider | None:
    """Return the single configured provider, or ``None`` when disabled.

    Unknown provider names raise :class:`ImageProviderConfigurationError` so a
    typo fails clearly at startup. A known provider without its API key still
    returns a provider object, but that object reports itself unavailable; SVG
    operation is unaffected.
    """
    name = (settings.image_provider or "none").strip().lower()
    if name in ("", "none"):
        return None

    if name not in _DEFAULT_MODELS:
        supported = ", ".join(SUPPORTED_PROVIDERS)
        raise ImageProviderConfigurationError(
            f"Unknown image provider '{name}'. Supported providers: {supported}.",
            provider=name,
        )

    model = (settings.image_model or "").strip() or _DEFAULT_MODELS[name]
    api_key = (
        settings.openai_api_key if name == "openai" else settings.gemini_api_key
    )
    if not (api_key or "").strip():
        return UnavailableImageProvider(
            name=name,
            model=model,
            reason=(
                f"{_API_KEY_NAMES[name]} is not set; AI image generation is "
                "unavailable. SVG generation is fully functional."
            ),
        )

    if name == "openai":
        return OpenAIImageProvider(
            api_key=api_key,
            model=model,
            size=settings.image_size,
            quality=settings.image_quality,
            timeout_seconds=settings.image_timeout_seconds,
            max_bytes=settings.image_max_bytes,
            prompt_max_chars=settings.image_prompt_max_chars,
        )

    return GeminiImageProvider(
        api_key=api_key,
        model=model,
        size=settings.image_size,
        quality=settings.image_quality,
        timeout_seconds=settings.image_timeout_seconds,
        max_bytes=settings.image_max_bytes,
        prompt_max_chars=settings.image_prompt_max_chars,
    )
