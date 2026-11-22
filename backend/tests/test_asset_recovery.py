import hashlib
import json

API = "/api/v1"


def _visualize(client, instruction: dict) -> dict:
    response = client.post(f"{API}/visualize", json=instruction)
    assert response.status_code == 200, response.text
    return response.json()


def _delete_asset_files(settings, asset_row) -> None:
    for relative in (asset_row["file_path"], asset_row["thumbnail_path"]):
        if relative:
            (settings.assets_dir / relative).unlink(missing_ok=True)


def test_missing_asset_is_recovered_when_reading_a_scene(client, settings, sample_instruction):
    created = _visualize(client, sample_instruction)
    asset_id = created["asset"]["asset_id"]
    row = client.app.state.assets.get_row(asset_id)
    _delete_asset_files(settings, row)

    scene = client.get(f"{API}/scenes/bus-tracking-demo")
    assert scene.status_code == 200, scene.text
    body = scene.json()
    assert body["svg"].lstrip().startswith("<svg")
    assert any("regenerated and the asset restored" in warning for warning in body["warnings"])
    assert any("fingerprint and checksum verification" in warning for warning in body["warnings"])
    assert (settings.assets_dir / row["file_path"]).is_file()
    assert (settings.assets_dir / row["thumbnail_path"]).is_file()
    assert client.get(f"{API}/assets/{asset_id}/content").status_code == 200

    # Once restored, the next read is a plain verified cache read again.
    second = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert second["warnings"] == []
    assert second["svg"] == body["svg"]


def test_content_and_thumbnail_requests_recover_a_missing_file(client, settings, sample_instruction):
    created = _visualize(client, sample_instruction)
    asset_id = created["asset"]["asset_id"]
    row = client.app.state.assets.get_row(asset_id)
    _delete_asset_files(settings, row)

    content = client.get(f"{API}/assets/{asset_id}/content")
    assert content.status_code == 200, content.text
    assert content.headers["x-mindbridge-asset-recovered"] == "true"
    assert content.text.lstrip().startswith("<svg")

    # The thumbnail was restored by the same recovery; serving it is a normal read.
    thumbnail = client.get(f"{API}/assets/{asset_id}/thumbnail")
    assert thumbnail.status_code == 200
    assert "x-mindbridge-asset-recovered" not in thumbnail.headers

    _delete_asset_files(settings, row)
    recovered_thumb = client.get(f"{API}/assets/{asset_id}/thumbnail")
    assert recovered_thumb.status_code == 200
    assert recovered_thumb.headers["x-mindbridge-asset-recovered"] == "true"


def test_stored_assets_record_checksums_that_match_their_bytes(client, settings, sample_instruction):
    created = _visualize(client, sample_instruction)
    asset_id = created["asset"]["asset_id"]
    row = client.app.state.assets.get_row(asset_id)
    metadata = json.loads(row["metadata_json"])
    main_bytes = (settings.assets_dir / row["file_path"]).read_bytes()
    thumb_bytes = (settings.assets_dir / row["thumbnail_path"]).read_bytes()

    assert metadata["file_sha256"] == hashlib.sha256(main_bytes).hexdigest()
    assert metadata["thumbnail_sha256"] == hashlib.sha256(thumb_bytes).hexdigest()
    assert metadata["generator"].startswith("mindbridge-engine3.svg@")
    assert metadata["namespace"].startswith("mb-")

    # Verified on every read: intact files keep serving.
    assert client.get(f"{API}/assets/{asset_id}/content").status_code == 200
    assert client.get(f"{API}/scenes/bus-tracking-demo").status_code == 200


def test_hash_mismatch_is_rejected_and_never_served(client, settings, sample_instruction):
    created = _visualize(client, sample_instruction)
    asset_id = created["asset"]["asset_id"]
    path = settings.assets_dir / client.app.state.assets.get_row(asset_id)["file_path"]
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg"><text>tampered</text></svg>', encoding="utf-8")

    scene = client.get(f"{API}/scenes/bus-tracking-demo")
    assert scene.status_code == 500
    assert "integrity verification" in scene.json()["detail"]
    assert "tampered" not in scene.text

    content = client.get(f"{API}/assets/{asset_id}/content")
    assert content.status_code == 500
    assert "tampered" not in content.text

    # A cache hit on visualize must also refuse the corrupted file instead of re-serving it.
    visualize = client.post(f"{API}/visualize", json=sample_instruction)
    assert visualize.status_code == 500
    assert "tampered" not in visualize.text


