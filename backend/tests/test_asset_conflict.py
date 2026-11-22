import copy
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

API = "/api/v1"


def _visualize(client, instruction: dict, expected_revision: int | None = None):
    params = {} if expected_revision is None else {"expected_revision": expected_revision}
    return client.post(f"{API}/visualize", params=params, json=instruction)


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def _asset_state(client):
    with client.app.state.database.session() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT asset_id, content_hash, reuse_count, last_used_at FROM assets "
                "ORDER BY asset_id"
            ).fetchall()
        ]
        revisions = int(
            connection.execute("SELECT COUNT(*) AS count FROM scene_revisions").fetchone()["count"]
        )
    return rows, revisions


def _files(settings):
    return sorted(
        path.relative_to(settings.assets_dir).as_posix()
        for path in settings.assets_dir.rglob("*")
        if path.is_file()
    )


def _gc(client):
    return client.get(f"{API}/assets/gc").json()


def _revision_asset(client, scene_id: str, revision: int) -> str:
    with client.app.state.database.session() as connection:
        row = connection.execute(
            "SELECT asset_id FROM scene_revisions WHERE scene_id = ? AND revision = ?",
            (scene_id, revision),
        ).fetchone()
    assert row is not None
    return str(row["asset_id"])


def _synchronized_render(service, monkeypatch):
    original = service._render_scene
    barrier = threading.Barrier(2, timeout=10)

    def synchronized(instruction, expected_revision):
        barrier.wait()
        return original(instruction, expected_revision=expected_revision)

    monkeypatch.setattr(service, "_render_scene", synchronized)


def test_stale_guarded_visualize_creates_no_asset(client, settings, sample_instruction):
    _ok(_visualize(client, sample_instruction))
    applied = client.post(
        f"{API}/scenes/bus-tracking-demo/update",
        json={"expected_revision": 1, "title": "Advanced"},
    )
    assert applied.status_code == 200, applied.text

    before_assets, before_revisions = _asset_state(client)
    before_files = _files(settings)

    changed = copy.deepcopy(sample_instruction)
    changed["nodes"][0]["label"] = "Bus 999"
    response = _visualize(client, changed, expected_revision=1)

    assert response.status_code == 409
    assert _asset_state(client) == (before_assets, before_revisions)
    assert _files(settings) == before_files
    assert _gc(client)["orphaned_files"] == []


def test_stale_guarded_identical_visualize_does_not_touch_asset(client, sample_instruction):
    _ok(_visualize(client, sample_instruction))
    applied = client.post(
        f"{API}/scenes/bus-tracking-demo/update",
        json={"expected_revision": 1, "title": "Advanced"},
    )
    assert applied.status_code == 200, applied.text

    before_assets, before_revisions = _asset_state(client)
    response = _visualize(client, sample_instruction, expected_revision=1)

    assert response.status_code == 409
    assert _asset_state(client) == (before_assets, before_revisions)


def test_concurrent_create_only_race_leaves_no_loser_asset(
    client, settings, sample_instruction, monkeypatch
):
    service = client.app.state.scene_service
    _synchronized_render(service, monkeypatch)

    labels = ["Bus 101 (create A)", "Bus 101 (create B)"]

    def post(label: str):
        data = copy.deepcopy(sample_instruction)
        data["scene_id"] = "create-race"
        data["nodes"][0]["label"] = label
        return _visualize(client, data, expected_revision=0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(post, labels))

    assert sorted(response.status_code for response in responses) == [200, 409]
    rows, revisions = _asset_state(client)
    assert len(rows) == 1
    assert revisions == 1
    assert len(_files(settings)) == 2  # SVG + thumbnail
    assert _gc(client)["orphaned_files"] == []


def test_concurrent_identical_guarded_visualizes_have_one_winner(
    client, settings, sample_instruction, monkeypatch
):
    _ok(_visualize(client, sample_instruction))
    service = client.app.state.scene_service
    _synchronized_render(service, monkeypatch)

    changed = copy.deepcopy(sample_instruction)
    changed["nodes"][0]["label"] = "Bus 101 (identical race)"

    def post():
        return _visualize(client, changed, expected_revision=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(), range(2)))

    assert sorted(response.status_code for response in responses) == [200, 409]
    rows, revisions = _asset_state(client)
    assert len(rows) == 2  # original asset + the new visual
    assert revisions == 2
    assert _gc(client)["orphaned_files"] == []


