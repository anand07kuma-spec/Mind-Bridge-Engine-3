import copy
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from app import limits
from app.main import create_app
from fastapi.testclient import TestClient

API = "/api/v1"


def _visualize_response(
    client,
    instruction: dict,
    expected_revision: int | None = None,
):
    params = {} if expected_revision is None else {"expected_revision": expected_revision}
    return client.post(f"{API}/visualize", params=params, json=instruction)


def _visualize(
    client,
    instruction: dict,
    expected_revision: int | None = None,
):
    response = _visualize_response(client, instruction, expected_revision=expected_revision)
    assert response.status_code == 200, response.text
    return response.json()


def test_health_reports_ok(client):
    response = client.get(f"{API}/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["engine"] == "mindbridge-engine3"
    assert body["database"] == "ok"
    assert body["storage"] == "ok"


def test_visualize_returns_renderable_svg(client, sample_instruction):
    body = _visualize(client, sample_instruction)
    assert body["scene_id"] == "bus-tracking-demo"
    assert body["revision"] == 1
    assert body["cache_hit"] is False
    assert body["asset"]["kind"] == "svg"
    assert body["asset"]["width"] and body["asset"]["height"]
    assert body["svg"].lstrip().startswith("<svg")
    assert "Live Bus Tracking System" in body["svg"]
    assert body["warnings"] == []


def test_scene_can_be_fetched_after_visualize(client, sample_instruction):
    body = _visualize(client, sample_instruction)
    scene = client.get(f"{API}/scenes/bus-tracking-demo")
    assert scene.status_code == 200
    data = scene.json()
    assert data["title"] == "Live Bus Tracking System"
    assert data["current_revision"] == 1
    assert len(data["nodes"]) == 5
    assert len(data["edges"]) == 4
    assert len(data["revisions"]) == 1
    assert data["revisions"][0]["asset_id"] == body["asset"]["asset_id"]
    assert data["current_asset"]["asset_id"] == body["asset"]["asset_id"]
    assert "<svg" in data["svg"]


def test_identical_visual_reuses_asset_without_a_new_revision(client, sample_instruction):
    first = _visualize(client, sample_instruction)
    second = _visualize(client, sample_instruction)

    assert second["cache_hit"] is True
    assert second["asset"]["asset_id"] == first["asset"]["asset_id"]
    assert second["asset"]["reuse_count"] == 1
    assert second["revision"] == first["revision"] == 1

    assets = client.get(f"{API}/assets").json()
    assert assets["total"] == 1
    assert assets["items"][0]["reuse_count"] == 1

    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert [revision["revision"] for revision in scene["revisions"]] == [1]
    assert scene["current_revision"] == 1


def test_legitimate_change_after_identical_visual_still_creates_revision(client, sample_instruction):
    _visualize(client, sample_instruction)
    _visualize(client, sample_instruction)

    changed = copy.deepcopy(sample_instruction)
    changed["nodes"][0]["label"] = "Bus 101 (Express)"
    third = _visualize(client, changed)

    assert third["revision"] == 2
    assert third["cache_hit"] is False
    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert [revision["revision"] for revision in scene["revisions"]] == [2, 1]


def test_visualize_with_matching_expected_revision_creates_next_revision(
    client, sample_instruction
):
    _visualize(client, sample_instruction)
    changed = copy.deepcopy(sample_instruction)
    changed["nodes"][0]["label"] = "Bus 101 (Express)"

    body = _visualize(client, changed, expected_revision=1)

    assert body["revision"] == 2
    assert body["cache_hit"] is False
    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert scene["current_revision"] == 2
    assert [revision["revision"] for revision in scene["revisions"]] == [2, 1]
    labels = {node["id"]: node["label"] for node in scene["nodes"]}
    assert labels["bus-1"] == "Bus 101 (Express)"


def test_visualize_with_stale_expected_revision_returns_409_without_writing(
    client, sample_instruction
):
    _visualize(client, sample_instruction)
    applied = client.post(
        f"{API}/scenes/bus-tracking-demo/update",
        json={"expected_revision": 1, "title": "Guarded update title"},
    )
    assert applied.status_code == 200, applied.text

    changed = copy.deepcopy(sample_instruction)
    changed["nodes"][0]["label"] = "Stale visualize label"
    response = _visualize_response(client, changed, expected_revision=1)

    assert response.status_code == 409
    assert "revision 2" in response.json()["detail"]
    assert "revision 1 was expected" in response.json()["detail"]

    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert scene["current_revision"] == 2
    assert scene["title"] == "Guarded update title"
    assert len(scene["revisions"]) == 2
    labels = {node["id"]: node["label"] for node in scene["nodes"]}
    assert labels["bus-1"] == "Bus 101"


def test_visualize_with_expected_revision_zero_creates_absent_scene(
    client, sample_instruction
):
    body = _visualize(client, sample_instruction, expected_revision=0)

    assert body["revision"] == 1
    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert scene["current_revision"] == 1
    assert [revision["revision"] for revision in scene["revisions"]] == [1]


def test_visualize_with_expected_revision_zero_rejects_existing_scene(
    client, sample_instruction
):
    _visualize(client, sample_instruction)
    changed = copy.deepcopy(sample_instruction)
    changed["nodes"][0]["label"] = "Should not replace"
    response = _visualize_response(client, changed, expected_revision=0)

    assert response.status_code == 409
    assert "revision 1" in response.json()["detail"]
    assert "revision 0 was expected" in response.json()["detail"]

    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert scene["current_revision"] == 1
    assert len(scene["revisions"]) == 1
    labels = {node["id"]: node["label"] for node in scene["nodes"]}
    assert labels["bus-1"] == "Bus 101"


def test_visualize_with_nonzero_expected_revision_rejects_absent_scene(
    client, sample_instruction
):
    response = _visualize_response(client, sample_instruction, expected_revision=1)

    assert response.status_code == 409
    assert "revision 0" in response.json()["detail"]
    assert "revision 1 was expected" in response.json()["detail"]
    assert client.get(f"{API}/scenes/bus-tracking-demo").status_code == 404


def test_visualize_with_matching_expected_revision_is_idempotent_for_identical_instruction(
    client, sample_instruction
):
    first = _visualize(client, sample_instruction)
    second = _visualize(client, sample_instruction, expected_revision=1)

    assert second["revision"] == 1
    assert second["cache_hit"] is True
    assert second["asset"]["asset_id"] == first["asset"]["asset_id"]

    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert scene["current_revision"] == 1
    assert len(scene["revisions"]) == 1


def test_visualize_without_expected_revision_remains_legacy_last_write_wins(
    client, sample_instruction
):
    _visualize(client, sample_instruction)
    applied = client.post(
        f"{API}/scenes/bus-tracking-demo/update",
        json={"expected_revision": 1, "title": "Guarded update title"},
    )
    assert applied.status_code == 200, applied.text

    changed = copy.deepcopy(sample_instruction)
    changed["title"] = "Legacy visualize title"
    changed["nodes"][0]["label"] = "Legacy visualize label"
    body = _visualize(client, changed)

    assert body["revision"] == 3
    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert scene["current_revision"] == 3
    assert scene["title"] == "Legacy visualize title"
    assert [revision["revision"] for revision in scene["revisions"]] == [3, 2, 1]
    labels = {node["id"]: node["label"] for node in scene["nodes"]}
    assert labels["bus-1"] == "Legacy visualize label"


def test_visualize_rejects_negative_expected_revision(client, sample_instruction):
    response = _visualize_response(client, sample_instruction, expected_revision=-1)

    assert response.status_code == 422
    assert client.get(f"{API}/scenes/bus-tracking-demo").status_code == 404


def test_visualize_body_expected_revision_is_still_rejected(client, sample_instruction):
    body = copy.deepcopy(sample_instruction)
    body["expected_revision"] = 0
    response = _visualize_response(client, body, expected_revision=0)

    assert response.status_code == 422
    assert client.get(f"{API}/scenes/bus-tracking-demo").status_code == 404


def test_same_visual_in_another_scene_shares_the_asset(client, sample_instruction):
    first = _visualize(client, sample_instruction)
    other = copy.deepcopy(sample_instruction)
    other["scene_id"] = "bus-tracking-copy"
    second = _visualize(client, other)

    assert second["cache_hit"] is True
    assert second["asset"]["asset_id"] == first["asset"]["asset_id"]
    assert client.get(f"{API}/scenes/bus-tracking-copy").status_code == 200

    # The shared asset is re-prefixed per scene, so both SVGs can coexist.
    first_ids = set(re.findall(r'(?<![-\w])id="([^"]+)"', first["svg"]))
    second_ids = set(re.findall(r'(?<![-\w])id="([^"]+)"', second["svg"]))
    assert first_ids and second_ids
    assert first_ids.isdisjoint(second_ids)


def test_changed_label_creates_a_new_asset(client, sample_instruction):
    first = _visualize(client, sample_instruction)
    changed = copy.deepcopy(sample_instruction)
    changed["nodes"][0]["label"] = "Bus 101 (Express)"
    second = _visualize(client, changed)

    assert second["cache_hit"] is False
    assert second["asset"]["asset_id"] != first["asset"]["asset_id"]
    assert client.get(f"{API}/assets").json()["total"] == 2


def test_asset_content_and_thumbnail_are_served(client, sample_instruction):
    body = _visualize(client, sample_instruction)
    asset_id = body["asset"]["asset_id"]

    content = client.get(f"{API}/assets/{asset_id}/content")
    assert content.status_code == 200
    assert content.headers["content-type"].startswith("image/svg+xml")
    assert content.text.lstrip().startswith("<svg")
    assert "immutable" in content.headers["cache-control"]

    thumbnail = client.get(f"{API}/assets/{asset_id}/thumbnail")
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"].startswith("image/svg+xml")

    metadata = client.get(f"{API}/assets/{asset_id}")
    assert metadata.status_code == 200
    assert metadata.json()["asset_id"] == asset_id


def test_asset_files_exist_on_disk(settings, client, sample_instruction):
    _visualize(client, sample_instruction)
    svg_files = list((settings.assets_dir / "svg").glob("*.svg"))
    thumb_files = list((settings.assets_dir / "thumbnails").glob("*.svg"))
    assert len(svg_files) == 1
    assert len(thumb_files) == 1
    assert svg_files[0].read_text(encoding="utf-8").startswith("<svg")


def test_scene_update_adds_node_and_edge(client, sample_instruction):
    _visualize(client, sample_instruction)
    patch = {
        "title": "Live Bus Tracking System v2",
        "nodes_to_add": [{"id": "eta-engine", "label": "ETA Engine", "kind": "service"}],
        "edges_to_add": [
            {"id": "gps-eta", "source": "gps-server", "target": "eta-engine", "label": "Positions"}
        ],
    }
    response = client.post(f"{API}/scenes/bus-tracking-demo/update", json=patch)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"] == 2
    assert body["cache_hit"] is False
    assert "ETA Engine" in body["svg"]

    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert scene["title"] == "Live Bus Tracking System v2"
    assert len(scene["nodes"]) == 6
    assert len(scene["edges"]) == 5


def test_scene_update_can_patch_and_remove(client, sample_instruction):
    _visualize(client, sample_instruction)
    response = client.post(
        f"{API}/scenes/bus-tracking-demo/update",
        json={
            "nodes_to_update": [{"id": "passenger", "label": "Rider App", "kind": "phone"}],
            "nodes_to_remove": ["bus-3"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "Rider App" in body["svg"]
    assert "Bus 103" not in body["svg"]
    assert "bus3-gps" not in body["svg"]

    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert len(scene["nodes"]) == 4
    assert len(scene["edges"]) == 3
    assert scene["current_revision"] == 2


def test_scene_update_validation_errors(client, sample_instruction):
    _visualize(client, sample_instruction)

    missing_node = client.post(
        f"{API}/scenes/bus-tracking-demo/update",
        json={"nodes_to_remove": ["ghost"]},
    )
    assert missing_node.status_code == 422
    assert "ghost" in missing_node.json()["detail"]

    duplicate_node = client.post(
        f"{API}/scenes/bus-tracking-demo/update",
        json={"nodes_to_add": [{"id": "bus-1", "label": "Again", "kind": "bus"}]},
    )
    assert duplicate_node.status_code == 422

    bad_edge = client.post(
        f"{API}/scenes/bus-tracking-demo/update",
        json={"edges_to_add": [{"id": "bad", "source": "bus-1", "target": "nope"}]},
    )
    assert bad_edge.status_code == 422
    assert "unknown target node" in bad_edge.json()["detail"]


def test_update_unknown_scene_returns_404(client):
    response = client.post(f"{API}/scenes/never-created/update", json={"title": "Nope"})
    assert response.status_code == 404


def test_get_unknown_scene_returns_404(client):
    assert client.get(f"{API}/scenes/never-created").status_code == 404


def test_invalid_instruction_is_rejected(client, sample_instruction):
    broken = copy.deepcopy(sample_instruction)
    broken["edges"].append({"id": "broken", "source": "bus-1", "target": "missing"})
    response = client.post(f"{API}/visualize", json=broken)
    assert response.status_code == 422


def test_assets_can_be_filtered(client, sample_instruction):
    _visualize(client, sample_instruction)
    changed = copy.deepcopy(sample_instruction)
    changed["scene_id"] = "other-scene"
    changed["title"] = "Another Scene"
    _visualize(client, changed)

    assert client.get(f"{API}/assets").json()["total"] == 2
    assert client.get(f"{API}/assets", params={"kind": "image"}).json()["total"] == 0
    assert client.get(f"{API}/assets", params={"q": "diagram"}).json()["total"] == 2
    assert client.get(f"{API}/assets", params={"limit": 1}).json()["total"] == 2
    assert len(client.get(f"{API}/assets", params={"limit": 1}).json()["items"]) == 1


def test_unknown_asset_returns_404(client):
    assert client.get(f"{API}/assets/ast_doesnotexist").status_code == 404
    assert client.get(f"{API}/assets/ast_doesnotexist/content").status_code == 404
    assert client.get(f"{API}/assets/not-an-asset/content").status_code == 404


def test_sample_endpoint_serves_demo_instruction(client):
    response = client.get(f"{API}/samples/visual-instruction")
    assert response.status_code == 200
    data = response.json()
    assert data["scene_id"] == "bus-tracking-demo"
    assert len(data["nodes"]) == 5


def test_concurrent_visualize_allocates_unique_revisions(client, sample_instruction):
    def post(index: int):
        data = copy.deepcopy(sample_instruction)
        data["scene_id"] = "concurrent-scene"
        data["nodes"][0]["label"] = f"Bus {index}"
        return client.post(f"{API}/visualize", json=data)

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(post, range(5)))

    assert [response.status_code for response in responses] == [200] * 5
    revisions = sorted(response.json()["revision"] for response in responses)
    assert revisions == [1, 2, 3, 4, 5]

    scene = client.get(f"{API}/scenes/concurrent-scene").json()
    assert scene["current_revision"] == 5
    assert sorted(item["revision"] for item in scene["revisions"]) == [1, 2, 3, 4, 5]


def test_concurrent_guarded_visualizes_have_one_winner(
    client, sample_instruction, monkeypatch
):
    _visualize(client, sample_instruction)

    service = client.app.state.scene_service
    original_render_scene = service._render_scene
    barrier = threading.Barrier(2, timeout=10)

    def synchronized_render_scene(instruction, expected_revision):
        barrier.wait()
        return original_render_scene(instruction, expected_revision=expected_revision)

    labels = ["Bus 101 (guarded A)", "Bus 101 (guarded B)"]

    def post(label: str):
        data = copy.deepcopy(sample_instruction)
        data["nodes"][0]["label"] = label
        return label, _visualize_response(client, data, expected_revision=1)

    with monkeypatch.context() as patch:
        patch.setattr(service, "_render_scene", synchronized_render_scene)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(post, labels))

    statuses = sorted(response.status_code for _, response in results)
    assert statuses == [200, 409], [response.text for _, response in results]

    winner_label, winner = next(
        (label, response) for label, response in results if response.status_code == 200
    )
    loser_label, loser = next(
        (label, response) for label, response in results if response.status_code == 409
    )
    assert winner.json()["revision"] == 2
    assert "revision 1 was expected" in loser.json()["detail"]

    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert scene["current_revision"] == 2
    assert [revision["revision"] for revision in scene["revisions"]] == [2, 1]
    final_labels = {node["id"]: node["label"] for node in scene["nodes"]}
    assert final_labels["bus-1"] == winner_label
    assert final_labels["bus-1"] != loser_label


def test_concurrent_create_only_visualizes_have_one_winner(
    client, sample_instruction, monkeypatch
):
    service = client.app.state.scene_service
    original_render_scene = service._render_scene
    barrier = threading.Barrier(2, timeout=10)

    def synchronized_render_scene(instruction, expected_revision):
        barrier.wait()
        return original_render_scene(instruction, expected_revision=expected_revision)

    labels = ["Bus 101 (create A)", "Bus 101 (create B)"]

    def post(label: str):
        data = copy.deepcopy(sample_instruction)
        data["scene_id"] = "create-race"
        data["nodes"][0]["label"] = label
        return label, _visualize_response(client, data, expected_revision=0)

    with monkeypatch.context() as patch:
        patch.setattr(service, "_render_scene", synchronized_render_scene)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(post, labels))

    statuses = sorted(response.status_code for _, response in results)
    assert statuses == [200, 409], [response.text for _, response in results]

    winner_label, winner = next(
        (label, response) for label, response in results if response.status_code == 200
    )
    loser_label, loser = next(
        (label, response) for label, response in results if response.status_code == 409
    )
    assert winner.json()["revision"] == 1
    assert "revision 0 was expected" in loser.json()["detail"]

    scene = client.get(f"{API}/scenes/create-race").json()
    assert scene["current_revision"] == 1
    assert [revision["revision"] for revision in scene["revisions"]] == [1]
    final_labels = {node["id"]: node["label"] for node in scene["nodes"]}
    assert final_labels["bus-1"] == winner_label
    assert final_labels["bus-1"] != loser_label


def test_guarded_visualize_cannot_overwrite_guarded_update(
    client, sample_instruction, monkeypatch
):
    _visualize(client, sample_instruction)

    service = client.app.state.scene_service
    original_render_scene = service._render_scene
    barrier = threading.Barrier(2, timeout=10)

    def synchronized_render_scene(instruction, expected_revision):
        barrier.wait()
        return original_render_scene(instruction, expected_revision=expected_revision)

    visualize_data = copy.deepcopy(sample_instruction)
    visualize_data["nodes"][0]["label"] = "Guarded visualize label"

    def post_visualize():
        return "visualize", _visualize_response(client, visualize_data, expected_revision=1)

    def post_update():
        return "update", client.post(
            f"{API}/scenes/bus-tracking-demo/update",
            json={"expected_revision": 1, "title": "Guarded update title"},
        )

    with monkeypatch.context() as patch:
        patch.setattr(service, "_render_scene", synchronized_render_scene)
        with ThreadPoolExecutor(max_workers=2) as pool:
            visualize_future = pool.submit(post_visualize)
            update_future = pool.submit(post_update)
            results = [visualize_future.result(), update_future.result()]

    statuses = sorted(response.status_code for _, response in results)
    assert statuses == [200, 409], [response.text for _, response in results]

    winner_kind, winner = next(
        (kind, response) for kind, response in results if response.status_code == 200
    )
    _, loser = next(
        (kind, response) for kind, response in results if response.status_code == 409
    )
    assert winner.json()["revision"] == 2
    assert "revision 1 was expected" in loser.json()["detail"]

    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert scene["current_revision"] == 2
    assert [revision["revision"] for revision in scene["revisions"]] == [2, 1]
    labels = {node["id"]: node["label"] for node in scene["nodes"]}
    if winner_kind == "update":
        assert scene["title"] == "Guarded update title"
        assert labels["bus-1"] == "Bus 101"
    else:
        assert scene["title"] == "Live Bus Tracking System"
        assert labels["bus-1"] == "Guarded visualize label"


def test_concurrent_updates_with_the_same_expected_revision_have_one_winner(
    client, sample_instruction
):
    _visualize(client, sample_instruction)

    def post(index: int):
        return client.post(
            f"{API}/scenes/bus-tracking-demo/update",
            json={
                "expected_revision": 1,
                "nodes_to_add": [
                    {"id": f"svc-{index}", "label": f"Service {index}", "kind": "service"}
                ],
            },
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(post, range(4)))

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 409, 409, 409]

    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert scene["current_revision"] == 2
    assert len(scene["revisions"]) == 2


