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
from app.image_providers.openai import (
    DEFAULT_OPENAI_IMAGE_MODEL,
    HttpResponse,
    OpenAIImageProvider,
)
from tests.fakes import FakeHttpTransport
from tests.test_image_validation import PNG_1X1

API_KEY = "sk-test-secret"
PROMPT = "Create a diagram of a bus tracking system."


def _provider(
    transport: FakeHttpTransport,
    **overrides: object,
) -> OpenAIImageProvider:
    options: dict[str, object] = {
        "api_key": API_KEY,
        "model": DEFAULT_OPENAI_IMAGE_MODEL,
        "size": "1024x1024",
        "quality": "standard",
        "timeout_seconds": 60.0,
        "max_bytes": 1_000_000,
        "prompt_max_chars": 4_000,
        "transport": transport,
    }
    options.update(overrides)
    return OpenAIImageProvider(**options)  # type: ignore[arg-type]


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


def _success_response(
    image: bytes = PNG_1X1,
    *,
    document_extra: dict[str, object] | None = None,
    item_extra: dict[str, object] | None = None,
) -> HttpResponse:
    item: dict[str, object] = {
        "b64_json": base64.b64encode(image).decode("ascii"),
    }
    item.update(item_extra or {})
    payload: dict[str, object] = {"data": [item]}
    payload.update(document_extra or {})
    return _json_response(payload)


def test_openai_success_parses_base64_image():
    transport = FakeHttpTransport(response=_success_response(document_extra={"id": "img_123"}))
    provider = _provider(transport)

    image = provider.generate(_request())

    assert image.data == PNG_1X1
    assert image.mime_type == "image/png"
    assert image.extension == "png"
    assert image.width == 1
    assert image.height == 1
    assert image.provider == "openai"
    assert image.model == DEFAULT_OPENAI_IMAGE_MODEL
    assert image.provider_request_id == "img_123"
    assert image.revised_prompt is None
    assert image.metadata["size"] == "1024x1024"
    assert image.metadata["quality"] == "standard"


def test_openai_payload_uses_configured_values():
    transport = FakeHttpTransport(response=_success_response())
    provider = _provider(transport, timeout_seconds=12.5)

    provider.generate(
        _request(width=512, height=512, quality="low", timeout_seconds=1.0)
    )

    call = transport.calls[0]
    payload = json.loads(call.body.decode("utf-8"))
    assert call.url == "https://api.openai.com/v1/images/generations"
    assert call.timeout == 12.5
    assert call.headers["Authorization"] == f"Bearer {API_KEY}"
    assert call.headers["Content-Type"] == "application/json"
    assert payload["model"] == DEFAULT_OPENAI_IMAGE_MODEL
    assert payload["prompt"] == PROMPT
    assert payload["size"] == "1024x1024"
    assert payload["quality"] == "standard"
    assert payload["n"] == 1
    assert "response_format" not in payload


def test_openai_dall_e_requests_base64_response():
    transport = FakeHttpTransport(response=_success_response())
    provider = _provider(transport, model="dall-e-3")

    provider.generate(_request())

    payload = json.loads(transport.calls[0].body.decode("utf-8"))
    assert payload["response_format"] == "b64_json"


def test_openai_rejects_prompt_over_configured_limit():
    transport = FakeHttpTransport(response=_success_response())
    provider = _provider(transport, prompt_max_chars=10)

    with pytest.raises(ImageProviderResponseError, match="maximum length"):
        provider.generate(_request())

    assert transport.calls == []


def test_openai_strict_base64_failure():
    transport = FakeHttpTransport(
        response=_json_response({"data": [{"b64_json": "not base64!!!"}]})
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="invalid base64") as excinfo:
        provider.generate(_request())

    assert "not base64!!!" not in str(excinfo.value)


def test_openai_auth_error_is_mapped():
    transport = FakeHttpTransport(response=_json_response({}, status=401))
    provider = _provider(transport)

    with pytest.raises(ImageProviderAuthError):
        provider.generate(_request())


def test_openai_rate_limit_is_mapped_with_retry_after():
    transport = FakeHttpTransport(
        response=_json_response({}, status=429, headers={"Retry-After": "2.5"})
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderRateLimitError) as excinfo:
        provider.generate(_request())

    error = excinfo.value
    assert error.retryable is True
    assert error.retry_after == 2.5


def test_openai_timeout_exception_is_mapped():
    transport = FakeHttpTransport(error=TimeoutError("slow"))
    provider = _provider(transport)

    with pytest.raises(ImageProviderTimeoutError) as excinfo:
        provider.generate(_request())

    assert excinfo.value.retryable is True


def test_openai_url_error_timeout_is_mapped():
    transport = FakeHttpTransport(error=urllib.error.URLError(TimeoutError("slow")))
    provider = _provider(transport)

    with pytest.raises(ImageProviderTimeoutError):
        provider.generate(_request())


