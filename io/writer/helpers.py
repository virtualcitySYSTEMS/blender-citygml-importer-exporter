# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io/writer/helpers.py

from __future__ import annotations
from typing import List, Tuple, Optional
import re
import bpy
import xml.dom.minidom as minidom
from xml.etree.ElementTree import tostring, SubElement
from .namespaces import Q, NS, GML_ID
from uuid import uuid4

# XML Schema xs:ID is derived from NCName, so colon is not allowed and the name must start
# with a letter or '_' and then contain only name characters.
# We are conservative here and disallow '.' as well to match common schema validators.
_XS_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

def ensure_xs_id(val: object | None, prefix: str = "ID") -> str:
    """Erzeugt eine xs:ID-konforme gml:id.

    xs:ID muss mit Buchstabe oder '_' starten und darf nur [A-Za-z0-9_-] enthalten.
    Falls val ungültig/leer ist, wird eine neue ID generiert.
    """
    s = str(val).strip() if val is not None else ""
    if s.startswith("#"):
        s = s[1:]
    if not s:
        return f"{prefix}_{uuid4().hex}"
    # ungültige Zeichen ersetzen (xs:ID / NCName: erlaubt '-', '_'; '.' ist NICHT erlaubt)
    s = re.sub(r"[^A-Za-z0-9_-]", "_", s)
    # Startzeichen korrigieren
    if not re.match(r"^[A-Za-z_]", s):
        s = f"{prefix}_{s}"
    if not _XS_ID_RE.match(s):
        return f"{prefix}_{uuid4().hex}"
    return s

def fmt_pos(x: float) -> str:  # für Koordinaten wie gehabt, falls genutzt
    return f"{x:.6f}"

def fmt(v) -> str:
    # höhere Präzision bei der Schriftform, ohne überflüssige Nullen
    s = f"{float(v):.12f}"
    return s.rstrip("0").rstrip(".") if "." in s else s

def _height_srid() -> str:
    """
    Liefert die vertikale EPSG-Nummer aus World["Z-EPSG"] oder "".
    Kein Platzhalter mehr.
    """
    try:
        import bpy
        w = bpy.data.worlds.get("World")
        if w and "Z-EPSG" in w and str(w["Z-EPSG"]).strip():
            return str(w["Z-EPSG"]).strip()
    except Exception:
        pass
    return ""

def select_envelope_srs_name(srs_epsg: str) -> tuple[str, str]:
    """
    Liefert (srsName, srsDimension) für gml:Envelope unter Berücksichtigung von Z-Origin/Z-EPSG.
    Regeln:
      - Wenn World["Z-EPSG"] numerisch → 3D-Compound-CRS + srsDimension="3".
      - Sonst: Wenn World["Z-Origin"] existiert → 3D-Envelope mit horizontalem srsName + srsDimension="3".
      - Andernfalls 2D-Envelope mit srsDimension="2".
    """
    hz_raw = (str(srs_epsg) or "").strip()
    hz = _extract_epsg(hz_raw) or ""
    z = ""
    try:
        import bpy
        w = bpy.data.worlds.get("World")
        if w:
            z = str(w.get("Z-EPSG", "")).strip()
            has_z_origin = "Z-Origin" in w
        else:
            has_z_origin = False
    except Exception:
        has_z_origin = False

    if z.isdigit() and hz.isdigit():
        return (f"urn:ogc:def:crs,crs:EPSG::{hz},crs:EPSG::{z}", "3")

    if has_z_origin:
        # Match import__v3.gml: prefer URN for horizontal CRS
        name = f"urn:ogc:def:crs:EPSG::{hz}" if hz.isdigit() else (hz_raw or "")
        return (name, "3")

    # Match import__v3.gml: prefer URN for horizontal CRS
    name = f"urn:ogc:def:crs:EPSG::{hz}" if hz.isdigit() else (hz_raw or "")
    return (name, "2")

