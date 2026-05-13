# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Consolidated: shared implementation in shared/writer_geometry.py

from __future__ import annotations

from typing import List, Optional, Tuple

from .helpers import fmt
from .namespaces import Q
from ...shared.writer_geometry import (
    format_poslist as _format_poslist,
    write_compositesurface_with_polygons as _write_compositesurface_with_polygons,
    write_polygon_with_ring_ids as _write_polygon_with_ring_ids,
)


def format_poslist(ring_xyz: List[Tuple[float, float, float]]) -> str:
    return _format_poslist(ring_xyz, fmt)


def write_polygon_with_ring_ids(
    parent_ms,
    poly_gid: str,
    exterior_ring: List[Tuple[float, float, float]],
    interior_rings: Optional[List[List[Tuple[float, float, float]]]],
    srs_name: Optional[str],
    exterior_ring_id: str,
    interior_ring_ids: Optional[List[str]],
):
    return _write_polygon_with_ring_ids(
        parent_ms,
        poly_gid,
        exterior_ring,
        interior_rings,
        srs_name,
        exterior_ring_id,
        interior_ring_ids,
        qname=Q,
        fmt=fmt,
    )


def write_compositesurface_with_polygons(
    parent_ms,
    cs_id: str,
    polygons: List[dict],
    srs_name: Optional[str],
):
    return _write_compositesurface_with_polygons(
        parent_ms,
        cs_id,
        polygons,
        srs_name,
        qname=Q,
        fmt=fmt,
    )
