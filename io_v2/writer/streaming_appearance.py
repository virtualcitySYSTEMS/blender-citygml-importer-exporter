# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io_v2/writer/streaming_appearance.py
"""
Streaming Appearance Collector for CityGML 2.0 Exports

Adapts io/writer/streaming_appearance.py for CityGML 2.0.
Manages appearance data (materials, textures) during streaming export.
"""

from __future__ import annotations
from typing import Dict, List, Tuple, Optional, Set
import bpy

# Import appearance handling from CityGML 3.0 (mostly version-agnostic)
from ...io.writer.streaming_appearance import (
    StreamingAppearanceCollector as BaseStreamingAppearanceCollector
)


class StreamingAppearanceCollector(BaseStreamingAppearanceCollector):
    """
    Appearance collector for CityGML 2.0 streaming export.
    
    Handles materials and textures with CityGML 2.0 specific formatting.
    """
    
    def __init__(self, texture_output_dir: Optional[str] = None):
        """
        Initialize appearance collector for CityGML 2.0.
        
        Args:
            texture_output_dir: Directory for texture file output
        """
        super().__init__(texture_output_dir)
        self.version = "2.0"
    
    def get_appearance_namespace(self) -> str:
        """Get CityGML 2.0 appearance namespace."""
        return "http://www.opengis.net/citygml/appearance/2.0"


# Re-export helper functions
from ...io.writer.streaming_appearance import (
    collect_material_data,
    collect_texture_data,
    generate_appearance_xml
)