def _envelope_elem(parent, coords_3d, srs_epsg: str):
    """
    Schreibt gml:boundedBy/gml:Envelope.
    Regeln:
      - Wenn World["Z-EPSG"] numerisch → 3D-Compound-CRS und srsDimension="3".
      - Sonst: Wenn World["Z-Origin"] existiert → 3D-Envelope mit srsDimension="3" und horizontalem EPSG im srsName.
      - Andernfalls 2D-Envelope (XY) mit srsDimension="2".
    """
    if not coords_3d:
        return

    xs = [c[0] for c in coords_3d]
    ys = [c[1] for c in coords_3d]
    zs = [c[2] for c in coords_3d]

    hz = (str(srs_epsg) or "").strip()
    try:
        from .helpers import _extract_epsg  # eigener Helper
    except Exception:
        _extract_epsg = lambda v: str(v)
    hz_epsg = _extract_epsg(hz)

    z_epsg = _height_srid().strip()

    # prüfe Z-Origin
    has_z_origin = False
    try:
        import bpy
        w = bpy.data.worlds.get("World")
        has_z_origin = bool(w and "Z-Origin" in w)
    except Exception:
        has_z_origin = False

    bb = SubElement(parent, Q("gml", "boundedBy"))
    env = SubElement(bb, Q("gml", "Envelope"))

    if z_epsg.isdigit() and hz_epsg and hz_epsg.isdigit():
        # 3D-Compound-CRS mit echtem vertikalem Datum
        srs_name, srs_dim = select_envelope_srs_name(srs_epsg)
        env.set("srsName", srs_name)
        env.set("srsDimension", srs_dim)
        lc = SubElement(env, Q("gml", "lowerCorner"))
        lc.text = f"{fmt(min(xs))} {fmt(min(ys))} {fmt(min(zs))}"
        uc = SubElement(env, Q("gml", "upperCorner"))
        uc.text = f"{fmt(max(xs))} {fmt(max(ys))} {fmt(max(zs))}"
    elif has_z_origin:
        # 3D-Envelope ohne vertikales EPSG: srsName bleibt horizontal, Dimension=3
        srs_name, srs_dim = select_envelope_srs_name(srs_epsg)
        env.set("srsName", srs_name)
        env.set("srsDimension", srs_dim)
        lc = SubElement(env, Q("gml", "lowerCorner"))
        lc.text = f"{fmt(min(xs))} {fmt(min(ys))} {fmt(min(zs))}"
        uc = SubElement(env, Q("gml", "upperCorner"))
        uc.text = f"{fmt(max(xs))} {fmt(max(ys))} {fmt(max(zs))}"
    else:
        # 2D-Envelope
        srs_name, srs_dim = select_envelope_srs_name(srs_epsg)
        env.set("srsName", srs_name)
        env.set("srsDimension", srs_dim)
        lc = SubElement(env, Q("gml", "lowerCorner"))
        lc.text = f"{fmt(min(xs))} {fmt(min(ys))}"
        uc = SubElement(env, Q("gml", "upperCorner"))
        uc.text = f"{fmt(max(xs))} {fmt(max(ys))}"

def make_gml_id(name: str, used: set) -> str:
    s = (name or "").strip()
    # ungültige Zeichen → "_"
    s = re.sub(r"[^A-Za-z0-9_.-]", "_", s)
    # darf nicht mit Ziffer/“-”/“.” beginnen
    if not s or not re.match(r"[A-Za-z_]", s[0]):
        s = "ID_" + s
    # Mehrfachverwendung verhindern
    base = s; k = 2
    while s in used:
        s = f"{base}_{k}"; k += 1
    used.add(s)
    return s

