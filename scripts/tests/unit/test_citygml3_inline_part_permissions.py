import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
EXPORTER_PATH = ROOT / "io" / "writer" / "exporter.py"


def load_allowed_inline_part_types_function():
    source = EXPORTER_PATH.read_text(encoding="utf-8")
    module = ast.parse(source, filename=str(EXPORTER_PATH))

    target = None
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == "_allowed_inline_part_types_for_parent":
            target = node
            break

    if target is None:
        raise AssertionError("_allowed_inline_part_types_for_parent not found in exporter.py")

    extracted_module = ast.Module(body=[target], type_ignores=[])
    ast.fix_missing_locations(extracted_module)

    namespace = {}
    exec(compile(extracted_module, str(EXPORTER_PATH), "exec"), namespace)
    return namespace["_allowed_inline_part_types_for_parent"]


def test_citygml3_nested_building_parts_are_allowed_under_building_parts():
    allowed = load_allowed_inline_part_types_function()

    result = allowed("bldg", "BuildingPart")

    assert "BuildingPart" in result
    assert "BuildingInstallation" in result


def test_citygml3_nested_bridge_parts_are_allowed_under_bridge_parts():
    allowed = load_allowed_inline_part_types_function()

    result = allowed("brid", "BridgePart")

    assert "BridgePart" in result
    assert "BridgeInstallation" in result


def test_citygml3_nested_tunnel_parts_are_allowed_under_tunnel_parts():
    allowed = load_allowed_inline_part_types_function()

    result = allowed("tun", "TunnelPart")

    assert "TunnelPart" in result
    assert "TunnelInstallation" in result