"""FastAPI application entry point.

Run from the ``backend`` folder:
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config import Settings
from app.image_providers.base import ImageProvider
from app.image_providers.factory import build_image_provider
from app.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from app.observability import configure_logging
from app.schemas.scene import SCHEMA_VERSION, SUPPORTED_SCHEMA_VERSIONS
from app.services.asset_manager import AssetManager
from app.services.scene_service import SceneService
from app.storage.database import Database

DESCRIPTION = (
    "Engine 3 turns structured visual instructions from Engine 2 into SVG "
    "diagrams, remembers generated assets and serves them to the frontend."
)


def create_app(
    settings: Settings | None = None,
    image_provider: ImageProvider | None = None,
) -> FastAPI:
    settings = settings or Settings.from_environment()
    if image_provider is None:
        image_provider = build_image_provider(settings)
    settings.ensure_directories()
    configure_logging()

    database = Database(settings.db_path)
    database.initialize()
    assets = AssetManager(database, settings.assets_dir, settings.api_prefix)
    scene_service = SceneService(
        database,
        assets,
        image_provider=image_provider,
        settings=settings,
    )

    application = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description=DESCRIPTION,
    )
    application.state.settings = settings
    application.state.database = database
    application.state.assets = assets
    application.state.scene_service = scene_service
    application.state.image_provider = image_provider

    # Last middleware added is the outermost layer: CORS wraps request
    # correlation, which wraps the body-size guard, so error responses still
    # carry CORS and ``X-Request-ID`` headers.
    application.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    application.add_middleware(RequestContextMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    application.include_router(router, prefix=settings.api_prefix)

    @application.get("/", include_in_schema=False)
    def index() -> dict:
        return {
            "engine": "Mind Bridge Engine 3",
            "status": "ok",
            "docs": "/docs",
            "api_prefix": settings.api_prefix,
            "schema_version": SCHEMA_VERSION,
            "schema_versions": list(SUPPORTED_SCHEMA_VERSIONS),
            "max_request_bytes": settings.max_request_bytes,
            "health": f"{settings.api_prefix}/health",
            "visualize": f"{settings.api_prefix}/visualize",
        }

    return application


app = create_app()
