# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
UI module for CityGML ModelTyper
"""

from . import properties
from . import panels

def register():
    properties.register()
    panels.register()

def unregister():
    panels.unregister()
    properties.unregister()
