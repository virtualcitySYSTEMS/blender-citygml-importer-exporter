# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
import bpy, bmesh
from .namespaces import NS
from .xml_utils import localname, get_attr, inherit_srs, parse_poslist, is_axis_order_latlon, _norm_id
from .curve_segments import (
    parse_arc_segment,
    parse_circle_segment,
    parse_arcstring_segment,
    parse_arc_by_centerpoint_segment,
    parse_circle_by_centerpoint_segment,
)

# OPTIMIZATION: numpy für schnelle Koordinaten-Transformationen
try:
    from ...shared.numpy_geometry import transform_coordinates_fast, apply_offset_fast
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    print("[CityGML] Warning: numpy not available, using slower Python transforms")

GML_ID_LOOKUP = {}
XLINK_HREF = '{http://www.w3.org/1999/xlink}href'

# OPTIMIZED: Pre-compiled tag sets for O(1) surface type detection
# Statt bei jedem Element tag.split + string-Vergleiche zu machen,
# werden die vollqualifizierten Tags einmalig vorberechnet.
_CON_SURFACE_EXTRA_TAGS = {
    f"{{{NS['bldg']}}}BuildingConstructiveElement",
    f"{{{NS['brid']}}}BridgeConstructiveElement",
    f"{{{NS['brid']}}}BridgeInstallation",
    f"{{{NS['tun']}}}TunnelConstructiveElement",
    f"{{{NS['tun']}}}TunnelInstallation",
}

# Namespace-URIs für Surface-Erkennung
_CON_NS_URI = NS["con"]
_THEMATIC_SURFACE_NS_URIS = frozenset([
    NS.get("con", ""), NS.get("core", ""),
    NS.get("bldg", ""), NS.get("brid", ""), NS.get("tun", ""),
])

# OPTIMIZED: Pre-compiled tag set für iter_features() Single-Pass Subfeature-Erkennung
# Statt ~30 separate findall() Aufrufe pro cityObjectMember → ein einziger iter() Durchlauf.
_SUBFEATURE_TAGS = frozenset([
    # Building elements
    f"{{{NS['bldg']}}}BuildingPart",
    f"{{{NS['bldg']}}}BuildingConstructiveElement",
    f"{{{NS['bldg']}}}BuildingSubdivision",
    f"{{{NS['bldg']}}}Storey",
    f"{{{NS['bldg']}}}BuildingRoom",
    f"{{{NS['bldg']}}}BuildingUnit",
    f"{{{NS['bldg']}}}BuildingFurniture",
    f"{{{NS['bldg']}}}BuildingInstallation",
    # Bridge elements
    f"{{{NS['brid']}}}BridgePart",
    f"{{{NS['brid']}}}BridgeConstructiveElement",
    f"{{{NS['brid']}}}BridgeInstallation",
    f"{{{NS['brid']}}}BridgeRoom",
    f"{{{NS['brid']}}}BridgeFurniture",
    # Tunnel elements
    f"{{{NS['tun']}}}TunnelPart",
    f"{{{NS['tun']}}}HollowSpace",
    f"{{{NS['tun']}}}TunnelConstructiveElement",
    f"{{{NS['tun']}}}TunnelFurniture",
    f"{{{NS['tun']}}}TunnelInstallation",
    # Generic spaces
    f"{{{NS['gen']}}}GenericLogicalSpace",
    f"{{{NS['gen']}}}GenericOccupiedSpace",
    f"{{{NS['gen']}}}GenericUnoccupiedSpace",
    f"{{{NS['gen']}}}GenericThematicSurface",
    # CityObjectGroup
    f"{{{NS['grp']}}}CityObjectGroup",
    # Dynamizers
    f"{{{NS['dyn']}}}Dynamizer",
    # Transportation elements
    f"{{{NS['tran']}}}TrafficArea",
    f"{{{NS['tran']}}}AuxiliaryTrafficArea",
    f"{{{NS['tran']}}}TrafficSpace",
    f"{{{NS['tran']}}}AuxiliaryTrafficSpace",
    f"{{{NS['tran']}}}Section",
    f"{{{NS['tran']}}}Intersection",
    f"{{{NS['tran']}}}Marking",
    f"{{{NS['tran']}}}Hole",
    f"{{{NS['tran']}}}ClearanceSpace",
    # LandUse
    f"{{{NS['luse']}}}LandUse",
    # Relief (sub-features of ReliefFeature)
    f"{{{NS['dem']}}}TINRelief",
    f"{{{NS['dem']}}}MassPointRelief",
    f"{{{NS['dem']}}}BreaklineRelief",
    f"{{{NS['dem']}}}RasterRelief",
])

# Sondertags für con:filling//con:Window und con:filling//con:Door
_FILLING_TAGS = frozenset([
    f"{{{NS['con']}}}Window",
    f"{{{NS['con']}}}Door",
])
_OPENING_SURFACE_TAGS = frozenset([
    *_FILLING_TAGS,
    f"{{{NS['con']}}}WindowSurface",
    f"{{{NS['con']}}}DoorSurface",
    f"{{{NS['bldg']}}}Window",
    f"{{{NS['bldg']}}}Door",
])


def _is_filling_opening_surface(el):
    tag = getattr(el, "tag", None)
    return isinstance(tag, str) and tag in _OPENING_SURFACE_TAGS

def _is_con_surface(el):
    """Erkennt Construction-Surfaces im Sinne von CityGML 3.

    - Alle Elemente im Namespace con: deren Localname auf "Surface" endet
      werden als thematische Flächen behandelt (RoofSurface, WallSurface,
      GroundSurface, OuterCeilingSurface, OuterFloorSurface, ClosureSurface, ...).
    - Zusätzlich werden bestimmte Feature-Typen als Flächenträger behandelt,
      damit ihre Polygone ebenfalls einen Surface-Typ erhalten:
        * bldg:BuildingConstructiveElement
        * brid:BridgeConstructiveElement
        * brid:BridgeInstallation
        * tun:TunnelConstructiveElement
        * tun:TunnelInstallation
    """
    tag = el.tag
    if not isinstance(tag, str) or not tag.startswith("{"):
        return False
    # OPTIMIZED: Schnelltest über pre-compiled Set
    if tag in _CON_SURFACE_EXTRA_TAGS:
        return True
    # con:-Namespace + endet auf "Surface"
    _brace = tag.index("}")
    uri = tag[1:_brace]
    ln = tag[_brace + 1:]
    if ln in {"fillingSurface"}:
        return False
    if ln.startswith("lod") and ("MultiSurface" in ln or "Solid" in ln or "MultiCurve" in ln):
        return False
    if uri == _CON_NS_URI and ln.endswith("Surface"):
        return True
    return False

def _is_thematic_boundary_surface(el):
    """Erkennt thematische BoundarySurfaces (CityGML 2.x und 3.x Building/Bridge/Tunnel).

    Hintergrund:
    - CityGML 3 nutzt i.d.R. con:*Surface (Namespace con) für thematische Flächen.
    - CityGML 2 nutzt bldg:*Surface (WallSurface, RoofSurface, GroundSurface, ...)
      direkt im jeweiligen Modul-Namespace (bldg/brid/tun).

    Für den Import benötigen wir eine robuste Zuordnung Polygon gml:id -> Surface-Typ,
    unabhängig von der CityGML-Version.
    """
    tag = el.tag
    if not isinstance(tag, str) or not tag.startswith("{"):
        return False
    # OPTIMIZED: Schnelltest – endet der Tag überhaupt auf "Surface"?
    if not tag.endswith("Surface"):
        return False
    _brace = tag.index("}")
    uri = tag[1:_brace]
    ln = tag[_brace + 1:]
    if ln in {"fillingSurface"}:
        return False

    # Exclude LOD container elements (lod0MultiSurface, lod1MultiSurface, ...)
    if ln.startswith("lod") and len(ln) > 3:
        try:
            if ln[3].isdigit():
                return False
        except (IndexError, ValueError):
            pass

    # OPTIMIZED: URI gegen pre-compiled frozenset prüfen
    return uri in _THEMATIC_SURFACE_NS_URIS

def _parse_surface_generic_attributes(surf_el):
    """
    Liest core:genericAttribute / gen:*Attribute an einer Surface
    und gibt ein Dict {name: wert} zurück, analog zur Feature-Logik im Importer.
    """
    from .namespaces import NS

    def _cast(ln_lower, txt):
        t = (txt or "").strip()
        if not t:
            return t
        if ln_lower.endswith("DoubleAttribute"):
            try:
                return float(t)
            except Exception:
                return t
        if ln_lower.endswith("IntAttribute"):
            try:
                return int(t)
            except Exception:
                return t
        return t

    attrs = {}

    # Wrapper-Variante: <core:genericAttribute><gen:*Attribute>...</gen:*Attribute></core:genericAttribute>
    for holder in surf_el.findall("./core:genericAttribute", NS) + surf_el.findall("./gen:genericAttribute", NS):
        for el in list(holder):
            if not isinstance(el.tag, str):
                continue
            ln = el.tag.split("}")[-1].lower()
            name = (el.findtext("./gen:name", default="", namespaces=NS) or "").strip()
            val  = (el.findtext("./gen:value", default="", namespaces=NS) or "").strip()
            if name:
                attrs[name] = _cast(ln, val)

    # Direkt-Variante: <gen:*Attribute> direkt unter der Surface
    for el in surf_el.findall("./gen:*", NS):
        if not isinstance(el.tag, str):
            continue
        ln = el.tag.split("}")[-1].lower()
        if not ln.endswith("attribute"):
            continue
        name = (el.findtext("./gen:name", default="", namespaces=NS) or "").strip()
        val  = (el.findtext("./gen:value", default="", namespaces=NS) or "").strip()
        if name:
            attrs[name] = _cast(ln, val)

    # CityGML 3 Construction: relationToConstruction direkt an der Surface
    rel_el = surf_el.find("./con:relationToConstruction", NS)
    if rel_el is not None:
        rel_txt = (rel_el.text or "").strip()
        if rel_txt:
            # als Custom Property 'relationToConstruction' am Material ablegen
            attrs["relationToConstruction"] = rel_txt

    return attrs

