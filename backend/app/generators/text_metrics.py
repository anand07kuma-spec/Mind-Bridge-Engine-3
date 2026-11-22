"""Display-width helpers for SVG text.

SVG text has no layout engine we can query before rendering, so the generator
estimates each grapheme cluster in *display units*: roughly half an em. The
ASCII table is calibrated against the primary UI fonts (Inter/Segoe UI) and
rounds wide glyphs upward, so text no longer silently spills out of a card.
East Asian and emoji clusters are 2 units, combining marks are zero-width and
other scripts are measured conservatively. Truncation and wrapping are
grapheme-aware so emoji ZWJ sequences, flags, skin-tone modifiers and Indic
combining marks are never split across lines.
"""

from __future__ import annotations

import unicodedata

ELLIPSIS = "…"


def sanitize_xml_text(text: str) -> str:
    """Replace code points XML 1.0 forbids with U+FFFD.

    XML 1.0 only permits tab, LF, CR, ``#x20-#xD7FF``, ``#xE000-#xFFFD`` and
    ``#x10000-#x10FFFF``. Other C0 controls, lone surrogates and ``#xFFFE`` /
    ``#xFFFF`` make the whole document unparseable, so they cannot simply be
    escaped. Replacing them keeps the document valid and visible.
    """
    cleaned: list[str] = []
    changed = False
    for char in text:
        code = ord(char)
        if (
            char in "\t\n\r"
            or 0x20 <= code <= 0xD7FF
            or 0xE000 <= code <= 0xFFFD
            or 0x10000 <= code <= 0x10FFFF
        ):
            cleaned.append(char)
        else:
            cleaned.append("\uFFFD")
            changed = True
    return "".join(cleaned) if changed else text


_ZWJ = "\u200d"
_VS15 = "\ufe0e"
_VS16 = "\ufe0f"
_KEYCAP = "\u20e3"

_ATTACHED = {_VS15, _VS16, _KEYCAP}


def _is_regional_indicator(char: str) -> bool:
    return 0x1F1E6 <= ord(char) <= 0x1F1FF


def _is_emoji_modifier(char: str) -> bool:
    return 0x1F3FB <= ord(char) <= 0x1F3FF


def _is_emoji_base(char: str) -> bool:
    code = ord(char)
    return 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF


def grapheme_clusters(text: str) -> list[str]:
    """Split text into user-visible clusters (approximation of UAX #29)."""
    clusters: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        index += 1
        cluster = char
        if _is_regional_indicator(char) and index < length and _is_regional_indicator(text[index]):
            cluster += text[index]
            index += 1
        while index < length:
            char = text[index]
            if char == _ZWJ and index + 1 < length:
                cluster += char + text[index + 1]
                index += 2
                continue
            if char in _ATTACHED or _is_emoji_modifier(char) or unicodedata.category(char).startswith("M"):
                cluster += char
                index += 1
                continue
            break
        clusters.append(cluster)
    return clusters


# Advance widths in display units (1 unit = half an em), measured on Segoe UI
# at 13 px and rounded up where hinting makes small text wider. Values are
# intentionally conservative: an over-estimate shortens a label, an
# under-estimate lets it escape its container.
_ASCII_UNITS: dict[str, float] = {
    "A": 1.75, "B": 1.6, "C": 1.7, "D": 1.9, "E": 1.4, "F": 1.3, "G": 1.85,
    "H": 1.9, "I": 0.8, "J": 1.0, "K": 1.55, "L": 1.3, "M": 2.5, "N": 2.0,
    "O": 2.0, "P": 1.55, "Q": 2.0, "R": 1.6, "S": 1.45, "T": 1.4, "U": 1.85,
    "V": 1.7, "W": 2.5, "X": 1.6, "Y": 1.45, "Z": 1.55,
    "a": 1.4, "b": 1.6, "c": 1.25, "d": 1.6, "e": 1.4, "f": 0.85, "g": 1.6,
    "h": 1.55, "i": 0.65, "j": 0.65, "k": 1.3, "l": 0.65, "m": 2.35, "n": 1.55,
    "o": 1.6, "p": 1.6, "q": 1.6, "r": 0.95, "s": 1.15, "t": 0.95, "u": 1.55,
    "v": 1.3, "w": 1.9, "x": 1.25, "y": 1.3, "z": 1.25,
    "0": 1.45, "1": 1.45, "2": 1.45, "3": 1.45, "4": 1.45, "5": 1.45,
    "6": 1.45, "7": 1.45, "8": 1.45, "9": 1.45,
    " ": 0.75, ".": 0.65, ",": 0.65, ";": 0.65, ":": 0.65, "'": 0.65,
    '"': 1.1, "!": 0.8, "|": 0.65, "(": 0.85, ")": 0.85, "[": 0.85,
    "]": 0.85, "{": 0.85, "}": 0.85, "#": 1.6, "*": 1.15, "-": 1.1,
    "_": 1.15, "/": 1.1, "+": 1.9, "=": 1.9, "<": 1.9, ">": 1.9,
    "?": 1.25, "\\": 1.0, ELLIPSIS: 1.0,
}


