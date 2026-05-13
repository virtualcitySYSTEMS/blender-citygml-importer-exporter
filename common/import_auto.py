# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# common/import_auto.py
"""
Automatischer CityGML-Import mit Versionserkennung
===================================================

Erkennt automatisch ob CityGML 2.0 oder 3.0 und ruft die entsprechende
Import-Funktion auf.
"""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import bpy

def import_citygml_auto(
    filepath: str,
    context: 'bpy.types.Context',
    *,
    import_appearance: bool = True,
) -> None:
    """
    Importiert CityGML-Datei mit automatischer Versionserkennung.
    
    Args:
        filepath: Pfad zur CityGML-Datei
        context: Blender-Context
    
    Erkennt die Version und ruft dann auf:
    - CityGML 2.0 → io_v2.reader.import_citygml2_into_blender
    - CityGML 3.0 → io.reader.import_citygml3_into_blender
    """
    from ..ops.validate_auto import detect_version_from_file

    # Version erkennen (robust, ohne Voll-Import)
    version, msg = detect_version_from_file(filepath)

    if version == '2.0':
        from ..io_v2 import import_citygml2_into_blender
        print(f"[CityGML Import] {msg} - verwende CityGML 2.0 Importer")
        import_citygml2_into_blender(filepath, context, import_appearance=import_appearance)
        return

    if version == '3.0':
        from ..io import import_citygml3_into_blender
        print(f"[CityGML Import] {msg} - verwende CityGML 3.0 Importer")
        import_citygml3_into_blender(filepath, context, import_appearance=import_appearance)
        return

    # Unknown: try a lightweight hint from file header to avoid routing 2.0 into 3.0 importer.
    # This prevents "Null objects only" when a CityGML 2.0 file is mis-detected or parse fails.
    try:
        from pathlib import Path
        header = Path(filepath).read_text(encoding="utf-8", errors="ignore")[:200_000].lower()
        if "citygml/2.0" in header or "citygml/building/2.0" in header or "citygml/core/2.0" in header:
            from ..io_v2 import import_citygml2_into_blender
            print(f"[CityGML Import] Version unbekannt ({msg}) - Header deutet auf CityGML 2.0; verwende 2.0 Importer")
            import_citygml2_into_blender(filepath, context, import_appearance=import_appearance)
            return
        if "citygml/3.0" in header or "citygml/core/3.0" in header or "citygml/building/3.0" in header:
            from ..io import import_citygml3_into_blender
            print(f"[CityGML Import] Version unbekannt ({msg}) - Header deutet auf CityGML 3.0; verwende 3.0 Importer")
            import_citygml3_into_blender(filepath, context, import_appearance=import_appearance)
            return
    except Exception:
        pass

    # Final fallback: prefer CityGML 2.0 importer because 2.0 files are common and 3.0 importer can
    # silently create empty/null objects when fed 2.0 structures.
    from ..io_v2 import import_citygml2_into_blender
    print(f"[CityGML Import] Version unbekannt ({msg}) - verwende CityGML 2.0 Importer als Fallback")
    import_citygml2_into_blender(filepath, context, import_appearance=import_appearance)