def iter_features(root, gml_id_filter=None, bbox_filter=None, feature_type_filter=None, ns_manager=None):
    """
    Iteriert über alle CityObjects im CityGML:
    - sammelt CityObjects aus core:cityObjectMember/core:member/gml:featureMember/gml:member
    - extrahiert verschachtelte Subfeatures (Building/Bridge/Tunnel/Generics/Traffic/Windows/Doors)
    - wendet optional GML-ID-, BBOX- und Feature-Typ-Filter an.
    
    Args:
        root: XML root element
        gml_id_filter: Set of GML IDs to filter
        bbox_filter: [min_x, min_y, max_x, max_y] for spatial filtering
        feature_type_filter: Set of feature type names to include (e.g. {'Building', 'Bridge'})
        ns_manager: Optional DynamicNamespaceManager für dynamische Namespace-Erkennung
    """
    # CityGML 3: cityObjectMember kann mit oder ohne core: Präfix vorkommen
    # (abhängig davon, ob core als Default-Namespace deklariert ist)
    members = []
    for elem in root:
        tag = elem.tag if isinstance(elem.tag, str) else ""
        # Unterstütze sowohl core:cityObjectMember als auch unprefixed cityObjectMember
        if tag == "cityObjectMember" or tag.endswith("}cityObjectMember"):
            members.append(elem)
        elif tag == "member" or tag.endswith("}member"):
            members.append(elem)
        elif tag.endswith("}featureMember"):
            members.append(elem)

    # Dynamische Namespace-Erkennung wenn ns_manager übergeben wurde
    if ns_manager:
        from ...common.dynamic_namespaces import DynamicNamespaceManager
        if not isinstance(ns_manager, DynamicNamespaceManager):
            # Fallback: erstelle Manager aus root
            ns_manager = DynamicNamespaceManager(root)
        allowed_ns = tuple(ns_manager.get_all_namespaces_including_ades())
    else:
        # Rückwärtskompatibilität: hardcodierte Namespaces
        allowed_ns = (
            NS["core"], NS["bldg"], NS["brid"], NS["tun"], NS["tran"],
            NS["veg"], NS["wtr"], NS["con"], NS["frn"], NS["grp"],
            NS["luse"], NS["dem"], NS["pcl"], NS["tex"], NS["vers"], NS["dyn"], NS["gen"]
        )

    gml_id_attr = f"{{{NS['gml']}}}id"
    seen_ids = set()

    for mem in members:
        for child in list(mem):
            if not (isinstance(child.tag, str) and child.tag.startswith("{")):
                continue

            ns_uri = child.tag.split("}")[0].strip("{")
            if ns_uri not in allowed_ns:
                continue

            ln = localname(child.tag)
            if ln in ("CityModel", "Appearance"):
                continue

            # -------------------------
            # Subfeatures einsammeln
            # -------------------------
            # OPTIMIZED: Ein einziger iter()-Durchlauf statt ~30 separate findall()-Aufrufe.
            # Jeder findall() traversiert den gesamten Subbaum – bei 10.000 Features
            # waren das ~300.000 XPath-Traversierungen. Jetzt nur noch 1 pro Member.
            subs = []
            for _sub_el in child.iter():
                _sub_tag = getattr(_sub_el, 'tag', None)
                if not isinstance(_sub_tag, str) or _sub_el is child:
                    continue
                if _sub_tag in _SUBFEATURE_TAGS:
                    subs.append(_sub_el)
                # Sonderfall: con:filling//con:Window und con:filling//con:Door
                # Window/Door müssen unter con:filling liegen
                elif _sub_tag in _FILLING_TAGS:
                    # Prüfe ob ein Vorfahr con:filling ist
                    _p = _sub_el.getparent() if hasattr(_sub_el, 'getparent') else None
                    while _p is not None and _p is not child:
                        _ptag = getattr(_p, 'tag', '')
                        if isinstance(_ptag, str) and _ptag.endswith('}filling'):
                            subs.append(_sub_el)
                            break
                        _p = _p.getparent() if hasattr(_p, 'getparent') else None

            # Vegetation: PlantCover und SolitaryVegetationObject sind Top-Level-Features
            # und werden nicht als Subfeatures behandelt (werden über cityObjectMember importiert)

            # -------------------------
            # Parent + Subfeatures
            # -------------------------
            # Buildings: verschachtelte BuildingInstallation-Objekte gehören semantisch zum Building
            # (z.B. bldg:outerBuildingInstallation). Für den Import behandeln wir diese daher NICHT
            # als eigenständige Features, sondern importieren das Top-Level Building/BuildingPart.
            # Storey-Sub-Features und BuildingRoom werden jedoch als eigene Features importiert,
            # damit buildingSubdivision/Storey- und buildingRoom-Hierarchien erhalten bleiben.
            if ns_uri == NS["bldg"] and ln in ("Building", "BuildingPart"):
                part_subs = [s for s in subs if localname(s.tag) == "BuildingPart"]
                storey_subs = [s for s in subs if localname(s.tag) == "Storey"]
                room_subs = [s for s in subs if localname(s.tag) == "BuildingRoom"]
                inst_subs = [s for s in subs if localname(s.tag) == "BuildingInstallation"]
                candidates = [child] + part_subs + storey_subs + room_subs + inst_subs

            elif ns_uri == NS["brid"] and ln in ("Bridge", "BridgePart"):
                # Bei Bridges:
                # - Bridge selbst importieren
                # - BridgePart/BridgeRoom importieren
                # - BridgeInstallation als eigenes Feature (kann core:boundary haben)
                # - BridgeConstructiveElement ist in CityGML 3 ein untergeordnetes Element
                #   und wird nicht als eigenes Blender-Objekt importiert.
                subs_filtered = [
                    s for s in subs
                    if localname(s.tag) not in (
                        "IntBridgeInstallation",
                        "BridgeConstructiveElement",
                        "BridgeFurniture",
                    )
                ]
                candidates = [child] + subs_filtered

            elif ns_uri == NS["tun"] and ln in ("Tunnel", "TunnelPart"):
                # Tunnel: import parent + TunnelPart + TunnelInstallation sub-features.
                # TunnelInstallation als eigenes Feature (kann core:boundary haben).
                part_subs = [s for s in subs if localname(s.tag) == "TunnelPart"]
                inst_subs = [s for s in subs if localname(s.tag) == "TunnelInstallation"]
                candidates = [child] + part_subs + inst_subs
            else:
                candidates = subs or [child]

            for feat in candidates:
                if not (isinstance(feat.tag, str) and feat.tag.startswith("{")):
                    continue

                fid = feat.get(gml_id_attr, "")

                # GML-ID-Filter (optional)
                if gml_id_filter and fid and fid not in gml_id_filter:
                    continue

                # Doppelte gml:ids vermeiden
                if fid and fid in seen_ids:
                    continue
                if fid:
                    seen_ids.add(fid)

                # BBOX-Filter (optional)
                if bbox_filter:
                    xmin, ymin, xmax, ymax = bbox_filter
                    env = feat.find(".//gml:Envelope", NS)
                    if env is not None:
                        lower_el = env.find(".//gml:lowerCorner", NS)
                        upper_el = env.find(".//gml:upperCorner", NS)
                        if lower_el is not None and upper_el is not None:
                            try:
                                lower_vals = [float(x) for x in lower_el.text.split()]
                                upper_vals = [float(x) for x in upper_el.text.split()]
                                # einfache 2D-Schnittprüfung
                                if not (lower_vals[0] <= xmax and upper_vals[0] >= xmin and
                                        lower_vals[1] <= ymax and upper_vals[1] >= ymin):
                                    continue
                            except (ValueError, IndexError):
                                # bei Problemen → nicht filtern
                                pass
                
                # Feature-Typ-Filter (optional)
                if feature_type_filter:
                    feat_type = localname(feat.tag)
                    if feat_type not in feature_type_filter:
                        continue

                yield feat

def _rings_from_surface(surface_el, srs_surface):
    rings = []
    # exterior
    ext_lr = surface_el.find(".//gml:patches//gml:PolygonPatch//gml:exterior//gml:LinearRing", NS)
    if ext_lr is not None:
        pos = ext_lr.find(".//gml:posList", NS)
        if pos is not None:
            coords = parse_poslist(pos, pos.get("srsName") or srs_surface)
            rid = ext_lr.get(f"{{{NS['gml']}}}id") or ""
            rings.append((coords, rid, False))
    # interior(s)
    for lr in surface_el.findall(".//gml:patches//gml:PolygonPatch//gml:interior//gml:LinearRing", NS):
        pos = lr.find(".//gml:posList", NS)
        if pos is None:
            continue
        coords = parse_poslist(pos, pos.get("srsName") or srs_surface)
        rid = lr.get(f"{{{NS['gml']}}}id") or ""
        rings.append((coords, rid, True))
    return rings

