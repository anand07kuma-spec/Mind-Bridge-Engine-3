from __future__ import annotations

import base64
import json
import logging
import urllib.error

import pytest
from app.image_providers.base import (
    ImageGenerationRequest,
    ImageProviderAuthError,
    ImageProviderConfigurationError,
    ImageProviderContentPolicyError,
    ImageProviderRateLimitError,
    ImageProviderResponseError,
    ImageProviderTimeoutError,
    ImageProviderUnavailableError,
)
from app.image_providers.gemini import (
    DEFAULT_GEMINI_API_BASE,
    DEFAULT_GEMINI_IMAGE_MODEL,
    GeminiImageProvider,
)
from app.image_providers.http import HttpResponse
from tests.fakes import FakeHttpTransport
from tests.test_image_validation import JPEG_1X1, PNG_1X1, WEBP_1X1

API_KEY = "gm-test-secret"
PROMPT = "Create a diagram of a bus tracking system."


def _provider(
    transport: FakeHttpTransport,
    **overrides: object,
) -> GeminiImageProvider:
    options: dict[str, object] = {
        "api_key": API_KEY,
        "model": DEFAULT_GEMINI_IMAGE_MODEL,
        "size": "1024x1024",
        "quality": "standard",
        "timeout_seconds": 60.0,
        "max_bytes": 1_000_000,
        "prompt_max_chars": 4_000,
        "transport": transport,
    }
    options.update(overrides)
    return GeminiImageProvider(**options)  # type: ignore[arg-type]


def _request(**overrides: object) -> ImageGenerationRequest:
    options: dict[str, object] = {
        "prompt": PROMPT,
        "scene_id": "scene-1",
    }
    options.update(overrides)
    return ImageGenerationRequest(**options)  # type: ignore[arg-type]


def _json_response(
    payload: object,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers=headers or {},
        body=json.dumps(payload).encode("utf-8"),
    )


def _inline_response(
    image: bytes = PNG_1X1,
    *,
    mime_type: str = "image/png",
    document_extra: dict[str, object] | None = None,
    parts_extra: list[object] | None = None,
) -> HttpResponse:
    parts: list[object] = [
        {"text": "Here is the generated image."},
        {
            "inlineData": {
                "mimeType": mime_type,
                "data": base64.b64encode(image).decode("ascii"),
            }
        },
    ]
    parts.extend(parts_extra or [])
    payload: dict[str, object] = {
        "candidates": [{"content": {"role": "model", "parts": parts}}]
    }
    payload.update(document_extra or {})
    return _json_response(payload)


def test_gemini_success_parses_inline_image():
    transport = FakeHttpTransport(
        response=_inline_response(document_extra={"responseId": "resp_123"})
    )
    provider = _provider(transport)

    image = provider.generate(_request())

    assert image.data == PNG_1X1
    assert image.mime_type == "image/png"
    assert image.extension == "png"
    assert image.width == 1
    assert image.height == 1
    assert image.provider == "gemini"
    assert image.model == DEFAULT_GEMINI_IMAGE_MODEL
    assert image.provider_request_id == "resp_123"
    assert image.revised_prompt is None
    assert image.metadata["size"] == "1024x1024"
    assert image.metadata["quality"] == "standard"
    assert image.metadata["aspect_ratio"] == "1:1"
    assert image.metadata["image_size"] == "1K"


@pytest.mark.parametrize(
    ("image", "mime_type", "extension"),
    [
        (PNG_1X1, "image/png", "png"),
        (JPEG_1X1, "image/jpeg", "jpg"),
        (WEBP_1X1, "image/webp", "webp"),
    ],
)
def test_gemini_supports_allowed_mime_types(image, mime_type, extension):
    transport = FakeHttpTransport(
        response=_inline_response(image, mime_type=mime_type)
    )
    provider = _provider(transport)

    result = provider.generate(_request())

    assert result.data == image
    assert result.mime_type == mime_type
    assert result.extension == extension


