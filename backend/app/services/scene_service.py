"""Scene orchestration: validate -> plan -> render -> remember -> serve."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config import Settings
from app.generators.svg_generator import (
    GENERATOR_NAME,
    GENERATOR_VERSION,
    PROVIDER_NAME,
    RenderResult,
    SvgGenerator,
    apply_namespace,
)
from app.image_providers.base import (
    GeneratedImage,
    ImageGenerationRequest,
    ImageProvider,
    ImageProviderNotConfiguredError,
)
from app.image_providers.prompt import PromptBuildResult, build_image_prompt
from app.image_providers.validation import validate_image_bytes
from app.schemas.scene import (
    AssetSummary,
    SceneDetail,
    SceneRevisionSummary,
    SceneUpdate,
    VisualInstruction,
    VisualizeResponse,
)
from app.services.asset_manager import AssetIntegrityError, AssetManager, PreparedAsset, utc_now
from app.storage.database import Database


class SceneNotFoundError(LookupError):
    def __init__(self, scene_id: str):
        super().__init__(f"Scene '{scene_id}' was not found")
        self.scene_id = scene_id


class SceneUpdateError(ValueError):
    """Raised when an incremental update cannot be applied safely."""


class SceneConflictError(RuntimeError):
    """Raised when an expected revision no longer matches the stored scene."""

    def __init__(self, scene_id: str, expected: int, actual: int):
        super().__init__(
            f"Scene '{scene_id}' is at revision {actual}, but revision {expected} was "
            "expected. Re-fetch the scene and retry."
        )
        self.scene_id = scene_id
        self.expected = expected
        self.actual = actual


class SceneBusyError(RuntimeError):
    """Raised when SQLite cannot obtain its write lock within the timeout."""

    def __init__(self, scene_id: str):
        super().__init__(
            f"Scene '{scene_id}' could not be written because the database is busy. "
            "Retry the request."
        )
        self.scene_id = scene_id


class SceneStorageError(RuntimeError):
    """Raised when a missing asset cannot be regenerated and persisted."""

    def __init__(self, scene_id: str):
        super().__init__(
            f"Scene '{scene_id}' references an asset whose file is missing and the asset "
            "store could not write a replacement. Check storage permissions."
        )
        self.scene_id = scene_id


class AssetRecoveryError(RuntimeError):
    """Raised when a missing asset cannot be rebuilt from stored scene data."""

    def __init__(self, asset_id: str, reason: str):
        super().__init__(f"Asset '{asset_id}' is missing and could not be recovered: {reason}.")
        self.asset_id = asset_id


class _ImageSingleFlight:
    """Per-process coalescing of identical image generation requests.

    Callers holding the same key are serialized. The first performs the provider
    call and persists the asset; later callers re-check the asset store and reuse
    it, so one process never pays twice for an identical visual.

    Limitation: the lock lives in memory. Multiple uvicorn workers, multiple
    hosts, or a process restart do not share it, so cross-process races can still
    issue duplicate provider requests. Correctness is unaffected: ``_persist_scene``
    adopts an identical committed asset and discards the loser's staged files.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[str, tuple[threading.Lock, int]] = {}

    @contextmanager
    def hold(self, key: str) -> Iterator[None]:
        with self._guard:
            lock, participants = self._entries.get(key, (threading.Lock(), 0))
            self._entries[key] = (lock, participants + 1)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._guard:
                current_lock, participants = self._entries[key]
                if participants <= 1:
                    del self._entries[key]
                else:
                    self._entries[key] = (current_lock, participants - 1)


