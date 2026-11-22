import copy
import re
import xml.etree.ElementTree as ET

from app.generators.diagram_generator import HEADER_HEIGHT, PAGE_PADDING, plan_diagram
from app.generators.svg_generator import SvgGenerator
from app.generators.text_metrics import (
    display_width,
    estimated_px,
    grapheme_clusters,
    sanitize_xml_text,
    truncate_display,
    wrap_display,
)
from app.schemas.scene import VisualInstruction

SVG_NS = "{http://www.w3.org/2000/svg}"


def _generate(sample_instruction: dict):
    instruction = VisualInstruction.model_validate(sample_instruction)
    return SvgGenerator().generate(instruction)


def test_sample_svg_is_well_formed_and_sized(sample_instruction):
    result = _generate(sample_instruction)
    root = ET.fromstring(result.svg)
    assert root.tag == f"{SVG_NS}svg"
    assert root.attrib["viewBox"] == f"0 0 {result.width} {result.height}"
    assert result.width >= 900
    assert result.height >= 400
    assert result.warnings == []


def test_svg_contains_nodes_icons_arrows_and_labels(sample_instruction):
    svg = _generate(sample_instruction).svg
    expected_labels = [
        "Live Bus Tracking System",
        "Bus 101",
        "Bus 102",
        "Bus 103",
        "GPS Server",
        "Passenger App",
        "GPS ping",
        "Live location",
    ]
    for label in expected_labels:
        assert label in svg

    prefix = re.search(r'aria-labelledby="([^"]+)-title"', svg)
    assert prefix is not None
    namespace = prefix.group(1)
    assert svg.count('class="mb-node"') == 5
    assert svg.count(f'marker-end="url(#{namespace}-arrow)"') == 4
    for kind in ("bus", "server", "passenger"):
        assert f'id="{namespace}-icon-{kind}"' in svg
        assert f'href="#{namespace}-icon-{kind}"' in svg
    assert f'filter="url(#{namespace}-card-shadow)"' in svg
    assert f'fill="url(#{namespace}-header-wash)"' in svg
    assert f'<title id="{namespace}-title">' in svg
    assert 'data-node-id="gps-server"' in svg


def test_layered_layout_left_to_right_is_readable(sample_instruction):
    instruction = VisualInstruction.model_validate(sample_instruction)
    layout = plan_diagram(instruction)

    assert layout.nodes["bus-1"].x < layout.nodes["gps-server"].x < layout.nodes["passenger"].x
    assert layout.nodes["bus-1"].x == layout.nodes["bus-2"].x == layout.nodes["bus-3"].x
    assert layout.nodes["bus-1"].y < layout.nodes["bus-2"].y < layout.nodes["bus-3"].y

    centroid = sum(layout.nodes[f"bus-{i}"].center_y for i in (1, 2, 3)) / 3
    assert abs(centroid - layout.nodes["gps-server"].center_y) < 0.01
    assert layout.nodes["bus-1"].x == PAGE_PADDING
    assert layout.nodes["bus-1"].y >= HEADER_HEIGHT


def test_top_to_bottom_direction(sample_instruction):
    data = copy.deepcopy(sample_instruction)
    data["options"]["direction"] = "TB"
    instruction = VisualInstruction.model_validate(data)
    layout = plan_diagram(instruction)

    assert layout.nodes["bus-1"].y < layout.nodes["gps-server"].y < layout.nodes["passenger"].y
    assert layout.nodes["bus-1"].x < layout.nodes["bus-2"].x < layout.nodes["bus-3"].x
    svg = SvgGenerator().generate(instruction).svg
    ET.fromstring(svg)


