"""Deterministic, provider-neutral prompt construction for AI images.

Engine 2 never supplies a free-form prompt. Every prompt is derived from the
validated structured scene contract, so the same scene always produces the same
prompt and hash. Labels and descriptions are treated as untrusted data: control
characters are removed, whitespace is collapsed, and values are emitted as JSON
strings so they cannot break the prompt structure.

The full prompt is returned only to the provider call site. ``safe_metadata``
contains just the hash, template version and size counters, which is what later
stages may persist in asset metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.schemas.scene import VisualInstruction

DEFAULT_PROMPT_TEMPLATE_VERSION = "1"
DEFAULT_MAX_PROMPT_CHARS = 4_000

_PROMPT_TEMPLATE = """Create a single, clean technical diagram image.

Visual direction: {direction}.
Title: {title}
Description: {description}
Entities:
{entities}
Connections:
{connections}
Style: {style}

Use a plain light background, clear readable labels, consistent iconography,
balanced spacing and high contrast. Do not add unrelated text, watermarks,
signatures or UI chrome."""

_DEFAULT_STYLE = "clean, professional, flat vector-style technical illustration"
_NO_DESCRIPTION = "No additional description was provided."
_NO_CONNECTIONS = "- (no connections)"

_CONTROL_CHARS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f]"
)
_WHITESPACE = re.compile(r"\s+")

_DIRECTION_NAMES = {"LR": "left-to-right", "TB": "top-to-bottom"}


@dataclass(frozen=True)
class PromptBuildResult:
    """Prompt plus the only fields that are safe to persist or log."""

    prompt: str
    prompt_hash: str
    template_version: str
    char_count: int
    truncated: bool

    def safe_metadata(self) -> dict[str, Any]:
        """Return metadata that never contains the full prompt."""
        return {
            "prompt_hash": self.prompt_hash,
            "prompt_template_version": self.template_version,
            "prompt_chars": self.char_count,
            "prompt_truncated": self.truncated,
        }


def build_image_prompt(
    instruction: VisualInstruction,
    *,
    style: str | None = None,
    max_chars: int = DEFAULT_MAX_PROMPT_CHARS,
    template_version: str = DEFAULT_PROMPT_TEMPLATE_VERSION,
) -> PromptBuildResult:
    """Build a deterministic prompt from validated structured scene fields.

    ``style`` is a provider-neutral hint. It is sanitized like every other
    untrusted value. No field from ``NodeSpec.metadata`` is ever included.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    version = (template_version or "").strip()
    if not version:
        raise ValueError("template_version must not be empty")

    direction_code = instruction.options.direction
    direction = _DIRECTION_NAMES.get(direction_code, direction_code)

    nodes = sorted(instruction.nodes, key=lambda node: node.id)
    labels = {
        node.id: _clean_text(node.label, max_length=80) or node.id for node in nodes
    }

    entity_lines = [
        f"- {_quote(labels[node.id])} ({_clean_text(node.kind, max_length=32) or 'generic'})"
        for node in nodes
    ]

    edges = sorted(
        instruction.edges,
        key=lambda edge: (
            edge.source,
            edge.target,
            edge.id or "",
            edge.label or "",
        ),
    )
    connection_lines: list[str] = []
    for edge in edges:
        source = labels.get(edge.source, edge.source)
        target = labels.get(edge.target, edge.target)
        edge_label = _clean_text(edge.label, max_length=48)
        arrow = "arrow" if edge.show_arrow else "no arrow"
        if edge_label:
            connection_lines.append(
                f"- {_quote(source)} -> {_quote(target)} "
                f"({edge.style}, {arrow}, label {_quote(edge_label)})"
            )
        else:
            connection_lines.append(
                f"- {_quote(source)} -> {_quote(target)} ({edge.style}, {arrow})"
            )

    title = _clean_text(instruction.title, max_length=120) or "Untitled diagram"
    description = (
        _clean_text(instruction.description, max_length=400) or _NO_DESCRIPTION
    )
    style_text = _clean_text(style, max_length=120) or _DEFAULT_STYLE

    prompt = _PROMPT_TEMPLATE.format(
        direction=f"{direction} ({direction_code})",
        title=_quote(title),
        description=_quote(description),
        entities="\n".join(entity_lines),
        connections="\n".join(connection_lines) or _NO_CONNECTIONS,
        style=_quote(style_text),
    )

    truncated = len(prompt) > max_chars
    if truncated:
        prompt = _truncate(prompt, max_chars)

    return PromptBuildResult(
        prompt=prompt,
        prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        template_version=version,
        char_count=len(prompt),
        truncated=truncated,
    )


def _clean_text(value: str | None, *, max_length: int) -> str:
    if value is None:
        return ""
    text = _CONTROL_CHARS.sub("", value)
    text = _WHITESPACE.sub(" ", text).strip()
    if len(text) > max_length:
        if max_length <= 1:
            return text[:max_length]
        text = text[: max_length - 1].rstrip() + "…"
    return text


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _truncate(value: str, max_chars: int) -> str:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if len(value) <= max_chars:
        return value
    if max_chars == 1:
        return "…"
    return value[: max_chars - 1].rstrip() + "…"
