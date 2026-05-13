# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Core module for CityGML ModelTyper
Geometry, classification, and material management
"""

from . import geometry
from . import classification
from . import materials
from . import advanced_classification
from . import property_mapping

def register():
    geometry.register() if hasattr(geometry, 'register') else None
    classification.register() if hasattr(classification, 'register') else None
    materials.register() if hasattr(materials, 'register') else None
    advanced_classification.register() if hasattr(advanced_classification, 'register') else None
    property_mapping.register() if hasattr(property_mapping, 'register') else None

def unregister():
    property_mapping.unregister() if hasattr(property_mapping, 'unregister') else None
    advanced_classification.unregister() if hasattr(advanced_classification, 'unregister') else None
    materials.unregister() if hasattr(materials, 'unregister') else None
    classification.unregister() if hasattr(classification, 'unregister') else None
    geometry.unregister() if hasattr(geometry, 'unregister') else None