def test_gemini_uses_last_non_thought_inline_image():
    thought_part = {
        "thought": True,
        "inlineData": {
            "mimeType": "image/png",
            "data": base64.b64encode(PNG_1X1).decode("ascii"),
        },
    }
    transport = FakeHttpTransport(
        response=_inline_response(JPEG_1X1, mime_type="image/jpeg", parts_extra=[thought_part])
    )
    provider = _provider(transport)

    image = provider.generate(_request())

    assert image.mime_type == "image/jpeg"
    assert image.extension == "jpg"


def test_gemini_ignores_thought_image_after_final_image():
    thought_part = {
        "thought": True,
        "inlineData": {
            "mimeType": "image/jpeg",
            "data": base64.b64encode(JPEG_1X1).decode("ascii"),
        },
    }
    transport = FakeHttpTransport(
        response=_inline_response(PNG_1X1, parts_extra=[thought_part])
    )
    provider = _provider(transport)

    image = provider.generate(_request())

    assert image.mime_type == "image/png"


def test_gemini_accepts_snake_case_inline_data():
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": base64.b64encode(PNG_1X1).decode("ascii"),
                            }
                        }
                    ]
                }
            }
        ]
    }
    transport = FakeHttpTransport(response=_json_response(payload))
    provider = _provider(transport)

    image = provider.generate(_request())

    assert image.data == PNG_1X1
    assert image.mime_type == "image/png"


def test_gemini_payload_uses_configured_values():
    transport = FakeHttpTransport(response=_inline_response())
    provider = _provider(
        transport,
        model="gemini-3-pro-image-preview",
        size="1536x1024",
        quality="high",
        timeout_seconds=12.5,
    )

    provider.generate(
        _request(width=512, height=512, quality="low", timeout_seconds=1.0)
    )

    call = transport.calls[0]
    payload = json.loads(call.body.decode("utf-8"))
    assert call.url == (
        f"{DEFAULT_GEMINI_API_BASE}/gemini-3-pro-image-preview:generateContent"
    )
    assert call.timeout == 12.5
    assert call.headers["x-goog-api-key"] == API_KEY
    assert call.headers["Content-Type"] == "application/json"
    assert payload["contents"][0]["parts"][0]["text"] == PROMPT
    assert payload["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
    assert payload["generationConfig"]["imageConfig"]["aspectRatio"] == "3:2"
    assert payload["generationConfig"]["imageConfig"]["imageSize"] == "2K"
    assert "model" not in payload


def test_gemini_api_key_is_sent_in_header_not_url_or_body():
    transport = FakeHttpTransport(response=_inline_response())
    provider = _provider(transport)

    provider.generate(_request())

    call = transport.calls[0]
    assert call.headers["x-goog-api-key"] == API_KEY
    assert API_KEY not in call.url
    assert API_KEY not in call.body.decode("utf-8")
    assert "key=" not in call.url.lower()


def test_gemini_clamps_image_size_for_25_flash_model():
    transport = FakeHttpTransport(response=_inline_response())
    provider = _provider(transport, model=DEFAULT_GEMINI_IMAGE_MODEL, quality="ultra")

    provider.generate(_request())

    payload = json.loads(transport.calls[0].body.decode("utf-8"))
    assert payload["generationConfig"]["imageConfig"]["imageSize"] == "1K"


def test_gemini_auto_size_omits_aspect_ratio():
    transport = FakeHttpTransport(response=_inline_response())
    provider = _provider(transport, size="auto")

    provider.generate(_request())

    payload = json.loads(transport.calls[0].body.decode("utf-8"))
    image_config = payload["generationConfig"]["imageConfig"]
    assert "aspectRatio" not in image_config
    assert image_config["imageSize"] == "1K"


def test_gemini_unsupported_aspect_ratio_is_omitted():
    transport = FakeHttpTransport(response=_inline_response())
    provider = _provider(transport, size="1000x999")

    provider.generate(_request())

    payload = json.loads(transport.calls[0].body.decode("utf-8"))
    assert "aspectRatio" not in payload["generationConfig"]["imageConfig"]


def test_gemini_normalizes_models_prefix_in_url():
    transport = FakeHttpTransport(response=_inline_response())
    provider = _provider(transport, model="models/gemini-3-pro-image-preview")

    provider.generate(_request())

    assert provider.model == "gemini-3-pro-image-preview"
    assert transport.calls[0].url == (
        f"{DEFAULT_GEMINI_API_BASE}/gemini-3-pro-image-preview:generateContent"
    )


def test_gemini_rejects_prompt_over_configured_limit():
    transport = FakeHttpTransport(response=_inline_response())
    provider = _provider(transport, prompt_max_chars=10)

    with pytest.raises(ImageProviderResponseError, match="maximum length"):
        provider.generate(_request())

    assert transport.calls == []


def test_gemini_strict_base64_rejection():
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": "not base64!!!",
                            }
                        }
                    ]
                }
            }
        ]
    }
    transport = FakeHttpTransport(response=_json_response(payload))
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="invalid base64") as excinfo:
        provider.generate(_request())

    assert "not base64!!!" not in str(excinfo.value)


