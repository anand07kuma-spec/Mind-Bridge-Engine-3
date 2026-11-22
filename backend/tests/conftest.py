from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings
from app.main import create_app

SAMPLE_PATH = PROJECT_ROOT / "sample_data" / "bus_tracking.json"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        assets_dir=tmp_path / "assets",
        db_path=tmp_path / "data" / "mindbridge-test.db",
        sample_path=SAMPLE_PATH,
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def sample_instruction() -> dict:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def instruction(sample_instruction: dict):
    from app.schemas.scene import VisualInstruction

    return VisualInstruction.model_validate(sample_instruction)
