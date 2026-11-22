"""API contract shared between Mind Bridge Engine 2 and Engine 3.

Engine 2 sends a *visual instruction*: a validated, structured description of
what should appear on screen. Engine 3 never interprets speech or intent on
its own; it only renders this contract.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app import limits

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
SCHEMA_VERSION = "1.0"
SUPPORTED_SCHEMA_VERSIONS: tuple[str, ...] = (SCHEMA_VERSION,)

NodeKind = Literal[
    "bus",
    "server",
    "passenger",
    "phone",
    "database",
    "cloud",
    "service",
    "sensor",
    "generic",
]

AssetKind = Literal["svg", "image"]
RenderMode = Literal["svg", "image"]
EdgeStyle = Literal["solid", "dashed"]


def _metadata_depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_metadata_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_metadata_depth(item) for item in value), default=0)
    return 0


def _validate_metadata(value: dict[str, Any]) -> dict[str, Any]:
    depth = _metadata_depth(value)
    if depth > limits.METADATA_MAX_DEPTH:
        raise ValueError(
            f"metadata must be at most {limits.METADATA_MAX_DEPTH} levels deep (got {depth})"
        )
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata must be JSON-serializable") from exc
    if len(encoded) > limits.METADATA_MAX_BYTES:
        raise ValueError(
            f"metadata must serialize to at most {limits.METADATA_MAX_BYTES} bytes "
            f"(got {len(encoded)})"
        )
    return value


class Position(BaseModel):
    """Top-left position relative to the diagram content area, in pixels."""

    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0, le=8000)
    y: float = Field(ge=0, le=8000)


class NodeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN, description="Stable identifier used by edges and scene updates.")
    label: str = Field(min_length=1, max_length=80)
    kind: NodeKind = "generic"
    group: str | None = Field(default=None, max_length=64, description="Optional semantic group, e.g. 'fleet'.")
    position: Position | None = Field(
        default=None,
        description="Optional manual position. If *every* node has one, auto-layout is skipped.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Free-form, never rendered.")

    @field_validator("metadata")
    @classmethod
    def _metadata_limits(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_metadata(value)


class EdgeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(
        default=None,
        pattern=ID_PATTERN,
        description="Optional but recommended. Auto-derived as '<source>-><target>' when omitted.",
    )
    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)
    label: str | None = Field(default=None, max_length=48)
    style: EdgeStyle = "solid"
    show_arrow: bool = True


class DiagramOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: Literal["LR", "TB"] = Field(default="LR", description="Layered layout direction.")
    show_edge_labels: bool = True


class VisualInstruction(BaseModel):
    """Input payload accepted by POST /api/v1/visualize."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(
        default=SCHEMA_VERSION,
        max_length=16,
        description="Instruction contract version. Only supported versions are accepted.",
    )
    scene_id: str = Field(pattern=ID_PATTERN)
    type: Literal["diagram"] = "diagram"
    render_mode: RenderMode = Field(
        default="svg",
        description=(
            "Output mode. 'svg' (default) is the deterministic vector diagram; "
            "'image' explicitly requests one AI-generated raster image. Image mode "
            "is never inferred from provider configuration or API-key presence."
        ),
    )
    title: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=400)
    options: DiagramOptions = Field(default_factory=DiagramOptions)
    nodes: list[NodeSpec] = Field(min_length=1, max_length=80)
    edges: list[EdgeSpec] = Field(default_factory=list, max_length=160)

    @field_validator("schema_version")
    @classmethod
    def _supported_schema_version(cls, value: str) -> str:
        if value not in SUPPORTED_SCHEMA_VERSIONS:
            supported = ", ".join(SUPPORTED_SCHEMA_VERSIONS)
            raise ValueError(
                f"Unsupported schema_version '{value}'. Supported versions: {supported}."
            )
        return value

    @model_validator(mode="after")
    def _validate_graph(self) -> VisualInstruction:
        node_ids = [node.id for node in self.nodes]
        duplicates = {node_id for node_id in node_ids if node_ids.count(node_id) > 1}
        if duplicates:
            raise ValueError(f"Duplicate node ids: {', '.join(sorted(duplicates))}")

        known = set(node_ids)
        seen_edges: set[str] = set()
        for edge in self.edges:
            if edge.source not in known:
                raise ValueError(f"Edge '{edge.id or '?'}' references unknown source node '{edge.source}'")
            if edge.target not in known:
                raise ValueError(f"Edge '{edge.id or '?'}' references unknown target node '{edge.target}'")
            if edge.source == edge.target:
                raise ValueError(f"Self-loop edges are not supported (node '{edge.source}')")
            if edge.id is None:
                edge.id = f"{edge.source}->{edge.target}"
            if edge.id in seen_edges:
                raise ValueError(f"Duplicate edge id '{edge.id}'; give parallel edges explicit ids")
            seen_edges.add(edge.id)
        return self