def test_gemini_missing_inline_image_data_is_mapped():
    payload = {
        "candidates": [{"content": {"parts": [{"text": "No image generated."}]}}]
    }
    transport = FakeHttpTransport(response=_json_response(payload))
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="no inline image data"):
        provider.generate(_request())


def test_gemini_file_data_is_rejected_without_exposing_uri():
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "fileData": {
                                "mimeType": "image/png",
                                "fileUri": "https://example.com/secret-image.png",
                            }
                        }
                    ]
                }
            }
        ]
    }
    transport = FakeHttpTransport(response=_json_response(payload))
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="URI downloads are disabled") as excinfo:
        provider.generate(_request())

    assert "https://example.com/secret-image.png" not in str(excinfo.value)


def test_gemini_malformed_json_is_mapped():
    transport = FakeHttpTransport(
        response=HttpResponse(status_code=200, headers={}, body=b"{not json")
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="malformed JSON"):
        provider.generate(_request())


def test_gemini_unexpected_response_shape_is_mapped():
    transport = FakeHttpTransport(response=_json_response(["unexpected"]))
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="unexpected response shape"):
        provider.generate(_request())


def test_gemini_missing_candidates_is_mapped():
    transport = FakeHttpTransport(response=_json_response({}))
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="no candidates"):
        provider.generate(_request())


def test_gemini_unexpected_candidate_is_mapped():
    transport = FakeHttpTransport(response=_json_response({"candidates": ["nope"]}))
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="unexpected candidate"):
        provider.generate(_request())


def test_gemini_missing_content_is_mapped():
    transport = FakeHttpTransport(response=_json_response({"candidates": [{}]}))
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="no content"):
        provider.generate(_request())


def test_gemini_missing_parts_is_mapped():
    transport = FakeHttpTransport(
        response=_json_response({"candidates": [{"content": {}}]})
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="no content parts"):
        provider.generate(_request())


def test_gemini_invalid_image_bytes_are_rejected():
    transport = FakeHttpTransport(
        response=_inline_response(b"not an image", mime_type="image/png")
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="signature"):
        provider.generate(_request())


def test_gemini_oversized_image_is_rejected():
    transport = FakeHttpTransport(response=_inline_response())
    provider = _provider(transport, max_bytes=10)

    with pytest.raises(ImageProviderResponseError, match="maximum"):
        provider.generate(_request())


def test_gemini_mime_signature_mismatch_is_rejected():
    transport = FakeHttpTransport(
        response=_inline_response(PNG_1X1, mime_type="image/jpeg")
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="does not match"):
        provider.generate(_request())


@pytest.mark.parametrize("status", [401, 403])
def test_gemini_auth_status_is_mapped(status):
    transport = FakeHttpTransport(response=_json_response({}, status=status))
    provider = _provider(transport)

    with pytest.raises(ImageProviderAuthError):
        provider.generate(_request())