def test_manual_positions_are_honoured(sample_instruction):
    data = copy.deepcopy(sample_instruction)
    positions = {
        "bus-1": {"x": 0, "y": 0},
        "bus-2": {"x": 0, "y": 140},
        "bus-3": {"x": 0, "y": 280},
        "gps-server": {"x": 320, "y": 140},
        "passenger": {"x": 640, "y": 140},
    }
    for node in data["nodes"]:
        node["position"] = positions[node["id"]]

    instruction = VisualInstruction.model_validate(data)
    layout = plan_diagram(instruction)
    assert layout.nodes["gps-server"].x == PAGE_PADDING + 320
    assert layout.nodes["gps-server"].y == HEADER_HEIGHT + 24 + 140
    assert layout.warnings == []
    ET.fromstring(SvgGenerator().generate(instruction).svg)


def test_partial_manual_positions_fall_back_to_auto_layout(sample_instruction):
    data = copy.deepcopy(sample_instruction)
    data["nodes"][0]["position"] = {"x": 10, "y": 10}
    instruction = VisualInstruction.model_validate(data)
    layout = plan_diagram(instruction)
    assert layout.nodes["bus-1"].x == PAGE_PADDING
    assert any("falling back" in warning for warning in layout.warnings)


def test_user_text_is_escaped(sample_instruction):
    data = copy.deepcopy(sample_instruction)
    data["title"] = 'Metrics & "KPIs"'
    data["nodes"][0]["label"] = "Bus <1> & Co"
    instruction = VisualInstruction.model_validate(data)
    svg = SvgGenerator().generate(instruction).svg
    ET.fromstring(svg)  # would raise if ampersands were not escaped
    assert "Bus &lt;1&gt; &amp; Co" in svg
    assert "Metrics &amp;" in svg


def test_rendering_is_deterministic(sample_instruction):
    first = _generate(sample_instruction)
    second = _generate(sample_instruction)
    assert first.svg == second.svg
    assert (first.width, first.height) == (second.width, second.height)


def test_cycles_render_with_warning(sample_instruction):
    data = copy.deepcopy(sample_instruction)
    data["nodes"] = [
        {"id": "a", "label": "A", "kind": "generic"},
        {"id": "b", "label": "B", "kind": "generic"},
        {"id": "c", "label": "C", "kind": "generic"},
    ]
    data["edges"] = [
        {"id": "ab", "source": "a", "target": "b"},
        {"id": "bc", "source": "b", "target": "c"},
        {"id": "ca", "source": "c", "target": "a"},
    ]
    instruction = VisualInstruction.model_validate(data)
    result = SvgGenerator().generate(instruction)
    ET.fromstring(result.svg)
    assert any("cycle" in warning.lower() for warning in result.warnings)


def test_long_labels_wrap_without_breaking_svg(sample_instruction):
    data = copy.deepcopy(sample_instruction)
    data["nodes"][0]["label"] = "Superlong Passenger Information Display"
    instruction = VisualInstruction.model_validate(data)
    svg = SvgGenerator().generate(instruction).svg
    ET.fromstring(svg)
    assert "…" in svg


def test_edge_styles_and_label_toggle(sample_instruction):
    data = copy.deepcopy(sample_instruction)
    data["edges"][0]["style"] = "dashed"
    data["options"]["show_edge_labels"] = False
    instruction = VisualInstruction.model_validate(data)
    svg = SvgGenerator().generate(instruction).svg
    assert 'stroke-dasharray="5 4"' in svg
    assert "GPS ping" not in svg


def test_every_kind_has_an_icon():
    from app.generators.svg_generator import ICONS, KIND_COLORS
    from app.schemas.scene import NodeKind

    kinds = set(NodeKind.__args__)
    assert kinds == set(ICONS)
    assert kinds == set(KIND_COLORS)


def test_multiple_svgs_on_one_page_have_disjoint_ids(sample_instruction):
    first = _generate(sample_instruction).svg
    data = copy.deepcopy(sample_instruction)
    data["nodes"][0]["label"] = "Bus 999"
    second = SvgGenerator().generate(VisualInstruction.model_validate(data)).svg

    first_ids = set(re.findall(r'(?<![-\w])id="([^"]+)"', first))
    second_ids = set(re.findall(r'(?<![-\w])id="([^"]+)"', second))
    assert first_ids
    assert first_ids.isdisjoint(second_ids)

    # Every reference stays inside its own document.
    for svg, ids in ((first, first_ids), (second, second_ids)):
        references = set(re.findall(r"url\(#([^)]+)\)", svg)) | set(
            re.findall(r'href="#([^"]+)"', svg)
        )
        assert references <= ids


