"""
Test 1: Plugin-Aktivierung
Prüft ob das Add-on fehlerfrei aktiviert werden kann.

Aufruf:
  blender --background --python scripts/tests/test_activate.py
"""
import sys
import bpy

ADDON_MODULE = "citygml-importer-exporter"


def main():
    print(f"[TEST] Aktiviere Add-on: {ADDON_MODULE}")

    try:
        bpy.ops.preferences.addon_enable(module=ADDON_MODULE)
    except Exception as e:
        print(f"[TEST] FEHLER bei addon_enable: {e}")
        sys.exit(1)

    # Prüfe ob das Addon in den Preferences registriert ist
    if ADDON_MODULE not in bpy.context.preferences.addons:
        print(f"[TEST] FEHLER: Add-on '{ADDON_MODULE}' nicht in addons gefunden nach Aktivierung")
        sys.exit(1)

    # Prüfe ob der Import-Operator verfügbar ist
    if not hasattr(bpy.ops, "cgml3") or not hasattr(bpy.ops.cgml3, "import_gml_file"):
        print("[TEST] FEHLER: Operator cgml3.import_gml_file nicht verfügbar")
        sys.exit(1)

    # Prüfe ob der Export-Operator verfügbar ist
    if not hasattr(bpy.ops.cgml3, "export_gml_file"):
        print("[TEST] FEHLER: Operator cgml3.export_gml_file nicht verfügbar")
        sys.exit(1)

    print("[TEST] OK - Add-on erfolgreich aktiviert, Operatoren verfügbar")
    sys.exit(0)


if __name__ == "__main__":
    main()