def _rings_from_polygon(poly, srs_poly):
    rings = []
    ext_lr = poly.find("./gml:exterior//gml:LinearRing", NS)
    if ext_lr is not None:
        # Try posList first (more efficient for large coordinate lists)
        pos = ext_lr.find(".//gml:posList", NS)
        if pos is not None:
            coords = parse_poslist(pos, pos.get("srsName") or srs_poly)
            rid = ext_lr.get(f"{{{NS['gml']}}}id") or ""
            rings.append((coords, rid, False))
        else:
            # Fallback: Parse individual gml:pos elements (CityGML 2.0 style)
            pos_elements = ext_lr.findall(".//gml:pos", NS)
            if pos_elements:
                coords = []
                for pos_el in pos_elements:
                    text = (pos_el.text or "").strip()
                    if text:
                        parts = text.split()
                        if len(parts) >= 2:
                            x, y = float(parts[0]), float(parts[1])
                            z = float(parts[2]) if len(parts) > 2 else 0.0
                            coords.append((x, y, z))
                if coords:
                    rid = ext_lr.get(f"{{{NS['gml']}}}id") or ""
                    rings.append((coords, rid, False))

    for inter in poly.findall("./gml:interior", NS):
        lr = inter.find(".//gml:LinearRing", NS)
        if lr is None: continue

        # Try posList first
        pos = lr.find(".//gml:posList", NS)
        if pos is not None:
            coords = parse_poslist(pos, pos.get("srsName") or srs_poly)
            rid = lr.get(f"{{{NS['gml']}}}id") or ""
            rings.append((coords, rid, True))
        else:
            # Fallback: Parse individual gml:pos elements
            pos_elements = lr.findall(".//gml:pos", NS)
            if pos_elements:
                coords = []
                for pos_el in pos_elements:
                    text = (pos_el.text or "").strip()
                    if text:
                        parts = text.split()
                        if len(parts) >= 2:
                            x, y = float(parts[0]), float(parts[1])
                            z = float(parts[2]) if len(parts) > 2 else 0.0
                            coords.append((x, y, z))
                if coords:
                    rid = lr.get(f"{{{NS['gml']}}}id") or ""
                    rings.append((coords, rid, True))
    return rings

