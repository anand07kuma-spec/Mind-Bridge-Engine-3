"""Provider-neutral validation of generated image bytes.

Only PNG, JPEG and WebP are accepted. A provider's declared MIME type is never
trusted on its own: the actual file signature decides the format, and a declared
type that disagrees with the signature is rejected. Extensions are derived from
the validated signature, never from provider output.

Dimensions are parsed with bounds-checked standard-library code for all three
formats, so no image-decoding dependency is required. The parser validates
structure and positive dimensions; it does not decode pixel data.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.image_providers.base import ImageProviderResponseError

ALLOWED_MIME_TYPES: tuple[str, ...] = ("image/png", "image/jpeg", "image/webp")

_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
}

_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


class ImageValidationError(ImageProviderResponseError):
    """Raised when provider output is empty, oversized, mismatched or malformed.

    Messages describe the validation rule only; image bytes and provider
    response bodies are never included.
    """


@dataclass(frozen=True)
class ValidatedImage:
    """Image bytes plus the format and dimensions proven by validation."""

    data: bytes
    mime_type: str
    extension: str
    width: int
    height: int


def validate_image_bytes(
    data: bytes,
    *,
    declared_mime_type: str = "",
    max_bytes: int,
    provider: str = "",
    model: str | None = None,
) -> ValidatedImage:
    """Validate image bytes and return their proven format and dimensions.

    ``declared_mime_type`` may be empty when a provider does not report a MIME
    type (the OpenAI Images API does not). In that case the signature alone
    determines the format, and the signature must still be on the allowlist.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ImageValidationError(
            "Image payload must be bytes.",
            provider=provider,
            model=model,
        )
    raw = bytes(data)
    if not raw:
        raise ImageValidationError(
            "Image payload must not be empty.",
            provider=provider,
            model=model,
        )
    if len(raw) > max_bytes:
        raise ImageValidationError(
            f"Image payload exceeds the configured maximum of {max_bytes} bytes.",
            provider=provider,
            model=model,
        )

    declared = _normalize_mime(declared_mime_type)
    if declared and declared not in _EXTENSIONS:
        raise ImageValidationError(
            "Image payload uses a MIME type that is not on the allowlist.",
            provider=provider,
            model=model,
        )

    detected = _detect_mime(raw)
    if detected is None:
        raise ImageValidationError(
            "Image payload has an unrecognized or unsupported file signature.",
            provider=provider,
            model=model,
        )
    if declared and declared != detected:
        raise ImageValidationError(
            "Declared MIME type does not match the image file signature "
            f"(declared {declared}, detected {detected}).",
            provider=provider,
            model=model,
        )

    try:
        width, height = _dimensions(raw, detected)
    except (IndexError, ValueError) as exc:
        raise ImageValidationError(
            "Image payload is malformed or truncated.",
            provider=provider,
            model=model,
        ) from exc
    if width <= 0 or height <= 0:
        raise ImageValidationError(
            "Image dimensions must be positive.",
            provider=provider,
            model=model,
        )

    return ValidatedImage(
        data=raw,
        mime_type=detected,
        extension=_EXTENSIONS[detected],
        width=width,
        height=height,
    )


def _normalize_mime(value: str) -> str:
    mime = (value or "").split(";", 1)[0].strip().lower()
    return _MIME_ALIASES.get(mime, mime)


def _detect_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _dimensions(data: bytes, mime_type: str) -> tuple[int, int]:
    if mime_type == "image/png":
        return _png_dimensions(data)
    if mime_type == "image/jpeg":
        return _jpeg_dimensions(data)
    if mime_type == "image/webp":
        return _webp_dimensions(data)
    raise ValueError("unsupported image format")


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 33:
        raise ValueError("truncated PNG")
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("bad PNG signature")
    if data[8:12] != b"\x00\x00\x00\x0d" or data[12:16] != b"IHDR":
        raise ValueError("missing PNG IHDR")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if len(data) < 12 or data[-12:-8] != b"\x00\x00\x00\x00" or data[-8:-4] != b"IEND":
        raise ValueError("missing PNG IEND")
    return width, height


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 4 or data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
        raise ValueError("truncated JPEG")

    index = 2
    while index + 4 <= len(data):
        if data[index] != 0xFF:
            raise ValueError("invalid JPEG marker")
        marker = data[index + 1]
        if marker == 0xFF:
            # Fill bytes may precede a marker.
            index += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xD9:
            break
        if marker == 0xDA:
            break
        length = int.from_bytes(data[index + 2 : index + 4], "big")
        if length < 2 or index + 2 + length > len(data):
            raise ValueError("invalid JPEG segment length")
        if marker in _JPEG_SOF_MARKERS:
            if length < 7:
                raise ValueError("truncated JPEG frame header")
            height = int.from_bytes(data[index + 5 : index + 7], "big")
            width = int.from_bytes(data[index + 7 : index + 9], "big")
            return width, height
        index += 2 + length
    raise ValueError("missing JPEG frame header")


def _webp_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("truncated WebP")
    riff_size = int.from_bytes(data[4:8], "little")
    if riff_size + 8 > len(data):
        raise ValueError("truncated WebP RIFF container")

    chunk_type = data[12:16]
    chunk_size = int.from_bytes(data[16:20], "little")
    if 20 + chunk_size > len(data):
        raise ValueError("truncated WebP chunk")
    payload = data[20 : 20 + chunk_size]

    if chunk_type == b"VP8X":
        if len(payload) < 10:
            raise ValueError("truncated WebP VP8X header")
        width = int.from_bytes(payload[4:7], "little") + 1
        height = int.from_bytes(payload[7:10], "little") + 1
    elif chunk_type == b"VP8L":
        if len(payload) < 5 or payload[0] != 0x2F:
            raise ValueError("invalid WebP VP8L header")
        bits = int.from_bytes(payload[1:5], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
    elif chunk_type == b"VP8 ":
        if len(payload) < 10 or payload[3:6] != b"\x9d\x01\x2a":
            raise ValueError("invalid WebP VP8 header")
        width = int.from_bytes(payload[6:8], "little") & 0x3FFF
        height = int.from_bytes(payload[8:10], "little") & 0x3FFF
    else:
        raise ValueError("unsupported WebP chunk")

    return width, height
