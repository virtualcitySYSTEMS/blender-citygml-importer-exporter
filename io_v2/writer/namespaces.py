# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Namespace registry + QName helper for CityGML 2.0 export.

Wichtig:
- Default-Namespace ist CityGML Core (http://www.opengis.net/citygml/2.0)
- Prefixe werden korrekt registriert
- schemaLocation verweist standardmäßig auf schemas.opengis.net (keine lokalen file:// Pfade)
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

# --- Namespaces (CityGML 2.0 + dependencies) ---
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XLINK_NS = "http://www.w3.org/1999/xlink"
GML_NS = "http://www.opengis.net/gml"

# CityGML 2.0 core (default namespace)
CORE_NS = "http://www.opengis.net/citygml/2.0"

NS = {
    # dependencies
    "xsi": XSI_NS,
    "xlink": XLINK_NS,
    "gml": GML_NS,

    # core
    "core": CORE_NS,  # wird als Default-Namespace gesetzt

    # CityGML 2.0 modules
    "app": "http://www.opengis.net/citygml/appearance/2.0",
    "bldg": "http://www.opengis.net/citygml/building/2.0",
    "brid": "http://www.opengis.net/citygml/bridge/2.0",
    "frn": "http://www.opengis.net/citygml/cityfurniture/2.0",
    "gen": "http://www.opengis.net/citygml/generics/2.0",
    "grp": "http://www.opengis.net/citygml/cityobjectgroup/2.0",
    "dem": "http://www.opengis.net/citygml/relief/2.0",
    "tran": "http://www.opengis.net/citygml/transportation/2.0",
    "tun": "http://www.opengis.net/citygml/tunnel/2.0",
    "veg": "http://www.opengis.net/citygml/vegetation/2.0",
    "wtr": "http://www.opengis.net/citygml/waterbody/2.0",
    "luse": "http://www.opengis.net/citygml/landuse/2.0",

    # Address
    "xAL": "urn:oasis:names:tc:ciq:xsdschema:xAL:2.0",
}


def Q(prefix: str, local: str) -> str:
    uri = NS.get(prefix)
    if not uri:
        raise KeyError(f"Unknown namespace prefix '{prefix}'")
    return f"{{{uri}}}{local}"


def register_export_namespaces_citygml2() -> None:
    # Default namespace: profiles/base (wie in FZK-Haus)
    ET.register_namespace("", "http://www.opengis.net/citygml/profiles/base/2.0")

    # Register all prefixes including core
    for pfx, uri in NS.items():
        ET.register_namespace(pfx, uri)


def build_schema_location_citygml2(modules: list[str] | None = None) -> str:
    # Base schema for CityGML 2.0
    pairs: list[tuple[str, str]] = [
        (NS["core"], "http://schemas.opengis.net/citygml/2.0/cityGMLBase.xsd"),
    ]

    if modules is None:
        modules = ["app", "bldg", "brid", "frn", "gen", "grp", "dem", "tran", "tun", "veg", "wtr", "luse"]

    xsd = {
        "app": "http://schemas.opengis.net/citygml/appearance/2.0/appearance.xsd",
        "bldg": "http://schemas.opengis.net/citygml/building/2.0/building.xsd",
        "brid": "http://schemas.opengis.net/citygml/bridge/2.0/bridge.xsd",
        "frn": "http://schemas.opengis.net/citygml/cityfurniture/2.0/cityFurniture.xsd",
        "gen": "http://schemas.opengis.net/citygml/generics/2.0/generics.xsd",
        "grp": "http://schemas.opengis.net/citygml/cityobjectgroup/2.0/cityObjectGroup.xsd",
        "dem": "http://schemas.opengis.net/citygml/relief/2.0/relief.xsd",
        "tran": "http://schemas.opengis.net/citygml/transportation/2.0/transportation.xsd",
        "tun": "http://schemas.opengis.net/citygml/tunnel/2.0/tunnel.xsd",
        "veg": "http://schemas.opengis.net/citygml/vegetation/2.0/vegetation.xsd",
        "wtr": "http://schemas.opengis.net/citygml/waterbody/2.0/waterBody.xsd",
        "luse": "http://schemas.opengis.net/citygml/landuse/2.0/landUse.xsd",
    }

    for m in modules:
        if m in xsd:
            pairs.append((NS[m], xsd[m]))

    return " ".join(f"{ns_uri} {xsd_url}" for ns_uri, xsd_url in pairs)


GML_ID = Q("gml", "id")
XSI_SL = Q("xsi", "schemaLocation")

register_export_namespaces = register_export_namespaces_citygml2
build_schema_location = build_schema_location_citygml2

