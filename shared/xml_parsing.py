# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# shared/xml_parsing.py
"""
XML Parsing Utilities - Shared between CityGML 2.0 and 3.0

Common XML parsing functions used across both CityGML versions.

Performance: This module uses Cython-accelerated coordinate parsing when available,
providing 10-25× speedup for large files. Falls back gracefully to pure Python.
"""

try:
    from lxml import etree as ET
except ImportError:
    import xml.etree.ElementTree as ET

# Try to import Cython-accelerated coordinate parser
try:
    from ..cython_ext.coord_parser import (
        parse_coords_fast,
        parse_coords_as_tuples,
        parse_coords_swap_axes,
    )
    CYTHON_AVAILABLE = True
except ImportError:
    CYTHON_AVAILABLE = False


def localname(tag: str) -> str:
    """Extract local name from qualified tag name."""
    return tag.split("}")[-1] if isinstance(tag, str) and "}" in tag else tag


def get_attr(el, ns_key: str, name: str, ns_dict: dict):
    """Get attribute with namespace."""
    uri = ns_dict.get(ns_key)
    return el.get(f"{{{uri}}}{name}") if uri else None


def inherit_srs(el, default_srs):
    """Inherit srsName from element or ancestors."""
    cur = el
    while cur is not None:
        srs = cur.get("srsName")
        if srs:
            return srs
        cur = cur.getparent() if hasattr(cur, "getparent") else None
    return default_srs


def is_axis_order_latlon(srs_name: str) -> bool:
    """Check if SRS uses lat/lon axis order (needs swapping to lon/lat)."""
    if not srs_name:
        return False
    s = srs_name.strip().lower()
    # EPSG:4326 in OGC URN format uses lat/lon order
    return ("opengis.net/def/crs/epsg" in s and s.endswith("/4326")) or s == "epsg:4326"


def parse_poslist(poslist_el, srs_name):
    """
    Parse gml:posList element into list of (x, y, z) tuples.
    Handles axis order swapping for geographic CRS.
    Automatically closes rings if needed.
    
    Performance: Uses Cython acceleration when available (10-25× faster).
    """
    text = (poslist_el.text or "").strip()
    if not text:
        return []
    
    # Determine dimension
    dim_attr = poslist_el.get("srsDimension")
    
    # Check if axis order needs swapping
    axis_swapped = is_axis_order_latlon(srs_name)
    
    # Fast path: Cython-accelerated parsing  — grouping done at C level
    if CYTHON_AVAILABLE:
        # Resolve dimension (attribute preferred, fallback: parse flat + modulo)
        if dim_attr and dim_attr.isdigit():
            dim = int(dim_attr)
        else:
            flat = parse_coords_fast(text)
            dim = 3 if len(flat) % 3 == 0 else 2
        
        if axis_swapped:
            # parse_coords_swap_axes returns List[(y,x,z)] — already swapped at C level
            res = parse_coords_swap_axes(text, dim)
        else:
            # parse_coords_as_tuples returns tuple of tuples — convert to list for append
            res = list(parse_coords_as_tuples(text, dim))
    
    # Fallback: Pure Python parsing  — list-comprehension (faster than append loop)
    else:
        coords = [float(x) for x in text.split()]
        dim = int(dim_attr) if dim_attr and dim_attr.isdigit() else (3 if len(coords) % 3 == 0 else 2)
        
        if dim == 3:
            if axis_swapped:
                res = [(coords[i+1], coords[i], coords[i+2]) for i in range(0, len(coords) - 2, 3)]
            else:
                res = [(coords[i], coords[i+1], coords[i+2]) for i in range(0, len(coords) - 2, 3)]
        else:
            if axis_swapped:
                res = [(coords[i+1], coords[i], 0.0) for i in range(0, len(coords) - 1, 2)]
            else:
                res = [(coords[i], coords[i+1], 0.0) for i in range(0, len(coords) - 1, 2)]
    
    # Auto-close rings
    if len(res) >= 3 and res[0] != res[-1]:
        res.append(res[0])
    
    return res


def _norm_id(s: str | None) -> str:
    """Normalize GML ID reference (remove leading #)."""
    if not s:
        return ""
    s = s.strip()
    return s[1:] if s.startswith("#") else s
