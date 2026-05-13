# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
from __future__ import annotations
from typing import List, Tuple
from xml.etree.ElementTree import Element, SubElement

from .namespaces import Q, build_schema_location_citygml2
from .namespaces import register_export_namespaces_citygml2
import os
import bpy

def create_citymodel_root(srs_epsg: str = "") -> Element:
    """Create CityGML 2.0 CityModel root matching export_v2.gml."""
    # Ensure xsi/xlink/gml/core prefixes are bound for serializers/validators like FME.
    try:
        register_export_namespaces_citygml2()
    except Exception:
        pass
    root = Element(Q("core", "CityModel"))
    mode = "REMOTE"
    try:
        mode = str(getattr(getattr(bpy.context, "scene", None), "cgml3", None).export_schema_location_mode)
    except Exception:
        mode = os.environ.get("CGML_SCHEMA_LOCATION_MODE", "REMOTE")

    if mode == "NONE":
        pass
    else:
        # LOCAL currently behaves like REMOTE (can be reintroduced if needed).
        root.set(Q("xsi", "schemaLocation"), build_schema_location_citygml2())
    return root

def add_bounded_by(root: Element, coords_3d: List[Tuple[float, float, float]], srs_name: str, srs_dimension: str):
    """No-op: export_v2.gml has no CityModel-level boundedBy."""
    return

def add_cityobject_member(root: Element):
    return SubElement(root, Q("core", "cityObjectMember"))
