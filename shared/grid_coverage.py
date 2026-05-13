# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# shared/grid_coverage.py
"""
GML RectifiedGridCoverage Parser für RasterRelief.

Unterstützt:
- gml:RectifiedGridCoverage (Raster-Höhendaten)
- gml:GridEnvelope (Dimensionen und Ausdehnung)
- gml:rangeSet (Höhenwerte)
- gml:origin (Ursprungspunkt)
- gml:offsetVector (Gitterauflösung)

Blender-Repräsentation:
- Grid → Mesh mit regelmäßiger Topologie
- Höhenwerte → Z-Koordinaten der Vertices
- UV-Mapping basierend auf Grid-Position

Shared between io/reader/ and io_v2/reader/ — NS dict must be passed as parameter.
"""

from __future__ import annotations
from typing import List, Tuple, Optional, Dict
import re


def parse_grid_envelope(limits_el, ns: Dict[str, str]) -> Optional[Dict]:
    """
    Parse gml:GridEnvelope für Dimensionen.

    Returns:
        Dict mit 'low': (x, y), 'high': (x, y), 'rows', 'cols'
    """
    if limits_el is None:
        return None

    low_text = limits_el.findtext(".//gml:low", namespaces=ns)
    high_text = limits_el.findtext(".//gml:high", namespaces=ns)

    if not low_text or not high_text:
        return None

    try:
        low_parts = low_text.strip().split()
        high_parts = high_text.strip().split()

        if len(low_parts) < 2 or len(high_parts) < 2:
            return None

        low_x, low_y = int(low_parts[0]), int(low_parts[1])
        high_x, high_y = int(high_parts[0]), int(high_parts[1])

        # Dimensions: high - low + 1 (inclusive)
        cols = high_x - low_x + 1
        rows = high_y - low_y + 1

        return {
            'low': (low_x, low_y),
            'high': (high_x, high_y),
            'rows': rows,
            'cols': cols,
            'total_points': rows * cols
        }
    except (ValueError, IndexError):
        return None


def parse_origin(origin_el, srs: str, ref_origin: Tuple[float, float, float],
                 ns: Dict[str, str], is_axis_order_latlon_func) -> Optional[Tuple[float, float, float]]:
    """
    Parse gml:origin (Ursprungspunkt des Grids).

    Returns:
        (x, y, z) Koordinaten
    """
    if origin_el is None:
        return None

    rx, ry, rz = ref_origin

    # Try gml:Point/gml:pos
    pos_el = origin_el.find(".//gml:Point/gml:pos", ns)
    if pos_el is not None:
        coords_str = (pos_el.text or "").strip()
        if coords_str:
            parts = coords_str.split()
            if len(parts) >= 2:
                if is_axis_order_latlon_func(srs):
                    if len(parts) == 2:
                        x, y, z = float(parts[1]), float(parts[0]), 0.0
                    else:
                        x, y, z = float(parts[1]), float(parts[0]), float(parts[2])
                else:
                    if len(parts) == 2:
                        x, y = float(parts[0]), float(parts[1])
                        z = 0.0
                    else:
                        x, y, z = float(parts[0]), float(parts[1]), float(parts[2])

                return (x - rx, y - ry, z - rz)

    return None


def parse_offset_vectors(grid_el, srs: str, ns: Dict[str, str],
                         is_axis_order_latlon_func) -> Optional[Dict]:
    """
    Parse gml:offsetVector (Gitterauflösung und Orientierung).

    Returns:
        Dict mit 'offset_x': (dx, dy, dz), 'offset_y': (dx, dy, dz)
    """
    if grid_el is None:
        return None

    offset_els = grid_el.findall(".//gml:offsetVector", ns)

    if len(offset_els) < 2:
        return None

    offsets = []
    for offset_el in offset_els[:2]:
        offset_text = (offset_el.text or "").strip()
        if offset_text:
            parts = offset_text.split()
            if len(parts) >= 2:
                if is_axis_order_latlon_func(srs):
                    if len(parts) == 2:
                        dx, dy, dz = float(parts[1]), float(parts[0]), 0.0
                    else:
                        dx, dy, dz = float(parts[1]), float(parts[0]), float(parts[2])
                else:
                    if len(parts) == 2:
                        dx, dy = float(parts[0]), float(parts[1])
                        dz = 0.0
                    else:
                        dx, dy, dz = float(parts[0]), float(parts[1]), float(parts[2])

                offsets.append((dx, dy, dz))

    if len(offsets) >= 2:
        return {
            'offset_x': offsets[0],
            'offset_y': offsets[1],
        }

    return None


