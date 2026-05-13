# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Common I/O Utilities
====================

Gemeinsame Funktionen für CityGML 2.0 und 3.0 Import/Export.
"""

from .version_detection import (
    detect_citygml_version,
    get_version_info,
    print_version_info,
)
from .import_auto import import_citygml_auto

__all__ = [
    'detect_citygml_version',
    'get_version_info',
    'print_version_info',
    'import_citygml_auto',
]