def test_scene_instruction_that_no_longer_matches_the_asset_is_rejected(
    client, settings, sample_instruction
):
    _visualize(client, sample_instruction)
    database = client.app.state.database
    with database.session(immediate=True) as connection:
        row = connection.execute(
            "SELECT instruction_json FROM scenes WHERE scene_id = ?", ("bus-tracking-demo",)
        ).fetchone()
        data = json.loads(row["instruction_json"])
        data["title"] = "A different visual"
        connection.execute(
            "UPDATE scenes SET title = ?, instruction_json = ? WHERE scene_id = ?",
            (data["title"], json.dumps(data), "bus-tracking-demo"),
        )

    response = client.get(f"{API}/scenes/bus-tracking-demo")
    assert response.status_code == 500
    assert "fingerprint" in response.json()["detail"]


def test_missing_asset_without_a_stored_instruction_returns_503(client, settings):
    asset = client.app.state.assets.create_asset(
        kind="svg",
        data=b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        content_hash="0" * 64,
        extension="svg",
        mime_type="image/svg+xml",
        provider="engine3-svg",
        width=10,
        height=10,
    )
    row = client.app.state.assets.get_row(asset["asset_id"])
    _delete_asset_files(settings, row)

    response = client.get(f"{API}/assets/{asset['asset_id']}/content")
    assert response.status_code == 503
    assert "could not be recovered" in response.json()["detail"]
    assert not (settings.assets_dir / row["file_path"]).exists()


def test_asset_from_another_generator_version_is_not_fabricated(client, settings, sample_instruction):
    created = _visualize(client, sample_instruction)
    asset_id = created["asset"]["asset_id"]
    row = client.app.state.assets.get_row(asset_id)
    metadata = json.loads(row["metadata_json"])
    metadata["generator"] = "mindbridge-engine3.svg@0.0.1"
    with client.app.state.database.session(immediate=True) as connection:
        connection.execute(
            "UPDATE assets SET metadata_json = ? WHERE asset_id = ?",
            (json.dumps(metadata), asset_id),
        )
    _delete_asset_files(settings, row)

    response = client.get(f"{API}/assets/{asset_id}/content")
    assert response.status_code == 503
    assert "could not be recovered" in response.json()["detail"]
    assert "0.0.1" in response.json()["detail"]
    assert not (settings.assets_dir / row["file_path"]).exists()


def test_recovery_warnings_propagate_through_the_visualize_response(
    client, settings, sample_instruction
):
    created = _visualize(client, sample_instruction)
    row = client.app.state.assets.get_row(created["asset"]["asset_id"])
    _delete_asset_files(settings, row)

    repaired = _visualize(client, sample_instruction)
    assert repaired["asset"]["asset_id"] == created["asset"]["asset_id"]
    assert repaired["cache_hit"] is False
    assert any("regenerated and the asset restored" in warning for warning in repaired["warnings"])
    assert any("verification after recovery" in warning for warning in repaired["warnings"])


def test_gc_report_is_dry_run_and_never_modifies_state(client, settings, sample_instruction):
    created = _visualize(client, sample_instruction)
    assets = client.app.state.assets

    orphan = assets.create_asset(
        kind="svg",
        data=b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        content_hash="1" * 64,
        extension="svg",
        mime_type="image/svg+xml",
        provider="engine3-svg",
        width=10,
        height=10,
    )
    unresolved = assets.create_asset(
        kind="svg",
        data=b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        content_hash="2" * 64,
        extension="svg",
        mime_type="image/svg+xml",
        provider="engine3-svg",
        width=10,
        height=10,
    )
    unresolved_row = assets.get_row(unresolved["asset_id"])
    _delete_asset_files(settings, unresolved_row)
    stray = settings.assets_dir / "svg" / "stray.svg"
    stray.write_text("<svg/>", encoding="utf-8")

    def snapshot():
        with client.app.state.database.session() as connection:
            rows = [
                tuple(row)
                for row in connection.execute(
                    "SELECT asset_id, content_hash, file_path, thumbnail_path, metadata_json, "
                    "reuse_count, last_used_at FROM assets ORDER BY asset_id"
                ).fetchall()
            ]
            revisions = connection.execute("SELECT COUNT(*) AS count FROM scene_revisions").fetchone()[
                "count"
            ]
        files = sorted(
            (path.relative_to(settings.assets_dir).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
            for path in settings.assets_dir.rglob("*")
            if path.is_file()
        )
        return rows, revisions, files

    before = snapshot()
    response = client.get(f"{API}/assets/gc")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["dry_run"] is True
    assert report["asset_count"] == 3
    assert report["referenced_asset_count"] == 1

    orphaned_ids = {item["asset_id"] for item in report["orphaned_assets"]}
    assert orphaned_ids == {orphan["asset_id"], unresolved["asset_id"]}
    assert created["asset"]["asset_id"] not in orphaned_ids
    assert "svg/stray.svg" in report["orphaned_files"]
    assert any(
        missing.startswith(f"{unresolved['asset_id']}:") for missing in report["missing_files"]
    )

    assert snapshot() == before
    assert stray.is_file()
