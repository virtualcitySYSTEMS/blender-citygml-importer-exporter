# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
import bpy, bmesh
import hashlib
from .namespaces import NS
from .xml_utils import localname, get_attr, inherit_srs, parse_poslist, is_axis_order_latlon, _norm_id
from .curve_segments import (
    parse_arc_segment,
    parse_circle_segment,
    parse_arcstring_segment,
    parse_arc_by_centerpoint_segment,
    parse_circle_by_centerpoint_segment,
)

GML_ID_LOOKUP = {}
XLINK_HREF = '{http://www.w3.org/1999/xlink}href'

def _is_con_surface(el):
    """Erkennt thematische Flächen (Boundary Surfaces) in CityGML 2.0.

    CityGML 2.0:
    - Keine con: namespace (Construction-Modul ist CityGML 3.0)
    - Boundary Surfaces sind direkt in bldg:, brid:, tun: Namespaces
    - Z.B. bldg:RoofSurface, bldg:WallSurface, bldg:GroundSurface
    - Auch: brid:RoofSurface, tun:RoofSurface etc.
    
    NICHT: lod2MultiSurface, lod2Solid, etc. (das sind Geometrie-Container!)
    """
    if not isinstance(el.tag, str) or not el.tag.startswith("{"):
        return False
    uri, ln = el.tag[1:].split("}", 1)

    # CityGML 2.0: Boundary Surfaces in Building/Bridge/Tunnel Namespaces
    if uri in (NS.get("bldg"), NS.get("brid"), NS.get("tun")):
        # EXCLUSION: lodXMultiSurface, lodXSolid sind KEINE thematischen Surfaces
        if ln.startswith("lod") and ("MultiSurface" in ln or "Solid" in ln or "MultiCurve" in ln):
            return False

        # Boundary Surface Types (enden mit "Surface")
        if ln.endswith("Surface"):
            return True

        # Auch Installationen und Konstruktionselemente als Flächenträger
        if ln in (
            "BuildingInstallation", "IntBuildingInstallation",
            "BridgeInstallation", "IntBridgeInstallation",
            "BridgeConstructionElement", "IntBridgeConstructionElement",
            "TunnelInstallation", "IntTunnelInstallation"
        ):
            return True

    return False


def _is_opening_surface(el):
    """Erkennt CityGML-2 Opening-Features als eigene Surface-Kontexte."""
    if not isinstance(el.tag, str) or not el.tag.startswith("{"):
        return False
    uri, ln = el.tag[1:].split("}", 1)
    return uri == NS.get("bldg") and ln in ("Window", "Door")

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
        if ln_lower.endswith("doubleattribute"):
            try:
                return float(t)
            except Exception:
                return t
        if ln_lower.endswith("intattribute"):
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
            name = (el.get("name") or "").strip()
            if not name:
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
        # CityGML 2.0 speichert den Attributnamen typischerweise direkt als XML-Attribut
        # (<gen:doubleAttribute name="Flaeche">), nicht als <gen:name>-Kindelement.
        name = (el.get("name") or "").strip()
        if not name:
            name = (el.findtext("./gen:name", default="", namespaces=NS) or "").strip()
        val  = (el.findtext("./gen:value", default="", namespaces=NS) or "").strip()
        if name:
            attrs[name] = _cast(ln, val)

    # CityGML 2.0: No relationToConstruction (that's CityGML 3.0 only)
    
    return attrs

