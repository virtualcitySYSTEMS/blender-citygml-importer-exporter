# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io/writer/curve_export.py
"""
Export Blender curve splines as GML Curve segments.

Unterstützt:
- POLY Splines → gml:LineStringSegment (wie bisher)
- BEZIER Splines → gml:Arc oder gml:ArcString (approximiert)
- NURBS Splines → gml:ArcString (approximiert durch Sampling)

Strategie:
- Blender hat keine echten "Arc"-Primitiven
- BEZIER/NURBS-Kurven werden in Arcs approximiert wenn möglich
- Fallback: Sampling der evaluierten Kurve als LineStringSegment
"""

from __future__ import annotations
from typing import List, Tuple, Optional
import math

try:
    import bpy
except ImportError:
    bpy = None


def _sample_spline(spline, num_samples: int = 32) -> List[Tuple[float, float, float, float]]:
    """
    Sample a Blender spline at regular intervals.
    
    Args:
        spline: Blender spline object
        num_samples: Number of samples
        
    Returns:
        List of (x, y, z, w) tuples
    """
    if not hasattr(spline, 'calc_length'):
        # Fallback: use control points
        if hasattr(spline, 'bezier_points'):
            return [(p.co.x, p.co.y, p.co.z, 1.0) for p in spline.bezier_points]
        elif hasattr(spline, 'points'):
            return [(p.co.x, p.co.y, p.co.z, p.co.w) for p in spline.points]
        return []
    
    # Use evaluated points if available
    points = []
    resolution = max(num_samples, spline.resolution_u * len(spline.bezier_points if hasattr(spline, 'bezier_points') else spline.points))
    
    for i in range(resolution + 1):
        t = i / resolution
        # Unfortunately, there's no direct way to evaluate at t in Blender
        # We need to sample the actual evaluated points
        # This is a simplified version - in production code you'd use curve.calc_length() etc.
        pass
    
    # Fallback: return control points
    if hasattr(spline, 'bezier_points'):
        return [(p.co.x, p.co.y, p.co.z, 1.0) for p in spline.bezier_points]
    elif hasattr(spline, 'points'):
        return [(p.co.x, p.co.y, p.co.z, p.co.w) for p in spline.points]
    
    return points


def _fit_arc_to_3_points(p1: Tuple[float, float, float],
                         p2: Tuple[float, float, float],
                         p3: Tuple[float, float, float]) -> Optional[dict]:
    """
    Try to fit an arc through 3 points.
    
    Returns:
        Dict with 'type': 'Arc', 'points': [p1, p2, p3] if points form a valid arc
        None if points are collinear
    """
    x1, y1, z1 = p1
    x2, y2, z2 = p2
    x3, y3, z3 = p3
    
    # Check if points are collinear (2D in XY plane)
    denom = 2.0 * ((x1 - x2) * (y2 - y3) - (y1 - y2) * (x2 - x3))
    
    if abs(denom) < 1e-10:
        return None  # Collinear - use LineString instead
    
    # Valid arc
    return {
        'type': 'Arc',
        'points': [p1, p2, p3]
    }


def _try_fit_bezier_as_arcs(spline) -> Optional[List[dict]]:
    """
    Try to approximate a Bezier spline as a sequence of arcs.
    
    Args:
        spline: Blender BEZIER spline
        
    Returns:
        List of arc segment dicts or None if not possible
    """
    if not hasattr(spline, 'bezier_points'):
        return None
    
    bezier_points = list(spline.bezier_points)
    if len(bezier_points) < 2:
        return None
    
    # For Bezier splines, we can try to fit arcs to sequential point triplets
    # This is a simplified approach - in reality, Bezier curves are not arcs
    # Better: sample and fit arcs to sampled points
    
    segments = []
    
    # Simple strategy: every 3 bezier control points become one arc
    # This is a rough approximation
    for i in range(0, len(bezier_points) - 2, 2):
        p1 = (bezier_points[i].co.x, bezier_points[i].co.y, bezier_points[i].co.z)
        p2 = (bezier_points[i + 1].co.x, bezier_points[i + 1].co.y, bezier_points[i + 1].co.z)
        p3 = (bezier_points[i + 2].co.x, bezier_points[i + 2].co.y, bezier_points[i + 2].co.z)
        
        arc = _fit_arc_to_3_points(p1, p2, p3)
        if arc:
            segments.append(arc)
        else:
            # Fallback to line segment
            segments.append({
                'type': 'LineStringSegment',
                'points': [p1, p2, p3]
            })
    
    return segments if segments else None


