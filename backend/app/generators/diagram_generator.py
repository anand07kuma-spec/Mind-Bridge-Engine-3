"""Visual planning for diagram scenes.

Engine 2 sends nodes and edges, not coordinates. This module turns that graph
into a deterministic, readable layout:

* layers are derived from edge direction (longest-path layering),
* nodes inside a layer are ordered by the barycenter of their parents to
  reduce edge crossings,
* manual positions are honoured when every node supplies one.

The renderer (`svg_generator`) only deals with drawing a completed layout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise

from app.schemas.scene import EdgeSpec, NodeSpec, VisualInstruction

CARD_WIDTH = 180.0
CARD_HEIGHT = 88.0
LAYER_GAP_LR = 104.0
LAYER_GAP_TB = 64.0
NODE_GAP_LR = 30.0
NODE_GAP_TB = 40.0
PAGE_PADDING = 56.0
HEADER_HEIGHT = 140.0
FOOTER_HEIGHT = 52.0
MANUAL_CONTENT_PADDING = 24.0
MIN_CONTENT_HEIGHT = 300.0
MIN_CANVAS_WIDTH = 920.0

# Non-adjacent edges need a free band to travel across. The gutter is only
# reserved on graphs whose single row/column leaves no inter-card gap.
ROUTING_GUTTER_BASE = 30.0
ROUTING_GUTTER_STEP = 10.0
ROUTING_GUTTER_LANES = 5
MIN_ROUTING_BAND = 14.0


@dataclass
class NodeBox:
    spec: NodeSpec
    x: float
    y: float
    width: float = CARD_WIDTH
    height: float = CARD_HEIGHT

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2


@dataclass
class DiagramLayout:
    nodes: dict[str, NodeBox]
    edges: list[EdgeSpec]
    width: float
    height: float
    direction: str
    warnings: list[str] = field(default_factory=list)
    levels: dict[str, int] = field(default_factory=dict)
    gutter: float = 0.0
    manual: bool = False

    @property
    def content_top(self) -> float:
        return HEADER_HEIGHT


def horizontal_bands(boxes: dict[str, NodeBox]) -> list[float]:
    """Y centers of free bands that span every card column, sorted ascending."""
    return _band_centers([(box.y, box.y + box.height) for box in boxes.values()])


def vertical_bands(boxes: dict[str, NodeBox]) -> list[float]:
    """X centers of free bands that span every card row, sorted ascending."""
    return _band_centers([(box.x, box.x + box.width) for box in boxes.values()])


def _band_centers(intervals: list[tuple[float, float]]) -> list[float]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 0.5:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [
        (first[1] + second[0]) / 2
        for first, second in pairwise(merged)
        if second[0] - first[1] >= MIN_ROUTING_BAND
    ]


def plan_diagram(instruction: VisualInstruction) -> DiagramLayout:
    """Return absolute bounding boxes for every node plus the canvas size."""
    warnings: list[str] = []
    if all(node.position is not None for node in instruction.nodes):
        layout = _manual_layout(instruction)
    else:
        if any(node.position is not None for node in instruction.nodes):
            warnings.append("Manual positions must be provided for every node; falling back to automatic layout.")
        layout = _auto_layout(instruction, warnings)
    layout.warnings = warnings
    return layout


def _manual_layout(instruction: VisualInstruction) -> DiagramLayout:
    boxes: dict[str, NodeBox] = {}
    max_x = 0.0
    max_y = 0.0
    for node in instruction.nodes:
        position = node.position
        assert position is not None  # guaranteed by caller
        max_x = max(max_x, position.x)
        max_y = max(max_y, position.y)
        boxes[node.id] = NodeBox(
            spec=node,
            x=PAGE_PADDING + position.x,
            y=HEADER_HEIGHT + MANUAL_CONTENT_PADDING + position.y,
        )

    width = max(MIN_CANVAS_WIDTH, PAGE_PADDING * 2 + max_x + CARD_WIDTH)
    content_height = max_y + CARD_HEIGHT + MANUAL_CONTENT_PADDING * 2
    height = HEADER_HEIGHT + content_height + FOOTER_HEIGHT
    return DiagramLayout(
        nodes=boxes,
        edges=instruction.edges,
        width=width,
        height=height,
        direction=instruction.options.direction,
        manual=True,
    )


def _auto_layout(instruction: VisualInstruction, warnings: list[str]) -> DiagramLayout:
    node_order = {node.id: index for index, node in enumerate(instruction.nodes)}
    incoming: dict[str, list[str]] = {node.id: [] for node in instruction.nodes}
    for edge in instruction.edges:
        incoming[edge.target].append(edge.source)

    levels: dict[str, int] = {}
    visiting: set[str] = set()
    cycle_reported = False

    def level_of(node_id: str) -> int:
        nonlocal cycle_reported
        if node_id in levels:
            return levels[node_id]
        if node_id in visiting:
            if not cycle_reported:
                warnings.append("The graph contains a cycle; layout uses a best-effort ordering.")
                cycle_reported = True
            return 0
        visiting.add(node_id)
        value = 0
        for parent in incoming[node_id]:
            value = max(value, level_of(parent) + 1)
        visiting.discard(node_id)
        # Defensive cap: cycles must never produce runaway coordinates.
        value = min(value, max(len(instruction.nodes) - 1, 0))
        levels[node_id] = value
        return value

    columns: dict[int, list[str]] = {}
    for node in instruction.nodes:
        columns.setdefault(level_of(node.id), []).append(node.id)

    # One barycenter pass: order each column by the average position of its
    # parents in the previous column. Stable sort keeps input order as tie-break.
    previous_position: dict[str, int] = {}
    for level in sorted(columns):
        column = columns[level]
        if level == 0 or not previous_position:
            column.sort(key=lambda node_id: node_order[node_id])
        else:

            def barycenter(
                node_id: str, previous: dict[str, int] = previous_position
            ) -> tuple[int, float]:
                parent_slots = [previous[p] for p in incoming[node_id] if p in previous]
                if not parent_slots:
                    return (1, float(node_order[node_id]))
                return (0, sum(parent_slots) / len(parent_slots))

            column.sort(key=barycenter)
        previous_position = {node_id: index for index, node_id in enumerate(column)}

    direction = instruction.options.direction
    boxes: dict[str, NodeBox] = {}
    # Levels can start above zero (a fully cyclic graph) or leave gaps, so the
    # canvas is sized for the highest index, not just the number of columns.
    level_span = max(levels.values(), default=0) + 1

    if direction == "LR":
        column_count = level_span
        widest_column = max(len(column) for column in columns.values())
        content_height = max(
            MIN_CONTENT_HEIGHT,
            widest_column * CARD_HEIGHT + (widest_column - 1) * NODE_GAP_LR,
        )
        width = max(
            MIN_CANVAS_WIDTH,
            PAGE_PADDING * 2 + column_count * CARD_WIDTH + max(column_count - 1, 0) * LAYER_GAP_LR,
        )
        height = HEADER_HEIGHT + content_height + FOOTER_HEIGHT

        for level, column in columns.items():
            column_height = len(column) * CARD_HEIGHT + (len(column) - 1) * NODE_GAP_LR
            start_y = HEADER_HEIGHT + (content_height - column_height) / 2
            for row, node_id in enumerate(column):
                boxes[node_id] = NodeBox(
                    spec=_find_node(instruction, node_id),
                    x=PAGE_PADDING + level * (CARD_WIDTH + LAYER_GAP_LR),
                    y=start_y + row * (CARD_HEIGHT + NODE_GAP_LR),
                )
    else:  # top-to-bottom
        row_count = level_span
        widest_row = max(len(column) for column in columns.values())
        content_width = max(
            MIN_CANVAS_WIDTH - PAGE_PADDING * 2,
            widest_row * CARD_WIDTH + (widest_row - 1) * NODE_GAP_TB,
        )
        width = max(MIN_CANVAS_WIDTH, content_width + PAGE_PADDING * 2)
        height = (
            HEADER_HEIGHT
            + row_count * CARD_HEIGHT
            + max(row_count - 1, 0) * LAYER_GAP_TB
            + FOOTER_HEIGHT
        )

        for level, column in columns.items():
            row_width = len(column) * CARD_WIDTH + (len(column) - 1) * NODE_GAP_TB
            start_x = PAGE_PADDING + (content_width - row_width) / 2
            for index, node_id in enumerate(column):
                boxes[node_id] = NodeBox(
                    spec=_find_node(instruction, node_id),
                    x=start_x + index * (CARD_WIDTH + NODE_GAP_TB),
                    y=HEADER_HEIGHT + level * (CARD_HEIGHT + LAYER_GAP_TB),
                )

    gutter = _reserve_routing_gutter(instruction, boxes, direction)
    if gutter:
        if direction == "LR":
            for box in boxes.values():
                box.y += gutter
            height += gutter
        else:
            for box in boxes.values():
                box.x += gutter
            width += gutter

    return DiagramLayout(
        nodes=boxes,
        edges=instruction.edges,
        width=width,
        height=height,
        direction=direction,
        levels=dict(levels),
        gutter=gutter,
    )


def _reserve_routing_gutter(
    instruction: VisualInstruction,
    boxes: dict[str, NodeBox],
    direction: str,
) -> float:
    """Reserve a channel band when no full-width inter-card gap exists.

    Adjacent edges travel inside the regular layer gap and never need it.
    """
    non_adjacent = [
        edge for edge in instruction.edges if not _is_adjacent(boxes, edge, direction)
    ]
    if not non_adjacent:
        return 0.0
    bands = horizontal_bands(boxes) if direction == "LR" else vertical_bands(boxes)
    if bands:
        return 0.0
    lanes = min(len(non_adjacent), ROUTING_GUTTER_LANES)
    return ROUTING_GUTTER_BASE + ROUTING_GUTTER_STEP * (lanes - 1)


def _is_adjacent(boxes: dict[str, NodeBox], edge: EdgeSpec, direction: str) -> bool:
    source = boxes.get(edge.source)
    target = boxes.get(edge.target)
    if source is None or target is None:
        return True
    if direction == "LR":
        return target.x > source.x and target.x - (source.x + source.width) <= LAYER_GAP_LR + 1.0
    return target.y > source.y and target.y - (source.y + source.height) <= LAYER_GAP_TB + 1.0


def _find_node(instruction: VisualInstruction, node_id: str) -> NodeSpec:
    for node in instruction.nodes:
        if node.id == node_id:
            return node
    raise KeyError(node_id)
