"""Obstacle-aware, deterministic edge routing.

Adjacent layers keep the original direct bezier so simple graphs render
byte-identically. Edges that skip a layer, or travel backwards, are routed
through free bands:

* row/column bands between cards that span the whole diagram (``horizontal_bands``
  / ``vertical_bands`` from the layout planner),
* or a reserved gutter above/left of the content when a graph has only one
  row/column and therefore no inter-card band.

Back edges enter the target from its *trailing* side, so their path stays
visually distinct from forward edges. Every coordinate is derived from the
layout, so identical instructions always produce identical paths.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.generators.diagram_generator import (
    HEADER_HEIGHT,
    LAYER_GAP_LR,
    LAYER_GAP_TB,
    MIN_ROUTING_BAND,
    PAGE_PADDING,
    ROUTING_GUTTER_LANES,
    DiagramLayout,
    NodeBox,
    horizontal_bands,
    vertical_bands,
)
from app.schemas.scene import EdgeSpec, VisualInstruction

DIRECT = "direct"
CHANNEL = "channel"
BACK = "back"

_DIRECT_TS = (0.5, 0.38, 0.62, 0.26, 0.74)
_LANE_TS = (0.5, 0.35, 0.65, 0.2, 0.8)
_PARALLEL_SPACING = 14.0
_LANE_SPACING = 6.0
_HALF_BAND = MIN_ROUTING_BAND / 2 - 1.0


@dataclass(frozen=True)
class EdgeRoute:
    path: str
    kind: str
    points: tuple[tuple[float, float], ...]
    anchors: tuple[tuple[float, float], ...]


def edge_key(edge: EdgeSpec) -> str:
    return edge.id or f"{edge.source}->{edge.target}"


def plan_routes(
    instruction: VisualInstruction,
    layout: DiagramLayout,
) -> dict[str, EdgeRoute]:
    routes: dict[str, EdgeRoute] = {}
    groups: dict[tuple[str, str], list[str]] = {}
    for edge in instruction.edges:
        groups.setdefault((edge.source, edge.target), []).append(edge_key(edge))

    bands = []
    if not layout.manual and layout.levels:
        bands = (
            horizontal_bands(layout.nodes)
            if layout.direction == "LR"
            else vertical_bands(layout.nodes)
        )

    slot = 0
    for edge in instruction.edges:
        source = layout.nodes.get(edge.source)
        target = layout.nodes.get(edge.target)
        if source is None or target is None:
            continue

        key = edge_key(edge)
        group = groups[(edge.source, edge.target)]
        pair_index = group.index(key)
        pair_count = len(group)

        if layout.manual or not layout.levels:
            routes[key] = _direct_route(
                source, target, layout.direction, pair_index, pair_count, offset=False
            )
            continue

        if _adjacent(source, target, layout.direction):
            routes[key] = _direct_route(source, target, layout.direction, pair_index, pair_count)
            continue

        lane = None
        anchor_lane = None
        if bands:
            desired = (
                (source.center_y + target.center_y) / 2
                if layout.direction == "LR"
                else (source.center_x + target.center_x) / 2
            )
            anchor_lane = min(bands, key=lambda value: (abs(value - desired), value))
            lane = anchor_lane + _pair_offset(
                pair_index, pair_count, _LANE_SPACING, _HALF_BAND
            )
        else:
            lane = _gutter_lane(layout, slot)
            anchor_lane = lane
            slot += 1

        if _backward(source, target, layout.direction):
            routes[key] = _back_route(source, target, layout, lane, anchor_lane)
        else:
            routes[key] = _channel_route(source, target, layout, lane, anchor_lane)

    return routes


def _adjacent(source: NodeBox, target: NodeBox, direction: str) -> bool:
    if direction == "LR":
        return target.x > source.x and target.x - (source.x + source.width) <= LAYER_GAP_LR + 1.0
    return target.y > source.y and target.y - (source.y + source.height) <= LAYER_GAP_TB + 1.0


def _backward(source: NodeBox, target: NodeBox, direction: str) -> bool:
    if direction == "LR":
        return target.x <= source.x
    return target.y <= source.y


def _pair_offset(index: int, count: int, spacing: float, limit: float) -> float:
    if count < 2:
        return 0.0
    step = min(spacing, (2 * limit) / (count - 1))
    offset = (index - (count - 1) / 2) * step
    return max(-limit, min(limit, offset))


def _gutter_lane(layout: DiagramLayout, slot: int) -> float:
    """Distinct lane inside the reserved gutter, never over a card."""
    fraction = (slot % ROUTING_GUTTER_LANES + 1) / (ROUTING_GUTTER_LANES + 1)
    offset = layout.gutter * fraction
    if layout.direction == "LR":
        return HEADER_HEIGHT + offset
    return PAGE_PADDING + offset


def _direct_route(
    source: NodeBox,
    target: NodeBox,
    direction: str,
    pair_index: int,
    pair_count: int,
    *,
    offset: bool = True,
) -> EdgeRoute:
    shift = _pair_offset(pair_index, pair_count, _PARALLEL_SPACING, 22.0) if offset else 0.0
    if direction == "LR":
        start_x = source.x + source.width
        start_y = source.center_y
        end_x = target.x - 8
        end_y = target.center_y
        if end_x <= start_x:  # manual backward edge keeps the legacy arch
            control_1 = (start_x + 46, start_y - 84)
            control_2 = (end_x - 46, end_y - 84)
        else:
            curve = max(28.0, (end_x - start_x) * 0.45)
            control_1 = (start_x + curve, start_y + shift)
            control_2 = (end_x - curve, end_y + shift)
    else:
        start_x = source.center_x
        start_y = source.y + source.height
        end_x = target.center_x
        end_y = target.y - 8
        if end_y <= start_y:
            control_1 = (start_x + 92 + shift, start_y + 42)
            control_2 = (end_x + 92 + shift, end_y - 42)
        else:
            curve = max(24.0, (end_y - start_y) * 0.45)
            control_1 = (start_x + shift, start_y + curve)
            control_2 = (end_x + shift, end_y - curve)

    start = (start_x, start_y)
    end = (end_x, end_y)
    path = (
        f"M{_fmt(start_x)},{_fmt(start_y)} "
        f"C{_fmt(control_1[0])},{_fmt(control_1[1])} "
        f"{_fmt(control_2[0])},{_fmt(control_2[1])} "
        f"{_fmt(end_x)},{_fmt(end_y)}"
    )
    anchors = [
        _bezier_point(start, control_1, control_2, end, t) for t in _DIRECT_TS
    ]
    if (direction == "LR" and end_x > start_x) or (
        direction == "TB" and end_y > start_y
    ):
        # Centre labels in the free corridor between the two cards instead of
        # on the shortened curve midpoint (the arrow stops 8 px short).
        mid = _bezier_point(start, control_1, control_2, end, 0.5)
        if direction == "LR":
            anchors.insert(0, ((start_x + target.x) / 2, mid[1]))
        else:
            anchors.insert(0, (mid[0], (start_y + target.y) / 2))
    return EdgeRoute(
        path=path,
        kind=DIRECT,
        points=(start, control_1, control_2, end),
        anchors=tuple(anchors),
    )


def _channel_route(
    source: NodeBox,
    target: NodeBox,
    layout: DiagramLayout,
    lane: float,
    anchor_lane: float,
) -> EdgeRoute:
    """Forward edge that skips at least one layer."""
    if layout.direction == "LR":
        gap = LAYER_GAP_LR
        start = (source.x + source.width, source.center_y)
        end = (target.x - 8, target.center_y)
        left = source.x + source.width + gap / 2
        right = target.x - gap / 2
        points = (
            start,
            (left, start[1]),
            (left, lane),
            (right, lane),
            (right, end[1]),
            end,
        )
        anchors = _segment_anchors((left, anchor_lane), (right, anchor_lane))
    else:
        gap = LAYER_GAP_TB
        start = (source.center_x, source.y + source.height)
        end = (target.center_x, target.y - 8)
        top = source.y + source.height + gap / 2
        bottom = target.y - gap / 2
        points = (
            start,
            (start[0], top),
            (lane, top),
            (lane, bottom),
            (end[0], bottom),
            end,
        )
        anchors = _segment_anchors((anchor_lane, top), (anchor_lane, bottom))
    return EdgeRoute(path=_polyline(points), kind=CHANNEL, points=points, anchors=anchors)


def _back_route(
    source: NodeBox,
    target: NodeBox,
    layout: DiagramLayout,
    lane: float,
    anchor_lane: float,
) -> EdgeRoute:
    """Back edge: approaches the target from its trailing side."""
    points: tuple[tuple[float, float], ...]
    anchors: tuple[tuple[float, float], ...]
    if layout.direction == "LR":
        gap = LAYER_GAP_LR
        start = (source.x, source.center_y)
        end = (target.x + target.width + 8, target.center_y)
        left = source.x - gap / 2
        right = target.x + target.width + gap / 2
        if abs(right - left) < 0.01:
            # Adjacent columns: one shared channel, no redundant lane detour.
            points = (start, (left, start[1]), (left, end[1]), end)
            anchors = _segment_anchors((left, start[1]), (left, end[1]))
        else:
            points = (
                start,
                (left, start[1]),
                (left, lane),
                (right, lane),
                (right, end[1]),
                end,
            )
            anchors = _segment_anchors((left, anchor_lane), (right, anchor_lane))
    else:
        gap = LAYER_GAP_TB
        start = (source.center_x, source.y)
        end = (target.center_x, target.y + target.height + 8)
        top = source.y - gap / 2
        bottom = target.y + target.height + gap / 2
        if abs(top - bottom) < 0.01:
            points = (start, (start[0], top), (end[0], top), end)
            anchors = _segment_anchors((start[0], top), (end[0], top))
        else:
            points = (
                start,
                (start[0], top),
                (lane, top),
                (lane, bottom),
                (end[0], bottom),
                end,
            )
            anchors = _segment_anchors((anchor_lane, top), (anchor_lane, bottom))
    return EdgeRoute(path=_polyline(points), kind=BACK, points=points, anchors=anchors)


def _segment_anchors(
    first: tuple[float, float],
    second: tuple[float, float],
) -> tuple[tuple[float, float], ...]:
    x1, y1 = first
    x2, y2 = second
    return tuple(
        (x1 + (x2 - x1) * t, y1 + (y2 - y1) * t) for t in _LANE_TS
    )


def _polyline(points: tuple[tuple[float, float], ...]) -> str:
    start = points[0]
    segments = " ".join(f"L{_fmt(x)},{_fmt(y)}" for x, y in points[1:])
    return f"M{_fmt(start[0])},{_fmt(start[1])} {segments}"


def _bezier_point(
    start: tuple[float, float],
    control_1: tuple[float, float],
    control_2: tuple[float, float],
    end: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    u = 1 - t
    x = u**3 * start[0] + 3 * u**2 * t * control_1[0] + 3 * u * t**2 * control_2[0] + t**3 * end[0]
    y = u**3 * start[1] + 3 * u**2 * t * control_1[1] + 3 * u * t**2 * control_2[1] + t**3 * end[1]
    return x, y


def _fmt(value: float) -> str:
    rounded = round(value, 2)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:.2f}"
