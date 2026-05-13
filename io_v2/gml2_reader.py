# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
CityGML 2.0 Reader - Legacy Wrapper (DEPRECATED)
=========================================

Legacy import-wrapper für CityGML 2.0 Dateien.
Nutzt die modulare Struktur aus io_v2/reader/.

Verwendung (empfohlen):
    from .io_v2 import import_citygml2_into_blender
    import_citygml2_into_blender('/path/to/file.gml', context)

Legacy (noch unterstützt):
    from .io_v2.gml2_reader import import_citygml2
    import_citygml2('/path/to/file.gml', context)
"""

from .reader.importer import import_citygml3_into_blender as import_citygml2_into_blender

# Alias für konsistente API
import_citygml2 = import_citygml2_into_blender

__all__ = ['import_citygml2', 'import_citygml2_into_blender']
