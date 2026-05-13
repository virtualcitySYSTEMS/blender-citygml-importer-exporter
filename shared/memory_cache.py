# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# shared/memory_cache.py
"""
Memory Management System - Shared between CityGML 2.0 and 3.0
==============================================================

Provides intelligent memory management for large CityGML imports:
- LRU Cache for geometry deduplication with size limits
- Memory monitoring and statistics
- Automatic batch size tuning based on available RAM
- XML element cleanup to prevent memory leaks
"""

from typing import Dict, Tuple, Optional, Any
from collections import OrderedDict
import sys
import gc


class LRUGeometryCache:
    """
    LRU (Least Recently Used) cache for geometry deduplication.
    
    Limits both the number of cached meshes and total memory usage.
    Automatically evicts least recently used entries when limits are exceeded.
    """
    
    def __init__(self, max_entries: int = 1000, max_memory_mb: float = 500.0):
        """
        Initialize LRU geometry cache.
        
        Args:
            max_entries: Maximum number of cached mesh geometries
            max_memory_mb: Maximum memory usage in megabytes
        """
        self.max_entries = max_entries
        self.max_memory_bytes = int(max_memory_mb * 1024 * 1024)
        self.cache: OrderedDict[str, Any] = OrderedDict()
        self.sizes: Dict[str, int] = {}  # Hash -> estimated size in bytes
        self.total_size = 0
        
        # Statistics
        self.hits = 0
        self.misses = 0
        self.evictions = 0
    
    def get(self, key: str) -> Optional[Any]:
        """
        Get cached mesh by hash, updating LRU order.
        
        Args:
            key: Geometry hash
            
        Returns:
            Cached mesh_data or None if not found
        """
        if key not in self.cache:
            self.misses += 1
            return None
        
        self.hits += 1
        # Move to end (most recently used)
        self.cache.move_to_end(key)
        return self.cache[key]
    
    def put(self, key: str, mesh_data: Any, estimated_size: int = None):
        """
        Add mesh to cache, evicting old entries if necessary.
        
        Args:
            key: Geometry hash
            mesh_data: Blender mesh data
            estimated_size: Estimated memory size in bytes (auto-calculated if None)
        """
        # Estimate size if not provided
        if estimated_size is None:
            estimated_size = self._estimate_mesh_size(mesh_data)
        
        # Remove existing entry if present
        if key in self.cache:
            self.total_size -= self.sizes[key]
            del self.cache[key]
            del self.sizes[key]
        
        # Add new entry
        self.cache[key] = mesh_data
        self.sizes[key] = estimated_size
        self.total_size += estimated_size
        
        # Evict entries if over limits
        self._evict_if_needed()
    
    def _evict_if_needed(self):
        """Evict least recently used entries until within limits."""
        while self.cache and (
            len(self.cache) > self.max_entries or 
            self.total_size > self.max_memory_bytes
        ):
            # Remove oldest (first) entry
            key, mesh_data = self.cache.popitem(last=False)
            self.total_size -= self.sizes[key]
            del self.sizes[key]
            self.evictions += 1
    
    def _estimate_mesh_size(self, mesh_data) -> int:
        """
        Estimate memory size of a mesh in bytes.
        
        Rough approximation:
        - Each vertex: 12 bytes (3 floats)
        - Each polygon: 8 bytes average (2 ints)
        - Overhead: 1KB
        """
        try:
            vert_count = len(mesh_data.vertices)
            poly_count = len(mesh_data.polygons)
            return vert_count * 12 + poly_count * 8 + 1024
        except (AttributeError, TypeError):
            return 10240  # Default 10KB
    
    def clear(self):
        """Clear all cached entries."""
        self.cache.clear()
        self.sizes.clear()
        self.total_size = 0
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total_requests = self.hits + self.misses
        hit_rate = (self.hits / total_requests * 100) if total_requests > 0 else 0
        
        return {
            'entries': len(self.cache),
            'total_size_mb': self.total_size / (1024 * 1024),
            'max_entries': self.max_entries,
            'max_size_mb': self.max_memory_bytes / (1024 * 1024),
            'hits': self.hits,
            'misses': self.misses,
            'evictions': self.evictions,
            'hit_rate': hit_rate,
            'avg_entry_size_kb': (self.total_size / len(self.cache) / 1024) if self.cache else 0
        }


