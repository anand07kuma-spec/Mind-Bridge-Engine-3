"""Obstacle-aware edge routing regression tests (P2-2).

The goldens in ``golden/routing_before.json`` were captured from generator
1.2.0 before routing changed, so they prove which graphs stayed identical and
which gained a reserved routing gutter.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from itertools import pairwise
from pathlib import Path

import pytest
from app.generators.diagram_generator import (
    HEADER_HEIGHT,
    PAGE_PADDING,
    plan_diagram,
)
from app.generators.edge_routing import BACK, CHANNEL, DIRECT, edge_key, plan_routes
from app.generators.svg_generator import GENERATOR_NAME, GENERATOR_VERSION, SvgGenerator
from app.schemas.scene import VisualInstruction
from tests.routing_scenarios import SCENARIOS

SVG_NS = "{http://www.w3.org/2000/svg}"
GOLDEN_PATH = Path(__file__).parent / "golden" / "routing_before.json"
GOLDEN = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
AFTER_PATH = Path(__file__).parent / "golden" / "routing_after.json"
AFTER = json.loads(AFTER_PATH.read_text(encoding="utf-8"))

# Graphs whose edges are all adjacent (or manual): layout and bytes unchanged.
NEUTRAL_SCENARIOS = ("sequential_lr", "sequential_tb", "manual_lr")

# name: (axis, gutter, extra height from sizing the canvas to real levels)
GUTTER_SCENARIOS = {
    "skip_lr": ("height", 40.0, 0.0),
    "skip_tb": ("width", 40.0, 0.0),
    "back_edge_lr": ("height", 40.0, 0.0),
    # Cyclic levels start at 1, which used to place cards off the old canvas.
    "back_edge_tb": ("width", 40.0, 152.0),
    "dense_lr": ("height", 60.0, 0.0),
    "long_label_skip_lr": ("height", 30.0, 0.0),
}

OBSTACLE_SCENARIOS = (
    "skip_lr",
    "skip_tb",
    "back_edge_lr",
    "back_edge_tb",
    "dense_lr",
    "bands_lr",
    "long_label_skip_lr",
    "parallel_lr",
)

LABEL_SCENARIOS = (
    "skip_lr",
    "skip_tb",
    "back_edge_lr",
    "back_edge_tb",
    "dense_lr",
    "bands_lr",
    "long_label_skip_lr",
    "parallel_lr",
    "sample_lr",
)


def _instruction(name: str) -> VisualInstruction:
    return VisualInstruction.model_validate(SCENARIOS[name]())


def _render(name: str):
    return SvgGenerator().generate(_instruction(name))


def _parallel_instruction(
    count: int,
    direction: str = "LR",
    manual: bool = False,
) -> VisualInstruction:
    """One node pair with ``count`` labelled parallel edges."""
    data: dict = {
        "schema_version": "1.0",
        "scene_id": f"parallel-{direction.lower()}-{count}{'-manual' if manual else ''}",
        "title": "Parallel label scenario",
        "options": {"direction": direction},
        "nodes": [
            {"id": "p", "label": "Producer", "kind": "generic"},
            {"id": "q", "label": "Consumer", "kind": "generic"},
        ],
        "edges": [
            {"id": f"pq-{index}", "source": "p", "target": "q", "label": f"lane {index}"}
            for index in range(count)
        ],
    }
    if manual:
        data["nodes"][0]["position"] = {"x": 0, "y": 0}
        data["nodes"][1]["position"] = (
            {"x": 320, "y": 0} if direction == "LR" else {"x": 0, "y": 240}
        )
    return VisualInstruction.model_validate(data)


def _assert_parallel_labels_clear(result, count: int) -> None:
    labels = _svg_labels(result.svg)
    cards = _svg_cards(result.svg)

    assert len(labels) == count
    for label in labels:
        assert label[0] >= 0 and label[1] >= 0
        assert label[0] + label[2] <= result.width
        assert label[1] + label[3] <= result.height
        hits = [node_id for node_id, card in cards.items() if _strict_overlap(label, card)]
        assert hits == [], f"label overlaps cards: {hits}"

    for index, first in enumerate(labels):
        for second in labels[index + 1 :]:
            assert not _strict_overlap(first, second), "parallel labels overlap"

    assert "overlap" not in " ".join(result.warnings).lower()


def _sha(svg: str) -> str:
    return hashlib.sha256(svg.encode("utf-8")).hexdigest()


def _card_rects(layout) -> dict[str, tuple[float, float, float, float]]:
    return {
        node_id: (box.x, box.y, box.width, box.height)
        for node_id, box in layout.nodes.items()
    }


def _inside(rect, point, epsilon: float = 1.0) -> bool:
    x, y, width, height = rect
    px, py = point
    return x + epsilon < px < x + width - epsilon and y + epsilon < py < y + height - epsilon


def _samples(route) -> list[tuple[float, float]]:
    """Dense samples along a polyline or cubic bezier route."""
    points: list[tuple[float, float]] = []
    if route.kind == DIRECT:
        p0, p1, p2, p3 = route.points
        for step in range(41):
            t = step / 40
            u = 1 - t
            points.append(
                (
                    u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0],
                    u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1],
                )
            )
        return points
    for (x1, y1), (x2, y2) in pairwise(route.points):
        for step in range(21):
            t = step / 20
            points.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
    return points


def _svg_cards(svg: str) -> dict[str, tuple[float, float, float, float]]:
    root = ET.fromstring(svg)
    cards: dict[str, tuple[float, float, float, float]] = {}
    for group in root.iter():
        if group.attrib.get("class") != "mb-node":
            continue
        rect = group.find(f"{SVG_NS}rect")
        assert rect is not None
        cards[group.attrib["data-node-id"]] = (
            float(rect.attrib["x"]),
            float(rect.attrib["y"]),
            float(rect.attrib["width"]),
            float(rect.attrib["height"]),
        )
    return cards


def _svg_labels(svg: str) -> list[tuple[float, float, float, float]]:
    root = ET.fromstring(svg)
    labels: list[tuple[float, float, float, float]] = []
    for group in root.iter():
        if group.attrib.get("class") != "mb-edge-label":
            continue
        rect = group.find(f"{SVG_NS}rect")
        assert rect is not None
        labels.append(
            (
                float(rect.attrib["x"]),
                float(rect.attrib["y"]),
                float(rect.attrib["width"]),
                float(rect.attrib["height"]),
            )
        )
    return labels


def _strict_overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return not (
        ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay
    )


# ---------------------------------------------------------------------------
# Golden comparison: before vs after
# ---------------------------------------------------------------------------


def test_golden_snapshot_predates_the_new_routing_version():
    assert GOLDEN["generator"] == f"{GENERATOR_NAME}@1.2.0"
    assert f"{GENERATOR_NAME}@{GENERATOR_VERSION}" == "mindbridge-engine3.svg@1.4.0"
    assert AFTER["generator"] == f"{GENERATOR_NAME}@{GENERATOR_VERSION}"
    assert AFTER["recorded_from"] == "post-parallel-label-fix"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_current_rendering_matches_post_change_golden(name):
    """Locks 1.3.0 routing output; refresh only together with a version bump."""
    entry = AFTER["scenarios"][name]
    result = _render(name)

    assert (result.width, result.height) == (entry["width"], entry["height"])
    assert _sha(result.svg) == entry["svg_sha256"]
    assert result.warnings == entry["warnings"]


@pytest.mark.parametrize("name", NEUTRAL_SCENARIOS)
def test_adjacent_and_manual_graphs_render_byte_identically(name):
    golden = GOLDEN["scenarios"][name]
    result = _render(name)

    assert (result.width, result.height) == (golden["width"], golden["height"])
    assert _sha(result.svg) == golden["svg_sha256"]
    assert result.warnings == golden["warnings"]


@pytest.mark.parametrize("name", sorted(GUTTER_SCENARIOS))
def test_skip_and_back_graphs_gain_a_reserved_gutter(name):
    axis, gutter, extra_height = GUTTER_SCENARIOS[name]
    golden = GOLDEN["scenarios"][name]
    instruction = _instruction(name)
    layout = plan_diagram(instruction)
    result = SvgGenerator().generate(instruction)

    assert layout.gutter == gutter
    assert result.height == golden["height"] + (gutter if axis == "height" else extra_height)
    assert result.width == golden["width"] + (gutter if axis == "width" else 0)

    # The gutter sits between the header and the cards, never on top of them.
    if axis == "height":
        golden_top = min(box[1] for box in golden["boxes"].values())
        assert min(box.y for box in layout.nodes.values()) == golden_top + gutter
    else:
        golden_left = min(box[0] for box in golden["boxes"].values())
        assert min(box.x for box in layout.nodes.values()) == golden_left + gutter
        assert min(box.y for box in layout.nodes.values()) >= HEADER_HEIGHT


def test_sample_scene_only_moves_edge_labels():
    """The sample has adjacent edges only, so routing keeps the old curve."""
    golden = GOLDEN["scenarios"]["sample_lr"]
    result = _render("sample_lr")

    assert (result.width, result.height) == (golden["width"], golden["height"])
    assert result.warnings == golden["warnings"]
    # Labels now centre in the free corridor rather than the arrow-shortened
    # curve midpoint, which used to graze the card by half a pixel.
    assert "Some edge labels overlap node cards" not in " ".join(result.warnings)


# ---------------------------------------------------------------------------
# Obstacle avoidance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", OBSTACLE_SCENARIOS)
def test_routes_never_cross_unrelated_cards(name):
    instruction = _instruction(name)
    layout = plan_diagram(instruction)
    routes = plan_routes(instruction, layout)
    cards = _card_rects(layout)

    for edge in instruction.edges:
        route = routes[edge_key(edge)]
        unrelated = {
            node_id: rect
            for node_id, rect in cards.items()
            if node_id not in {edge.source, edge.target}
        }
        for point in _samples(route):
            hits = [node_id for node_id, rect in unrelated.items() if _inside(rect, point)]
            assert hits == [], f"{name}:{edge.id} crosses {hits}"


@pytest.mark.parametrize("name", LABEL_SCENARIOS)
def test_edge_labels_never_overlap_cards(name):
    result = _render(name)
    cards = _svg_cards(result.svg)
    labels = _svg_labels(result.svg)

    for label in labels:
        assert label[0] >= 0 and label[1] >= 0
        assert label[0] + label[2] <= result.width
        assert label[1] + label[3] <= result.height
        hits = [node_id for node_id, card in cards.items() if _strict_overlap(label, card)]
        assert hits == [], f"{name}: label overlaps {hits}"
    assert "overlap node cards" not in " ".join(result.warnings)


def test_skip_edges_use_the_channel_router():
    instruction = _instruction("skip_lr")
    routes = plan_routes(instruction, plan_diagram(instruction))
    assert routes["ac"].kind == CHANNEL
    assert routes["dc"].kind == CHANNEL

    # Orthogonal channel: right gap, lane, target gap.
    xs = [x for x, _ in routes["ac"].points]
    ys = [y for _, y in routes["ac"].points]
    assert len(set(xs)) == 4
    assert any(ys.count(y) == 2 for y in set(ys))


def test_multi_row_skip_edges_use_a_full_width_band():
    instruction = _instruction("bands_lr")
    layout = plan_diagram(instruction)
    assert layout.gutter == 0.0

    routes = plan_routes(instruction, layout)
    route = routes["af"]
    assert route.kind == CHANNEL

    lane_y = route.points[2][1]
    assert route.points[3][1] == lane_y
    for rect in _card_rects(layout).values():
        assert not (rect[1] < lane_y < rect[1] + rect[3])


def test_top_to_bottom_skip_edges_use_the_channel_router():
    instruction = _instruction("skip_tb")
    routes = plan_routes(instruction, plan_diagram(instruction))
    assert routes["ac"].kind == CHANNEL
    xs = [x for x, _ in routes["ac"].points]
    ys = [y for _, y in routes["ac"].points]
    assert len(set(ys)) == 4
    assert any(xs.count(x) == 2 for x in set(xs))


@pytest.mark.parametrize("name", ["back_edge_lr", "dense_lr"])
def test_back_edges_enter_the_trailing_side(name):
    instruction = _instruction(name)
    layout = plan_diagram(instruction)
    routes = plan_routes(instruction, layout)
    back = [
        (edge, routes[edge_key(edge)])
        for edge in instruction.edges
        if routes[edge_key(edge)].kind == BACK
    ]
    assert back, "expected at least one back edge"

    for edge, route in back:
        target = layout.nodes[edge.target]
        end_x, end_y = route.points[-1]
        assert end_x > target.x + target.width
        assert abs(end_y - target.center_y) < 0.01


def test_top_to_bottom_back_edges_enter_the_trailing_side():
    instruction = _instruction("back_edge_tb")
    layout = plan_diagram(instruction)
    routes = plan_routes(instruction, layout)
    assert routes["ca"].kind == BACK

    target = layout.nodes["a"]
    end_x, end_y = routes["ca"].points[-1]
    assert end_y > target.y + target.height
    assert abs(end_x - target.center_x) < 0.01


def test_parallel_edges_between_the_same_nodes_are_distinct():
    instruction = _instruction("parallel_lr")
    routes = plan_routes(instruction, plan_diagram(instruction))
    paths = [routes[f"pq-{index}"].path for index in range(3)]
    assert len(set(paths)) == 3

    # Distinct control offsets keep each curve visually separate.
    midpoints = {round(routes[f"pq-{index}"].anchors[0][1], 2) for index in range(3)}
    assert len(midpoints) == 3


@pytest.mark.parametrize("direction", ["LR", "TB"])
@pytest.mark.parametrize("count", [2, 3, 6])
def test_parallel_edge_labels_do_not_overlap(direction, count):
    result = SvgGenerator().generate(_parallel_instruction(count, direction))
    _assert_parallel_labels_clear(result, count)


@pytest.mark.parametrize("direction", ["LR", "TB"])
@pytest.mark.parametrize("count", [2, 3, 6])
def test_manual_parallel_edge_labels_do_not_overlap(direction, count):
    result = SvgGenerator().generate(
        _parallel_instruction(count, direction, manual=True)
    )
    _assert_parallel_labels_clear(result, count)


def test_crowded_parallel_labels_are_kept_and_warn():
    result = SvgGenerator().generate(_parallel_instruction(20, "LR"))
    labels = _svg_labels(result.svg)

    # An impossible stack must still render every label and explain the problem.
    assert len(labels) == 20
    assert any(
        "edge labels" in warning.lower()
        and ("canvas" in warning.lower() or "overlap" in warning.lower())
        for warning in result.warnings
    )


def test_parallel_label_placement_is_deterministic():
    instruction = _parallel_instruction(6, "LR")
    first = SvgGenerator().generate(instruction)
    second = SvgGenerator().generate(instruction)

    assert first.svg == second.svg
    assert first.warnings == second.warnings


def test_dense_graph_routing_is_deterministic():
    instruction = _instruction("dense_lr")
    first = SvgGenerator().generate(instruction)
    second = SvgGenerator().generate(instruction)
    assert first.svg == second.svg
    assert first.warnings == second.warnings


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_routing_is_stable_across_repeated_renders(name):
    first = _render(name)
    second = _render(name)
    assert first.svg == second.svg
    assert first.warnings == second.warnings
    assert (first.width, first.height) == (second.width, second.height)


def test_long_skip_label_fits_without_shortening():
    result = _render("long_label_skip_lr")
    assert "Passenger telemetry replication pipeline" in result.svg
    assert not any("Edge labels shortened" in warning for warning in result.warnings)


# ---------------------------------------------------------------------------
# Preserved behaviour
# ---------------------------------------------------------------------------


def test_routing_keeps_sanitization_and_namespacing():
    from app.generators.svg_generator import apply_namespace

    data = copy.deepcopy(SCENARIOS["skip_lr"]())
    data["edges"][3]["label"] = "bypass \x01 route"
    instruction = VisualInstruction.model_validate(data)
    result = SvgGenerator().generate(instruction)

    ET.fromstring(result.svg)
    assert "\x01" not in result.svg
    assert "\ufffd" in result.svg
    assert any("U+FFFD" in warning for warning in result.warnings)

    prefix = re.search(r'aria-labelledby="([^"]+)-title"', result.svg)
    assert prefix is not None
    renamed = apply_namespace(result.svg, "mb-skip-second-scene")
    assert renamed.count('aria-labelledby="mb-skip-second-scene-title"') == 1
    assert 'id="mb-skip-second-scene-title"' in renamed
    assert 'href="#mb-skip-second-scene-icon-' in renamed


def test_layout_warnings_survive_on_cached_scene_reads():
    instruction = _instruction("back_edge_lr")
    warnings = SvgGenerator().warnings_for(instruction)
    assert any("cycle" in warning.lower() for warning in warnings)


def test_tiny_canvas_budget_is_unchanged_for_manual_layouts():
    instruction = _instruction("manual_lr")
    layout = plan_diagram(instruction)
    assert layout.manual is True
    assert layout.gutter == 0.0
    assert layout.width == GOLDEN["scenarios"]["manual_lr"]["width"]
    assert layout.height == GOLDEN["scenarios"]["manual_lr"]["height"]
    assert min(box.x for box in layout.nodes.values()) == PAGE_PADDING