def build_geometry(feat_el, default_srs, ref_origin, include_surface_attrs=False, lod_filter=None, bake_implicit=True, exclude_subtrees=None):
    """
    Baut aus einem CityGML-Feature (feat_el) Polygon-Faces, Vertices und Oberflächenlabels.

    Wichtige Punkte:
    - Explizite Geometrien (MultiSurface / CompositeSurface / Solid / Polygon) werden wie bisher verarbeitet.
    - Implizite Geometrien (core:ImplicitGeometry) werden komplett unterstützt:
      * transformationMatrix + referencePoint werden zu einer 4x4-Matrix kombiniert.
      * relativeGeometry @xlink:href wird aufgelöst:
        - das referenzierte Geometrieelement (gml:id) wird über GML_ID_LOOKUP gefunden,
        - eine tiefe Kopie wird unterhalb von <relativeGeometry> eingefügt,
        - dadurch liegt die Geometrie innerhalb der ImplicitGeometry und bekommt denselben Transform.
    
    Args:
        feat_el: Feature XML element
        default_srs: Default spatial reference system
        ref_origin: Reference origin (x, y, z)
        include_surface_attrs: Include surface attributes in return
        lod_filter: Set of LOD numbers to include (e.g. {0, 1, 2}), None = all LODs
        exclude_subtrees: Optional list of XML elements whose descendants should be skipped
                          (used to exclude Storey geometry from the parent Building's mesh)
    """
    rx, ry, rz = ref_origin

    # Pre-build exclusion sets for child feature subtrees.
    #
    # lxml may hand out different Python proxy objects for the same XML node during
    # separate XPath/findall/iter traversals.  A pure id(element)-based check is
    # therefore not stable enough here: inline child features such as
    # bldg:BuildingInstallation can leak into the parent Building mesh and later be
    # exported twice, or unrelated elements can be skipped after proxy id reuse.
    # Use stable gml:id and ancestor checks instead.
    _exclude_oids = set()
    _exclude_root_gml_ids = set()
    _exclude_gml_ids = set()
    if exclude_subtrees:
        for subtree_root in exclude_subtrees:
            try:
                _root_gid = get_attr(subtree_root, "gml", "id") or ""
            except Exception:
                _root_gid = ""
            if _root_gid:
                _exclude_root_gml_ids.add(_norm_id(_root_gid))
            for _ex_el in subtree_root.iter():
                _exclude_oids.add(id(_ex_el))
                try:
                    _ex_gid = get_attr(_ex_el, "gml", "id") or ""
                except Exception:
                    _ex_gid = ""
                if _ex_gid:
                    _exclude_gml_ids.add(_norm_id(_ex_gid))

    def _is_excluded_element(el) -> bool:
        if el is None or (not _exclude_root_gml_ids and not _exclude_gml_ids):
            return False

        cur = el
        while cur is not None:
            try:
                cur_gid = get_attr(cur, "gml", "id") or ""
            except Exception:
                cur_gid = ""
            if cur_gid and _norm_id(cur_gid) in _exclude_root_gml_ids:
                return True

            cur = cur.getparent() if hasattr(cur, "getparent") else None

        return False

    def _has_excluded_gml_id(el) -> bool:
        if el is None or not _exclude_gml_ids:
            return False
        try:
            gid = get_attr(el, "gml", "id") or ""
        except Exception:
            gid = ""
        return bool(gid and _norm_id(gid) in _exclude_gml_ids)

    def _get_lod_from_parent(el):
        """
        Geht die Elternkette hoch und sucht nach einem Tag wie lod2MultiSurface, lod2Geometry, lod2Solid etc.
        Liefert die LoD-Nummer als int oder None.

        Hinweis: benötigt lxml-Elemente (getparent). Wenn nicht verfügbar, liefert None.
        """
        try:
            parent = getattr(el, "getparent", lambda: None)()
            while parent is not None:
                tag = localname(getattr(parent, "tag", "") or "")
                if tag.startswith("lod") and len(tag) > 4 and tag[3].isdigit():
                    return int(tag[3])
                parent = getattr(parent, "getparent", lambda: None)()
        except Exception:
            return None
        return None

    def _implicit_transform_for(elem):
        """
        Sucht in der Vorfahrenkette von elem nach einer core:ImplicitGeometry
        und baut eine 4x4-Transformationsmatrix:
            - transformationMatrix (falls vorhanden)
            - plus referencePoint als Translation
        """
        cur = elem
        while cur is not None:
            tag = getattr(cur, "tag", None)
            # Check for ImplicitGeometry with or without namespace prefix
            if isinstance(tag, str) and localname(tag) == "ImplicitGeometry":
                # 1) transformationMatrix lesen
                tm_el = cur.find(".//{http://www.opengis.net/citygml/3.0}transformationMatrix")
                if tm_el is None:
                    tm_el = cur.find(".//core:transformationMatrix", NS)
                # Fallback: search for transformationMatrix without namespace
                if tm_el is None:
                    for el in cur.iter():
                        if localname(getattr(el, "tag", "")) == "transformationMatrix":
                            tm_el = el
                            break

                M = None
                if tm_el is not None and (tm_el.text or "").strip():
                    txt = " ".join(tm_el.text.split())
                    try:
                        vals = [float(x) for x in txt.split()]
                    except ValueError:
                        vals = []
                    if len(vals) == 16:
                        # 4x4, zeilenweise
                        M = [
                            list(vals[0:4]),
                            list(vals[4:8]),
                            list(vals[8:12]),
                            list(vals[12:16]),
                        ]

                # 2) referencePoint lesen
                ref_x = ref_y = ref_z = 0.0
                ref_el = None
                for rp in cur.iter():
                    rtag = getattr(rp, "tag", None)
                    if not isinstance(rtag, str):
                        continue
                    if localname(rtag) != "referencePoint":
                        continue
                    ref_el = rp
                    break

                if ref_el is not None:
                    pos_el = ref_el.find(".//gml:pos", NS)
                    if pos_el is None:
                        pos_el = ref_el.find(".//gml:posList", NS)
                    if pos_el is not None and (pos_el.text or "").strip():
                        txt = " ".join(pos_el.text.split())
                        try:
                            vals = [float(x) for x in txt.split()]
                        except ValueError:
                            vals = []
                        if len(vals) >= 2:
                            ref_x, ref_y = vals[0], vals[1]
                            ref_z = vals[2] if len(vals) >= 3 else 0.0

                # 3) Matrix mit Translation kombinieren
                if M is None:
                    # nur Translation durch referencePoint
                    return [
                        [1.0, 0.0, 0.0, ref_x],
                        [0.0, 1.0, 0.0, ref_y],
                        [0.0, 0.0, 1.0, ref_z],
                        [0.0, 0.0, 0.0, 1.0],
                    ]
                else:
                    M = [row[:] for row in M]
                    M[0][3] += ref_x
                    M[1][3] += ref_y
                    M[2][3] += ref_z
                    return M

            cur = cur.getparent() if hasattr(cur, "getparent") else None

        return None

    def _apply_transform(elem, coords):
        """
        coords: Liste von (x,y,z) direkt aus den GML-Koordinaten.

        - Ohne ImplicitGeometry: nur Datei-Offset ref_origin abziehen.
        - Mit ImplicitGeometry: transformationMatrix + referencePoint auswerten,
          dann ref_origin abziehen.
        """
        M = _implicit_transform_for(elem)

        if M is None:
            # normale (explizite) Geometrie
            # OPTIMIZATION: numpy nur bei sehr großen Meshes (>5000 vertices)
            # Grund: Array↔List overhead dominiert bei kleinen Datasets (siehe Profiling)
            if NUMPY_AVAILABLE and len(coords) >= 5000:
                return apply_offset_fast(coords, (rx, ry, rz))
            else:
                # Python-Fallback (schneller für typische CityGML-Gebäude)
                out = []
                for (x, y, z) in coords:
                    out.append((x - rx, y - ry, z - rz))
                return out
        else:
            if not bake_implicit:
                # implizite Template-Geometrie: keine Implicit-Transformation anwenden
                # und keinen Datei-Offset (ref_origin) abziehen (Template ist lokal).
                return coords
            else:
                # implizite Geometrie: volle 3D-Transformation
                # OPTIMIZATION: numpy nur bei großen Instanz-Meshes (>5000 vertices)
                # Realistisch: Templates haben meist <1000 vertices
                if NUMPY_AVAILABLE and len(coords) >= 5000:
                    out = transform_coordinates_fast(coords, M, offset=(rx, ry, rz))
                else:
                    # Python-Fallback (Standard für CityGML)
                    out = []
                    m00, m01, m02, m03 = M[0]
                    m10, m11, m12, m13 = M[1]
                    m20, m21, m22, m23 = M[2]
                    for (x, y, z) in coords:
                        X = m00 * x + m01 * y + m02 * z + m03
                        Y = m10 * x + m11 * y + m12 * z + m13
                        Z = m20 * x + m21 * y + m22 * z + m23
                        out.append((X - rx, Y - ry, Z - rz))

        # schließenden Duplikatpunkt entfernen
        if len(out) >= 4 and all(abs(out[0][k] - out[-1][k]) < 1e-9 for k in range(3)):
            out = out[:-1]
        # direkt aufeinanderfolgende Duplikate entfernen
        clean = []
        for p in out:
            if not clean or any(abs(p[k] - clean[-1][k]) > 1e-12 for k in range(3)):
                clean.append(p)
        return clean

    """ verts, faces, surf_labels, vmap = [], [], [], {}

    def add_vertex(co):
        idx = vmap.get(co)
        if idx is None:
            idx = len(verts)
            verts.append(co)
            vmap[co] = idx
        return idx """
    
    verts, faces, surf_labels = [], [], []

    def add_vertex(co):
        idx = len(verts)
        verts.append(co)
        return idx

    # Map:
    # - Polygon-ID -> con:SurfaceTyp (RoofSurface, WallSurface, GroundSurface, etc.)
    # - Polygon-ID -> GenericAttributes des zugehörigen Surface
    # - Polygon-ID -> Surface-gml:id (für GenericAttributeSet mit gen:codeSpace)
    # - Polygon-ID -> MultiSurface-gml:id (für Material-Custom-Property)
    # - Polygon-ID -> CompositeSurface-gml:id (für SurfaceTyp="CompositeSurface")
    multisurface_id_by_poly_id = {}
    compositesurface_id_by_poly_id = {}
    poly_label_by_id = {}
    poly_surface_attrs_by_id = {}
    surface_id_by_poly_id = {}
    filling_parent_surface_id_by_poly_id = {}  # NEU: Parent-Surface-ID für fillingSurface-Kinder (Door/Window)
    
    # NEU: Tracking für Polygone aus thematischen Surfaces (boundary, buildingInstallation)
    # um Duplikate aus lod2Solid zu vermeiden
    thematic_surface_poly_ids = set()
    poly_also_in_solid = {}  # poly_id -> True (wenn Polygon auch in Solid vorkommt)
    poly_has_surface = {}  # OID des Polygon-Elements -> True (wenn unter thematischer Surface)
    poly_source = {}  # OID -> "main_feature" oder "sub_feature" (BuildingInstallation etc.)
    poly_in_shell = {}  # OID -> True (wenn Polygon aus Solid/Shell-Struktur kommt)
    
    # OID-Backfill für Polygone OHNE gml:id (wichtig für BuildingInstallation!)
    poly_label_by_oid = {}  # OID -> SurfaceTyp (für Polygone ohne gml:id)

    # WICHTIG: Installationen sind KEINE BoundarySurfaces!
    INSTALL_TYPES = {
        "BuildingInstallation", "IntBuildingInstallation",
        "BridgeInstallation", "IntBridgeInstallation",
        "BridgeConstructionElement", "IntBridgeConstructionElement",
        "TunnelInstallation", "IntTunnelInstallation",
    }

    DIRECT_SUBFEATURE_TYPES = {
        "BuildingPart", "BridgePart", "TunnelPart",
        "BuildingInstallation", "IntBuildingInstallation",
        "BridgeInstallation", "IntBridgeInstallation",
        "TunnelInstallation", "IntTunnelInstallation",
        "BuildingConstructiveElement", "BridgeConstructiveElement",
        "BridgeConstructionElement", "TunnelConstructiveElement",
        "BuildingFurniture", "BridgeFurniture", "TunnelFurniture",
        "BuildingRoom", "BridgeRoom", "HollowSpace", "Storey",
    }

    # Subfeature relation mapping for CityGML 3.0
    # Maps inline subfeature local names to (namespace_prefix, relation_element_name)
    SUBFEATURE_RELATIONS = {
        "BuildingPart": ("bldg", "buildingPart"),
        "BridgePart": ("brid", "bridgePart"),
        "TunnelPart": ("tun", "tunnelPart"),
        "BuildingInstallation": ("bldg", "buildingInstallation"),
        "BuildingFurniture": ("bldg", "buildingFurniture"),
        "BridgeInstallation": ("brid", "bridgeInstallation"),
        "BridgeConstructionElement": ("brid", "bridgeConstructionElement"),
        "BridgeFurniture": ("brid", "bridgeFurniture"),
        "TunnelInstallation": ("tun", "tunnelInstallation"),
        "TunnelFurniture": ("tun", "tunnelFurniture"),
    }

    # Feature ID for subfeature context
    _feat_id_raw = get_attr(feat_el, "gml", "id") or ""
    _feat_id_norm = _norm_id(_feat_id_raw) if _feat_id_raw else ""

    def _subfeature_context_for_polygon(poly_el) -> dict:
        """
        If a polygon belongs to an inline subfeature of the current feature
        (BuildingPart/BridgePart/TunnelPart/Installation/Furniture/...),
        return stable metadata for export-side reconstruction.
        """
        parent = poly_el.getparent() if hasattr(poly_el, 'getparent') else None

        while parent is not None and parent is not feat_el:
            ln = localname(getattr(parent, "tag", ""))
            relation_info = SUBFEATURE_RELATIONS.get(ln)
            if relation_info:
                rel_ns, rel_local = relation_info
                sub_id_raw = get_attr(parent, "gml", "id") or ""
                sub_id_norm = _norm_id(sub_id_raw) if sub_id_raw else f"{_feat_id_norm}_{ln}_{id(parent)}"
                attrs = {
                    "cgml3_subfeature_type": ln,
                    "cgml3_subfeature_id": sub_id_norm,
                    "cgml3_subfeature_parent_id": _feat_id_norm,
                    "cgml3_subfeature_relation": rel_local,
                    "cgml3_subfeature_namespace": rel_ns,
                }
                return attrs
            parent = parent.getparent() if hasattr(parent, 'getparent') else None

        return {}

    def _opening_parent_surface_context(opening_el):
        """Return the enclosing parent surface id/type for Door/Window elements."""
        if not _is_filling_opening_surface(opening_el):
            return "", ""

        parent = opening_el.getparent() if hasattr(opening_el, 'getparent') else None
        while parent is not None and parent is not feat_el:
            if (
                not _is_filling_opening_surface(parent)
                and (_is_con_surface(parent) or _is_thematic_boundary_surface(parent))
            ):
                parent_local = localname(parent.tag)
                parent_id_raw = get_attr(parent, "gml", "id") or ""
                parent_id_norm = _norm_id(parent_id_raw) if parent_id_raw else ""
                return parent_id_norm, parent_local
            parent = parent.getparent() if hasattr(parent, 'getparent') else None

        return "", ""

    def _enrich_opening_surface_attrs(surf_el, surf_attrs):
        enriched = dict(surf_attrs or {})
        parent_id, parent_type = _opening_parent_surface_context(surf_el)
        if parent_id:
            enriched.setdefault("filling_parent_surface_id", parent_id)
            enriched.setdefault("opening_surface_id", parent_id)
        if parent_type:
            enriched.setdefault("BoundarySurfaceType", parent_type)
            enriched.setdefault("opening_surface_type", parent_type)
        return enriched

    def _find_surface_parent(poly_el):
        """Sucht in der Eltern-Hierarchie nach einem Surface-Typ-Element (WallSurface, RoofSurface, etc.)"""
        parent = poly_el.getparent() if hasattr(poly_el, 'getparent') else None
        while parent is not None:
            if _is_filling_opening_surface(parent) or _is_con_surface(parent) or _is_thematic_boundary_surface(parent):
                return parent
            parent = parent.getparent() if hasattr(parent, 'getparent') else None
        return None

    # NOTE: avoid noisy debug counts in normal operation

    for surf in feat_el.iter():
        # Skip elements inside excluded subtrees (e.g. Storey geometry in parent Building)
        if _is_excluded_element(surf):
            continue
        surf_local = localname(surf.tag)
        
        # In CityGML 2.x liegen Roof/Wall/GroundSurface etc. im bldg:-Namespace,
        # in CityGML 3.x häufig im con:-Namespace. Beides soll Polygon-Labels setzen.
        # NUR echte thematische Surfaces, NICHT die Installations-Features selbst
        is_thematic_surface = _is_filling_opening_surface(surf) or _is_con_surface(surf) or _is_thematic_boundary_surface(surf)
        
        if is_thematic_surface:
            surf_attrs = _parse_surface_generic_attributes(surf)
            surf_attrs = _enrich_opening_surface_attrs(surf, surf_attrs)

            # gml:id der Surface merken (normalisiert)
            surf_id_raw = get_attr(surf, "gml", "id") or ""
            surf_id_norm = _norm_id(surf_id_raw) if surf_id_raw else ""
            
            # NOTE: avoid noisy debug counts in normal operation

            # Suche nach allen Polygonen unterhalb dieser Surface
            # Verwende .iter() statt .findall() für bessere Kompatibilität mit Default-Namespaces
            for el in surf.iter():
                if not isinstance(el.tag, str):
                    continue
                if localname(el.tag) != "Polygon":
                    continue
                
                oid = id(el)
                pid = get_attr(el, "gml", "id")
                
                # IMMER OID-Mapping setzen (für Polygone ohne gml:id)
                poly_label_by_oid[oid] = surf_local
                poly_has_surface[oid] = True
                
                # Nur wenn gml:id vorhanden: zusätzlich ID-Mapping
                if pid:
                    poly_label_by_id[pid] = surf_local
                    thematic_surface_poly_ids.add(pid)

                    if surf_id_norm:
                        # fillingSurface-Tracking: Wenn dieses Polygon bereits einer
                        # anderen Surface zugeordnet war (z.B. WallSurface), speichere
                        # deren ID als Parent-Surface für spaeteren Export als con:fillingSurface.
                        _prev_sid = surface_id_by_poly_id.get(pid, "")
                        if _prev_sid and _prev_sid != surf_id_norm:
                            filling_parent_surface_id_by_poly_id[pid] = _prev_sid
                        surface_id_by_poly_id[pid] = surf_id_norm

                    if surf_attrs:
                        # ggf. vorhandene Attribute ergänzen/überschreiben
                        prev = poly_surface_attrs_by_id.get(pid, {})
                        merged = dict(prev)
                        merged.update(surf_attrs)
                        poly_surface_attrs_by_id[pid] = merged

                    # Subfeature context: store relation metadata for roundtrip export
                    subfeature_attrs = _subfeature_context_for_polygon(el)
                    if subfeature_attrs:
                        prev = poly_surface_attrs_by_id.get(pid, {})
                        merged = dict(prev)
                        merged.update(subfeature_attrs)
                        poly_surface_attrs_by_id[pid] = merged


    polys = []
    seen = set()
    
    # NOTE: avoid noisy debug counters in normal operation

    def _push(poly_el, srs_ctx):
        if poly_el is None:
            return
        oid = id(poly_el)
        if oid in seen:
            return
        if _is_excluded_element(poly_el) or _has_excluded_gml_id(poly_el):
            return
        seen.add(oid)
        polys.append((poly_el, srs_ctx))

    # --- XLink resolution for OrientableSurface/baseSurface references ---
    # CityGML 3.0 often uses: <gml:OrientableSurface><gml:baseSurface xlink:href="#PolyID..."/></gml:OrientableSurface>
    # We need to resolve these XLinks to get the actual Polygon geometries
    if GML_ID_LOOKUP:
        import copy
        resolved_count = 0
        
        # Find all baseSurface elements with xlink:href
        for base_surf_el in feat_el.findall(".//gml:baseSurface", NS):
            if _is_excluded_element(base_surf_el):
                continue
            href = base_surf_el.get(XLINK_HREF)
            if not href:
                continue
            
            # Already has children? Skip (already resolved or inline geometry)
            if list(base_surf_el):
                continue
            
            # Resolve XLink
            gid = _norm_id(href)
            if not gid:
                continue
            if gid in _exclude_gml_ids:
                continue
            
            geom_src = GML_ID_LOOKUP.get(gid)
            if geom_src is None:
                continue
            
            # Copy referenced geometry into baseSurface element
            geom_copy = copy.deepcopy(geom_src)
            base_surf_el.append(geom_copy)
            resolved_count += 1
        
        # NOTE: avoid noisy debug prints in normal operation

    # --- relativeGeometry @xlink:href → referenzierte Geometrie anhängen ---
    if GML_ID_LOOKUP:
        import copy
        # Suche nach ImplicitGeometry (mit und ohne core: Präfix)
        impl_elements = list(feat_el.findall(".//core:ImplicitGeometry", NS))
        
        # Fallback: unprefixed ImplicitGeometry im Default-NS
        for el in feat_el.iter():
            if not isinstance(el.tag, str):
                continue
            tag_local = el.tag.split("}")[-1]
            if tag_local == "ImplicitGeometry":
                tag_ns = el.tag.split("}")[0].strip("{") if "}" in el.tag else ""
                if not tag_ns or tag_ns == NS.get("core", ""):
                    if el not in impl_elements:
                        impl_elements.append(el)
        
        for impl in impl_elements:
            if _is_excluded_element(impl):
                continue
            # Suche relativeGeometry und relativeGMLGeometry (CityGML 2.0 compatibility)
            rel_elements = list(impl.findall(".//core:relativeGeometry", NS))
            for child in impl.iter():
                if not isinstance(child.tag, str):
                    continue
                tag_local = child.tag.split("}")[-1]
                # CityGML 3.0: relativeGeometry, CityGML 2.0: relativeGMLGeometry
                if tag_local in ("relativeGeometry", "relativeGMLGeometry"):
                    tag_ns = child.tag.split("}")[0].strip("{") if "}" in child.tag else ""
                    if not tag_ns or tag_ns == NS.get("core", ""):
                        if child not in rel_elements:
                            rel_elements.append(child)
            
            for rel in rel_elements:
                if _is_excluded_element(rel):
                    continue
                href = rel.get(XLINK_HREF)
                if not href:
                    continue
                gid = _norm_id(href)
                if not gid:
                    continue
                if gid in _exclude_gml_ids:
                    continue
                geom_src = GML_ID_LOOKUP.get(gid)
                if geom_src is None:
                    continue
                # Nur anhängen, wenn relativeGeometry bisher keine Kinder hat
                if list(rel):
                    continue
                geom_copy = copy.deepcopy(geom_src)
                rel.append(geom_copy)

    # MultiSurface
    for ms in feat_el.findall(".//gml:MultiSurface", NS):
        # Skip elements inside excluded subtrees
        if _is_excluded_element(ms):
            continue
        # LOD-Filter: Check parent element for LOD designation
        if lod_filter is not None:
            parent = ms.getparent()
            if parent is not None:
                parent_tag = localname(parent.tag) if hasattr(parent, 'tag') else ''
                # Check if parent is a LOD-specific element (e.g., lod2MultiSurface)
                if parent_tag.startswith('lod') and len(parent_tag) > 3:
                    try:
                        lod_num = int(parent_tag[3])  # Extract LOD number from 'lod2MultiSurface'
                        if lod_num not in lod_filter:
                            continue  # Skip this LOD
                    except (ValueError, IndexError):
                        pass  # Not a LOD-specific element, include it
        
        # Check if MultiSurface is inside a sub-feature (BuildingInstallation etc.)
        is_sub_feature = False
        sub_feature_label = None
        check_parent = ms.getparent() if hasattr(ms, 'getparent') else None
        while check_parent is not None and check_parent != feat_el:
            check_parent_ln = localname(check_parent.tag)
            if check_parent_ln in INSTALL_TYPES:
                is_sub_feature = True
                sub_feature_label = check_parent_ln
                break
            check_parent = check_parent.getparent() if hasattr(check_parent, 'getparent') else None
        
        srs_ms = ms.get("srsName") or default_srs

        # gml:id der MultiSurface merken (normalisiert)
        ms_id_raw = get_attr(ms, "gml", "id") or ""
        ms_id_norm = _norm_id(ms_id_raw) if ms_id_raw else ""

        # OPTIMIZED: Polygon/Triangle/Rectangle in einem einzigen iter()-Durchlauf sammeln
        # statt 3 separate findall()-Aufrufe pro surfaceMember
        _src = "sub_feature" if is_sub_feature else "main_feature"
        for _el in ms.findall(".//gml:surfaceMember", NS):
            for _geom in _el.iter():
                _gln = localname(getattr(_geom, 'tag', ''))
                if _gln not in ("Polygon", "Triangle", "Rectangle"):
                    continue
                _push(_geom, srs_ms)
                poly_source[id(_geom)] = _src
                if is_sub_feature and sub_feature_label:
                    poly_label_by_oid[id(_geom)] = sub_feature_label
                if ms_id_norm and _gln == "Polygon":
                    pid = get_attr(_geom, "gml", "id")
                    if pid:
                        multisurface_id_by_poly_id[pid] = ms_id_norm
                        # Subfeature context for polygons not covered by thematic surface loop
                        if pid not in poly_surface_attrs_by_id or "cgml3_subfeature_type" not in poly_surface_attrs_by_id.get(pid, {}):
                            subfeature_attrs = _subfeature_context_for_polygon(_geom)
                            if subfeature_attrs:
                                prev = poly_surface_attrs_by_id.get(pid, {})
                                merged = dict(prev)
                                merged.update(subfeature_attrs)
                                poly_surface_attrs_by_id[pid] = merged

    # CompositeSurface
    for cs in feat_el.findall(".//gml:CompositeSurface", NS):
        # Skip elements inside excluded subtrees
        if _is_excluded_element(cs):
            continue
        # LOD filter - check parent element
        if lod_filter is not None:
            parent = cs.getparent()
            if parent is not None:
                parent_tag = localname(parent.tag)
                if parent_tag.startswith("lod"):
                    try:
                        lod_num = int(parent_tag[3])  # Extract digit from "lodXCompositeSurface"
                        if lod_num not in lod_filter:
                            continue  # Skip this CompositeSurface
                    except (ValueError, IndexError):
                        pass
        
        # Check if CompositeSurface is inside a sub-feature (BuildingInstallation etc.)
        is_sub_feature = False
        sub_feature_label = None
        check_parent = cs.getparent() if hasattr(cs, 'getparent') else None
        while check_parent is not None and check_parent != feat_el:
            check_parent_ln = localname(check_parent.tag)
            if check_parent_ln in INSTALL_TYPES:
                is_sub_feature = True
                sub_feature_label = check_parent_ln
                break
            check_parent = check_parent.getparent() if hasattr(check_parent, 'getparent') else None
        
        srs_cs = cs.get("srsName") or default_srs

        # gml:id der CompositeSurface merken (normalisiert)
        cs_id_raw = get_attr(cs, "gml", "id") or ""
        cs_id_norm = _norm_id(cs_id_raw) if cs_id_raw else ""

        # OPTIMIZED: Polygon/Triangle/Rectangle in einem einzigen iter()-Durchlauf
        _src = "sub_feature" if is_sub_feature else "main_feature"
        for _el in cs.findall(".//gml:surfaceMember", NS):
            for _geom in _el.iter():
                _gln = localname(getattr(_geom, 'tag', ''))
                if _gln not in ("Polygon", "Triangle", "Rectangle"):
                    continue
                _push(_geom, srs_cs)
                poly_source[id(_geom)] = _src
                if is_sub_feature and sub_feature_label:
                    poly_label_by_oid[id(_geom)] = sub_feature_label
                if cs_id_norm and _gln == "Polygon":
                    pid = get_attr(_geom, "gml", "id")
                    if pid:
                        compositesurface_id_by_poly_id[pid] = cs_id_norm

    # Solid
    # Unterscheide zwischen direkten lod*Solid des Haupt-Features und verschachtelten Solids in Sub-Features
    for solid in feat_el.findall(".//gml:Solid", NS):
        # Skip elements inside excluded subtrees
        if _is_excluded_element(solid):
            continue
        # LOD filter - check parent element
        if lod_filter is not None:
            parent = solid.getparent()
            if parent is not None:
                parent_tag = localname(parent.tag)
                if parent_tag.startswith("lod"):
                    try:
                        lod_num = int(parent_tag[3])  # Extract digit from "lodXSolid"
                        if lod_num not in lod_filter:
                            continue  # Skip this Solid
                    except (ValueError, IndexError):
                        pass
        
        # Prüfe, ob dieses Solid in einem Sub-Feature (Installation etc.) liegt
        is_sub_feature = False
        sub_feature_label = None
        check_parent = solid.getparent() if hasattr(solid, 'getparent') else None
        while check_parent is not None and check_parent != feat_el:
            check_parent_ln = localname(check_parent.tag)
            if check_parent_ln in INSTALL_TYPES:
                is_sub_feature = True
                sub_feature_label = check_parent_ln  # z.B. "BuildingInstallation"
                break
            check_parent = check_parent.getparent() if hasattr(check_parent, 'getparent') else None
        
        srs_sd = solid.get("srsName") or default_srs
        
        # Prüfe ob dieses Solid eine Shell-Struktur hat (für Export wichtig)
        has_shell = solid.find(".//gml:exterior//gml:Shell", NS) is not None
        
        # OPTIMIZED: Polygon/Triangle/Rectangle in einem einzigen iter()-Durchlauf
        _src = "sub_feature" if is_sub_feature else "main_feature"
        for _el in solid.findall(".//gml:surfaceMember", NS):
            for _geom in _el.iter():
                _gln = localname(getattr(_geom, 'tag', ''))
                if _gln not in ("Polygon", "Triangle", "Rectangle"):
                    continue
                _push(_geom, srs_sd)
                poly_source[id(_geom)] = _src
                if is_sub_feature and sub_feature_label:
                    poly_label_by_oid[id(_geom)] = sub_feature_label
                if has_shell:
                    poly_in_shell[id(_geom)] = True
    
    # MultiSolid (mehrere Solid-Elemente)
    for multisolid in feat_el.findall(".//gml:MultiSolid", NS):
        # Skip elements inside excluded subtrees
        if _is_excluded_element(multisolid):
            continue
        srs_msd = multisolid.get("srsName") or default_srs
        # solidMember kann mehrere Solid-Elemente enthalten
        for solid in multisolid.findall(".//gml:solidMember//gml:Solid", NS):
            srs_sd = solid.get("srsName") or srs_msd
            # OPTIMIZED: single-pass statt 3x findall
            for _el in solid.findall(".//gml:surfaceMember", NS):
                for _geom in _el.iter():
                    _gln = localname(getattr(_geom, 'tag', ''))
                    if _gln not in ("Polygon", "Triangle", "Rectangle"):
                        continue
                    pid = get_attr(_geom, "gml", "id")
                    if pid and pid in thematic_surface_poly_ids:
                        if _gln == "Polygon":
                            poly_also_in_solid[pid] = True
                    else:
                        _push(_geom, srs_sd)
        # solidMembers (Plural-Variante, ohne Wrapper)
        for solid in multisolid.findall(".//gml:solidMembers//gml:Solid", NS):
            srs_sd = solid.get("srsName") or srs_msd
            # OPTIMIZED: single-pass statt 3x findall
            for _el in solid.findall(".//gml:surfaceMember", NS):
                for _geom in _el.iter():
                    _gln = localname(getattr(_geom, 'tag', ''))
                    if _gln not in ("Polygon", "Triangle", "Rectangle"):
                        continue
                    pid = get_attr(_geom, "gml", "id")
                    if pid and pid in thematic_surface_poly_ids:
                        if _gln == "Polygon":
                            poly_also_in_solid[pid] = True
                    else:
                        _push(_geom, srs_sd)
    
    # TriangulatedSurface (für TINRelief)
    for tin in feat_el.findall(".//gml:TriangulatedSurface", NS):
        srs_tin = tin.get("srsName") or default_srs
        for p in tin.findall(".//gml:Triangle", NS):
            _push(p, srs_tin)
        # Fallback auf Polygon (falls Triangle nicht verwendet)
        for p in tin.findall(".//gml:Polygon", NS):
            _push(p, srs_tin)
    
    # TINRelief explizit (dem:tin/gml:TriangulatedSurface)
    for tin_relief in feat_el.findall(".//dem:tin", NS):
        for tin_surf in tin_relief.findall(".//gml:TriangulatedSurface", NS):
            srs_tin = tin_surf.get("srsName") or default_srs
            for tri in tin_surf.findall(".//gml:Triangle", NS):
                _push(tri, srs_tin)
            for p in tin_surf.findall(".//gml:Polygon", NS):
                _push(p, srs_tin)

    # Polygone direkt unter feat_el (Fallback)
    for p in feat_el.findall(".//gml:Polygon", NS):
        _push(p, inherit_srs(p, default_srs))
    for t in feat_el.findall(".//gml:Triangle", NS):
        _push(t, inherit_srs(t, default_srs))
    for r in feat_el.findall(".//gml:Rectangle", NS):
        _push(r, inherit_srs(r, default_srs))
    
    # NOTE: avoid noisy debug prints in normal operation


    # Duplikat-Filter (nach CityGML 2 Vorbild):
    # ZWEITER DURCHLAUF: Jetzt wo alle Polygone gesammelt sind, prüfen wir für jedes,
    # ob es unter einer thematischen Surface liegt.
    # Dies ist notwendig, weil in CityGML 3.0 die Reihenfolge boundary->lod2Solid->buildingInstallation ist,
    # während in CityGML 2.0 die Reihenfolge lod2Solid->buildingInstallation->boundedBy ist.
    for p, _ in polys:
        parent = _find_surface_parent(p)
        if parent is not None:
            poly_has_surface[id(p)] = True

    def _has_boundary_surfaces():
        """Prüft, ob echte thematische BoundarySurfaces (RoofSurface/WallSurface/GroundSurface) vorhanden sind.
        
        Installations (BuildingInstallation etc.) zählen NICHT als BoundarySurfaces,
        da sie eigenständige semantische Objekte sind.
        """
        # Liste der echten BoundarySurface-Typen (con: und bldg: Namespace)
        BOUNDARY_SURFACE_TYPES = {
            "RoofSurface", "WallSurface", "GroundSurface",
            "CeilingSurface", "OuterCeilingSurface", "InteriorWallSurface",
            "FloorSurface", "OuterFloorSurface", "ClosureSurface",
            # Bridge/Tunnel-Varianten
            "BridgeRoofSurface", "BridgeWallSurface", "BridgeGroundSurface",
            "TunnelRoofSurface", "TunnelWallSurface", "TunnelGroundSurface",
            "TunnelCeilingSurface", "TunnelFloorSurface",
        }
        
        for p, _ in polys:
            if poly_has_surface.get(id(p), False):
                # Prüfe, ob das Polygon unter einer echten BoundarySurface liegt
                parent = _find_surface_parent(p)
                if parent is not None:
                    parent_ln = localname(parent.tag)
                    # Nur echte BoundarySurfaces zählen
                    if parent_ln in BOUNDARY_SURFACE_TYPES:
                        return True
        return False

    def _polygon_coord_signature(poly_el, srs_ctx):
        """Stable coordinate signature used only for duplicate filtering."""
        try:
            srs_poly = inherit_srs(poly_el, srs_ctx) or default_srs
            if localname(poly_el.tag) == "Surface":
                rings = _rings_from_surface(poly_el, srs_poly)
            else:
                rings = _rings_from_polygon(poly_el, srs_poly)
            if not rings:
                return None
            coords = _apply_transform(poly_el, rings[0][0])
            if len(coords) < 3:
                return None
            return tuple(sorted(
                (round(float(x), 6), round(float(y), 6), round(float(z), 6))
                for x, y, z in coords
            ))
        except Exception:
            return None

    if polys and _has_boundary_surfaces():
        # Sobald mindestens eine echte thematische BoundarySurface existiert,
        # filtern wir Duplikate aus dem Haupt-Feature lod2Solid.
        # Sub-Feature-Polygone (BuildingInstallation etc.) bleiben IMMER erhalten!
        #
        # Wenn das aktuelle Feature selbst ein CityGML-3-Subfeature ist (z.B.
        # ein BuildingPart aus Join Object Parts), darf seine direkte
        # lodXMultiSurface nicht pauschal entfernt werden. Manche Exporte
        # enthalten nur einzelne boundary/ClosureSurface-Flaechen und die
        # restliche Part-Geometrie direkt in lodXMultiSurface.
        preserve_direct_subfeature_geometry = localname(feat_el.tag) in DIRECT_SUBFEATURE_TYPES
        thematic_signatures = set()
        if preserve_direct_subfeature_geometry:
            for p, srs in polys:
                if poly_has_surface.get(id(p), False):
                    sig = _polygon_coord_signature(p, srs)
                    if sig:
                        thematic_signatures.add(sig)

        filtered = []
        for p, srs in polys:
            oid = id(p)
            # Sub-Feature-Polygone immer behalten
            if poly_source.get(oid) == "sub_feature":
                filtered.append((p, srs))
            # Haupt-Feature-Polygone nur behalten, wenn sie unter thematischer Surface liegen
            elif poly_has_surface.get(oid, False):
                filtered.append((p, srs))
            elif preserve_direct_subfeature_geometry:
                sig = _polygon_coord_signature(p, srs)
                if not sig or sig not in thematic_signatures:
                    filtered.append((p, srs))
            # Sonst: Polygon aus Haupt-Feature ohne thematischen Kontext -> Duplikat, entfernen
        polys = filtered

    # Rings → Vertices/Faces
    for poly_el, srs_ctx in polys:
        poly_id = get_attr(poly_el, "gml", "id") or ""
        srs_poly = inherit_srs(poly_el, srs_ctx) or default_srs

        if localname(poly_el.tag) == "Surface":
            rings = _rings_from_surface(poly_el, srs_poly)
        else:
            rings = _rings_from_polygon(poly_el, srs_poly)

        if not rings:
            continue

        # Label-Priorität:
        # 1. poly_label_by_id (wenn gml:id vorhanden)
        # 2. poly_label_by_oid (für Polygone ohne gml:id, z.B. BuildingInstallation)
        # 3. Feature localname als Fallback
        label = poly_label_by_id.get(poly_id) or poly_label_by_oid.get(id(poly_el))
        if not label:
            parent_surface = _find_surface_parent(poly_el)
            if parent_surface is not None:
                label = localname(parent_surface.tag)
                if poly_id:
                    poly_label_by_id.setdefault(poly_id, label)
                    parent_sid_raw = get_attr(parent_surface, "gml", "id") or ""
                    parent_sid_norm = _norm_id(parent_sid_raw) if parent_sid_raw else ""
                    if parent_sid_norm:
                        surface_id_by_poly_id.setdefault(poly_id, parent_sid_norm)
                    parent_attrs = _parse_surface_generic_attributes(parent_surface)
                    parent_attrs = _enrich_opening_surface_attrs(parent_surface, parent_attrs)
                    if parent_attrs:
                        prev_attrs = poly_surface_attrs_by_id.get(poly_id, {})
                        merged_attrs = dict(prev_attrs)
                        merged_attrs.update(parent_attrs)
                        poly_surface_attrs_by_id[poly_id] = merged_attrs
                        if "filling_parent_surface_id" in parent_attrs:
                            filling_parent_surface_id_by_poly_id.setdefault(
                                poly_id,
                                str(parent_attrs.get("filling_parent_surface_id") or ""),
                            )
        if not label:
            label = localname(feat_el.tag)

        for ring in rings:
            if len(ring) >= 3:
                coords, ring_id, is_gml_interior_ring = ring[0], ring[1], bool(ring[2])
            else:
                coords, ring_id = ring
                is_gml_interior_ring = False
            clean = _apply_transform(poly_el, coords)
            if len(clean) < 3:
                continue
            idxs = [add_vertex(c) for c in clean]
            faces.append(idxs)
            lod_num = _get_lod_from_parent(poly_el)

            # Prüfe ob dieses Polygon aus Shell-Struktur kommt
            in_shell = poly_in_shell.get(id(poly_el), False)

            # surf_labels: (label, poly_id, ring_id, srs_poly, idxs, lod_num, in_shell, is_gml_interior_ring)
            surf_labels.append((label, poly_id, ring_id, srs_poly or "", tuple(idxs), lod_num, in_shell, is_gml_interior_ring))
    
    # NOTE: avoid noisy debug prints in normal operation

    if include_surface_attrs:
        # neu: surface_id_by_poly_id, multisurface_id_by_poly_id, compositesurface_id_by_poly_id
        # UND poly_also_in_solid und filling_parent_surface_id_by_poly_id zurückgeben
        return (
            faces,
            verts,
            surf_labels,
            poly_surface_attrs_by_id,
            surface_id_by_poly_id,
            multisurface_id_by_poly_id,
            compositesurface_id_by_poly_id,
            poly_also_in_solid,  # NEU: Markierung für Polygone die auch in Solid vorkommen
            filling_parent_surface_id_by_poly_id,  # NEU: Parent-Surface-ID für fillingSurface-Kinder
        )
    return faces, verts, surf_labels


