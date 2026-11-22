"""OpenAI Images API adapter.

The adapter speaks the public HTTP API directly through an injectable transport,
so tests never touch the network. It requests base64 image data and never
downloads provider-supplied URLs. Failures are translated into the safe error
taxonomy from :mod:`app.image_providers.base`; messages never contain the API
key, the prompt, or a raw response body.

No automatic retries are performed. ``MIND_BRIDGE_IMAGE_MAX_RETRIES`` is
reserved for a later phase and is intentionally ignored here.

The configured model, size, quality and timeout are authoritative. The
provider-neutral request fields are hints for adapters that need them; this
adapter uses its construction-time configuration so one server-side setting
governs every paid request.
"""

from __future__ import annotations

import base64
import binascii
import json
import urllib.error
from collections.abc import Mapping

from app.image_providers.base import (
    GeneratedImage,
    ImageGenerationRequest,
    ImageProvider,
    ImageProviderAuthError,
    ImageProviderConfigurationError,
    ImageProviderContentPolicyError,
    ImageProviderError,
    ImageProviderRateLimitError,
    ImageProviderResponseError,
    ImageProviderTimeoutError,
    ImageProviderUnavailableError,
    ProviderCapabilities,
)
from app.image_providers.http import (
    HttpResponse,
    HttpTransport,
    UrllibHttpTransport,
    parse_retry_after,
)
from app.image_providers.validation import validate_image_bytes

DEFAULT_OPENAI_IMAGE_URL = "https://api.openai.com/v1/images/generations"
DEFAULT_OPENAI_IMAGE_MODEL = "gpt-image-1"

_CONTENT_POLICY_MARKERS = (
    "content policy",
    "content_policy",
    "safety",
    "moderation",
    "blocked",
    "violat",
)


