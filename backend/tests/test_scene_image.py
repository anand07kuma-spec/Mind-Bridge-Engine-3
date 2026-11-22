"""Phase 4 / Milestone 1: optional AI image rendering through SceneService.

Every provider is a deterministic in-memory fake. These tests never touch the
network and never request a real API key.
"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

import pytest
from app.config import Settings
from app.image_providers.base import (
    ImageProviderAuthError,
    ImageProviderConfigurationError,
    ImageProviderContentPolicyError,
    ImageProviderNotConfiguredError,
    ImageProviderRateLimitError,
    ImageProviderResponseError,
    ImageProviderTimeoutError,
    ImageProviderUnavailableError,
    UnavailableImageProvider,
)
from app.image_providers.prompt import build_image_prompt
from app.main import create_app
from fastapi.testclient import TestClient
from tests.fakes import FakeImageProvider

API = "/api/v1"
SAMPLE_PATH = Path(__file__).resolve().parents[2] / "sample_data" / "bus_tracking.json"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        assets_dir=tmp_path / "assets",
        db_path=tmp_path / "data" / "mindbridge-test.db",
        sample_path=SAMPLE_PATH,
        **overrides,
    )


def _image_instruction(sample_instruction: dict, **overrides: object) -> dict:
    payload = copy.deepcopy(sample_instruction)
    payload["render_mode"] = "image"
    payload.update(overrides)
    return payload


def _post(client, instruction: dict, expected_revision: int | None = None):
    params = {} if expected_revision is None else {"expected_revision": expected_revision}
    return client.post(f"{API}/visualize", params=params, json=instruction)


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def _counts(client) -> tuple[int, int]:
    with client.app.state.database.session() as connection:
        assets = int(connection.execute("SELECT COUNT(*) AS count FROM assets").fetchone()["count"])
        revisions = int(
            connection.execute("SELECT COUNT(*) AS count FROM scene_revisions").fetchone()["count"]
        )
    return assets, revisions


def _files(settings: Settings) -> list[str]:
    return sorted(
        path.relative_to(settings.assets_dir).as_posix()
        for path in settings.assets_dir.rglob("*")
        if path.is_file()
    )


@pytest.fixture
def image_app(settings: Settings):
    provider = FakeImageProvider()
    app = create_app(settings, image_provider=provider)
    with TestClient(app) as client:
        yield client, provider


class _GatedImageProvider(FakeImageProvider):
    """Fake provider that holds the first call open for concurrency tests."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.started = threading.Event()
        self.release = threading.Event()
        self.second_entered = threading.Event()
        self._state_lock = threading.Lock()
        self.active = 0
        self.entries = 0
        self.max_active = 0

    def generate(self, request):
        with self._state_lock:
            self.active += 1
            self.entries += 1
            self.max_active = max(self.max_active, self.active)
            if self.entries >= 2:
                self.second_entered.set()
        self.started.set()
        if not self.release.wait(timeout=10):
            raise AssertionError("gated provider was never released")
        try:
            return super().generate(request)
        finally:
            with self._state_lock:
                self.active -= 1


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_render_mode_defaults_to_svg(client, sample_instruction):
    body = _ok(_post(client, sample_instruction))
    assert body["render_mode"] == "svg"
    assert body["asset"]["kind"] == "svg"
    assert body["svg"]


def test_unknown_render_mode_is_rejected(client, sample_instruction):
    payload = copy.deepcopy(sample_instruction)
    payload["render_mode"] = "video"
    response = _post(client, payload)
    assert response.status_code == 422


def test_configured_provider_is_not_called_for_svg(image_app, sample_instruction):
    client, provider = image_app
    body = _ok(_post(client, sample_instruction))
    assert body["render_mode"] == "svg"
    assert body["asset"]["kind"] == "svg"
    assert provider.calls == []


def test_explicit_svg_mode_preserves_svg_bytes(client, sample_instruction):
    implicit = _ok(_post(client, sample_instruction))
    explicit_payload = copy.deepcopy(sample_instruction)
    explicit_payload["render_mode"] = "svg"
    explicit = _ok(_post(client, explicit_payload))

    assert explicit["render_mode"] == "svg"
    assert explicit["asset"]["content_hash"] == implicit["asset"]["content_hash"]
    assert explicit["svg"] == implicit["svg"]


