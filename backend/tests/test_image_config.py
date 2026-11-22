from __future__ import annotations

from app.config import Settings

_IMAGE_ENV = (
    "MIND_BRIDGE_IMAGE_PROVIDER",
    "MIND_BRIDGE_IMAGE_MODEL",
    "MIND_BRIDGE_IMAGE_SIZE",
    "MIND_BRIDGE_IMAGE_QUALITY",
    "MIND_BRIDGE_IMAGE_TIMEOUT_SECONDS",
    "MIND_BRIDGE_IMAGE_MAX_RETRIES",
    "MIND_BRIDGE_IMAGE_MAX_BYTES",
    "MIND_BRIDGE_IMAGE_PROMPT_MAX_CHARS",
    "MIND_BRIDGE_IMAGE_PROMPT_TEMPLATE_VERSION",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
)


def _clear_image_environment(monkeypatch) -> None:
    for name in _IMAGE_ENV:
        monkeypatch.delenv(name, raising=False)


def test_image_defaults_are_safe_and_disabled(monkeypatch):
    _clear_image_environment(monkeypatch)
    settings = Settings()

    assert settings.image_provider == "none"
    assert settings.image_model == ""
    assert settings.image_size == "1024x1024"
    assert settings.image_quality == "standard"
    assert settings.image_timeout_seconds == 60.0
    assert settings.image_max_retries == 0
    assert settings.image_max_bytes == 20_971_520
    assert settings.image_prompt_max_chars == 4_000
    assert settings.image_prompt_template_version == "1"
    assert settings.openai_api_key == ""
    assert settings.gemini_api_key == ""


def test_image_settings_are_environment_driven(monkeypatch):
    _clear_image_environment(monkeypatch)
    monkeypatch.setenv("MIND_BRIDGE_IMAGE_PROVIDER", " OpenAI ")
    monkeypatch.setenv("MIND_BRIDGE_IMAGE_MODEL", "gpt-image-test")
    monkeypatch.setenv("MIND_BRIDGE_IMAGE_SIZE", "1536x1024")
    monkeypatch.setenv("MIND_BRIDGE_IMAGE_QUALITY", "high")
    monkeypatch.setenv("MIND_BRIDGE_IMAGE_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("MIND_BRIDGE_IMAGE_MAX_RETRIES", "2")
    monkeypatch.setenv("MIND_BRIDGE_IMAGE_MAX_BYTES", "4096")
    monkeypatch.setenv("MIND_BRIDGE_IMAGE_PROMPT_MAX_CHARS", "500")
    monkeypatch.setenv("MIND_BRIDGE_IMAGE_PROMPT_TEMPLATE_VERSION", "2")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")

    settings = Settings()

    assert settings.image_provider == "openai"
    assert settings.image_model == "gpt-image-test"
    assert settings.image_size == "1536x1024"
    assert settings.image_quality == "high"
    assert settings.image_timeout_seconds == 12.5
    assert settings.image_max_retries == 2
    assert settings.image_max_bytes == 4096
    assert settings.image_prompt_max_chars == 500
    assert settings.image_prompt_template_version == "2"
    assert settings.openai_api_key == "openai-test-key"
    assert settings.gemini_api_key == "gemini-test-key"


def test_zero_retries_is_accepted(monkeypatch):
    _clear_image_environment(monkeypatch)
    monkeypatch.setenv("MIND_BRIDGE_IMAGE_MAX_RETRIES", "0")

    assert Settings().image_max_retries == 0


def test_negative_retries_falls_back_to_zero(monkeypatch):
    _clear_image_environment(monkeypatch)
    monkeypatch.setenv("MIND_BRIDGE_IMAGE_MAX_RETRIES", "-1")

    assert Settings().image_max_retries == 0


def test_invalid_timeout_falls_back_to_default(monkeypatch):
    _clear_image_environment(monkeypatch)
    monkeypatch.setenv("MIND_BRIDGE_IMAGE_TIMEOUT_SECONDS", "not-a-number")

    assert Settings().image_timeout_seconds == 60.0


def test_secrets_are_not_exposed_in_settings_repr(monkeypatch):
    _clear_image_environment(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gm-super-secret")

    rendered = repr(Settings())

    assert "sk-super-secret" not in rendered
    assert "gm-super-secret" not in rendered
    assert "openai_api_key" not in rendered
    assert "gemini_api_key" not in rendered