def _is_mark(char: str) -> bool:
    return unicodedata.category(char).startswith("M")


def cluster_width(cluster: str) -> float:
    if not cluster:
        return 0.0
    base = cluster[0]
    if _is_mark(base):
        return 0.0
    if (
        _KEYCAP in cluster
        or _VS16 in cluster
        or _is_regional_indicator(base)
        or _is_emoji_base(base)
    ):
        return 2.0
    code = ord(base)
    marks = sum(1 for char in cluster[1:] if _is_mark(char))
    if base == ELLIPSIS:
        return 1.0
    if code < 0x80:
        return _ASCII_UNITS.get(base, 1.2)
    if code < 0x1100:
        # Latin-1, Greek, Cyrillic, Arabic, Hebrew: narrow-to-average advances.
        return 1.3 + min(0.3 * marks, 0.6)
    if unicodedata.east_asian_width(base) in ("W", "F"):
        return 2.0
    # Indic and other complex scripts: wide bases with spacing/combining marks.
    return 1.5 + min(0.5 * marks, 1.0)


def display_width(text: str) -> float:
    return sum(cluster_width(cluster) for cluster in grapheme_clusters(text))


def estimated_px(text: str, font_size: float) -> float:
    """Estimated rendered width in pixels for a font of ``font_size`` px."""
    return display_width(text) * font_size / 2.0


def _trim_to_units(text: str, max_units: float) -> str:
    if max_units <= 0:
        return ""
    used = 0.0
    kept: list[str] = []
    for cluster in grapheme_clusters(text):
        width = cluster_width(cluster)
        if used + width > max_units:
            break
        kept.append(cluster)
        used += width
    return "".join(kept)


def truncate_display(text: str, max_units: float) -> tuple[str, bool]:
    """Single-line truncation to ``max_units`` including the ellipsis."""
    if max_units <= 0:
        return ("", True) if text else ("", False)
    if display_width(text) <= max_units:
        return text, False
    if max_units == 1:
        return ELLIPSIS, True
    kept = _trim_to_units(text, max_units - 1).rstrip()
    return (f"{kept}{ELLIPSIS}" if kept else ELLIPSIS), True


def wrap_display(text: str, max_units: float, max_lines: int = 2) -> tuple[list[str], bool]:
    """Wrap by display units, preferring spaces, then cluster boundaries."""
    if max_units <= 0 or max_lines <= 0:
        return [], bool(text)
    clusters = grapheme_clusters(text)
    lines: list[str] = []
    start = 0
    total = len(clusters)

    while start < total and len(lines) < max_lines:
        while start < total and clusters[start].isspace():
            start += 1
        if start >= total:
            break
        used = 0.0
        end = start
        last_space = -1
        while end < total:
            cluster = clusters[end]
            width = cluster_width(cluster)
            if cluster.isspace():
                last_space = end
            if used + width > max_units and end > start:
                break
            used += width
            end += 1
        if end >= total:
            lines.append("".join(clusters[start:end]).rstrip())
            start = total
            break
        if last_space > start:
            lines.append("".join(clusters[start:last_space]).rstrip())
            start = last_space + 1
        else:
            lines.append("".join(clusters[start:end]).rstrip())
            start = end

    truncated = start < total
    if not lines:
        lines = [""]
    if truncated:
        kept = _trim_to_units(lines[-1], max_units - 1).rstrip()
        lines[-1] = f"{kept}{ELLIPSIS}" if kept else ELLIPSIS
    return lines, truncated