class MemoryMonitor:
    """Monitor memory usage during import."""
    
    def __init__(self):
        self.peak_memory = 0
        self.initial_memory = self._get_memory_usage()
    
    def _get_memory_usage(self) -> int:
        """Get current memory usage in bytes."""
        try:
            import psutil
            process = psutil.Process()
            return process.memory_info().rss
        except (ImportError, Exception):
            # Fallback: use sys.getsizeof (less accurate)
            return 0
    
    def update(self):
        """Update peak memory tracking."""
        current = self._get_memory_usage()
        if current > self.peak_memory:
            self.peak_memory = current
    
    def get_stats(self) -> Dict[str, float]:
        """Get memory statistics in MB."""
        current = self._get_memory_usage()
        return {
            'initial_mb': self.initial_memory / (1024 * 1024),
            'current_mb': current / (1024 * 1024),
            'peak_mb': self.peak_memory / (1024 * 1024),
            'delta_mb': (current - self.initial_memory) / (1024 * 1024)
        }


def get_available_memory_mb() -> float:
    """
    Get available system memory in MB.
    
    Returns:
        Available memory in megabytes, or 4096 if cannot determine
    """
    try:
        import psutil
        mem = psutil.virtual_memory()
        return mem.available / (1024 * 1024)
    except (ImportError, Exception):
        # Conservative default: 4GB
        return 4096.0


def auto_tune_batch_size(feature_count: int, available_memory_mb: float = None) -> int:
    """
    Automatically determine optimal batch size based on available memory.
    
    Strategy:
    - More memory → larger batches for better throughput
    - Less memory → smaller batches to avoid OOM
    - Feature count → smaller batches for small imports (better progress)
    
    Args:
        feature_count: Total number of features
        available_memory_mb: Available memory in MB (auto-detected if None)
    
    Returns:
        Recommended batch size
    """
    if available_memory_mb is None:
        available_memory_mb = get_available_memory_mb()
    
    # Base batch size on feature count
    if feature_count < 100:
        base_size = 25
    elif feature_count < 1000:
        base_size = 50
    else:
        base_size = 100
    
    # Adjust based on available memory
    if available_memory_mb < 1024:  # < 1GB
        # Very constrained - reduce batch size
        multiplier = 0.5
    elif available_memory_mb < 2048:  # 1-2GB
        # Somewhat constrained - keep base size
        multiplier = 1.0
    elif available_memory_mb < 4096:  # 2-4GB
        # Good amount - slightly increase
        multiplier = 1.5
    else:  # > 4GB
        # Plenty of memory - larger batches
        multiplier = 2.0
    
    tuned_size = int(base_size * multiplier)
    
    # Clamp to reasonable range
    return max(10, min(tuned_size, 200))


def cleanup_xml_element(element):
    """
    Clean up XML element to free memory.
    
    Critical for iterparse to prevent memory accumulation.
    Clears element text, tail, attributes, and children.
    
    Args:
        element: XML element to clean up
    """
    if element is None:
        return
    
    try:
        # Clear element data
        element.clear()
        
        # Remove from parent to break reference
        while element.getprevious() is not None:
            try:
                del element.getparent()[0]
            except (AttributeError, TypeError):
                break
    except (AttributeError, TypeError):
        pass


def trigger_garbage_collection():
    """
    Trigger garbage collection to free unused memory.
    
    Should be called periodically during long imports.
    """
    gc.collect()


def get_memory_stats_summary(cache: LRUGeometryCache, monitor: MemoryMonitor) -> str:
    """
    Generate human-readable memory statistics summary.
    
    Args:
        cache: LRU geometry cache instance
        monitor: Memory monitor instance
    
    Returns:
        Formatted statistics string
    """
    cache_stats = cache.get_stats()
    mem_stats = monitor.get_stats()
    
    summary = f"""
=== Memory Statistics ===
Cache:
  Entries: {cache_stats['entries']}/{cache_stats['max_entries']}
  Size: {cache_stats['total_size_mb']:.1f} MB / {cache_stats['max_size_mb']:.1f} MB
  Hit Rate: {cache_stats['hit_rate']:.1f}%
  Hits: {cache_stats['hits']}, Misses: {cache_stats['misses']}, Evictions: {cache_stats['evictions']}
  Avg Entry: {cache_stats['avg_entry_size_kb']:.1f} KB

Memory:
  Initial: {mem_stats['initial_mb']:.1f} MB
  Current: {mem_stats['current_mb']:.1f} MB
  Peak: {mem_stats['peak_mb']:.1f} MB
  Delta: {mem_stats['delta_mb']:.1f} MB
=========================
"""
    return summary.strip()
