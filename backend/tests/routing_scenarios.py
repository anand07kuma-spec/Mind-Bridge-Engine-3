"""Deterministic graph scenarios shared by the routing tests and snapshot capture.

Kept free of app imports so the same definitions can drive a one-off snapshot
script before routing changes and the regression tests afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

SAMPLE_PATH = Path(__file__).resolve().parents[2] / "sample_data" / "bus_tracking.json"


def _instruction(scene_id: str, nodes: list[tuple[str, str]], edges: list[dict], **options) -> dict:
    return {
        "schema_version": "1.0",
        "scene_id": scene_id,
        "title": f"Routing scenario {scene_id}",
        "options": {"direction": "LR", **options},
        "nodes": [{"id": node_id, "label": label, "kind": "generic"} for node_id, label in nodes],
        "edges": edges,
    }


def sequential_lr() -> dict:
    return _instruction(
        "sequential",
        [("a", "Ingest"), ("b", "Normalize"), ("c", "Publish")],
        [
            {"id": "ab", "source": "a", "target": "b"},
            {"id": "bc", "source": "b", "target": "c"},
        ],
    )


def sequential_tb() -> dict:
    data = sequential_lr()
    data["scene_id"] = "sequential-tb"
    data["options"]["direction"] = "TB"
    return data


def skip_lr() -> dict:
    return _instruction(
        "skip",
        [("a", "Source A"), ("b", "Middle B"), ("c", "Sink C"), ("d", "Source D")],
        [
            {"id": "ab", "source": "a", "target": "b"},
            {"id": "db", "source": "d", "target": "b", "label": "mirror"},
            {"id": "bc", "source": "b", "target": "c"},
            {"id": "ac", "source": "a", "target": "c", "label": "bypass"},
            {"id": "dc", "source": "d", "target": "c", "label": "fallback"},
        ],
    )


def skip_tb() -> dict:
    data = skip_lr()
    data["scene_id"] = "skip-tb"
    data["options"]["direction"] = "TB"
    return data


def back_edge_lr() -> dict:
    return _instruction(
        "back-edge",
        [("a", "Collect"), ("b", "Process"), ("c", "Deliver")],
        [
            {"id": "ab", "source": "a", "target": "b"},
            {"id": "bc", "source": "b", "target": "c"},
            {"id": "ca", "source": "c", "target": "a", "label": "retry"},
        ],
    )


def back_edge_tb() -> dict:
    data = back_edge_lr()
    data["scene_id"] = "back-edge-tb"
    data["options"]["direction"] = "TB"
    return data


def parallel_lr() -> dict:
    return _instruction(
        "parallel",
        [("p", "Producer"), ("q", "Consumer")],
        [
            {"id": f"pq-{index}", "source": "p", "target": "q", "label": f"lane {index}"}
            for index in range(3)
        ],
    )


def dense_lr() -> dict:
    return _instruction(
        "dense",
        [
            ("a", "Alpha"), ("b", "Bravo"), ("c", "Charlie"),
            ("d", "Delta"), ("e", "Echo"), ("f", "Foxtrot"),
        ],
        [
            {"id": "ab", "source": "a", "target": "b"},
            {"id": "de", "source": "d", "target": "e"},
            {"id": "bc", "source": "b", "target": "c"},
            {"id": "ef", "source": "e", "target": "f"},
            {"id": "af", "source": "a", "target": "f", "label": "shortcut"},
            {"id": "cd", "source": "c", "target": "d", "label": "feedback"},
            {"id": "bf", "source": "b", "target": "f", "label": "wide link"},
            {"id": "fb", "source": "f", "target": "b"},
        ],
    )


def bands_lr() -> dict:
    """Two aligned rows, so a full-width band exists between them."""
    return _instruction(
        "bands",
        [
            ("a", "Alpha"), ("b", "Bravo"), ("c", "Charlie"),
            ("d", "Delta"), ("e", "Echo"), ("f", "Foxtrot"),
        ],
        [
            {"id": "ac", "source": "a", "target": "c"},
            {"id": "ce", "source": "c", "target": "e"},
            {"id": "bd", "source": "b", "target": "d"},
            {"id": "df", "source": "d", "target": "f"},
            {"id": "af", "source": "a", "target": "f", "label": "skip row"},
        ],
    )


def long_label_skip_lr() -> dict:
    return _instruction(
        "long-skip-label",
        [("a", "Collector"), ("b", "Queue"), ("c", "Archive")],
        [
            {"id": "ab", "source": "a", "target": "b"},
            {"id": "bc", "source": "b", "target": "c"},
            {
                "id": "ac",
                "source": "a",
                "target": "c",
                "label": "Passenger telemetry replication pipeline",
            },
        ],
    )


def manual_lr() -> dict:
    data = sequential_lr()
    data["scene_id"] = "manual"
    positions = {"a": {"x": 0, "y": 0}, "b": {"x": 320, "y": 140}, "c": {"x": 640, "y": 0}}
    for node in data["nodes"]:
        node["position"] = positions[node["id"]]
    return data


def sample_lr() -> dict:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


SCENARIOS = {
    "sequential_lr": sequential_lr,
    "sequential_tb": sequential_tb,
    "skip_lr": skip_lr,
    "skip_tb": skip_tb,
    "back_edge_lr": back_edge_lr,
    "back_edge_tb": back_edge_tb,
    "parallel_lr": parallel_lr,
    "dense_lr": dense_lr,
    "bands_lr": bands_lr,
    "long_label_skip_lr": long_label_skip_lr,
    "manual_lr": manual_lr,
    "sample_lr": sample_lr,
}
