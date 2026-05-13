# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io/writer/grid_export.py
"""
Export Blender meshes as GML RectifiedGridCoverage für RasterRelief.

Strategie:
- Reguläre Grid-Meshes → RectifiedGridCoverage mit Höhenwerten
- Erkennung von Grid-Topologie (regelmäßige Vertex-Anordnung)
- Extraktion von Höhenwerten aus Z-Koordinaten
- Berechnung von Origin und Offset-Vektoren

Voraussetzung:
- Mesh muss regelmäßige Grid-Topologie haben
- Vertices müssen in Reihen/Spalten angeordnet sein
"""

from __future__ import annotations
from typing import List, Tuple, Optional, Dict
import math

try:
    import bpy
    import numpy as np
except ImportError:
    bpy = None
    np = None


def detect_grid_topology(mesh) -> Optional[Dict]:
    """
    Erkennt ob ein Mesh eine regelmäßige Grid-Struktur hat.
    
    Args:
        mesh: Blender mesh data
        
    Returns:
        Dict mit 'rows', 'cols', 'vertices' (sorted), oder None
    """
    if not mesh or not hasattr(mesh, 'vertices'):
        return None
    
    verts = list(mesh.vertices)
    if len(verts) < 4:  # Minimum 2x2 grid
        return None
    
    # Sort vertices by Y (rows) then X (columns)
    # Assume grid is aligned to XY plane
    sorted_verts = sorted(verts, key=lambda v: (round(v.co.y, 6), round(v.co.x, 6)))
    
    # Try to detect rows
    # Group by Y coordinate (with tolerance)
    y_tolerance = 0.01
    rows = []
    current_row = []
    last_y = None
    
    for v in sorted_verts:
        y = v.co.y
        if last_y is None or abs(y - last_y) < y_tolerance:
            current_row.append(v)
            last_y = y
        else:
            if current_row:
                rows.append(current_row)
            current_row = [v]
            last_y = y
    
    if current_row:
        rows.append(current_row)
    
    # Check if all rows have same length (regular grid)
    if len(rows) < 2:
        return None
    
    cols_per_row = [len(r) for r in rows]
    if len(set(cols_per_row)) != 1:  # Not all rows same length
        return None
    
    num_rows = len(rows)
    num_cols = cols_per_row[0]
    
    return {
        'rows': num_rows,
        'cols': num_cols,
        'vertices': sorted_verts,
        'grid_rows': rows
    }


def calculate_grid_parameters(grid_data: Dict, matrix_world) -> Optional[Dict]:
    """
    Berechnet Origin und Offset-Vektoren für das Grid.
    
    Args:
        grid_data: Dict von detect_grid_topology()
        matrix_world: Object's world transformation
        
    Returns:
        Dict mit 'origin', 'offset_x', 'offset_y', 'values'
    """
    rows = grid_data['rows']
    cols = grid_data['cols']
    grid_rows = grid_data['grid_rows']
    
    if rows < 2 or cols < 2:
        return None
    
    # First vertex (bottom-left) as origin
    first_vert = grid_rows[0][0]
    origin_local = first_vert.co
    origin_world = matrix_world @ origin_local
    
    # Second vertex in first row for X offset
    second_vert = grid_rows[0][1] if len(grid_rows[0]) > 1 else None
    if second_vert is None:
        return None
    
    second_local = second_vert.co
    second_world = matrix_world @ second_local
    
    offset_x = (
        second_world.x - origin_world.x,
        second_world.y - origin_world.y,
        second_world.z - origin_world.z
    )
    
    # First vertex of second row for Y offset
    if len(grid_rows) > 1:
        first_vert_row2 = grid_rows[1][0]
        row2_local = first_vert_row2.co
        row2_world = matrix_world @ row2_local
        
        offset_y = (
            row2_world.x - origin_world.x,
            row2_world.y - origin_world.y,
            row2_world.z - origin_world.z
        )
    else:
        return None
    
    # Extract height values (Z coordinates in world space)
    values = []
    for row in grid_rows:
        for vert in row:
            world_pos = matrix_world @ vert.co
            values.append(float(world_pos.z))
    
    return {
        'origin': (float(origin_world.x), float(origin_world.y), float(origin_world.z)),
        'offset_x': offset_x,
        'offset_y': offset_y,
        'values': values
    }


def can_export_as_raster_relief(obj) -> bool:
    """
    Prüft ob ein Objekt als RasterRelief exportiert werden kann.
    
    Args:
        obj: Blender object
        
    Returns:
        True wenn Objekt Grid-Struktur hat
    """
    if not obj or obj.type != 'MESH':
        return False
    
    # Check if marked as RasterRelief
    feat_type = obj.get("cgml3_feature", "")
    if "RasterRelief" not in feat_type:
        return False
    
    # Check for stored grid properties
    has_grid_props = (
        "dem:grid_rows" in obj or
        "dem:grid_cols" in obj or
        "dem:has_grid_data" in obj
    )
    
    if has_grid_props:
        return True
    
    # Try to detect grid topology
    deps = bpy.context.evaluated_depsgraph_get()
    obj_eval = obj.evaluated_get(deps)
    mesh = obj_eval.to_mesh(preserve_all_data_layers=True, depsgraph=deps)
    
    if mesh:
        grid_data = detect_grid_topology(mesh)
        obj_eval.to_mesh_clear()
        return grid_data is not None
    
    return False


