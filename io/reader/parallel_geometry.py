# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Optimized Batch Geometry Processing for CityGML 3.0

Provides batch processing optimizations for geometry extraction.
Focuses on reducing overhead and improving cache efficiency with robust error recovery.
"""

from typing import List, Tuple, Optional, Set, Dict
import logging

try:
    from lxml import etree as ET
except ImportError:
    import xml.etree.ElementTree as ET

# Shared core: ImportErrorTracker and validate_geometry
from ...shared.parallel_processing import ImportErrorTracker, validate_geometry, estimate_optimal_batch_size  # noqa: F401


def batch_process_geometries(features: List, default_srs: str, ref_origin: Tuple[float, float, float],
                             lod_filter: Optional[Set[int]] = None, batch_size: int = 100,
                             error_tracker: Optional[ImportErrorTracker] = None):
    """
    Process features in batches for better cache locality and reduced overhead.
    
    This approach keeps all processing in the main thread (Blender API compatible)
    but optimizes by:
    1. Processing features in batches to improve CPU cache efficiency
    2. Pre-allocating data structures
    3. Reducing function call overhead
    4. Robust error recovery - individual feature failures don't abort import
    
    Args:
        features: List of feature XML elements
        default_srs: Default spatial reference system
        ref_origin: Reference origin (x, y, z)
        lod_filter: Optional LOD filter set
        batch_size: Number of features to process per batch
        error_tracker: Optional error tracker for diagnostics
    
    Returns:
        Generator yielding (feat_elem, verts, faces, surf_labels, ...) tuples for successful features
    """
    from .geometry import build_geometry
    from .namespaces import NS
    
    if error_tracker is None:
        error_tracker = ImportErrorTracker()
    
    total = len(features)
    
    for batch_start in range(0, total, batch_size):
        batch_end = min(batch_start + batch_size, total)
        batch = features[batch_start:batch_end]
        
        # Process batch
        for feat in batch:
            feat_id = None
            try:
                # Extract feature ID for error reporting
                feat_id = feat.get(f"{{{NS['gml']}}}id") or f"feat_{abs(hash(ET.tostring(feat)))}"
                
                # Build geometry - bake_implicit=True (default) ensures that
                # ImplicitGeometry (transformationMatrix + referencePoint) is fully
                # baked into the vertex coordinates, so no separate M_eff is needed.
                # Exclude inline child features from parent geometry. They are
                # imported as separate objects and must not leak into the parent mesh.
                _exclude_st = None
                _feat_ln = feat.tag.split("}")[-1]
                if _feat_ln in ("Building", "BuildingPart"):
                    _bldg_ns = NS.get("bldg", "")
                    _storey_els = feat.findall(".//{%s}Storey" % _bldg_ns)
                    _part_els = feat.findall(".//{%s}BuildingPart" % _bldg_ns)
                    _room_els = feat.findall(".//{%s}BuildingRoom" % _bldg_ns)
                    _inst_els = feat.findall(".//{%s}BuildingInstallation" % _bldg_ns)
                    _exclude_st = (
                        _storey_els + _part_els + _room_els + _inst_els
                        if (_storey_els or _part_els or _room_els or _inst_els)
                        else None
                    )
                elif _feat_ln in ("Bridge", "BridgePart"):
                    _brid_ns = NS.get("brid", "")
                    _part_els = feat.findall(".//{%s}BridgePart" % _brid_ns)
                    _inst_els = feat.findall(".//{%s}BridgeInstallation" % _brid_ns)
                    _exclude_st = (
                        _part_els + _inst_els
                        if (_part_els or _inst_els)
                        else None
                    )
                elif _feat_ln in ("Tunnel", "TunnelPart"):
                    _tun_ns = NS.get("tun", "")
                    _part_els = feat.findall(".//{%s}TunnelPart" % _tun_ns)
                    _inst_els = feat.findall(".//{%s}TunnelInstallation" % _tun_ns)
                    _exclude_st = (
                        _part_els + _inst_els
                        if (_part_els or _inst_els)
                        else None
                    )
                result = build_geometry(feat, default_srs, ref_origin, 
                                       include_surface_attrs=True, lod_filter=lod_filter,
                                       exclude_subtrees=_exclude_st)
                
                if len(result) == 9:
                    # NEU: 9 Werte mit filling_parent_surface_id_by_poly
                    faces, verts, surf_labels, surf_attrs_by_poly, surface_id_by_poly, multisurface_id_by_poly, compositesurface_id_by_poly, poly_also_in_solid, filling_parent_surface_id_by_poly = result
                elif len(result) == 8:
                    # 8 Werte mit poly_also_in_solid
                    faces, verts, surf_labels, surf_attrs_by_poly, surface_id_by_poly, multisurface_id_by_poly, compositesurface_id_by_poly, poly_also_in_solid = result
                    filling_parent_surface_id_by_poly = {}
                elif len(result) == 7:
                    # ALT: 7 Werte ohne poly_also_in_solid (Abwärtskompatibilität)
                    faces, verts, surf_labels, surf_attrs_by_poly, surface_id_by_poly, multisurface_id_by_poly, compositesurface_id_by_poly = result
                    poly_also_in_solid = {}
                    filling_parent_surface_id_by_poly = {}
                elif len(result) == 3:
                    faces, verts, surf_labels = result
                    surf_attrs_by_poly, surface_id_by_poly, multisurface_id_by_poly, compositesurface_id_by_poly, poly_also_in_solid, filling_parent_surface_id_by_poly = {}, {}, {}, {}, {}, {}
                else:
                    error_tracker.log_error(feat_id, "InvalidGeometryResult", 
                                          f"Unexpected build_geometry return format: {len(result)} elements")
                    continue
                
                # Validate geometry before yielding
                if verts and faces:
                    is_valid, error_msg = validate_geometry(verts, faces)
                    if not is_valid:
                        error_tracker.log_error(feat_id, "InvalidGeometry", error_msg)
                        continue
                
                # Yield successful result (NEU: mit poly_also_in_solid und filling_parent_surface_id_by_poly)
                yield (feat, verts, faces, surf_labels, surf_attrs_by_poly, surface_id_by_poly, multisurface_id_by_poly, compositesurface_id_by_poly, poly_also_in_solid, filling_parent_surface_id_by_poly)
                    
            except ET.ParseError as e:
                error_tracker.log_error(feat_id or "unknown", "XMLParseError", 
                                      "Failed to parse feature XML", e)
            except ValueError as e:
                error_tracker.log_error(feat_id or "unknown", "ValueError", 
                                      "Invalid value in geometry data", e)
            except TypeError as e:
                error_tracker.log_error(feat_id or "unknown", "TypeError", 
                                      "Type error in geometry processing", e)
            except Exception as e:
                # Catch-all for unexpected errors
                error_tracker.log_error(feat_id or "unknown", "UnexpectedError", 
                                      f"Unexpected error: {type(e).__name__}", e)

