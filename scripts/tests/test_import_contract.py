"""
Contract tests for representative CityGML imports.

Usage:
  blender --background --addons citygml_importer_exporter --python scripts/tests/test_import_contract.py -- --dir test_files
"""
import argparse
import os
import sys
from dataclasses import dataclass

import bpy

ADDON_MODULE = "citygml_importer_exporter"


@dataclass(frozen=True)
class ImportContract:
    relative_path: str
    expected_version: str
    expected_feature: str
    expected_gml_id: str
    min_objects: int = 1


CONTRACTS = (
    ImportContract("Building/Railway_Scene_LoD3_CityGML3_Building_only.gml", "3.0", "Building", "GMLID_BUI184698_512_898"),
    ImportContract("Bridge/Bridge_citygml3.gml", "3.0", "Bridge", "GMLID_BUI205585_1385_1373"),
    ImportContract("Building/Railway_Scene_LoD3_CityGML2_Building_only.gml", "2.0", "Building", "GMLID_BUI184698_512_898"),
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


def _object_custom_prop(obj, key: str) -> str:
    try:
        return str(obj.get(key) or "").strip()
    except Exception:
        return ""


def _scene_feature_objects() -> list:
    return [
        obj for obj in bpy.data.objects
        if _object_custom_prop(obj, "gml_id") or _object_custom_prop(obj, "cgml3_feature")
    ]


def _feature_values(key: str) -> set[str]:
    return {
        value
        for obj in bpy.data.objects
        for value in [_object_custom_prop(obj, key)]
        if value
    }


def _root_collection_names() -> list[str]:
    return [collection.name for collection in bpy.context.scene.collection.children]


def _assert_import_contract(base_dir: str, contract: ImportContract) -> tuple[bool, str]:
    filepath = os.path.join(base_dir, *contract.relative_path.split("/"))
    if not os.path.isfile(filepath):
        return False, f"fixture not found: {filepath}"

    _reset_scene()
    ok, msg = _ensure_addon_enabled()
    if not ok:
        return False, msg

    bpy.context.scene.cgml3.xsd_validate = False
    result = bpy.ops.cgml3.import_gml_file(
        "EXEC_DEFAULT",
        filepath=filepath,
        import_appearance=True,
    )
    if "FINISHED" not in result:
        return False, f"import did not finish: {result}"

    object_count = len(bpy.data.objects)
    if object_count < contract.min_objects:
        return False, f"expected at least {contract.min_objects} objects, got {object_count}"

    feature_objects = _scene_feature_objects()
    if not feature_objects:
        return False, "no objects with gml_id/cgml3_feature custom properties"

    gml_ids = _feature_values("gml_id")
    if contract.expected_gml_id not in gml_ids:
        preview = ", ".join(sorted(gml_ids)[:8])
        return False, f"missing expected gml_id {contract.expected_gml_id}; found: {preview}"

    features = _feature_values("cgml3_feature")
    if not any(value == contract.expected_feature or value.startswith(f"{contract.expected_feature}_") for value in features):
        preview = ", ".join(sorted(features)[:8])
        return False, f"missing expected feature {contract.expected_feature}; found: {preview}"

    root_names = _root_collection_names()
    version_token = f"CityGML{contract.expected_version.split('.')[0]}"
    if not any(version_token in name for name in root_names):
        return False, f"missing {version_token} root collection; roots: {root_names}"

    return True, (
        f"{contract.relative_path}: {object_count} objects, "
        f"{len(feature_objects)} feature objects, {len(gml_ids)} gml_id values"
    )


def main():
    parser = argparse.ArgumentParser(description="CityGML import contract tests")
    parser.add_argument("--dir", required=True, help="Base directory containing test fixtures")
    args = parser.parse_args(_extract_blender_args())

    base_dir = os.path.abspath(args.dir)
    failures = []
    print(f"[TEST] Import contract tests: {len(CONTRACTS)} fixtures")
    print("=" * 60)

    for contract in CONTRACTS:
        ok, msg = _assert_import_contract(base_dir, contract)
        if ok:
            print(f"  PASS  {msg}")
        else:
            print(f"  FAIL  {contract.relative_path} ({msg})")
            failures.append((contract.relative_path, msg))

    print("=" * 60)
    if failures:
        print("[TEST] Failed import contracts:")
        for rel_path, msg in failures:
            print(f"  - {rel_path}: {msg}")
        sys.exit(1)

    print("[TEST] OK - Import contracts passed")
    sys.exit(0)


if __name__ == "__main__":
    main()