def test_scene_detail_reports_render_mode(client, sample_instruction):
    created = _ok(_post(client, sample_instruction))
    detail = _ok(client.get(f"{API}/scenes/{created['scene_id']}"))
    assert detail["render_mode"] == "svg"
    assert detail["current_asset"]["kind"] == "svg"


def test_legacy_svg_scene_noop_update_keeps_revision(client, sample_instruction):
    created = _ok(_post(client, sample_instruction))

    # Simulate a scene persisted before render_mode existed.
    with client.app.state.database.session(immediate=True) as connection:
        row = connection.execute(
            "SELECT instruction_json FROM scenes WHERE scene_id = ?",
            (created["scene_id"],),
        ).fetchone()
        legacy = json.loads(row["instruction_json"])
        legacy.pop("render_mode", None)
        legacy_json = json.dumps(legacy, separators=(",", ":"), ensure_ascii=False)
        connection.execute(
            "UPDATE scenes SET instruction_json = ? WHERE scene_id = ?",
            (legacy_json, created["scene_id"]),
        )
        connection.execute(
            "UPDATE scene_revisions SET instruction_json = ? WHERE scene_id = ?",
            (legacy_json, created["scene_id"]),
        )

    updated = _ok(client.post(f"{API}/scenes/{created['scene_id']}/update", json={}))
    assert updated["revision"] == created["revision"]
    assert updated["render_mode"] == "svg"


# ---------------------------------------------------------------------------
# Image rendering and reuse
# ---------------------------------------------------------------------------


def test_image_visualize_returns_image_asset(image_app, sample_instruction, settings):
    client, provider = image_app
    body = _ok(_post(client, _image_instruction(sample_instruction)))

    assert body["render_mode"] == "image"
    assert body["svg"] is None
    assert body["asset"]["kind"] == "image"
    assert body["asset"]["mime_type"] == "image/png"
    assert body["asset"]["provider"] == "fake"
    assert body["cache_hit"] is False
    assert len(provider.calls) == 1

    row = client.app.state.assets.get_row(body["asset"]["asset_id"])
    assert row is not None
    assert row["file_path"].startswith("images/")
    assert row["thumbnail_path"] is None
    assert (settings.assets_dir / row["file_path"]).is_file()


def test_identical_image_request_reuses_asset_without_provider_call(
    image_app, sample_instruction
):
    client, provider = image_app
    first = _ok(_post(client, _image_instruction(sample_instruction)))
    second = _ok(_post(client, _image_instruction(sample_instruction)))

    assert second["cache_hit"] is True
    assert second["asset"]["asset_id"] == first["asset"]["asset_id"]
    assert second["revision"] == first["revision"]
    assert len(provider.calls) == 1


def test_identical_visuals_share_one_image_across_scenes(image_app, sample_instruction):
    client, provider = image_app
    first = _ok(
        _post(client, _image_instruction(sample_instruction, scene_id="image-scene-a"))
    )
    second = _ok(
        _post(client, _image_instruction(sample_instruction, scene_id="image-scene-b"))
    )

    assert first["asset"]["asset_id"] == second["asset"]["asset_id"]
    assert len(provider.calls) == 1
    assert _counts(client)[0] == 1


def test_svg_and_image_assets_do_not_collide(image_app, sample_instruction):
    client, provider = image_app
    svg = _ok(_post(client, sample_instruction))
    image = _ok(_post(client, _image_instruction(sample_instruction)))

    assert svg["asset"]["kind"] == "svg"
    assert image["asset"]["kind"] == "image"
    assert svg["asset"]["asset_id"] != image["asset"]["asset_id"]
    assert len(provider.calls) == 1