def test_gemini_400_invalid_key_is_mapped_to_auth():
    transport = FakeHttpTransport(
        response=_json_response(
            {
                "error": {
                    "status": "INVALID_ARGUMENT",
                    "message": "API key not valid. Please pass a valid API key.",
                }
            },
            status=400,
        )
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderAuthError):
        provider.generate(_request())


def test_gemini_rate_limit_is_mapped_with_retry_after_header():
    transport = FakeHttpTransport(
        response=_json_response({}, status=429, headers={"Retry-After": "2.5"})
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderRateLimitError) as excinfo:
        provider.generate(_request())

    assert excinfo.value.retryable is True
    assert excinfo.value.retry_after == 2.5


def test_gemini_rate_limit_uses_retry_delay_from_body():
    transport = FakeHttpTransport(
        response=_json_response(
            {
                "error": {
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3s"}],
                }
            },
            status=429,
        )
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderRateLimitError) as excinfo:
        provider.generate(_request())

    assert excinfo.value.retry_after == 3.0


def test_gemini_timeout_exception_is_mapped():
    transport = FakeHttpTransport(error=TimeoutError("slow"))
    provider = _provider(transport)

    with pytest.raises(ImageProviderTimeoutError) as excinfo:
        provider.generate(_request())

    assert excinfo.value.retryable is True


def test_gemini_url_error_timeout_is_mapped():
    transport = FakeHttpTransport(error=urllib.error.URLError(TimeoutError("slow")))
    provider = _provider(transport)

    with pytest.raises(ImageProviderTimeoutError):
        provider.generate(_request())


def test_gemini_url_error_is_mapped_to_unavailable():
    transport = FakeHttpTransport(error=urllib.error.URLError("dns failure"))
    provider = _provider(transport)

    with pytest.raises(ImageProviderUnavailableError) as excinfo:
        provider.generate(_request())

    assert excinfo.value.retryable is True


def test_gemini_os_error_is_mapped_to_unavailable():
    transport = FakeHttpTransport(error=OSError("network down"))
    provider = _provider(transport)

    with pytest.raises(ImageProviderUnavailableError):
        provider.generate(_request())


@pytest.mark.parametrize("status", [408, 504])
def test_gemini_http_timeout_status_is_mapped(status):
    transport = FakeHttpTransport(response=_json_response({}, status=status))
    provider = _provider(transport)

    with pytest.raises(ImageProviderTimeoutError):
        provider.generate(_request())


@pytest.mark.parametrize("status", [500, 503])
def test_gemini_server_error_is_mapped_to_unavailable(status):
    transport = FakeHttpTransport(response=_json_response({}, status=status))
    provider = _provider(transport)

    with pytest.raises(ImageProviderUnavailableError) as excinfo:
        provider.generate(_request())

    assert excinfo.value.retryable is True


def test_gemini_does_not_retry():
    transport = FakeHttpTransport(response=_json_response({}, status=500))
    provider = _provider(transport)

    with pytest.raises(ImageProviderUnavailableError):
        provider.generate(_request())

    assert len(transport.calls) == 1


def test_gemini_prompt_feedback_block_is_mapped_to_content_policy():
    transport = FakeHttpTransport(
        response=_json_response(
            {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}
        )
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderContentPolicyError):
        provider.generate(_request())


def test_gemini_safety_finish_reason_is_mapped_to_content_policy():
    transport = FakeHttpTransport(
        response=_json_response(
            {
                "candidates": [
                    {
                        "finishReason": "IMAGE_SAFETY",
                        "content": {"parts": []},
                    }
                ]
            }
        )
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderContentPolicyError):
        provider.generate(_request())


def test_gemini_safety_error_body_is_mapped_to_content_policy():
    transport = FakeHttpTransport(
        response=_json_response(
            {"error": {"message": "The prompt was blocked by safety filters."}},
            status=400,
        )
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderContentPolicyError):
        provider.generate(_request())