def test_concurrent_unguarded_updates_do_not_lose_changes(
    client, sample_instruction, monkeypatch
):
    _visualize(client, sample_instruction)

    service = client.app.state.scene_service
    original_render_scene = service._render_scene
    barrier = threading.Barrier(2, timeout=10)

    def synchronized_render_scene(instruction, expected_revision):
        # Both updates have read and merged revision 1 before either persists.
        barrier.wait()
        return original_render_scene(instruction, expected_revision=expected_revision)

    labels = {
        "bus-1": "Bus 101 (northbound)",
        "bus-2": "Bus 202 (southbound)",
    }

    def post(node_id: str):
        return node_id, client.post(
            f"{API}/scenes/bus-tracking-demo/update",
            json={"nodes_to_update": [{"id": node_id, "label": labels[node_id]}]},
        )

    with monkeypatch.context() as patch:
        patch.setattr(service, "_render_scene", synchronized_render_scene)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(post, labels))

    statuses = sorted(response.status_code for _, response in results)
    assert statuses == [200, 409], [response.text for _, response in results]

    winner_id, winner = next(
        (node_id, response) for node_id, response in results if response.status_code == 200
    )
    loser_id, loser = next(
        (node_id, response) for node_id, response in results if response.status_code == 409
    )
    assert winner.json()["revision"] == 2
    # The omitted expected_revision was replaced by the revision the patch was
    # built from, so the loser reports a stale revision rather than losing the
    # update silently.
    assert "revision 1 was expected" in loser.json()["detail"]

    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert scene["current_revision"] == 2
    current_labels = {node["id"]: node["label"] for node in scene["nodes"]}
    assert current_labels[winner_id] == labels[winner_id]
    assert current_labels[loser_id] != labels[loser_id]

    retry = client.post(
        f"{API}/scenes/bus-tracking-demo/update",
        json={
            "expected_revision": scene["current_revision"],
            "nodes_to_update": [{"id": loser_id, "label": labels[loser_id]}],
        },
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["revision"] == 3

    reconciled = client.get(f"{API}/scenes/bus-tracking-demo").json()
    reconciled_labels = {node["id"]: node["label"] for node in reconciled["nodes"]}
    assert reconciled_labels[winner_id] == labels[winner_id]
    assert reconciled_labels[loser_id] == labels[loser_id]


def test_stale_expected_revision_returns_409(client, sample_instruction):
    _visualize(client, sample_instruction)
    applied = client.post(
        f"{API}/scenes/bus-tracking-demo/update",
        json={"expected_revision": 1, "title": "Live Bus Tracking System v2"},
    )
    assert applied.status_code == 200
    assert applied.json()["revision"] == 2

    stale = client.post(
        f"{API}/scenes/bus-tracking-demo/update",
        json={"expected_revision": 1, "title": "Stale write"},
    )
    assert stale.status_code == 409
    assert "revision 2" in stale.json()["detail"]

    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert scene["current_revision"] == 2
    assert scene["title"] == "Live Bus Tracking System v2"


def test_matching_expected_revision_applies_the_update(client, sample_instruction):
    _visualize(client, sample_instruction)
    first = client.post(
        f"{API}/scenes/bus-tracking-demo/update",
        json={"expected_revision": 1, "title": "Version two"},
    )
    assert first.status_code == 200
    second = client.post(
        f"{API}/scenes/bus-tracking-demo/update",
        json={
            "expected_revision": 2,
            "nodes_to_add": [{"id": "eta-engine", "label": "ETA Engine", "kind": "service"}],
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["revision"] == 3


def test_empty_update_is_a_noop(client, sample_instruction):
    _visualize(client, sample_instruction)
    noop = client.post(f"{API}/scenes/bus-tracking-demo/update", json={})
    assert noop.status_code == 200, noop.text
    assert noop.json()["revision"] == 1

    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert scene["current_revision"] == 1
    assert len(scene["revisions"]) == 1


def test_missing_asset_file_is_repaired_and_reported(client, settings, sample_instruction):
    first = _visualize(client, sample_instruction)
    asset_id = first["asset"]["asset_id"]
    for path in (settings.assets_dir / "svg").glob("*.svg"):
        path.unlink()
    for path in (settings.assets_dir / "thumbnails").glob("*.svg"):
        path.unlink()

    repaired = _visualize(client, sample_instruction)

    assert repaired["asset"]["asset_id"] == asset_id
    assert repaired["cache_hit"] is False
    assert repaired["revision"] == 1
    assert any(
        "regenerated and the asset restored" in warning for warning in repaired["warnings"]
    )
    assert list((settings.assets_dir / "svg").glob("*.svg"))
    assert list((settings.assets_dir / "thumbnails").glob("*.svg"))
    content = client.get(f"{API}/assets/{asset_id}/content")
    assert content.status_code == 200
    assert content.text.lstrip().startswith("<svg")


def test_warnings_are_returned_on_fresh_render_and_cache_hit(client, sample_instruction):
    data = copy.deepcopy(sample_instruction)
    data["nodes"][0]["position"] = {"x": 10, "y": 10}

    fresh = _visualize(client, data)
    assert fresh["warnings"]
    assert any("falling back" in warning for warning in fresh["warnings"])

    cached = _visualize(client, data)
    assert cached["cache_hit"] is True
    assert cached["warnings"] == fresh["warnings"]

    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert scene["warnings"] == fresh["warnings"]


def test_two_scenes_rendered_together_have_disjoint_ids(client, sample_instruction):
    first = _visualize(client, sample_instruction)
    other = copy.deepcopy(sample_instruction)
    other["scene_id"] = "transport-overview"
    other["title"] = "Transport Overview"
    second = _visualize(client, other)

    first_ids = set(re.findall(r'(?<![-\w])id="([^"]+)"', first["svg"]))
    second_ids = set(re.findall(r'(?<![-\w])id="([^"]+)"', second["svg"]))
    assert first_ids.isdisjoint(second_ids)

    first_root = re.search(r'aria-labelledby="([^"]+)-title"', first["svg"])
    second_root = re.search(r'aria-labelledby="([^"]+)-title"', second["svg"])
    assert first_root and second_root
    assert first_root.group(1) != second_root.group(1)
    assert f'<title id="{first_root.group(1)}-title">' in first["svg"]
    assert f'<title id="{second_root.group(1)}-title">' in second["svg"]


def test_health_exposes_supported_schema_versions(client):
    body = client.get(f"{API}/health").json()
    assert body["schema_version"] == "1.0"
    assert body["schema_versions"] == ["1.0"]


def test_visualize_defaults_and_echoes_the_schema_version(client, sample_instruction):
    omitted = copy.deepcopy(sample_instruction)
    omitted.pop("schema_version", None)
    body = _visualize(client, omitted)
    assert body["schema_version"] == "1.0"

    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert scene["schema_version"] == "1.0"


def test_schema_version_survives_the_update_round_trip(client, sample_instruction):
    _visualize(client, sample_instruction)
    updated = client.post(
        f"{API}/scenes/bus-tracking-demo/update",
        json={"title": "Live Bus Tracking System v2"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["schema_version"] == "1.0"

    scene = client.get(f"{API}/scenes/bus-tracking-demo").json()
    assert scene["schema_version"] == "1.0"


def test_unsupported_schema_version_returns_422_with_supported_versions(client, sample_instruction):
    broken = copy.deepcopy(sample_instruction)
    broken["schema_version"] = "2.0"
    response = client.post(f"{API}/visualize", json=broken)
    assert response.status_code == 422
    rendered = response.text
    assert "Unsupported schema_version" in rendered
    assert "1.0" in rendered


def test_metadata_limits_are_enforced_over_http(client, sample_instruction):
    too_deep: dict = {}
    cursor = too_deep
    for index in range(limits.METADATA_MAX_DEPTH):
        child: dict = {}
        cursor[f"level-{index}"] = child
        cursor = child

    deep_payload = copy.deepcopy(sample_instruction)
    deep_payload["nodes"][0]["metadata"] = too_deep
    too_deep_response = client.post(f"{API}/visualize", json=deep_payload)
    assert too_deep_response.status_code == 422
    assert "levels deep" in too_deep_response.text

    large_payload = copy.deepcopy(sample_instruction)
    large_payload["nodes"][0]["metadata"] = {"blob": "x" * (limits.METADATA_MAX_BYTES + 1)}
    large_response = client.post(f"{API}/visualize", json=large_payload)
    assert large_response.status_code == 422
    assert "serialize to at most" in large_response.text


def _limited_client(settings, max_request_bytes: int) -> TestClient:
    app = create_app(replace(settings, max_request_bytes=max_request_bytes))
    return TestClient(app)


def test_oversized_request_body_returns_413(settings, sample_instruction):
    with _limited_client(settings, 512) as client:
        response = client.post(f"{API}/visualize", json=sample_instruction)
        assert response.status_code == 413
        assert "512-byte limit" in response.json()["detail"]


def test_chunked_request_body_is_measured_and_rejected(settings):
    chunks = iter([b'{"scene_id": "chunked",', b' "title": "' + b"x" * 250, b'"}'])
    with _limited_client(settings, 128) as client:
        response = client.post(
            f"{API}/visualize",
            content=chunks,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413
        assert "128-byte limit" in response.json()["detail"]


def test_request_id_is_generated_and_echoed(client):
    first = client.get(f"{API}/health")
    second = client.get(f"{API}/health")
    first_id = first.headers.get("x-request-id")
    assert first_id
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,128}", first_id)
    assert first_id != second.headers.get("x-request-id")


def test_valid_incoming_request_id_is_preserved(client):
    response = client.get(f"{API}/health", headers={"X-Request-ID": "trace-abc.123"})
    assert response.headers["x-request-id"] == "trace-abc.123"


def test_invalid_incoming_request_id_is_replaced(client):
    response = client.get(f"{API}/health", headers={"X-Request-ID": "bad id with spaces"})
    assert response.headers["x-request-id"] != "bad id with spaces"
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,128}", response.headers["x-request-id"])


def test_error_responses_carry_a_request_id(client):
    response = client.get(f"{API}/scenes/never-created")
    assert response.status_code == 404
    assert response.headers.get("x-request-id")


def test_api_prefix_is_configurable(settings):
    app = create_app(replace(settings, api_prefix="/engine/v2"))
    with TestClient(app) as client:
        assert client.get("/engine/v2/health").status_code == 200
        assert client.get("/api/v1/health").status_code == 404
        index = client.get("/").json()
        assert index["api_prefix"] == "/engine/v2"
        assert index["schema_versions"] == ["1.0"]