def test_image_fingerprint_tracks_generation_inputs(image_app, instruction):
    client, provider = image_app
    service = client.app.state.scene_service
    prompt = build_image_prompt(instruction, template_version="1")

    def fingerprint(visual, built_prompt, size="1024x1024", quality="standard"):
        return service.assets.compute_content_hash(
            service._image_fingerprint(
                visual,
                provider=provider,
                prompt=built_prompt,
                size=size,
                quality=quality,
            )
        )

    base = fingerprint(instruction, prompt)
    assert base == fingerprint(instruction, prompt)

    original_model = provider.model
    provider.model = "fake-image-2"
    try:
        assert fingerprint(instruction, prompt) != base
    finally:
        provider.model = original_model

    assert fingerprint(instruction, prompt, size="512x512") != base
    assert fingerprint(instruction, prompt, quality="high") != base

    template_two = build_image_prompt(instruction, template_version="2")
    assert fingerprint(instruction, template_two) != base

    renamed = instruction.model_copy(update={"title": "A different title"})
    renamed_prompt = build_image_prompt(renamed, template_version="1")
    assert fingerprint(renamed, renamed_prompt) != base


def test_image_metadata_contains_hash_but_not_the_prompt(image_app, sample_instruction):
    client, _provider = image_app
    body = _ok(_post(client, _image_instruction(sample_instruction)))
    row = client.app.state.assets.get_row(body["asset"]["asset_id"])
    metadata = json.loads(row["metadata_json"])

    assert metadata["prompt_hash"]
    assert metadata["prompt_template_version"] == "1"
    assert metadata["size"] == "1024x1024"
    assert metadata["quality"] == "standard"
    assert metadata["render_mode"] == "image"
    assert "prompt" not in metadata
    assert sample_instruction["title"] not in row["metadata_json"]


# ---------------------------------------------------------------------------
# Mode persistence and updates
# ---------------------------------------------------------------------------


def test_image_mode_persists_across_get_scene_and_updates(image_app, sample_instruction):
    client, provider = image_app
    created = _ok(_post(client, _image_instruction(sample_instruction)))

    detail = _ok(client.get(f"{API}/scenes/{created['scene_id']}"))
    assert detail["render_mode"] == "image"
    assert detail["svg"] is None
    assert detail["current_asset"]["kind"] == "image"

    updated = _ok(
        client.post(
            f"{API}/scenes/{created['scene_id']}/update",
            json={"title": "Renamed image scene"},
        )
    )
    assert updated["render_mode"] == "image"
    assert updated["asset"]["kind"] == "image"
    assert updated["revision"] == created["revision"] + 1
    assert len(provider.calls) == 2

    switched = _ok(
        client.post(
            f"{API}/scenes/{created['scene_id']}/update",
            json={"render_mode": "svg"},
        )
    )
    assert switched["render_mode"] == "svg"
    assert switched["asset"]["kind"] == "svg"
    assert switched["svg"]
    assert len(provider.calls) == 2

    # Omitted render_mode keeps the stored mode (now SVG).
    unchanged_mode = _ok(
        client.post(
            f"{API}/scenes/{created['scene_id']}/update",
            json={"title": "Still SVG"},
        )
    )
    assert unchanged_mode["render_mode"] == "svg"
    assert len(provider.calls) == 2


def test_update_can_switch_an_svg_scene_to_image(image_app, sample_instruction):
    client, provider = image_app
    created = _ok(_post(client, sample_instruction))
    assert created["render_mode"] == "svg"

    switched = _ok(
        client.post(
            f"{API}/scenes/{created['scene_id']}/update",
            json={"render_mode": "image"},
        )
    )
    assert switched["render_mode"] == "image"
    assert switched["asset"]["kind"] == "image"
    assert switched["svg"] is None
    assert len(provider.calls) == 1


# ---------------------------------------------------------------------------
# Missing files and provider failures
# ---------------------------------------------------------------------------


