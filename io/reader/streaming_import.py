# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Streaming Import for CityGML 3.0 using iterparse.

This module provides memory-efficient import for very large CityGML files (>500 MB)
by processing features incrementally instead of loading the entire XML tree into memory.

Key advantages:
- ~70-90% less memory usage for large files
- Progressive processing (can start creating objects before full file is read)
- Better progress tracking (more granular updates)

Limitations:
- Slightly slower for small files due to event-based parsing overhead
- XLink resolution between features requires two-pass approach
"""

try:
    from lxml import etree as ET
    HAS_LXML = True
except ImportError:
    import xml.etree.ElementTree as ET
    HAS_LXML = False

import bpy
from pathlib import Path
from typing import Optional, Set, Tuple, Dict, List

from .namespaces import NS
from .xml_utils import localname


def is_streaming_available() -> bool:
    """Check if streaming import is available (requires lxml)."""
    return HAS_LXML and hasattr(ET, 'iterparse')


def get_root_attributes_streaming(path: str) -> Dict[str, str]:
    """
    Extract root element attributes without loading entire file.
    
    Returns dict with keys: srsName, schemaLocation, envelope_srs, lower_corner
    """
    if not is_streaming_available():
        return {}
    
    result = {
        'srsName': None,
        'schemaLocation': None,
        'envelope_srs': None,
        'lower_corner': None,
    }
    
    try:
        for event, elem in ET.iterparse(path, events=('start',)):
            # Only process root element
            if localname(elem.tag) == 'CityModel':
                result['srsName'] = elem.get('srsName')
                result['schemaLocation'] = elem.get('{http://www.w3.org/2001/XMLSchema-instance}schemaLocation')
                
                # Extract envelope
                env = elem.find('.//gml:boundedBy/gml:Envelope', NS)
                if env is not None:
                    result['envelope_srs'] = env.get('srsName')
                    lower = env.findtext('./gml:lowerCorner', namespaces=NS)
                    if lower:
                        result['lower_corner'] = lower.strip()
                
                break  # Only need root element
            
    except Exception:
        pass
    
    return result


def count_features_streaming(path: str, gml_id_filter: Optional[Set[str]] = None) -> int:
    """
    Count total features in file without loading full tree.
    
    Args:
        path: Path to CityGML file
        gml_id_filter: Optional set of GML IDs to filter
    
    Returns:
        Number of features that would be imported
    """
    if not is_streaming_available():
        return 0
    
    count = 0
    gml_id_attr = f"{{{NS['gml']}}}id"
    
    # Keep this list in sync with iter_features_streaming() below.
    feature_types = (
        'Building', 'BuildingPart', 'BuildingUnit', 'Storey',
        'BuildingRoom',
        'BuildingConstructiveElement', 'BuildingSubdivision',
        'Bridge', 'BridgePart', 'BridgeRoom',
        'BridgeConstructiveElement',
        'Tunnel', 'TunnelPart', 'HollowSpace',
        'TunnelConstructiveElement',
        'SolitaryVegetationObject', 'PlantCover', 'WaterBody', 'Road', 'Railway',
        'WaterSurface',
        'Track', 'Square', 'Waterway',
        'Section', 'Intersection', 'TrafficSpace', 'AuxiliaryTrafficSpace',
        'Marking', 'Hole', 'ClearanceSpace',
        'CityFurniture', 'LandUse',
        'ReliefFeature', 'TINRelief', 'MassPointRelief', 'BreaklineRelief', 'RasterRelief',
        'CityObjectGroup',
        'GenericOccupiedSpace', 'GenericLogicalSpace', 'GenericUnoccupiedSpace',
        'GenericThematicSurface',
        'PointCloud', 'Dynamizer',
        'Window', 'Door',  # Construction Module
    )
    
    try:
        for event, elem in ET.iterparse(path, events=('end',), tag=None):
            if event != 'end':
                continue
            
            ln = localname(elem.tag)
            if ln not in feature_types:
                elem.clear()
                continue
            
            # Apply GML-ID filter if specified
            if gml_id_filter:
                feat_id = elem.get(gml_id_attr)
                if feat_id not in gml_id_filter:
                    elem.clear()
                    continue
            
            count += 1
            elem.clear()  # Free memory
            
    except Exception:
        pass
    
    return count


def iter_features_streaming(
    path: str,
    gml_id_filter: Optional[Set[str]] = None,
    bbox_filter: Optional[List[float]] = None,
    feature_type_filter: Optional[Set[str]] = None,
    ns_manager = None,
):
    """
    Iterator that yields CityGML features one at a time using iterparse.
    
    This is memory-efficient for large files as it doesn't load the entire
    XML tree into memory.
    
    Args:
        path: Path to CityGML file
        gml_id_filter: Optional set of GML IDs to import
        bbox_filter: Optional bounding box [min_x, min_y, max_x, max_y]
        feature_type_filter: Optional set of feature type names to import
        ns_manager: Optional DynamicNamespaceManager (Note: limited use in streaming mode)
    
    Yields:
        XML elements representing CityGML features
    """
    if not is_streaming_available():
        raise RuntimeError("Streaming import requires lxml")
    
    gml_id_attr = f"{{{NS['gml']}}}id"
    
    # Feature types to extract (top-level features)
    # NOTE: Sub-features like BuildingFurniture, BuildingInstallation are NOT listed here
    # as they should be part of their parent feature (BuildingRoom, Building)
    # NOTE: In streaming mode, we still use a hardcoded list for performance
    # as we can't parse root.nsmap before streaming starts
    feature_types = (
        'Building', 'BuildingPart', 'BuildingUnit', 'Storey',
        'BuildingRoom',  # Contains buildingFurniture, buildingInstallation as sub-features
        'BuildingConstructiveElement', 'BuildingSubdivision',
        'Bridge', 'BridgePart', 'BridgeRoom',  # BridgeFurniture is sub-feature
        'BridgeConstructiveElement',
        'Tunnel', 'TunnelPart', 'HollowSpace',  # TunnelFurniture, TunnelInstallation are sub-features
        'TunnelConstructiveElement',
        'SolitaryVegetationObject', 'PlantCover', 'WaterBody', 'WaterSurface',
        'Road', 'Railway', 'Track', 'Square', 'Waterway',
        'Section', 'Intersection', 'TrafficSpace', 'AuxiliaryTrafficSpace',
        'Marking', 'Hole', 'ClearanceSpace',
        'CityFurniture', 'LandUse', 'ReliefFeature', 'TINRelief', 'MassPointRelief',
        'BreaklineRelief', 'RasterRelief', 'CityObjectGroup',
        'GenericOccupiedSpace', 'GenericLogicalSpace', 'GenericUnoccupiedSpace',
        'GenericThematicSurface', 'PointCloud', 'Dynamizer',
        'Window', 'Door',  # Construction Module (CityGML 3.0)
    )
    
    try:
        # Use iterparse with 'end' events to process complete elements
        context = ET.iterparse(path, events=('end',), huge_tree=True)
        
        for event, elem in context:
            if event != 'end':
                continue
            
            ln = localname(elem.tag)
            
            # IMPORTANT:
            # In streaming mode the parent feature still needs its child nodes when the
            # feature end tag is reached. Clearing arbitrary non-feature nodes here drops
            # semantic/attribute content such as bldg:roofType or con:height before the
            # importer can store them as custom properties.
            if ln not in feature_types:
                continue
            
            # Apply feature type filter
            if feature_type_filter and ln not in feature_type_filter:
                elem.clear()
                continue
            
            # Apply GML-ID filter
            if gml_id_filter:
                feat_id = elem.get(gml_id_attr)
                if feat_id not in gml_id_filter:
                    elem.clear()
                    continue
            
            # Apply BBOX filter
            if bbox_filter:
                if not _feature_in_bbox(elem, bbox_filter):
                    elem.clear()
                    continue
            
            # IMPORTANT:
            # Do not clear the element here. The caller (importer) needs the full element
            # (including genericAttribute blocks) to extract and store custom properties.
            # Clearing here causes loss of attributes before _store_common_attributes runs.
            yield elem
            
            # Also clear parent references to allow GC
            while elem.getprevious() is not None:
                del elem.getparent()[0]
    
    except Exception as e:
        raise RuntimeError(f"Streaming import failed: {e}")


def _bbox_intersects(candidate_bbox: List[float], query_bbox: List[float]) -> bool:
    """Return True when two 2D bounding boxes intersect."""
    cand_min_x, cand_min_y, cand_max_x, cand_max_y = candidate_bbox
    query_min_x, query_min_y, query_max_x, query_max_y = query_bbox
    return not (
        cand_max_x < query_min_x or cand_min_x > query_max_x or
        cand_max_y < query_min_y or cand_min_y > query_max_y
    )


def _parse_envelope_bbox(env) -> Optional[List[float]]:
    """Extract ``[min_x, min_y, max_x, max_y]`` from a gml:Envelope."""
    if env is None:
        return None

    lower = env.findtext('./gml:lowerCorner', namespaces=NS)
    upper = env.findtext('./gml:upperCorner', namespaces=NS)
    if not lower or not upper:
        return None

    lower_coords = [float(x) for x in lower.strip().split()]
    upper_coords = [float(x) for x in upper.strip().split()]
    if len(lower_coords) < 2 or len(upper_coords) < 2:
        return None

    return [lower_coords[0], lower_coords[1], upper_coords[0], upper_coords[1]]


def _iter_spatial_candidates(elem):
    """
    Yield the current feature and its ancestors up to, but excluding, CityModel.

    Nested streaming features often have no own envelope although their parent
    Building/Bridge/Tunnel does. Checking ancestors aligns streaming behavior
    better with the DOM importer, which filters on the assembled feature tree.
    """
    current = elem
    while current is not None:
        tag = getattr(current, 'tag', None)
        if isinstance(tag, str) and localname(tag) == 'CityModel':
            break
        yield current
        current = current.getparent() if hasattr(current, 'getparent') else None


def _iter_xy_coords(elem):
    """Yield 2D coordinates from common GML coordinate encodings."""
    for poslist in elem.findall('.//gml:posList', NS):
        text = (poslist.text or '').strip()
        if not text:
            continue
        vals = [float(x) for x in text.split()]
        srs_dimension = (poslist.get('srsDimension') or '').strip()
        if srs_dimension.isdigit():
            dim = max(2, int(srs_dimension))
        elif len(vals) % 3 == 0:
            dim = 3
        elif len(vals) % 2 == 0:
            dim = 2
        else:
            continue
        for i in range(0, len(vals) - (dim - 1), dim):
            yield vals[i], vals[i + 1]

    for pos in elem.findall('.//gml:pos', NS):
        text = (pos.text or '').strip()
        if not text:
            continue
        vals = [float(x) for x in text.split()]
        if len(vals) >= 2:
            yield vals[0], vals[1]

    for coords_el in elem.findall('.//gml:coordinates', NS):
        text = (coords_el.text or '').strip()
        if not text:
            continue
        coord_sep = coords_el.get('cs', ',')
        tuple_sep = coords_el.get('ts', ' ')
        for tuple_text in text.split(tuple_sep):
            tuple_text = tuple_text.strip()
            if not tuple_text:
                continue
            vals = [float(x) for x in tuple_text.split(coord_sep) if x.strip()]
            if len(vals) >= 2:
                yield vals[0], vals[1]


def _coords_bbox(elem) -> Optional[List[float]]:
    """Compute a 2D bbox from coordinates embedded in a feature subtree."""
    min_x = min_y = float('inf')
    max_x = max_y = float('-inf')
    found = False

    for x, y in _iter_xy_coords(elem):
        found = True
        min_x = min(min_x, x)
        min_y = min(min_y, y)
        max_x = max(max_x, x)
        max_y = max(max_y, y)

    if not found:
        return None

    return [min_x, min_y, max_x, max_y]


def _feature_in_bbox(elem, bbox: List[float]) -> bool:
    """
    Check if feature is within bounding box.
    
    Args:
        elem: Feature element
        bbox: [min_x, min_y, max_x, max_y]
    
    Returns:
        True if feature intersects bbox
    """
    try:
        for candidate in _iter_spatial_candidates(elem):
            env_bbox = _parse_envelope_bbox(candidate.find('.//gml:Envelope', NS))
            if env_bbox is not None:
                return _bbox_intersects(env_bbox, bbox)

            candidate_bbox = _coords_bbox(candidate)
            if candidate_bbox is not None:
                return _bbox_intersects(candidate_bbox, bbox)

        # No spatial evidence found: exclude instead of leaking past the filter.
        return False

    except Exception:
        # Errors should not silently disable the spatial filter.
        return False


def should_use_streaming(file_path: str, threshold_mb: float = 100.0) -> bool:
    """
    Determine if streaming import should be used based on file size.
    
    Args:
        file_path: Path to CityGML file
        threshold_mb: File size threshold in MB (default: 100 MB)
    
    Returns:
        True if streaming import is recommended
    """
    if not is_streaming_available():
        return False
    
    try:
        size_mb = Path(file_path).stat().st_size / (1024 * 1024)
        return size_mb >= threshold_mb
    except Exception:
        return False
