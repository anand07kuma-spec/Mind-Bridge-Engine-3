"""Visual Asset Memory.

Stores generated visuals on disk, keeps searchable metadata in SQLite and
implements exact-match reuse: if the same visual fingerprint was rendered
before, the existing asset is served instead of regenerating it.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.storage.database import Database

ASSET_ID_RE = re.compile(r"^ast_[a-z0-9]{8,32}$")


class AssetIntegrityError(RuntimeError):
    """Raised when stored asset bytes no longer match their recorded checksum."""

    def __init__(self, asset_id: str, reason: str):
        super().__init__(
            f"Asset '{asset_id}' failed integrity verification ({reason}); it was not served."
        )
        self.asset_id = asset_id
        self.reason = reason


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class PreparedAsset:
    """Files written for an asset that has not been committed to SQLite yet.

    ``prepare_asset`` writes the files and returns this payload. The row is only
    inserted later, inside the scene-persistence transaction, once the revision
    guard has passed. If the guard fails (or the transaction rolls back), only
    these freshly generated files are discarded.
    """

    asset_id: str
    kind: str
    content_hash: str
    provider: str
    mime_type: str
    file_path: str
    thumbnail_path: str | None
    width: int | None
    height: int | None
    category: str | None
    tags: list[str]
    metadata: dict[str, Any]
    created_at: str


class AssetManager:
    def __init__(self, db: Database, assets_dir: Path, api_prefix: str = "/api/v1"):
        self.db = db
        self.assets_dir = Path(assets_dir)
        self.api_prefix = api_prefix.rstrip("/")

    # ------------------------------------------------------------------
    # Fingerprinting
    # ------------------------------------------------------------------

    @staticmethod
    def compute_content_hash(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------

    @staticmethod
    def checksum(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def metadata_of(row: sqlite3.Row) -> dict[str, Any]:
        try:
            value = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _expected_checksum(self, row: sqlite3.Row, thumbnail: bool) -> str | None:
        metadata = self.metadata_of(row)
        if thumbnail and row["thumbnail_path"]:
            return metadata.get("thumbnail_sha256") or metadata.get("file_sha256")
        return metadata.get("file_sha256")

    def _verify_file(self, row: sqlite3.Row, path: Path, thumbnail: bool) -> None:
        expected = self._expected_checksum(row, thumbnail)
        if expected is not None and self._hash_file(path) != expected:
            raise AssetIntegrityError(row["asset_id"], "content does not match its recorded checksum")

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def find_exact(self, content_hash: str, kind: str, provider: str) -> dict[str, Any] | None:
        with self.db.session() as connection:
            row = connection.execute(
                "SELECT * FROM assets WHERE content_hash = ? AND kind = ? AND provider = ? "
                "ORDER BY created_at LIMIT 1",
                (content_hash, kind, provider),
            ).fetchone()
        return self._summary(row) if row else None

    def get(self, asset_id: str) -> dict[str, Any] | None:
        if not ASSET_ID_RE.match(asset_id):
            return None
        with self.db.session() as connection:
            row = connection.execute("SELECT * FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
        return self._summary(row) if row else None

    def get_row(self, asset_id: str) -> sqlite3.Row | None:
        if not ASSET_ID_RE.match(asset_id):
            return None
        with self.db.session() as connection:
            return connection.execute("SELECT * FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()

    def list_assets(
        self,
        kind: str | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append(
                "(tags_json LIKE ? ESCAPE '\\' OR category LIKE ? ESCAPE '\\' "
                "OR metadata_json LIKE ? ESCAPE '\\')"
            )
            pattern = f"%{escaped}%"
            params.extend([pattern, pattern, pattern])
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        with self.db.session() as connection:
            total = connection.execute(f"SELECT COUNT(*) AS count FROM assets {clause}", params).fetchone()["count"]
            rows = connection.execute(
                f"SELECT * FROM assets {clause} ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [self._summary(row) for row in rows], int(total)

    def resolve_file(self, asset_id: str, thumbnail: bool = False) -> tuple[Path, str] | None:
        row = self.get_row(asset_id)
        if row is None:
            return None
        relative = row["thumbnail_path"] if thumbnail and row["thumbnail_path"] else row["file_path"]
        path = (self.assets_dir / relative).resolve()
        if not path.is_relative_to(self.assets_dir.resolve()):
            return None
        if not path.is_file():
            return None
        self._verify_file(row, path, thumbnail)
        media_type = row["mime_type"] if not thumbnail else (mimetypes.guess_type(path.name)[0] or row["mime_type"])
        return path, media_type

    def read_svg(self, asset_id: str) -> str | None:
        row = self.get_row(asset_id)
        if row is None or row["kind"] != "svg":
            return None
        path = (self.assets_dir / row["file_path"]).resolve()
        if not path.is_relative_to(self.assets_dir.resolve()) or not path.is_file():
            return None
        self._verify_file(row, path, thumbnail=False)
        return path.read_text(encoding="utf-8")

    def restore_file(
        self,
        asset_id: str,
        data: bytes,
        thumbnail: bytes | None = None,
        namespace: str | None = None,
    ) -> bool:
        """Rewrite a stored asset whose file disappeared, keeping its id and row.

        The recorded checksums are refreshed so later reads verify the restored
        bytes instead of the lost originals.
        """
        row = self.get_row(asset_id)
        if row is None or row["kind"] != "svg":
            return False
        thumbnail_bytes = thumbnail if thumbnail is not None else data
        metadata = self.metadata_of(row)
        metadata["file_sha256"] = self.checksum(data)
        if row["thumbnail_path"]:
            metadata["thumbnail_sha256"] = self.checksum(thumbnail_bytes)
        if namespace:
            metadata["namespace"] = namespace
        metadata_json = json.dumps(metadata, ensure_ascii=False)

        try:
            self._write_stored_file(row["file_path"], data)
            if row["thumbnail_path"]:
                self._write_stored_file(row["thumbnail_path"], thumbnail_bytes)
            self._write_metadata(asset_id, metadata_json)
            return True
        except OSError:
            pass
        except sqlite3.Error:
            return False

        # The original location is no longer writable: relocate the asset and
        # point the existing row at the new files so the DB stays truthful.
        relocated = f"svg/{asset_id}-restored.svg"
        relocated_thumb = f"thumbnails/{asset_id}-restored.svg" if row["thumbnail_path"] else None
        try:
            self._write_stored_file(relocated, data)
            if relocated_thumb:
                self._write_stored_file(relocated_thumb, thumbnail_bytes)
            with self.db.session(immediate=True) as connection:
                connection.execute(
                    "UPDATE assets SET file_path = ?, thumbnail_path = ?, metadata_json = ? "
                    "WHERE asset_id = ?",
                    (relocated, relocated_thumb, metadata_json, asset_id),
                )
            return True
        except (OSError, sqlite3.Error):
            for relative in (relocated, relocated_thumb):
                if relative:
                    (self.assets_dir / relative).unlink(missing_ok=True)
            return False

    def _write_metadata(self, asset_id: str, metadata_json: str) -> None:
        with self.db.session() as connection:
            connection.execute(
                "UPDATE assets SET metadata_json = ? WHERE asset_id = ?",
                (metadata_json, asset_id),
            )

    def _write_stored_file(self, relative: str, data: bytes) -> None:
        path = (self.assets_dir / relative).resolve()
        if not path.is_relative_to(self.assets_dir.resolve()):
            raise OSError(f"Refusing to write outside the asset store: {relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def prepare_asset(
        self,
        *,
        kind: str,
        data: bytes,
        content_hash: str,
        extension: str,
        mime_type: str,
        provider: str,
        category: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        width: int | None = None,
        height: int | None = None,
        thumbnail: bytes | None = None,
    ) -> PreparedAsset:
        """Write an asset's files without inserting its database row."""
        asset_id = f"ast_{uuid4().hex[:16]}"
        folder = "svg" if kind == "svg" else "images"
        relative = f"{folder}/{asset_id}.{extension}"
        file_path = self.assets_dir / relative
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(data)

        thumbnail_relative: str | None = None
        thumbnail_bytes = thumbnail if thumbnail is not None else (data if kind == "svg" else None)
        if thumbnail_bytes is not None:
            thumbnail_relative = f"thumbnails/{asset_id}.{extension}"
            thumb_path = self.assets_dir / thumbnail_relative
            thumb_path.parent.mkdir(parents=True, exist_ok=True)
            thumb_path.write_bytes(thumbnail_bytes)

        stored_metadata: dict[str, Any] = {**(metadata or {}), "file_sha256": self.checksum(data)}
        if thumbnail_bytes is not None:
            stored_metadata["thumbnail_sha256"] = self.checksum(thumbnail_bytes)

        return PreparedAsset(
            asset_id=asset_id,
            kind=kind,
            content_hash=content_hash,
            provider=provider,
            mime_type=mime_type,
            file_path=relative,
            thumbnail_path=thumbnail_relative,
            width=width,
            height=height,
            category=category,
            tags=list(tags or []),
            metadata=stored_metadata,
            created_at=utc_now(),
        )

    def insert_prepared(
        self,
        connection: sqlite3.Connection,
        prepared: PreparedAsset,
    ) -> None:
        """Insert a prepared asset row using the caller's transaction connection."""
        connection.execute(
            """
            INSERT INTO assets (
                asset_id, kind, content_hash, file_path, thumbnail_path, width, height,
                category, tags_json, metadata_json, provider, mime_type, reuse_count,
                created_at, last_used_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                prepared.asset_id,
                prepared.kind,
                prepared.content_hash,
                prepared.file_path,
                prepared.thumbnail_path,
                prepared.width,
                prepared.height,
                prepared.category,
                json.dumps(prepared.tags, ensure_ascii=False),
                json.dumps(prepared.metadata, ensure_ascii=False),
                prepared.provider,
                prepared.mime_type,
                prepared.created_at,
                prepared.created_at,
            ),
        )

    def find_exact_row(
        self,
        connection: sqlite3.Connection,
        content_hash: str,
        kind: str,
        provider: str,
    ) -> sqlite3.Row | None:
        """Exact-match lookup on the caller's transaction connection."""
        return connection.execute(
            "SELECT * FROM assets WHERE content_hash = ? AND kind = ? AND provider = ? "
            "ORDER BY created_at LIMIT 1",
            (content_hash, kind, provider),
        ).fetchone()

    def touch_in(self, connection: sqlite3.Connection, asset_id: str) -> None:
        """Record a reuse on the caller's transaction connection."""
        connection.execute(
            "UPDATE assets SET reuse_count = reuse_count + 1, last_used_at = ? WHERE asset_id = ?",
            (utc_now(), asset_id),
        )

    def discard_prepared(self, prepared: PreparedAsset) -> None:
        """Delete only the files staged for ``prepared``.

        Cleanup is best-effort: a locked file is left for the GC report and never
        turns an already-committed request into a failure.
        """
        for relative in (prepared.file_path, prepared.thumbnail_path):
            if not relative:
                continue
            try:
                (self.assets_dir / relative).unlink(missing_ok=True)
            except OSError:
                continue

    def create_asset(
        self,
        *,
        kind: str,
        data: bytes,
        content_hash: str,
        extension: str,
        mime_type: str,
        provider: str,
        category: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        width: int | None = None,
        height: int | None = None,
        thumbnail: bytes | None = None,
    ) -> dict[str, Any]:
        existing = self.find_exact(content_hash, kind, provider)
        if existing:
            return existing

        prepared = self.prepare_asset(
            kind=kind,
            data=data,
            content_hash=content_hash,
            extension=extension,
            mime_type=mime_type,
            provider=provider,
            category=category,
            tags=tags,
            metadata=metadata,
            width=width,
            height=height,
            thumbnail=thumbnail,
        )
        try:
            with self.db.session() as connection:
                self.insert_prepared(connection, prepared)
        except sqlite3.IntegrityError:
            # Another request created the identical asset first: drop our copy.
            self.discard_prepared(prepared)
            existing = self.find_exact(content_hash, kind, provider)
            if existing is None:
                raise
            return existing

        stored = self.get(prepared.asset_id)
        assert stored is not None
        return stored

    def touch(self, asset_id: str) -> None:
        """Record a cache reuse so the UI can show how often an asset was reused."""
        with self.db.session() as connection:
            self.touch_in(connection, asset_id)

    def count(self) -> int:
        with self.db.session() as connection:
            return int(connection.execute("SELECT COUNT(*) AS count FROM assets").fetchone()["count"])

    # ------------------------------------------------------------------
    # Maintenance (read-only in this milestone)
    # ------------------------------------------------------------------

    def garbage_collection_report(self) -> dict[str, Any]:
        """Dry-run inventory of unused assets and stray files.

        Deliberately read-only: it never deletes rows or files, so an operator
        can review the report before any future cleanup exists.
        """
        with self.db.session() as connection:
            rows = connection.execute(
                "SELECT asset_id, kind, content_hash, file_path, thumbnail_path FROM assets "
                "ORDER BY asset_id"
            ).fetchall()
            referenced = {
                row["asset_id"]
                for row in connection.execute("SELECT DISTINCT asset_id FROM scene_revisions").fetchall()
            }

        known_paths = {row["file_path"] for row in rows}
        known_paths.update(row["thumbnail_path"] for row in rows if row["thumbnail_path"])

        disk_files: set[str] = set()
        for folder in ("svg", "images", "thumbnails"):
            base = self.assets_dir / folder
            if not base.is_dir():
                continue
            for path in base.rglob("*"):
                if path.is_file():
                    disk_files.add(path.relative_to(self.assets_dir).as_posix())

        orphaned_assets = [
            {
                "asset_id": row["asset_id"],
                "kind": row["kind"],
                "content_hash": row["content_hash"],
                "file_path": row["file_path"],
                "reason": "not referenced by any scene revision",
            }
            for row in rows
            if row["asset_id"] not in referenced
        ]
        missing_files = sorted(
            f"{row['asset_id']}:{row['file_path']}"
            for row in rows
            if not (self.assets_dir / row["file_path"]).is_file()
        )

        return {
            "dry_run": True,
            "asset_count": len(rows),
            "referenced_asset_count": len({row["asset_id"] for row in rows} & referenced),
            "orphaned_assets": orphaned_assets,
            "orphaned_files": sorted(disk_files - known_paths),
            "missing_files": missing_files,
        }

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def _summary(self, row: sqlite3.Row) -> dict[str, Any]:
        asset_id = row["asset_id"]
        return {
            "asset_id": asset_id,
            "kind": row["kind"],
            "content_hash": row["content_hash"],
            "mime_type": row["mime_type"],
            "width": row["width"],
            "height": row["height"],
            "category": row["category"],
            "tags": json.loads(row["tags_json"] or "[]"),
            "provider": row["provider"],
            "reuse_count": row["reuse_count"],
            "url": f"{self.api_prefix}/assets/{asset_id}/content",
            "thumbnail_url": f"{self.api_prefix}/assets/{asset_id}/thumbnail",
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
        }
