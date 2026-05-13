# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Consolidated GML curve segment parser shared by CityGML 2.0 and 3.0 readers.

from __future__ import annotations

import math
from typing import Callable, List, Mapping, Optional, Tuple


Point3D = Tuple[float, float, float]
AxisOrderChecker = Callable[[str], bool]
PosListParser = Callable[[object, str], List[Point3D]]


def _parse_pos_element(
    pos_el,
    srs: str,
    ref_origin: Point3D,
    is_axis_order_latlon_func: AxisOrderChecker,
) -> Optional[Point3D]:
    rx, ry, rz = ref_origin
    coords_str = (pos_el.text or "").strip()
    if not coords_str:
        return None

    parts = coords_str.split()
    if len(parts) < 2:
        return None

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


def _circle_through_3_points(
    p1: Point3D,
    p2: Point3D,
    p3: Point3D,
) -> Optional[Tuple[Point3D, float]]:
    x1, y1, z1 = p1
    x2, y2, z2 = p2
    x3, y3, z3 = p3

    denom = 2.0 * ((x1 - x2) * (y2 - y3) - (y1 - y2) * (x2 - x3))
    if abs(denom) < 1e-10:
        return None

    d1 = x1 * x1 + y1 * y1
    d2 = x2 * x2 + y2 * y2
    d3 = x3 * x3 + y3 * y3

    cx = ((d1 * (y2 - y3) + d2 * (y3 - y1) + d3 * (y1 - y2)) / denom)
    cy = ((d1 * (x3 - x2) + d2 * (x1 - x3) + d3 * (x2 - x1)) / denom)
    cz = (z1 + z2 + z3) / 3.0

    dx = x1 - cx
    dy = y1 - cy
    radius = math.sqrt(dx * dx + dy * dy)

    return ((cx, cy, cz), radius)


def _interpolate_arc(
    p1: Point3D,
    p2: Point3D,
    p3: Point3D,
    num_segments: int = 16,
) -> List[Point3D]:
    circle_result = _circle_through_3_points(p1, p2, p3)
    if circle_result is None:
        return [p1, p2, p3]

    center, radius = circle_result
    cx, cy, cz = center

    def angle(px, py):
        return math.atan2(py - cy, px - cx)

    angle1 = angle(p1[0], p1[1])
    angle2 = angle(p2[0], p2[1])
    angle3 = angle(p3[0], p3[1])

    def normalize_angle(a):
        while a < 0:
            a += 2 * math.pi
        while a >= 2 * math.pi:
            a -= 2 * math.pi
        return a

    angle1 = normalize_angle(angle1)
    angle2 = normalize_angle(angle2)
    angle3 = normalize_angle(angle3)

    if angle1 < angle3:
        if angle1 < angle2 < angle3:
            sweep = angle3 - angle1
            start_angle = angle1
        else:
            sweep = -(2 * math.pi - (angle3 - angle1))
            start_angle = angle1
    else:
        if angle3 < angle2 < angle1:
            sweep = -(angle1 - angle3)
            start_angle = angle1
        else:
            sweep = 2 * math.pi - (angle1 - angle3)
            start_angle = angle1

    points = []
    for i in range(num_segments + 1):
        t = i / num_segments
        angle = start_angle + t * sweep

        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        z = p1[2] + t * (p3[2] - p1[2])

        points.append((x, y, z))

    return points


def _interpolate_circle(
    p1: Point3D,
    p2: Point3D,
    p3: Point3D,
    num_segments: int = 32,
) -> List[Point3D]:
    circle_result = _circle_through_3_points(p1, p2, p3)
    if circle_result is None:
        return [p1, p2, p3, p1]

    center, radius = circle_result
    cx, cy, cz = center
    start_angle = math.atan2(p1[1] - cy, p1[0] - cx)

    points = []
    for i in range(num_segments + 1):
        angle = start_angle + (2 * math.pi * i / num_segments)

        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        z = cz

        points.append((x, y, z))

    return points


def _interpolate_arc_by_centerpoint(
    center: Point3D,
    radius: float,
    start_angle: float,
    end_angle: float,
    num_segments: int = 16,
) -> List[Point3D]:
    cx, cy, cz = center
    sweep = end_angle - start_angle

    while sweep > 2 * math.pi:
        sweep -= 2 * math.pi
    while sweep < -2 * math.pi:
        sweep += 2 * math.pi

    points = []
    for i in range(num_segments + 1):
        t = i / num_segments
        angle = start_angle + t * sweep

        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        z = cz

        points.append((x, y, z))

    return points


def parse_arc_segment(
    seg_el,
    srs: str,
    ref_origin: Point3D,
    *,
    ns: Mapping[str, str],
    parse_poslist_func: PosListParser,
    is_axis_order_latlon_func: AxisOrderChecker,
) -> Optional[List[Point3D]]:
    pos_elements = seg_el.findall(".//gml:pos", ns)

    if len(pos_elements) != 3:
        poslist_el = seg_el.find(".//gml:posList", ns)
        if poslist_el is not None:
            coords = parse_poslist_func(poslist_el, srs)
            if len(coords) >= 3:
                rx, ry, rz = ref_origin
                p1 = (coords[0][0] - rx, coords[0][1] - ry, coords[0][2] - rz)
                p2 = (coords[1][0] - rx, coords[1][1] - ry, coords[1][2] - rz)
                p3 = (coords[2][0] - rx, coords[2][1] - ry, coords[2][2] - rz)
                return _interpolate_arc(p1, p2, p3)
        return None

    p1 = _parse_pos_element(pos_elements[0], srs, ref_origin, is_axis_order_latlon_func)
    p2 = _parse_pos_element(pos_elements[1], srs, ref_origin, is_axis_order_latlon_func)
    p3 = _parse_pos_element(pos_elements[2], srs, ref_origin, is_axis_order_latlon_func)

    if not all([p1, p2, p3]):
        return None

    return _interpolate_arc(p1, p2, p3)