class OpenAIImageProvider(ImageProvider):
    """OpenAI Images API adapter with a deterministic, injectable transport."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_OPENAI_IMAGE_MODEL,
        size: str = "1024x1024",
        quality: str = "standard",
        timeout_seconds: float = 60.0,
        max_bytes: int = 20_971_520,
        prompt_max_chars: int = 4_000,
        base_url: str = DEFAULT_OPENAI_IMAGE_URL,
        transport: HttpTransport | None = None,
    ) -> None:
        if not (api_key or "").strip():
            raise ImageProviderConfigurationError(
                "OPENAI_API_KEY is required for the OpenAI image provider.",
                provider=self.name,
                model=model or None,
            )
        if not (model or "").strip():
            raise ImageProviderConfigurationError(
                "An OpenAI image model must be configured.",
                provider=self.name,
            )
        if not (size or "").strip():
            raise ImageProviderConfigurationError(
                "An OpenAI image size must be configured.",
                provider=self.name,
                model=model,
            )
        if size.strip().lower() != "auto" and _parse_size(size) is None:
            raise ImageProviderConfigurationError(
                "MIND_BRIDGE_IMAGE_SIZE must be WIDTHxHEIGHT or 'auto'.",
                provider=self.name,
                model=model,
            )
        if not (quality or "").strip():
            raise ImageProviderConfigurationError(
                "An OpenAI image quality must be configured.",
                provider=self.name,
                model=model,
            )
        if timeout_seconds <= 0:
            raise ImageProviderConfigurationError(
                "Image timeout must be positive.",
                provider=self.name,
                model=model,
            )
        if max_bytes <= 0:
            raise ImageProviderConfigurationError(
                "Image maximum bytes must be positive.",
                provider=self.name,
                model=model,
            )
        if prompt_max_chars <= 0:
            raise ImageProviderConfigurationError(
                "Image prompt maximum characters must be positive.",
                provider=self.name,
                model=model,
            )

        self._api_key = api_key.strip()
        self.model = model.strip()
        self._size = size.strip()
        self._quality = quality.strip()
        self._timeout_seconds = float(timeout_seconds)
        self._max_bytes = int(max_bytes)
        self._prompt_max_chars = int(prompt_max_chars)
        self._base_url = base_url
        self._transport: HttpTransport = transport or UrllibHttpTransport()

    @property
    def size(self) -> str:
        return self._size

    @property
    def quality(self) -> str:
        return self._quality

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def prompt_max_chars(self) -> int:
        return self._prompt_max_chars

    def capabilities(self) -> ProviderCapabilities:
        width, height = _parse_size(self._size) or (1024, 1024)
        return ProviderCapabilities(
            name=self.name,
            available=True,
            models=(self.model,),
            default_model=self.model,
            supported_sizes=(self._size,),
            supports_quality=True,
            supports_reference_images=False,
            max_width=width,
            max_height=height,
            notes=(
                "OpenAI Images API adapter; base64 responses only, "
                "no URL downloads."
            ),
        )

    def generate(self, request: ImageGenerationRequest) -> GeneratedImage:
        if len(request.prompt) > self._prompt_max_chars:
            raise ImageProviderResponseError(
                "The image prompt exceeds the configured maximum length.",
                provider=self.name,
                model=self.model,
            )

        payload = self._build_payload(request.prompt)
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            response = self._transport.post(
                self._base_url,
                headers=headers,
                body=body,
                timeout=self._timeout_seconds,
            )
        except ImageProviderError:
            raise
        except TimeoutError as exc:
            raise ImageProviderTimeoutError(
                "The image provider did not respond within the configured timeout.",
                provider=self.name,
                model=self.model,
                retryable=True,
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise ImageProviderTimeoutError(
                    "The image provider did not respond within the configured timeout.",
                    provider=self.name,
                    model=self.model,
                    retryable=True,
                ) from exc
            raise ImageProviderUnavailableError(
                "The image provider could not be reached.",
                provider=self.name,
                model=self.model,
                retryable=True,
            ) from exc
        except OSError as exc:
            raise ImageProviderUnavailableError(
                "The image provider could not be reached.",
                provider=self.name,
                model=self.model,
                retryable=True,
            ) from exc

        return self._parse_response(response, request)

    def _build_payload(self, prompt: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self.model,
            "prompt": prompt,
            "size": self._size,
            "quality": self._quality,
            "n": 1,
        }
        # DALL-E models default to URL responses; gpt-image-1 always returns
        # base64 and rejects the response_format parameter.
        if self.model.lower().startswith("dall-e"):
            payload["response_format"] = "b64_json"
        return payload

    def _parse_response(
        self,
        response: HttpResponse,
        request: ImageGenerationRequest,
    ) -> GeneratedImage:
        status = response.status_code

        if status in (401, 403):
            raise ImageProviderAuthError(
                "The image provider rejected its credentials.",
                provider=self.name,
                model=self.model,
            )
        if status == 429:
            raise ImageProviderRateLimitError(
                "The image provider asked us to slow down.",
                provider=self.name,
                model=self.model,
                retryable=True,
                retry_after=parse_retry_after(response.headers),
            )
        if status in (408, 504):
            raise ImageProviderTimeoutError(
                "The image provider did not respond within the configured timeout.",
                provider=self.name,
                model=self.model,
                retryable=True,
            )
        if status >= 500:
            raise ImageProviderUnavailableError(
                "The image provider is temporarily unavailable.",
                provider=self.name,
                model=self.model,
                retryable=True,
            )
        if status < 200 or status >= 300:
            if _looks_like_content_policy(response.body):
                raise ImageProviderContentPolicyError(
                    "The image provider blocked the request.",
                    provider=self.name,
                    model=self.model,
                )
            raise ImageProviderResponseError(
                f"The image provider returned an error (HTTP {status}).",
                provider=self.name,
                model=self.model,
            )

        document = _decode_json(response.body, self.name, self.model)
        data = document.get("data")
        if not isinstance(data, list) or not data:
            raise ImageProviderResponseError(
                "The image provider returned no image data.",
                provider=self.name,
                model=self.model,
            )
        item = data[0]
        if not isinstance(item, dict):
            raise ImageProviderResponseError(
                "The image provider returned an unexpected image entry.",
                provider=self.name,
                model=self.model,
            )

        encoded = item.get("b64_json")
        if not isinstance(encoded, str) or not encoded.strip():
            if "url" in item:
                raise ImageProviderResponseError(
                    "The image provider returned a URL; URL downloads are disabled.",
                    provider=self.name,
                    model=self.model,
                )
            raise ImageProviderResponseError(
                "The image provider returned no base64 image data.",
                provider=self.name,
                model=self.model,
            )

        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageProviderResponseError(
                "The image provider returned invalid base64 image data.",
                provider=self.name,
                model=self.model,
            ) from exc

        validated = validate_image_bytes(
            raw,
            declared_mime_type=_declared_mime(document, item),
            max_bytes=self._max_bytes,
            provider=self.name,
            model=self.model,
        )

        request_id = document.get("id")
        if not isinstance(request_id, str) or not request_id.strip():
            request_id = None

        return GeneratedImage(
            data=validated.data,
            mime_type=validated.mime_type,
            extension=validated.extension,
            width=validated.width,
            height=validated.height,
            provider=self.name,
            model=self.model,
            # Provider-revised prompts can contain the original prompt, so they
            # are intentionally not propagated into persistent metadata.
            revised_prompt=None,
            provider_request_id=request_id,
            metadata=_safe_metadata(request, self._size, self._quality),
        )


def _parse_size(value: str) -> tuple[int, int] | None:
    parts = value.strip().lower().split("x")
    if len(parts) != 2:
        return None
    try:
        width = int(parts[0])
        height = int(parts[1])
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _decode_json(
    body: bytes,
    provider: str,
    model: str,
) -> dict[str, object]:
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImageProviderResponseError(
            "The image provider returned malformed JSON.",
            provider=provider,
            model=model,
        ) from exc
    if not isinstance(document, dict):
        raise ImageProviderResponseError(
            "The image provider returned an unexpected response shape.",
            provider=provider,
            model=model,
        )
    return document


def _declared_mime(
    document: Mapping[str, object],
    item: Mapping[str, object],
) -> str:
    output_format = item.get("output_format") or document.get("output_format")
    if not isinstance(output_format, str):
        return ""
    normalized = output_format.strip().lower()
    if normalized in {"jpg", "jpeg"}:
        return "image/jpeg"
    if normalized in {"png", "webp"}:
        return f"image/{normalized}"
    return ""


def _safe_metadata(
    request: ImageGenerationRequest,
    size: str,
    quality: str,
) -> dict[str, object]:
    metadata: dict[str, object] = {"size": size, "quality": quality}
    if not isinstance(request.metadata, dict):
        return metadata
    for key in ("prompt_hash", "prompt_template_version"):
        value = request.metadata.get(key)
        if isinstance(value, str) and value:
            metadata[key] = value
    return metadata


def _looks_like_content_policy(body: bytes) -> bool:
    if not body:
        return False
    text = body[:8192].decode("utf-8", errors="ignore").lower()
    return any(marker in text for marker in _CONTENT_POLICY_MARKERS)