def test_overlong_title_is_truncated_with_a_warning(sample_instruction):
    data = copy.deepcopy(sample_instruction)
    data["title"] = ("Long descriptive title " * 8).strip()[:120]
    result = SvgGenerator().generate(VisualInstruction.model_validate(data))
    ET.fromstring(result.svg)
    assert "…" in result.svg
    assert any("title" in warning and "ellipsis" in warning for warning in result.warnings)


def test_overlong_description_is_truncated_with_a_warning(sample_instruction):
    data = copy.deepcopy(sample_instruction)
    data["description"] = ("A very long description " * 30).strip()[:400]
    result = SvgGenerator().generate(VisualInstruction.model_validate(data))
    ET.fromstring(result.svg)
    assert "…" in result.svg
    assert any("description" in warning and "ellipsis" in warning for warning in result.warnings)


def test_short_title_and_description_do_not_warn(sample_instruction):
    result = _generate(sample_instruction)
    title_warnings = [w for w in result.warnings if "title" in w]
    description_warnings = [w for w in result.warnings if "description" in w]
    assert title_warnings == []
    assert description_warnings == []


def test_long_node_label_is_shortened_with_an_explicit_warning(sample_instruction):
    data = copy.deepcopy(sample_instruction)
    data["nodes"][0]["label"] = "Superlong Passenger Information Display Panel"
    result = SvgGenerator().generate(VisualInstruction.model_validate(data))
    ET.fromstring(result.svg)
    assert "…" in result.svg
    assert any(
        "Node labels shortened" in warning and "bus-1" in warning
        for warning in result.warnings
    )


def test_cjk_and_emoji_labels_stay_well_formed(sample_instruction):
    data = copy.deepcopy(sample_instruction)
    data["nodes"][0]["label"] = "公交实时追踪与乘客信息显示终端"
    data["nodes"][1]["label"] = "🚌 快速公交"
    data["nodes"][2]["label"] = "👨‍👩‍👧‍👦 家庭群组"
    svg = SvgGenerator().generate(VisualInstruction.model_validate(data)).svg
    ET.fromstring(svg)
    assert "公交实时追踪与乘客信息显示终端" in svg or "…" in svg
    assert "🚌" in svg
    assert "👨‍👩‍👧‍👦" in svg
    assert "\u200d </text>" not in svg


def test_parallel_edge_labels_do_not_overlap(sample_instruction):
    data = copy.deepcopy(sample_instruction)
    data["nodes"] = data["nodes"][:2]
    data["edges"] = [
        {"id": f"parallel-{index}", "source": "bus-1", "target": "bus-2", "label": f"link {index}"}
        for index in range(6)
    ]
    result = SvgGenerator().generate(VisualInstruction.model_validate(data))
    ET.fromstring(result.svg)
    assert not any("edge labels overlap" in warning for warning in result.warnings)


def test_grapheme_clusters_keep_emoji_sequences_whole():
    family = "👨‍👩‍👧‍👦"
    assert grapheme_clusters(family) == [family]
    assert grapheme_clusters("🇮🇳") == ["🇮🇳"]
    assert grapheme_clusters("e\u0301") == ["e\u0301"]
    assert display_width(family) == 2
    assert display_width("🇮🇳") == 2
    assert display_width("東京") == 4


def test_truncate_and_wrap_respect_display_units():
    text, truncated = truncate_display("東京タワー", 5)
    assert truncated is True
    assert display_width(text) == 5
    assert text.endswith("…")

    lines, truncated = wrap_display("東京タワーの夜景", 4, max_lines=2)
    assert truncated is True
    assert all(display_width(line) <= 4 for line in lines)
    assert lines[-1].endswith("…")


