import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
XSI_SCHEMA_LOCATION = "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation"


def load_module(relative_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


version_detection = load_module("common/version_detection.py", "version_detection_under_test")
validate_auto = load_module("ops/validate_auto.py", "validate_auto_under_test")


class FakeRoot:
    def __init__(self, nsmap, schema_location="", tag="{http://example.test}CityModel"):
        self.nsmap = nsmap
        self.tag = tag
        self._schema_location = schema_location

    def get(self, key, default=None):
        if key == XSI_SCHEMA_LOCATION:
            return self._schema_location
        return default


def test_detect_citygml_version_from_namespace_map():
    assert (
        version_detection.detect_citygml_version(
            FakeRoot({"core": "http://www.opengis.net/citygml/3.0"})
        )
        == "3.0"
    )
    assert (
        version_detection.detect_citygml_version(
            FakeRoot({"core": "http://www.opengis.net/citygml/2.0"})
        )
        == "2.0"
    )


def test_detect_citygml_version_from_schema_location():
    assert (
        version_detection.detect_citygml_version(
            FakeRoot({}, "http://schemas.opengis.net/citygml/3.0/core.xsd")
        )
        == "3.0"
    )
    assert (
        version_detection.detect_citygml_version(
            FakeRoot({}, "http://schemas.opengis.net/citygml/2.0/cityGMLBase.xsd")
        )
        == "2.0"
    )


def test_detect_citygml_version_from_gml_fallback():
    assert (
        version_detection.detect_citygml_version(
            FakeRoot({"gml": "http://www.opengis.net/gml/3.2"}, tag="{x}CityModel")
        )
        == "3.0"
    )
    assert (
        version_detection.detect_citygml_version(
            FakeRoot({"gml": "http://www.opengis.net/gml"}, tag="{x}CityModel")
        )
        == "2.0"
    )


def test_detect_citygml_version_returns_unknown_for_unrelated_xml():
    assert version_detection.detect_citygml_version(FakeRoot({"gml": "http://example.test"})) == "unknown"


def test_detect_version_from_file_reads_root_namespace(tmp_path):
    citygml3 = tmp_path / "citygml3.gml"
    citygml3.write_text(
        '<core:CityModel xmlns:core="http://www.opengis.net/citygml/3.0" />',
        encoding="utf-8",
    )

    citygml2 = tmp_path / "citygml2.gml"
    citygml2.write_text(
        '<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0" />',
        encoding="utf-8",
    )

    assert validate_auto.detect_version_from_file(str(citygml3))[0] == "3.0"
    assert validate_auto.detect_version_from_file(str(citygml2))[0] == "2.0"


def test_detect_version_from_file_corrects_appearance_subfolder_path(tmp_path):
    citygml = tmp_path / "sample.gml"
    citygml.write_text(
        '<core:CityModel xmlns:core="http://www.opengis.net/citygml/3.0" />',
        encoding="utf-8",
    )
    appearance_dir = tmp_path / "appearance"
    appearance_dir.mkdir()

    resolved_path, note = validate_auto._resolve_citygml_file(str(appearance_dir / "sample.gml"))
    version, message = validate_auto.detect_version_from_file(str(appearance_dir / "sample.gml"))

    assert Path(resolved_path) == citygml
    assert note is not None
    assert version == "3.0"
    assert "CityGML 3.0" in message