def parse_circle_segment(
    seg_el,
    srs: str,
    ref_origin: Point3D,
    *,
    ns: Mapping[str, str],
    parse_poslist_func: PosListParser,
    is_axis_order_latlon_func: AxisOrderChecker,
) -> Optional[List[Point3D]]:
    pos_elements = seg_el.findall(".//gml:pos", ns)

    if len(pos_elements) != 3:
        poslist_el = seg_el.find(".//gml:posList", ns)
        if poslist_el is not None:
            coords = parse_poslist_func(poslist_el, srs)
            if len(coords) >= 3:
                rx, ry, rz = ref_origin
                p1 = (coords[0][0] - rx, coords[0][1] - ry, coords[0][2] - rz)
                p2 = (coords[1][0] - rx, coords[1][1] - ry, coords[1][2] - rz)
                p3 = (coords[2][0] - rx, coords[2][1] - ry, coords[2][2] - rz)
                return _interpolate_circle(p1, p2, p3)
        return None

    p1 = _parse_pos_element(pos_elements[0], srs, ref_origin, is_axis_order_latlon_func)
    p2 = _parse_pos_element(pos_elements[1], srs, ref_origin, is_axis_order_latlon_func)
    p3 = _parse_pos_element(pos_elements[2], srs, ref_origin, is_axis_order_latlon_func)

    if not all([p1, p2, p3]):
        return None

    return _interpolate_circle(p1, p2, p3)


def parse_arcstring_segment(
    seg_el,
    srs: str,
    ref_origin: Point3D,
    *,
    ns: Mapping[str, str],
    parse_poslist_func: PosListParser,
    is_axis_order_latlon_func: AxisOrderChecker,
) -> Optional[List[Point3D]]:
    poslist_el = seg_el.find(".//gml:posList", ns)
    if poslist_el is not None:
        coords = parse_poslist_func(poslist_el, srs)
        if len(coords) >= 3:
            rx, ry, rz = ref_origin
            coords_adj = [(x - rx, y - ry, z - rz) for (x, y, z) in coords]

            all_points = []
            i = 0
            while i + 2 < len(coords_adj):
                p1 = coords_adj[i]
                p2 = coords_adj[i + 1]
                p3 = coords_adj[i + 2]

                arc_points = _interpolate_arc(p1, p2, p3)

                if all_points and arc_points:
                    all_points.extend(arc_points[1:])
                else:
                    all_points.extend(arc_points)

                i += 2

            return all_points if all_points else None

    pos_elements = seg_el.findall(".//gml:pos", ns)
    if len(pos_elements) >= 3:
        coords = []
        for pos_el in pos_elements:
            pt = _parse_pos_element(pos_el, srs, ref_origin, is_axis_order_latlon_func)
            if pt:
                coords.append(pt)

        if len(coords) >= 3:
            all_points = []
            i = 0
            while i + 2 < len(coords):
                arc_points = _interpolate_arc(coords[i], coords[i + 1], coords[i + 2])

                if all_points and arc_points:
                    all_points.extend(arc_points[1:])
                else:
                    all_points.extend(arc_points)

                i += 2

            return all_points if all_points else None

    return None


def parse_arc_by_centerpoint_segment(
    seg_el,
    srs: str,
    ref_origin: Point3D,
    *,
    ns: Mapping[str, str],
    is_axis_order_latlon_func: AxisOrderChecker,
) -> Optional[List[Point3D]]:
    pos_el = seg_el.find(".//gml:pos", ns)
    if pos_el is None:
        return None

    center = _parse_pos_element(pos_el, srs, ref_origin, is_axis_order_latlon_func)
    if center is None:
        return None

    radius_el = seg_el.find(".//gml:radius", ns)
    if radius_el is None:
        return None

    try:
        radius = float((radius_el.text or "").strip())
    except (ValueError, AttributeError):
        return None

    start_angle_el = seg_el.find(".//gml:startAngle", ns)
    end_angle_el = seg_el.find(".//gml:endAngle", ns)

    try:
        start_angle = float((start_angle_el.text or "0").strip()) if start_angle_el is not None else 0.0
        end_angle = float((end_angle_el.text or "0").strip()) if end_angle_el is not None else 2 * math.pi
    except (ValueError, AttributeError):
        return None

    start_uom = start_angle_el.get("uom") if start_angle_el is not None else "rad"
    end_uom = end_angle_el.get("uom") if end_angle_el is not None else "rad"

    if start_uom == "deg":
        start_angle = math.radians(start_angle)
    if end_uom == "deg":
        end_angle = math.radians(end_angle)

    return _interpolate_arc_by_centerpoint(center, radius, start_angle, end_angle)


def parse_circle_by_centerpoint_segment(
    seg_el,
    srs: str,
    ref_origin: Point3D,
    *,
    ns: Mapping[str, str],
    is_axis_order_latlon_func: AxisOrderChecker,
) -> Optional[List[Point3D]]:
    pos_el = seg_el.find(".//gml:pos", ns)
    if pos_el is None:
        return None

    center = _parse_pos_element(pos_el, srs, ref_origin, is_axis_order_latlon_func)
    if center is None:
        return None

    radius_el = seg_el.find(".//gml:radius", ns)
    if radius_el is None:
        return None

    try:
        radius = float((radius_el.text or "").strip())
    except (ValueError, AttributeError):
        return None

    return _interpolate_arc_by_centerpoint(center, radius, 0.0, 2 * math.pi, num_segments=32)