def parse_range_set(range_set_el, ns: Dict[str, str],
                    expected_count: Optional[int] = None) -> Optional[List[float]]:
    """
    Parse gml:rangeSet (Höhenwerte).

    Returns:
        Liste von Höhenwerten (floats)
    """
    if range_set_el is None:
        return None

    # Try gml:DataBlock/gml:tupleList
    tuple_list_el = range_set_el.find(".//gml:DataBlock/gml:tupleList", ns)
    if tuple_list_el is not None:
        values_text = (tuple_list_el.text or "").strip()
        if values_text:
            values_text = values_text.replace(',', ' ')
            parts = values_text.split()

            try:
                values = [float(p) for p in parts]

                if expected_count is not None and len(values) != expected_count:
                    pass

                return values
            except ValueError:
                return None

    # Try gml:File (external file reference)
    file_el = range_set_el.find(".//gml:File", ns)
    if file_el is not None:
        file_name = file_el.findtext(".//gml:fileName", namespaces=ns)
        if file_name:
            return None

    return None


def parse_rectified_grid_coverage(grid_el, srs: str, ref_origin: Tuple[float, float, float],
                                  ns: Dict[str, str], is_axis_order_latlon_func) -> Optional[Dict]:
    """
    Parse vollständiges gml:RectifiedGridCoverage.

    Args:
        grid_el: XML Element für RectifiedGridCoverage
        srs: SRS Name
        ref_origin: Referenz-Ursprung für Offset
        ns: Namespace dict (NS)
        is_axis_order_latlon_func: Function to check axis order

    Returns:
        Dict mit allen Grid-Informationen
    """
    if grid_el is None:
        return None

    result = {}

    # Grid ID — try both GML 3.2 and GML 3.1.1 namespace
    gml_uri = ns.get("gml", "")
    grid_id = grid_el.get(f"{{{gml_uri}}}id")
    if not grid_id:
        # Fallback: try without namespace (some files use plain 'id')
        grid_id = grid_el.get("id")
    if grid_id:
        result['grid_id'] = grid_id

    # SRS
    grid_srs = grid_el.get("srsName") or srs
    result['srs'] = grid_srs

    # Dimensions (gml:limits/gml:GridEnvelope)
    limits_el = grid_el.find(".//gml:limits/gml:GridEnvelope", ns)
    dimensions = parse_grid_envelope(limits_el, ns)
    if dimensions:
        result['dimensions'] = dimensions
    else:
        return None

    # Origin (gml:origin)
    origin_el = grid_el.find(".//gml:origin", ns)
    origin = parse_origin(origin_el, grid_srs, ref_origin, ns, is_axis_order_latlon_func)
    if origin:
        result['origin'] = origin
    else:
        result['origin'] = (0.0, 0.0, 0.0)

    # Offset vectors (gml:offsetVector)
    offsets = parse_offset_vectors(grid_el, grid_srs, ns, is_axis_order_latlon_func)
    if offsets:
        result['offsets'] = offsets
    else:
        result['offsets'] = {
            'offset_x': (1.0, 0.0, 0.0),
            'offset_y': (0.0, 1.0, 0.0)
        }

    # Range set (height values)
    range_set_el = grid_el.find(".//gml:rangeSet", ns)
    values = parse_range_set(range_set_el, ns, dimensions['total_points'])
    if values:
        result['values'] = values
    else:
        result['values'] = [result['origin'][2]] * dimensions['total_points']

    return result


def create_mesh_from_grid(grid_data: Dict, name: str = "RasterRelief"):
    """
    Erstellt ein Blender-Mesh aus Grid-Daten.

    Args:
        grid_data: Dict von parse_rectified_grid_coverage()
        name: Name für das Mesh

    Returns:
        (mesh, verts, faces) - Blender Mesh-Daten
    """
    import bpy
    import bmesh

    dimensions = grid_data['dimensions']
    origin = grid_data['origin']
    offsets = grid_data['offsets']
    values = grid_data['values']

    rows = dimensions['rows']
    cols = dimensions['cols']

    offset_x = offsets['offset_x']
    offset_y = offsets['offset_y']

    ox, oy, oz = origin

    # Create vertices
    verts = []
    for row in range(rows):
        for col in range(cols):
            idx = row * cols + col

            x = ox + col * offset_x[0] + row * offset_y[0]
            y = oy + col * offset_x[1] + row * offset_y[1]

            if idx < len(values):
                z = values[idx]
            else:
                z = oz

            verts.append((x, y, z))

    # Create faces (quads between adjacent vertices)
    faces = []
    for row in range(rows - 1):
        for col in range(cols - 1):
            idx0 = row * cols + col
            idx1 = row * cols + (col + 1)
            idx2 = (row + 1) * cols + (col + 1)
            idx3 = (row + 1) * cols + col

            faces.append([idx0, idx1, idx2, idx3])

    # Create mesh
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()

    return mesh, verts, faces
