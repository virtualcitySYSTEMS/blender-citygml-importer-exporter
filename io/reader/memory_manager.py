# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Consolidated: shared implementation in shared/memory_cache.py
"""
Memory Management System for CityGML Import - Re-export from shared.
"""
from ...shared.memory_cache import (  # noqa: F401
    LRUGeometryCache,
    MemoryMonitor,
    get_available_memory_mb,
    auto_tune_batch_size,
    cleanup_xml_element,
    trigger_garbage_collection,
    get_memory_stats_summary,
)