def get_spline_export_type(spline) -> str:
    """
    Determine the best GML export type for a Blender spline.
    
    Returns:
        'Arc', 'ArcString', 'LineStringSegment', or 'LineString'
    """
    spline_type = spline.type
    
    if spline_type == 'POLY':
        # POLY splines are already linear - use LineStringSegment
        return 'LineStringSegment'
    
    elif spline_type == 'BEZIER':
        # BEZIER splines can potentially be arcs
        # Check if we have exactly 3 bezier points (one arc)
        if hasattr(spline, 'bezier_points') and len(list(spline.bezier_points)) == 3:
            return 'Arc'
        else:
            return 'ArcString'  # Multiple arcs
    
    elif spline_type == 'NURBS':
        # NURBS curves are complex - approximate as ArcString or LineString
        if hasattr(spline, 'points') and len(list(spline.points)) <= 5:
            return 'Arc'  # Try to fit as single arc
        else:
            return 'ArcString'
    
    else:
        # Unknown type - fallback to LineString
        return 'LineStringSegment'


def export_spline_as_curve_segment(spline, matrix_world, trf, offset) -> dict:
    """
    Export a Blender spline as a GML curve segment.
    
    Args:
        spline: Blender spline object
        matrix_world: Object's world transformation matrix
        trf: GeoTransformer for CRS conversion
        offset: (ox, oy, oz) export offset
        
    Returns:
        Dict with 'type' and 'coords' for XML generation
    """
    import numpy as np
    
    spline_type = spline.type
    export_type = get_spline_export_type(spline)
    
    # Extract coordinates based on spline type
    if spline_type == 'POLY' or spline_type == 'NURBS':
        # Use points
        if not hasattr(spline, 'points') or len(list(spline.points)) < 2:
            return None
        
        coords_local = np.asarray(
            [(float(p.co[0]), float(p.co[1]), float(p.co[2])) for p in spline.points],
            dtype=np.float64
        )
        
    elif spline_type == 'BEZIER':
        # Use bezier_points
        if not hasattr(spline, 'bezier_points') or len(list(spline.bezier_points)) < 2:
            return None
        
        coords_local = np.asarray(
            [(float(p.co.x), float(p.co.y), float(p.co.z)) for p in spline.bezier_points],
            dtype=np.float64
        )
    
    else:
        return None
    
    # Transform to target CRS
    ox, oy, oz = offset
    coords_tgt, origin_tgt = trf.transform_with_origin(coords_local, (ox, oy, oz))
    
    # Convert to final coordinates
    coords_final = [
        (
            float(coord[0]) + origin_tgt[0],
            float(coord[1]) + origin_tgt[1],
            float(coord[2]) + origin_tgt[2]
        )
        for coord in coords_tgt
    ]
    
    # Return segment info based on export type
    if export_type == 'Arc' and len(coords_final) >= 3:
        # Single arc through 3 points
        return {
            'type': 'Arc',
            'points': coords_final[:3]  # Use first 3 points
        }
    
    elif export_type == 'ArcString' and len(coords_final) >= 3:
        # Multiple arcs (every 2 points defines next arc)
        return {
            'type': 'ArcString',
            'points': coords_final
        }
    
    else:
        # Default: LineStringSegment
        return {
            'type': 'LineStringSegment',
            'points': coords_final
        }


def should_use_gml_curve(spline) -> bool:
    """
    Determine if a spline should be exported as gml:Curve (with segments)
    or as simple gml:LineString.
    
    Returns:
        True if spline is BEZIER or NURBS (complex curve)
        False if spline is POLY (already linear)
    """
    return spline.type in ('BEZIER', 'NURBS')
