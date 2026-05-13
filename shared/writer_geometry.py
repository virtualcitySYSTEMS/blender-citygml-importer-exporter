# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Consolidated writer geometry helpers shared by CityGML 2.0 and 3.0 exporters.

from __future__ import annotations

from typing import Callable, List, Optional, Tuple
from xml.etree.ElementTree import SubElement


Formatter = Callable[[float], str]
QNameFactory = Callable[[str, str], str]
Ring3D = List[Tuple[float, float, float]]


def format_poslist(ring_xyz: Ring3D, fmt: Formatter) -> str:
    flat: list[str] = []
    for x, y, z in ring_xyz:
        flat.extend((fmt(x), fmt(y), fmt(z)))
    return " ".join(flat)


def write_polygon_with_ring_ids(
    parent_ms,
    poly_gid: str,
    exterior_ring: Ring3D,
    interior_rings: Optional[List[Ring3D]],
    srs_name: Optional[str],
    exterior_ring_id: str,
    interior_ring_ids: Optional[List[str]],
    *,
    qname: QNameFactory,
    fmt: Formatter,
):
    s_member = SubElement(parent_ms, qname("gml", "surfaceMember"))
    gpoly = SubElement(s_member, qname("gml", "Polygon"), {qname("gml", "id"): poly_gid})

    exterior = SubElement(gpoly, qname("gml", "exterior"))
    lre = SubElement(exterior, qname("gml", "LinearRing"), {qname("gml", "id"): exterior_ring_id})
    posliste = SubElement(lre, qname("gml", "posList"))
    posliste.set("srsDimension", "3")
    posliste.text = format_poslist(exterior_ring, fmt)

    if interior_rings and interior_ring_ids:
        for ring, rid in zip(interior_rings, interior_ring_ids):
            interior = SubElement(gpoly, qname("gml", "interior"))
            lri = SubElement(interior, qname("gml", "LinearRing"), {qname("gml", "id"): rid})
            poslisti = SubElement(lri, qname("gml", "posList"))
            poslisti.set("srsDimension", "3")
            poslisti.text = format_poslist(ring, fmt)


def write_compositesurface_with_polygons(
    parent_ms,
    cs_id: str,
    polygons: List[dict],
    srs_name: Optional[str],
    *,
    qname: QNameFactory,
    fmt: Formatter,
):
    s_member = SubElement(parent_ms, qname("gml", "surfaceMember"))
    cs = SubElement(s_member, qname("gml", "CompositeSurface"), {qname("gml", "id"): cs_id})

    for poly in polygons:
        poly_s_member = SubElement(cs, qname("gml", "surfaceMember"))
        gpoly = SubElement(poly_s_member, qname("gml", "Polygon"), {qname("gml", "id"): poly["poly_gid"]})

        exterior = SubElement(gpoly, qname("gml", "exterior"))
        lre = SubElement(exterior, qname("gml", "LinearRing"), {qname("gml", "id"): poly["ext_id"]})
        posliste = SubElement(lre, qname("gml", "posList"))
        posliste.set("srsDimension", "3")
        posliste.text = format_poslist(poly["ext_xyz"], fmt)

        if poly.get("int_xyz") and poly.get("int_ids"):
            for ring, rid in zip(poly["int_xyz"], poly["int_ids"]):
                interior = SubElement(gpoly, qname("gml", "interior"))
                lri = SubElement(interior, qname("gml", "LinearRing"), {qname("gml", "id"): rid})
                poslisti = SubElement(lri, qname("gml", "posList"))
                poslisti.set("srsDimension", "3")
                poslisti.text = format_poslist(ring, fmt)
