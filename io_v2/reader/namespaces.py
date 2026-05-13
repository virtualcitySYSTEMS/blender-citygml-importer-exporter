# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# CityGML 2.0 Namespace Definitions für Reader
# =============================================
# Namespace URIs für CityGML 2.0 Standard (OGC 12-019)
# Analog zu io/reader/namespaces.py für CityGML 3.0

NS = {
    # OGC/GML 3.1.1 (CityGML 2.0 basiert auf GML 3.1.1)
    "gml":  "http://www.opengis.net/gml",

    # CityGML 2.0 Core + Module
    "core": "http://www.opengis.net/citygml/2.0",
    "app":  "http://www.opengis.net/citygml/appearance/2.0",
    "bldg": "http://www.opengis.net/citygml/building/2.0",
    "brid": "http://www.opengis.net/citygml/bridge/2.0",
    # Kein "con:" in CityGML 2.0 - Boundary Surfaces sind im bldg/brid/tun Namespace
    "dem":  "http://www.opengis.net/citygml/relief/2.0",
    "frn":  "http://www.opengis.net/citygml/cityfurniture/2.0",
    "grp":  "http://www.opengis.net/citygml/cityobjectgroup/2.0",
    "gen":  "http://www.opengis.net/citygml/generics/2.0",
    "luse": "http://www.opengis.net/citygml/landuse/2.0",
    # Kein "pcl:" in CityGML 2.0 - PointCloud ist neu in 3.0
    "tex":  "http://www.opengis.net/citygml/texturedsurface/2.0",
    "tran": "http://www.opengis.net/citygml/transportation/2.0",
    "tun":  "http://www.opengis.net/citygml/tunnel/2.0",
    "veg":  "http://www.opengis.net/citygml/vegetation/2.0",
    # Kein "vers:" in CityGML 2.0 - Versioning ist neu in 3.0
    # Kein "dyn:" in CityGML 2.0 - Dynamizer ist neu in 3.0
    "wtr":  "http://www.opengis.net/citygml/waterbody/2.0",

    # Externe Namespaces
    "xAL":  "urn:oasis:names:tc:ciq:xsdschema:xAL:2.0",  # xAL 2.0 für Adressen
    "xlink": "http://www.w3.org/1999/xlink",
    "xsi":  "http://www.w3.org/2001/XMLSchema-instance",
}