def test_gemini_metadata_whitelist_excludes_prompt_and_secrets():
    transport = FakeHttpTransport(response=_inline_response())
    provider = _provider(transport)

    image = provider.generate(
        _request(
            metadata={
                "prompt": "TOP SECRET PROMPT",
                "prompt_hash": "abc123",
                "prompt_template_version": "1",
                "api_key": API_KEY,
            }
        )
    )

    assert image.metadata["prompt_hash"] == "abc123"
    assert image.metadata["prompt_template_version"] == "1"
    assert "TOP SECRET PROMPT" not in str(image.metadata)
    assert API_KEY not in str(image.metadata)


def test_gemini_does_not_leak_secret_or_prompt_in_errors_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    transport = FakeHttpTransport(
        response=HttpResponse(
            status_code=401,
            headers={},
            body=(
                f'{{"error":{{"message":"bad key {API_KEY} for prompt {PROMPT}"}}}}'
            ).encode(),
        )
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderAuthError) as excinfo:
        provider.generate(_request())

    rendered = "\n".join(
        (str(excinfo.value), repr(excinfo.value), repr(provider), caplog.text)
    )
    assert API_KEY not in rendered
    assert PROMPT not in rendered


def test_gemini_response_id_falls_back_to_id():
    transport = FakeHttpTransport(
        response=_inline_response(document_extra={"id": "resp_abc"})
    )
    provider = _provider(transport)

    image = provider.generate(_request())

    assert image.provider_request_id == "resp_abc"


def test_gemini_construction_performs_no_network_calls(monkeypatch):
    def fail_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access during provider construction")

    monkeypatch.setattr("urllib.request.urlopen", fail_network)
    monkeypatch.setattr("socket.create_connection", fail_network)

    provider = GeminiImageProvider(api_key=API_KEY)

    assert provider.model == DEFAULT_GEMINI_IMAGE_MODEL


def test_gemini_generation_with_fake_transport_performs_no_network_calls(monkeypatch):
    def fail_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access with a fake transport")

    monkeypatch.setattr("urllib.request.urlopen", fail_network)
    monkeypatch.setattr("socket.create_connection", fail_network)
    transport = FakeHttpTransport(response=_inline_response())
    provider = _provider(transport)

    image = provider.generate(_request())

    assert image.data == PNG_1X1
    assert len(transport.calls) == 1


def test_gemini_never_downloads_provider_urls(monkeypatch):
    def fail_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("provider URL must not be downloaded")

    monkeypatch.setattr("urllib.request.urlopen", fail_network)
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "fileData": {
                                "mimeType": "image/png",
                                "fileUri": "https://example.com/image.png",
                            }
                        }
                    ]
                }
            }
        ]
    }
    transport = FakeHttpTransport(response=_json_response(payload))
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError):
        provider.generate(_request())


def test_gemini_missing_key_raises_configuration_error():
    with pytest.raises(ImageProviderConfigurationError, match="GEMINI_API_KEY"):
        GeminiImageProvider(api_key="")


def test_gemini_invalid_size_raises_configuration_error():
    with pytest.raises(ImageProviderConfigurationError, match="WIDTHxHEIGHT"):
        GeminiImageProvider(api_key=API_KEY, size="huge")


def test_gemini_capabilities_are_available_and_static():
    provider = GeminiImageProvider(
        api_key=API_KEY,
        model="gemini-3-pro-image-preview",
        size="1536x1024",
        quality="high",
        transport=FakeHttpTransport(response=_inline_response()),
    )

    capabilities = provider.capabilities()

    assert capabilities.name == "gemini"
    assert capabilities.available is True
    assert capabilities.models == ("gemini-3-pro-image-preview",)
    assert capabilities.default_model == "gemini-3-pro-image-preview"
    assert capabilities.supported_sizes == ("1536x1024",)
    assert capabilities.supports_quality is True
    assert capabilities.supports_reference_images is False
    assert capabilities.max_width == 1536
    assert capabilities.max_height == 1024
    assert "inline" in capabilities.notes.lower()