def test_stale_image_update_conflict_keeps_asset_and_skips_provider(
    image_app, sample_instruction
):
    """A rejected image update must not spend a provider call or corrupt the asset."""
    client, provider = image_app
    created = _ok(_post(client, _image_instruction(sample_instruction)))
    assert created["revision"] == 1
    assert created["asset"]["kind"] == "image"
    assert len(provider.calls) == 1

    current = _ok(
        client.post(
            f"{API}/scenes/{created['scene_id']}/update",
            json={"expected_revision": 1, "title": "Renamed image scene"},
        )
    )
    assert current["revision"] == 2
    assert len(provider.calls) == 2
    current_asset_id = current["asset"]["asset_id"]

    stale = client.post(
        f"{API}/scenes/{created['scene_id']}/update",
        json={"expected_revision": 1, "title": "Stale rename"},
    )
    assert stale.status_code == 409
    assert len(provider.calls) == 2

    detail = _ok(client.get(f"{API}/scenes/{created['scene_id']}"))
    assert detail["current_revision"] == 2
    assert detail["title"] == "Renamed image scene"
    assert detail["render_mode"] == "image"
    assert detail["current_asset"]["asset_id"] == current_asset_id
    assert client.get(f"{API}/assets/{current_asset_id}/content").status_code == 200
    assert client.get(f"{API}/assets/gc").json()["orphaned_files"] == []


def test_missing_image_file_is_never_regenerated(
    image_app, sample_instruction, settings
):
    client, provider = image_app
    created = _ok(_post(client, _image_instruction(sample_instruction)))
    asset_id = created["asset"]["asset_id"]
    row = client.app.state.assets.get_row(asset_id)
    (settings.assets_dir / row["file_path"]).unlink()
    calls_before = len(provider.calls)

    again = _post(client, _image_instruction(sample_instruction))
    assert again.status_code == 503
    assert "never regenerated" in again.json()["detail"]
    assert len(provider.calls) == calls_before

    detail = _ok(client.get(f"{API}/scenes/{created['scene_id']}"))
    assert detail["svg"] is None
    assert any("not regenerated" in warning for warning in detail["warnings"])

    content = client.get(f"{API}/assets/{asset_id}/content")
    assert content.status_code == 503
    assert len(provider.calls) == calls_before


def test_image_mode_without_a_provider_returns_503(client, sample_instruction):
    response = _post(client, _image_instruction(sample_instruction))
    assert response.status_code == 503
    assert "No image provider" in response.json()["detail"]
    assert _counts(client) == (0, 0)


def test_unavailable_provider_returns_503(tmp_path, sample_instruction):
    provider = UnavailableImageProvider(
        name="openai",
        model="gpt-image-1",
        reason="OPENAI_API_KEY is not set.",
    )
    app = create_app(_settings(tmp_path), image_provider=provider)
    with TestClient(app) as client:
        response = _post(client, _image_instruction(sample_instruction))
        assert response.status_code == 503
        assert "OPENAI_API_KEY is not set." in response.json()["detail"]
        assert _counts(client) == (0, 0)


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (ImageProviderNotConfiguredError("not configured"), 503),
        (ImageProviderUnavailableError("provider down"), 503),
        (ImageProviderAuthError("bad credentials"), 502),
        (ImageProviderRateLimitError("slow down", retry_after=7), 429),
        (ImageProviderTimeoutError("timed out"), 504),
        (ImageProviderContentPolicyError("blocked by policy"), 422),
        (ImageProviderResponseError("malformed response"), 502),
        (ImageProviderConfigurationError("invalid config"), 500),
    ],
)
def test_provider_errors_map_to_safe_http_status(
    tmp_path, sample_instruction, error, expected_status
):
    provider = FakeImageProvider(error=error)
    app = create_app(_settings(tmp_path), image_provider=provider)
    with TestClient(app) as client:
        response = _post(client, _image_instruction(sample_instruction))

        assert response.status_code == expected_status
        assert response.json()["detail"] == str(error)
        if expected_status == 429:
            assert response.headers["Retry-After"] == "7"
        assert _counts(client) == (0, 0)


def test_invalid_provider_bytes_are_rejected(tmp_path, sample_instruction):
    provider = FakeImageProvider(data=b"this is not an image")
    app = create_app(_settings(tmp_path), image_provider=provider)
    with TestClient(app) as client:
        response = _post(client, _image_instruction(sample_instruction))
        assert response.status_code == 502
        assert _counts(client) == (0, 0)


