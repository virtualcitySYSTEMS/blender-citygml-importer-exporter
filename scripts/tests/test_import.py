"""
Test: CityGML Import (alle Feature-Type-Ordner)
Durchsucht test_files/ nach Unterordnern mit .gml-Dateien und importiert jede einzeln.
Jede Datei wird in einer frischen Blender-Szene importiert.

Aufruf:
  blender --background --addons citygml_importer_exporter --python scripts/tests/test_import.py -- --dir test_files
"""
import argparse
import os
import sys

import bpy

ADDON_MODULE = "citygml_importer_exporter"


def _extract_blender_args() -> list[str]:
    if "--" not in sys.argv:
        return []
    return sys.argv[sys.argv.index("--") + 1:]


def _collect_gml_files(base_dir: str) -> list[str]:
    """Sammelt alle .gml-Dateien aus Unterordnern von base_dir."""
    gml_files = []
    for entry in sorted(os.listdir(base_dir)):
        subdir = os.path.join(base_dir, entry)
        if not os.path.isdir(subdir):
            continue
        # Überspringe Appearance-Ordner
        if entry.lower() == "appearance":
            continue
        for fname in sorted(os.listdir(subdir)):
            if fname.lower().endswith(".gml"):
                gml_files.append(os.path.join(subdir, fname))
    return gml_files


def _reset_scene():
    """Löscht alle Objekte, Meshes, Collections für einen frischen Import."""
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _import_single_file(filepath: str) -> tuple[bool, str]:
    """Importiert eine einzelne GML-Datei und gibt (success, message) zurück."""
    _reset_scene()

    # Add-on sicherstellen
    if ADDON_MODULE not in bpy.context.preferences.addons:
        try:
            bpy.ops.preferences.addon_enable(module=ADDON_MODULE)
        except Exception as e:
            return False, f"addon_enable fehlgeschlagen: {e}"

    # XSD-Validierung deaktivieren
    bpy.context.scene.cgml3.xsd_validate = False

    objects_before = len(bpy.data.objects)

    try:
        result = bpy.ops.cgml3.import_gml_file(
            "EXEC_DEFAULT",
            filepath=filepath,
            import_appearance=True,
        )
    except RuntimeError as e:
        return False, f"Import-Exception: {e}"

    if "FINISHED" not in result:
        return False, f"Import nicht erfolgreich: {result}"

    imported_count = len(bpy.data.objects) - objects_before
    if imported_count <= 0:
        return False, "Keine Objekte importiert"

    return True, f"{imported_count} Objekt(e) importiert"


def main():
    parser = argparse.ArgumentParser(description="CityGML Import Test (alle Feature-Typen)")
    parser.add_argument("--dir", required=True, help="Basis-Verzeichnis mit Feature-Typ-Unterordnern")
    args = parser.parse_args(_extract_blender_args())

    base_dir = os.path.abspath(args.dir)
    if not os.path.isdir(base_dir):
        print(f"[TEST] FEHLER: Verzeichnis nicht gefunden: {base_dir}")
        sys.exit(1)

    gml_files = _collect_gml_files(base_dir)
    if not gml_files:
        print(f"[TEST] FEHLER: Keine .gml-Dateien in Unterordnern von {base_dir}")
        sys.exit(1)

    print(f"[TEST] Import-Test: {len(gml_files)} Dateien gefunden")
    print("=" * 60)

    passed = 0
    failed = 0
    failures = []

    for filepath in gml_files:
        rel_path = os.path.relpath(filepath, base_dir)
        success, msg = _import_single_file(filepath)
        if success:
            passed += 1
            print(f"  PASS  {rel_path} ({msg})")
        else:
            failed += 1
            failures.append((rel_path, msg))
            print(f"  FAIL  {rel_path} ({msg})")

    print("=" * 60)
    print(f"[TEST] Ergebnis: {passed} bestanden, {failed} fehlgeschlagen, {passed + failed} gesamt")

    if failures:
        print("\n[TEST] Fehlgeschlagene Dateien:")
        for rel_path, msg in failures:
            print(f"  - {rel_path}: {msg}")
        sys.exit(1)

    print("[TEST] OK - Alle Imports erfolgreich")
    sys.exit(0)


if __name__ == "__main__":
    main()