class SceneService:
    def __init__(
        self,
        db: Database,
        assets: AssetManager,
        generator: SvgGenerator | None = None,
        image_provider: ImageProvider | None = None,
        settings: Settings | None = None,
    ):
        self.db = db
        self.assets = assets
        self.generator = generator or SvgGenerator()
        self.image_provider = image_provider
        self.settings = settings or Settings()
        self._image_single_flight = _ImageSingleFlight()

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------

    def visualize(
        self,
        instruction: VisualInstruction,
        expected_revision: int | None = None,
    ) -> VisualizeResponse:
        # Advisory fast-fail: the authoritative check still runs inside the
        # persistence transaction. Omitted guards keep the legacy behavior.
        if expected_revision is not None:
            self._check_expected_revision(instruction.scene_id, expected_revision)
        return self._render_scene(instruction, expected_revision=expected_revision)

    def _render_scene(
        self,
        instruction: VisualInstruction,
        expected_revision: int | None,
    ) -> VisualizeResponse:
        if instruction.render_mode == "image":
            return self._render_image_scene(instruction, expected_revision)
        return self._render_svg_scene(instruction, expected_revision)

    def _render_svg_scene(
        self,
        instruction: VisualInstruction,
        expected_revision: int | None,
    ) -> VisualizeResponse:
        content_hash = self.assets.compute_content_hash(self._fingerprint(instruction))
        namespace = self._document_namespace(instruction, content_hash)
        warnings: list[str] = []

        asset = self.assets.find_exact(content_hash, kind="svg", provider=PROVIDER_NAME)
        cache_hit = asset is not None
        prepared: PreparedAsset | None = None

        if asset is None:
            rendered = self.generator.generate(instruction, namespace=namespace)
            svg = rendered.svg
            warnings.extend(rendered.warnings)
            prepared = self._prepare_rendered(instruction, rendered, content_hash)
            asset_id = prepared.asset_id
        else:
            asset_id = asset["asset_id"]
            stored_svg = self.assets.read_svg(asset_id)
            if stored_svg is not None:
                svg = apply_namespace(stored_svg, namespace)
                warnings.extend(self.generator.warnings_for(instruction))
            else:
                # Database and filesystem disagree: rebuild the exact stored asset.
                asset_row = self.assets.get_row(asset_id)
                if asset_row is None:
                    raise AssetIntegrityError(asset_id, "the asset record is missing")
                warnings.extend(self.generator.warnings_for(instruction))
                svg, recovery_warnings = self._recover_asset(
                    asset_row, instruction, touch=False
                )
                warnings.extend(recovery_warnings)
                cache_hit = False

        revision, resolved_asset_id = self._persist_scene(
            instruction,
            asset_id,
            prepared=prepared,
            expected_revision=expected_revision,
        )
        asset = self.assets.get(resolved_asset_id)
        if asset is None:
            raise AssetIntegrityError(resolved_asset_id, "the asset record is missing")

        return VisualizeResponse(
            scene_id=instruction.scene_id,
            schema_version=instruction.schema_version,
            render_mode=instruction.render_mode,
            revision=revision,
            cache_hit=cache_hit,
            asset=AssetSummary(**asset),
            svg=svg,
            warnings=warnings,
        )

    def _prepare_rendered(
        self,
        instruction: VisualInstruction,
        rendered: RenderResult,
        content_hash: str,
    ) -> PreparedAsset:
        return self.assets.prepare_asset(
            kind="svg",
            data=rendered.svg.encode("utf-8"),
            content_hash=content_hash,
            extension="svg",
            mime_type="image/svg+xml",
            provider=PROVIDER_NAME,
            category="diagram",
            tags=sorted({node.kind for node in instruction.nodes} | {"diagram", "svg"}),
            metadata={
                "title": instruction.title,
                "generator": f"{GENERATOR_NAME}@{GENERATOR_VERSION}",
                "namespace": rendered.namespace,
                "direction": instruction.options.direction,
                "node_count": len(instruction.nodes),
                "edge_count": len(instruction.edges),
            },
            width=rendered.width,
            height=rendered.height,
        )

    def _render_image_scene(
        self,
        instruction: VisualInstruction,
        expected_revision: int | None,
    ) -> VisualizeResponse:
        provider = self._require_image_provider()
        prompt = build_image_prompt(
            instruction,
            max_chars=self.settings.image_prompt_max_chars,
            template_version=self.settings.image_prompt_template_version,
        )
        size = self.settings.image_size
        quality = self.settings.image_quality
        width, height = _parse_size(size) or (1024, 1024)
        content_hash = self.assets.compute_content_hash(
            self._image_fingerprint(
                instruction,
                provider=provider,
                prompt=prompt,
                size=size,
                quality=quality,
            )
        )

        # Serialize identical visuals within this process so a concurrent burst
        # cannot issue duplicate paid provider calls. The asset lookup is repeated
        # inside the lock because the first caller may have committed while this
        # request waited.
        with self._image_single_flight.hold(content_hash):
            asset = self.assets.find_exact(content_hash, kind="image", provider=provider.name)
            prepared: PreparedAsset | None = None

            if asset is None:
                generated = self._generate_image(
                    provider,
                    instruction,
                    prompt,
                    width=width,
                    height=height,
                )
                prepared = self._prepare_image(
                    instruction,
                    prompt,
                    generated,
                    content_hash,
                    size=size,
                    quality=quality,
                )
                asset_id = prepared.asset_id
                cache_hit = False
            else:
                asset_id = str(asset["asset_id"])
                cache_hit = True
                if self.assets.resolve_file(asset_id) is None:
                    # A stored image is never re-requested from the provider.
                    raise AssetRecoveryError(
                        asset_id,
                        "image assets are never regenerated automatically; "
                        "request image generation explicitly",
                    )

            revision, resolved_asset_id = self._persist_scene(
                instruction,
                asset_id,
                prepared=prepared,
                expected_revision=expected_revision,
            )

        resolved = self.assets.get(resolved_asset_id)
        if resolved is None:
            raise AssetIntegrityError(resolved_asset_id, "the asset record is missing")

        return VisualizeResponse(
            scene_id=instruction.scene_id,
            schema_version=instruction.schema_version,
            render_mode="image",
            revision=revision,
            cache_hit=cache_hit,
            asset=AssetSummary(**resolved),
            svg=None,
            warnings=[],
        )

    def _require_image_provider(self) -> ImageProvider:
        provider = self.image_provider
        if provider is None:
            raise ImageProviderNotConfiguredError(
                "No image provider is configured. SVG generation is fully functional."
            )
        capabilities = provider.capabilities()
        if not capabilities.available:
            reason = getattr(provider, "reason", None)
            raise ImageProviderNotConfiguredError(
                reason
                or "The configured image provider is unavailable. SVG generation is fully functional.",
                provider=provider.name,
                model=provider.model or None,
            )
        return provider

    def _generate_image(
        self,
        provider: ImageProvider,
        instruction: VisualInstruction,
        prompt: PromptBuildResult,
        *,
        width: int,
        height: int,
    ) -> GeneratedImage:
        request = ImageGenerationRequest(
            prompt=prompt.prompt,
            scene_id=instruction.scene_id,
            revision=0,
            width=width,
            height=height,
            quality=self.settings.image_quality,
            timeout_seconds=self.settings.image_timeout_seconds,
            metadata=prompt.safe_metadata(),
        )
        generated = provider.generate(request)
        validated = validate_image_bytes(
            generated.data,
            declared_mime_type=generated.mime_type,
            max_bytes=self.settings.image_max_bytes,
            provider=generated.provider or provider.name,
            model=generated.model or provider.model or None,
        )
        return GeneratedImage(
            data=validated.data,
            mime_type=validated.mime_type,
            extension=validated.extension,
            width=validated.width,
            height=validated.height,
            thumbnail=generated.thumbnail,
            provider=generated.provider or provider.name,
            model=generated.model or provider.model,
            revised_prompt=None,
            provider_request_id=generated.provider_request_id,
            metadata=dict(generated.metadata),
        )

    def _prepare_image(
        self,
        instruction: VisualInstruction,
        prompt: PromptBuildResult,
        generated: GeneratedImage,
        content_hash: str,
        *,
        size: str,
        quality: str,
    ) -> PreparedAsset:
        provider = generated.provider or (
            self.image_provider.name if self.image_provider is not None else ""
        )
        metadata: dict[str, Any] = {
            **prompt.safe_metadata(),
            "provider": provider,
            "model": generated.model,
            "size": size,
            "quality": quality,
            "render_mode": "image",
        }
        if generated.provider_request_id:
            metadata["provider_request_id"] = generated.provider_request_id

        return self.assets.prepare_asset(
            kind="image",
            data=generated.data,
            content_hash=content_hash,
            extension=generated.extension,
            mime_type=generated.mime_type,
            provider=provider,
            category="diagram",
            tags=sorted({node.kind for node in instruction.nodes} | {"diagram", "image"}),
            metadata=metadata,
            width=generated.width,
            height=generated.height,
            thumbnail=generated.thumbnail,
        )

    def _image_fingerprint(
        self,
        instruction: VisualInstruction,
        *,
        provider: ImageProvider,
        prompt: PromptBuildResult,
        size: str,
        quality: str,
    ) -> dict[str, Any]:
        """Identity of one paid image generation.

        Provider, model, configured size/quality and prompt template/hash are all
        part of the identity: changing any of them must produce a new asset rather
        than silently reusing an image generated under different conditions.
        """
        return {
            "render_mode": "image",
            "provider": provider.name,
            "model": provider.model,
            "size": size,
            "quality": quality,
            "prompt_template_version": prompt.template_version,
            "prompt_hash": prompt.prompt_hash,
            "visual": self._visual_identity(instruction),
        }

    def _document_namespace(self, instruction: VisualInstruction, content_hash: str) -> str:
        """Stable id prefix for one scene + visual; namespaces every SVG element id."""
        scene = re.sub(r"[^A-Za-z0-9_-]", "-", instruction.scene_id)[:32]
        return f"mb-{scene}-{content_hash[:10]}"

    def update_scene(self, scene_id: str, patch: SceneUpdate) -> VisualizeResponse:
        row = self._get_scene_row(scene_id)
        if row is None:
            raise SceneNotFoundError(scene_id)

        current_revision = int(row["current_revision"])
        if patch.expected_revision is not None and patch.expected_revision != current_revision:
            raise SceneConflictError(scene_id, patch.expected_revision, current_revision)

        # The patch was merged from ``current_revision``. Even when the client
        # omits ``expected_revision``, persist against that revision so a
        # concurrent update cannot silently overwrite this one.
        base_revision = (
            patch.expected_revision
            if patch.expected_revision is not None
            else current_revision
        )

        data: dict[str, Any] = json.loads(row["instruction_json"])
        fields = patch.model_fields_set

        if "title" in fields and patch.title is not None:
            data["title"] = patch.title
        if "description" in fields:
            data["description"] = patch.description
        if "options" in fields and patch.options is not None:
            data["options"] = patch.options.model_dump(mode="json")
        if "render_mode" in fields and patch.render_mode is not None:
            data["render_mode"] = patch.render_mode

        nodes: list[dict[str, Any]] = data["nodes"]
        edges: list[dict[str, Any]] = data["edges"]

        # 1. Explicit edge removals (before node removals, for clear errors).
        remove_edge_ids = set(patch.edges_to_remove)
        if remove_edge_ids:
            known_edge_ids = {edge.get("id") for edge in edges}
            missing = remove_edge_ids - known_edge_ids
            if missing:
                raise SceneUpdateError(f"Unknown edge ids in edges_to_remove: {', '.join(sorted(missing))}")
            edges = [edge for edge in edges if edge.get("id") not in remove_edge_ids]

        # 2. Node removals also drop every connected edge.
        remove_node_ids = set(patch.nodes_to_remove)
        if remove_node_ids:
            known_node_ids = {node["id"] for node in nodes}
            missing = remove_node_ids - known_node_ids
            if missing:
                raise SceneUpdateError(f"Unknown node ids in nodes_to_remove: {', '.join(sorted(missing))}")
            nodes = [node for node in nodes if node["id"] not in remove_node_ids]
            edges = [
                edge
                for edge in edges
                if edge["source"] not in remove_node_ids and edge["target"] not in remove_node_ids
            ]

        # 3. Node updates.
        for node_patch in patch.nodes_to_update:
            target = next((node for node in nodes if node["id"] == node_patch.id), None)
            if target is None:
                raise SceneUpdateError(f"Cannot update unknown node '{node_patch.id}'")
            provided = node_patch.model_fields_set
            if "label" in provided and node_patch.label is not None:
                target["label"] = node_patch.label
            if "kind" in provided and node_patch.kind is not None:
                target["kind"] = node_patch.kind
            if "group" in provided:
                target["group"] = node_patch.group
            if "position" in provided:
                target["position"] = (
                    node_patch.position.model_dump(mode="json") if node_patch.position else None
                )
            if "metadata" in provided and node_patch.metadata is not None:
                target["metadata"] = node_patch.metadata

        # 4. Node additions.
        existing_ids = {node["id"] for node in nodes}
        for node in patch.nodes_to_add:
            if node.id in existing_ids:
                raise SceneUpdateError(f"Node '{node.id}' already exists in scene '{scene_id}'")
            existing_ids.add(node.id)
            nodes.append(node.model_dump(mode="json"))

        # 5. Edge additions.
        existing_edge_ids = {edge.get("id") for edge in edges}
        for edge in patch.edges_to_add:
            if edge.id in existing_edge_ids:
                raise SceneUpdateError(f"Edge '{edge.id}' already exists in scene '{scene_id}'")
            existing_edge_ids.add(edge.id)
            edges.append(edge.model_dump(mode="json"))

        data["nodes"] = nodes
        data["edges"] = edges

        try:
            instruction = VisualInstruction.model_validate(data)
        except ValidationError as exc:
            raise SceneUpdateError(_format_validation_error(exc)) from exc

        return self._render_scene(instruction, expected_revision=base_revision)

    def get_scene(self, scene_id: str) -> SceneDetail:
        row = self._get_scene_row(scene_id)
        if row is None:
            raise SceneNotFoundError(scene_id)

        instruction = VisualInstruction.model_validate_json(row["instruction_json"])
        asset_id = self._current_asset_id(scene_id, int(row["current_revision"]))
        asset_row = self.assets.get_row(asset_id) if asset_id else None
        warnings: list[str] = (
            [] if instruction.render_mode == "image" else self.generator.warnings_for(instruction)
        )
        svg: str | None = None
        if asset_id is not None and asset_row is None:
            raise AssetIntegrityError(asset_id, "the asset record is missing")
        if asset_row is not None:
            stored_asset_id = asset_row["asset_id"]
            self._verify_asset_fingerprint(asset_row, instruction)
            if asset_row["kind"] == "svg":
                svg = self.assets.read_svg(stored_asset_id)
                if svg is None:
                    svg, recovery_warnings = self._recover_asset(asset_row, instruction)
                    warnings.extend(recovery_warnings)
                    asset_row = self.assets.get_row(stored_asset_id) or asset_row
                svg = apply_namespace(
                    svg,
                    self._document_namespace(instruction, asset_row["content_hash"]),
                )
            elif self.assets.resolve_file(stored_asset_id) is None:
                warnings.append(
                    "Image asset file is missing and is not regenerated automatically."
                )

        current_asset = self.assets.get(asset_id) if asset_id else None
        with self.db.session() as connection:
            revisions = connection.execute(
                "SELECT revision, asset_id, created_at FROM scene_revisions "
                "WHERE scene_id = ? ORDER BY revision DESC",
                (scene_id,),
            ).fetchall()

        return SceneDetail(
            scene_id=scene_id,
            schema_version=instruction.schema_version,
            render_mode=instruction.render_mode,
            title=instruction.title,
            description=instruction.description,
            type=instruction.type,
            options=instruction.options,
            nodes=instruction.nodes,
            edges=instruction.edges,
            current_revision=int(row["current_revision"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            revisions=[SceneRevisionSummary(**dict(revision)) for revision in revisions],
            current_asset=AssetSummary(**current_asset) if current_asset else None,
            svg=svg,
            warnings=warnings,
        )

    def get_asset(self, asset_id: str) -> dict[str, Any] | None:
        return self.assets.get(asset_id)

    def list_assets(
        self,
        kind: str | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        return self.assets.list_assets(kind=kind, query=query, limit=limit, offset=offset)

    def get_asset_file(
        self,
        asset_id: str,
        thumbnail: bool = False,
    ) -> tuple[Path, str, bool] | None:
        """Resolve an asset file, recovering it from stored scene data if missing.

        Returns ``None`` for unknown assets, otherwise ``(path, media_type,
        recovered)``. Integrity failures raise instead of serving bad bytes.
        """
        row = self.assets.get_row(asset_id)
        if row is None:
            return None
        resolved = self.assets.resolve_file(asset_id, thumbnail=thumbnail)
        if resolved is not None:
            path, media_type = resolved
            return path, media_type, False

        if row["kind"] != "svg":
            raise AssetRecoveryError(
                asset_id,
                "image assets are never regenerated automatically; "
                "request image generation explicitly",
            )

        instruction = self._instruction_for_asset(asset_id)
        if instruction is None:
            raise AssetRecoveryError(asset_id, "no stored scene instruction references it")
        self._recover_asset(row, instruction)

        resolved = self.assets.resolve_file(asset_id, thumbnail=thumbnail)
        if resolved is None:
            raise AssetRecoveryError(asset_id, "the restored file could not be read back")
        path, media_type = resolved
        return path, media_type, True

    def asset_gc_report(self) -> dict[str, Any]:
        return self.assets.garbage_collection_report()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _visual_identity(self, instruction: VisualInstruction) -> dict[str, Any]:
        """Fields that describe what the visual looks like.

        ``scene_id``, node metadata and groups are excluded: they carry no
        pixels, so two scenes that look identical share one asset. The SVG
        generator stamp is deliberately not part of this identity, so image
        fingerprints do not change when the SVG renderer is upgraded.
        """
        spec = instruction.model_dump(mode="json")
        return {
            "type": spec["type"],
            "title": spec["title"],
            "description": spec["description"],
            "options": spec["options"],
            "nodes": [
                {
                    "id": node["id"],
                    "label": node["label"],
                    "kind": node["kind"],
                    "position": node["position"],
                }
                for node in spec["nodes"]
            ],
            "edges": [
                {
                    "id": edge["id"],
                    "source": edge["source"],
                    "target": edge["target"],
                    "label": edge["label"],
                    "style": edge["style"],
                    "show_arrow": edge["show_arrow"],
                }
                for edge in spec["edges"]
            ],
        }

    def _fingerprint(
        self,
        instruction: VisualInstruction,
        generator: str | None = None,
    ) -> dict[str, Any]:
        """Visual identity of an SVG scene, stamped with the SVG generator."""
        return {
            "generator": generator or f"{GENERATOR_NAME}@{GENERATOR_VERSION}",
            **self._visual_identity(instruction),
        }

    @staticmethod
    def _stored_generator(asset_row: sqlite3.Row) -> str | None:
        value = AssetManager.metadata_of(asset_row).get("generator")
        return value if isinstance(value, str) else None

    def _fingerprint_hash(self, instruction: VisualInstruction, generator: str | None) -> str:
        return self.assets.compute_content_hash(self._fingerprint(instruction, generator=generator))

    def _verify_asset_fingerprint(
        self,
        asset_row: sqlite3.Row,
        instruction: VisualInstruction,
    ) -> None:
        """Reject a scene whose instruction no longer produces its stored asset.

        Assets created by older generator versions keep their own generator stamp
        in metadata, so an upgrade never rewrites a scene's identity.

        Image assets are intentionally skipped: their bytes come from an external
        model and cannot be reproduced from the instruction. The file checksum
        verified by ``resolve_file`` is the authoritative integrity check there.
        """
        if asset_row["kind"] != "svg":
            return
        stored_generator = self._stored_generator(asset_row)
        if stored_generator is None:
            return
        if self._fingerprint_hash(instruction, stored_generator) != asset_row["content_hash"]:
            raise AssetIntegrityError(
                asset_row["asset_id"],
                "the stored scene instruction does not match the asset fingerprint",
            )

    def _recover_asset(
        self,
        asset_row: sqlite3.Row,
        instruction: VisualInstruction,
        *,
        touch: bool = True,
    ) -> tuple[str, list[str]]:
        """Regenerate a missing SVG asset and verify it before restoring it.

        Recovery is only attempted when the stored instruction reproduces the
        asset's recorded fingerprint and the asset was produced by the running
        generator version; otherwise it raises instead of fabricating a visual.

        ``touch=False`` defers the reuse-count update to the caller so a rejected
        scene write does not increment ``reuse_count``.
        """
        asset_id = asset_row["asset_id"]
        if asset_row["kind"] != "svg":
            raise AssetRecoveryError(asset_id, "only SVG diagrams can be regenerated from instructions")

        metadata = AssetManager.metadata_of(asset_row)
        stored_generator = self._stored_generator(asset_row)
        current_generator = f"{GENERATOR_NAME}@{GENERATOR_VERSION}"
        if stored_generator is not None and stored_generator != current_generator:
            raise AssetRecoveryError(
                asset_id,
                f"it was generated by {stored_generator}, not by the running {current_generator}",
            )

        expected_hash = self._fingerprint_hash(instruction, stored_generator)
        if expected_hash != asset_row["content_hash"]:
            raise AssetIntegrityError(
                asset_id,
                "the stored scene instruction does not match the asset fingerprint",
            )

        document_namespace = self._document_namespace(instruction, expected_hash)
        stored_namespace = metadata.get("namespace")
        generation_namespace = (
            stored_namespace
            if isinstance(stored_namespace, str) and stored_namespace
            else document_namespace
        )
        rendered = self.generator.generate(instruction, namespace=generation_namespace)
        data = rendered.svg.encode("utf-8")

        expected_checksum = metadata.get("file_sha256")
        checksum_verified = bool(expected_checksum and generation_namespace == stored_namespace)
        if checksum_verified and self.assets.checksum(data) != expected_checksum:
            raise AssetIntegrityError(
                asset_id,
                "the regenerated visual does not match the recorded checksum",
            )

        if not self.assets.restore_file(asset_id, data, namespace=generation_namespace):
            raise SceneStorageError(instruction.scene_id)
        if touch:
            self.assets.touch(asset_id)

        verification = "fingerprint and checksum" if checksum_verified else "fingerprint"
        return apply_namespace(rendered.svg, document_namespace), [
            "Stored asset file was missing; the visual was regenerated and the asset restored.",
            f"Asset '{asset_id}' passed {verification} verification after recovery.",
        ]

    def _instruction_for_asset(self, asset_id: str) -> VisualInstruction | None:
        """Find any stored revision that references an asset, for content recovery."""
        with self.db.session() as connection:
            row = connection.execute(
                "SELECT instruction_json FROM scene_revisions WHERE asset_id = ? "
                "ORDER BY created_at, scene_id, revision LIMIT 1",
                (asset_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return VisualInstruction.model_validate_json(row["instruction_json"])
        except ValidationError:
            return None

    def _check_expected_revision(self, scene_id: str, expected_revision: int) -> None:
        """Advisory guard read; the authoritative check runs in ``_persist_scene``."""
        with self.db.session() as connection:
            row = connection.execute(
                "SELECT current_revision FROM scenes WHERE scene_id = ?", (scene_id,)
            ).fetchone()
        actual = int(row["current_revision"]) if row is not None else 0
        if actual != expected_revision:
            raise SceneConflictError(scene_id, expected_revision, actual)

    def _resolve_prepared(
        self,
        connection: sqlite3.Connection,
        prepared: PreparedAsset,
    ) -> str:
        """Insert a staged asset or adopt an identical one on the caller's connection."""
        existing = self.assets.find_exact_row(
            connection, prepared.content_hash, prepared.kind, prepared.provider
        )
        if existing is not None:
            return str(existing["asset_id"])
        try:
            self.assets.insert_prepared(connection, prepared)
        except sqlite3.IntegrityError:
            # Defensive: BEGIN IMMEDIATE should make this unreachable, but adopt
            # rather than fail if the unique index still rejects the row.
            existing = self.assets.find_exact_row(
                connection, prepared.content_hash, prepared.kind, prepared.provider
            )
            if existing is None:
                raise
            return str(existing["asset_id"])
        return prepared.asset_id

    def _persist_scene(
        self,
        instruction: VisualInstruction,
        asset_id: str,
        prepared: PreparedAsset | None = None,
        expected_revision: int | None = None,
    ) -> tuple[int, str]:
        """Allocate the next revision and commit asset changes atomically.

        ``BEGIN IMMEDIATE`` serializes the read-modify-write across concurrent
        requests. The optional ``expected_revision`` guard is checked first in the
        same transaction; revision 0 is create-only, and an omitted guard preserves
        the legacy upsert behavior.

        A newly rendered asset is staged on disk by the caller. Its row is inserted
        here only after the guard passes, using the same connection. If an identical
        asset was committed by another request, it is adopted instead. Files staged
        for a request that loses the race are discarded after the transaction, and
        only files created by that request are ever deleted.
        """
        # SVG is the default mode, so omitting it keeps pre-Phase-4 stored JSON
        # byte-identical and avoids a spurious revision on a no-op update.
        payload = instruction.model_dump_json(
            exclude={"render_mode"} if instruction.render_mode == "svg" else None
        )
        now = utc_now()
        scene_id = instruction.scene_id
        resolved_asset_id = asset_id
        prepared_committed = False
        revision = 0
        try:
            with self.db.session(immediate=True) as connection:
                row = connection.execute(
                    "SELECT current_revision, instruction_json FROM scenes WHERE scene_id = ?",
                    (scene_id,),
                ).fetchone()

                if row is None:
                    # Revision 0 is the create-only guard: an omitted guard keeps
                    # the legacy upsert behavior, while a positive guard means the
                    # caller expected an existing scene.
                    if expected_revision is not None and expected_revision != 0:
                        raise SceneConflictError(scene_id, expected_revision, 0)

                    if prepared is not None:
                        resolved_asset_id = self._resolve_prepared(connection, prepared)
                    else:
                        # A shared asset already existed before this scene.
                        self.assets.touch_in(connection, resolved_asset_id)

                    revision = 1
                    connection.execute(
                        "INSERT INTO scenes (scene_id, title, description, instruction_json, "
                        "current_revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            scene_id,
                            instruction.title,
                            instruction.description,
                            payload,
                            revision,
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO scene_revisions (scene_id, revision, instruction_json, asset_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (scene_id, revision, payload, resolved_asset_id, now),
                    )
                else:
                    current = int(row["current_revision"])
                    if expected_revision is not None and current != expected_revision:
                        raise SceneConflictError(scene_id, expected_revision, current)

                    if prepared is not None:
                        resolved_asset_id = self._resolve_prepared(connection, prepared)
                    else:
                        self.assets.touch_in(connection, resolved_asset_id)

                    current_asset = connection.execute(
                        "SELECT asset_id FROM scene_revisions WHERE scene_id = ? AND revision = ?",
                        (scene_id, current),
                    ).fetchone()
                    unchanged = (
                        row["instruction_json"] == payload
                        and current_asset is not None
                        and current_asset["asset_id"] == resolved_asset_id
                    )
                    if unchanged:
                        revision = current
                    else:
                        revision = current + 1
                        connection.execute(
                            "UPDATE scenes SET title = ?, description = ?, instruction_json = ?, "
                            "current_revision = ?, updated_at = ? "
                            "WHERE scene_id = ? AND current_revision = ?",
                            (
                                instruction.title,
                                instruction.description,
                                payload,
                                revision,
                                now,
                                scene_id,
                                current,
                            ),
                        )
                        connection.execute(
                            "INSERT INTO scene_revisions (scene_id, revision, instruction_json, asset_id, created_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (scene_id, revision, payload, resolved_asset_id, now),
                        )

            if prepared is not None and resolved_asset_id == prepared.asset_id:
                prepared_committed = True
            return revision, resolved_asset_id
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise SceneBusyError(scene_id) from exc
            raise
        finally:
            if prepared is not None and not prepared_committed:
                self.assets.discard_prepared(prepared)

    def _get_scene_row(self, scene_id: str):
        with self.db.session() as connection:
            return connection.execute("SELECT * FROM scenes WHERE scene_id = ?", (scene_id,)).fetchone()

    def _current_asset_id(self, scene_id: str, revision: int) -> str | None:
        with self.db.session() as connection:
            row = connection.execute(
                "SELECT asset_id FROM scene_revisions WHERE scene_id = ? AND revision = ?",
                (scene_id, revision),
            ).fetchone()
        return row["asset_id"] if row else None


def _parse_size(value: str) -> tuple[int, int] | None:
    """Parse a configured ``WIDTHxHEIGHT`` string; ``auto`` is not a size."""
    parts = value.strip().lower().split("x")
    if len(parts) != 2:
        return None
    try:
        width = int(parts[0])
        height = int(parts[1])
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _format_validation_error(exc: ValidationError) -> str:
    messages: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"] if part != "body")
        messages.append(f"{location}: {error['msg']}" if location else str(error["msg"]))
    return "; ".join(messages)
