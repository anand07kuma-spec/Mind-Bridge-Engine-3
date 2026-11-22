"""JSON-to-SVG rendering.

The generator consumes a :class:`~app.schemas.scene.VisualInstruction`, asks
:mod:`diagram_generator` for a layout and emits a self-contained SVG document.

Design notes
------------
* Icons, the arrow head and the card shadow are re-usable ``<symbol>`` /
  ``<marker>`` / ``<filter>`` definitions in ``<defs>``.
* Every node becomes a ``<g class="mb-node" data-node-id="...">`` group so the
  frontend (or a future scene editor) can identify and restyle elements.
* All user-provided text is sanitized for XML 1.0 and then escaped.
* The SVG has an intrinsic ``viewBox`` and scales responsively in the browser.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from app.generators.diagram_generator import (
    CARD_WIDTH,
    HEADER_HEIGHT,
    PAGE_PADDING,
    DiagramLayout,
    NodeBox,
    plan_diagram,
)
from app.generators.edge_routing import EdgeRoute, edge_key, plan_routes
from app.generators.text_metrics import (
    estimated_px,
    sanitize_xml_text,
    truncate_display,
    wrap_display,
)
from app.schemas.scene import VisualInstruction

GENERATOR_NAME = "mindbridge-engine3.svg"
GENERATOR_VERSION = "1.4.0"
PROVIDER_NAME = "engine3-svg"

FONT_STACK = "Inter, 'Segoe UI', system-ui, -apple-system, sans-serif"

INK = "#0F172A"
MUTED = "#5B6B84"
FAINT = "#94A3B8"
LINE = "#E4EAF2"
EDGE_COLOR = "#8CA0B8"

KIND_COLORS: dict[str, str] = {
    "bus": "#0EA5E9",
    "server": "#6366F1",
    "passenger": "#10B981",
    "phone": "#8B5CF6",
    "database": "#F59E0B",
    "cloud": "#64748B",
    "service": "#EC4899",
    "sensor": "#14B8A6",
    "generic": "#475569",
}

# 24x24 line icons. ``currentColor`` lets the node tint the icon per kind.
ICONS: dict[str, str] = {
    "bus": (
        '<rect x="3" y="4" width="18" height="12" rx="2.6"/>'
        '<path d="M3 11h18M7 4v7M17 4v7"/>'
        '<circle cx="7.5" cy="18.5" r="1.5"/>'
        '<circle cx="16.5" cy="18.5" r="1.5"/>'
    ),
    "server": (
        '<rect x="4" y="4" width="16" height="7" rx="1.8"/>'
        '<rect x="4" y="13" width="16" height="7" rx="1.8"/>'
        '<path d="M7.5 7.5h.01M7.5 16.5h.01M11 7.5h5M11 16.5h5"/>'
    ),
    "passenger": (
        '<circle cx="12" cy="8" r="3.4"/>'
        '<path d="M5.5 20c.5-4.2 3.2-6.4 6.5-6.4s6 2.2 6.5 6.4"/>'
    ),
    "phone": '<rect x="7" y="3" width="10" height="18" rx="2.6"/><path d="M10.5 18.5h3"/>',
    "database": (
        '<ellipse cx="12" cy="6" rx="7" ry="3"/>'
        '<path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/>'
        '<path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/>'
    ),
    "cloud": (
        '<path d="M7.5 18.5h10a3.8 3.8 0 0 0 .4-7.6 5.6 5.6 0 0 0-10.8-1.7 '
        'A4.6 4.6 0 0 0 7.5 18.5Z"/>'
    ),
    "service": (
        '<path d="M12 3.2 19 7v10l-7 3.8L5 17V7l7-3.8Z"/>'
        '<circle cx="12" cy="12" r="2.2"/>'
    ),
    "sensor": (
        '<circle cx="12" cy="17" r="1.8"/>'
        '<path d="M8.8 13.8a4.5 4.5 0 0 1 6.4 0"/>'
        '<path d="M6 11a8.5 8.5 0 0 1 12 0"/>'
    ),
    "generic": '<rect x="4.5" y="4.5" width="15" height="15" rx="4"/><path d="M12 8.5v7M8.5 12h7"/>',
}


@dataclass
class RenderResult:
    svg: str
    width: int
    height: int
    warnings: list[str] = field(default_factory=list)
    namespace: str = ""


@dataclass
class NodeLabelPlan:
    lines: list[str]
    truncated: bool


@dataclass
class EdgeLabelPlan:
    x: float
    y: float
    width: float
    text: str


@dataclass
class _EdgeLabelSpec:
    """A label whose text is fitted but whose position is not chosen yet."""

    key: str
    text: str
    width: float
    anchors: tuple[tuple[float, float], ...]


@dataclass
class DisplayPlan:
    title: str
    title_truncated: bool
    description: str | None
    description_truncated: bool
    node_labels: dict[str, NodeLabelPlan]
    edge_labels: dict[str, EdgeLabelPlan]
    warnings: list[str] = field(default_factory=list)


# Approximate glyph widths in pixels per display unit. CJK/emoji clusters count
# as two units, so these divide the available pixel box into display units.
TITLE_PX_PER_UNIT = 13.2  # 26 px title
DESCRIPTION_PX_PER_UNIT = 6.25  # 12.5 px description (half an em per unit)
NODE_PX_PER_UNIT = 6.5  # 13 px node label
EDGE_LABEL_PX_PER_UNIT = 5.25  # 10.5 px edge label (half an em per unit)
NODE_TEXT_LEFT = 70.0
NODE_TEXT_RIGHT = 14.0
EDGE_LABEL_HEIGHT = 22.0
EDGE_LABEL_GAP = 4.0
EDGE_LABEL_PADDING = 16.0
EDGE_LABEL_MARGIN = 32.0
MIN_EDGE_LABEL_WIDTH = 96.0


class SvgGenerator:
    """Pure, deterministic renderer: same instruction + version => same bytes."""

    def generate(self, instruction: VisualInstruction, namespace: str | None = None) -> RenderResult:
        layout = plan_diagram(instruction)
        routes = plan_routes(instruction, layout)
        display = _build_display_plan(instruction, layout, routes)
        prefix = namespace or _default_namespace(instruction)
        svg = self._render(instruction, layout, prefix, display, routes)
        return RenderResult(
            svg=svg,
            width=round(layout.width),
            height=round(layout.height),
            warnings=[*layout.warnings, *display.warnings],
            namespace=prefix,
        )

    def warnings_for(self, instruction: VisualInstruction) -> list[str]:
        """Layout and display warnings without rendering, usable on cache hits."""
        layout = plan_diagram(instruction)
        routes = plan_routes(instruction, layout)
        return [
            *layout.warnings,
            *_build_display_plan(instruction, layout, routes).warnings,
        ]

    # ------------------------------------------------------------------
    # Document scaffolding
    # ------------------------------------------------------------------

    def _render(
        self,
        instruction: VisualInstruction,
        layout: DiagramLayout,
        prefix: str,
        display: DisplayPlan,
        routes: dict[str, EdgeRoute],
    ) -> str:
        width = round(layout.width)
        height = round(layout.height)
        title = _text(display.title)
        parts: list[str] = [
            (
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="{prefix}-title" '
                f'font-family="{FONT_STACK}" style="max-width:100%;height:auto;display:block">'
            ),
            f'<title id="{prefix}-title">{title} — generated by Mind Bridge Engine 3</title>',
        ]
        parts.append(self._defs(prefix))
        parts.append(
            f'<rect x="0" y="0" width="{width}" height="{height}" rx="22" fill="#FFFFFF" '
            f'stroke="{LINE}" stroke-width="1"/>'
        )
        parts.append(
            f'<rect x="0" y="0" width="{width}" height="{HEADER_HEIGHT}" rx="22" '
            f'fill="url(#{prefix}-header-wash)"/>'
        )
        parts.append(
            f'<rect x="0" y="{HEADER_HEIGHT - 22}" width="{width}" height="22" fill="#FFFFFF"/>'
        )
        parts.append(self._header(width, display))
        parts.append(self._edges(instruction, layout, prefix, display, routes))
        parts.append(self._nodes(layout, prefix, display))
        parts.append(self._footer(instruction, width, height))
        parts.append("</svg>")
        return "\n".join(parts)

    def _defs(self, prefix: str) -> str:
        icons = "\n".join(
            f'<symbol id="{prefix}-icon-{kind}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">{shapecode}</symbol>'
            for kind, shapecode in ICONS.items()
        )
        return f"""<defs>
