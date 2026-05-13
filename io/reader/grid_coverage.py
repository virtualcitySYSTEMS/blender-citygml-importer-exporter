# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io/reader/grid_coverage.py
"""
GML RectifiedGridCoverage Parser für RasterRelief (CityGML 3.0).

Delegates to shared/grid_coverage.py with CityGML 3.0 namespaces.
"""

from __future__ import annotations
from typing import List, Tuple, Optional, Dict
from .namespaces import NS
from .xml_utils import is_axis_order_latlon
from ...shared.grid_coverage import (
    create_mesh_from_grid,
)
from ...shared import grid_coverage as _shared


def parse_grid_envelope(limits_el) -> Optional[Dict]:
    return _shared.parse_grid_envelope(limits_el, NS)


def parse_origin(origin_el, srs: str, ref_origin: Tuple[float, float, float]) -> Optional[Tuple[float, float, float]]:
    return _shared.parse_origin(origin_el, srs, ref_origin, NS, is_axis_order_latlon)


def parse_offset_vectors(grid_el, srs: str) -> Optional[Dict]:
    return _shared.parse_offset_vectors(grid_el, srs, NS, is_axis_order_latlon)


def parse_range_set(range_set_el, expected_count: Optional[int] = None) -> Optional[List[float]]:
    return _shared.parse_range_set(range_set_el, NS, expected_count)


def parse_rectified_grid_coverage(grid_el, srs: str, ref_origin: Tuple[float, float, float]) -> Optional[Dict]:
    return _shared.parse_rectified_grid_coverage(grid_el, srs, ref_origin, NS, is_axis_order_latlon)