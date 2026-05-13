# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
CityGML 2.0 Writer - Legacy Wrapper (DEPRECATED)
=========================================

Legacy export-wrapper für CityGML 2.0 Dateien.
Nutzt die modulare Struktur aus io_v2/writer/.

Verwendung (empfohlen):
    from .io_v2 import export_citygml2_from_blender
    export_citygml2_from_blender('/path/to/output.gml', context, srs_name='EPSG:25832')

Legacy (noch unterstützt):
    from .io_v2.gml2_writer import export_citygml2
    export_citygml2('/path/to/output.gml', context, srs_name='EPSG:25832')
"""

from .writer.exporter import export_blender_to_citygml3 as export_citygml2_from_blender

# Alias für konsistente API
export_citygml2 = export_citygml2_from_blender

__all__ = ['export_citygml2', 'export_citygml2_from_blender']