<linearGradient id="{prefix}-header-wash" x1="0" y1="0" x2="0" y2="1">
  <stop offset="0" stop-color="#F7FAFF"/>
  <stop offset="1" stop-color="#FFFFFF"/>
</linearGradient>
<filter id="{prefix}-card-shadow" x="-25%" y="-25%" width="150%" height="170%">
  <feDropShadow dx="0" dy="6" stdDeviation="9" flood-color="#0F172A" flood-opacity="0.07"/>
</filter>
<marker id="{prefix}-arrow" viewBox="0 0 12 12" refX="10.2" refY="6" markerWidth="12" markerHeight="12"
        markerUnits="userSpaceOnUse" orient="auto">
  <path d="M2.5 2.2 10.2 6 2.5 9.8" fill="none" stroke="{EDGE_COLOR}" stroke-width="1.6"
        stroke-linecap="round" stroke-linejoin="round"/>
</marker>
{icons}
</defs>"""

    def _header(self, width: int, display: DisplayPlan) -> str:
        badge_width = 138
        badge_x = width - PAGE_PADDING - badge_width
        badge = (
            f'<g class="mb-badge">'
            f'<rect x="{badge_x}" y="30" width="{badge_width}" height="28" rx="14" fill="#F1F5FF" '
            f'stroke="#DDE7FE" stroke-width="1"/>'
            f'<circle cx="{badge_x + 16}" cy="44" r="3" fill="#6366F1"/>'
            f'<text x="{badge_x + 26}" y="47.5" font-size="9.5" font-weight="600" fill="#4C5FC0" '
            f'letter-spacing="1.1">SVG DIAGRAM</text>'
            f'</g>'
        )
        description_line = (
            f'<text x="{PAGE_PADDING}" y="99" font-size="12.5" fill="{MUTED}">'
            f"{_text(display.description)}</text>"
            if display.description
            else ""
        )
        return f"""<g class="mb-header">
