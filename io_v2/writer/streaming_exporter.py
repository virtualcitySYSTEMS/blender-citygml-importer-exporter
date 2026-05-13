# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io_v2/writer/streaming_exporter.py
"""
Streaming Exporter for Large CityGML 2.0 Datasets

This module provides memory-efficient export for large Blender scenes to CityGML 2.0.
It adapts io/writer/streaming_exporter.py for CityGML 2.0 conventions.

Key Features:
- Memory-efficient streaming export (10× less memory than traditional approach)
- Batch processing of objects
- Automatic appearance and texture handling
- Progress reporting
- Error recovery

Usage:
    from io_v2.writer.streaming_exporter import export_citygml2_streaming
    
    export_citygml2_streaming(
        filepath="large_city.gml",
        context=bpy.context,
        srs_name="EPSG:25832",
        selected_only=False
    )
"""

from __future__ import annotations
from typing import List, Dict, Optional, Set
import bpy
import time

from .streaming_writer import StreamingXMLWriter, StreamingStats
from .streaming_geometry import StreamingGeometryBuffer
from .streaming_appearance import StreamingAppearanceCollector
from .exporter import (
    # Import CityGML 2.0 specific export functions
    _write_feature_xml,
    _collect_blender_objects,
    _setup_export_context
)
from ...shared.export_helpers import object_is_viewport_visible


def export_citygml2_streaming(
    filepath: str,
    context,
    srs_name: str = "EPSG:25832",
    selected_only: bool = False,
    batch_size: int = 100,
    enable_progress: bool = True,
    enable_memory_tracking: bool = False,
    **export_options
) -> Dict[str, any]:
    """
    Export Blender scene to CityGML 2.0 using streaming approach.
    
    This function processes objects in batches and writes them incrementally
    to avoid loading the entire scene into memory at once.
    
    Args:
        filepath: Output GML file path
        context: Blender context
        srs_name: Spatial reference system (e.g., "EPSG:25832")
        selected_only: Export only selected objects
        batch_size: Number of objects to process per batch
        enable_progress: Show progress updates
        enable_memory_tracking: Track memory usage (debugging)
        **export_options: Additional export options
    
    Returns:
        Dictionary with export statistics
    """
    start_time = time.time()
    
    # Collect objects to export
    objects = [
        obj for obj in _collect_blender_objects(context, selected_only)
        if object_is_viewport_visible(obj, context)
    ]
    total_objects = len(objects)
    
    if total_objects == 0:
        return {
            'status': 'warning',
            'message': 'No objects to export',
            'objects_exported': 0
        }
    
    # Initialize streaming components
    geometry_buffer = StreamingGeometryBuffer(max_buffer_size_mb=100.0)
    appearance_collector = StreamingAppearanceCollector()
    
    # Setup export context
    export_ctx = _setup_export_context(context, export_options)
    
    objects_exported = 0
    errors = []
    
    # Open streaming writer and process objects
    with StreamingXMLWriter(
        filepath=filepath,
        srs_name=srs_name,
        version="2.0",
        enable_memory_tracking=enable_memory_tracking
    ) as writer:
        
        # Process objects in batches
        for batch_start in range(0, total_objects, batch_size):
            batch_end = min(batch_start + batch_size, total_objects)
            batch = objects[batch_start:batch_end]
            
            if enable_progress:
                progress = (batch_start / total_objects) * 100
                print(f"[CityGML 2.0 Export] Processing batch {batch_start}-{batch_end} / {total_objects} ({progress:.1f}%)")
            
            # Process each object in batch
            for obj in batch:
                try:
                    # Generate feature XML for this object
                    feature_xml = _write_feature_xml(
                        obj=obj,
                        context=export_ctx,
                        srs_name=srs_name,
                        version="2.0"
                    )
                    
                    if feature_xml:
                        # Write to file immediately
                        writer.write_city_object(feature_xml)
                        objects_exported += 1
                        
                        # Collect appearance data
                        appearance_collector.collect_from_object(obj)
                    
                except Exception as e:
                    error_msg = f"Failed to export object '{obj.name}': {str(e)}"
                    errors.append(error_msg)
                    if enable_progress:
                        print(f"[WARNING] {error_msg}")
            
            # Flush geometry buffer after each batch
            geometry_buffer.flush()
        
        # Write appearance data at end
        if enable_progress:
            print("[CityGML 2.0 Export] Writing appearance data...")
        
        appearance_xml = appearance_collector.generate_appearance_xml(version="2.0")
        if appearance_xml:
            writer.write_appearance_member(appearance_xml)
    
    end_time = time.time()
    duration = end_time - start_time
    
    # Gather statistics
    stats = writer.get_stats()
    
    result = {
        'status': 'success' if not errors else 'partial',
        'objects_exported': objects_exported,
        'total_objects': total_objects,
        'duration_seconds': duration,
        'throughput_objects_per_sec': objects_exported / duration if duration > 0 else 0,
        'memory_peak_mb': stats.peak_memory_mb if enable_memory_tracking else None,
        'errors': errors,
        'warnings': []
    }
    
    if enable_progress:
        print(f"\n[CityGML 2.0 Export] Complete!")
        print(f"  Objects Exported: {objects_exported} / {total_objects}")
        print(f"  Duration: {duration:.2f}s ({result['throughput_objects_per_sec']:.1f} objects/s)")
        if enable_memory_tracking and stats.peak_memory_mb:
            print(f"  Peak Memory: {stats.peak_memory_mb:.2f} MB")
        if errors:
            print(f"  Errors: {len(errors)}")
    
    return result


# Convenience function aliases
def export_large_citygml2(filepath: str, context, **kwargs) -> Dict:
    """
    Convenience function for exporting large CityGML 2.0 datasets.
    Automatically enables streaming mode.
    """
    return export_citygml2_streaming(
        filepath=filepath,
        context=context,
        enable_progress=True,
        **kwargs
    )
