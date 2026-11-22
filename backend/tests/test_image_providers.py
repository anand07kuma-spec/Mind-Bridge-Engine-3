from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.config import Settings
from app.image_providers.base import (
    GeneratedImage,
    ImageGenerationRequest,
    ImageProviderConfigurationError,
    ImageProviderError,
    ImageProviderNotConfiguredError,
    ImageProviderRateLimitError,
    ProviderCapabilities,
)
from app.image_providers.factory import build_image_provider
from app.image_providers.gemini import GeminiImageProvider
from app.image_providers.openai import OpenAIImageProvider
from app.main import create_app
from fastapi.testclient import TestClient
from tests.fakes import FakeImageProvider

SAMPLE_PATH = Path(__file__).resolve().parents[2] / "sample_data" / "bus_tracking.json"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        assets_dir=tmp_path / "assets",
        db_path=tmp_path / "data" / "mindbridge-test.db",
        sample_path=SAMPLE_PATH,
        **overrides,
    )


def test_factory_selects_none(tmp_path: Path):
    assert build_image_provider(_settings(tmp_path, image_provider="none")) is None


def test_factory_selects_openai_with_configured_model(tmp_path: Path):
    provider = build_image_provider(
        _settings(
            tmp_path,
            image_provider="openai",
            image_model="gpt-image-test",
            openai_api_key="test-key",
        )
    )

    assert provider is not None
    assert provider.name == "openai"
    assert provider.model == "gpt-image-test"
    capabilities = provider.capabilities()
    assert capabilities.name == "openai"
    assert capabilities.available is True
    assert "base64" in capabilities.notes.lower()


def test_factory_selects_gemini_with_configured_model(tmp_path: Path):
    provider = build_image_provider(
        _settings(
            tmp_path,
            image_provider="gemini",
            image_model="gemini-image-test",
            gemini_api_key="test-key",
        )
    )

    assert provider is not None
    assert provider.name == "gemini"
    assert provider.model == "gemini-image-test"
    capabilities = provider.capabilities()
    assert capabilities.name == "gemini"
    assert capabilities.available is True
    assert "inline" in capabilities.notes.lower()


def test_factory_uses_provider_default_models(tmp_path: Path):
    openai = build_image_provider(
        _settings(tmp_path, image_provider="openai", openai_api_key="test-key")
    )
    gemini = build_image_provider(
        _settings(tmp_path, image_provider="gemini", gemini_api_key="test-key")
    )

    assert openai is not None and openai.model == "gpt-image-1"
    assert gemini is not None and gemini.model == "gemini-2.5-flash-image"


def test_factory_passes_image_settings_to_openai_adapter(tmp_path: Path):
    provider = build_image_provider(
        _settings(
            tmp_path,
            image_provider="openai",
            openai_api_key="test-key",
            image_size="1536x1024",
            image_quality="high",
            image_timeout_seconds=12.5,
            image_max_bytes=4096,
            image_prompt_max_chars=500,
        )
    )

    assert isinstance(provider, OpenAIImageProvider)
    assert provider.size == "1536x1024"
    assert provider.quality == "high"
    assert provider.timeout_seconds == 12.5
    assert provider.max_bytes == 4096
    assert provider.prompt_max_chars == 500


def test_factory_passes_image_settings_to_gemini_adapter(tmp_path: Path):
    provider = build_image_provider(
        _settings(
            tmp_path,
            image_provider="gemini",
            gemini_api_key="test-key",
            image_size="1536x1024",
            image_quality="high",
            image_timeout_seconds=12.5,
            image_max_bytes=4096,
            image_prompt_max_chars=500,
        )
    )

    assert isinstance(provider, GeminiImageProvider)
    assert provider.size == "1536x1024"
    assert provider.quality == "high"
    assert provider.timeout_seconds == 12.5
    assert provider.max_bytes == 4096
    assert provider.prompt_max_chars == 500


def test_unknown_provider_fails_clearly(tmp_path: Path):
    with pytest.raises(ImageProviderConfigurationError) as excinfo:
        build_image_provider(_settings(tmp_path, image_provider="mystery"))

    message = str(excinfo.value)
    assert "unknown image provider 'mystery'" in message.lower()
    assert "none, openai, gemini" in message


def test_unknown_provider_fails_at_app_startup(tmp_path: Path):
    with pytest.raises(ImageProviderConfigurationError):
        create_app(_settings(tmp_path, image_provider="mystery"))


def test_missing_key_returns_unavailable_provider(tmp_path: Path):
    provider = build_image_provider(
        _settings(tmp_path, image_provider="openai", openai_api_key="")
    )

    assert provider is not None
    capabilities = provider.capabilities()
    assert capabilities.available is False
    assert "OPENAI_API_KEY" in capabilities.notes

    with pytest.raises(ImageProviderNotConfiguredError):
        provider.generate(ImageGenerationRequest(prompt="Draw a bus", scene_id="scene-1"))


def test_missing_gemini_key_returns_unavailable_provider(tmp_path: Path):
    provider = build_image_provider(
        _settings(tmp_path, image_provider="gemini", gemini_api_key="")
    )

    assert provider is not None
    capabilities = provider.capabilities()
    assert capabilities.available is False
    assert "GEMINI_API_KEY" in capabilities.notes

    with pytest.raises(ImageProviderNotConfiguredError):
        provider.generate(ImageGenerationRequest(prompt="Draw a bus", scene_id="scene-1"))