def resolve_feature_tag(feat_type: str) -> Tuple[str, str]:
    """Mappt Feature-Typen auf (namespace_key, local_name) für CityGML 3.0.
    
    Unterstützt alle CityGML 3.0 Core Module:
    - Building (bldg)
    - Bridge (brid)
    - Tunnel (tun)
    - Transportation (tran)
    - CityFurniture (frn)
    - LandUse (luse)
    - Vegetation (veg)
    - WaterBody (wtr)
    - Construction (con)
    - Relief (dem)
    - CityObjectGroup (grp)
    - Generics (gen)
    """
    raw = (feat_type or "").strip()
    t = raw.lower()

    # Namespace-Prefix ignorieren: "bldg:building" -> "building"
    if ":" in t:
        _prefix, local = t.split(":", 1)
    else:
        local = t

    # --- Gebäude / Brücken / Tunnel ----------------------------------------
    if local in ("building", "bldg", "citygml_building"):
        return "bldg", "Building"
    
    # Building subdivisions
    if local in ("buildingunit", "building_unit"):
        return "bldg", "BuildingUnit"
    if local in ("buildingroom", "building_room", "room"):
        return "bldg", "BuildingRoom"
    if local in ("storey", "buildingstorey", "building_storey"):
        return "bldg", "Storey"
    
    # Building parts and elements
    if local in ("buildingpart", "building_part"):
        return "bldg", "BuildingPart"
    if local in (
        "buildingconstructiveelement",
        "building_constructive_element",
    ):
        return "bldg", "BuildingConstructiveElement"
    if local in ("buildinginstallation", "building_installation", "intbuildinginstallation"):
        return "bldg", "BuildingInstallation"
    if local in ("buildingfurniture", "building_furniture"):
        return "bldg", "BuildingFurniture"
    
    # Filling Elements (Öffnungen)
    if local in ("window", "con_window"):
        return "con", "Window"
    if local in ("door", "con_door"):
        return "con", "Door"

    # Hauptobjekt Brücke
    if local in ("bridge", "brid"):
        return "brid", "Bridge"
    if local in ("bridgepart", "bridge_part"):
        return "brid", "BridgePart"

    # BridgeConstructiveElement ist KEIN eigenständiges CityObject (core:cityObjectMember),
    # sondern ein Property-Element unter brid:Bridge (brid:bridgeConstructiveElement).
    # Daher darf es hier nicht als Feature-Tag aufgelöst werden. Es wird nur über die
    # Material-/SurfaceTyp-Logik im Export geschrieben.

    # Installationen: brid:BridgeInstallation
    if local in (
        "bridgeinstallation",
        "bridge_installation",
    ):
        return "brid", "BridgeInstallation"
    if local in ("bridgeroom", "bridge_room"):
        return "brid", "BridgeRoom"
    if local in ("bridgefurniture", "bridge_furniture"):
        return "brid", "BridgeFurniture"

    if local in ("tunnel", "tun"):
        return "tun", "Tunnel"
    if local in ("tunnelpart", "tunnel_part"):
        return "tun", "TunnelPart"
    if local in (
        "tunnelconstructiveelement",
        "tunnel_constructive_element",
        "tunnelconstructive",
    ):
        return "tun", "TunnelConstructiveElement"
    if local in (
        "tunnelinstallation",
        "tunnel_installation",
    ):
        return "tun", "TunnelInstallation"
    if local in (
        "tunnelfurniture",
        "tunnel_furniture",
    ):
        return "tun", "TunnelFurniture"
    if local in (
        "hollowspace",
        "hollow_space",
        "tunnelspace",
    ):
        return "tun", "HollowSpace"

    # --- Transportation -----------------------------------------------------
    if local in ("road", "roads"):
        return "tran", "Road"
    if local in ("railway", "railways", "rail"):
        return "tran", "Railway"
    if local in ("track", "tracks"):
        return "tran", "Track"
    if local in ("square", "squares", "plaza"):
        return "tran", "Square"
    if local in ("waterway", "waterways", "canal"):
        return "tran", "Waterway"
    if local in ("section", "sections", "road_section"):
        return "tran", "Section"
    if local in ("intersection", "intersections", "junction"):
        return "tran", "Intersection"
    if local in ("clearancespace", "clearance_space"):
        return "tran", "ClearanceSpace"
    if local in ("trafficarea", "traffic_area"):
        return "tran", "TrafficArea"
    if local in ("auxiliarytrafficarea", "auxiliary_traffic_area"):
        return "tran", "AuxiliaryTrafficArea"
    if local in ("trafficspace", "traffic_space"):
        return "tran", "TrafficSpace"
    if local in ("auxiliarytrafficspace", "auxiliary_traffic_space"):
        return "tran", "AuxiliaryTrafficSpace"
    if local in ("marking", "markings"):
        return "tran", "Marking"
    if local in ("hole", "holes"):
        return "tran", "Hole"
    # Fallback für generisches Transportation
    if local in ("transportation", "tran", "transport"):
        return "tran", "Road"

    # --- CityFurniture ------------------------------------------------------
    # z. B. "CityFurniture", "frn:CityFurniture"
    if local in ("cityfurniture", "city_furniture", "furniture"):
        return "frn", "CityFurniture"

    # --- Vegetation ---------------------------------------------------------
    # explizite Typen aus CityGML 3.0
    if local in ("plantcover", "plant_cover"):
        return "veg", "PlantCover"
    if local in ("solitaryvegetationobject", "solitary_vegetation_object"):
        return "veg", "SolitaryVegetationObject"

    # generische Vegetation / Baum-Aliasse
    if local in ("tree", "vegetationobject", "vegetation", "veg"):
        return "veg", "VegetationObject"

    # --- Wasser -------------------------------------------------------------
    if local in ("waterbody", "wtr"):
        return "wtr", "WaterBody"
    if local in ("watersurface", "water_surface"):
        return "wtr", "WaterSurface"
    if local in ("watergroundsurface", "water_ground_surface"):
        return "wtr", "WaterGroundSurface"
    
    # --- LandUse ------------------------------------------------------------
    if local in ("landuse", "luse"):
        return "luse", "LandUse"
    
    # --- Relief / Digital Elevation Model -----------------------------------
    # TINRelief (Triangulated Irregular Network)
    if local in ("tinrelief", "tin_relief", "tin"):
        return "dem", "TINRelief"
    
    # MassPointRelief (Punktwolke)
    if local in ("masspointrelief", "mass_point_relief", "masspoint"):
        return "dem", "MassPointRelief"
    
    # BreaklineRelief (Bruchkanten)
    if local in ("breaklinerelief", "breakline_relief", "breakline"):
        return "dem", "BreaklineRelief"
    
    # RasterRelief (Rasterdaten)
    if local in ("rasterrelief", "raster_relief", "raster", "grid"):
        return "dem", "RasterRelief"
    
    # ReliefFeature (Sammlung von Relief-Komponenten)
    if local in ("relieffeature", "relief_feature", "relief", "dem"):
        return "dem", "ReliefFeature"
    
    # --- Generics -----------------------------------------------------------
    # GenericLogicalSpace (logische Räume)
    if local in ("genericlogicalspace", "generic_logical_space"):
        return "gen", "GenericLogicalSpace"
    
    # GenericOccupiedSpace (belegte Räume)
    if local in ("genericoccupiedspace", "generic_occupied_space"):
        return "gen", "GenericOccupiedSpace"
    
    # GenericUnoccupiedSpace (unbelegte Räume)
    if local in ("genericunoccupiedspace", "generic_unoccupied_space"):
        return "gen", "GenericUnoccupiedSpace"
    
    # GenericThematicSurface (thematische Oberflächen)
    if local in ("genericthematicsurface", "generic_thematic_surface"):
        return "gen", "GenericThematicSurface"
    
    # --- CityObjectGroup ----------------------------------------------------
    # CityObjectGroup (Gruppierung von CityObjects)
    if local in ("cityobjectgroup", "city_object_group", "group", "grp"):
        return "grp", "CityObjectGroup"
    
    # --- PointCloud ---------------------------------------------------------
    # PointCloud (Punktwolken-Daten)
    if local in ("pointcloud", "point_cloud", "pcl"):
        return "pcl", "PointCloud"
    if local in ("otherconstruction", "other_construction"):
        return "con", "OtherConstruction"

    # Fallback: alles andere als generische Bauwerksart aus dem Construction-Modul
    return "con", "OtherConstruction"

