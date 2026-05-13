# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Consolidated: shared implementation in shared/reader_curve_segments.py

from __future__ import annotations

from typing import List, Optional, Tuple

from .namespaces import NS
from .xml_utils import is_axis_order_latlon, parse_poslist
from ...shared.reader_curve_segments import (
    _circle_through_3_points,
    _interpolate_arc,
    _interpolate_arc_by_centerpoint,
    _interpolate_circle,
    _parse_pos_element as _shared_parse_pos_element,
    parse_arc_by_centerpoint_segment as _parse_arc_by_centerpoint_segment,
    parse_arc_segment as _parse_arc_segment,
    parse_arcstring_segment as _parse_arcstring_segment,
    parse_circle_by_centerpoint_segment as _parse_circle_by_centerpoint_segment,
    parse_circle_segment as _parse_circle_segment,
)


def _parse_pos_element(pos_el, srs: str, ref_origin: Tuple[float, float, float]) -> Optional[Tuple[float, float, float]]:
    return _shared_parse_pos_element(pos_el, srs, ref_origin, is_axis_order_latlon)


def parse_arc_segment(seg_el, srs: str, ref_origin: Tuple[float, float, float]) -> Optional[List[Tuple[float, float, float]]]:
    return _parse_arc_segment(
        seg_el,
        srs,
        ref_origin,
        ns=NS,
        parse_poslist_func=parse_poslist,
        is_axis_order_latlon_func=is_axis_order_latlon,
    )


def parse_circle_segment(seg_el, srs: str, ref_origin: Tuple[float, float, float]) -> Optional[List[Tuple[float, float, float]]]:
    return _parse_circle_segment(
        seg_el,
        srs,
        ref_origin,
        ns=NS,
        parse_poslist_func=parse_poslist,
        is_axis_order_latlon_func=is_axis_order_latlon,
    )


def parse_arcstring_segment(seg_el, srs: str, ref_origin: Tuple[float, float, float]) -> Optional[List[Tuple[float, float, float]]]:
    return _parse_arcstring_segment(
        seg_el,
        srs,
        ref_origin,
        ns=NS,
        parse_poslist_func=parse_poslist,
        is_axis_order_latlon_func=is_axis_order_latlon,
    )


def parse_arc_by_centerpoint_segment(seg_el, srs: str, ref_origin: Tuple[float, float, float]) -> Optional[List[Tuple[float, float, float]]]:
    return _parse_arc_by_centerpoint_segment(
        seg_el,
        srs,
        ref_origin,
        ns=NS,
        is_axis_order_latlon_func=is_axis_order_latlon,
    )


def parse_circle_by_centerpoint_segment(seg_el, srs: str, ref_origin: Tuple[float, float, float]) -> Optional[List[Tuple[float, float, float]]]:
    return _parse_circle_by_centerpoint_segment(
        seg_el,
        srs,
        ref_origin,
        ns=NS,
        is_axis_order_latlon_func=is_axis_order_latlon,
    )
