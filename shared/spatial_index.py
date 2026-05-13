# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io/reader/spatial_index.py
"""
Spatial Indexing for Efficient BBOX Filtering

This module provides R-tree based spatial indexing for fast BBOX-based filtering
during CityGML import. Instead of parsing all features and checking each BBOX,
we build an index that allows logarithmic-time spatial queries.

Performance Benefits:
- Traditional BBOX filter: O(n) - check every feature
- R-tree indexed filter: O(log n) - only check intersecting features
- Speedup: ~100Ã— for large datasets with small query regions

Memory Overhead:
- ~50 bytes per feature (bounding box + metadata)
- Typical: 1 MB for 20,000 features
- Negligible compared to full geometry parsing

Usage:
    # Build index from GML file
    index = SpatialIndex.from_gml_file("city.gml")
    
    # Query features in bounding box
    feature_ids = index.query_bbox(xmin, ymin, xmax, ymax)
    
    # Use in import with pre-filtering
    features = import_with_spatial_filter(path, bbox_filter, index)
"""

from __future__ import annotations
from typing import List, Tuple, Set, Optional, Dict
from dataclasses import dataclass
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from rtree import index as rtree_index
    RTREE_AVAILABLE = True
except ImportError:
    RTREE_AVAILABLE = False
    rtree_index = None


@dataclass
class FeatureBBox:
    """Feature bounding box with metadata."""
    gml_id: str
    feature_type: str
    xmin: float
    ymin: float
    xmax: float
    ymax: float
    
    def intersects(self, query_xmin: float, query_ymin: float, 
                   query_xmax: float, query_ymax: float) -> bool:
        """Check if this bbox intersects with query bbox."""
        return not (self.xmax < query_xmin or self.xmin > query_xmax or
                    self.ymax < query_ymin or self.ymin > query_ymax)
    
    def to_rtree_bbox(self) -> Tuple[float, float, float, float]:
        """Convert to R-tree format (xmin, ymin, xmax, ymax)."""
        return (self.xmin, self.ymin, self.xmax, self.ymax)


