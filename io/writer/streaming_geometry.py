# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io/writer/streaming_geometry.py
"""
Streaming Geometry Writers for CityGML Export

Provides memory-efficient geometry writing that integrates with StreamingXMLWriter.
These functions create geometry elements that can be immediately written and freed.

Key Differences from Standard Geometry Writers:
- No parent element modification (returns new element)
- Self-contained element creation
- Designed for immediate serialization
- Minimal memory footprint
"""

from __future__ import annotations
from typing import List, Tuple, Optional
from xml.etree.ElementTree import Element, SubElement
from uuid import uuid4

from .namespaces import Q, GML_ID


def create_polygon_element(
    poly_gid: str,
    exterior_ring: List[Tuple[float, float, float]],
    interior_rings: Optional[List[List[Tuple[float, float, float]]]] = None,
    srs_name: str = "",
    exterior_ring_id: Optional[str] = None,
    interior_ring_ids: Optional[List[str]] = None,
) -> Element:
    """
    Create standalone Polygon element.
    
    Args:
        poly_gid: GML ID for polygon
        exterior_ring: List of (x, y, z) coordinates
        interior_rings: Optional list of interior rings
        srs_name: SRS identifier
        exterior_ring_id: Optional GML ID for exterior ring
        interior_ring_ids: Optional GML IDs for interior rings
    
    Returns:
        Complete Polygon element ready for serialization
    """
    poly = Element(Q("gml", "Polygon"), {GML_ID: poly_gid})
    
    if srs_name:
        poly.set("srsName", srs_name)
    
    # Exterior ring
    ext = SubElement(poly, Q("gml", "exterior"))
    ext_ring = SubElement(ext, Q("gml", "LinearRing"))
    
    if exterior_ring_id:
        ext_ring.set(GML_ID, exterior_ring_id)
    
    ext_poslist = SubElement(ext_ring, Q("gml", "posList"))
    ext_poslist.set("srsDimension", "3")
    ext_coords = " ".join(f"{x} {y} {z}" for x, y, z in exterior_ring)
    ext_poslist.text = ext_coords
    
    # Interior rings
    if interior_rings:
        for i, int_ring in enumerate(interior_rings):
            interior = SubElement(poly, Q("gml", "interior"))
            int_ring_elem = SubElement(interior, Q("gml", "LinearRing"))
            
            if interior_ring_ids and i < len(interior_ring_ids):
                int_ring_elem.set(GML_ID, interior_ring_ids[i])
            
            int_poslist = SubElement(int_ring_elem, Q("gml", "posList"))
            int_poslist.set("srsDimension", "3")
            int_coords = " ".join(f"{x} {y} {z}" for x, y, z in int_ring)
            int_poslist.text = int_coords
    
    return poly


def create_multisurface_element(
    ms_gid: str,
    polygons: List[Element],
    srs_name: str = "",
) -> Element:
    """
    Create standalone MultiSurface element from polygon elements.
    
    Args:
        ms_gid: GML ID for MultiSurface
        polygons: List of Polygon elements
        srs_name: SRS identifier
    
    Returns:
        Complete MultiSurface element
    """
    ms = Element(Q("gml", "MultiSurface"), {GML_ID: ms_gid})
    
    if srs_name:
        ms.set("srsName", srs_name)
    
    for poly in polygons:
        sm = SubElement(ms, Q("gml", "surfaceMember"))
        sm.append(poly)
    
    return ms


def create_solid_element(
    solid_gid: str,
    polygon_gids: List[str],
    srs_name: str = "",
) -> Element:
    """
    Create standalone Solid element referencing polygons via XLink.
    
    Args:
        solid_gid: GML ID for Solid
        polygon_gids: List of polygon GML IDs to reference
        srs_name: SRS identifier
    
    Returns:
        Complete Solid element with surfaceMember references
    """
    solid = Element(Q("gml", "Solid"), {GML_ID: solid_gid})
    
    if srs_name:
        solid.set("srsName", srs_name)
    
    ext = SubElement(solid, Q("gml", "exterior"))
    shell = SubElement(ext, Q("gml", "Shell"), {GML_ID: f"UUID_{uuid4()}"})
    
    for poly_gid in polygon_gids:
        sm = SubElement(shell, Q("gml", "surfaceMember"))
        sm.set(Q("xlink", "href"), f"#{poly_gid}")
    
    return solid


def create_composite_surface_element(
    cs_gid: str,
    polygon_gids: List[str],
    srs_name: str = "",
) -> Element:
    """
    Create standalone CompositeSurface element.
    
    Args:
        cs_gid: GML ID for CompositeSurface
        polygon_gids: List of polygon GML IDs to reference
        srs_name: SRS identifier
    
    Returns:
        Complete CompositeSurface element
    """
    cs = Element(Q("gml", "CompositeSurface"), {GML_ID: cs_gid})
    
    if srs_name:
        cs.set("srsName", srs_name)
    
    for poly_gid in polygon_gids:
        sm = SubElement(cs, Q("gml", "surfaceMember"))
        sm.set(Q("xlink", "href"), f"#{poly_gid}")
    
    return cs


