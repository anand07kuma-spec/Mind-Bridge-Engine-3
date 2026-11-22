"""SQLite persistence for scenes, revisions and the Visual Asset Memory."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS scenes (
    scene_id         TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    description      TEXT,
    instruction_json TEXT NOT NULL,
    current_revision INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scene_revisions (
    scene_id         TEXT NOT NULL REFERENCES scenes(scene_id) ON DELETE CASCADE,
    revision         INTEGER NOT NULL,
    instruction_json TEXT NOT NULL,
    asset_id         TEXT NOT NULL REFERENCES assets(asset_id),
    created_at       TEXT NOT NULL,
    PRIMARY KEY (scene_id, revision)
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id       TEXT PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('svg', 'image')),
    content_hash   TEXT NOT NULL,
    file_path      TEXT NOT NULL,
    thumbnail_path TEXT,
    width          INTEGER,
    height         INTEGER,
    category       TEXT,
    tags_json      TEXT NOT NULL DEFAULT '[]',
    metadata_json  TEXT NOT NULL DEFAULT '{}',
    provider       TEXT NOT NULL,
    mime_type      TEXT NOT NULL,
    reuse_count    INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    last_used_at   TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_exact_match
    ON assets (content_hash, kind, provider);
CREATE INDEX IF NOT EXISTS idx_assets_created_at ON assets (created_at);
CREATE INDEX IF NOT EXISTS idx_scene_revisions_scene ON scene_revisions (scene_id);
"""


class Database:
    """Small connection helper; one short-lived connection per request/unit of work."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.session() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def session(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Short-lived transaction.

        ``immediate=True`` acquires SQLite's write lock before the first read,
        which makes read-modify-write sequences such as revision allocation
        atomic under concurrent requests.
        """
        connection = self.connect()
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
