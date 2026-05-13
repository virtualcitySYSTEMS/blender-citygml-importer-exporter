# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
CityGML 2.0 Reader Module
=========================

Import-Funktionalität für CityGML 2.0 Dateien.
Verwendet generische Feature-Verarbeitung analog zu CityGML 3.0.
"""

from .importer import import_citygml2_into_blender

__all__ = [
    'import_citygml2_into_blender',
]