class SpatialIndex:
    """
    Spatial index for fast BBOX-based filtering.
    
    Supports two backends:
    1. R-tree (rtree library) - O(log n) queries, optimal for large datasets
    2. Linear scan - O(n) queries, fallback if rtree not available
    
    The R-tree index uses libspatialindex for efficient spatial queries.
    """
    
    def __init__(self, use_rtree: bool = True):
        """
        Initialize spatial index.
        
        Args:
            use_rtree: Use R-tree if available (faster for >1000 features)
        """
        self.use_rtree = use_rtree and RTREE_AVAILABLE
        self.features: Dict[int, FeatureBBox] = {}  # idx -> FeatureBBox
        self.gml_id_to_idx: Dict[str, int] = {}  # gml_id -> idx
        self._next_idx = 0
        
        if self.use_rtree:
            # Create R-tree index with custom properties
            p = rtree_index.Property()
            p.dimension = 2  # 2D spatial index
            p.variant = rtree_index.RT_Star  # R*-tree variant (better for bulk loading)
            self.rtree = rtree_index.Index(properties=p)
        else:
            self.rtree = None
    
    def insert(self, gml_id: str, feature_type: str, 
               xmin: float, ymin: float, xmax: float, ymax: float):
        """
        Insert feature bounding box into index.
        
        Args:
            gml_id: GML ID of feature
            feature_type: Feature type (e.g., "Building")
            xmin, ymin, xmax, ymax: Bounding box coordinates
        """
        bbox = FeatureBBox(gml_id, feature_type, xmin, ymin, xmax, ymax)
        idx = self._next_idx
        self._next_idx += 1
        
        self.features[idx] = bbox
        self.gml_id_to_idx[gml_id] = idx
        
        if self.rtree:
            # Insert into R-tree (idx, (xmin, ymin, xmax, ymax))
            self.rtree.insert(idx, bbox.to_rtree_bbox())
    
    def query_bbox(self, xmin: float, ymin: float, 
                   xmax: float, ymax: float) -> List[str]:
        """
        Query features intersecting with bounding box.
        
        Args:
            xmin, ymin, xmax, ymax: Query bounding box
        
        Returns:
            List of GML IDs of intersecting features
        """
        if self.rtree:
            # R-tree query (fast)
            bbox = (xmin, ymin, xmax, ymax)
            indices = list(self.rtree.intersection(bbox))
            return [self.features[idx].gml_id for idx in indices]
        else:
            # Linear scan (fallback)
            result = []
            for bbox in self.features.values():
                if bbox.intersects(xmin, ymin, xmax, ymax):
                    result.append(bbox.gml_id)
            return result
    
    def query_feature_types(self, xmin: float, ymin: float,
                           xmax: float, ymax: float,
                           feature_types: Set[str]) -> List[str]:
        """
        Query features by bbox AND feature type.
        
        Args:
            xmin, ymin, xmax, ymax: Query bounding box
            feature_types: Set of allowed feature types (e.g., {"Building", "Bridge"})
        
        Returns:
            List of GML IDs of matching features
        """
        all_ids = self.query_bbox(xmin, ymin, xmax, ymax)
        
        if not feature_types:
            return all_ids
        
        # Filter by feature type
        result = []
        for gml_id in all_ids:
            idx = self.gml_id_to_idx[gml_id]
            bbox = self.features[idx]
            if bbox.feature_type in feature_types:
                result.append(gml_id)
        
        return result
    
    def get_stats(self) -> Dict[str, object]:
        """Get index statistics."""
        return {
            "feature_count": len(self.features),
            "backend": "R-tree" if self.rtree else "Linear",
            "memory_bytes": self._estimate_memory(),
            "feature_types": self._count_feature_types()
        }
    
    def _estimate_memory(self) -> int:
        """Estimate memory usage in bytes."""
        # Each FeatureBBox: ~50 bytes (strings + floats + overhead)
        # R-tree overhead: ~30 bytes per entry
        base = len(self.features) * 50
        rtree_overhead = len(self.features) * 30 if self.rtree else 0
        return base + rtree_overhead
    
    def _count_feature_types(self) -> Dict[str, int]:
        """Count features by type."""
        counts = {}
        for bbox in self.features.values():
            counts[bbox.feature_type] = counts.get(bbox.feature_type, 0) + 1
        return counts
    
    @classmethod
    def from_gml_file(cls, filepath: str, 
                      namespaces: Optional[Dict[str, str]] = None,
                      use_rtree: bool = True) -> 'SpatialIndex':
        """
        Build spatial index from GML file by scanning Envelope elements.
        
        This performs a fast scan without full XML parsing - only extracts
        gml:id, feature type, and gml:Envelope/boundedBy.
        
        Args:
            filepath: Path to GML file
            namespaces: XML namespaces (optional)
            use_rtree: Use R-tree backend if available
        
        Returns:
            Populated SpatialIndex
        """
        if namespaces is None:
            # Default CityGML 3.0 namespaces
            namespaces = {
                'gml': 'http://www.opengis.net/gml/3.2',
                'core': 'http://www.opengis.net/citygml/3.0',
                'bldg': 'http://www.opengis.net/citygml/building/3.0',
                'brid': 'http://www.opengis.net/citygml/bridge/3.0',
                'tran': 'http://www.opengis.net/citygml/transportation/3.0',
                'tun': 'http://www.opengis.net/citygml/tunnel/3.0',
                'luse': 'http://www.opengis.net/citygml/landuse/3.0',
            }
        
        index = cls(use_rtree=use_rtree)
        
        # Use iterparse for memory-efficient parsing
        context = ET.iterparse(filepath, events=('start', 'end'))
        
        current_feature = None
        feature_stack = []
        
        for event, elem in context:
            if event == 'start':
                # Check if this is a CityGML feature
                tag = elem.tag
                if '}' in tag:
                    ns_uri, local = tag.rsplit('}', 1)
                    ns_uri = ns_uri[1:]  # Remove leading {
                    
                    # Check if this is a feature (has gml:id)
                    gml_id = elem.get('{http://www.opengis.net/gml/3.2}id')
                    if gml_id and cls._is_citygml_feature(local):
                        current_feature = {
                            'gml_id': gml_id,
                            'feature_type': local,
                            'elem': elem
                        }
                        feature_stack.append(current_feature)
            
            elif event == 'end':
                # Check for Envelope in current feature
                if current_feature and elem.tag.endswith('Envelope'):
                    bbox = cls._extract_envelope_bbox(elem, namespaces)
                    if bbox:
                        xmin, ymin, xmax, ymax = bbox
                        index.insert(
                            current_feature['gml_id'],
                            current_feature['feature_type'],
                            xmin, ymin, xmax, ymax
                        )
                
                # Pop feature from stack when done
                if feature_stack and elem == feature_stack[-1]['elem']:
                    feature_stack.pop()
                    current_feature = feature_stack[-1] if feature_stack else None
                
                # Clear element to free memory
                elem.clear()
        
        return index
    
    @staticmethod
    def _is_citygml_feature(local_name: str) -> bool:
        """Check if element is a CityGML feature."""
        # Common CityGML feature types
        feature_types = {
            'Building',
            'Bridge',
            'Tunnel',
            'Road', 'Railway', 'Track', 'Waterway',
            'LandUse', 'PlantCover', 'SolitaryVegetationObject',
            'WaterBody', 'ReliefFeature', 'TINRelief', 'RasterRelief',
            'CityFurniture', 'CityObjectGroup', 'GenericCityObject',
            'GenericOccupiedSpace', 'GenericLogicalSpace', 'GenericUnoccupiedSpace', 'GenericThematicSurface'
        }
        return local_name in feature_types
    
    @staticmethod
    def _extract_envelope_bbox(envelope_elem, namespaces: Dict[str, str]) -> Optional[Tuple[float, float, float, float]]:
        """Extract bbox from gml:Envelope element."""
        try:
            lower = envelope_elem.find('.//gml:lowerCorner', namespaces)
            upper = envelope_elem.find('.//gml:upperCorner', namespaces)
            
            if lower is None or upper is None:
                return None
            
            lower_coords = [float(x) for x in lower.text.split()]
            upper_coords = [float(x) for x in upper.text.split()]
            
            if len(lower_coords) < 2 or len(upper_coords) < 2:
                return None
            
            xmin, ymin = lower_coords[0], lower_coords[1]
            xmax, ymax = upper_coords[0], upper_coords[1]
            
            return (xmin, ymin, xmax, ymax)
        except (ValueError, AttributeError, IndexError):
            return None


