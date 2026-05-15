"""
Test: CityGML export for all feature-type fixture folders.

The test imports each .gml fixture, exports it in the source CityGML version,
and checks the exported files for XML well-formedness, expected CityGML version
markers, cityObjectMember content, and duplicate gml:id values.

Usage:
  blender --background --addons citygml_importer_exporter --python scripts/tests/test_export.py -- --dir test_files
"""
import argparse
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

import bpy

ADDON_MODULE = "citygml_importer_exporter"
XSI_SCHEMA_LOCATION = "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation"
CITYGML_VERSION_MARKERS = {
    "2.0": (
        "http://www.opengis.net/citygml/2.0",
        "http://www.opengis.net/citygml/profiles/base/2.0",
        "citygml/2.0",
    ),
    "3.0": (
        "http://www.opengis.net/citygml/3.0",
        "citygml/3.0",
    ),
}

PART_RELATION_FEATURES = (
    ("consistsOfBuildingPart", "BuildingPart"),
    ("consistsOfBridgePart", "BridgePart"),
    ("consistsOfTunnelPart", "TunnelPart"),
)


def _extract_blender_args() -> list[str]:
    if "--" not in sys.argv:
        return []
    return sys.argv[sys.argv.index("--") + 1:]


def _collect_gml_files(base_dir: str) -> list[str]:
    """Collect all .gml files from direct feature-type subfolders."""
    gml_files = []
    for entry in sorted(os.listdir(base_dir)):
        subdir = os.path.join(base_dir, entry)
        if not os.path.isdir(subdir):
            continue
        if entry.lower() == "appearance":
            continue
        for fname in sorted(os.listdir(subdir)):
            if fname.lower().endswith(".gml"):
                gml_files.append(os.path.join(subdir, fname))
    return gml_files


def _reset_scene():
    """Reset Blender to an empty scene before each import/export test."""
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _split_xml_tag(tag: str) -> tuple[str, str]:
    if tag.startswith("{") and "}" in tag:
        namespace, local_name = tag[1:].split("}", 1)
        return namespace, local_name
    return "", tag


def _collect_xml_namespaces(filepath: str) -> list[str]:
    namespaces = []
    try:
        for _event, namespace in ET.iterparse(filepath, events=("start-ns",)):
            _prefix, uri = namespace
            if uri not in namespaces:
                namespaces.append(uri)
    except ET.ParseError:
        return namespaces
    return namespaces


def _detect_citygml_version(filepath: str) -> tuple[str | None, str]:
    """Detect the source CityGML version from root namespace/schema hints."""
    try:
        root = ET.parse(filepath).getroot()
    except ET.ParseError as e:
        return None, f"Input XML not well-formed: {e}"

    root_namespace, root_local_name = _split_xml_tag(root.tag)
    if root_local_name != "CityModel":
        return None, f"Unexpected input root element: {root.tag}"

    if root_namespace == "http://www.opengis.net/citygml/3.0":
        return "3.0", ""
    if root_namespace == "http://www.opengis.net/citygml/2.0":
        return "2.0", ""

    schema_location = root.get(XSI_SCHEMA_LOCATION, "")
    namespaces = _collect_xml_namespaces(filepath)
    version_hints = [root_namespace, schema_location, *namespaces]
    version_hints = [hint.lower() for hint in version_hints if hint]

    for version, markers in CITYGML_VERSION_MARKERS.items():
        marker_values = tuple(marker.lower() for marker in markers)
        if any(marker in hint for hint in version_hints for marker in marker_values):
            return version, ""

    return None, "CityGML version not found in input namespaces/schemaLocation"


def _assert_export_xml_contract(filepath: str, version: str) -> tuple[bool, str]:
    """Check the exported file beyond mere existence."""
    try:
        tree = ET.parse(filepath)
    except ET.ParseError as e:
        return False, f"Export XML not well-formed: {e}"

    root = tree.getroot()
    root_namespace, root_local_name = _split_xml_tag(root.tag)
    if root_local_name != "CityModel":
        return False, f"Unexpected root element: {root.tag}"

    schema_location = root.get(XSI_SCHEMA_LOCATION, "")
    namespaces = _collect_xml_namespaces(filepath)
    version_hints = [root_namespace, schema_location, *namespaces]
    version_hints = [hint.lower() for hint in version_hints if hint]
    markers = tuple(marker.lower() for marker in CITYGML_VERSION_MARKERS[version])
    if not any(marker in hint for hint in version_hints for marker in markers):
        return False, f"CityGML version {version} not found in namespaces/schemaLocation"

    city_object_members = [
        elem for elem in root.iter()
        if _split_xml_tag(str(elem.tag))[1] == "cityObjectMember"
    ]
    if not city_object_members:
        return False, "Export contains no cityObjectMember"

    gml_id_namespaces = (
        "http://www.opengis.net/gml/3.2",
        "http://www.opengis.net/gml",
    )
    gml_ids = []
    for elem in root.iter():
        for namespace in gml_id_namespaces:
            value = elem.get(f"{{{namespace}}}id")
            if value:
                gml_ids.append(value)

    seen = set()
    duplicates = set()
    for value in gml_ids:
        if value in seen:
            duplicates.add(value)
        else:
            seen.add(value)
    duplicates = sorted(duplicates)
    if duplicates:
        preview = ", ".join(duplicates[:5])
        return False, f"Duplicate gml:id values in export: {preview}"

    return True, f"XML OK ({len(city_object_members)} cityObjectMember, {len(gml_ids)} gml:id)"


