import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def load_materials_common():
    bpy_module = sys.modules.setdefault("bpy", types.SimpleNamespace())
    if not hasattr(bpy_module, "types"):
        bpy_module.types = types.SimpleNamespace()
    if not hasattr(bpy_module.types, "Material"):
        bpy_module.types.Material = object
    if not hasattr(bpy_module.types, "Object"):
        bpy_module.types.Object = object

    spec = importlib.util.spec_from_file_location(
        "materials_common_under_test",
        ROOT / "shared/materials_common.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


materials_common = load_materials_common()


def test_normalize_crs_to_epsg_handles_common_citygml_crs_forms():
    cases = [
        ("EPSG:25832", "EPSG:25832"),
        ("epsg:4326", "EPSG:4326"),
        ("urn:ogc:def:crs:EPSG::25832", "EPSG:25832"),
        ("urn:x-ogc:def:crs:EPSG:6.18.3:4326", "EPSG:4326"),
        ("http://www.opengis.net/gml/srs/epsg.xml#3857", "EPSG:3857"),
        ("http://www.opengis.net/def/crs/EPSG/0/4258", "EPSG:4258"),
        ("urn:ogc:def:crs,crs:EPSG::25832,crs:EPSG::5783", "EPSG:25832+5783"),
    ]

    for source, expected in cases:
        assert materials_common.normalize_crs_to_epsg(source) == expected


def test_normalize_crs_to_epsg_handles_adv_urns():
    cases = [
        ("urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH", "EPSG:25832+5783"),
        ("urn:adv:crs:ETRS89_UTM33*DE_DHHN92_NH", "EPSG:25833+5783"),
        ("urn:adv:crs:ETRS89_UTM32*DE_DHHN2016_NH", "EPSG:25832"),
        ("urn:adv:crs:ETRS89_UTM33*DE_DHHN2016_NH", "EPSG:25833"),
        ("urn:adv:crs:ETRS89_UTM32", "EPSG:25832"),
        ("urn:adv:crs:DE_DHDN_3GK3*DE_DHN92_NH", "EPSG:31467+5783"),
    ]

    for source, expected in cases:
        assert materials_common.normalize_crs_to_epsg(source) == expected


def test_extract_vertical_epsg_handles_compound_and_adv_urns():
    cases = [
        ("urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH", "5783"),
        ("urn:adv:crs:ETRS89_UTM32*DE_DHHN2016_NH", "7837"),
        ("urn:ogc:def:crs,crs:EPSG::25832,crs:EPSG::5783", "5783"),
        ("EPSG:4979", ""),
        ("EPSG:25832", ""),
        ("invalid_crs_string", ""),
        ("", ""),
        (None, ""),
    ]

    for source, expected in cases:
        assert materials_common.extract_vertical_epsg(source) == expected


def test_unknown_crs_is_not_treated_as_storable_material_property():
    for source in ("invalid_crs", "", None):
        epsg = materials_common.normalize_crs_to_epsg(source)
        assert epsg == "Unknown CRS"
        assert not (epsg and epsg != "Unknown CRS" and not epsg.startswith("Unknown"))
