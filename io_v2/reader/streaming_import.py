# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Streaming Import for CityGML 2.0 using iterparse.

Memory-efficient import for very large CityGML 2.0 files.
See io.reader.streaming_import for detailed documentation.
"""

try:
    from lxml import etree as ET
    HAS_LXML = True
except ImportError:
    import xml.etree.ElementTree as ET
    HAS_LXML = False

from pathlib import Path
from typing import Optional, Set, List, Dict

from .namespaces import NS
from ..reader.xml_utils import localname


def is_streaming_available() -> bool:
    """Check if streaming import is available (requires lxml)."""
    return HAS_LXML and hasattr(ET, 'iterparse')


def iter_features_streaming(
    path: str,
    gml_id_filter: Optional[Set[str]] = None,
    bbox_filter: Optional[List[float]] = None,
    feature_type_filter: Optional[Set[str]] = None,
):
    """
    Iterator that yields CityGML 2.0 features one at a time using iterparse.
    
    Args:
        path: Path to CityGML file
        gml_id_filter: Optional set of GML IDs to import
        bbox_filter: Optional bounding box [min_x, min_y, max_x, max_y]
        feature_type_filter: Optional set of feature type names to import
    
    Yields:
        XML elements representing CityGML features
    """
    if not is_streaming_available():
        raise RuntimeError("Streaming import requires lxml")
    
    gml_id_attr = f"{{{NS['gml']}}}id"

    # CityGML 2.0 feature types
    feature_types = (
        'Building', 'BuildingPart', 'Bridge', 'BridgePart', 'Tunnel', 'TunnelPart',
        'BuildingInstallation', 'IntBuildingInstallation',
        'SolitaryVegetationObject', 'PlantCover', 'WaterBody', 'WaterSurface',
        'Road', 'Railway', 'Track', 'Square',
        'TrafficArea', 'AuxiliaryTrafficArea',
        'CityFurniture', 'LandUse', 'TINRelief', 'MassPointRelief',
        'BreaklineRelief', 'RasterRelief', 'CityObjectGroup',
        'GenericCityObject',
        'BridgeRoom', 'BridgeFurniture',
    )

    # Preserve-only names: these often appear under parent features as containers or sub-features.
    # NOTE: The CityGML 2.0 streaming iterator intentionally does not clear non-feature nodes
    # because doing so can remove embedded geometry before the parent is yielded. This set is
    # kept for robustness (and parity with CityGML 3.0) in case clearing is reintroduced later.
    preserve_elements = {
        # CityGML 2.0 container/property names frequently used under Room/Building/etc.
        'interiorFurniture', 'roomInstallation',
        'outerBuildingInstallation', 'interiorBuildingInstallation',
        'outerBridgeInstallation', 'interiorBridgeInstallation', 'bridgeRoomInstallation',
        'outerTunnelInstallation', 'interiorTunnelInstallation', 'hollowSpaceInstallation',
        # Waterbody / boundary surface / texturedsurface
        'TexturedSurface',
        '_BoundarySurface', '_WaterBoundarySurface', 'WaterGroundSurface', 'WaterClosureSurface',
        'lod2Surface', 'lod3Surface', 'lod4Surface',
        # Common boundary surface names
        'WallSurface', 'RoofSurface', 'GroundSurface', 'ClosureSurface',
        'FloorSurface', 'CeilingSurface', 'InteriorWallSurface', 'OuterFloorSurface',
        'OuterCeilingSurface',
        # Installations / furniture
        'BuildingFurniture', 'BuildingInstallation', 'IntBuildingInstallation',
        'BridgeInstallation', 'IntBridgeInstallation',
        'TunnelFurniture', 'TunnelInstallation', 'IntTunnelInstallation',
        # HollowSpace is a CityGML 3.0 feature but may occur in converted datasets
        'HollowSpace',
    }
    
    try:
        context = ET.iterparse(path, events=('end',), huge_tree=True)
        # NOTE: avoid aggressive clearing of non-feature nodes; see below.
        
        for event, elem in context:
            if event != 'end':
                continue
            
            ln = localname(elem.tag)

            # IMPORTANT:
            # CityGML 2.0 geometry is typically embedded under the feature element. Clearing
            # non-feature nodes during iterparse can silently remove geometry before the parent
            # feature (e.g., bldg:Building) is yielded. This results in "Empty features" and
            # only Null objects in Blender.
            #
            # Strategy:
            # - Only "yield" when we see a feature end tag.
            # - Do NOT call elem.clear() for arbitrary non-feature nodes.
            # - Rely on sibling-pruning after yielding a feature to keep memory under control.
            if ln not in feature_types:
                # Keep element intact for its parent feature; no clearing here.
                continue
            
            # Apply feature type filter
            if feature_type_filter and ln not in feature_type_filter:
                elem.clear()
                continue
            
            # Apply filters
            if gml_id_filter:
                feat_id = elem.get(gml_id_attr)
                if feat_id not in gml_id_filter:
                    elem.clear()
                    continue
            
            if bbox_filter:
                if not _feature_in_bbox(elem, bbox_filter):
                    elem.clear()
                    continue
            
            yield elem
            
            # Memory cleanup - DON'T clear the element itself as it's still needed for geometry processing
            # Only clean up previous siblings to manage memory
            # elem.clear()  # REMOVED: This was clearing the element before geometry could be extracted!
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
    """Check if feature is within bounding box."""
    try:
        for candidate in _iter_spatial_candidates(elem):
            env_bbox = _parse_envelope_bbox(candidate.find('.//gml:Envelope', NS))
            if env_bbox is not None:
                return _bbox_intersects(env_bbox, bbox)

            candidate_bbox = _coords_bbox(candidate)
            if candidate_bbox is not None:
                return _bbox_intersects(candidate_bbox, bbox)

        return False
    except Exception:
        return False


def should_use_streaming(file_path: str, threshold_mb: float = 100.0) -> bool:
    """Determine if streaming import should be used based on file size."""
    if not is_streaming_available():
        return False
    
    try:
        size_mb = Path(file_path).stat().st_size / (1024 * 1024)
        return size_mb >= threshold_mb
    except Exception:
        return False