def test_image_content_and_thumbnail_are_served(image_app, sample_instruction):
    client, _provider = image_app
    body = _ok(_post(client, _image_instruction(sample_instruction)))
    asset_id = body["asset"]["asset_id"]

    content = client.get(f"{API}/assets/{asset_id}/content")
    assert content.status_code == 200
    assert content.headers["content-type"].startswith("image/png")
    assert content.content

    thumbnail = client.get(f"{API}/assets/{asset_id}/thumbnail")
    assert thumbnail.status_code == 200
    assert thumbnail.content == content.content


# ---------------------------------------------------------------------------
# Concurrency and staged-file cleanup
# ---------------------------------------------------------------------------


def test_single_flight_coalesces_identical_image_requests(tmp_path, sample_instruction):
    provider = _GatedImageProvider()
    settings = _settings(tmp_path)
    app = create_app(settings, image_provider=provider)

    with TestClient(app) as client:
        first = _image_instruction(sample_instruction, scene_id="single-flight-a")
        second = _image_instruction(sample_instruction, scene_id="single-flight-b")
        responses: list = []

        def post(payload: dict) -> None:
            responses.append(_post(client, payload))

        thread_one = threading.Thread(target=post, args=(first,), name="image-one")
        thread_one.start()
        assert provider.started.wait(timeout=10)

        thread_two = threading.Thread(target=post, args=(second,), name="image-two")
        thread_two.start()

        # A broken single-flight would let the second request enter generate().
        assert not provider.second_entered.wait(timeout=0.5)

        provider.release.set()
        thread_one.join(timeout=10)
        thread_two.join(timeout=10)
        assert not thread_one.is_alive()
        assert not thread_two.is_alive()

        assert [response.status_code for response in responses] == [200, 200]
        bodies = [response.json() for response in responses]
        assert bodies[0]["asset"]["asset_id"] == bodies[1]["asset"]["asset_id"]
        assert len(provider.calls) == 1
        assert provider.max_active == 1
        assert _counts(client)[0] == 1


def test_concurrent_guarded_image_requests_have_one_winner(
    tmp_path, sample_instruction
):
    provider = _GatedImageProvider()
    settings = _settings(tmp_path)
    app = create_app(settings, image_provider=provider)

    with TestClient(app) as client:
        payload = _image_instruction(sample_instruction)
        responses: list = []

        def post() -> None:
            responses.append(_post(client, payload, expected_revision=0))

        thread_one = threading.Thread(target=post, name="guarded-image-one")
        thread_one.start()
        assert provider.started.wait(timeout=10)

        thread_two = threading.Thread(target=post, name="guarded-image-two")
        thread_two.start()
        assert not provider.second_entered.wait(timeout=0.5)

        provider.release.set()
        thread_one.join(timeout=10)
        thread_two.join(timeout=10)
        assert not thread_one.is_alive()
        assert not thread_two.is_alive()

        assert sorted(response.status_code for response in responses) == [200, 409]
        assert len(provider.calls) == 1
        assert _counts(client) == (1, 1)
        files = _files(settings)
        assert len(files) == 1
        assert files[0].startswith("images/")
        assert client.get(f"{API}/assets/gc").json()["orphaned_files"] == []


def test_stale_image_request_discards_staged_files(
    image_app, sample_instruction, settings, monkeypatch
):
    client, provider = image_app
    _ok(_post(client, sample_instruction))  # SVG scene at revision 1
    before_files = _files(settings)
    before_counts = _counts(client)

    service = client.app.state.scene_service
    monkeypatch.setattr(service, "_check_expected_revision", lambda *args, **kwargs: None)

    response = _post(
        client,
        _image_instruction(sample_instruction),
        expected_revision=0,
    )

    assert response.status_code == 409
    assert len(provider.calls) == 1
    assert _files(settings) == before_files
    assert _counts(client) == before_counts
    assert client.get(f"{API}/assets/gc").json()["orphaned_files"] == []
