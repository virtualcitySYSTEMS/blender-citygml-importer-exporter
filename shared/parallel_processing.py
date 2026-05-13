# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# shared/parallel_processing.py
"""
Optimized Batch Geometry Processing - Shared between CityGML 2.0 and 3.0

Provides batch processing optimizations for geometry extraction.
Focuses on reducing overhead and improving cache efficiency with robust error recovery.
"""

from typing import List, Tuple, Optional, Set, Dict
import logging

try:
    from lxml import etree as ET
except ImportError:
    import xml.etree.ElementTree as ET


# Error tracking for import diagnostics
class ImportErrorTracker:
    """Tracks errors during batch geometry processing for diagnostics."""
    
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.failed_features = {}
        
    def log_error(self, feat_id: str, error_type: str, message: str, exception: Exception = None):
        """Log a feature processing error."""
        error_info = {
            'feature_id': feat_id,
            'error_type': error_type,
            'message': message,
            'exception': str(exception) if exception else None
        }
        self.errors.append(error_info)
        self.failed_features[feat_id] = error_info
        
    def log_warning(self, feat_id: str, warning_type: str, message: str):
        """Log a processing warning."""
        warning_info = {
            'feature_id': feat_id,
            'warning_type': warning_type,
            'message': message
        }
        self.warnings.append(warning_info)
    
    def get_summary(self) -> Dict:
        """Get error summary statistics."""
        return {
            'total_errors': len(self.errors),
            'total_warnings': len(self.warnings),
            'failed_features': len(self.failed_features),
            'error_types': self._count_by_type(self.errors, 'error_type'),
            'warning_types': self._count_by_type(self.warnings, 'warning_type')
        }
    
    def _count_by_type(self, items: List[Dict], key: str) -> Dict[str, int]:
        """Count items by type."""
        counts = {}
        for item in items:
            item_type = item.get(key, 'unknown')
            counts[item_type] = counts.get(item_type, 0) + 1
        return counts


def validate_geometry(verts: List, faces: List[List[int]]) -> Tuple[bool, Optional[str]]:
    """
    Validate geometry before mesh creation.
    
    Args:
        verts: List of vertices
        faces: List of face indices
    
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not verts:
        return False, "No vertices"
    
    if not faces:
        return False, "No faces"
    
    # Check for invalid face indices
    max_vert_idx = len(verts) - 1
    for face_idx, face in enumerate(faces):
        if len(face) < 3:
            return False, f"Face {face_idx} has less than 3 vertices"
        
        for vert_idx in face:
            if vert_idx < 0 or vert_idx > max_vert_idx:
                return False, f"Face {face_idx} references invalid vertex index {vert_idx}"
    
    # Check for degenerate vertices (NaN, Inf)
    for vert_idx, (x, y, z) in enumerate(verts):
        try:
            if not all(isinstance(v, (int, float)) and abs(v) < 1e15 for v in (x, y, z)):
                return False, f"Vertex {vert_idx} has invalid coordinates: ({x}, {y}, {z})"
        except (TypeError, ValueError):
            return False, f"Vertex {vert_idx} has non-numeric coordinates"
    
    return True, None


def batch_extract_geometry(features: List, build_geometry_func, default_srs: str, 
                          ref_origin: Tuple[float, float, float], 
                          error_tracker: Optional[ImportErrorTracker] = None,
                          **kwargs) -> List[Dict]:
    """
    Extract geometry from multiple features in batch.
    
    Args:
        features: List of XML feature elements
        build_geometry_func: Function to build geometry from feature element
        default_srs: Default spatial reference system
        ref_origin: Reference origin for coordinate transformation
        error_tracker: Optional error tracker for diagnostics
        **kwargs: Additional arguments for build_geometry_func
    
    Returns:
        List of geometry dictionaries with metadata
    """
    if error_tracker is None:
        error_tracker = ImportErrorTracker()
    
    results = []
    
    for feat_el in features:
        feat_id = feat_el.get("{http://www.opengis.net/gml}id") or \
                  feat_el.get("{http://www.opengis.net/gml/3.2}id") or \
                  "unknown"
        
        try:
            geom_data = build_geometry_func(feat_el, default_srs, ref_origin, **kwargs)
            
            if geom_data:
                # Validate before adding
                verts = geom_data.get('verts', [])
                faces = geom_data.get('faces', [])
                
                is_valid, error_msg = validate_geometry(verts, faces)
                
                if is_valid:
                    geom_data['feature_id'] = feat_id
                    results.append(geom_data)
                else:
                    error_tracker.log_error(feat_id, 'invalid_geometry', error_msg)
            else:
                error_tracker.log_warning(feat_id, 'no_geometry', 'No geometry extracted')
                
        except Exception as e:
            error_tracker.log_error(feat_id, 'extraction_exception', 
                                   f"Failed to extract geometry: {str(e)}", e)
    
    return results


def filter_features_by_bbox(features: List, bbox: Tuple[float, float, float, float],
                           get_envelope_func) -> List:
    """
    Filter features by bounding box.
    
    Args:
        features: List of XML feature elements
        bbox: Tuple of (minx, miny, maxx, maxy)
        get_envelope_func: Function to get envelope from feature element
    
    Returns:
        Filtered list of features
    """
    minx, miny, maxx, maxy = bbox
    filtered = []
    
    for feat_el in features:
        try:
            envelope = get_envelope_func(feat_el)
            if envelope:
                fminx, fminy, fmaxx, fmaxy = envelope
                # Check for overlap
                if not (fmaxx < minx or fminx > maxx or fmaxy < miny or fminy > maxy):
                    filtered.append(feat_el)
            else:
                # No envelope, include by default
                filtered.append(feat_el)
        except Exception:
            # On error, include feature
            filtered.append(feat_el)
    
    return filtered


def optimize_batch_size(available_memory_mb: float, 
                       avg_feature_size_mb: float = 0.5,
                       min_batch: int = 10,
                       max_batch: int = 1000) -> int:
    """
    Calculate optimal batch size based on available memory.
    
    Args:
        available_memory_mb: Available memory in megabytes
        avg_feature_size_mb: Average feature size in memory
        min_batch: Minimum batch size
        max_batch: Maximum batch size
    
    Returns:
        Optimal batch size
    """
    # Reserve 20% of memory for overhead
    usable_memory = available_memory_mb * 0.8
    
    # Calculate batch size
    calculated_batch = int(usable_memory / avg_feature_size_mb) if avg_feature_size_mb > 0 else max_batch
    
    # Clamp to min/max
    return max(min_batch, min(calculated_batch, max_batch))


def estimate_optimal_batch_size(feature_count: int, avg_feature_complexity: int = 50) -> int:
    """
    Estimate optimal batch size based on feature count and complexity.

    Args:
        feature_count: Total number of features
        avg_feature_complexity: Average polygons per feature (rough estimate)

    Returns:
        Recommended batch size
    """
    if feature_count < 100:
        return 25
    elif feature_count < 1000:
        return 50
    else:
        return 100