def test_openai_url_error_is_mapped_to_unavailable():
    transport = FakeHttpTransport(error=urllib.error.URLError("dns failure"))
    provider = _provider(transport)

    with pytest.raises(ImageProviderUnavailableError) as excinfo:
        provider.generate(_request())

    assert excinfo.value.retryable is True


def test_openai_content_policy_error_is_mapped():
    transport = FakeHttpTransport(
        response=_json_response(
            {
                "error": {
                    "code": "content_policy_violation",
                    "message": "The prompt was blocked by moderation.",
                }
            },
            status=400,
        )
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderContentPolicyError):
        provider.generate(_request())


def test_openai_server_error_is_mapped_to_unavailable():
    transport = FakeHttpTransport(response=_json_response({}, status=500))
    provider = _provider(transport)

    with pytest.raises(ImageProviderUnavailableError) as excinfo:
        provider.generate(_request())

    assert excinfo.value.retryable is True


def test_openai_does_not_retry():
    transport = FakeHttpTransport(response=_json_response({}, status=500))
    provider = _provider(transport)

    with pytest.raises(ImageProviderUnavailableError):
        provider.generate(_request())

    assert len(transport.calls) == 1


def test_openai_malformed_json_is_mapped():
    transport = FakeHttpTransport(
        response=HttpResponse(status_code=200, headers={}, body=b"{not json")
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="malformed JSON"):
        provider.generate(_request())


def test_openai_unexpected_response_shape_is_mapped():
    transport = FakeHttpTransport(response=_json_response(["unexpected"]))
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="unexpected response shape"):
        provider.generate(_request())


def test_openai_missing_data_is_mapped():
    transport = FakeHttpTransport(response=_json_response({}))
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="no image data"):
        provider.generate(_request())


def test_openai_unexpected_data_entry_is_mapped():
    transport = FakeHttpTransport(response=_json_response({"data": ["nope"]}))
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="unexpected image entry"):
        provider.generate(_request())


def test_openai_url_only_response_is_rejected_without_exposing_url():
    transport = FakeHttpTransport(
        response=_json_response(
            {"data": [{"url": "https://example.com/secret-image.png"}]}
        )
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="URL downloads are disabled") as excinfo:
        provider.generate(_request())

    assert "https://example.com/secret-image.png" not in str(excinfo.value)


def test_openai_invalid_image_bytes_are_rejected():
    transport = FakeHttpTransport(
        response=_success_response(image=b"not an image")
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="signature"):
        provider.generate(_request())


def test_openai_oversized_image_is_rejected():
    transport = FakeHttpTransport(response=_success_response())
    provider = _provider(transport, max_bytes=10)

    with pytest.raises(ImageProviderResponseError, match="maximum"):
        provider.generate(_request())


def test_openai_output_format_mismatch_is_rejected():
    transport = FakeHttpTransport(
        response=_success_response(item_extra={"output_format": "jpeg"})
    )
    provider = _provider(transport)

    with pytest.raises(ImageProviderResponseError, match="does not match"):
        provider.generate(_request())


def test_openai_metadata_whitelist_excludes_prompt_and_secrets():
    transport = FakeHttpTransport(response=_success_response())
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


def test_openai_does_not_leak_secret_or_prompt_in_errors_or_logs(caplog):
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


def test_openai_construction_performs_no_network_calls(monkeypatch):
    def fail_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access during provider construction")

    monkeypatch.setattr("urllib.request.urlopen", fail_network)
    transport = FakeHttpTransport(response=_success_response())

    provider = _provider(transport)
    provider.generate(_request())

    assert len(transport.calls) == 1


def test_openai_missing_key_raises_configuration_error():
    with pytest.raises(ImageProviderConfigurationError, match="OPENAI_API_KEY"):
        OpenAIImageProvider(api_key="")


def test_openai_invalid_size_raises_configuration_error():
    with pytest.raises(ImageProviderConfigurationError, match="WIDTHxHEIGHT"):
        OpenAIImageProvider(api_key=API_KEY, size="huge")


def test_openai_capabilities_are_available_and_static():
    provider = OpenAIImageProvider(
        api_key=API_KEY,
        model="gpt-image-test",
        size="1536x1024",
        quality="high",
        transport=FakeHttpTransport(response=_success_response()),
    )

    capabilities = provider.capabilities()

    assert capabilities.name == "openai"
    assert capabilities.available is True
    assert capabilities.models == ("gpt-image-test",)
    assert capabilities.default_model == "gpt-image-test"
    assert capabilities.supported_sizes == ("1536x1024",)
    assert capabilities.supports_quality is True
    assert capabilities.max_width == 1536
    assert capabilities.max_height == 1024
