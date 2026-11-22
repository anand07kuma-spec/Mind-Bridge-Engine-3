"""Google Gemini image generation adapter.

The adapter uses the native ``generateContent`` endpoint with an injectable HTTP
transport. The API key is sent in the ``x-goog-api-key`` header, never in the URL
or query string. Responses must contain inline image data; URI responses are
never downloaded.

Failures are translated into the safe error taxonomy from
:mod:`app.image_providers.base`; messages never contain the API key, the prompt,
or a raw response body. No automatic retries are performed.

Configured model, size, quality and timeout are authoritative. Pixel sizes are
folded into a supported Gemini aspect ratio, and quality is folded into an image
size tier only when the selected model supports that tier.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import urllib.error
import urllib.parse

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

DEFAULT_GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"
DEFAULT_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

_CONTENT_POLICY_MARKERS = (
    "safety",
    "blocked",
    "blocklist",
    "prohibited",
    "content policy",
    "content_policy",
    "content filter",
    "responsible ai",
    "harmful",
    "refus",
)

_AUTH_MARKERS = (
    "api key not valid",
    "api_key_invalid",
    "invalid api key",
    "unauthenticated",
    "permission_denied",
    "permission denied",
    "does not have permission",
)

_SAFETY_FINISH_REASONS = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
    }
)

# Common configured pixel sizes mapped to the aspect ratios Gemini documents.
_SIZE_TO_ASPECT_RATIO = {
    "1024x1024": "1:1",
    "1536x1024": "3:2",
    "1024x1536": "2:3",
    "1792x1024": "16:9",
    "1024x1792": "9:16",
    "1344x768": "16:9",
    "768x1344": "9:16",
    "1248x832": "3:2",
    "832x1248": "2:3",
    "1184x864": "4:3",
    "864x1184": "3:4",
    "1536x672": "21:9",
}

_SUPPORTED_ASPECT_RATIOS = {
    (1, 1): "1:1",
    (2, 3): "2:3",
    (3, 2): "3:2",
    (3, 4): "3:4",
    (4, 3): "4:3",
    (9, 16): "9:16",
    (16, 9): "16:9",
    (21, 9): "21:9",
}

_QUALITY_TO_IMAGE_SIZE = {
    "low": "512",
    "standard": "1K",
    "medium": "2K",
    "high": "2K",
    "hd": "2K",
    "ultra": "4K",
    "512": "512",
    "1k": "1K",
    "2k": "2K",
    "4k": "4K",
    "auto": "1K",
}


class GeminiImageProvider(ImageProvider):
    """Gemini ``generateContent`` adapter with a deterministic transport."""

    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_GEMINI_IMAGE_MODEL,
        size: str = "1024x1024",
        quality: str = "standard",
        timeout_seconds: float = 60.0,
        max_bytes: int = 20_971_520,
        prompt_max_chars: int = 4_000,
        base_url: str = DEFAULT_GEMINI_API_BASE,
        transport: HttpTransport | None = None,
    ) -> None:
        if not (api_key or "").strip():
            raise ImageProviderConfigurationError(
                "GEMINI_API_KEY is required for the Gemini image provider.",
                provider=self.name,
                model=model or None,
            )
        if not (model or "").strip():
            raise ImageProviderConfigurationError(
                "A Gemini image model must be configured.",
                provider=self.name,
            )
        if not (size or "").strip():
            raise ImageProviderConfigurationError(
                "A Gemini image size must be configured.",
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
                "A Gemini image quality must be configured.",
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
        if not (base_url or "").strip():
            raise ImageProviderConfigurationError(
                "A Gemini API base URL must be configured.",
                provider=self.name,
                model=model,
            )

        self._api_key = api_key.strip()
        self.model = model.strip().removeprefix("models/")
        self._size = size.strip()
        self._quality = quality.strip()
        self._timeout_seconds = float(timeout_seconds)
        self._max_bytes = int(max_bytes)
        self._prompt_max_chars = int(prompt_max_chars)
        self._base_url = base_url.strip()
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
                "Gemini generateContent adapter; inline images only, "
                "no URI downloads."
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
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = (
            f"{self._base_url.rstrip('/')}/"
            f"{urllib.parse.quote(self.model, safe='.-_')}:generateContent"
        )

        try:
            response = self._transport.post(
                url,
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
        image_config: dict[str, str] = {}
        aspect_ratio = _aspect_ratio(self._size)
        if aspect_ratio:
            image_config["aspectRatio"] = aspect_ratio
        image_size = _image_size_for_model(self.model, self._quality)
        if image_size:
            image_config["imageSize"] = image_size

        generation_config: dict[str, object] = {
            "responseModalities": ["TEXT", "IMAGE"],
        }
        if image_config:
            generation_config["imageConfig"] = image_config

        return {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": generation_config,
        }

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
                retry_after=_retry_after(response),
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
            if _looks_like_auth(response.body):
                raise ImageProviderAuthError(
                    "The image provider rejected its credentials.",
                    provider=self.name,
                    model=self.model,
                )
            if _looks_like_safety(response.body):
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

        prompt_feedback = document.get("promptFeedback")
        if isinstance(prompt_feedback, dict):
            block_reason = prompt_feedback.get("blockReason")
            if isinstance(block_reason, str) and block_reason.strip():
                raise ImageProviderContentPolicyError(
                    "The image provider blocked the request.",
                    provider=self.name,
                    model=self.model,
                )

        candidates = document.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ImageProviderResponseError(
                "The image provider returned no candidates.",
                provider=self.name,
                model=self.model,
            )
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise ImageProviderResponseError(
                "The image provider returned an unexpected candidate.",
                provider=self.name,
                model=self.model,
            )

        finish_reason = candidate.get("finishReason")
        if isinstance(finish_reason, str) and finish_reason.upper() in _SAFETY_FINISH_REASONS:
            raise ImageProviderContentPolicyError(
                "The image provider blocked the request.",
                provider=self.name,
                model=self.model,
            )

        content = candidate.get("content")
        if not isinstance(content, dict):
            raise ImageProviderResponseError(
                "The image provider returned no content.",
                provider=self.name,
                model=self.model,
            )
        parts = content.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ImageProviderResponseError(
                "The image provider returned no content parts.",
                provider=self.name,
                model=self.model,
            )

        image_part = _find_inline_image(parts)
        if image_part is None:
            if _has_file_data(parts):
                raise ImageProviderResponseError(
                    "The image provider returned a URI; URI downloads are disabled.",
                    provider=self.name,
                    model=self.model,
                )
            raise ImageProviderResponseError(
                "The image provider returned no inline image data.",
                provider=self.name,
                model=self.model,
            )

        mime_type, encoded = image_part
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
            declared_mime_type=mime_type,
            max_bytes=self._max_bytes,
            provider=self.name,
            model=self.model,
        )

        request_id = document.get("responseId") or document.get("id")
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
            revised_prompt=None,
            provider_request_id=request_id,
            metadata=_safe_metadata(
                request,
                self._size,
                self._quality,
                _aspect_ratio(self._size),
                _image_size_for_model(self.model, self._quality),
            ),
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


def _aspect_ratio(size: str) -> str | None:
    normalized = size.strip().lower().replace(" ", "")
    if normalized in _SIZE_TO_ASPECT_RATIO:
        return _SIZE_TO_ASPECT_RATIO[normalized]
    parsed = _parse_size(normalized)
    if parsed is None:
        return None
    width, height = parsed
    divisor = math.gcd(width, height)
    return _SUPPORTED_ASPECT_RATIOS.get((width // divisor, height // divisor))


def _supported_image_sizes(model: str) -> tuple[str, ...]:
    normalized = model.lower()
    if "3.1" in normalized or "3-1" in normalized or "flash-lite" in normalized:
        return ("512", "1K", "2K", "4K")
    if "gemini-3" in normalized or "nano-banana" in normalized:
        return ("1K", "2K", "4K")
    return ("1K",)


def _image_size_for_model(model: str, quality: str) -> str:
    requested = _QUALITY_TO_IMAGE_SIZE.get(quality.strip().lower(), "1K")
    supported = _supported_image_sizes(model)
    if requested in supported:
        return requested
    if "1K" in supported:
        return "1K"
    return supported[0]


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


def _find_inline_image(parts: list[object]) -> tuple[str, str] | None:
    found: tuple[str, str] | None = None
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("thought") is True:
            continue
        inline = part.get("inlineData")
        if not isinstance(inline, dict):
            inline = part.get("inline_data")
        if not isinstance(inline, dict):
            continue
        data = inline.get("data")
        if not isinstance(data, str) or not data.strip():
            continue
        mime_type = inline.get("mimeType") or inline.get("mime_type") or ""
        if not isinstance(mime_type, str):
            mime_type = ""
        # Gemini can emit intermediate images; the last non-thought image is
        # the final result.
        found = (mime_type, data)
    return found


def _has_file_data(parts: list[object]) -> bool:
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("fileData") or part.get("file_data"):
            return True
    return False


def _safe_metadata(
    request: ImageGenerationRequest,
    size: str,
    quality: str,
    aspect_ratio: str | None,
    image_size: str | None,
) -> dict[str, object]:
    metadata: dict[str, object] = {"size": size, "quality": quality}
    if aspect_ratio:
        metadata["aspect_ratio"] = aspect_ratio
    if image_size:
        metadata["image_size"] = image_size
    if not isinstance(request.metadata, dict):
        return metadata
    for key in ("prompt_hash", "prompt_template_version"):
        value = request.metadata.get(key)
        if isinstance(value, str) and value:
            metadata[key] = value
    return metadata


def _looks_like_safety(body: bytes) -> bool:
    if not body:
        return False
    text = body[:8192].decode("utf-8", errors="ignore").lower()
    return any(marker in text for marker in _CONTENT_POLICY_MARKERS)


def _looks_like_auth(body: bytes) -> bool:
    if not body:
        return False
    text = body[:8192].decode("utf-8", errors="ignore").lower()
    return any(marker in text for marker in _AUTH_MARKERS)


def _retry_after(response: HttpResponse) -> float | None:
    header_value = parse_retry_after(response.headers)
    if header_value is not None:
        return header_value
    return _retry_delay_from_body(response.body)


def _retry_delay_from_body(body: bytes) -> float | None:
    if not body:
        return None
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    error = document.get("error")
    if not isinstance(error, dict):
        return None
    details = error.get("details")
    if not isinstance(details, list):
        return None
    for detail in details:
        if not isinstance(detail, dict):
            continue
        delay = detail.get("retryDelay") or detail.get("retry_delay")
        if not isinstance(delay, str) or not delay.endswith("s"):
            continue
        try:
            seconds = float(delay[:-1])
        except ValueError:
            continue
        return seconds if seconds >= 0 else None
    return None