def test_concurrent_identical_creates_share_one_asset(
    client, settings, sample_instruction, monkeypatch
):
    service = client.app.state.scene_service
    original_find = service.assets.find_exact
    barrier = threading.Barrier(2, timeout=10)
    calls = {"count": 0}
    lock = threading.Lock()

    def synchronized_find(*args, **kwargs):
        barrier.wait()
        with lock:
            calls["count"] += 1
            if calls["count"] <= 2:
                return None
        return original_find(*args, **kwargs)

    monkeypatch.setattr(service.assets, "find_exact", synchronized_find)

    def post(scene_id: str):
        data = copy.deepcopy(sample_instruction)
        data["scene_id"] = scene_id
        return _visualize(client, data)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(post, ["shared-a", "shared-b"]))

    assert [response.status_code for response in responses] == [200, 200]
    rows, revisions = _asset_state(client)
    assert len(rows) == 1
    assert revisions == 2
    assert rows[0]["reuse_count"] == 0
    shared_id = rows[0]["asset_id"]
    assert _revision_asset(client, "shared-a", 1) == shared_id
    assert _revision_asset(client, "shared-b", 1) == shared_id
    assert _gc(client)["orphaned_files"] == []


def test_losing_visualize_cannot_delete_a_shared_asset(
    client, settings, sample_instruction, monkeypatch
):
    shared = copy.deepcopy(sample_instruction)
    shared["scene_id"] = "shared-owner"
    shared["nodes"][0]["label"] = "Bus 101 (shared)"
    shared_asset = _ok(_visualize(client, shared))["asset"]["asset_id"]
    shared_row = client.app.state.assets.get_row(shared_asset)
    shared_files = {shared_row["file_path"], shared_row["thumbnail_path"]}

    _ok(_visualize(client, sample_instruction))

    service = client.app.state.scene_service
    original_find = service.assets.find_exact
    calls = {"count": 0}

    def miss_once(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return original_find(*args, **kwargs)

    monkeypatch.setattr(service.assets, "find_exact", miss_once)
    monkeypatch.setattr(service, "_check_expected_revision", lambda *args, **kwargs: None)

    changed = copy.deepcopy(shared)
    changed["scene_id"] = "bus-tracking-demo"
    response = _visualize(client, changed, expected_revision=0)

    assert response.status_code == 409
    assert client.app.state.assets.get_row(shared_asset) is not None
    for relative in shared_files:
        assert (settings.assets_dir / relative).is_file()
    assert client.get(f"{API}/scenes/shared-owner").status_code == 200
    assert _gc(client)["orphaned_files"] == []


def test_adopting_visualize_discards_only_its_own_staged_files(
    client, settings, sample_instruction, monkeypatch
):
    shared = copy.deepcopy(sample_instruction)
    shared["scene_id"] = "shared-owner"
    shared["nodes"][0]["label"] = "Bus 101 (shared)"
    shared_asset = _ok(_visualize(client, shared))["asset"]["asset_id"]
    shared_row = client.app.state.assets.get_row(shared_asset)
    shared_files = {shared_row["file_path"], shared_row["thumbnail_path"]}

    _ok(_visualize(client, sample_instruction))

    service = client.app.state.scene_service
    original_find = service.assets.find_exact
    calls = {"count": 0}

    def miss_once(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return original_find(*args, **kwargs)

    monkeypatch.setattr(service.assets, "find_exact", miss_once)

    adopting = copy.deepcopy(shared)
    adopting["scene_id"] = "bus-tracking-demo"
    response = _ok(_visualize(client, adopting, expected_revision=1))

    assert response["asset"]["asset_id"] == shared_asset
    assert _revision_asset(client, "bus-tracking-demo", 2) == shared_asset
    assert client.app.state.assets.get_row(shared_asset) is not None
    for relative in shared_files:
        assert (settings.assets_dir / relative).is_file()
    assert _gc(client)["orphaned_files"] == []


def test_guarded_visualize_losing_to_update_has_no_asset_side_effect(
    client, settings, sample_instruction, monkeypatch
):
    _ok(_visualize(client, sample_instruction))
    service = client.app.state.scene_service
    original_persist = service._persist_scene
    visualize_ready = threading.Event()
    update_done = threading.Event()

    def delayed_persist(instruction, asset_id, prepared=None, expected_revision=None):
        if instruction.nodes[0].label == "Guarded visualize loser":
            visualize_ready.set()
            assert update_done.wait(timeout=10)
        return original_persist(
            instruction,
            asset_id,
            prepared=prepared,
            expected_revision=expected_revision,
        )

    changed = copy.deepcopy(sample_instruction)
    changed["nodes"][0]["label"] = "Guarded visualize loser"
    result: dict = {}
    before_files = _files(settings)

    def run_visualize():
        result["response"] = _visualize(client, changed, expected_revision=1)

    with monkeypatch.context() as patch:
        patch.setattr(service, "_persist_scene", delayed_persist)
        thread = threading.Thread(target=run_visualize, name="visualize-thread")
        thread.start()
        assert visualize_ready.wait(timeout=10)

        update = client.post(
            f"{API}/scenes/bus-tracking-demo/update",
            json={
                "expected_revision": 1,
                "nodes_to_update": [{"id": "gps-server", "metadata": {"role": "winner"}}],
            },
        )
        assert update.status_code == 200, update.text
        after_update = _asset_state(client)

        update_done.set()
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert result["response"].status_code == 409
    assert _asset_state(client) == after_update
    assert _files(settings) == before_files
    assert _gc(client)["orphaned_files"] == []


def test_successful_cache_hit_increments_reuse_count_once(client, sample_instruction):
    first = _ok(_visualize(client, sample_instruction))
    assert first["asset"]["reuse_count"] == 0

    second = _ok(_visualize(client, sample_instruction))

    assert second["cache_hit"] is True
    assert second["asset"]["asset_id"] == first["asset"]["asset_id"]
    assert second["asset"]["reuse_count"] == 1
    rows, revisions = _asset_state(client)
    assert len(rows) == 1
    assert rows[0]["reuse_count"] == 1
    assert revisions == 1


def test_successful_recovery_increments_reuse_count_once(
    client, settings, sample_instruction
):
    created = _ok(_visualize(client, sample_instruction))
    row = client.app.state.assets.get_row(created["asset"]["asset_id"])
    for relative in (row["file_path"], row["thumbnail_path"]):
        (settings.assets_dir / relative).unlink()

    repaired = _ok(_visualize(client, sample_instruction))

    assert repaired["asset"]["asset_id"] == created["asset"]["asset_id"]
    assert repaired["cache_hit"] is False
    assert repaired["asset"]["reuse_count"] == 1
    assert any("regenerated and the asset restored" in warning for warning in repaired["warnings"])
    rows, revisions = _asset_state(client)
    assert len(rows) == 1
    assert rows[0]["reuse_count"] == 1
    assert revisions == 1


def test_stale_guard_recovery_repairs_file_without_touching(
    client, settings, sample_instruction, monkeypatch
):
    created = _ok(_visualize(client, sample_instruction))
    applied = client.post(
        f"{API}/scenes/bus-tracking-demo/update",
        json={"expected_revision": 1, "title": "Advanced"},
    )
    assert applied.status_code == 200, applied.text

    row = client.app.state.assets.get_row(created["asset"]["asset_id"])
    for relative in (row["file_path"], row["thumbnail_path"]):
        (settings.assets_dir / relative).unlink()

    before_assets, before_revisions = _asset_state(client)
    service = client.app.state.scene_service
    monkeypatch.setattr(service, "_check_expected_revision", lambda *args, **kwargs: None)

    response = _visualize(client, sample_instruction, expected_revision=1)

    assert response.status_code == 409
    assert (settings.assets_dir / row["file_path"]).is_file()
    assert (settings.assets_dir / row["thumbnail_path"]).is_file()
    assert _asset_state(client) == (before_assets, before_revisions)


def test_persistence_uses_transaction_scoped_asset_helpers(
    client, sample_instruction, monkeypatch
):
    service = client.app.state.scene_service
    state = {"persisting": False}
    seen: list[tuple[str, sqlite3.Connection]] = []

    original_persist = service._persist_scene

    def tracked_persist(*args, **kwargs):
        state["persisting"] = True
        try:
            return original_persist(*args, **kwargs)
        finally:
            state["persisting"] = False

    original_touch = service.assets.touch
    original_find = service.assets.find_exact

    def guarded_touch(*args, **kwargs):
        assert not state["persisting"], "touch() opened a nested connection during persistence"
        return original_touch(*args, **kwargs)

    def guarded_find(*args, **kwargs):
        assert not state["persisting"], "find_exact() opened a nested connection during persistence"
        return original_find(*args, **kwargs)

    original_insert = service.assets.insert_prepared
    original_touch_in = service.assets.touch_in

    def record_insert(connection, prepared):
        assert isinstance(connection, sqlite3.Connection)
        seen.append(("insert", connection))
        return original_insert(connection, prepared)

    def record_touch(connection, asset_id):
        assert isinstance(connection, sqlite3.Connection)
        seen.append(("touch", connection))
        return original_touch_in(connection, asset_id)

    monkeypatch.setattr(service, "_persist_scene", tracked_persist)
    monkeypatch.setattr(service.assets, "touch", guarded_touch)
    monkeypatch.setattr(service.assets, "find_exact", guarded_find)
    monkeypatch.setattr(service.assets, "insert_prepared", record_insert)
    monkeypatch.setattr(service.assets, "touch_in", record_touch)

    _ok(_visualize(client, sample_instruction))
    _ok(_visualize(client, sample_instruction))

    assert [kind for kind, _ in seen] == ["insert", "touch"]