<text x="{PAGE_PADDING}" y="44" font-size="9.5" font-weight="600" fill="#7C8AA5"
      letter-spacing="2">MIND BRIDGE · ENGINE 3 VISUALIZATION</text>
{badge}
<text x="{PAGE_PADDING}" y="78" font-size="26" font-weight="600" fill="{INK}"
      letter-spacing="-0.3">{_text(display.title)}</text>
{description_line}
<line x1="{PAGE_PADDING}" y1="116" x2="{width - PAGE_PADDING}" y2="116" stroke="{LINE}" stroke-width="1"/>
</g>"""

    def _footer(self, instruction: VisualInstruction, width: int, height: int) -> str:
        node_count = len(instruction.nodes)
        edge_count = len(instruction.edges)
        node_word = "node" if node_count == 1 else "nodes"
        edge_word = "connection" if edge_count == 1 else "connections"
        summary = f"{node_count} {node_word} · {edge_count} {edge_word}"
        return f"""<g class="mb-footer">
<line x1="{PAGE_PADDING}" y1="{height - 46}" x2="{width - PAGE_PADDING}" y2="{height - 46}"
      stroke="{LINE}" stroke-width="1"/>
<text x="{PAGE_PADDING}" y="{height - 22}" font-size="9.5" fill="{FAINT}" letter-spacing="1.3">
MIND BRIDGE ENGINE 3 · GENERATED VISUAL</text>
<text x="{width - PAGE_PADDING}" y="{height - 22}" font-size="9.5" fill="{FAINT}"
      text-anchor="end">{summary}</text>
