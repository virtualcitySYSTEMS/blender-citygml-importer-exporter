# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
from __future__ import annotations
from typing import List, Tuple
from xml.etree.ElementTree import Element, SubElement, register_namespace
import os
import bpy
from .namespaces import Q, XSI_SL, NS, build_schema_location
from .helpers import _envelope_elem

def create_citymodel_root(srs_epsg: str) -> Element:
    """
    Erzeugt das <core:CityModel>-Wurzelelement mit allen Namespace-Attributen
    und xsi:schemaLocation wie im Original.
    """
    # Prefer explicit core prefix on the root element: <core:CityModel>.
    # Some validators reject an unprefixed <CityModel> even when a default namespace is present.
    try:
        register_namespace("core", NS["core"])
    except Exception:
        pass

    # Ensure all known module prefixes are registered so the root header matches import__v3.gml
    # even if some modules are not used in the current export.
    for p, u in (NS or {}).items():
        try:
            if p in ("core",):
                continue
            register_namespace(p, u)
        except Exception:
            pass

    root = Element(Q("core", "CityModel"))
    # CityGML 3.0 requires that core elements live in the CityGML core namespace.
    # We intentionally keep the root tag prefixed (<core:CityModel>), but we also set the
    # default namespace to the same URI so unprefixed children like <cityObjectMember>,
    # <appearanceMember>, <creationDate>, ... are correctly in the CityGML core namespace
    # (and not in "no-namespace").
    try:
        root.set("xmlns", NS["core"])
    except Exception:
        pass

    mode = "REMOTE"
    try:
        mode = str(getattr(getattr(bpy.context, "scene", None), "cgml3", None).export_schema_location_mode)
    except Exception:
        mode = os.environ.get("CGML_SCHEMA_LOCATION_MODE", "REMOTE")

    # schemaLocation
    if mode == "NONE":
        pass
    else:
        # LOCAL currently behaves like REMOTE (can be reintroduced if needed).
        root.set(XSI_SL, build_schema_location())
    # weitere statische Attribute oder Metadaten bleiben unverändert
    return root

def add_bounded_by(root: Element,
                   coords_3d: List[Tuple[float, float, float]],
                   srs_epsg: str) -> None:
    """
    Fügt <gml:boundedBy>/<gml:Envelope> hinzu und positioniert es
    am Anfang des CityModel-Inhalts (vor cityObjectMember etc.).
    """
    if not coords_3d:
        return

    # wie bisher: Envelope erzeugen (wird ans Ende gehängt)
    _envelope_elem(root, coords_3d, srs_epsg)

    # neu erzeugtes <gml:boundedBy> nach vorne verschieben
    bb_tag = Q("gml", "boundedBy")
    bbs = [child for child in list(root) if child.tag == bb_tag]
    if not bbs:
        return

    bb = bbs[-1]          # das zuletzt angelegte boundedBy
    root.remove(bb)
    root.insert(0, bb)    # an Position 0 einfügen

def add_cityobject_member(root: Element):
    """
    Fügt ein <core:cityObjectMember> hinzu und gibt das Element zurück.
    """
    # FME compatibility: some readers are picky about the prefix on cityObjectMember.
    # Using the unprefixed element keeps it in the default (core) namespace.
    return SubElement(root, "cityObjectMember")
