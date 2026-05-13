# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io/__init__.py
"""I/O package for CityGML Blender add-on.

Enthält:
- CityGML 3.0: import_citygml3_into_blender, export_blender_to_citygml3
- Common: Version detection utilities
"""

from ..common.version_detection import detect_citygml_version, get_version_info


def import_citygml3_into_blender(*args, **kwargs):
    from .gml3_reader import import_citygml3_into_blender as _import_citygml3_into_blender
    return _import_citygml3_into_blender(*args, **kwargs)


def export_blender_to_citygml3(*args, **kwargs):
    from .gml3_writer import export_blender_to_citygml3 as _export_blender_to_citygml3
    return _export_blender_to_citygml3(*args, **kwargs)

__all__ = [
    # CityGML 3.0
    "import_citygml3_into_blender",
    "export_blender_to_citygml3",
    # Common
    "detect_citygml_version",
    "get_version_info",
]