</g>"""

    # ------------------------------------------------------------------
    # Edges
    # ------------------------------------------------------------------

    def _edges(
        self,
        instruction: VisualInstruction,
        layout: DiagramLayout,
        prefix: str,
        display: DisplayPlan,
        routes: dict[str, EdgeRoute],
    ) -> str:
        chunks: list[str] = ['<g class="mb-edges">']
        label_chunks: list[str] = []
        for edge in instruction.edges:
            if edge.source not in layout.nodes or edge.target not in layout.nodes:
                continue
            route = routes.get(edge_key(edge))
            if route is None:
                continue
            dash = ' stroke-dasharray="5 4"' if edge.style == "dashed" else ""
            marker = f' marker-end="url(#{prefix}-arrow)"' if edge.show_arrow else ""
            chunks.append(
                f'<path d="{route.path}" fill="none" stroke="{EDGE_COLOR}" stroke-width="1.6" '
                f'stroke-linecap="round"{dash}{marker}/>'
            )
            plan = display.edge_labels.get(edge_key(edge))
            if plan is not None and edge.label:
                label_chunks.append(_edge_label(plan.text, plan.x, plan.y, plan.width))
        chunks.extend(label_chunks)
        chunks.append("</g>")
        return "\n".join(chunks)

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def _nodes(self, layout: DiagramLayout, prefix: str, display: DisplayPlan) -> str:
        chunks: list[str] = ['<g class="mb-nodes">']
        for node_id, node_box in layout.nodes.items():
            chunks.append(_node_markup(node_box, prefix, display.node_labels[node_id]))
        chunks.append("</g>")
        return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------


def _default_namespace(instruction: VisualInstruction) -> str:
    """Stable document id so multiple SVGs can share a page without collisions.

    ``render_mode`` is excluded so adding the optional field does not change the
    bytes (or the id namespace) of an existing SVG instruction.
    """
    scene = re.sub(r"[^A-Za-z0-9_-]", "-", instruction.scene_id)[:32]
    digest = hashlib.sha256(
        instruction.model_dump_json(exclude={"render_mode"}).encode("utf-8")
    ).hexdigest()[:10]
    return f"mb-{scene}-{digest}"


_TITLE_ID_RE = re.compile(r'aria-labelledby="([^"]+)-title"')


def apply_namespace(svg: str, namespace: str) -> str:
    """Re-prefix a stored SVG so each scene keeps its own id namespace.

    Exact-match assets are shared between scenes with identical visuals, so the
    stored document may carry another scene's prefix. Rewriting it on read keeps
    ids unique per scene without duplicating the file.
    """
    match = _TITLE_ID_RE.search(svg)
    if match is None or match.group(1) == namespace:
        return svg
    return svg.replace(match.group(1), namespace)


def _build_display_plan(
    instruction: VisualInstruction,
    layout: DiagramLayout,
    routes: dict[str, EdgeRoute],
) -> DisplayPlan:
    """Fit text into the deterministic layout, recording visible warnings."""
    warnings: list[str] = []

    title_source = sanitize_xml_text(instruction.title)
    description_source = (
        sanitize_xml_text(instruction.description) if instruction.description else None
    )
    node_sources = {node.id: sanitize_xml_text(node.label) for node in instruction.nodes}
    edge_sources = {
        edge.id: sanitize_xml_text(edge.label)
        for edge in instruction.edges
        if edge.label is not None
    }

    replaced: list[str] = []
    if title_source != instruction.title:
        replaced.append("title")
    if description_source is not None and description_source != instruction.description:
        replaced.append("description")
    replaced.extend(
        f"node '{node.id}'" for node in instruction.nodes if node_sources[node.id] != node.label
    )
    replaced.extend(
        f"edge '{edge.id}'"
        for edge in instruction.edges
        if edge.id in edge_sources and edge_sources[edge.id] != edge.label
    )
    if replaced:
        warnings.append(
            "Invalid XML characters were replaced with U+FFFD in: " + ", ".join(replaced) + "."
        )

    header_width = layout.width - PAGE_PADDING * 2
    title_units = max(8.0, header_width / TITLE_PX_PER_UNIT)
    title, title_truncated = truncate_display(title_source, title_units)
    if title_truncated:
        warnings.append(
            f"The scene title is longer than the header (estimated "
            f"{estimated_px(title_source, 26):.0f} px of {header_width:.0f} px available); "
            "it is shortened with an ellipsis."
        )
    elif estimated_px(title, 26) > header_width + 0.5:
        warnings.append(
            f"The scene title may overflow the header (estimated "
            f"{estimated_px(title, 26):.0f} px of {header_width:.0f} px available)."
        )

    description: str | None = None
    description_truncated = False
    if description_source:
        description_units = max(8.0, header_width / DESCRIPTION_PX_PER_UNIT)
        description, description_truncated = truncate_display(
            description_source, description_units
        )
        if description_truncated:
            warnings.append(
                f"The scene description is longer than the header (estimated "
                f"{estimated_px(description_source, 12.5):.0f} px of {header_width:.0f} px "
                "available); it is shortened with an ellipsis."
            )
        elif estimated_px(description, 12.5) > header_width + 0.5:
            warnings.append(
                f"The scene description may overflow the header (estimated "
                f"{estimated_px(description, 12.5):.0f} px of {header_width:.0f} px available)."
            )

    node_labels: dict[str, NodeLabelPlan] = {}
    shortened: list[str] = []
    overflowing: list[str] = []
    for node_id, box in layout.nodes.items():
        available = box.width - NODE_TEXT_LEFT - NODE_TEXT_RIGHT
        node_units = max(4.0, available / NODE_PX_PER_UNIT)
        lines, truncated = wrap_display(node_sources[node_id], node_units, max_lines=2)
        node_labels[node_id] = NodeLabelPlan(lines=lines, truncated=truncated)
        if truncated:
            shortened.append(
                f"{node_id} (estimated {estimated_px(node_sources[node_id], 13):.0f} px "
                f"of {available:.0f} px available)"
            )
        else:
            widest = max((estimated_px(line, 13) for line in lines), default=0.0)
            if widest > available + 0.5:
                overflowing.append(
                    f"{node_id} (estimated {widest:.0f} px of {available:.0f} px available)"
                )
    if shortened:
        warnings.append(
            "Node labels shortened to fit their cards: " + "; ".join(sorted(shortened)) + "."
        )
    if overflowing:
        warnings.append(
            "Node labels may overflow their cards: " + "; ".join(sorted(overflowing)) + "."
        )

    edge_labels: dict[str, EdgeLabelPlan] = {}
    if instruction.options.show_edge_labels:
        placed: list[tuple[float, float, float, float]] = []
        node_rects = [
            (box.x, box.y, box.width, box.height) for box in layout.nodes.values()
        ]
        overlap = False
        card_overlap = False
        bounds_overlap = False
        shortened_edges: list[str] = []
        overflowing_edges: list[str] = []

        # Fit every label once, in instruction order, so warnings stay stable.
        specs: dict[str, _EdgeLabelSpec] = {}
        for edge in instruction.edges:
            label = edge_sources.get(edge.id)
            if not label:
                continue
            source = layout.nodes.get(edge.source)
            target = layout.nodes.get(edge.target)
            route = routes.get(edge_key(edge))
            if source is None or target is None or route is None:
                continue
            budget = _edge_label_budget(layout, source, target)
            max_units = max(4.0, (budget - EDGE_LABEL_PADDING) / EDGE_LABEL_PX_PER_UNIT)
            text, truncated = truncate_display(label, max_units)
            text_width = min(
                budget,
                max(24.0, estimated_px(text, 10.5) + EDGE_LABEL_PADDING),
            )
            if truncated:
                shortened_edges.append(
                    f"{edge.id} (estimated {estimated_px(label, 10.5):.0f} px "
                    f"of {budget:.0f} px available)"
                )
            elif estimated_px(text, 10.5) + EDGE_LABEL_PADDING > budget + 0.5:
                overflowing_edges.append(
                    f"{edge.id} (estimated {estimated_px(text, 10.5) + EDGE_LABEL_PADDING:.0f} px "
                    f"of {budget:.0f} px available)"
                )
            specs[edge_key(edge)] = _EdgeLabelSpec(
                key=edge_key(edge),
                text=text,
                width=text_width,
                anchors=route.anchors,
            )

        groups: dict[tuple[str, str], list[str]] = {}
        for edge in instruction.edges:
            key = edge_key(edge)
            if key in specs:
                groups.setdefault((edge.source, edge.target), []).append(key)

        handled_groups: set[tuple[str, str]] = set()
        for edge in instruction.edges:
            key = edge_key(edge)
            spec = specs.get(key)
            if spec is None:
                continue
            group_key = (edge.source, edge.target)
            group_keys = groups[group_key]
            if len(group_keys) == 1:
                placement = _choose_label_anchor(
                    spec.anchors, spec.width, placed, node_rects
                )
                if placement is None:
                    continue
                x, y, hits_card, hits_label = placement
                edge_labels[key] = EdgeLabelPlan(
                    x=x, y=y, width=spec.width, text=spec.text
                )
                card_overlap = card_overlap or hits_card
                overlap = overlap or hits_label
                continue

            if group_key in handled_groups:
                continue
            handled_groups.add(group_key)
            group_specs = [specs[group_key_] for group_key_ in group_keys]
            placements = _place_parallel_labels(
                group_specs, layout, placed, node_rects
            )
            for group_spec, group_placement in zip(
                group_specs, placements, strict=True
            ):
                x, y, hits_card, hits_label, outside = group_placement
                edge_labels[group_spec.key] = EdgeLabelPlan(
                    x=x, y=y, width=group_spec.width, text=group_spec.text
                )
                card_overlap = card_overlap or hits_card
                overlap = overlap or hits_label
                bounds_overlap = bounds_overlap or outside

        if shortened_edges:
            warnings.append(
                "Edge labels shortened to fit the space between their nodes: "
                + "; ".join(shortened_edges)
                + "."
            )
        if overflowing_edges:
            warnings.append(
                "Edge labels may overflow their layout space: "
                + "; ".join(overflowing_edges)
                + "."
            )
        if card_overlap:
            warnings.append(
                "Some edge labels overlap node cards; shorten edge labels or simplify the graph."
            )
        if overlap:
            warnings.append(
                "Some edge labels overlap; reduce parallel connections or disable labels."
            )
        if bounds_overlap:
            warnings.append(
                "Some edge labels could not be placed inside the canvas; shorten edge labels "
                "or simplify the graph."
            )

    return DisplayPlan(
        title=title,
        title_truncated=title_truncated,
        description=description,
        description_truncated=description_truncated,
        node_labels=node_labels,
        edge_labels=edge_labels,
        warnings=warnings,
    )


def _edge_label_budget(layout: DiagramLayout, source: NodeBox, target: NodeBox) -> float:
    """Pixels a label may occupy between two cards, bounded by the canvas."""
    if layout.direction == "LR":
        span = abs(target.center_x - source.center_x)
    else:
        span = abs(target.center_y - source.center_y)
    gap = max(span - CARD_WIDTH, 0.0) + EDGE_LABEL_MARGIN
    canvas_budget = layout.width - PAGE_PADDING * 2
    return max(MIN_EDGE_LABEL_WIDTH, min(canvas_budget, gap))


def _rects_overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    gap: float,
) -> bool:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return not (
        ax + aw + gap <= bx
        or bx + bw + gap <= ax
        or ay + ah + gap <= by
        or by + bh + gap <= ay
    )


def _preferred_anchor(spec: _EdgeLabelSpec) -> tuple[float, float]:
    return spec.anchors[0] if spec.anchors else (0.0, 0.0)


def _rect_inside_canvas(
    rect: tuple[float, float, float, float],
    layout: DiagramLayout,
) -> bool:
    x, y, width, height = rect
    return (
        x >= -0.5
        and y >= -0.5
        and x + width <= layout.width + 0.5
        and y + height <= layout.height + 0.5
    )


def _place_parallel_labels(
    specs: list[_EdgeLabelSpec],
    layout: DiagramLayout,
    placed: list[tuple[float, float, float, float]],
    node_rects: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, bool, bool, bool]]:
    """Stack labels for one ``(source, target)`` group across its corridor.

    Each label keeps its own position along the flow axis, so it stays near its
    edge, and labels are separated across the flow axis by the pill height plus
    the collision gap. The whole stack shifts when a card or canvas edge is in
    the way. When no shift is collision-free the least-bad stack is used and the
    caller warns; labels are never dropped.
    """
    count = len(specs)
    vertical = layout.direction == "LR"
    spacing = (
        EDGE_LABEL_HEIGHT + EDGE_LABEL_GAP
        if vertical
        else max(spec.width for spec in specs) + EDGE_LABEL_GAP
    )
    anchors = [_preferred_anchor(spec) for spec in specs]
    base = (
        sum(anchor[1] for anchor in anchors) / count
        if vertical
        else sum(anchor[0] for anchor in anchors) / count
    )

    max_steps = min(
        64,
        max(8, int(max(layout.width, layout.height) / spacing) + count),
    )
    shifts = [0.0]
    for step in range(1, max_steps + 1):
        shifts.append(step * spacing)
        shifts.append(-step * spacing)

    best_score: tuple[int, int, int, float] | None = None
    best_rects: list[tuple[float, float, tuple[float, float, float, float]]] = []
    best_outside = 0
    best_card_hits = 0
    best_label_hits = 0

    for shift in shifts:
        rects: list[tuple[float, float, tuple[float, float, float, float]]] = []
        for index, spec in enumerate(specs):
            anchor_x, anchor_y = anchors[index]
            slot = (index - (count - 1) / 2) * spacing + shift
            if vertical:
                x, y = anchor_x, base + slot
            else:
                x, y = base + slot, anchor_y
            rect = (
                x - spec.width / 2,
                y - EDGE_LABEL_HEIGHT / 2,
                spec.width,
                EDGE_LABEL_HEIGHT,
            )
            rects.append((x, y, rect))

        outside = sum(
            1 for _, _, rect in rects if not _rect_inside_canvas(rect, layout)
        )
        card_hits = sum(
            1
            for _, _, rect in rects
            for node_rect in node_rects
            if _rects_overlap(rect, node_rect, EDGE_LABEL_GAP)
        )
        label_hits = sum(
            1
            for _, _, rect in rects
            for other in placed
            if _rects_overlap(rect, other, EDGE_LABEL_GAP)
        )
        score = (outside, card_hits, label_hits, abs(shift))
        if best_score is None or score < best_score:
            best_score = score
            best_rects = rects
            best_outside = outside
            best_card_hits = card_hits
            best_label_hits = label_hits
        if score[:3] == (0, 0, 0):
            break

    assert best_score is not None  # shifts always contains 0.0
    placements: list[tuple[float, float, bool, bool, bool]] = []
    for x, y, rect in best_rects:
        placed.append(rect)
        placements.append(
            (x, y, best_card_hits > 0, best_label_hits > 0, best_outside > 0)
        )
    return placements


def _choose_label_anchor(
    anchors: tuple[tuple[float, float], ...],
    text_width: float,
    placed: list[tuple[float, float, float, float]],
    node_rects: list[tuple[float, float, float, float]],
) -> tuple[float, float, bool, bool] | None:
    """First anchor whose label clears every card and earlier labels.

    When no anchor is free the least-bad one is used and the caller raises a
    warning, so a label is never silently dropped.
    """
    best: tuple[int, int, float, float] | None = None
    for x, y in anchors:
        rect = (
            x - text_width / 2,
            y - EDGE_LABEL_HEIGHT / 2,
            text_width,
            EDGE_LABEL_HEIGHT,
        )
        hits_card = sum(
            1
            for node_rect in node_rects
            if _rects_overlap(rect, node_rect, EDGE_LABEL_GAP)
        )
        hits_label = sum(
            1 for other in placed if _rects_overlap(rect, other, EDGE_LABEL_GAP)
        )
        if hits_card == 0 and hits_label == 0:
            placed.append(rect)
            return x, y, False, False
        if best is None or (hits_card, hits_label) < best[:2]:
            best = (hits_card, hits_label, x, y)
    if best is None:
        return None
    hits_card, hits_label, x, y = best
    placed.append(
        (
            x - text_width / 2,
            y - EDGE_LABEL_HEIGHT / 2,
            text_width,
            EDGE_LABEL_HEIGHT,
        )
    )
    return x, y, hits_card > 0, hits_label > 0


def _node_markup(box: NodeBox, prefix: str, label_plan: NodeLabelPlan) -> str:
    node = box.spec
    color = KIND_COLORS.get(node.kind, KIND_COLORS["generic"])
    tint = _rgba(color, 0.12)
    lines = label_plan.lines or [""]
    center_y = box.y + box.height / 2
    text_x = box.x + NODE_TEXT_LEFT

    if len(lines) == 1:
        label_svg = (
            f'<text x="{text_x}" y="{_n(center_y + 4)}" font-size="13" font-weight="600" '
            f'fill="{INK}">{_text(lines[0])}</text>'
            f'<text x="{text_x}" y="{_n(center_y + 22)}" font-size="8.5" font-weight="600" '
            f'fill="{FAINT}" letter-spacing="1.2">{node.kind.upper()}</text>'
        )
    else:
        label_svg = (
            f'<text x="{text_x}" y="{_n(center_y - 3)}" font-size="13" font-weight="600" '
            f'fill="{INK}">{_text(lines[0])}</text>'
            f'<text x="{text_x}" y="{_n(center_y + 13)}" font-size="13" font-weight="600" '
            f'fill="{INK}">{_text(lines[1])}</text>'
            f'<text x="{text_x}" y="{_n(center_y + 30)}" font-size="8.5" font-weight="600" '
            f'fill="{FAINT}" letter-spacing="1.2">{node.kind.upper()}</text>'
        )

    return f"""<g class="mb-node" data-node-id="{_attr(node.id)}">
