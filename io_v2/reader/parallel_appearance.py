# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Parallel Appearance Processing for CityGML 2.0

Mirror of CityGML 3.0 parallel appearance processor for CityGML 2.0.
See io.reader.parallel_appearance for detailed documentation.
"""

import os
import multiprocessing
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Dict, List, Tuple, Optional, Set
from pathlib import Path
import time


class ParallelAppearanceProcessor:
    """
    Manages parallel processing of appearance data (textures, materials).
    
    Identical to CityGML 3.0 version - see io.reader.parallel_appearance
    for full documentation.
    """
    
    def __init__(self, max_workers: int = None):
        """Initialize processor with thread pool (auto-sized based on CPU cores)."""
        # OPTIMIZATION: Auto-size thread pool based on CPU cores
        if max_workers is None:
            try:
                cpu_count = multiprocessing.cpu_count()
                max_workers = min(8, max(2, cpu_count - 1))
                print(f"[ParallelAppearance CityGML 2.0] Auto-sized thread pool: {max_workers} workers ({cpu_count} CPU cores)")
            except Exception:
                max_workers = 4
                print(f"[ParallelAppearance CityGML 2.0] Using fallback thread pool: {max_workers} workers")
        
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
        """Preload textures in parallel using thread pool."""
        if not texture_paths:
            return {}
        
        start_time = time.time()
        results = {}
        
        paths_list = list(texture_paths)
        
        if self.max_workers <= 1:
            for path in paths_list:
                resolved = self._resolve_and_validate_path(path, base_path)
                if resolved:
                    results[resolved] = True
                    self.stats["textures_preloaded"] += 1
            return results
        
        num_batches = (len(paths_list) + batch_size - 1) // batch_size
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            for batch_idx in range(num_batches):
                start_idx = batch_idx * batch_size
                end_idx = min(start_idx + batch_size, len(paths_list))
                batch = paths_list[start_idx:end_idx]
                
                future_to_path = {
                    executor.submit(
                        self._resolve_and_validate_path, 
                        path, 
                        base_path
                    ): path 
                    for path in batch
                }
                
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
        """Resolve and validate texture path (thread-safe)."""
        if not texture_path:
            return None
        
        cache_key = f"{base_path}::{texture_path}"
        with self.cache_lock:
            if cache_key in self.image_cache:
                self.stats["cache_hits"] += 1
                return self.image_cache[cache_key]
        
        self.stats["cache_misses"] += 1
        
        try:
            if os.path.isabs(texture_path):
                resolved = texture_path
            else:
                resolved = os.path.join(os.path.dirname(base_path), texture_path)
            
            resolved = os.path.normpath(resolved)
            
            if not os.path.exists(resolved):
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
                    with self.cache_lock:
                        self.image_cache[cache_key] = None
                    return None
            
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
        """Prepare material specifications in batches."""
        if not material_specs:
            return []
        
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
        
        prepared = []
        
        for tex_path, specs in by_texture.items():
            prepared.extend(specs)
        
        prepared.extend(no_texture)
        
        return prepared
    
    def get_stats(self) -> Dict:
        """Get processing statistics."""
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
    """Collect all unique texture paths from appearance data."""
    textures = set()
    
    for data in ptex_by_ring.values():
        img = data.get("image")
        if img:
            textures.add(img)
    
    for data in ptex_by_poly.values():
        img = data.get("image")
        if img:
            textures.add(img)
    
    for img in poly_to_image.values():
        if img:
            textures.add(img)
    
    return textures


def get_parallel_stats_summary(processor: ParallelAppearanceProcessor) -> str:
    """Get formatted statistics summary."""
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


_global_processor: Optional[ParallelAppearanceProcessor] = None


def get_global_processor(max_workers: int = 4) -> ParallelAppearanceProcessor:
    """Get or create global processor instance."""
    global _global_processor
    if _global_processor is None:
        _global_processor = ParallelAppearanceProcessor(max_workers=max_workers)
    return _global_processor


def reset_global_processor():
    """Reset global processor."""
    global _global_processor
    if _global_processor:
        _global_processor.clear_cache()
    _global_processor = None