def iter_features(root, gml_id_filter=None, bbox_filter=None, feature_type_filter=None, ns_manager=None):
    """
    Iteriert über alle CityObjects im CityGML 2.0:
    - sammelt CityObjects aus cityObjectMember/gml:featureMember
    - extrahiert verschachtelte Subfeatures (Building/Bridge/Tunnel/Generics/Traffic/Windows/Doors)
    - wendet optional GML-ID-, BBOX- und Feature-Typ-Filter an.
    
    CityGML 2.0: Kein con:, pcl:, dyn:, vers: Namespace
    
    Args:
        root: XML root element
        gml_id_filter: Set of GML IDs to filter
        bbox_filter: [min_x, min_y, max_x, max_y] for spatial filtering
        feature_type_filter: Set of feature type names to include
        ns_manager: Optional DynamicNamespaceManager für dynamische Namespace-Erkennung
    """
    # CityGML 2.0: cityObjectMember (OHNE Namespace-Präfix im Tag!)
    # Das Tag heißt einfach "cityObjectMember" ohne {namespace}
    members = []
    for elem in root:
        tag = elem.tag if isinstance(elem.tag, str) else ""
        # CityGML 2.0: cityObjectMember ohne Namespace-Präfix
        if tag == "cityObjectMember" or tag.endswith("}cityObjectMember"):
            members.append(elem)
        # Fallback: gml:featureMember
        elif tag.endswith("}featureMember") or tag.endswith("}member"):
            members.append(elem)

    # Dynamische Namespace-Erkennung wenn ns_manager übergeben wurde
    if ns_manager:
        from ...common.dynamic_namespaces import DynamicNamespaceManager
        if not isinstance(ns_manager, DynamicNamespaceManager):
            # Fallback: erstelle Manager aus root
            ns_manager = DynamicNamespaceManager(root)
        allowed_ns = tuple(ns_manager.get_all_namespaces_including_ades())
    else:
        # Rückwärtskompatibilität: hardcodierte Namespaces für CityGML 2.0
        allowed_ns = (
            NS["core"], NS["bldg"], NS["brid"], NS["tun"], NS["tran"],
            NS["veg"], NS["wtr"], NS["frn"], NS["grp"],
            NS["luse"], NS["dem"], NS["tex"], NS["gen"]
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
            subs = []

            # Building elements (CityGML 2.0: Room/BuildingInstallation/InteriorFurniture)
            subs += child.findall(".//bldg:Room", NS) + \
                    child.findall(".//bldg:BuildingInstallation", NS) + \
                    child.findall(".//bldg:IntBuildingInstallation", NS) + \
                    child.findall(".//bldg:InteriorFurniture", NS)

            # Bridge elements (CityGML 2.0: BridgePart/BridgeInstallation/BridgeRoom/BridgeConstructionElement)
            subs += child.findall(".//brid:BridgePart", NS) + \
                child.findall(".//brid:BridgeInstallation", NS) + \
                child.findall(".//brid:IntBridgeInstallation", NS) + \
                child.findall(".//brid:BridgeRoom", NS) + \
                child.findall(".//brid:BridgeConstructionElement", NS)

            # Tunnel elements (CityGML 2.0: TunnelPart/HollowSpace/TunnelInstallation)
            subs += child.findall(".//tun:TunnelPart", NS) + \
                    child.findall(".//tun:HollowSpace", NS) + \
                    child.findall(".//tun:TunnelInstallation", NS) + \
                    child.findall(".//tun:IntTunnelInstallation", NS)

            # CityGML 2.0: Keine Generic Spaces (GenericLogicalSpace etc. sind CityGML 3.0)
            # Nur GenericCityObject
            subs += child.findall(".//gen:GenericCityObject", NS)
            
            # CityObjectGroup
            subs += child.findall(".//grp:CityObjectGroup", NS)

            # CityGML 2.0: Keine Dynamizers (CityGML 3.0 feature)

            # CityGML 2.0: Window/Door direkt in Building/BoundarySurface
            subs += child.findall(".//bldg:Window", NS) + \
                    child.findall(".//bldg:Door", NS)

            # Transportation elements (CityGML 2.0)
            subs += child.findall(".//tran:TrafficArea", NS) + \
                    child.findall(".//tran:AuxiliaryTrafficArea", NS)

            # LandUse elements
            subs += child.findall(".//luse:LandUse", NS)

            # -------------------------
            # Parent + Subfeatures
            # -------------------------
            # CityGML 2.0: Keine BridgeConstructiveElement (das ist CityGML 3.0)
            # Buildings: verschachtelte BuildingInstallation-Objekte gehören semantisch zum Building
            # (z.B. bldg:outerBuildingInstallation). Für den Import behandeln wir diese daher NICHT
            # als eigenständige Features, sondern importieren das Top-Level Building/BuildingPart.
            if ns_uri == NS["bldg"] and ln == "Building":
                # Nur direkte Parts über consistsOfBuildingPart, nicht alle .//BuildingPart,
                # um Doppelzählung bei verschachtelten Parts zu vermeiden.
                parts = []
                for rel in child.findall("./bldg:consistsOfBuildingPart", NS):
                    p = rel.find("./bldg:BuildingPart", NS)
                    if p is not None:
                        parts.append(p)
                # BuildingInstallation als eigene Features extrahieren
                installations = []
                for rel in child.findall("./bldg:outerBuildingInstallation", NS):
                    inst = rel.find("./bldg:BuildingInstallation", NS)
                    if inst is not None:
                        installations.append(inst)
                for rel in child.findall("./bldg:interiorBuildingInstallation", NS):
                    inst = rel.find("./bldg:IntBuildingInstallation", NS)
                    if inst is not None:
                        installations.append(inst)
                candidates = [child] + parts + installations

            # CityGML 2.0: Keine BridgeConstructiveElement (das ist CityGML 3.0)
            elif ns_uri == NS["brid"] and ln == "Bridge":
                # Bei Bridges:
                # - Bridge selbst importieren
                # - BridgePart/BridgeRoom importieren
                # - BridgeInstallation, IntBridgeInstallation, BridgeConstructionElement, IntBridgeConstructionElement: KEINE eigenen Features, sondern als Surfaces behandeln
                def is_bridge_installation_type(tag):
                    ln = localname(tag)
                    return ln in ("BridgeInstallation", "IntBridgeInstallation", "BridgeConstructionElement", "IntBridgeConstructionElement")
                subs_filtered = [
                    s for s in subs
                    if not is_bridge_installation_type(s.tag)
                ]
                candidates = [child] + subs_filtered
            elif ns_uri == NS["tun"] and ln == "Tunnel":
                # Bei Tunneln:
                # - Tunnel selbst importieren
                # - TunnelPart/HollowSpace importieren
                # - TunnelInstallation/IntTunnelInstallation nicht als Top-Level-Duplikate importieren
                def is_tunnel_installation_type(tag):
                    ln = localname(tag)
                    return ln in ("TunnelInstallation", "IntTunnelInstallation")
                subs_filtered = [
                    s for s in subs
                    if not is_tunnel_installation_type(s.tag)
                ]
                candidates = [child] + subs_filtered
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
                
                # Feature-Type-Filter (optional)
                if feature_type_filter:
                    feat_ln = localname(feat.tag)
                    if feat_ln not in feature_type_filter:
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
            rings.append((coords, rid))
    # interior(s)
    for lr in surface_el.findall(".//gml:patches//gml:PolygonPatch//gml:interior//gml:LinearRing", NS):
        pos = lr.find(".//gml:posList", NS)
        if pos is None: 
            continue
        coords = parse_poslist(pos, pos.get("srsName") or srs_surface)
        rid = lr.get(f"{{{NS['gml']}}}id") or ""
        rings.append((coords, rid))
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
            rings.append((coords, rid))
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
                    rings.append((coords, rid))
    
    for inter in poly.findall("./gml:interior", NS):
        lr = inter.find(".//gml:LinearRing", NS)
        if lr is None: continue
        
        # Try posList first
        pos = lr.find(".//gml:posList", NS)
        if pos is not None:
            coords = parse_poslist(pos, pos.get("srsName") or srs_poly)
            rid = lr.get(f"{{{NS['gml']}}}id") or ""
            rings.append((coords, rid))
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
                    rings.append((coords, rid))
    return rings


def _get_lod_from_parent(el):
    """Geht die Elternkette hoch und sucht nach einem Tag wie lod2MultiSurface, lod2Geometry, lod2Solid etc. Liefert die LoD-Nummer als int oder None."""
    parent = getattr(el, 'getparent', lambda: None)()
    while parent is not None:
        tag = localname(getattr(parent, 'tag', ''))
        if tag.startswith('lod') and len(tag) > 4 and tag[3].isdigit():
            try:
                return int(tag[3])
            except Exception:
                pass
        parent = getattr(parent, 'getparent', lambda: None)()
    return None

def citygml2_subfeature_excludes(feat_el, feat_ln=None):
    """Return inline child features that must not be folded into the parent mesh."""
    if feat_el is None:
        return []

    feat_ln = feat_ln or localname(getattr(feat_el, "tag", ""))
    excludes = []

    def _add(path):
        try:
            excludes.extend([el for el in feat_el.findall(path, NS) if el is not None])
        except Exception:
            pass

    if feat_ln == "Building":
        _add("./bldg:consistsOfBuildingPart/bldg:BuildingPart")
        _add("./bldg:outerBuildingInstallation/bldg:BuildingInstallation")
        _add("./bldg:interiorBuildingInstallation/bldg:IntBuildingInstallation")
    elif feat_ln == "Bridge":
        _add("./brid:consistsOfBridgePart/brid:BridgePart")
    elif feat_ln == "Tunnel":
        _add("./tun:consistsOfTunnelPart/tun:TunnelPart")
        _add("./tun:consistsOfHollowSpace/tun:HollowSpace")
        _add("./tun:hollowSpaceMember/tun:HollowSpace")

    return excludes


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
    """
    rx, ry, rz = ref_origin

    _exclude_root_gml_ids = set()
    _exclude_gml_ids = set()
    if exclude_subtrees:
        for subtree_root in exclude_subtrees:
            try:
                root_gid = get_attr(subtree_root, "gml", "id") or ""
            except Exception:
                root_gid = ""
            if root_gid:
                _exclude_root_gml_ids.add(_norm_id(root_gid))
            try:
                iterator = subtree_root.iter()
            except Exception:
                iterator = []
            for ex_el in iterator:
                try:
                    ex_gid = get_attr(ex_el, "gml", "id") or ""
                except Exception:
                    ex_gid = ""
                if ex_gid:
                    _exclude_gml_ids.add(_norm_id(ex_gid))

    def _is_excluded_element(el) -> bool:
        if el is None or not _exclude_root_gml_ids:
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
            if isinstance(tag, str) and tag.startswith("{") and localname(tag) == "ImplicitGeometry":
                # 1) transformationMatrix lesen
                tm_el = cur.find(".//{http://www.opengis.net/citygml/3.0}transformationMatrix")
                if tm_el is None:
                    tm_el = cur.find(".//core:transformationMatrix", NS)

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
        M = _implicit_transform_for(elem)
        out = []

        if M is None:
            # normale (explizite) Geometrie
            for (x, y, z) in coords:
                out.append((x - rx, y - ry, z - rz))
        else:
            if not bake_implicit:
                # implizite Template-Geometrie: keine Implicit-Transformation anwenden
                # und keinen Datei-Offset (ref_origin) abziehen (Template ist lokal).
                for (x, y, z) in coords:
                    out.append((x, y, z))
            else:
                # implizite Geometrie: volle 3D-Transformation
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

    # Vertex welding: minimiert Float-Rauschen an gemeinsamen Kanten
    # (wichtig, weil der Exporter BuildingInstallation nach identischen Kanten split/joint)
    VERT_WELD_ND = 5  # 1e-5 in CRS-Einheiten (bei Metern ~0.01 mm)

    verts = []
    faces = []
    surf_labels = []
    vmap = {}

    def _vkey(co):
        return (
            round(float(co[0]), VERT_WELD_ND),
            round(float(co[1]), VERT_WELD_ND),
            round(float(co[2]), VERT_WELD_ND),
        )

    def add_vertex(co):
        key = _vkey(co)
        idx = vmap.get(key)
        if idx is None:
            idx = len(verts)
            verts.append(co)  # Originalwert der ersten Sichtung behalten
            vmap[key] = idx
        return idx

    # Map:
    # - Polygon-ID -> con:SurfaceTyp (RoofSurface, WallSurface, GroundSurface, etc.)
    # - Polygon-ID -> GenericAttributes des zugehörigen Surface
    # - Polygon-ID -> Surface-gml:id (für GenericAttributeSet mit gen:codeSpace)
    # - Polygon-ID -> MultiSurface-gml:id (für Material-Custom-Property)
    # - Polygon-ID -> CompositeSurface-gml:id (für Material-Custom-Property "is_compositesurface_member")
    multisurface_id_by_poly_id = {}
    compositesurface_id_by_poly_id = {}
    poly_label_by_id = {}
    poly_surface_attrs_by_id = {}
    surface_id_by_poly_id = {}
    # Merker: Polygon ist Member einer CompositeSurface (für Duplikat-Filter)
    compositesurface_member_by_oid = {}
    
    # Hilfsfunktion: Finde Surface-Typ-Element in Eltern-Hierarchie
    def _find_surface_parent(poly_el):
        """Sucht in der Eltern-Hierarchie nach einem Surface-Typ-Element (WallSurface, RoofSurface, etc.)"""
        parent = poly_el.getparent()
        while parent is not None:
            if _is_opening_surface(parent) or _is_con_surface(parent):
                return parent
            parent = parent.getparent()
        return None

    def _prefer_label(existing: str | None, candidate: str | None) -> str | None:
        """Wählt deterministisch den 'besseren' Label, falls mehrere Quellen vorhanden sind.

        Problem: Bei manchen CityGML-2 Dateien tauchen dieselben Polygone sowohl in
        thematischen BoundarySurfaces (bldg:RoofSurface/WallSurface/...) als auch
        nochmals in lodXSolid/lodXMultiSurface oder ähnlichen Containern auf.
        Je nach Traversal-Reihenfolge kann dann der 'falsche' Surface-Typ gewinnen.

        Priorität:
        1) Roof/Wall/Ground/Closure/... Surfaces (enden mit 'Surface')
        2) alle anderen Labels
        """
        e = (existing or "").strip()
        c = (candidate or "").strip()
        if not e:
            return c or None
        if not c:
            return e
        opening_labels = {"Window", "Door"}
        e_is_opening = e in opening_labels
        c_is_opening = c in opening_labels
        if e_is_opening and not c_is_opening:
            return e
        if c_is_opening and not e_is_opening:
            return c
        e_is_surface = e.endswith("Surface")
        c_is_surface = c.endswith("Surface")
        if e_is_surface and not c_is_surface:
            return e
        if c_is_surface and not e_is_surface:
            return c
        # beide gleichartig -> bestehenden Wert behalten (stabil)
        return e

    def _merge_surface_attrs(existing: dict | None, incoming: dict | None) -> dict:
        merged = dict(existing or {})
        if incoming:
            merged.update(incoming)
        return merged

    def _remember_parent_surface_context(pid: str, prev_label: str | None, prev_surface_id: str, surf_local: str, surf_attrs: dict | None) -> dict:
        if surf_local not in {"Window", "Door"} or not prev_surface_id:
            return dict(surf_attrs or {})
        enriched = _merge_surface_attrs(None, surf_attrs)
        enriched["filling_parent_surface_id"] = prev_surface_id
        enriched["opening_surface_id"] = prev_surface_id
        if prev_label:
            enriched["BoundarySurfaceType"] = prev_label
            enriched["opening_surface_type"] = prev_label
        return enriched

    def _store_poly_surface_context(pid: str, surf_local: str, surf_id_norm: str, surf_attrs: dict | None) -> None:
        prev_label = poly_label_by_id.get(pid)
        prev_surface_id = surface_id_by_poly_id.get(pid, "")
        merged_label = _prefer_label(prev_label, surf_local) or surf_local
        merged_attrs = _remember_parent_surface_context(pid, prev_label, prev_surface_id, surf_local, surf_attrs)

        poly_label_by_id[pid] = merged_label
        if surf_id_norm and (not prev_surface_id or merged_label == surf_local):
            surface_id_by_poly_id[pid] = surf_id_norm
        if merged_attrs:
            poly_surface_attrs_by_id[pid] = _merge_surface_attrs(poly_surface_attrs_by_id.get(pid), merged_attrs)

    polys = []
    seen = set()

    # Debug output
    import sys
    feat_tag = feat_el.tag.split("}")[-1] if "}" in feat_el.tag else feat_el.tag
    feat_id_raw = get_attr(feat_el, "gml", "id") or "unknown"
    # NOTE: avoid noisy debug prints in normal operation
    poly_has_surface = {}

    # --- PATCH: stabile IDs & OID-Backfill für Polygone ohne gml:id (z.B. Triangle in TriangulatedSurface) ---
    # Wichtig: Python's id(...) und hash(...) sind nicht stabil zwischen Runs.
    # Für fehlende gml:id daher deterministische Hashes aus dem XML erzeugen.
    try:
        from lxml import etree as ET
    except Exception:
        import xml.etree.ElementTree as ET

    def _stable_hash(el) -> str:
        """Deterministischer, kurzer Hash eines XML-Elements (für synthetische IDs)."""
        try:
            b = ET.tostring(el, encoding="utf-8", with_tail=False)
        except TypeError:
            b = ET.tostring(el, encoding="utf-8")
        return hashlib.sha1(b).hexdigest()[:12]

    feat_id_raw = get_attr(feat_el, "gml", "id") or ""
    feat_id_norm = _norm_id(feat_id_raw) if feat_id_raw else f"feat_{_stable_hash(feat_el)}"

    INSTALL_TYPES = {
        "BuildingInstallation", "IntBuildingInstallation",
        "BridgeInstallation", "IntBridgeInstallation",
        "BridgeConstructionElement", "IntBridgeConstructionElement",
        "TunnelInstallation", "IntTunnelInstallation",
    }

    def _opening_parent_surface_context(opening_el):
        """Return the enclosing boundary surface id/type for CityGML 2 openings."""
        if opening_el is None or localname(getattr(opening_el, "tag", "")) not in {"Window", "Door"}:
            return "", ""

        try:
            parent = opening_el.getparent()
        except Exception:
            parent = None

        while parent is not None and parent is not feat_el:
            if _is_con_surface(parent) and not _is_opening_surface(parent):
                parent_local = localname(parent.tag)
                parent_id_raw = get_attr(parent, "gml", "id") or ""
                parent_id_norm = _norm_id(parent_id_raw) if parent_id_raw else ""
                if (not parent_id_norm) and (parent_local in INSTALL_TYPES):
                    parent_id_norm = f"{feat_id_norm}_{parent_local}_{_stable_hash(parent)}"
                return parent_id_norm, parent_local
            try:
                parent = parent.getparent()
            except Exception:
                parent = None

        return "", ""

    def _enrich_opening_surface_attrs(surf_local: str, surf_el, surf_attrs: dict | None) -> dict:
        enriched = dict(surf_attrs or {})
        if surf_local not in {"Window", "Door"}:
            return enriched

        parent_id, parent_type = _opening_parent_surface_context(surf_el)
        if parent_id:
            enriched.setdefault("filling_parent_surface_id", parent_id)
            enriched.setdefault("opening_surface_id", parent_id)
        if parent_type:
            enriched.setdefault("BoundarySurfaceType", parent_type)
            enriched.setdefault("opening_surface_type", parent_type)
        return enriched

    SUBFEATURE_RELATIONS = {
        "BuildingPart": ("bldg", "consistsOfBuildingPart"),
        "BridgePart": ("brid", "consistsOfBridgePart"),
        "TunnelPart": ("tun", "consistsOfTunnelPart"),
        "BuildingInstallation": ("bldg", "outerBuildingInstallation"),
        "IntBuildingInstallation": ("bldg", "interiorBuildingInstallation"),
        "BuildingFurniture": ("bldg", "interiorFurniture"),
        "BridgeInstallation": ("brid", "outerBridgeInstallation"),
        "IntBridgeInstallation": ("brid", "interiorBridgeInstallation"),
        "BridgeConstructionElement": ("brid", "outerBridgeConstruction"),
        "IntBridgeConstructionElement": ("brid", "interiorBridgeConstruction"),
        "BridgeFurniture": ("brid", "interiorFurniture"),
        "TunnelInstallation": ("tun", "outerTunnelInstallation"),
        "IntTunnelInstallation": ("tun", "interiorTunnelInstallation"),
        "TunnelFurniture": ("tun", "interiorFurniture"),
    }

    def is_install_type(tag):
        ln = localname(tag)
        return ln in INSTALL_TYPES

    # Backfill-Mappings: Element-OID -> Infos (falls Polygon selbst keine gml:id hat)
    poly_label_by_oid = {}
    poly_surface_attrs_by_oid = {}
    surface_id_by_oid = {}
    multisurface_id_by_oid = {}
    compositesurface_id_by_oid = {}
    # --- /PATCH ---

    def _direct_child_text(el, wanted_local: str) -> str:
        try:
            for child in list(el):
                if localname(getattr(child, "tag", "")) == wanted_local:
                    txt = (getattr(child, "text", "") or "").strip()
                    if txt:
                        return txt
        except Exception:
            return ""
        return ""

    def _subfeature_context_for_polygon(poly_el) -> dict:
        """
        If a polygon belongs to an inline subfeature of the current feature
        (BuildingPart/BridgePart/TunnelPart/Installation/Furniture/...),
        return stable metadata for export-side reconstruction.
        """
        try:
            parent = poly_el.getparent()
        except Exception:
            parent = None

        while parent is not None and parent is not feat_el:
            ln = localname(getattr(parent, "tag", ""))
            relation_info = SUBFEATURE_RELATIONS.get(ln)
            if relation_info:
                rel_ns, rel_local = relation_info
                sub_id_raw = get_attr(parent, "gml", "id") or ""
                sub_id_norm = _norm_id(sub_id_raw) if sub_id_raw else f"{feat_id_norm}_{ln}_{_stable_hash(parent)}"
                attrs = {
                    "cgml3_subfeature_type": ln,
                    "cgml3_subfeature_id": sub_id_norm,
                    "cgml3_subfeature_parent_id": feat_id_norm,
                    "cgml3_subfeature_relation": rel_local,
                    "cgml3_subfeature_namespace": rel_ns,
                }
                sub_name = _direct_child_text(parent, "name")
                if sub_name:
                    attrs["cgml3_subfeature_name"] = sub_name
                return attrs
            try:
                parent = parent.getparent()
            except Exception:
                parent = None

        return {}

    def _push(poly_el, srs_ctx):
        if poly_el is None:
            return
        if _is_excluded_element(poly_el) or _has_excluded_gml_id(poly_el):
            return
        oid = id(poly_el)
        if oid in seen:
            return
        seen.add(oid)
        polys.append((poly_el, srs_ctx))

        # Finde Surface-Parent und erfasse Surface-Typ
        surf_parent = _find_surface_parent(poly_el)
        # Falls Context bereits per XLink-Prepass gesetzt wurde, nicht überschreiben
        poly_has_surface[oid] = poly_has_surface.get(oid, False) or (surf_parent is not None)
        if surf_parent is not None:
            surf_local = localname(surf_parent.tag)
            surf_attrs = _parse_surface_generic_attributes(surf_parent)
            surf_attrs = _enrich_opening_surface_attrs(surf_local, surf_parent, surf_attrs)

            # gml:id der Surface merken (normalisiert)
            surf_id_raw = get_attr(surf_parent, "gml", "id") or ""
            surf_id_norm = _norm_id(surf_id_raw) if surf_id_raw else ""

            # Für Installation-Objekte kommt oft keine gml:id -> synthetische, aber stabile ID erzeugen
            if (not surf_id_norm) and (surf_local in INSTALL_TYPES):
                surf_id_norm = f"{feat_id_norm}_{surf_local}_{_stable_hash(surf_parent)}"

            # OID-Backfill (auch wenn Polygon keine gml:id hat)
            poly_label_by_oid[oid] = surf_local
            if surf_id_norm:
                surface_id_by_oid[oid] = surf_id_norm
            if surf_attrs:
                poly_surface_attrs_by_oid[oid] = dict(surf_attrs)

            # BuildingInstallation/. soll exportseitig immer als MultiSurface laufen:
            if surf_local in INSTALL_TYPES:
                ms_base = surf_id_norm or f"{feat_id_norm}_{surf_local}_{_stable_hash(surf_parent)}"
                multisurface_id_by_oid[oid] = f"{ms_base}_ms"

            # Polygon-gml:id-basierter Lookup (wenn vorhanden)
            pid = get_attr(poly_el, "gml", "id")
            if pid:
                _store_poly_surface_context(pid, surf_local, surf_id_norm, surf_attrs)

        subfeature_attrs = _subfeature_context_for_polygon(poly_el)
        if subfeature_attrs:
            poly_surface_attrs_by_oid[oid] = _merge_surface_attrs(
                poly_surface_attrs_by_oid.get(oid),
                subfeature_attrs,
            )
            pid = get_attr(poly_el, "gml", "id")
            if pid:
                poly_surface_attrs_by_id[pid] = _merge_surface_attrs(
                    poly_surface_attrs_by_id.get(pid),
                    subfeature_attrs,
                )

    # --- relativeGeometry @xlink:href → referenzierte Geometrie anhängen ---
    if GML_ID_LOOKUP:
        import copy
        # CityGML 2.0: Suche nach localname (ohne Namespace-Präfix)
        for impl in feat_el.iter():
            impl_tag = getattr(impl, "tag", None)
            if not isinstance(impl_tag, str) or localname(impl_tag) != "ImplicitGeometry":
                continue
            if _is_excluded_element(impl):
                continue
            for rel in impl.iter():
                rel_tag = getattr(rel, "tag", None)
                if not isinstance(rel_tag, str):
                    continue
                ln = localname(rel_tag)
                # CityGML 2.0: relativeGMLGeometry, CityGML 3.0: relativeGeometry
                if ln not in ("relativeGeometry", "relativeGMLGeometry"):
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


    # ------------------------------------------------------------
    # PREPASS: BoundarySurface/Installation -> referenzierte Geometrie via xlink:href
    #
    # Hintergrund:
    # Viele Exporte (u.a. FME) legen MultiSurface/CompositeSurface als eigenständige
    # Geometrie-Objekte ab und referenzieren diese aus bldg:RoofSurface/WallSurface/...
    # über xlink:href (z.B. <bldg:lod2MultiSurface xlink:href="#ms1"/>).
    # In diesem Fall liegt die Polygon-Geometrie NICHT in der Eltern-Hierarchie der
    # thematischen Surface, wodurch _find_surface_parent() keinen Surface-Typ findet
    # und die Heuristik (Roof/Wall/Ground) greift -> typisches Symptom: Roof <-> Wall.
    #
    # Lösung:
    # Wir propagieren Surface-Typ, Surface-ID und GenericAttributes der referenzierenden
    # BoundarySurface auf ALLE Polygone der referenzierten Geometrie (ohne Duplikate zu erzeugen).
    # ------------------------------------------------------------
    if GML_ID_LOOKUP:
        def _apply_surface_context_to_geom(target_geom, surf_local, surf_id_norm, surf_attrs):
            if target_geom is None:
                return
            for poly in target_geom.iter():
                if not isinstance(getattr(poly, "tag", None), str):
                    continue
                if localname(poly.tag) not in ("Polygon", "Triangle", "Rectangle", "Surface"):
                    continue

                oid = id(poly)

                # Label / Context nur setzen, wenn noch nicht vorhanden (Nested Geometrie gewinnt)
                poly_label_by_oid[oid] = _prefer_label(poly_label_by_oid.get(oid), surf_local) or surf_local

                # Wichtig für den Duplikat-Filter: dieses Polygon hat thematischen Kontext
                poly_has_surface[oid] = True

                # Surface-ID / GenericAttrs an das Polygon hängen (OID-Backfill)
                if surf_id_norm and oid not in surface_id_by_oid:
                    surface_id_by_oid[oid] = surf_id_norm

                if surf_attrs:
                    prev = poly_surface_attrs_by_oid.get(oid, {})
                    merged = dict(prev)
                    merged.update(surf_attrs)
                    poly_surface_attrs_by_oid[oid] = merged

                # Optional: direkt per gml:id mappen (falls vorhanden)
                pid = get_attr(poly, "gml", "id") or ""
                if pid:
                    _store_poly_surface_context(pid, surf_local, surf_id_norm, surf_attrs)

        for surf_el in feat_el.iter():
            if not (_is_con_surface(surf_el) or _is_opening_surface(surf_el)):
                continue
            if _is_excluded_element(surf_el):
                continue

            surf_local = localname(surf_el.tag)
            surf_attrs = _parse_surface_generic_attributes(surf_el)
            surf_attrs = _enrich_opening_surface_attrs(surf_local, surf_el, surf_attrs)

            # gml:id der Surface (normalisiert), inkl. stabiler Fallback für Installations ohne gml:id
            surf_id_raw = get_attr(surf_el, "gml", "id") or ""
            surf_id_norm = _norm_id(surf_id_raw) if surf_id_raw else ""
            if (not surf_id_norm) and (surf_local in INSTALL_TYPES):
                surf_id_norm = f"{feat_id_norm}_{surf_local}_{_stable_hash(surf_el)}"

            # Alle xlink:href innerhalb der Surface (inkl. lodXMultiSurface, surfaceMember, usw.)
            for ref in surf_el.iter():
                href = ref.get(XLINK_HREF)
                if not href:
                    continue
                gid = _norm_id(href)
                if not gid:
                    continue

                geom_target = GML_ID_LOOKUP.get(gid)
                if geom_target is None:
                    continue

                _apply_surface_context_to_geom(geom_target, surf_local, surf_id_norm, surf_attrs)

    # Debug: Starting geometry search
    import sys
    # NOTE: avoid noisy debug prints in normal operation

    # IMPORTANT: In streaming mode, elements come from lxml.iterparse. That is fine, but
    # this module may also be used with xml.etree fallback in edge cases. To be robust,
    # fall back to a namespace-agnostic scan if the namespace-based XPath returns 0.
    multisurfaces = feat_el.findall(".//gml:MultiSurface", NS)
    if not multisurfaces:
        multisurfaces = []
        for elem in feat_el.iter():
            tag = getattr(elem, "tag", None)
            if isinstance(tag, str) and (tag.endswith("}MultiSurface") or tag == "MultiSurface"):
                multisurfaces.append(elem)

    # NOTE: avoid noisy debug prints in normal operation
    
    for ms in multisurfaces:
        if _is_excluded_element(ms):
            continue
        # LOD filter - check parent element
        if lod_filter is not None:
            parent = ms.getparent()
            if parent is not None:
                parent_tag = localname(parent.tag)
                if parent_tag.startswith("lod"):
                    try:
                        lod_num = int(parent_tag[3])  # Extract digit from "lodXMultiSurface"
                        if lod_num not in lod_filter:
                            continue  # Skip this MultiSurface
                    except (ValueError, IndexError):
                        pass
        
        srs_ms = ms.get("srsName") or default_srs

        # gml:id der MultiSurface merken (normalisiert)
        ms_id_raw = get_attr(ms, "gml", "id") or ""
        ms_id_norm = _norm_id(ms_id_raw) if ms_id_raw else ""

        for p in ms.findall(".//gml:surfaceMember//gml:Polygon", NS):
            if _is_excluded_element(p) or _has_excluded_gml_id(p):
                continue
            _push(p, srs_ms)
            if ms_id_norm:
                pid = get_attr(p, "gml", "id") or ""
                if pid:
                    multisurface_id_by_poly_id[_norm_id(pid)] = ms_id_norm
                else:
                    # Polygon ohne gml:id -> über OID mappen (wird später backfilled)
                    multisurface_id_by_oid[id(p)] = ms_id_norm

        for t in ms.findall(".//gml:surfaceMember//gml:Triangle", NS):
            if _is_excluded_element(t) or _has_excluded_gml_id(t):
                continue
            _push(t, srs_ms)
            if ms_id_norm:
                multisurface_id_by_oid[id(t)] = ms_id_norm

        for r in ms.findall(".//gml:surfaceMember//gml:Rectangle", NS):
            if _is_excluded_element(r) or _has_excluded_gml_id(r):
                continue
            _push(r, srs_ms)
            if ms_id_norm:
                multisurface_id_by_oid[id(r)] = ms_id_norm

    # CompositeSurface IDs (gml:CompositeSurface)
    for cs in feat_el.findall(".//gml:CompositeSurface", NS):
        if _is_excluded_element(cs):
            continue
        # LOD-Container wie lod2CompositeSurface etc. ignorieren
        if localname(cs.tag).startswith("lod") and ("CompositeSurface" in localname(cs.tag)):
            continue

        srs_cs = cs.get("srsName") or default_srs

        cs_id_raw = get_attr(cs, "gml", "id") or ""
        cs_id_norm = _norm_id(cs_id_raw) if cs_id_raw else f"{feat_id_norm}_CompositeSurface_{_stable_hash(cs)}"

        def _mark_composite_member(el):
            """Merkt: Element ist Member einer CompositeSurface.

            CompositeSurface ist ein Geometrie-Container und darf NICHT als thematischer
            Surface-Typ (Label/SurfaceTyp) verwendet werden.
            """
            if el is None:
                return
            oid = id(el)
            compositesurface_member_by_oid[oid] = True

            # Für Material-Property: CompositeSurface-ID am Polygon merken
            pid = get_attr(el, "gml", "id") or ""
            if pid:
                compositesurface_id_by_poly_id[_norm_id(pid)] = cs_id_norm
            else:
                compositesurface_id_by_oid[oid] = cs_id_norm

            # WICHTIG: KEIN Label setzen. CompositeSurface ist kein thematischer Surface-Typ.
            # Die Zuordnung (Roof/Wall/Ground/...) passiert später über BoundarySurface-Parent
            # oder (bei rein geometrischer Modellierung) per Heuristik.

        # Member: Polygon
        for p in cs.findall(".//gml:surfaceMember//gml:Polygon", NS):
            if _is_excluded_element(p) or _has_excluded_gml_id(p):
                continue
            _push(p, srs_cs)
            _mark_composite_member(p)

        # Member: Triangle
        for t in cs.findall(".//gml:surfaceMember//gml:Triangle", NS):
            if _is_excluded_element(t) or _has_excluded_gml_id(t):
                continue
            _push(t, srs_cs)
            _mark_composite_member(t)

        # Member: Rectangle
        for r in cs.findall(".//gml:surfaceMember//gml:Rectangle", NS):
            if _is_excluded_element(r) or _has_excluded_gml_id(r):
                continue
            _push(r, srs_cs)
            _mark_composite_member(r)

    # Solid
    for solid in feat_el.findall(".//gml:Solid", NS):
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
        
        srs_sd = solid.get("srsName") or default_srs
        for p in solid.findall(".//gml:surfaceMember//gml:Polygon", NS):
            if _is_excluded_element(p) or _has_excluded_gml_id(p):
                continue
            _push(p, srs_sd)
        for t in solid.findall(".//gml:surfaceMember//gml:Triangle", NS):
            if _is_excluded_element(t) or _has_excluded_gml_id(t):
                continue
            _push(t, srs_sd)
        for r in solid.findall(".//gml:surfaceMember//gml:Rectangle", NS):
            if _is_excluded_element(r) or _has_excluded_gml_id(r):
                continue
            _push(r, srs_sd)
    
    # MultiSolid (mehrere Solid-Elemente)
    for multisolid in feat_el.findall(".//gml:MultiSolid", NS):
        if _is_excluded_element(multisolid):
            continue
        srs_msd = multisolid.get("srsName") or default_srs
        # solidMember kann mehrere Solid-Elemente enthalten
        for solid in multisolid.findall(".//gml:solidMember//gml:Solid", NS):
            if _is_excluded_element(solid):
                continue
            srs_sd = solid.get("srsName") or srs_msd
            for p in solid.findall(".//gml:surfaceMember//gml:Polygon", NS):
                if _is_excluded_element(p) or _has_excluded_gml_id(p):
                    continue
                _push(p, srs_sd)
            for t in solid.findall(".//gml:surfaceMember//gml:Triangle", NS):
                if _is_excluded_element(t) or _has_excluded_gml_id(t):
                    continue
                _push(t, srs_sd)
            for r in solid.findall(".//gml:surfaceMember//gml:Rectangle", NS):
                if _is_excluded_element(r) or _has_excluded_gml_id(r):
                    continue
                _push(r, srs_sd)
        # solidMembers (Plural-Variante, ohne Wrapper)
        for solid in multisolid.findall(".//gml:solidMembers//gml:Solid", NS):
            if _is_excluded_element(solid):
                continue
            srs_sd = solid.get("srsName") or srs_msd
            for p in solid.findall(".//gml:surfaceMember//gml:Polygon", NS):
                if _is_excluded_element(p) or _has_excluded_gml_id(p):
                    continue
                _push(p, srs_sd)
            for t in solid.findall(".//gml:surfaceMember//gml:Triangle", NS):
                if _is_excluded_element(t) or _has_excluded_gml_id(t):
                    continue
                _push(t, srs_sd)
            for r in solid.findall(".//gml:surfaceMember//gml:Rectangle", NS):
                if _is_excluded_element(r) or _has_excluded_gml_id(r):
                    continue
                _push(r, srs_sd)
    
    # TriangulatedSurface (für TINRelief)
    for tin in feat_el.findall(".//gml:TriangulatedSurface", NS):
        if _is_excluded_element(tin):
            continue
        srs_tin = tin.get("srsName") or default_srs
        for p in tin.findall(".//gml:Triangle", NS):
            _push(p, srs_tin)
        # Fallback auf Polygon (falls Triangle nicht verwendet)
        for p in tin.findall(".//gml:Polygon", NS):
            _push(p, srs_tin)
    
    # TINRelief explizit (dem:tin/gml:TriangulatedSurface)
    for tin_relief in feat_el.findall(".//dem:tin", NS):
        if _is_excluded_element(tin_relief):
            continue
        for tin_surf in tin_relief.findall(".//gml:TriangulatedSurface", NS):
            if _is_excluded_element(tin_surf):
                continue
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
    
    # Falls thematische BoundarySurfaces (RoofSurface, WallSurface, etc.) vorhanden sind,
    # sind Polygone ohne solchen Kontext meist Duplikate aus lodXSolid/lodXMultiSurface.
    # Diese entfernen wir, damit Material->SurfaceTyp korrekt ist.
    #
    # WICHTIG: BridgeInstallation/BuildingInstallation sind KEINE BoundarySurfaces,
    # sondern semantische Subfeatures mit eigener Geometrie. Sie sollten NICHT den
    # Duplikat-Filter für die Haupt-Feature-Geometrie (lod2MultiSurface) aktivieren.
    
    def _has_boundary_surfaces():
        """Prüft, ob echte thematische BoundarySurfaces (nicht Installationen) vorhanden sind."""
        for p, _ in polys:
            if poly_has_surface.get(id(p), False):
                # Prüfe, ob das Polygon unter einer echten BoundarySurface liegt
                # (nicht unter Installation/ConstructionElement)
                parent = _find_surface_parent(p)
                if parent is not None:
                    parent_ln = localname(parent.tag)
                    # Installations und ConstructionElements sind KEINE BoundarySurfaces
                    if parent_ln not in INSTALL_TYPES:
                        return True
        return False
    
    if polys and _has_boundary_surfaces():
        # Sobald mindestens eine echte thematische BoundarySurface existiert,
        # behalten wir ausschließlich Polygone mit thematischem Kontext.
        # Das filtert Duplikate aus bldg:lodXSolid/gml:CompositeSurface zuverlässig weg.
        polys = [(p, srs) for p, srs in polys if poly_has_surface.get(id(p), False)]

    # ------------------------------------------------------------
    # Heuristik: SurfaceTyp für Geometrien ohne thematische Surfaces
    # (z.B. bldg:lod2Solid mit gml:CompositeSurface)
    #
    # Ziel: JEDES Face bekommt einen validen SurfaceTyp (Roof/Wall/Ground),
    # ohne dass "CompositeSurface" als Typ auftaucht.
    # ------------------------------------------------------------
    INFER_SURFACE_FOR_FEATURES = {
        "Building", "BuildingPart",
        "Bridge", "BridgePart",
        "Tunnel", "TunnelPart",
    }

    def _infer_surface_type_from_points(pts, zmin, zmax, horiz_thr=0.9):
        """Leitet RoofSurface/WallSurface/GroundSurface aus Geometrie ab.

        - Wenn Face annähernd horizontal (|nz| >= horiz_thr):
          * Face näher am zmax -> RoofSurface
          * sonst -> GroundSurface
        - Sonst: WallSurface
        """
        if not pts or len(pts) < 3:
            return "WallSurface"

        x1, y1, z1 = pts[0]
        x2, y2, z2 = pts[1]
        x3, y3, z3 = pts[2]
        ux, uy, uz = (x2 - x1), (y2 - y1), (z2 - z1)
        vx, vy, vz = (x3 - x1), (y3 - y1), (z3 - z1)
        # Cross product u x v
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        nlen = (nx * nx + ny * ny + nz * nz) ** 0.5
        if nlen < 1e-12:
            return "WallSurface"
        nz /= nlen

        if abs(nz) >= horiz_thr:
            cz = sum(p[2] for p in pts) / float(len(pts))
            if abs(zmax - zmin) < 1e-9:
                return "RoofSurface"
            # näher am oberen Ende => Dach
            return "RoofSurface" if abs(zmax - cz) <= abs(cz - zmin) else "GroundSurface"

        return "WallSurface"

    # Debug: Check polygon collection
    import sys
    # NOTE: avoid noisy debug prints in normal operation

    # zmin/zmax (für Roof/Ground Heuristik). Nur aus den tatsächlich importierten Polys.
    zmin = float("inf")
    zmax = float("-inf")
    for _poly_el, _srs_ctx in polys:
        _srs_poly = inherit_srs(_poly_el, _srs_ctx) or default_srs
        if localname(_poly_el.tag) == "Surface":
            _rings = _rings_from_surface(_poly_el, _srs_poly)
        else:
            _rings = _rings_from_polygon(_poly_el, _srs_poly)
        for _coords, _ in _rings:
            _clean = _apply_transform(_poly_el, _coords)
            for _p in _clean:
                zmin = min(zmin, float(_p[2]))
                zmax = max(zmax, float(_p[2]))
    if zmin == float("inf"):
        zmin = zmax = 0.0

    # Debug: Sample first few polygons
    import sys
    sample_count = 0
    rings_empty_count = 0
    clean_empty_count = 0

    # Rings → Vertices/Faces
    for poly_el, srs_ctx in polys:
        poly_oid = id(poly_el)
        poly_id = get_attr(poly_el, "gml", "id") or ""
        if not poly_id:
            # Synthetische, stabile Polygon-ID (wichtig für Triangles ohne gml:id)
            poly_id = f"{feat_id_norm}_poly_{_stable_hash(poly_el)}"

        # Backfill: Infos aus OID-Mappings auf diese (ggf. synthetische) poly_id übertragen
        if poly_oid in poly_label_by_oid:
            poly_label_by_id[poly_id] = _prefer_label(poly_label_by_id.get(poly_id), poly_label_by_oid[poly_oid]) or poly_label_by_oid[poly_oid]

        if poly_oid in poly_surface_attrs_by_oid:
            prev = poly_surface_attrs_by_id.get(poly_id, {})
            merged = dict(prev)
            merged.update(poly_surface_attrs_by_oid[poly_oid])
            poly_surface_attrs_by_id[poly_id] = merged

        if poly_oid in surface_id_by_oid and poly_id not in surface_id_by_poly_id:
            surface_id_by_poly_id[poly_id] = surface_id_by_oid[poly_oid]

        if poly_oid in multisurface_id_by_oid and poly_id not in multisurface_id_by_poly_id:
            multisurface_id_by_poly_id[poly_id] = multisurface_id_by_oid[poly_oid]

        if poly_oid in compositesurface_id_by_oid and poly_id not in compositesurface_id_by_poly_id:
            compositesurface_id_by_poly_id[poly_id] = compositesurface_id_by_oid[poly_oid]
        srs_poly = inherit_srs(poly_el, srs_ctx) or default_srs

        if localname(poly_el.tag) == "Surface":
            rings = _rings_from_surface(poly_el, srs_poly)
        else:
            rings = _rings_from_polygon(poly_el, srs_poly)

        if not rings:
            rings_empty_count += 1
            # Silent by default (was debug print)
            continue

        # Label-Lookup: zuerst nach gml:id, dann Surface-Parent.
        # CompositeSurface wird explizit NICHT als Label verwendet.
        label = poly_label_by_id.get(poly_id)
        if not label:
            surf_parent = _find_surface_parent(poly_el)
            if surf_parent is not None:
                label = localname(surf_parent.tag)

        feat_label = localname(feat_el.tag)
        infer_surface = (not label) and (
            compositesurface_member_by_oid.get(poly_oid, False)
            or feat_label in INFER_SURFACE_FOR_FEATURES
        )
        
        for coords, ring_id in rings:
            clean = _apply_transform(poly_el, coords)
            if len(clean) < 3:
                clean_empty_count += 1
                # Silent by default (was debug print)
                continue
            idxs_raw = [add_vertex(c) for c in clean]

            # Nach Welding können aufeinanderfolgende Punkte auf denselben Vertex fallen:
            # -> consecutive duplicates entfernen, sonst degenerierte Faces.
            idxs = []
            for i in idxs_raw:
                if not idxs or i != idxs[-1]:
                    idxs.append(i)

            # Abschluss-Duplikat entfernen (Ring geschlossen)
            if len(idxs) >= 3 and idxs[0] == idxs[-1]:
                idxs = idxs[:-1]

            if len(idxs) < 3:
                continue

            # Finaler Surface-Typ pro Face
            final_label = label
            if not final_label:
                final_label = (
                    _infer_surface_type_from_points(clean, zmin, zmax)
                    if infer_surface
                    else feat_label
                )

            faces.append(idxs)
            # LoD-Nummer aus Elternkette bestimmen
            lod_num = _get_lod_from_parent(poly_el)
            surf_labels.append((final_label, poly_id, ring_id, srs_poly or "", tuple(idxs), lod_num))

    # Output results with debugging
    import sys
    # NOTE: avoid noisy debug prints in normal operation

    if include_surface_attrs:
        # neu: surface_id_by_poly_id, multisurface_id_by_poly_id UND compositesurface_id_by_poly_id zurückgeben
        return (
            faces,
            verts,
            surf_labels,
            poly_surface_attrs_by_id,
            surface_id_by_poly_id,
            multisurface_id_by_poly_id,
            compositesurface_id_by_poly_id,
        )
    return faces, verts, surf_labels


def build_geometry_hierarchical(feat_el, default_srs, ref_origin, include_surface_attrs=False, lod_filter=None, bake_implicit=True):
    """
    Erweiterte Version von build_geometry, die Geometrie hierarchisch gruppiert zurückgibt.
    
    Hybrid-Ansatz für bessere Blender-Strukturierung:
    - Äußere Gebäudehülle (boundedBy) → ein Mesh mit Material-Slots
    - Räume (interiorRoom) → separate Mesh-Objekte mit boundedBy-Surfaces
    - Furniture/Installations → separate Child-Objekte
    
    WICHTIG: Extrahiert nur boundedBy-Surfaces, NICHT Solid-Geometrie (vermeidet Duplikate).
    
    Returns:
        dict mit folgender Struktur:
        {
            "outer_shell": {
                "verts": [...],
                "faces": [...],
                "surf_labels": [...],
                "poly_attrs": {...},
                "surface_ids": {...},
                "ms_ids": {...},
                "cs_ids": {...}
            },
            "rooms": {
                "room_gml_id": {
                    "name": "Room Name",
                    "verts": [...],
                    "faces": [...],
                    "surf_labels": [...],
                    "poly_attrs": {...},
                    "surface_ids": {...},
                    "ms_ids": {...},
                    "cs_ids": {...},
                    "furniture": {
                        "furniture_gml_id": {...}
                    },
                    "installations": {
                        "installation_gml_id": {...}
                    },
                    "openings": {
                        "opening_gml_id": {
                            "name": "Door/Window Name",
                            "opening_type": "Door"|"Window",
                            "verts": [...],
                            "faces": [...],
                            "surf_labels": [...]
                        }
                    }
                }
            }
        }
    """
    import sys
    import copy
    try:
        from lxml import etree as ET
    except:
        import xml.etree.ElementTree as ET
    
    result = {
        "outer_shell": {
            "verts": [],
            "faces": [],
            "surf_labels": [],
            "poly_attrs": {},
            "surface_ids": {},
            "ms_ids": {},
            "cs_ids": {}
        },
        "rooms": {},
        "feature_type": localname(feat_el.tag)
    }
    
    # ============================================================================
    # OUTER SHELL: nur die Geometrie des Features selbst.
    # ============================================================================
    
    # Klone das Feature-Element
    feat_outer_only = copy.deepcopy(feat_el)
    
    # Entferne alle interiorRoom, interiorFurniture, roomInstallation Elemente UND lod4Solid
    for room_el in feat_outer_only.findall(".//bldg:interiorRoom", NS):
        parent = room_el.getparent()
        if parent is not None:
            parent.remove(room_el)
    
    for furn_el in feat_outer_only.findall(".//bldg:interiorFurniture", NS):
        parent = furn_el.getparent()
        if parent is not None:
            parent.remove(furn_el)

    # Entferne BuildingParts aus dem Parent, damit deren Geometrie nicht in der OuterShell landet
    for part_container in feat_outer_only.findall(".//bldg:consistsOfBuildingPart", NS):
        parent = part_container.getparent()
        if parent is not None:
            parent.remove(part_container)

    # Entferne BuildingInstallations aus dem Parent, damit deren Geometrie nicht in der OuterShell landet
    for inst_container in feat_outer_only.findall(".//bldg:outerBuildingInstallation", NS):
        parent = inst_container.getparent()
        if parent is not None:
            parent.remove(inst_container)
    for inst_container in feat_outer_only.findall(".//bldg:interiorBuildingInstallation", NS):
        parent = inst_container.getparent()
        if parent is not None:
            parent.remove(inst_container)
    
    # WICHTIG: Entferne lod4Solid (vermeidet Duplikate, da boundedBy dieselben Surfaces enthält)
    for solid_el in feat_outer_only.findall(".//bldg:lod4Solid", NS):
        parent = solid_el.getparent()
        if parent is not None:
            parent.remove(solid_el)
    
    # WICHTIG: Entferne Openings (Door/Window) – diese werden separat extrahiert
    for opening_el in feat_outer_only.findall(".//bldg:opening", NS):
        parent = opening_el.getparent()
        if parent is not None:
            parent.remove(opening_el)
    
    # Injiziere synthetische gml:ids auf BoundarySurfaces ohne gml:id
    # (nötig damit build_geometry surface_id_by_poly_id korrekt befüllt)
    _feat_id_raw = get_attr(feat_el, "gml", "id") or "feat"
    _outer_synth_ids = {}  # bb_index → synth_id
    for _bb_i, _bb_el in enumerate(feat_outer_only.findall("bldg:boundedBy", NS)):
        for _ch in _bb_el:
            if isinstance(_ch.tag, str) and "}" in _ch.tag:
                _ch_ln = _ch.tag.rsplit("}", 1)[1]
                if _ch_ln.endswith("Surface") and not get_attr(_ch, "gml", "id"):
                    _synth = f"synth_{_feat_id_raw}_OuterShell_{_ch_ln}_{_bb_i}"
                    _ch.set("{%s}id" % NS["gml"], _synth)
                    _outer_synth_ids[_bb_i] = _synth
            break
    
    # Hole die Geometrie NUR für die äußere Hülle (boundedBy-Surfaces)
    base_result = build_geometry(feat_outer_only, default_srs, ref_origin, include_surface_attrs, lod_filter, bake_implicit)
    
    if include_surface_attrs:
        all_faces, all_verts, all_surf_labels, poly_attrs, surface_ids, ms_ids, cs_ids = base_result
    else:
        all_faces, all_verts, all_surf_labels = base_result
        poly_attrs = {}
        surface_ids = {}
        ms_ids = {}
        cs_ids = {}
    
    # Erkenne Hierarchie-Kontext aus surf_labels
    # surf_labels Format: (surf_type, poly_id, ring_id, srs, vert_indices, lod_num)
    
    # Analysiere Feature-Struktur für Room/Furniture/Installation Zuordnung
    rooms_dict = {}
    furniture_dict = {}
    installation_dict = {}
    
    # Finde alle Rooms
    for room_el in feat_el.findall(".//bldg:Room", NS):
        room_id = get_attr(room_el, "gml", "id") or f"room_{id(room_el)}"
        room_name = room_el.findtext(".//gml:name", namespaces=NS) or room_id
        rooms_dict[room_id] = {
            "element": room_el,
            "name": room_name,
            "furniture": {},
            "installations": {}
        }
        
        # Furniture in diesem Room
        for furn_el in room_el.findall(".//bldg:BuildingFurniture", NS) + room_el.findall(".//bldg:InteriorFurniture", NS):
            furn_id = get_attr(furn_el, "gml", "id") or f"furn_{id(furn_el)}"
            furn_name = furn_el.findtext(".//gml:name", namespaces=NS) or furn_id
            furniture_dict[furn_id] = furn_el
            rooms_dict[room_id]["furniture"][furn_id] = {
                "element": furn_el,
                "name": furn_name
            }
        
        # Installations in diesem Room
        for inst_el in room_el.findall(".//bldg:IntBuildingInstallation", NS) + room_el.findall(".//bldg:roomInstallation", NS):
            # roomInstallation ist der Container, IntBuildingInstallation das Element
            if localname(inst_el.tag) == "roomInstallation":
                inst_inner = inst_el.find(".//bldg:IntBuildingInstallation", NS)
                if inst_inner is not None:
                    inst_el = inst_inner
            
            inst_id = get_attr(inst_el, "gml", "id") or f"inst_{id(inst_el)}"
            inst_name = inst_el.findtext(".//gml:name", namespaces=NS) or inst_id
            installation_dict[inst_id] = inst_el
            rooms_dict[room_id]["installations"][inst_id] = {
                "element": inst_el,
                "name": inst_name
            }
    
    # Funktion zum Zuordnen von Polygonen zu hierarchischen Kontexten
    def get_context_for_polygon(poly_id, surf_label_entry):
        """
        Bestimmt, zu welchem Kontext (outer_shell, room_id, furniture_id, installation_id) 
        ein Polygon gehört, basierend auf der XML-Hierarchie.
        """
        # Versuche, das Polygon-Element im XML zu finden
        # Dies ist eine Heuristik basierend auf den poly_ids
        
        # Prüfe, ob poly_id zu einer bestimmten Room/Furniture/Installation gehört
        # Dies erfordert eine Rückwärts-Suche im XML-Baum
        
        # Vereinfachte Heuristik: Nutze surf_labels Informationen
        surf_type, _, _, _, _, _ = surf_label_entry
        
        # InteriorWallSurface, CeilingSurface, FloorSurface → gehört zu Room
        if surf_type in ("InteriorWallSurface", "CeilingSurface", "FloorSurface", "ClosureSurface"):
            # Müssen herausfinden, welcher Room
            # TODO: Erweiterte Logik erforderlich
            return ("room", None)  # Platzhalter
        
        # Äußere Surfaces gehören zur Outer Shell
        if surf_type in ("WallSurface", "RoofSurface", "GroundSurface", "OuterFloorSurface", "OuterCeilingSurface"):
            return ("outer_shell", None)
        
        # BuildingFurniture, IntBuildingInstallation
        if surf_type in ("BuildingFurniture", "IntBuildingInstallation"):
            return ("installation", None)  # Vereinfacht
        
        # Default: Outer Shell
        return ("outer_shell", None)
    
    # NOTE: avoid noisy debug prints in normal operation
    
    # Speichere Outer Shell
    result["outer_shell"]["verts"] = all_verts
    result["outer_shell"]["faces"] = all_faces
    result["outer_shell"]["surf_labels"] = all_surf_labels
    result["outer_shell"]["poly_attrs"] = poly_attrs
    result["outer_shell"]["surface_ids"] = surface_ids
    result["outer_shell"]["ms_ids"] = ms_ids
    result["outer_shell"]["cs_ids"] = cs_ids
    
    # Extrahiere Openings (Doors/Windows) aus den äußeren boundedBy-Surfaces
    outer_openings_dict = {}
    outer_surfaces_info = {}  # surface_id -> {type, name}
    for _outer_bb_idx, bounded_by_el in enumerate(feat_el.findall("bldg:boundedBy", NS)):
        # Hole die Surface-Element (WallSurface, RoofSurface, etc.)
        surface_el = None
        surface_type = None
        surface_name = None
        surface_id = None
        
        for child in bounded_by_el:
            if child.tag.startswith("{") and "}" in child.tag:
                ns_uri, local_name = child.tag.rsplit("}", 1)
                ns_uri = ns_uri[1:]
                if ns_uri == NS["bldg"] and "Surface" in local_name:
                    surface_el = child
                    surface_type = local_name
                    surface_name = child.findtext(".//gml:name", namespaces=NS)
                    surface_id = get_attr(child, "gml", "id") or _outer_synth_ids.get(_outer_bb_idx)
                    break
        
        # Speichere Surface-Info für per-Surface Export
        if surface_id and surface_type:
            outer_surfaces_info[surface_id] = {
                "type": surface_type,
                "name": surface_name or ""
            }
        
        # Suche Openings in dieser Surface
        for opening_container in bounded_by_el.findall(".//bldg:opening", NS):
            door_el = opening_container.find(".//bldg:Door", NS)
            window_el = opening_container.find(".//bldg:Window", NS)
            
            opening_el = door_el if door_el is not None else window_el
            if opening_el is None:
                continue
            
            opening_type = "Door" if door_el is not None else "Window"
            opening_id = get_attr(opening_el, "gml", "id") or f"opening_{id(opening_el)}"
            opening_name = opening_el.findtext(".//gml:name", namespaces=NS) or opening_id
            
            # Deep-copy damit _find_surface_parent() nicht zum Eltern-WallSurface hochläuft
            opening_geom = build_geometry(copy.deepcopy(opening_el), default_srs, ref_origin, False, lod_filter, bake_implicit)
            o_faces, o_verts, o_labels = opening_geom
            
            # Prüfe auf OrientableSurface/xlink (raumseitige Gegenstücke)
            xlink_refs = []
            if not o_faces:
                for os_el in opening_el.findall(".//{%s}OrientableSurface" % NS["gml"]):
                    base_surf = os_el.find("{%s}baseSurface" % NS["gml"])
                    if base_surf is not None:
                        href = base_surf.get(XLINK_HREF, "")
                        if href:
                            xlink_refs.append(href.lstrip("#"))
            
            outer_openings_dict[opening_id] = {
                "name": opening_name,
                "opening_type": opening_type,
                "verts": o_verts,
                "faces": o_faces,
                "surf_labels": o_labels,
                "surface_id": surface_id,
                "surface_type": surface_type,
                "surface_name": surface_name,
                "xlink_refs": xlink_refs
            }
    
    result["outer_shell"]["openings"] = outer_openings_dict
    result["outer_shell"]["surfaces_info"] = outer_surfaces_info
    
    # ============================================================================
    # ROOMS: Extrahiere boundedBy-Surfaces (OHNE lod4Solid) und Openings
    # ============================================================================
    
    for room_id, room_data in rooms_dict.items():
        room_el = room_data["element"]
        room_name = room_data["name"]
        
        # Klone Room-Element und entferne lod4Solid
        room_el_copy = copy.deepcopy(room_el)
        for solid_el in room_el_copy.findall(".//bldg:lod4Solid", NS):
            parent = solid_el.getparent()
            if parent is not None:
                parent.remove(solid_el)
        
        # Entferne auch Furniture/Installations (werden separat verarbeitet)
        # Hinweis: interiorFurniture ist der Container, BuildingFurniture ist das Kind
        for furn_container in room_el_copy.findall(".//bldg:interiorFurniture", NS):
            parent = furn_container.getparent()
            if parent is not None:
                parent.remove(furn_container)
        
        for inst_container in room_el_copy.findall(".//bldg:roomInstallation", NS):
            parent = inst_container.getparent()
            if parent is not None:
                parent.remove(inst_container)
        
        # Injiziere synthetische gml:ids auf BoundarySurfaces ohne gml:id
        _room_synth_ids = {}  # bb_index → synth_id
        for _bb_i, _bb_el in enumerate(room_el_copy.findall("bldg:boundedBy", NS)):
            for _ch in _bb_el:
                if isinstance(_ch.tag, str) and "}" in _ch.tag:
                    _ch_ln = _ch.tag.rsplit("}", 1)[1]
                    if _ch_ln.endswith("Surface") and not get_attr(_ch, "gml", "id"):
                        _synth = f"synth_{room_id}_{_ch_ln}_{_bb_i}"
                        _ch.set("{%s}id" % NS["gml"], _synth)
                        _room_synth_ids[_bb_i] = _synth
                break
        
        # Extrahiere Room-Geometrie (nur boundedBy-Surfaces)
        room_geom = build_geometry(room_el_copy, default_srs, ref_origin, include_surface_attrs, lod_filter, bake_implicit)
        
        if include_surface_attrs:
            r_faces, r_verts, r_labels, r_poly_attrs, r_surface_ids, r_ms_ids, r_cs_ids = room_geom
        else:
            r_faces, r_verts, r_labels = room_geom
            r_poly_attrs, r_surface_ids, r_ms_ids, r_cs_ids = {}, {}, {}, {}
        
        # Extrahiere Openings (Doors/Windows) aus boundedBy-Elements
        openings_dict = {}
        room_surfaces_info = {}  # surface_id -> {type, name}
        for _room_bb_idx, bounded_by_el in enumerate(room_el.findall("bldg:boundedBy", NS)):
            # Hole die Surface-Element (InteriorWallSurface, WallSurface, etc.)
            surface_el = None
            surface_type = None
            surface_name = None
            surface_id = None
            
            for child in bounded_by_el:
                if child.tag.startswith("{") and "}" in child.tag:
                    ns_uri, local_name = child.tag.rsplit("}", 1)
                    ns_uri = ns_uri[1:]  # Remove leading "{"
                    if ns_uri == NS["bldg"] and "Surface" in local_name:
                        surface_el = child
                        surface_type = local_name
                        surface_name = child.findtext(".//gml:name", namespaces=NS)
                        surface_id = get_attr(child, "gml", "id") or _room_synth_ids.get(_room_bb_idx)
                        break
            
            # Speichere Surface-Info für per-Surface Export
            if surface_id and surface_type:
                room_surfaces_info[surface_id] = {
                    "type": surface_type,
                    "name": surface_name or ""
                }
            
            # Suche Openings in dieser Surface
            for opening_container in bounded_by_el.findall(".//bldg:opening", NS):
                # Finde Door oder Window
                door_el = opening_container.find(".//bldg:Door", NS)
                window_el = opening_container.find(".//bldg:Window", NS)
                
                opening_el = door_el if door_el is not None else window_el
                if opening_el is None:
                    continue
                
                opening_type = "Door" if door_el is not None else "Window"
                opening_id = get_attr(opening_el, "gml", "id") or f"opening_{id(opening_el)}"
                opening_name = opening_el.findtext(".//gml:name", namespaces=NS) or opening_id
                
                # Deep-copy damit _find_surface_parent() nicht zum Eltern-WallSurface hochläuft
                opening_geom = build_geometry(copy.deepcopy(opening_el), default_srs, ref_origin, False, lod_filter, bake_implicit)
                o_faces, o_verts, o_labels = opening_geom
                
                # Prüfe auf OrientableSurface/xlink (raumseitige Gegenstücke)
                xlink_refs = []
                if not o_faces:
                    for os_el in opening_el.findall(".//{%s}OrientableSurface" % NS["gml"]):
                        orientation = os_el.get("orientation", "-")
                        base_surf = os_el.find("{%s}baseSurface" % NS["gml"])
                        if base_surf is not None:
                            href = base_surf.get(XLINK_HREF, "")
                            if href:
                                xlink_refs.append(href.lstrip("#"))
                
                openings_dict[opening_id] = {
                    "name": opening_name,
                    "opening_type": opening_type,
                    "verts": o_verts,
                    "faces": o_faces,
                    "surf_labels": o_labels,
                    # Zuordnung zur Surface speichern
                    "surface_id": surface_id,
                    "surface_type": surface_type,
                    "surface_name": surface_name,
                    "xlink_refs": xlink_refs
                }
        
        # (debug removed) room summary
        
        result["rooms"][room_id] = {
            "name": room_name,
            "verts": r_verts,
            "faces": r_faces,
            "surf_labels": r_labels,
            "poly_attrs": r_poly_attrs,
            "surface_ids": r_surface_ids,
            "ms_ids": r_ms_ids,
            "cs_ids": r_cs_ids,
            "furniture": {},
            "installations": {},
            "openings": openings_dict,
            "surfaces_info": room_surfaces_info
        }
        
        # Verarbeite Furniture für diesen Room
        for furn_id, furn_info in room_data["furniture"].items():
            furn_el = furn_info["element"]
            furn_geom = build_geometry(furn_el, default_srs, ref_origin, False, lod_filter, bake_implicit)
            f_faces, f_verts, f_labels = furn_geom
            
            result["rooms"][room_id]["furniture"][furn_id] = {
                "name": furn_info["name"],
                "verts": f_verts,
                "faces": f_faces,
                "surf_labels": f_labels
            }
            
            # (debug removed) furniture summary
        
        # Verarbeite Installations für diesen Room
        for inst_id, inst_info in room_data["installations"].items():
            inst_el = inst_info["element"]
            inst_geom = build_geometry(inst_el, default_srs, ref_origin, False, lod_filter, bake_implicit)
            i_faces, i_verts, i_labels = inst_geom
            
            result["rooms"][room_id]["installations"][inst_id] = {
                "name": inst_info["name"],
                "verts": i_verts,
                "faces": i_faces,
                "surf_labels": i_labels
            }
            
            # (debug removed) installation summary
    
    if include_surface_attrs:
        result["poly_surface_attrs"] = poly_attrs
        result["surface_ids"] = surface_ids
        result["multisurface_ids"] = ms_ids
        result["compositesurface_ids"] = cs_ids
    
    return result


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
