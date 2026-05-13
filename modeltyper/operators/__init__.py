# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Operators module for CityGML ModelTyper
"""

from . import auto_assign
from . import manual_assign
from . import analyze
from . import property_mapping

def register():
    auto_assign.register()
    manual_assign.register()
    analyze.register()
    property_mapping.register()

def unregister():
    property_mapping.unregister()
    analyze.unregister()
    manual_assign.unregister()
    auto_assign.unregister()