def parse_points(feat_el, default_srs="EPSG:25832", ref_origin=(0, 0, 0)):
    """
    Extrahiert gml:Point und gml:MultiPoint Geometrien aus einem CityGML-Feature.
    
    Args:
        feat_el: XML-Element des CityGML-Features
        default_srs: Standard-SRS falls nicht angegeben
        ref_origin: Referenz-Ursprung (rx, ry, rz) für Offset-Korrektur
        
    Returns:
        List of tuples: [(x, y, z, point_id, srs), ...]
    """
    rx, ry, rz = ref_origin
    points = []
    
    def _parse_point_coords(point_el, srs_ctx):
        """Extrahiert Koordinaten aus einem gml:Point Element."""
        srs = inherit_srs(point_el, srs_ctx) or default_srs
        
        # gml:pos direkt im Point
        pos_el = point_el.find(".//gml:pos", NS)
        if pos_el is not None:
            coords_str = (pos_el.text or "").strip()
            if coords_str:
                parts = coords_str.split()
                if len(parts) >= 2:
                    # Prüfen ob Lat/Lon-Reihenfolge
                    if is_axis_order_latlon(srs):
                        # Lat, Lon, [Height] → Lon, Lat, [Height]
                        if len(parts) == 2:
                            x, y = float(parts[1]), float(parts[0])
                            z = 0.0
                        else:
                            x, y, z = float(parts[1]), float(parts[0]), float(parts[2])
                    else:
                        # normale X, Y, [Z] Reihenfolge
                        if len(parts) == 2:
                            x, y = float(parts[0]), float(parts[1])
                            z = 0.0
                        else:
                            x, y, z = float(parts[0]), float(parts[1]), float(parts[2])
                    
                    # Offset-Korrektur
                    return (x - rx, y - ry, z - rz), srs
        
        # gml:coordinates (deprecated in GML 3.2 but still supported)
        coords_el = point_el.find(".//gml:coordinates", NS)
        if coords_el is not None:
            coords_str = (coords_el.text or "").strip()
            if coords_str:
                parts = coords_str.replace(',', ' ').split()
                if len(parts) >= 2:
                    if len(parts) == 2:
                        x, y = float(parts[0]), float(parts[1])
                        z = 0.0
                    else:
                        x, y, z = float(parts[0]), float(parts[1]), float(parts[2])
                    return (x - rx, y - ry, z - rz), srs
        
        return None, srs
    
    # gml:Point direkt
    for pt in feat_el.findall(".//gml:Point", NS):
        pt_id = get_attr(pt, "gml", "id") or ""
        srs_pt = pt.get("srsName") or default_srs
        coords, srs = _parse_point_coords(pt, srs_pt)
        if coords:
            points.append((coords[0], coords[1], coords[2], pt_id, srs))
    
    # gml:MultiPoint
    for mpt in feat_el.findall(".//gml:MultiPoint", NS):
        srs_mpt = mpt.get("srsName") or default_srs
        
        # gml:pointMember/gml:Point
        for pm in mpt.findall(".//gml:pointMember", NS):
            pt = pm.find(".//gml:Point", NS)
            if pt is not None:
                pt_id = get_attr(pt, "gml", "id") or ""
                coords, srs = _parse_point_coords(pt, srs_mpt)
                if coords:
                    points.append((coords[0], coords[1], coords[2], pt_id, srs))
        
        # gml:pointMembers (container for multiple points)
        for pms in mpt.findall(".//gml:pointMembers", NS):
            for pt in pms.findall(".//gml:Point", NS):
                pt_id = get_attr(pt, "gml", "id") or ""
                coords, srs = _parse_point_coords(pt, srs_mpt)
                if coords:
                    points.append((coords[0], coords[1], coords[2], pt_id, srs))
    
    return points


