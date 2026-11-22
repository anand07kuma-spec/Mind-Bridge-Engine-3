"""Input limits shared by request validation and configuration.

Values are read from the environment at import time so Pydantic validators in
:mod:`app.schemas.scene` can enforce them. Defaults are generous for diagram
payloads (80 nodes / 160 edges) while bounding parser and memory work.
"""

from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


METADATA_MAX_DEPTH = _env_int("MIND_BRIDGE_METADATA_MAX_DEPTH", 5)
METADATA_MAX_BYTES = _env_int("MIND_BRIDGE_METADATA_MAX_BYTES", 4096)
