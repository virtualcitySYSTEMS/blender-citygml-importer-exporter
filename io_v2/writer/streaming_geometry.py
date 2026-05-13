# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io_v2/writer/streaming_geometry.py
"""
Streaming Geometry Buffer for CityGML 2.0 Exports

Adapts io/writer/streaming_geometry.py for CityGML 2.0.
Handles batch processing of geometry data during export.
"""

from __future__ import annotations
from typing import Dict, List, Tuple, Optional, Any
import bpy

# Import geometry utilities from CityGML 3.0 (mostly version-agnostic)
from ...io.writer.streaming_geometry import StreamingGeometryBuffer as BaseStreamingGeometryBuffer


class StreamingGeometryBuffer(BaseStreamingGeometryBuffer):
    """
    Geometry buffer for CityGML 2.0 streaming export.
    
    Inherits most functionality from CityGML 3.0 implementation,
    with minor adaptations for 2.0-specific requirements.
    """
    
    def __init__(self, max_buffer_size_mb: float = 100.0):
        """
        Initialize geometry buffer for CityGML 2.0.
        
        Args:
            max_buffer_size_mb: Maximum buffer size in megabytes
        """
        super().__init__(max_buffer_size_mb)
        self.version = "2.0"
    
    def get_namespace_prefix(self, element_type: str) -> str:
        """
        Get namespace prefix for element type (CityGML 2.0 specific).
        
        Args:
            element_type: Type of element (e.g., 'Building', 'Road')
        
        Returns:
            Namespace prefix (e.g., 'bldg', 'tran')
        """
        # CityGML 2.0 uses same prefixes as 3.0, so delegate to parent
        return super().get_namespace_prefix(element_type)


# Re-export helper functions from base module
from ...io.writer.streaming_geometry import (
    estimate_geometry_size,
    batch_process_geometries
)