<title>{_text(node.label)}</title>
<rect x="{_n(box.x)}" y="{_n(box.y)}" width="{_n(box.width)}" height="{_n(box.height)}" rx="16"
      fill="#FFFFFF" stroke="{LINE}" stroke-width="1" filter="url(#{prefix}-card-shadow)"/>
<rect x="{_n(box.x + 9)}" y="{_n(box.y + 28)}" width="3" height="{_n(box.height - 56)}" rx="1.5"
      fill="{color}"/>
<circle cx="{_n(box.x + 42)}" cy="{_n(center_y)}" r="20" fill="{tint}"/>
<use href="#{prefix}-icon-{node.kind}" x="{_n(box.x + 30)}" y="{_n(center_y - 12)}" width="24" height="24"
     style="color:{color}"/>
{label_svg}
</g>"""


def _edge_label(label: str, x: float, y: float, text_width: float) -> str:
    left = x - text_width / 2
    top = y - EDGE_LABEL_HEIGHT / 2
    return (
        f'<g class="mb-edge-label">'
        f'<rect x="{_n(left)}" y="{_n(top)}" width="{_n(text_width)}" height="{_n(EDGE_LABEL_HEIGHT)}" '
        f'rx="11" fill="#FFFFFF" stroke="{LINE}" stroke-width="1"/>'
        f'<text x="{_n(x)}" y="{_n(y + 3.6)}" font-size="10.5" fill="#51637D" '
        f'text-anchor="middle">{_text(label)}</text>'
        f'</g>'
    )


def _rgba(hex_color: str, alpha: float) -> str:
    value = hex_color.lstrip("#")
    red, green, blue = (int(value[index : index + 2], 16) for index in (0, 2, 4))
    return f"rgba({red}, {green}, {blue}, {alpha})"


def _n(value: float) -> str:
    rounded = round(value, 2)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:.2f}"


def _text(value: str) -> str:
    return escape(sanitize_xml_text(value))


def _attr(value: str) -> str:
    return escape(sanitize_xml_text(value), {'"': "&quot;", "'": "&apos;"})