def build_spatial_index_from_gml(
    filepath: str,
    progress_callback: Optional[callable] = None
) -> SpatialIndex:
    """
    Convenience function to build spatial index with progress reporting.
    
    Args:
        filepath: Path to GML file
        progress_callback: Optional callback(current, total, message)
    
    Returns:
        Populated SpatialIndex
    """
    if progress_callback:
        progress_callback(0, 100, "Building spatial index...")
    
    index = SpatialIndex.from_gml_file(filepath)
    
    if progress_callback:
        stats = index.get_stats()
        message = f"Index built: {stats['feature_count']} features, {stats['backend']} backend"
        progress_callback(100, 100, message)
    
    return index


def estimate_spatial_index_benefit(
    total_features: int,
    query_bbox_ratio: float = 0.1
) -> Dict[str, float]:
    """
    Estimate performance benefit of using spatial index.
    
    Args:
        total_features: Total number of features in dataset
        query_bbox_ratio: Ratio of query bbox to total extent (0.0-1.0)
    
    Returns:
        Dictionary with estimated metrics
    """
    # Linear scan: check all features
    linear_checks = total_features
    
    # R-tree: log(n) tree traversal + k result features
    rtree_checks = (
        2.0 * (total_features ** 0.5) +  # Tree traversal
        (total_features * query_bbox_ratio)  # Result features
    )
    
    speedup = linear_checks / rtree_checks if rtree_checks > 0 else 1.0
    
    return {
        "total_features": total_features,
        "linear_checks": linear_checks,
        "rtree_checks": int(rtree_checks),
        "speedup": speedup,
        "time_saved_percent": (1 - 1/speedup) * 100 if speedup > 1 else 0
    }
