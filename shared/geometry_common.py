# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# shared/geometry_common.py
"""
Common Geometry Utilities - Shared between CityGML 2.0 and 3.0

This module contains geometry parsing functions that are identical or nearly identical
across CityGML versions, extracted to avoid code duplication.
"""

from typing import List, Tuple, Optional, Dict, Any
import bpy
import bmesh


def parse_surface_generic_attributes(surf_el, ns_dict) -> Dict[str, Any]:
    """
    Parse generic attributes from a surface element.
    Works for both CityGML 2.0 and 3.0.
    
    Args:
        surf_el: XML surface element
        ns_dict: Namespace dictionary
    
    Returns:
        Dictionary of attribute name -> value
    """
    def _cast(ln_lower, txt):
        t = (txt or "").strip()
        if not t:
            return t
        if "double" in ln_lower.lower():
            try:
                return float(t)
            except Exception:
                return t
        if "int" in ln_lower.lower():
            try:
                return int(t)
            except Exception:
                return t
        return t

    attrs = {}

    # Wrapper-Variante: <core:genericAttribute><gen:*Attribute>...</gen:*Attribute></core:genericAttribute>
    for holder in surf_el.findall("./core:genericAttribute", ns_dict) + surf_el.findall("./gen:genericAttribute", ns_dict):
        for el in list(holder):
            if not isinstance(el.tag, str):
                continue
            ln = el.tag.split("}")[-1].lower()
            name = (el.findtext("./gen:name", default="", namespaces=ns_dict) or "").strip()
            val = (el.findtext("./gen:value", default="", namespaces=ns_dict) or "").strip()
            if name:
                attrs[name] = _cast(ln, val)

    # Direkt-Variante: <gen:*Attribute> direkt unter der Surface
    for el in surf_el.findall("./gen:*", ns_dict):
        if not isinstance(el.tag, str):
            continue
        ln = el.tag.split("}")[-1].lower()
        if not ln.endswith("attribute"):
            continue
        name = (el.findtext("./gen:name", default="", namespaces=ns_dict) or "").strip()
        val = (el.findtext("./gen:value", default="", namespaces=ns_dict) or "").strip()
        if name:
            attrs[name] = _cast(ln, val)

    return attrs


def parse_points_from_poslist(feat_el, default_srs: str, ref_origin: Tuple[float, float, float],
                              parse_poslist_func, inherit_srs_func) -> List[Tuple[float, float, float]]:
    """
    Parse gml:Point geometries from a feature element.
    
    Args:
        feat_el: XML feature element
        default_srs: Default spatial reference system
        ref_origin: Reference origin (x, y, z) for coordinate transformation
        parse_poslist_func: Function to parse poslist elements
        inherit_srs_func: Function to inherit SRS from parents
    
    Returns:
        List of (x, y, z) tuples relative to ref_origin
    """
    rx, ry, rz = ref_origin
    points = []
    
    # Find all gml:Point elements
    for point_el in feat_el.iter():
        if not isinstance(point_el.tag, str):
            continue
        ln = point_el.tag.split("}")[-1]
        if ln != "Point":
            continue
        
        # Get SRS for this point
        srs = inherit_srs_func(point_el, default_srs)
        
        # Parse pos
        pos_els = point_el.findall(".//{http://www.opengis.net/gml}pos") + \
                  point_el.findall(".//{http://www.opengis.net/gml/3.2}pos")
        
        for pos_el in pos_els:
            coords_str = (pos_el.text or "").strip()
            if not coords_str:
                continue
            
            parts = coords_str.split()
            if len(parts) < 2:
                continue
            
            try:
                x = float(parts[0]) - rx
                y = float(parts[1]) - ry
                z = float(parts[2]) - rz if len(parts) > 2 else 0.0
                points.append((x, y, z))
            except (ValueError, IndexError):
                continue
    
    return points


def parse_linestrings_from_feature(feat_el, default_srs: str, ref_origin: Tuple[float, float, float],
                                   parse_poslist_func, inherit_srs_func) -> List[List[Tuple[float, float, float]]]:
    """
    Parse gml:LineString and gml:Curve geometries from a feature element.
    
    Args:
        feat_el: XML feature element
        default_srs: Default spatial reference system
        ref_origin: Reference origin
        parse_poslist_func: Function to parse poslist elements
        inherit_srs_func: Function to inherit SRS from parents
    
    Returns:
        List of linestrings, where each linestring is a list of (x, y, z) tuples
    """
    rx, ry, rz = ref_origin
    linestrings = []
    
    # Find all gml:LineString and gml:Curve elements
    for geom_el in feat_el.iter():
        if not isinstance(geom_el.tag, str):
            continue
        ln = geom_el.tag.split("}")[-1]
        if ln not in ("LineString", "Curve"):
            continue
        
        # Get SRS
        srs = inherit_srs_func(geom_el, default_srs)
        
        # Parse posList
        poslist_els = geom_el.findall(".//{http://www.opengis.net/gml}posList") + \
                      geom_el.findall(".//{http://www.opengis.net/gml/3.2}posList")
        
        for poslist_el in poslist_els:
            coords = parse_poslist_func(poslist_el, srs)
            if coords:
                # Apply reference origin offset
                linestring = [(x - rx, y - ry, z - rz) for x, y, z in coords]
                if linestring:
                    linestrings.append(linestring)
    
    return linestrings


