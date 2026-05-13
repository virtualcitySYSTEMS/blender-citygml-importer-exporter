import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def load_test_export_module():
    if "bpy" not in sys.modules:
        sys.modules["bpy"] = types.SimpleNamespace()

    spec = importlib.util.spec_from_file_location(
        "test_export_under_test",
        ROOT / "scripts/tests/test_export.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


export_test_module = load_test_export_module()


def write_xml(tmp_path: Path, text: str) -> str:
    path = tmp_path / "export.gml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def citygml3_xml(*, member=True, duplicate_id=False) -> str:
    duplicate = '<core:GenericCityObject gml:id="id-1" />' if duplicate_id else ""
    member_xml = (
        f"<cityObjectMember><core:GenericCityObject gml:id=\"id-1\" />{duplicate}</cityObjectMember>"
        if member
        else ""
    )
    return f"""<?xml version="1.0"?>
<core:CityModel
  xmlns="http://www.opengis.net/citygml/3.0"
  xmlns:core="http://www.opengis.net/citygml/3.0"
  xmlns:gml="http://www.opengis.net/gml/3.2"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.opengis.net/citygml/3.0 core.xsd">
  {member_xml}
</core:CityModel>
"""


def citygml2_xml() -> str:
    return """<?xml version="1.0"?>
<core:CityModel
  xmlns:core="http://www.opengis.net/citygml/2.0"
  xmlns:gml="http://www.opengis.net/gml"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.opengis.net/citygml/2.0 cityGMLBase.xsd">
  <core:cityObjectMember><core:GenericCityObject gml:id="id-1" /></core:cityObjectMember>
</core:CityModel>
"""


def assert_contract_fails(path: str, version: str, expected_message: str):
    ok, message = export_test_module._assert_export_xml_contract(path, version)

    assert not ok
    assert expected_message in message


def test_export_xml_contract_accepts_citygml3(tmp_path):
    path = write_xml(tmp_path, citygml3_xml())

    ok, message = export_test_module._assert_export_xml_contract(path, "3.0")

    assert ok
    assert "cityObjectMember" in message
    assert "gml:id" in message


def test_export_xml_contract_accepts_citygml2(tmp_path):
    path = write_xml(tmp_path, citygml2_xml())

    ok, message = export_test_module._assert_export_xml_contract(path, "2.0")

    assert ok
    assert "cityObjectMember" in message


def test_export_xml_contract_rejects_malformed_xml(tmp_path):
    path = write_xml(tmp_path, "<core:CityModel>")

    assert_contract_fails(path, "3.0", "not well-formed")


def test_export_xml_contract_rejects_wrong_root(tmp_path):
    path = write_xml(
        tmp_path,
        '<root xmlns:gml="http://www.opengis.net/gml/3.2"><cityObjectMember gml:id="id-1" /></root>',
    )

    assert_contract_fails(path, "3.0", "Unexpected root element")


def test_export_xml_contract_rejects_wrong_citygml_version(tmp_path):
    path = write_xml(tmp_path, citygml3_xml())

    assert_contract_fails(path, "2.0", "CityGML version 2.0")


def test_export_xml_contract_rejects_missing_city_object_member(tmp_path):
    path = write_xml(tmp_path, citygml3_xml(member=False))

    assert_contract_fails(path, "3.0", "no cityObjectMember")


def test_export_xml_contract_rejects_duplicate_gml_ids(tmp_path):
    path = write_xml(tmp_path, citygml3_xml(duplicate_id=True))

    assert_contract_fails(path, "3.0", "Duplicate gml:id")
