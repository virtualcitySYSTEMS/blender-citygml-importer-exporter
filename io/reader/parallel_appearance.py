# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Parallel Appearance Processing for CityGML 3.0

Provides multi-threaded texture loading and material preparation to improve
performance for files with many textures and materials.

Key Features:
- Thread pool for concurrent texture loading (I/O-bound)
- Batch processing of materials
- Thread-safe caching for loaded images
- Progress tracking and statistics
- Fallback to sequential processing on errors

Thread Safety:
- Image loading is thread-safe (read-only file operations)
- Material creation MUST happen on main thread (Blender limitation)
- Uses locks for shared cache access
"""

import os
import multiprocessing
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Dict, List, Tuple, Optional, Set
from pathlib import Path
import time

# Thread-safe cache for loaded images
_image_cache: Dict[str, Optional[str]] = {}
_image_cache_lock = Lock()

# Statistics
_stats = {
    "textures_loaded": 0,
    "cache_hits": 0,
    "cache_misses": 0,
    "parallel_batches": 0,
    "total_time": 0.0,
    "thread_pool_time": 0.0,
}


class ParallelAppearanceProcessor:
    """
    Manages parallel processing of appearance data (textures, materials).
    
    The processor separates I/O-bound operations (texture loading) from
    Blender API operations (material creation) to maximize performance.
    """
    
    def __init__(self, max_workers: int = None):
        """
        Initialize processor with thread pool.
        
        Args:
            max_workers: Maximum number of worker threads (default: auto-detect from CPU cores)
                        Set to 1 to disable parallelization for debugging.
        """
        # OPTIMIZATION: Auto-size thread pool based on CPU cores
        # I/O-bound operations benefit from more threads than CPU cores
        # Limit to 8 to avoid excessive thread overhead
        if max_workers is None:
            try:
                cpu_count = multiprocessing.cpu_count()
                max_workers = min(8, max(2, cpu_count - 1))
                print(f"[ParallelAppearance] Auto-sized thread pool: {max_workers} workers ({cpu_count} CPU cores)")
            except Exception:
                max_workers = 4  # Fallback
                print(f"[ParallelAppearance] Using fallback thread pool: {max_workers} workers")
        
        self.max_workers = max_workers
        self.image_cache = {}
        self.cache_lock = Lock()
        self.stats = {
            "textures_preloaded": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "batches_processed": 0,
            "total_load_time": 0.0,
        }
    
    def preload_textures(
        self, 
        texture_paths: Set[str], 
        base_path: str,
        batch_size: int = 50
    ) -> Dict[str, bool]:
        """
        Preload textures in parallel using thread pool.
        
        This function performs file existence checks and path resolution
        in parallel. The actual Blender image loading happens later on
        the main thread.
        
        Args:
            texture_paths: Set of texture file paths (relative or absolute)
            base_path: Base path for resolving relative paths
            batch_size: Number of textures per batch (default: 50)
        
        Returns:
            Dict mapping resolved paths to existence status (True/False)
        """
        if not texture_paths:
            return {}
        
        start_time = time.time()
        results = {}
        
        # Convert to list for batching
        paths_list = list(texture_paths)
        
        # Disable parallel processing if only 1 worker
        if self.max_workers <= 1:
            for path in paths_list:
                resolved = self._resolve_and_validate_path(path, base_path)
                if resolved:
                    results[resolved] = True
                    self.stats["textures_preloaded"] += 1
            return results
        
        # Process in batches to avoid thread pool overhead
        num_batches = (len(paths_list) + batch_size - 1) // batch_size
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            for batch_idx in range(num_batches):
                start_idx = batch_idx * batch_size
                end_idx = min(start_idx + batch_size, len(paths_list))
                batch = paths_list[start_idx:end_idx]
                
                # Submit batch jobs
                future_to_path = {
                    executor.submit(
                        self._resolve_and_validate_path, 
                        path, 
                        base_path
                    ): path 
                    for path in batch
                }
                
                # Collect results
                for future in as_completed(future_to_path):
                    original_path = future_to_path[future]
                    try:
                        resolved = future.result()
                        if resolved:
                            results[resolved] = True
                            self.stats["textures_preloaded"] += 1
                    except Exception as e:
                        print(f"Warning: Error preloading texture {original_path}: {e}")
                
                self.stats["batches_processed"] += 1
        
        self.stats["total_load_time"] = time.time() - start_time
        return results
    
    def _resolve_and_validate_path(
        self, 
        texture_path: str, 
        base_path: str
    ) -> Optional[str]:
        """
        Resolve and validate texture path (thread-safe).
        
        Args:
            texture_path: Relative or absolute texture path
            base_path: Base path for resolving relative paths
        
        Returns:
            Resolved absolute path if valid, None otherwise
        """
        if not texture_path:
            return None
        
        # Check cache first (with lock)
        cache_key = f"{base_path}::{texture_path}"
        with self.cache_lock:
            if cache_key in self.image_cache:
                self.stats["cache_hits"] += 1
                return self.image_cache[cache_key]
        
        self.stats["cache_misses"] += 1
        
        # Resolve path
        try:
            # Handle absolute paths
            if os.path.isabs(texture_path):
                resolved = texture_path
            else:
                # Relative to GML file
                resolved = os.path.join(os.path.dirname(base_path), texture_path)
            
            # Normalize path
            resolved = os.path.normpath(resolved)
            
            # Validate existence
            if not os.path.exists(resolved):
                # Try common texture directories
                base_dir = os.path.dirname(base_path)
                alternatives = [
                    os.path.join(base_dir, "textures", os.path.basename(texture_path)),
                    os.path.join(base_dir, "appearance", os.path.basename(texture_path)),
                    os.path.join(base_dir, os.path.basename(texture_path)),
                ]
                
                for alt in alternatives:
                    if os.path.exists(alt):
                        resolved = alt
                        break
                else:
                    # File not found
                    with self.cache_lock:
                        self.image_cache[cache_key] = None
                    return None
            
            # Cache result
            with self.cache_lock:
                self.image_cache[cache_key] = resolved
            
            return resolved
        
        except Exception as e:
            print(f"Warning: Error resolving texture path {texture_path}: {e}")
            with self.cache_lock:
                self.image_cache[cache_key] = None
            return None
    
    def batch_prepare_materials(
        self,
        material_specs: List[Dict],
        batch_size: int = 100
    ) -> List[Dict]:
        """
        Prepare material specifications in batches.
        
        This function organizes material data for efficient creation
        on the main thread. It groups materials by texture to maximize
        Blender's material reuse.
        
        Args:
            material_specs: List of material specifications
                           Each dict should have: name, texture_path, color
            batch_size: Number of materials per batch (default: 100)
        
        Returns:
            List of prepared material specs (grouped and optimized)
        """
        if not material_specs:
            return []
        
        # Group materials by texture
        by_texture = {}
        no_texture = []
        
        for spec in material_specs:
            tex_path = spec.get("texture_path")
            if tex_path:
                if tex_path not in by_texture:
                    by_texture[tex_path] = []
                by_texture[tex_path].append(spec)
            else:
                no_texture.append(spec)
        
        # Build optimized list: textured materials first (grouped), then colors
        prepared = []
        
        # Add textured materials (grouped for cache efficiency)
        for tex_path, specs in by_texture.items():
            prepared.extend(specs)
        
        # Add color-only materials
        prepared.extend(no_texture)
        
        return prepared
    
    def get_stats(self) -> Dict:
        """
        Get processing statistics.
        
        Returns:
            Dict with statistics:
            - textures_preloaded: Number of textures successfully preloaded
            - cache_hits/misses: Cache performance
            - batches_processed: Number of batches processed
            - total_load_time: Total preloading time in seconds
            - hit_rate: Cache hit rate percentage
        """
        total_accesses = self.stats["cache_hits"] + self.stats["cache_misses"]
        hit_rate = (
            100.0 * self.stats["cache_hits"] / total_accesses 
            if total_accesses > 0 
            else 0.0
        )
        
        return {
            **self.stats,
            "hit_rate": hit_rate,
        }
    
    def clear_cache(self):
        """Clear image cache and reset statistics."""
        with self.cache_lock:
            self.image_cache.clear()
        
        self.stats = {
            "textures_preloaded": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "batches_processed": 0,
            "total_load_time": 0.0,
        }


def collect_texture_paths(
    ptex_by_ring: Dict,
    ptex_by_poly: Dict,
    poly_to_image: Dict
) -> Set[str]:
    """
    Collect all unique texture paths from appearance data.
    
    Args:
        ptex_by_ring: Ring-based parameterized textures
        ptex_by_poly: Polygon-based parameterized textures
        poly_to_image: Polygon to image mapping
    
    Returns:
        Set of unique texture paths
    """
    textures = set()
    
    # From ring-based textures
    for data in ptex_by_ring.values():
        img = data.get("image")
        if img:
            textures.add(img)
    
    # From polygon-based textures
    for data in ptex_by_poly.values():
        img = data.get("image")
        if img:
            textures.add(img)
    
    # From polygon mappings
    for img in poly_to_image.values():
        if img:
            textures.add(img)
    
    return textures


def get_parallel_stats_summary(processor: ParallelAppearanceProcessor) -> str:
    """
    Get formatted statistics summary for parallel appearance processing.
    
    Args:
        processor: ParallelAppearanceProcessor instance
    
    Returns:
        Formatted statistics string
    """
    stats = processor.get_stats()
    
    lines = [
        "=== Parallel Appearance Statistics ===",
        f"Textures Preloaded: {stats['textures_preloaded']}",
        f"Batches Processed: {stats['batches_processed']}",
        f"",
        f"Cache Performance:",
        f"  Hits: {stats['cache_hits']}",
        f"  Misses: {stats['cache_misses']}",
        f"  Hit Rate: {stats['hit_rate']:.1f}%",
        f"",
        f"Performance:",
        f"  Total Load Time: {stats['total_load_time']:.3f}s",
        "=" * 38,
    ]
    
    return "\n".join(lines)


# Module-level convenience functions using global processor
_global_processor: Optional[ParallelAppearanceProcessor] = None


def get_global_processor(max_workers: int = 4) -> ParallelAppearanceProcessor:
    """
    Get or create global processor instance.
    
    Args:
        max_workers: Maximum worker threads (default: 4)
    
    Returns:
        Global ParallelAppearanceProcessor instance
    """
    global _global_processor
    if _global_processor is None:
        _global_processor = ParallelAppearanceProcessor(max_workers=max_workers)
    return _global_processor


def reset_global_processor():
    """Reset global processor (useful for testing)."""
    global _global_processor
    if _global_processor:
        _global_processor.clear_cache()
    _global_processor = None
