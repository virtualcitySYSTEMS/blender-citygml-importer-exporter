# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io_v2/writer/streaming_writer.py
"""
Streaming XML Writer for Large CityGML 2.0 Exports

This module adapts the CityGML 3.0 streaming writer for CityGML 2.0.
It wraps io/writer/streaming_writer.py with CityGML 2.0 namespace handling.

Key Differences from CityGML 3.0:
- Uses CityGML 2.0 namespaces (http://www.opengis.net/citygml/2.0)
- cityObjectMember instead of core:cityObjectMember
- Generic attributes as @name attributes
- Different schema locations
"""

from __future__ import annotations
from typing import Dict, List, Optional, TextIO, Set
from contextlib import contextmanager
from datetime import datetime
import os

# Import CityGML 2.0 namespace definitions
from .namespaces import NS, Q, GML_ID, build_schema_location_citygml2


# Re-export StreamingStats from CityGML 3.0 (version-agnostic)
from ...io.writer.streaming_writer import StreamingStats


class StreamingXMLWriter:
    """
    Incremental XML writer for CityGML 2.0 exports.
    
    This is a thin wrapper around the CityGML 3.0 StreamingXMLWriter,
    adapted for CityGML 2.0 namespace conventions.
    """
    
    def __init__(
        self,
        filepath: str,
        srs_name: str,
        version: str = "2.0",
        indent: str = "  ",
        enable_memory_tracking: bool = False
    ):
        """
        Initialize streaming writer for CityGML 2.0.
        
        Args:
            filepath: Output GML file path
            srs_name: SRS identifier (e.g., "EPSG:25832")
            version: CityGML version (should be "2.0")
            indent: Indentation string for pretty printing
            enable_memory_tracking: Enable memory profiling (requires tracemalloc)
        """
        self.filepath = filepath
        self.srs_name = srs_name
        self.version = version
        self.indent = indent
        self.enable_memory_tracking = enable_memory_tracking
        
        self._file: Optional[TextIO] = None
        self._indent_level = 0
        self._used_gml_ids: Set[str] = set()
        self.stats = StreamingStats()
        
        # Track if we need to close appearance tag
        self._appearance_open = False
    
    def __enter__(self):
        """Open file and write XML header + CityModel opening tag."""
        self._file = open(self.filepath, 'w', encoding='utf-8')
        self._write_xml_header()
        self._write_citymodel_open()
        
        if self.enable_memory_tracking:
            import tracemalloc
            tracemalloc.start()
        
        import time
        self.stats.start_time = time.time()
        
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Write closing tags and close file."""
        import time
        self.stats.end_time = time.time()
        
        self._write_citymodel_close()
        
        if self._file:
            self._file.close()
            self._file = None
        
        if self.enable_memory_tracking:
            import tracemalloc
            current, peak = tracemalloc.get_traced_memory()
            self.stats.peak_memory_mb = peak / 1024 / 1024
            tracemalloc.stop()
        
        return False  # Don't suppress exceptions
    
    def _write_xml_header(self):
        """Write XML declaration and processing instructions."""
        self._file.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    
    def _write_citymodel_open(self):
        """Write CityGML 2.0 CityModel opening tag with namespaces."""
        # CityGML 2.0 namespace declarations
        self._file.write('<CityModel xmlns="http://www.opengis.net/citygml/2.0"\n')
        self._write_indent(1)
        self._file.write('xmlns:gml="http://www.opengis.net/gml"\n')
        self._write_indent(1)
        self._file.write('xmlns:bldg="http://www.opengis.net/citygml/building/2.0"\n')
        self._write_indent(1)
        self._file.write('xmlns:brid="http://www.opengis.net/citygml/bridge/2.0"\n')
        self._write_indent(1)
        self._file.write('xmlns:tran="http://www.opengis.net/citygml/transportation/2.0"\n')
        self._write_indent(1)
        self._file.write('xmlns:tun="http://www.opengis.net/citygml/tunnel/2.0"\n')
        self._write_indent(1)
        self._file.write('xmlns:veg="http://www.opengis.net/citygml/vegetation/2.0"\n')
        self._write_indent(1)
        self._file.write('xmlns:wtr="http://www.opengis.net/citygml/waterbody/2.0"\n')
        self._write_indent(1)
        self._file.write('xmlns:luse="http://www.opengis.net/citygml/landuse/2.0"\n')
        self._write_indent(1)
        self._file.write('xmlns:gen="http://www.opengis.net/citygml/generics/2.0"\n')
        self._write_indent(1)
        self._file.write('xmlns:app="http://www.opengis.net/citygml/appearance/2.0"\n')
        self._write_indent(1)
        self._file.write('xmlns:frn="http://www.opengis.net/citygml/cityfurniture/2.0"\n')
        self._write_indent(1)
        self._file.write('xmlns:grp="http://www.opengis.net/citygml/cityobjectgroup/2.0"\n')
        self._write_indent(1)
        self._file.write('xmlns:dem="http://www.opengis.net/citygml/relief/2.0"\n')
        self._write_indent(1)
        self._file.write('xmlns:xAL="urn:oasis:names:tc:ciq:xsdschema:xAL:2.0"\n')
        self._write_indent(1)
        self._file.write('xmlns:xlink="http://www.w3.org/1999/xlink"\n')
        self._write_indent(1)
        self._file.write('xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n')
        self._write_indent(1)
        mode = "REMOTE"
        try:
            import bpy

            mode = str(getattr(getattr(bpy.context, "scene", None), "cgml3", None).export_schema_location_mode)
        except Exception:
            mode = os.environ.get("CGML_SCHEMA_LOCATION_MODE", "REMOTE")

        if mode == "NONE":
            self._file.write('>\n')
        else:
            # LOCAL currently behaves like REMOTE (can be reintroduced if needed).
            self._file.write(f'xsi:schemaLocation="{build_schema_location_citygml2()}">\n')
        
        self._indent_level = 1
    
    def _write_citymodel_close(self):
        """Write CityModel closing tag."""
        self._indent_level = 0
        self._file.write('</CityModel>\n')
    
    def _write_indent(self, level: Optional[int] = None):
        """Write indentation."""
        if level is None:
            level = self._indent_level
        self._file.write(self.indent * level)
    
    def write_city_object(self, feature_elem: str):
        """
        Write a single city object to file.
        
        Args:
            feature_elem: XML string of feature element
        """
        # Write cityObjectMember wrapper (CityGML 2.0 style, no namespace prefix)
        self._write_indent()
        self._file.write('<cityObjectMember>\n')
        
        # Write feature content
        self._indent_level += 1
        self._file.write(feature_elem)
        self._indent_level -= 1
        
        # Close wrapper
        self._write_indent()
        self._file.write('</cityObjectMember>\n')
        
        # Update statistics
        self.stats.objects_written += 1
        self.stats.bytes_written += len(feature_elem) + 50  # Approximate
        
        # Flush to disk to free memory
        self._file.flush()
    
    def write_appearance_member(self, appearance_elem: str):
        """
        Write an appearance member to file.
        
        Args:
            appearance_elem: XML string of appearance element
        """
        self._write_indent()
        self._file.write('<appearanceMember>\n')
        
        self._indent_level += 1
        self._file.write(appearance_elem)
        self._indent_level -= 1
        
        self._write_indent()
        self._file.write('</appearanceMember>\n')
        
        self.stats.appearances_written += 1
        self.stats.bytes_written += len(appearance_elem) + 50
        
        self._file.flush()
    
    def register_gml_id(self, gml_id: str):
        """Register a gml:id to prevent duplicates."""
        self._used_gml_ids.add(gml_id)
    
    def is_gml_id_used(self, gml_id: str) -> bool:
        """Check if gml:id has been used."""
        return gml_id in self._used_gml_ids
    
    def get_stats(self) -> StreamingStats:
        """Get export statistics."""
        return self.stats


@contextmanager
def open_streaming_writer(filepath: str, srs_name: str, **kwargs):
    """
    Context manager for streaming CityGML 2.0 export.
    
    Usage:
        with open_streaming_writer("output.gml", "EPSG:25832") as writer:
            for obj in blender_objects:
                feature_xml = create_feature_xml(obj)
                writer.write_city_object(feature_xml)
    """
    writer = StreamingXMLWriter(filepath, srs_name, **kwargs)
    try:
        yield writer.__enter__()
    finally:
        writer.__exit__(None, None, None)