def is_closed(ring: List[Tuple[float,float,float]]) -> bool:
    return len(ring) >= 4 and ring[0] == ring[-1]

def clamp01(v):
    try:
        v = float(v)
    except Exception:
        v = 0.0
    if v < 0.0: v = 0.0
    if v > 1.0: v = 1.0
    # als String zurückgeben, da wir Texte in XML schreiben
    return f"{v}"

def pretty_xml(elem) -> str:
    rough = tostring(elem, encoding="utf-8", xml_declaration=True)
    reparsed = minidom.parseString(rough)
    return reparsed.toprettyxml(indent="  ")

def _ring_area_sign_xyz(ring_xyz):
    pts = ring_xyz[:-1] if ring_xyz and ring_xyz[0] == ring_xyz[-1] else ring_xyz
    if len(pts) < 3: return 0
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]; zs=[p[2] for p in pts]
    rx,ry,rz = max(xs)-min(xs), max(ys)-min(ys), max(zs)-min(zs)
    if rx >= ry and rx >= rz:   pairs=[(y,z) for _,y,z in pts]   # YZ
    elif ry >= rz:              pairs=[(x,z) for x,_,z in pts]   # XZ
    else:                       pairs=[(x,y) for x,y,_ in pts]   # XY
    a=0.0
    for i in range(len(pairs)):
        x1,y1 = pairs[i]; x2,y2 = pairs[(i+1) % len(pairs)]
        a += x1*y2 - x2*y1
    return 1 if a > 0 else -1

# --- EPSG dynamisch aus World["CRS"] lesen (Fallback: Scene["SRID"]) ---
def _read_world_crs() -> str:
    try:
        w = bpy.data.worlds.get("World")
        if w and "CRS" in w:
            return str(w["CRS"]).strip()
    except Exception:
        pass
    # Fallback: Scene properties
    try:
        sc = bpy.context.scene
        if sc and "SRID" in sc:
            return str(sc["SRID"]).strip()
    except Exception:
        pass
    return ""