def test_xml_invalid_control_characters_are_sanitized(sample_instruction):
    assert sanitize_xml_text("a\x0bb") == "a\ufffdb"
    assert sanitize_xml_text("a\tb\nc\rd") == "a\tb\nc\rd"

    data = copy.deepcopy(sample_instruction)
    data["title"] = "Head \x0b office"
    data["description"] = "Coffee \x07 break"
    data["nodes"][0]["label"] = "Node \x01 one"
    data["edges"][0]["label"] = "Edge \x1f one"
    result = SvgGenerator().generate(VisualInstruction.model_validate(data))

    ET.fromstring(result.svg)  # strict XML parse; raises if a control character survived
    assert "\ufffd" in result.svg
    for invalid in ("\x0b", "\x07", "\x01", "\x1f"):
        assert invalid not in result.svg
    assert any(
        "Invalid XML characters" in warning and "U+FFFD" in warning
        for warning in result.warnings
    )


def test_wide_latin_glyphs_are_measured_conservatively():
    assert display_width("W") > display_width("i")
    assert display_width("W" * 14) > 14
    assert estimated_px("W" * 14, 13) > 96  # wider than the node text area


def test_long_wide_node_label_reports_measured_overflow(sample_instruction):
    data = copy.deepcopy(sample_instruction)
    data["nodes"][0]["label"] = "W" * 40
    result = SvgGenerator().generate(VisualInstruction.model_validate(data))

    ET.fromstring(result.svg)
    assert "…" in result.svg
    assert any(
        "Node labels shortened" in warning
        and "bus-1" in warning
        and "estimated" in warning
        and "of 96 px available" in warning
        for warning in result.warnings
    )


def test_hindi_and_wide_unicode_labels_stay_inside_cards(sample_instruction):
    short = "नमस्ते दुनिया"
    assert estimated_px(short, 13) >= 70

    data = copy.deepcopy(sample_instruction)
    data["nodes"][0]["label"] = short
    result = SvgGenerator().generate(VisualInstruction.model_validate(data))
    ET.fromstring(result.svg)
    assert not any("bus-1" in warning and "shortened" in warning for warning in result.warnings)

    data["nodes"][0]["label"] = "क्षेत्रीय परिवहन विभाग की वास्तविक समय स्थिति"
    result = SvgGenerator().generate(VisualInstruction.model_validate(data))
    ET.fromstring(result.svg)
    assert "…" in result.svg
    assert any(
        "Node labels shortened" in warning
        and "bus-1" in warning
        and "estimated" in warning
        for warning in result.warnings
    )


def test_long_edge_label_is_clamped_with_a_warning(sample_instruction):
    data = copy.deepcopy(sample_instruction)
    data["nodes"] = data["nodes"][:2]
    data["edges"] = [
        {"id": "long-edge", "source": "bus-1", "target": "bus-2", "label": "W" * 48}
    ]
    result = SvgGenerator().generate(VisualInstruction.model_validate(data))

    root = ET.fromstring(result.svg)
    assert any(
        "Edge labels shortened" in warning
        and "long-edge" in warning
        and "estimated" in warning
        for warning in result.warnings
    )

    groups = [group for group in root.iter() if group.attrib.get("class") == "mb-edge-label"]
    assert len(groups) == 1
    rect = groups[0].find(f"{SVG_NS}rect")
    text = groups[0].find(f"{SVG_NS}text")
    assert rect is not None and text is not None
    assert float(rect.attrib["width"]) <= result.width - PAGE_PADDING * 2
    assert text.text is not None and text.text.endswith("…")


def test_wide_and_sanitized_text_is_deterministic(sample_instruction):
    data = copy.deepcopy(sample_instruction)
    data["title"] = "नमस्ते \x01 दुनिया"
    data["nodes"][0]["label"] = "क्षेत्रीय परिवहन विभाग"
    data["edges"][0]["label"] = "W" * 48
    instruction = VisualInstruction.model_validate(data)

    first = SvgGenerator().generate(instruction)
    second = SvgGenerator().generate(instruction)
    assert first.svg == second.svg
    assert first.warnings == second.warnings
    assert (first.width, first.height) == (second.width, second.height)