def create_lod_geometry_property(
    lod_level: int,
    geometry_elem: Element,
    version: str = "3.0",
) -> Element:
    """
    Wrap geometry element in LOD property (e.g., lod2Solid).
    
    Args:
        lod_level: LOD level (0-4)
        geometry_elem: Geometry element (Solid, MultiSurface, etc.)
        version: CityGML version ("2.0" or "3.0")
    
    Returns:
        LOD property element with geometry
    """
    geom_type = geometry_elem.tag.split('}')[-1]  # Extract local name
    lod_prop_name = f"lod{lod_level}{geom_type}"
    
    ns_prefix = "core" if version == "3.0" else "bldg"
    lod_prop = Element(Q(ns_prefix, lod_prop_name))
    lod_prop.append(geometry_elem)
    
    return lod_prop


class StreamingGeometryBuffer:
    """
    Buffer for accumulating geometry elements before feature creation.
    
    Helps manage geometry for a single city object while keeping
    memory footprint minimal.
    
    Usage:
        buffer = StreamingGeometryBuffer()
        
        # Add polygons as they're generated
        poly = create_polygon_element(...)
        buffer.add_polygon(poly, lod=2, semantic_type="WallSurface")
        
        # Create feature with all geometry
        feature = create_building_element(...)
        buffer.attach_to_feature(feature)
        
        # Write feature and clear buffer
        writer.write_city_object(feature)
        buffer.clear()
    """
    
    def __init__(self):
        self._polygons_by_lod: dict = {}  # {lod: [(poly_elem, semantic_type), ...]}
        self._polygon_gids: List[str] = []
        
    def add_polygon(
        self,
        poly_elem: Element,
        lod: int,
        semantic_type: Optional[str] = None
    ):
        """Add polygon to buffer."""
        if lod not in self._polygons_by_lod:
            self._polygons_by_lod[lod] = []
        
        self._polygons_by_lod[lod].append((poly_elem, semantic_type))
        
        # Track GML ID
        gml_id = poly_elem.get(GML_ID)
        if gml_id:
            self._polygon_gids.append(gml_id)
    
    def attach_to_feature(
        self,
        feature_elem: Element,
        version: str = "3.0",
        use_solid: bool = True
    ):
        """
        Attach buffered geometry to feature element.
        
        Args:
            feature_elem: Feature element (e.g., Building)
            version: CityGML version
            use_solid: Create Solid if True, else CompositeSurface
        """
        for lod, polys in self._polygons_by_lod.items():
            if use_solid:
                # Create Solid with references
                solid = create_solid_element(
                    solid_gid=f"UUID_{uuid4()}",
                    polygon_gids=[p[0].get(GML_ID) for p in polys],
                )
                lod_prop = create_lod_geometry_property(lod, solid, version)
                feature_elem.append(lod_prop)
            else:
                # Create CompositeSurface
                cs = create_composite_surface_element(
                    cs_gid=f"UUID_{uuid4()}",
                    polygon_gids=[p[0].get(GML_ID) for p in polys],
                )
                lod_prop = create_lod_geometry_property(lod, cs, version)
                feature_elem.append(lod_prop)
            
            # Add boundary surfaces if semantic types present
            if version == "3.0":
                self._add_boundary_surfaces(feature_elem, polys, lod)
    
    def _add_boundary_surfaces(
        self,
        feature_elem: Element,
        polys: List[Tuple[Element, Optional[str]]],
        lod: int
    ):
        """Add boundarySurface elements for semantic surfaces."""
        for poly_elem, semantic_type in polys:
            if not semantic_type:
                continue
            
            # Create boundary surface
            boundary = SubElement(feature_elem, Q("bldg", "boundary"))
            surf_elem = SubElement(boundary, Q("bldg", semantic_type))
            
            # Reference polygon
            lod_prop = SubElement(surf_elem, Q("core", f"lod{lod}MultiSurface"))
            ms = SubElement(lod_prop, Q("gml", "MultiSurface"))
            sm = SubElement(ms, Q("gml", "surfaceMember"))
            sm.set(Q("xlink", "href"), f"#{poly_elem.get(GML_ID)}")
    
    def get_polygon_gids(self) -> List[str]:
        """Get all polygon GML IDs in buffer."""
        return self._polygon_gids.copy()
    
    def clear(self):
        """Clear buffer and free memory."""
        for lod_polys in self._polygons_by_lod.values():
            for poly_elem, _ in lod_polys:
                poly_elem.clear()
        
        self._polygons_by_lod.clear()
        self._polygon_gids.clear()
    
    def polygon_count(self) -> int:
        """Get total polygon count."""
        return sum(len(polys) for polys in self._polygons_by_lod.values())


def estimate_geometry_memory(
    polygon_count: int,
    avg_vertices_per_polygon: int = 5,
    has_interior_rings: bool = False
) -> float:
    """
    Estimate memory usage for geometry in MB.
    
    Args:
        polygon_count: Number of polygons
        avg_vertices_per_polygon: Average vertices per polygon
        has_interior_rings: Whether polygons have interior rings
    
    Returns:
        Estimated memory in MB
    """
    # Each vertex: 3 floats × 8 bytes = 24 bytes
    # Element overhead: ~200 bytes per polygon
    # String data: ~100 bytes per polygon (IDs, attributes)
    
    vertex_mem = polygon_count * avg_vertices_per_polygon * 24
    elem_mem = polygon_count * 200
    string_mem = polygon_count * 100
    
    if has_interior_rings:
        # Assume 20% have interior rings, avg 4 vertices
        interior_mem = polygon_count * 0.2 * 4 * 24
    else:
        interior_mem = 0
    
    total_bytes = vertex_mem + elem_mem + string_mem + interior_mem
    return total_bytes / 1024 / 1024
