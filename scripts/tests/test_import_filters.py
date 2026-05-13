"""
Filter contract tests for direct CityGML imports.

Usage:
  blender --background --addons citygml-importer-exporter --python scripts/tests/test_import_filters.py -- --dir test_files
"""
import argparse
import os
import sys
import xml.etree.ElementTree as ET

import bpy

ADDON_MODULE = "citygml-importer-exporter"
BUILDING_FIXTURE = "Building/Railway_Scene_LoD3_CityGML3_Building_only.gml"
GML_NAMESPACES = (
    "http://www.opengis.net/gml/3.2",
    "http://www.opengis.net/gml",
)


def _extract_blender_args() -> list[str]:
    if "--" not in sys.argv:
        return []
    return sys.argv[sys.argv.index("--") + 1:]


def _reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _ensure_addon_enabled() -> tuple[bool, str]:
    if ADDON_MODULE in bpy.context.preferences.addons:
        return True, "enabled"
    try:
        bpy.ops.preferences.addon_enable(module=ADDON_MODULE)
    except Exception as e:
        return False, f"addon_enable failed: {e}"
    return True, "enabled"


def _gml_id(elem) -> str:
    for namespace in GML_NAMESPACES:
        value = elem.get(f"{{{namespace}}}id")
        if value:
            return value
    return ""


def _first_feature_id(filepath: str, feature_name: str) -> str:
    root = ET.parse(filepath).getroot()
    for elem in root.iter():
        if str(elem.tag).split("}", 1)[-1] == feature_name:
            return _gml_id(elem)
    raise RuntimeError(f"No {feature_name} element found in {filepath}")


def _object_prop_values(key: str) -> set[str]:
    values = set()
    for obj in bpy.data.objects:
        try:
            value = str(obj.get(key) or "").strip()
        except Exception:
            value = ""
        if value:
            values.add(value)
    return values


def _all_import_filter_flags() -> tuple[str, ...]:
    return (
        "import_buildings",
        "import_bridges",
        "import_tunnels",
        "import_vegetation",
        "import_water",
        "import_transportation",
        "import_cityfurniture",
        "import_landuse",
        "import_relief",
        "import_generics",
    )


def _disable_all_import_feature_flags(scene_props):
    for attr_name in _all_import_filter_flags():
        setattr(scene_props, attr_name, False)


def _reset_filters(scene_props):
    scene_props.use_gmlid_filter = False
    scene_props.gmlid_filter = ""
    scene_props.use_feature_type_filter = False
    for attr_name in _all_import_filter_flags():
        setattr(scene_props, attr_name, True)
    scene_props.use_bbox_filter = False
    scene_props.bbox_coords_string = ""
    scene_props.bbox_min_x = 0.0
    scene_props.bbox_min_y = 0.0
    scene_props.bbox_max_x = 0.0
    scene_props.bbox_max_y = 0.0
    scene_props.use_lod_filter = False


def _import_fixture(filepath: str) -> tuple[bool, str]:
    result = bpy.ops.cgml3.import_gml_file(
        "EXEC_DEFAULT",
        filepath=filepath,
        import_appearance=False,
    )
    if "FINISHED" not in result:
        return False, f"import did not finish: {result}"
    return True, "imported"


def _run_gml_id_filter_test(filepath: str) -> tuple[bool, str]:
    _reset_scene()
    ok, msg = _ensure_addon_enabled()
    if not ok:
        return False, msg

    scene_props = bpy.context.scene.cgml3
    _reset_filters(scene_props)
    scene_props.xsd_validate = False
    target_id = _first_feature_id(filepath, "Building")
    scene_props.use_gmlid_filter = True
    scene_props.gmlid_filter = target_id

    ok, msg = _import_fixture(filepath)
    if not ok:
        return False, msg

    gml_ids = _object_prop_values("gml_id")
    if target_id not in gml_ids:
        return False, f"target gml_id {target_id} not imported; got {sorted(gml_ids)[:8]}"
    if not bpy.data.objects:
        return False, "no objects imported with GML-ID filter"

    return True, f"GML-ID filter imported target {target_id} with {len(bpy.data.objects)} objects"


def _run_feature_type_positive_filter_test(filepath: str) -> tuple[bool, str]:
    _reset_scene()
    ok, msg = _ensure_addon_enabled()
    if not ok:
        return False, msg

    scene_props = bpy.context.scene.cgml3
    _reset_filters(scene_props)
    scene_props.xsd_validate = False
    scene_props.use_feature_type_filter = True
    _disable_all_import_feature_flags(scene_props)
    scene_props.import_buildings = True

    ok, msg = _import_fixture(filepath)
    if not ok:
        return False, msg
    if not bpy.data.objects:
        return False, "building fixture imported no objects with building filter"

    features = _object_prop_values("cgml3_feature")
    if not any(feature == "Building" or feature.startswith("Building_") for feature in features):
        return False, f"Building feature missing after positive filter: {sorted(features)[:8]}"

    return True, f"Feature-type positive filter kept Building ({len(bpy.data.objects)} objects)"


def _run_feature_type_negative_filter_test(filepath: str) -> tuple[bool, str]:
    _reset_scene()
    ok, msg = _ensure_addon_enabled()
    if not ok:
        return False, msg

    scene_props = bpy.context.scene.cgml3
    _reset_filters(scene_props)
    scene_props.xsd_validate = False
    scene_props.use_feature_type_filter = True
    _disable_all_import_feature_flags(scene_props)
    scene_props.import_bridges = True

    ok, msg = _import_fixture(filepath)
    if not ok:
        return False, msg
    if bpy.data.objects:
        features = sorted(_object_prop_values("cgml3_feature"))[:8]
        return False, f"bridge-only filter imported objects from building fixture: {features}"

    return True, "Feature-type negative filter excluded Building fixture"


def main():
    parser = argparse.ArgumentParser(description="CityGML import filter contract tests")
    parser.add_argument("--dir", required=True, help="Base directory containing test fixtures")
    args = parser.parse_args(_extract_blender_args())

    filepath = os.path.join(os.path.abspath(args.dir), *BUILDING_FIXTURE.split("/"))
    if not os.path.isfile(filepath):
        print(f"[TEST] ERROR: fixture not found: {filepath}")
        sys.exit(1)

    tests = (
        ("GML-ID filter", _run_gml_id_filter_test),
        ("Feature-type positive filter", _run_feature_type_positive_filter_test),
        ("Feature-type negative filter", _run_feature_type_negative_filter_test),
    )

    failures = []
    print(f"[TEST] Import filter tests: {len(tests)}")
    print("=" * 60)
    for name, test_func in tests:
        ok, msg = test_func(filepath)
        if ok:
            print(f"  PASS  {name} ({msg})")
        else:
            print(f"  FAIL  {name} ({msg})")
            failures.append((name, msg))

    print("=" * 60)
    if failures:
        print("[TEST] Failed import filter tests:")
        for name, msg in failures:
            print(f"  - {name}: {msg}")
        sys.exit(1)

    print("[TEST] OK - Import filters passed")
    sys.exit(0)


if __name__ == "__main__":
    main()