def _count_inline_part_relations(filepath: str) -> dict[tuple[str, str], int]:
    try:
        root = ET.parse(filepath).getroot()
    except ET.ParseError:
        return {}

    counts = {}
    for relation_local, feature_local in PART_RELATION_FEATURES:
        total = 0
        for elem in root.iter():
            if _split_xml_tag(str(elem.tag))[1] != relation_local:
                continue
            if any(_split_xml_tag(str(child.tag))[1] == feature_local for child in list(elem)):
                total += 1
        counts[(relation_local, feature_local)] = total
    return counts


def _count_top_level_part_members(filepath: str) -> int:
    try:
        root = ET.parse(filepath).getroot()
    except ET.ParseError:
        return 0

    part_feature_names = {feature_local for _relation_local, feature_local in PART_RELATION_FEATURES}
    total = 0
    for member in root.iter():
        if _split_xml_tag(str(member.tag))[1] != "cityObjectMember":
            continue
        for child in list(member):
            if _split_xml_tag(str(child.tag))[1] in part_feature_names:
                total += 1
    return total


def _assert_citygml2_part_relations_preserved(input_path: str, export_path: str) -> tuple[bool, str]:
    input_counts = _count_inline_part_relations(input_path)
    input_total = sum(input_counts.values())
    if input_total == 0:
        return True, ""

    output_counts = _count_inline_part_relations(export_path)
    missing = []
    for key, input_count in input_counts.items():
        if input_count <= 0:
            continue
        output_count = output_counts.get(key, 0)
        if output_count < input_count:
            missing.append(f"{key[0]}/{key[1]} expected>={input_count} got={output_count}")

    if missing:
        return False, "Part relation(s) not preserved: " + "; ".join(missing)

    input_top_level_parts = _count_top_level_part_members(input_path)
    top_level_parts = _count_top_level_part_members(export_path)
    if top_level_parts > input_top_level_parts:
        return False, (
            "Part relation(s) preserved, but additional part object(s) were also "
            f"exported as top-level cityObjectMember: input={input_top_level_parts}, output={top_level_parts}"
        )

    return True, f"part relations OK ({input_total})"


def _export_single_file(filepath: str, version: str) -> tuple[bool, str]:
    """Import one GML file, export it as version, and return (success, message)."""
    _reset_scene()

    if ADDON_MODULE not in bpy.context.preferences.addons:
        try:
            bpy.ops.preferences.addon_enable(module=ADDON_MODULE)
        except Exception as e:
            return False, f"addon_enable failed: {e}"

    bpy.context.scene.cgml3.xsd_validate = False

    try:
        result = bpy.ops.cgml3.import_gml_file(
            "EXEC_DEFAULT",
            filepath=filepath,
            import_appearance=True,
        )
    except RuntimeError as e:
        return False, f"Import exception: {e}"

    if "FINISHED" not in result:
        return False, f"Import did not finish: {result}"

    if len(bpy.data.objects) == 0:
        return False, "No objects after import"

    # Test the normal full-scene export path.
    bpy.ops.object.select_all(action="DESELECT")
    basename = os.path.splitext(os.path.basename(filepath))[0]

    with tempfile.TemporaryDirectory(prefix="cgml3_export_test_") as tmpdir:
        export_path = os.path.join(tmpdir, f"{basename}_export_v{version}.gml")

        try:
            result = bpy.ops.cgml3.export_gml_file(
                "EXEC_DEFAULT",
                filepath=export_path,
                citygml_version=version,
            )
        except RuntimeError as e:
            return False, f"Export exception: {e}"

        if "FINISHED" not in result:
            return False, f"Export did not finish: {result}"

        if not os.path.isfile(export_path):
            return False, "Export file was not created"

        file_size = os.path.getsize(export_path)
        if file_size == 0:
            return False, "Export file is empty"

        contract_ok, contract_msg = _assert_export_xml_contract(export_path, version)
        if not contract_ok:
            return False, contract_msg

        if version == "2.0":
            parts_ok, parts_msg = _assert_citygml2_part_relations_preserved(filepath, export_path)
            if not parts_ok:
                return False, parts_msg
            if parts_msg:
                contract_msg = f"{contract_msg}, {parts_msg}"

        return True, f"Export OK ({file_size} bytes, {contract_msg})"


def main():
    parser = argparse.ArgumentParser(description="CityGML export test for all feature types")
    parser.add_argument("--dir", required=True, help="Base directory with feature-type subfolders")
    args = parser.parse_args(_extract_blender_args())

    base_dir = os.path.abspath(args.dir)
    if not os.path.isdir(base_dir):
        print(f"[TEST] ERROR: Directory not found: {base_dir}")
        sys.exit(1)

    gml_files = _collect_gml_files(base_dir)
    if not gml_files:
        print(f"[TEST] ERROR: No .gml files found below subfolders of {base_dir}")
        sys.exit(1)

    print(f"[TEST] Export test: {len(gml_files)} files, native CityGML version only")
    print("=" * 60)

    passed = 0
    failed = 0
    failures = []

    for filepath in gml_files:
        rel_path = os.path.relpath(filepath, base_dir)
        version, version_msg = _detect_citygml_version(filepath)
        if not version:
            failed += 1
            failures.append((rel_path, version_msg))
            print(f"  FAIL  {rel_path} ({version_msg})")
            continue

        test_name = f"{rel_path} -> v{version}"
        success, msg = _export_single_file(filepath, version)
        if success:
            passed += 1
            print(f"  PASS  {test_name} ({msg})")
        else:
            failed += 1
            failures.append((test_name, msg))
            print(f"  FAIL  {test_name} ({msg})")

    print("=" * 60)
    print(f"[TEST] Result: {passed} passed, {failed} failed, {passed + failed} total")

    if failures:
        print("\n[TEST] Failed tests:")
        for name, msg in failures:
            print(f"  - {name}: {msg}")
        sys.exit(1)

    print("[TEST] OK - All exports succeeded")
    sys.exit(0)


if __name__ == "__main__":
    main()