def test_factory_selects_exactly_one_provider_when_both_keys_exist(tmp_path: Path):
    gemini = build_image_provider(
        _settings(
            tmp_path,
            image_provider="gemini",
            openai_api_key="openai-test-key",
            gemini_api_key="gemini-test-key",
        )
    )
    openai = build_image_provider(
        _settings(
            tmp_path,
            image_provider="openai",
            openai_api_key="openai-test-key",
            gemini_api_key="gemini-test-key",
        )
    )

    assert isinstance(gemini, GeminiImageProvider)
    assert isinstance(openai, OpenAIImageProvider)


def test_missing_key_does_not_break_svg_operation(tmp_path: Path):
    settings = _settings(
        tmp_path,
        image_provider="openai",
        openai_api_key="",
    )
    app = create_app(settings)
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))

    with TestClient(app) as client:
        assert app.state.image_provider is not None
        response = client.post("/api/v1/visualize", json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["asset"]["kind"] == "svg"
    assert body["svg"]


def test_missing_gemini_key_does_not_break_svg_operation(tmp_path: Path):
    settings = _settings(
        tmp_path,
        image_provider="gemini",
        gemini_api_key="",
    )
    app = create_app(settings)
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))

    with TestClient(app) as client:
        assert app.state.image_provider is not None
        response = client.post("/api/v1/visualize", json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["asset"]["kind"] == "svg"
    assert body["svg"]


def test_provider_construction_performs_no_network_calls(tmp_path: Path, monkeypatch):
    def fail_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access during provider construction")

    monkeypatch.setattr("urllib.request.urlopen", fail_network)
    monkeypatch.setattr("socket.create_connection", fail_network)

    openai = build_image_provider(
        _settings(tmp_path, image_provider="openai", openai_api_key="test-key")
    )
    gemini = build_image_provider(
        _settings(tmp_path, image_provider="gemini", gemini_api_key="test-key")
    )

    assert openai is not None
    assert gemini is not None


def test_secrets_are_not_exposed_by_provider_or_api(tmp_path: Path):
    settings = _settings(
        tmp_path,
        image_provider="openai",
        openai_api_key="sk-super-secret",
        gemini_api_key="gm-super-secret",
    )
    provider = build_image_provider(settings)
    assert provider is not None

    rendered = "\n".join(
        (repr(settings), repr(provider), repr(provider.capabilities()))
    )
    assert "sk-super-secret" not in rendered
    assert "gm-super-secret" not in rendered

    app = create_app(settings)
    with TestClient(app) as client:
        for path in ("/", "/api/v1/health"):
            response = client.get(path)
            assert "sk-super-secret" not in response.text
            assert "gm-super-secret" not in response.text


def test_generated_image_rejects_invalid_payloads():
    with pytest.raises(ValueError, match="data"):
        GeneratedImage(data=b"", mime_type="image/png", extension="png")
    with pytest.raises(ValueError, match="image/"):
        GeneratedImage(data=b"bytes", mime_type="text/plain", extension="txt")
    with pytest.raises(ValueError, match="extension"):
        GeneratedImage(data=b"bytes", mime_type="image/png", extension=".")


def test_generated_image_normalizes_mime_and_extension():
    image = GeneratedImage(
        data=b"bytes",
        mime_type=" IMAGE/PNG ",
        extension=".PNG",
        width=10,
        height=10,
    )

    assert image.mime_type == "image/png"
    assert image.extension == "png"


def test_image_request_rejects_invalid_values():
    with pytest.raises(ValueError, match="prompt"):
        ImageGenerationRequest(prompt=" ", scene_id="scene-1")
    with pytest.raises(ValueError, match="scene_id"):
        ImageGenerationRequest(prompt="Draw", scene_id=" ")
    with pytest.raises(ValueError, match="width and height"):
        ImageGenerationRequest(prompt="Draw", scene_id="scene-1", width=0)
    with pytest.raises(ValueError, match="timeout"):
        ImageGenerationRequest(prompt="Draw", scene_id="scene-1", timeout_seconds=0)
    with pytest.raises(ValueError, match="quality"):
        ImageGenerationRequest(prompt="Draw", scene_id="scene-1", quality=" ")


def test_provider_capabilities_requires_a_name():
    with pytest.raises(ValueError, match="name"):
        ProviderCapabilities(name=" ", available=True)


def test_fake_provider_is_deterministic_and_offline():
    provider = FakeImageProvider()
    request = ImageGenerationRequest(prompt="Draw a bus", scene_id="scene-1")

    first = provider.generate(request)
    second = provider.generate(request)

    assert first.data == second.data
    assert first.mime_type == "image/png"
    assert first.extension == "png"
    assert first.provider == "fake"
    assert first.model == "fake-image-1"
    assert provider.calls == [request, request]


def test_error_taxonomy_exposes_safe_operational_metadata():
    error = ImageProviderRateLimitError(
        "The image provider asked us to slow down.",
        provider="openai",
        model="gpt-image-1",
        retryable=True,
        provider_request_id="req_123",
        retry_after=2.5,
    )

    assert isinstance(error, ImageProviderError)
    assert str(error) == "The image provider asked us to slow down."
    assert error.provider == "openai"
    assert error.model == "gpt-image-1"
    assert error.retryable is True
    assert error.provider_request_id == "req_123"
    assert error.retry_after == 2.5
