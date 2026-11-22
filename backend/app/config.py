"""Runtime configuration for Mind Bridge Engine 3.

Everything is file-system based so the engine stays standalone: no external
database, no object storage and no paid service is required to run it.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else default


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 and math.isfinite(value) else default


def _env_non_negative_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw.strip() if raw and raw.strip() else default


def _env_prefix(name: str, default: str) -> str:
    raw = (os.environ.get(name) or default).strip().strip("/")
    return f"/{raw}" if raw else ""


@dataclass
class Settings:
    app_name: str = "Mind Bridge · Engine 3"
    version: str = "0.1.0"
    api_prefix: str = field(default_factory=lambda: _env_prefix("MIND_BRIDGE_API_PREFIX", "/api/v1"))
    max_request_bytes: int = field(
        default_factory=lambda: _env_int("MIND_BRIDGE_MAX_REQUEST_BYTES", 1_048_576)
    )

    # Image generation (provider-independent and disabled by default)
    image_provider: str = field(
        default_factory=lambda: _env_str("MIND_BRIDGE_IMAGE_PROVIDER", "none").lower()
    )
    image_model: str = field(
        default_factory=lambda: _env_str("MIND_BRIDGE_IMAGE_MODEL", "")
    )
    image_size: str = field(
        default_factory=lambda: _env_str("MIND_BRIDGE_IMAGE_SIZE", "1024x1024")
    )
    image_quality: str = field(
        default_factory=lambda: _env_str("MIND_BRIDGE_IMAGE_QUALITY", "standard")
    )
    image_timeout_seconds: float = field(
        default_factory=lambda: _env_float("MIND_BRIDGE_IMAGE_TIMEOUT_SECONDS", 60.0)
    )
    image_max_retries: int = field(
        default_factory=lambda: _env_non_negative_int("MIND_BRIDGE_IMAGE_MAX_RETRIES", 0)
    )
    image_max_bytes: int = field(
        default_factory=lambda: _env_int("MIND_BRIDGE_IMAGE_MAX_BYTES", 20_971_520)
    )
    image_prompt_max_chars: int = field(
        default_factory=lambda: _env_int("MIND_BRIDGE_IMAGE_PROMPT_MAX_CHARS", 4_000)
    )
    image_prompt_template_version: str = field(
        default_factory=lambda: _env_str("MIND_BRIDGE_IMAGE_PROMPT_TEMPLATE_VERSION", "1")
    )

    # Storage
    project_root: Path = PROJECT_ROOT
    assets_dir: Path = field(
        default_factory=lambda: _env_path("MIND_BRIDGE_ASSETS_DIR", PROJECT_ROOT / "assets")
    )
    db_path: Path = field(
        default_factory=lambda: _env_path(
            "MIND_BRIDGE_DB_PATH", PROJECT_ROOT / "data" / "mindbridge.db"
        )
    )
    sample_path: Path = field(default_factory=lambda: PROJECT_ROOT / "sample_data" / "bus_tracking.json")

    # Integration
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: _env_list(
            "MIND_BRIDGE_CORS_ORIGINS",
            (
                "http://localhost:5173",
                "http://127.0.0.1:5173",
                "http://localhost:4173",
                "http://127.0.0.1:4173",
            ),
        )
    )

    # Backend-only secrets. ``repr=False`` keeps them out of logs and debugging
    # output; they are never included in API responses or frontend configuration.
    openai_api_key: str = field(
        default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""),
        repr=False,
    )
    gemini_api_key: str = field(
        default_factory=lambda: os.environ.get("GEMINI_API_KEY", ""),
        repr=False,
    )

    def ensure_directories(self) -> None:
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        for child in ("svg", "images", "thumbnails"):
            (self.assets_dir / child).mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_environment(cls) -> Settings:
        """Defaults already read MIND_BRIDGE_* variables at construction time."""
        return cls()
