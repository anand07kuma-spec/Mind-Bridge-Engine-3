"""Public HTTP API for Engine 3.

Contract summary (also documented in docs/ARCHITECTURE.md):

POST   /api/v1/visualize                      full visual instruction -> scene
POST   /api/v1/scenes/{scene_id}/update       incremental scene change
GET    /api/v1/scenes/{scene_id}              scene + revisions + current SVG
GET    /api/v1/assets                         Visual Asset Memory listing
GET    /api/v1/assets/{asset_id}              asset metadata
GET    /api/v1/assets/{asset_id}/content      raw asset bytes
GET    /api/v1/assets/{asset_id}/thumbnail    thumbnail bytes
GET    /api/v1/samples/visual-instruction     bundled demo instruction
GET    /api/v1/health                         liveness + readiness probe
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import Settings
from app.image_providers.base import (
    ImageProviderAuthError,
    ImageProviderConfigurationError,
    ImageProviderContentPolicyError,
    ImageProviderError,
    ImageProviderNotConfiguredError,
    ImageProviderRateLimitError,
    ImageProviderResponseError,
    ImageProviderTimeoutError,
    ImageProviderUnavailableError,
)
from app.observability import API_LOGGER_NAME
from app.schemas.scene import (
    AssetKind,
    AssetListResponse,
    AssetSummary,
    HealthResponse,
    SceneDetail,
    SceneUpdate,
    VisualInstruction,
    VisualizeResponse,
)
from app.services.asset_manager import AssetIntegrityError, utc_now
from app.services.scene_service import (
    AssetRecoveryError,
    SceneBusyError,
    SceneConflictError,
    SceneNotFoundError,
    SceneService,
    SceneStorageError,
    SceneUpdateError,
)
from app.storage.database import Database

router = APIRouter()
logger = logging.getLogger(API_LOGGER_NAME)


class OrphanedAssetInfo(BaseModel):
    asset_id: str
    kind: AssetKind
    content_hash: str
    file_path: str
    reason: str


class AssetGcReport(BaseModel):
    """Read-only inventory; this milestone never deletes assets."""

    dry_run: bool = True
    asset_count: int
    referenced_asset_count: int
    orphaned_assets: list[OrphanedAssetInfo] = Field(default_factory=list)
    orphaned_files: list[str] = Field(default_factory=list)
    missing_files: list[str] = Field(default_factory=list)


def _service(request: Request) -> SceneService:
    return request.app.state.scene_service


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _database(request: Request) -> Database:
    return request.app.state.database


def _image_provider_http_error(exc: ImageProviderError) -> HTTPException:
    """Translate the safe provider error taxonomy into public HTTP responses.

    Provider messages are already constructed without API keys, prompts or raw
    response bodies, so ``str(exc)`` is safe to expose. Credential failures are
    reported as ``502`` rather than ``401``/``403`` because the caller cannot fix
    a server-side key; only the operator can.
    """
    if isinstance(exc, (ImageProviderNotConfiguredError, ImageProviderUnavailableError)):
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif isinstance(exc, ImageProviderAuthError):
        status_code = status.HTTP_502_BAD_GATEWAY
    elif isinstance(exc, ImageProviderRateLimitError):
        status_code = status.HTTP_429_TOO_MANY_REQUESTS
    elif isinstance(exc, ImageProviderTimeoutError):
        status_code = status.HTTP_504_GATEWAY_TIMEOUT
    elif isinstance(exc, ImageProviderContentPolicyError):
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    elif isinstance(exc, ImageProviderResponseError):
        status_code = status.HTTP_502_BAD_GATEWAY
    elif isinstance(exc, ImageProviderConfigurationError):
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    else:
        status_code = status.HTTP_502_BAD_GATEWAY

    headers: dict[str, str] | None = None
    if isinstance(exc, ImageProviderRateLimitError) and exc.retry_after is not None:
        headers = {"Retry-After": str(max(0, int(exc.retry_after)))}
    return HTTPException(status_code=status_code, detail=str(exc), headers=headers)


@router.get("/health", response_model=HealthResponse, summary="Liveness and readiness")
def health(request: Request) -> HealthResponse:
    settings = _settings(request)
    database_status = "ok"
    try:
        with _database(request).session() as connection:
            connection.execute("SELECT 1")
    except Exception:  # noqa: BLE001 - a readiness probe must never crash
        database_status = "error"

    storage_status = "ok" if settings.assets_dir.is_dir() else "missing"
    overall: Literal["ok", "degraded"] = (
        "ok" if database_status == "ok" and storage_status == "ok" else "degraded"
    )
    return HealthResponse(
        status=overall,
        engine="mindbridge-engine3",
        version=settings.version,
        database=database_status,
        storage=storage_status,
        timestamp=utc_now(),
    )


@router.post(
    "/visualize",
    response_model=VisualizeResponse,
    status_code=status.HTTP_200_OK,
    summary="Render a visual instruction (creates a new scene revision)",
)
def visualize(
    instruction: VisualInstruction,
    request: Request,
    expected_revision: int | None = Query(
        default=None,
        ge=0,
        description=(
            "Optimistic-concurrency guard. Use 0 to create a new scene and the current "
            "revision to replace an existing scene. Omit for legacy unguarded behavior."
        ),
    ),
) -> VisualizeResponse:
    try:
        result = _service(request).visualize(instruction, expected_revision=expected_revision)
    except (SceneBusyError, SceneConflictError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except AssetIntegrityError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    except (AssetRecoveryError, SceneStorageError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except ImageProviderError as exc:
        logger.warning(
            "image.provider_error",
            extra={
                "scene_id": instruction.scene_id,
                "provider": exc.provider,
                "model": exc.model,
                "retryable": exc.retryable,
            },
        )
        raise _image_provider_http_error(exc) from exc

    logger.info(
        "scene.visualized",
        extra={
            "scene_id": result.scene_id,
            "revision": result.revision,
            "cache_hit": result.cache_hit,
            "warning_count": len(result.warnings),
        },
    )
    return result


@router.post(
    "/scenes/{scene_id}/update",
    response_model=VisualizeResponse,
    summary="Apply an incremental update to an existing scene",
)
def update_scene(scene_id: str, patch: SceneUpdate, request: Request) -> VisualizeResponse:
    try:
        result = _service(request).update_scene(scene_id, patch)
    except SceneNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (SceneConflictError, SceneBusyError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except AssetIntegrityError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    except (AssetRecoveryError, SceneStorageError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except ImageProviderError as exc:
        logger.warning(
            "image.provider_error",
            extra={
                "scene_id": scene_id,
                "provider": exc.provider,
                "model": exc.model,
                "retryable": exc.retryable,
            },
        )
        raise _image_provider_http_error(exc) from exc
    except SceneUpdateError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    logger.info(
        "scene.updated",
        extra={
            "scene_id": result.scene_id,
            "revision": result.revision,
            "cache_hit": result.cache_hit,
            "warning_count": len(result.warnings),
        },
    )
    return result


@router.get("/scenes/{scene_id}", response_model=SceneDetail, summary="Get the latest scene state")
def get_scene(scene_id: str, request: Request) -> SceneDetail:
    try:
        scene = _service(request).get_scene(scene_id)
    except SceneNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except AssetIntegrityError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    except (AssetRecoveryError, SceneStorageError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    logger.info(
        "scene.fetched",
        extra={"scene_id": scene.scene_id, "revision": scene.current_revision},
    )
    return scene


@router.get("/assets", response_model=AssetListResponse, summary="List Visual Asset Memory")
def list_assets(
    request: Request,
    kind: AssetKind | None = Query(default=None, description="Filter by asset kind"),
    q: str | None = Query(default=None, max_length=80, description="Substring match on tags/category/metadata"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AssetListResponse:
    items, total = _service(request).list_assets(kind=kind, query=q, limit=limit, offset=offset)
    return AssetListResponse(items=[AssetSummary(**item) for item in items], total=total)


@router.get(
    "/assets/gc",
    response_model=AssetGcReport,
    summary="Dry-run report of orphaned assets and stray files (never deletes)",
)
def asset_gc_report(request: Request) -> AssetGcReport:
    report = _service(request).asset_gc_report()
    logger.info(
        "assets.gc_report",
        extra={
            "dry_run": report["dry_run"],
            "orphaned_assets": len(report["orphaned_assets"]),
            "orphaned_files": len(report["orphaned_files"]),
            "missing_files": len(report["missing_files"]),
        },
    )
    return AssetGcReport(**report)


@router.get("/assets/{asset_id}", response_model=AssetSummary, summary="Get asset metadata")
def get_asset(asset_id: str, request: Request) -> AssetSummary:
    asset = _service(request).get_asset(asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset '{asset_id}' was not found")
    return AssetSummary(**asset)


def _serve_asset(asset_id: str, request: Request, thumbnail: bool) -> FileResponse:
    try:
        resolved = _service(request).get_asset_file(asset_id, thumbnail=thumbnail)
    except AssetIntegrityError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    except (AssetRecoveryError, SceneStorageError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    if resolved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset '{asset_id}' was not found")
    path, media_type, recovered = resolved
    headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    if recovered:
        # Structured signal without changing the raw-bytes response contract.
        headers["X-MindBridge-Asset-Recovered"] = "true"
    return FileResponse(path, media_type=media_type, headers=headers)


@router.get("/assets/{asset_id}/content", summary="Serve raw asset content")
def get_asset_content(asset_id: str, request: Request) -> FileResponse:
    return _serve_asset(asset_id, request, thumbnail=False)


@router.get("/assets/{asset_id}/thumbnail", summary="Serve a thumbnail (SVG assets reuse the vector)")
def get_asset_thumbnail(asset_id: str, request: Request) -> FileResponse:
    return _serve_asset(asset_id, request, thumbnail=True)


@router.get("/samples/visual-instruction", summary="Bundled bus-tracking demo instruction")
def sample_instruction(request: Request) -> dict:
    path = _settings(request).sample_path
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sample instruction file is missing")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Sample instruction file is not valid JSON",
        ) from exc