def parse_linestrings(feat_el, default_srs="EPSG:25832", ref_origin=(0, 0, 0)):
    """
    Extrahiert gml:LineString und gml:MultiCurve Geometrien aus einem CityGML-Feature.
    
    Args:
        feat_el: XML-Element des CityGML-Features
        default_srs: Standard-SRS falls nicht angegeben
        ref_origin: Referenz-Ursprung (rx, ry, rz) für Offset-Korrektur
        
    Returns:
        List of tuples: [(coords_list, linestring_id, srs), ...]
        wobei coords_list = [(x1,y1,z1), (x2,y2,z2), ...]
    """
    rx, ry, rz = ref_origin
    linestrings = []
    
    def _parse_linestring_coords(ls_el, srs_ctx):
        """Extrahiert Koordinaten aus einem gml:LineString Element."""
        srs = inherit_srs(ls_el, srs_ctx) or default_srs
        
        # gml:posList (bevorzugt in GML 3.2)
        poslist_el = ls_el.find(".//gml:posList", NS)
        if poslist_el is not None:
            coords = parse_poslist(poslist_el, srs)
            if coords:
                # Offset-Korrektur
                corrected = [(x - rx, y - ry, z - rz) for (x, y, z) in coords]
                return corrected, srs
        
        # gml:pos (mehrere einzelne Punkte)
        pos_elements = ls_el.findall(".//gml:pos", NS)
        if pos_elements:
            coords = []
            for pos_el in pos_elements:
                coords_str = (pos_el.text or "").strip()
                if coords_str:
                    parts = coords_str.split()
                    if len(parts) >= 2:
                        # Prüfen ob Lat/Lon-Reihenfolge
                        if is_axis_order_latlon(srs):
                            if len(parts) == 2:
                                x, y = float(parts[1]), float(parts[0])
                                z = 0.0
                            else:
                                x, y, z = float(parts[1]), float(parts[0]), float(parts[2])
                        else:
                            if len(parts) == 2:
                                x, y = float(parts[0]), float(parts[1])
                                z = 0.0
                            else:
                                x, y, z = float(parts[0]), float(parts[1]), float(parts[2])
                        coords.append((x - rx, y - ry, z - rz))
            if coords:
                return coords, srs
        
        # gml:coordinates (deprecated in GML 3.2 but still supported)
        coords_el = ls_el.find(".//gml:coordinates", NS)
        if coords_el is not None:
            coords_str = (coords_el.text or "").strip()
            if coords_str:
                # Parse coordinates (can be space or comma separated)
                coords = []
                # Replace commas with spaces, then split
                parts = coords_str.replace(',', ' ').split()
                i = 0
                while i < len(parts):
                    if i + 1 < len(parts):
                        x = float(parts[i])
                        y = float(parts[i + 1])
                        z = float(parts[i + 2]) if i + 2 < len(parts) and parts[i + 2].replace('.', '').replace('-', '').isdigit() else 0.0
                        coords.append((x - rx, y - ry, z - rz))
                        i += 3 if i + 2 < len(parts) else 2
                    else:
                        break
                if coords:
                    return coords, srs
        
        return None, srs
    
    # gml:LineString direkt
    for ls in feat_el.findall(".//gml:LineString", NS):
        ls_id = get_attr(ls, "gml", "id") or ""
        srs_ls = ls.get("srsName") or default_srs
        coords, srs = _parse_linestring_coords(ls, srs_ls)
        if coords and len(coords) >= 2:  # LineString benötigt mindestens 2 Punkte
            linestrings.append((coords, ls_id, srs))
    
    # gml:Curve (mit verschiedenen Segment-Typen)
    for curve in feat_el.findall(".//gml:Curve", NS):
        curve_id = get_attr(curve, "gml", "id") or ""
        srs_curve = curve.get("srsName") or default_srs
        
        # gml:segments container
        segments_el = curve.find(".//gml:segments", NS)
        if segments_el is not None:
            # gml:LineStringSegment
            for seg in segments_el.findall(".//gml:LineStringSegment", NS):
                coords, srs = _parse_linestring_coords(seg, srs_curve)
                if coords and len(coords) >= 2:
                    linestrings.append((coords, curve_id, srs))
            
            # gml:Arc
            for seg in segments_el.findall(".//gml:Arc", NS):
                coords = parse_arc_segment(seg, srs_curve, ref_origin)
                if coords and len(coords) >= 2:
                    linestrings.append((coords, curve_id, srs_curve))
            
            # gml:Circle
            for seg in segments_el.findall(".//gml:Circle", NS):
                coords = parse_circle_segment(seg, srs_curve, ref_origin)
                if coords and len(coords) >= 2:
                    linestrings.append((coords, curve_id, srs_curve))
            
            # gml:ArcString
            for seg in segments_el.findall(".//gml:ArcString", NS):
                coords = parse_arcstring_segment(seg, srs_curve, ref_origin)
                if coords and len(coords) >= 2:
                    linestrings.append((coords, curve_id, srs_curve))
            
            # gml:ArcByCenterPoint
            for seg in segments_el.findall(".//gml:ArcByCenterPoint", NS):
                coords = parse_arc_by_centerpoint_segment(seg, srs_curve, ref_origin)
                if coords and len(coords) >= 2:
                    linestrings.append((coords, curve_id, srs_curve))
            
            # gml:CircleByCenterPoint
            for seg in segments_el.findall(".//gml:CircleByCenterPoint", NS):
                coords = parse_circle_by_centerpoint_segment(seg, srs_curve, ref_origin)
                if coords and len(coords) >= 2:
                    linestrings.append((coords, curve_id, srs_curve))
    
    # gml:MultiCurve
    for mc in feat_el.findall(".//gml:MultiCurve", NS):
        srs_mc = mc.get("srsName") or default_srs
        
        # gml:curveMember/gml:LineString
        for cm in mc.findall(".//gml:curveMember", NS):
            ls = cm.find(".//gml:LineString", NS)
            if ls is not None:
                ls_id = get_attr(ls, "gml", "id") or ""
                coords, srs = _parse_linestring_coords(ls, srs_mc)
                if coords and len(coords) >= 2:
                    linestrings.append((coords, ls_id, srs))
            
            # gml:Curve innerhalb curveMember (mit verschiedenen Segment-Typen)
            curve = cm.find(".//gml:Curve", NS)
            if curve is not None:
                curve_id = get_attr(curve, "gml", "id") or ""
                
                segments_el = curve.find(".//gml:segments", NS)
                if segments_el is not None:
                    # LineStringSegment
                    for seg in segments_el.findall(".//gml:LineStringSegment", NS):
                        coords, srs = _parse_linestring_coords(seg, srs_mc)
                        if coords and len(coords) >= 2:
                            linestrings.append((coords, curve_id, srs))
                    
                    # Arc
                    for seg in segments_el.findall(".//gml:Arc", NS):
                        coords = parse_arc_segment(seg, srs_mc, ref_origin)
                        if coords and len(coords) >= 2:
                            linestrings.append((coords, curve_id, srs_mc))
                    
                    # Circle
                    for seg in segments_el.findall(".//gml:Circle", NS):
                        coords = parse_circle_segment(seg, srs_mc, ref_origin)
                        if coords and len(coords) >= 2:
                            linestrings.append((coords, curve_id, srs_mc))
                    
                    # ArcString
                    for seg in segments_el.findall(".//gml:ArcString", NS):
                        coords = parse_arcstring_segment(seg, srs_mc, ref_origin)
                        if coords and len(coords) >= 2:
                            linestrings.append((coords, curve_id, srs_mc))
                    
                    # ArcByCenterPoint
                    for seg in segments_el.findall(".//gml:ArcByCenterPoint", NS):
                        coords = parse_arc_by_centerpoint_segment(seg, srs_mc, ref_origin)
                        if coords and len(coords) >= 2:
                            linestrings.append((coords, curve_id, srs_mc))
                    
                    # CircleByCenterPoint
                    for seg in segments_el.findall(".//gml:CircleByCenterPoint", NS):
                        coords = parse_circle_by_centerpoint_segment(seg, srs_mc, ref_origin)
                        if coords and len(coords) >= 2:
                            linestrings.append((coords, curve_id, srs_mc))
        
        # gml:curveMembers (container for multiple curves)
        for cms in mc.findall(".//gml:curveMembers", NS):
            for ls in cms.findall(".//gml:LineString", NS):
                ls_id = get_attr(ls, "gml", "id") or ""
                coords, srs = _parse_linestring_coords(ls, srs_mc)
                if coords and len(coords) >= 2:
                    linestrings.append((coords, ls_id, srs))
    
    return linestrings
