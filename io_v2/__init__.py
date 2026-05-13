# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
CityGML 2.0 I/O Module
======================

Parallele Implementierung zu CityGML 3.0 mit vollständiger Unterstützung
für den CityGML 2.0 Standard (OGC 12-019, Februar 2012).

Hauptunterschiede zu CityGML 3.0:
- GML 3.1.1 (statt GML 3.2.1) - Namespace ohne /3.2 Suffix
- xAL 2.0 (statt xAL 3.0) für Adressen
- Temporal Properties als xs:date (statt xs:dateTime)
- Feature-Namen: Room (statt BuildingRoom), etc.
- LOD0: lod0FootPrint/lod0RoofEdge (statt lod0MultiSurface)
- Keine Construction-Module (Surfaces im Building-Modul)

Struktur:
- reader/: Import von CityGML 2.0 Dateien (GML 3.1.1 → Blender)
- writer/: Export nach CityGML 2.0 (Blender → GML 3.1.1)

Verwendung (aus Addon-Root):
    from io_v2 import import_citygml2_into_blender
    from io_v2 import export_citygml2_from_blender

Verwendung (aus Submodulen):
    from ..io_v2 import import_citygml2_into_blender
    from ...io_v2 import export_citygml2_from_blender
"""

__version__ = '1.0.0'
__citygml_version__ = '2.0'



def import_citygml2_into_blender(*args, **kwargs):
    from .reader.importer import import_citygml2_into_blender as _import_citygml2_into_blender
    return _import_citygml2_into_blender(*args, **kwargs)


def export_citygml2_from_blender(*args, **kwargs):
    from .writer.exporter import export_blender_to_citygml3 as _export_citygml2_from_blender
    return _export_citygml2_from_blender(*args, **kwargs)

__all__ = [
    'import_citygml2_into_blender',
    'export_citygml2_from_blender',
]
