# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
CityGML 2.0 Writer Module
=========================

Export-Funktionalität für CityGML 2.0 Dateien.
Verwendet generische Feature-Verarbeitung analog zu CityGML 3.0.
"""

from .exporter import export_blender_to_citygml3 as export_citygml2_from_blender

__all__ = [
    'export_citygml2_from_blender',
]