class NodePatch(BaseModel):
    """Partial update for a single node, used by the scene update endpoint."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    label: str | None = Field(default=None, min_length=1, max_length=80)
    kind: NodeKind | None = None
    group: str | None = Field(default=None, max_length=64)
    position: Position | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("metadata")
    @classmethod
    def _metadata_limits(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return value if value is None else _validate_metadata(value)


class SceneUpdate(BaseModel):
    """Incremental scene change for POST /api/v1/scenes/{scene_id}/update."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Optimistic-concurrency guard. When set, the update is rejected with 409 "
            "unless the scene is still at this revision."
        ),
    )
    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=400)
    options: DiagramOptions | None = None
    render_mode: RenderMode | None = Field(
        default=None,
        description=(
            "Optional explicit render-mode change. Omit to keep the scene's stored "
            "mode; 'svg' or 'image' switches it for the resulting revision."
        ),
    )
    nodes_to_add: list[NodeSpec] = Field(default_factory=list, max_length=40)
    nodes_to_update: list[NodePatch] = Field(default_factory=list, max_length=40)
    nodes_to_remove: list[str] = Field(default_factory=list, max_length=40)
    edges_to_add: list[EdgeSpec] = Field(default_factory=list, max_length=80)
    edges_to_remove: list[str] = Field(default_factory=list, max_length=80)

    @model_validator(mode="after")
    def _fill_edge_ids(self) -> SceneUpdate:
        for edge in self.edges_to_add:
            if edge.id is None:
                edge.id = f"{edge.source}->{edge.target}"
        return self


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class AssetSummary(BaseModel):
    asset_id: str
    kind: AssetKind
    content_hash: str
    mime_type: str
    width: int | None = None
    height: int | None = None
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    provider: str
    reuse_count: int = 0
    url: str
    thumbnail_url: str
    created_at: str
    last_used_at: str


class SceneRevisionSummary(BaseModel):
    revision: int
    asset_id: str
    created_at: str


class SceneDetail(BaseModel):
    scene_id: str
    schema_version: str = SCHEMA_VERSION
    type: Literal["diagram"] = "diagram"
    render_mode: RenderMode = "svg"
    title: str
    description: str | None = None
    options: DiagramOptions
    nodes: list[NodeSpec]
    edges: list[EdgeSpec]
    current_revision: int
    created_at: str
    updated_at: str
    revisions: list[SceneRevisionSummary] = Field(default_factory=list)
    current_asset: AssetSummary | None = None
    svg: str | None = Field(default=None, description="Inline SVG of the current revision (diagram scenes).")
    warnings: list[str] = Field(
        default_factory=list,
        description="Layout warnings for the current instruction (deterministic, also returned on cache hits).",
    )


class VisualizeResponse(BaseModel):
    scene_id: str
    schema_version: str = SCHEMA_VERSION
    render_mode: RenderMode = "svg"
    revision: int
    cache_hit: bool = Field(description="True when an identical asset was reused from Visual Asset Memory.")
    asset: AssetSummary
    svg: str | None = Field(default=None, description="Ready-to-render SVG markup.")
    warnings: list[str] = Field(default_factory=list)


class AssetListResponse(BaseModel):
    items: list[AssetSummary]
    total: int


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    engine: str
    version: str
    database: str
    storage: str
    timestamp: str
    schema_version: str = SCHEMA_VERSION
    schema_versions: list[str] = Field(default_factory=lambda: list(SUPPORTED_SCHEMA_VERSIONS))
