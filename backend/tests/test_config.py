from __future__ import annotations

import pytest
from app.config import Settings


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/api/v1", "/api/v1"),
        ("api/v2/", "/api/v2"),
        ("///api/v3///", "/api/v3"),
        ("/", ""),
    ],
)
def test_api_prefix_is_normalized(monkeypatch, raw, expected):
    monkeypatch.setenv("MIND_BRIDGE_API_PREFIX", raw)
    assert Settings().api_prefix == expected


def test_api_prefix_defaults_to_v1(monkeypatch):
    monkeypatch.delenv("MIND_BRIDGE_API_PREFIX", raising=False)
    assert Settings().api_prefix == "/api/v1"


def test_max_request_bytes_is_environment_driven(monkeypatch):
    monkeypatch.setenv("MIND_BRIDGE_MAX_REQUEST_BYTES", "2048")
    assert Settings().max_request_bytes == 2048


@pytest.mark.parametrize("raw", ["0", "-10", "not-a-number", "  "])
def test_invalid_max_request_bytes_falls_back_to_default(monkeypatch, raw):
    monkeypatch.setenv("MIND_BRIDGE_MAX_REQUEST_BYTES", raw)
    assert Settings().max_request_bytes == 1_048_576