def create_mesh_from_geometry(verts: List[Tuple[float, float, float]], 
                              faces: List[List[int]], 
                              name: str = "Mesh") -> bpy.types.Mesh:
    """
    Create a Blender mesh from vertices and faces.
    
    Args:
        verts: List of (x, y, z) vertex coordinates
        faces: List of face vertex indices
        name: Mesh name
    
    Returns:
        Blender mesh data
    """
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    return mesh


def create_bmesh_from_geometry(verts: List[Tuple[float, float, float]], 
                               faces: List[List[int]]) -> bmesh.types.BMesh:
    """
    Create a BMesh from vertices and faces.
    
    Args:
        verts: List of (x, y, z) vertex coordinates
        faces: List of face vertex indices
    
    Returns:
        BMesh instance
    """
    bm = bmesh.new()
    
    # Add vertices
    bm_verts = [bm.verts.new(v) for v in verts]
    bm.verts.ensure_lookup_table()
    
    # Add faces
    for face_indices in faces:
        try:
            face_verts = [bm_verts[i] for i in face_indices]
            bm.faces.new(face_verts)
        except (ValueError, IndexError):
            continue
    
    bm.faces.ensure_lookup_table()
    return bm


def triangulate_polygon(vertices: List[Tuple[float, float, float]]) -> List[List[int]]:
    """
    Triangulate a polygon using Blender's BMesh triangulation.
    
    Args:
        vertices: List of vertex coordinates
    
    Returns:
        List of triangulated face indices
    """
    if len(vertices) < 3:
        return []
    
    if len(vertices) == 3:
        return [[0, 1, 2]]
    
    if len(vertices) == 4:
        return [[0, 1, 2], [0, 2, 3]]
    
    # For n-gons (n > 4), use BMesh triangulation
    try:
        bm = bmesh.new()
        bm_verts = [bm.verts.new(v) for v in vertices]
        bm.verts.ensure_lookup_table()
        
        face = bm.faces.new(bm_verts)
        bmesh.ops.triangulate(bm, faces=[face])
        
        # Extract triangulated faces
        result = []
        for f in bm.faces:
            result.append([v.index for v in f.verts])
        
        bm.free()
        return result
    except Exception:
        # Fallback: fan triangulation
        return [[0, i, i + 1] for i in range(1, len(vertices) - 1)]


def calculate_polygon_normal(vertices: List[Tuple[float, float, float]]) -> Tuple[float, float, float]:
    """
    Calculate normal vector for a polygon using Newell's method.
    
    Args:
        vertices: List of vertex coordinates
    
    Returns:
        Normal vector (nx, ny, nz)
    """
    if len(vertices) < 3:
        return (0.0, 0.0, 1.0)
    
    nx, ny, nz = 0.0, 0.0, 0.0
    
    for i in range(len(vertices)):
        v1 = vertices[i]
        v2 = vertices[(i + 1) % len(vertices)]
        
        nx += (v1[1] - v2[1]) * (v1[2] + v2[2])
        ny += (v1[2] - v2[2]) * (v1[0] + v2[0])
        nz += (v1[0] - v2[0]) * (v1[1] + v2[1])
    
    # Normalize
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length > 1e-10:
        return (nx / length, ny / length, nz / length)
    
    return (0.0, 0.0, 1.0)


def filter_degenerate_faces(faces: List[List[int]], verts: List[Tuple[float, float, float]], 
                           tolerance: float = 1e-6) -> List[List[int]]:
    """
    Filter out degenerate faces (duplicate vertices, colinear vertices).
    
    Args:
        faces: List of face vertex indices
        verts: List of vertex coordinates
        tolerance: Distance tolerance for duplicate vertex detection
    
    Returns:
        Filtered list of faces
    """
    filtered = []
    
    for face in faces:
        if len(face) < 3:
            continue
        
        # Remove duplicate consecutive vertices
        unique_indices = []
        for idx in face:
            if not unique_indices or idx != unique_indices[-1]:
                unique_indices.append(idx)
        
        # Check if first and last are duplicates
        if len(unique_indices) > 1 and unique_indices[0] == unique_indices[-1]:
            unique_indices = unique_indices[:-1]
        
        if len(unique_indices) < 3:
            continue
        
        # Check for degenerate geometry (all vertices at same position)
        face_verts = [verts[i] for i in unique_indices]
        all_same = True
        first = face_verts[0]
        for v in face_verts[1:]:
            dist = ((v[0] - first[0])**2 + (v[1] - first[1])**2 + (v[2] - first[2])**2) ** 0.5
            if dist > tolerance:
                all_same = False
                break
        
        if not all_same:
            filtered.append(unique_indices)
    
    return filtered
