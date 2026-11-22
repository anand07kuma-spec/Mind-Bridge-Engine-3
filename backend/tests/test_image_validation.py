from __future__ import annotations

import base64

import pytest
from app.image_providers.base import ImageProviderResponseError
from app.image_providers.validation import (
    ALLOWED_MIME_TYPES,
    ImageValidationError,
    validate_image_bytes,
)

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)

JPEG_1X1 = bytes.fromhex(
    "ffd8"
    "ffe000104a46494600010100000100010000"
    "ffc00011080001000103011100021101031101"
    "ffd9"
)

WEBP_1X1 = (
    b"RIFF"
    + (22).to_bytes(4, "little")
    + b"WEBP"
    + b"VP8X"
    + (10).to_bytes(4, "little")
    + b"\x00"
    + b"\x00\x00\x00"
    + b"\x00\x00\x00"
    + b"\x00\x00\x00"
)

WEBP_VP8L_1X1 = (
    b"RIFF"
    + (17).to_bytes(4, "little")
    + b"WEBP"
    + b"VP8L"
    + (5).to_bytes(4, "little")
    + b"\x2f\x00\x00\x00\x00"
)


def test_allowed_mime_allowlist_is_explicit():
    assert ALLOWED_MIME_TYPES == ("image/png", "image/jpeg", "image/webp")


def test_valid_png_is_accepted():
    image = validate_image_bytes(
        PNG_1X1,
        declared_mime_type="image/png",
        max_bytes=1_000_000,
    )

    assert image.mime_type == "image/png"
    assert image.extension == "png"
    assert image.width == 1
    assert image.height == 1
    assert image.data == PNG_1X1


def test_valid_jpeg_is_accepted():
    image = validate_image_bytes(
        JPEG_1X1,
        declared_mime_type="image/jpeg",
        max_bytes=1_000_000,
    )

    assert image.mime_type == "image/jpeg"
    assert image.extension == "jpg"
    assert image.width == 1
    assert image.height == 1


def test_valid_webp_is_accepted():
    image = validate_image_bytes(
        WEBP_1X1,
        declared_mime_type="image/webp",
        max_bytes=1_000_000,
    )

    assert image.mime_type == "image/webp"
    assert image.extension == "webp"
    assert image.width == 1
    assert image.height == 1


def test_valid_webp_vp8l_is_accepted():
    image = validate_image_bytes(
        WEBP_VP8L_1X1,
        declared_mime_type="image/webp",
        max_bytes=1_000_000,
    )

    assert image.mime_type == "image/webp"
    assert image.width == 1
    assert image.height == 1


def test_mime_parameters_and_case_are_normalized():
    image = validate_image_bytes(
        PNG_1X1,
        declared_mime_type=" IMAGE/PNG; charset=binary ",
        max_bytes=1_000_000,
    )

    assert image.mime_type == "image/png"


def test_jpg_alias_is_normalized():
    image = validate_image_bytes(
        JPEG_1X1,
        declared_mime_type="image/jpg",
        max_bytes=1_000_000,
    )

    assert image.mime_type == "image/jpeg"
    assert image.extension == "jpg"


def test_extension_is_derived_from_signature():
    image = validate_image_bytes(
        PNG_1X1,
        declared_mime_type="image/png",
        max_bytes=1_000_000,
    )

    assert image.extension == "png"


def test_empty_declared_mime_uses_signature():
    image = validate_image_bytes(
        WEBP_1X1,
        declared_mime_type="",
        max_bytes=1_000_000,
    )

    assert image.mime_type == "image/webp"
    assert image.extension == "webp"


def test_mismatched_mime_and_signature_is_rejected():
    with pytest.raises(ImageValidationError, match="does not match"):
        validate_image_bytes(
            PNG_1X1,
            declared_mime_type="image/jpeg",
            max_bytes=1_000_000,
        )


def test_unsupported_mime_is_rejected():
    with pytest.raises(ImageValidationError, match="allowlist"):
        validate_image_bytes(
            b"GIF89a" + b"\x00" * 32,
            declared_mime_type="image/gif",
            max_bytes=1_000_000,
        )


def test_unknown_signature_is_rejected():
    with pytest.raises(ImageValidationError, match="signature"):
        validate_image_bytes(
            b"not an image",
            declared_mime_type="image/png",
            max_bytes=1_000_000,
        )


def test_empty_bytes_are_rejected():
    with pytest.raises(ImageValidationError, match="empty"):
        validate_image_bytes(
            b"",
            declared_mime_type="image/png",
            max_bytes=1_000_000,
        )


def test_oversized_bytes_are_rejected():
    with pytest.raises(ImageValidationError, match="maximum"):
        validate_image_bytes(
            PNG_1X1,
            declared_mime_type="image/png",
            max_bytes=len(PNG_1X1) - 1,
        )


def test_malformed_png_is_rejected():
    with pytest.raises(ImageValidationError, match="malformed"):
        validate_image_bytes(
            PNG_1X1[:20],
            declared_mime_type="image/png",
            max_bytes=1_000_000,
        )


def test_malformed_jpeg_is_rejected():
    with pytest.raises(ImageValidationError, match="malformed"):
        validate_image_bytes(
            b"\xff\xd8\xff\xe0\x00\x10" + b"\x00" * 8 + b"\xff\xd9",
            declared_mime_type="image/jpeg",
            max_bytes=1_000_000,
        )


def test_malformed_webp_is_rejected():
    with pytest.raises(ImageValidationError, match="malformed"):
        validate_image_bytes(
            WEBP_1X1[:16],
            declared_mime_type="image/webp",
            max_bytes=1_000_000,
        )


def test_max_bytes_must_be_positive():
    with pytest.raises(ValueError, match="max_bytes"):
        validate_image_bytes(
            PNG_1X1,
            declared_mime_type="image/png",
            max_bytes=0,
        )


def test_error_message_never_contains_image_bytes():
    secret = b"SECRET-IMAGE-BYTES"
    with pytest.raises(ImageValidationError) as excinfo:
        validate_image_bytes(
            secret,
            declared_mime_type="image/png",
            max_bytes=1_000_000,
        )

    assert "SECRET-IMAGE-BYTES" not in str(excinfo.value)


def test_validation_error_is_a_safe_provider_error():
    error = ImageValidationError("safe message", provider="openai", model="gpt-image-1")

    assert isinstance(error, ImageProviderResponseError)
    assert error.provider == "openai"
    assert error.model == "gpt-image-1"
