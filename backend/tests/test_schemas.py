import pytest
from app import limits
from app.schemas.scene import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    EdgeSpec,
    NodeSpec,
    SceneUpdate,
    VisualInstruction,
)
from pydantic import ValidationError


def _base(sample_instruction: dict) -> dict:
    return sample_instruction


def test_valid_instruction_accepts_sample(sample_instruction):
    instruction = VisualInstruction.model_validate(sample_instruction)
    assert len(instruction.nodes) == 5
    assert all(edge.id for edge in instruction.edges)


def test_edge_id_is_derived_when_omitted(sample_instruction):
    data = _base(sample_instruction)
    data["edges"] = [{"source": "bus-1", "target": "gps-server"}]
    instruction = VisualInstruction.model_validate(data)
    assert instruction.edges[0].id == "bus-1->gps-server"


def test_duplicate_node_ids_rejected(sample_instruction):
    data = _base(sample_instruction)
    data["nodes"].append({"id": "bus-1", "label": "Duplicate", "kind": "bus"})
    with pytest.raises(ValidationError, match="Duplicate node ids"):
        VisualInstruction.model_validate(data)


def test_unknown_edge_endpoint_rejected(sample_instruction):
    data = _base(sample_instruction)
    data["edges"].append({"id": "ghost", "source": "bus-1", "target": "does-not-exist"})
    with pytest.raises(ValidationError, match="unknown target node"):
        VisualInstruction.model_validate(data)


def test_self_loop_rejected(sample_instruction):
    data = _base(sample_instruction)
    data["edges"] = [{"source": "bus-1", "target": "bus-1"}]
    with pytest.raises(ValidationError, match="Self-loop"):
        VisualInstruction.model_validate(data)


def test_duplicate_edge_ids_rejected(sample_instruction):
    data = _base(sample_instruction)
    data["edges"] = [
        {"id": "same", "source": "bus-1", "target": "gps-server"},
        {"id": "same", "source": "bus-2", "target": "gps-server"},
    ]
    with pytest.raises(ValidationError, match="Duplicate edge id"):
        VisualInstruction.model_validate(data)


def test_unknown_fields_rejected(sample_instruction):
    data = _base(sample_instruction)
    data["surprise"] = True
    with pytest.raises(ValidationError):
        VisualInstruction.model_validate(data)


def test_scene_update_fills_edge_ids():
    patch = SceneUpdate.model_validate(
        {"edges_to_add": [{"source": "bus-1", "target": "gps-server"}]}
    )
    edge = patch.edges_to_add[0]
    assert isinstance(edge, EdgeSpec)
    assert edge.id == "bus-1->gps-server"


def test_node_kind_must_be_supported(sample_instruction):
    data = _base(sample_instruction)
    data["nodes"][0]["kind"] = "spaceship"
    with pytest.raises(ValidationError):
        VisualInstruction.model_validate(data)


def test_scene_update_accepts_an_expected_revision():
    patch = SceneUpdate.model_validate({"expected_revision": 3, "title": "Version three"})
    assert patch.expected_revision == 3
    assert patch.title == "Version three"


def test_scene_update_rejects_a_negative_expected_revision():
    with pytest.raises(ValidationError):
        SceneUpdate.model_validate({"expected_revision": -1})


def test_schema_version_is_optional_and_defaults_to_supported_version(sample_instruction):
    data = _base(sample_instruction)
    data.pop("schema_version", None)
    instruction = VisualInstruction.model_validate(data)
    assert instruction.schema_version == SCHEMA_VERSION
    assert SCHEMA_VERSION in SUPPORTED_SCHEMA_VERSIONS


def test_supported_schema_version_is_accepted(sample_instruction):
    data = _base(sample_instruction)
    data["schema_version"] = "1.0"
    assert VisualInstruction.model_validate(data).schema_version == "1.0"


@pytest.mark.parametrize("version", ["2.0", "1.1", "", "latest"])
def test_unsupported_schema_version_is_rejected(sample_instruction, version):
    data = _base(sample_instruction)
    data["schema_version"] = version
    with pytest.raises(ValidationError, match="Unsupported schema_version"):
        VisualInstruction.model_validate(data)


def _nested_metadata(depth: int) -> dict:
    root: dict = {}
    cursor = root
    for index in range(depth - 1):
        child: dict = {}
        cursor[f"level-{index}"] = child
        cursor = child
    return root


def test_metadata_depth_limit_is_enforced():
    allowed = _nested_metadata(limits.METADATA_MAX_DEPTH)
    NodeSpec.model_validate({"id": "n1", "label": "N1", "metadata": allowed})

    too_deep = _nested_metadata(limits.METADATA_MAX_DEPTH + 1)
    with pytest.raises(ValidationError, match="levels deep"):
        NodeSpec.model_validate({"id": "n1", "label": "N1", "metadata": too_deep})


def test_metadata_size_limit_is_enforced():
    allowed = {"blob": "x" * (limits.METADATA_MAX_BYTES - 20)}
    NodeSpec.model_validate({"id": "n1", "label": "N1", "metadata": allowed})

    oversized = {"blob": "x" * (limits.METADATA_MAX_BYTES + 1)}
    with pytest.raises(ValidationError, match="serialize to at most"):
        NodeSpec.model_validate({"id": "n1", "label": "N1", "metadata": oversized})


def test_update_node_metadata_limits_are_enforced():
    too_deep = _nested_metadata(limits.METADATA_MAX_DEPTH + 1)
    with pytest.raises(ValidationError, match="levels deep"):
        SceneUpdate.model_validate(
            {"nodes_to_update": [{"id": "n1", "metadata": too_deep}]}
        )

    with pytest.raises(ValidationError, match="serialize to at most"):
        SceneUpdate.model_validate(
            {"nodes_to_add": [
                {
                    "id": "n2",
                    "label": "N2",
                    "metadata": {"blob": "x" * (limits.METADATA_MAX_BYTES + 1)},
                }
            ]}
        )