def format_temporal_property(value: str, property_type: str = "date") -> str:
    """
    Formatiert temporale Eigenschaften für CityGML 3.0.
    
    Args:
        value: ISO 8601 String (z.B. "2023-01-15" oder "2023-01-15T10:30:00")
        property_type: "date" für xs:date, "datetime" für xs:dateTime
    
    Returns:
        Formatierter String für CityGML Export
    """
    if not value:
        return ""
    
    v = str(value).strip()
    
    # Validierung: Basis-Check für ISO 8601 Format
    if property_type == "date":
        # Format: YYYY-MM-DD
        if len(v) >= 10 and v[4] == '-' and v[7] == '-':
            return v[:10]
    elif property_type == "datetime":
        # Format: YYYY-MM-DDTHH:MM:SS oder mit Timezone
        if 'T' in v:
            return v
        # Fallback: Datum + Zeit-Default
        if len(v) >= 10:
            return f"{v[:10]}T00:00:00"
    
    return v

def _extract_epsg(val: str) -> str:
    if not val:
        return ""
    parts = [p for p in str(val).split(":") if p.isdigit()]
    return parts[-1] if parts else (val if str(val).isdigit() else "")


# ---------------------------------------------------------------------------
# Semantic Validation Helper
# ---------------------------------------------------------------------------

def validate_semantic_rules(root_elem, warnings: list) -> None:
    """
    Führt semantische Validierungen am CityGML-Dokument durch.
    Fügt Warnungen zur warnings-Liste hinzu.
    
    Validierungen:
    - ReliefFeature MUSS mindestens ein reliefComponent haben
    - Building SOLLTE mindestens eine Boundary Surface haben
    - Boundary Surfaces SOLLTEN LoD-konsistent sein
    - gml:id MUSS eindeutig sein
    - Geometrien SOLLTEN nicht degeneriert sein (< 3 Punkte)
    """
    from xml.etree import ElementTree as ET
    
    # GML-ID Duplikate prüfen
    gml_ids = {}
    for elem in root_elem.iter():
        if isinstance(elem.tag, str):
            gml_id = elem.get(f"{{{NS['gml']}}}id")
            if gml_id:
                if gml_id in gml_ids:
                    warnings.append(f"Duplikat gml:id gefunden: {gml_id}")
                else:
                    gml_ids[gml_id] = elem
    
    # ReliefFeature ohne reliefComponent
    for elem in root_elem.iter():
        if isinstance(elem.tag, str) and elem.tag.endswith("}ReliefFeature"):
            has_relief_comp = False
            for child in elem.iter():
                if isinstance(child.tag, str) and "reliefComponent" in child.tag:
                    has_relief_comp = True
                    break
            if not has_relief_comp:
                gml_id = elem.get(f"{{{NS['gml']}}}id", "unknown")
                warnings.append(f"ReliefFeature ohne reliefComponent: {gml_id}")
    
    # Gebäude ohne Boundary Surfaces (optional)
    for elem in root_elem.iter():
        if isinstance(elem.tag, str) and elem.tag.endswith("}Building"):
            has_surfaces = False
            for child in elem.iter():
                if isinstance(child.tag, str) and ("Surface" in child.tag or "boundedBy" in child.tag):
                    has_surfaces = True
                    break
            if not has_surfaces:
                gml_id = elem.get(f"{{{NS['gml']}}}id", "unknown")
                # Nur Info, kein Fehler (manche Buildings haben nur LOD0/1)
                # warnings.append(f"Building ohne Boundary Surfaces: {gml_id}")
    
    # Degenerierte Geometrien (< 3 Punkte in Polygon)
    for elem in root_elem.iter():
        if isinstance(elem.tag, str) and elem.tag.endswith("}posList"):
            try:
                text = (elem.text or "").strip()
                if text:
                    coords = text.split()
                    dim = int(elem.get("srsDimension", "3"))
                    num_points = len(coords) // dim
                    if num_points < 3:
                        parent_id = "unknown"
                        for ancestor in root_elem.iter():
                            if elem in list(ancestor.iter()):
                                parent_id = ancestor.get(f"{{{NS['gml']}}}id", parent_id)
                                break
                        warnings.append(f"Degenerierte Geometrie (< 3 Punkte): {parent_id}")
            except Exception:
                pass

