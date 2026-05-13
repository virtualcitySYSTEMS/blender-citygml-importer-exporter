# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io/writer/namespaces.py

from __future__ import annotations

from xml.etree.ElementTree import register_namespace

# Prefix -> URI
NS = {
    "gml": "http://www.opengis.net/gml/3.2",
    "core": "http://www.opengis.net/citygml/3.0",
    "app": "http://www.opengis.net/citygml/appearance/3.0",
    "bldg": "http://www.opengis.net/citygml/building/3.0",
    "brid": "http://www.opengis.net/citygml/bridge/3.0",
    "con": "http://www.opengis.net/citygml/construction/3.0",
    "ct": "urn:oasis:names:tc:ciq:ct:3",
    "dem": "http://www.opengis.net/citygml/relief/3.0",
    "dyn": "http://www.opengis.net/citygml/dynamizer/3.0",
    "frn": "http://www.opengis.net/citygml/cityfurniture/3.0",
    "grp": "http://www.opengis.net/citygml/cityobjectgroup/3.0",
    "gen": "http://www.opengis.net/citygml/generics/3.0",
    "luse": "http://www.opengis.net/citygml/landuse/3.0",
    "pcl": "http://www.opengis.net/citygml/pointcloud/3.0",
    "tran": "http://www.opengis.net/citygml/transportation/3.0",
    "tun": "http://www.opengis.net/citygml/tunnel/3.0",
    "veg": "http://www.opengis.net/citygml/vegetation/3.0",
    "vers": "http://www.opengis.net/citygml/versioning/3.0",
    "wtr": "http://www.opengis.net/citygml/waterbody/3.0",
    "tsml": "http://www.opengis.net/tsml/1.0",
    "sos": "http://www.opengis.net/sos/2.0",
    "xAL": "urn:oasis:names:tc:ciq:xal:3",
    "xlink": "http://www.w3.org/1999/xlink",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    # optional ADE
    "ade": "http://www.3dcitydb.org/citygml-ade/3.0/citygml/1.0",
}

# Register non-default namespaces. The default namespace decision is made in document.py.
for p, u in NS.items():
    if p == "core":
        continue
    try:
        register_namespace(p, u)
    except ValueError:
        # xml.etree.ElementTree reserves prefixes matching "ns\\d+" for its own
        # auto-generated namespace prefixes. Keep NS mapping for lookups, but do not
        # force-register such prefixes globally.
        pass

GML_ID = f"{{{NS['gml']}}}id"
XSI_SL = f"{{{NS['xsi']}}}schemaLocation"


def Q(ns: str, ln: str) -> str:
    return f"{{{NS[ns]}}}{ln}"


def QC(local: str) -> str:
    return Q("con", local)


def QG(local: str) -> str:
    return Q("gml", local)


def build_schema_location() -> str:
    pairs = [
        (NS["con"], "http://schemas.opengis.net/citygml/construction/3.0/construction.xsd"),
        (NS["tran"], "http://schemas.opengis.net/citygml/transportation/3.0/transportation.xsd"),
        (NS["wtr"], "http://schemas.opengis.net/citygml/waterbody/3.0/waterBody.xsd"),
        (NS["veg"], "http://schemas.opengis.net/citygml/vegetation/3.0/vegetation.xsd"),
        (NS["dem"], "http://schemas.opengis.net/citygml/relief/3.0/relief.xsd"),
        (NS["bldg"], "http://schemas.opengis.net/citygml/building/3.0/building.xsd"),
        (NS["grp"], "http://schemas.opengis.net/citygml/cityobjectgroup/3.0/cityObjectGroup.xsd"),
        (NS["dyn"], "http://schemas.opengis.net/citygml/dynamizer/3.0/dynamizer.xsd"),
        (NS["pcl"], "http://schemas.opengis.net/citygml/pointcloud/3.0/pointCloud.xsd"),
        (NS["tun"], "http://schemas.opengis.net/citygml/tunnel/3.0/tunnel.xsd"),
        (NS["frn"], "http://schemas.opengis.net/citygml/cityfurniture/3.0/cityFurniture.xsd"),
        (NS["gen"], "http://schemas.opengis.net/citygml/generics/3.0/generics.xsd"),
        (NS["app"], "http://schemas.opengis.net/citygml/appearance/3.0/appearance.xsd"),
        (NS["luse"], "http://schemas.opengis.net/citygml/landuse/3.0/landUse.xsd"),
        (NS["brid"], "http://schemas.opengis.net/citygml/bridge/3.0/bridge.xsd"),
        (NS["vers"], "http://schemas.opengis.net/citygml/versioning/3.0/versioning.xsd"),
    ]
    return " ".join(f"{ns} {xsd}" for ns, xsd in pairs)