def export_rectified_grid_coverage(obj, matrix_world, trf, offset, srs_name: str) -> Optional[Dict]:
    """
    Exportiert ein Blender-Mesh als RectifiedGridCoverage-Daten.
    
    Args:
        obj: Blender object (MESH)
        matrix_world: Object's world transformation
        trf: GeoTransformer for CRS conversion
        offset: (ox, oy, oz) export offset
        srs_name: Target SRS
        
    Returns:
        Dict mit Grid-Daten für XML-Generierung oder None
    """
    if not obj or obj.type != 'MESH':
        return None
    
    # Get evaluated mesh
    deps = bpy.context.evaluated_depsgraph_get()
    obj_eval = obj.evaluated_get(deps)
    mesh = obj_eval.to_mesh(preserve_all_data_layers=True, depsgraph=deps)
    
    if not mesh:
        return None
    
    try:
        # Check for stored grid data (from import)
        rows = obj.get("dem:grid_rows")
        cols = obj.get("dem:grid_cols")
        
        if rows and cols:
            # Use stored dimensions
            grid_data = {
                'rows': int(rows),
                'cols': int(cols),
                'vertices': list(mesh.vertices)
            }
            
            # Need to create grid_rows structure
            sorted_verts = sorted(mesh.vertices, key=lambda v: (round(v.co.y, 6), round(v.co.x, 6)))
            grid_rows = []
            for r in range(int(rows)):
                start_idx = r * int(cols)
                end_idx = start_idx + int(cols)
                grid_rows.append(sorted_verts[start_idx:end_idx])
            grid_data['grid_rows'] = grid_rows
        else:
            # Detect grid topology
            grid_data = detect_grid_topology(mesh)
            if not grid_data:
                obj_eval.to_mesh_clear()
                return None
        
        # Calculate grid parameters
        params = calculate_grid_parameters(grid_data, matrix_world)
        if not params:
            obj_eval.to_mesh_clear()
            return None
        
        # Transform to target CRS
        ox, oy, oz = offset
        
        # Transform origin
        origin_local = np.asarray([params['origin']], dtype=np.float64)
        origin_tgt, origin_tgt_offset = trf.transform_with_origin(origin_local, (ox, oy, oz))
        
        origin_final = (
            float(origin_tgt[0][0]) + origin_tgt_offset[0],
            float(origin_tgt[0][1]) + origin_tgt_offset[1],
            float(origin_tgt[0][2]) + origin_tgt_offset[2]
        )
        
        # Transform offset vectors (direction only, no translation)
        # For offsets, we only need the direction/magnitude, not absolute position
        # So we transform two points and take the difference
        offset_x_point = np.asarray([
            (params['origin'][0] + params['offset_x'][0],
             params['origin'][1] + params['offset_x'][1],
             params['origin'][2] + params['offset_x'][2])
        ], dtype=np.float64)
        
        offset_x_tgt, _ = trf.transform_with_origin(offset_x_point, (ox, oy, oz))
        
        offset_x_final = (
            float(offset_x_tgt[0][0] + origin_tgt_offset[0] - origin_final[0]),
            float(offset_x_tgt[0][1] + origin_tgt_offset[1] - origin_final[1]),
            float(offset_x_tgt[0][2] + origin_tgt_offset[2] - origin_final[2])
        )
        
        # Same for Y offset
        offset_y_point = np.asarray([
            (params['origin'][0] + params['offset_y'][0],
             params['origin'][1] + params['offset_y'][1],
             params['origin'][2] + params['offset_y'][2])
        ], dtype=np.float64)
        
        offset_y_tgt, _ = trf.transform_with_origin(offset_y_point, (ox, oy, oz))
        
        offset_y_final = (
            float(offset_y_tgt[0][0] + origin_tgt_offset[0] - origin_final[0]),
            float(offset_y_tgt[0][1] + origin_tgt_offset[1] - origin_final[1]),
            float(offset_y_tgt[0][2] + origin_tgt_offset[2] - origin_final[2])
        )
        
        # Height values don't need transformation (already in world space)
        # But we might need to adjust them based on the Z offset
        values = params['values']
        
        result = {
            'rows': grid_data['rows'],
            'cols': grid_data['cols'],
            'origin': origin_final,
            'offset_x': offset_x_final,
            'offset_y': offset_y_final,
            'values': values,
            'srs': srs_name
        }
        
        obj_eval.to_mesh_clear()
        return result
    
    except Exception as e:
        obj_eval.to_mesh_clear()
        return None
