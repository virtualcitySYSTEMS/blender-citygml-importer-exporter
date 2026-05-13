# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
import bpy, bmesh
from .appearance import _resolve_image_path
from .xml_utils import _norm_id
from ...shared.materials_common import normalize_crs_to_epsg
import re

# Alias for backward compatibility
_normalize_epsg = normalize_crs_to_epsg

# materials.py

def _map_surface_type(label: str) -> str:
    """Normalisiert den Surface-Typ für das Custom Property "SurfaceTyp".

    - Für Construction-Boundaries (RoofSurface, WallSurface, GroundSurface, ...)
      wird auf diese Kernklassen abgebildet.
    - Für TrafficArea/AuxiliaryTrafficArea wird "GroundSurface" verwendet.
    - Für Building-spezifische Klassen (Storey, BuildingConstructiveElement, Window, Door)
      werden sprechende Typen zurückgegeben.
    - Für alle anderen Labels (z.B. CityFurniture, SolitaryVegetationObject, PlantCover,
      Bridge ohne ConstructionSurfaces, GenericSpaces, ADE-Klassen) wird der
      ursprüngliche Label-Name beibehalten, um die CityGML 3 Feature-Typen nicht
      künstlich zu "WallSurface" zu verflachen.
    """
    l = (label or "").strip()
    if not l:
        return "Unknown"

    # CompositeSurface ist ein Geometrie-Container und darf nicht als SurfaceTyp persistiert werden.
    # Falls dennoch ein Label "CompositeSurface" durchrutscht, mappen wir defensiv auf einen gültigen Typ.
    if l == "CompositeSurface":
        # CompositeSurface ist ein reiner Geometrie-Container.
        # Wenn dieser Wert hier auftaucht, fehlt die thematische Zuordnung -> bewusst "Unknown".
        return "Unknown"

    # Kern-Surfaces (Construction-Modul)
    if l in ("RoofSurface", "WallSurface", "GroundSurface"):
        return l
    # Erweiterte Construction-Surfaces (CityGML 3.0)
    if l in ("CeilingSurface", "OuterCeilingSurface", "InteriorWallSurface", 
             "FloorSurface", "OuterFloorSurface", "ClosureSurface"):
        return l
    
    # Transportation (CityGML 3.0)
    if l in ("AuxiliaryTrafficArea", "AuxiliaryTrafficSpace"):
        return l
    if l in ("TrafficArea", "TrafficSpace"):
        return l
    if l in ("Marking", "Hole", "HoleSurface"):
        return l
    
    # Bridge-spezifische Surfaces
    if l == "BridgeRoofSurface":
        return "RoofSurface"
    if l == "BridgeWallSurface":
        return "WallSurface"
    if l == "BridgeGroundSurface":
        return "GroundSurface"
    
    # Tunnel-spezifische Surfaces (CityGML 3.0)
    if l == "TunnelRoofSurface":
        return "RoofSurface"
    if l == "TunnelWallSurface":
        return "WallSurface"
    if l == "TunnelGroundSurface":
        return "GroundSurface"
    if l == "TunnelCeilingSurface":
        return "CeilingSurface"
    if l == "TunnelFloorSurface":
        return "FloorSurface"

    # Öffnungen / Gebäude-spezifische Klassen
    if l == "Window":
        return "Window"
    if l == "Door":
        return "Door"
    if l == "WindowSurface":
        return "WindowSurface"
    if l == "DoorSurface":
        return "DoorSurface"
    if l == "Storey":
        return "Storey"
    if l == "BuildingConstructiveElement":
        return "BuildingElement"

    # Generische Erkennung von *RoofSurface / *WallSurface / *GroundSurface
    if l.endswith("RoofSurface"):
        return "RoofSurface"
    if l.endswith("WallSurface"):
        return "WallSurface"
    if l.endswith("GroundSurface"):
        return "GroundSurface"

    # Default: CityGML-Feature-Typ beibehalten (z.B. CityFurniture,
    # SolitaryVegetationObject, PlantCover, Bridge, GenericSpace, ADE-Klasse, ...)
    return l


def _compact_material_id(raw_id: object, fallback: object = "") -> str:
    text = str(raw_id or "").strip()
    if text.startswith("ID_"):
        text = text[3:]
    elif text.startswith("UUID_"):
        text = text[5:]
    text = "".join(ch for ch in text if ch.isalnum())
    if text:
        return text[-12:]
    fallback_text = "".join(ch for ch in str(fallback or "").strip() if ch.isalnum())
    if fallback_text:
        return fallback_text[-12:]
    return "material"


def _interior_material_prefix(surface_type: str) -> str:
    if surface_type in {"CeilingSurface", "OuterCeilingSurface", "RoofSurface"}:
        return "Ceiling"
    if surface_type in {"FloorSurface", "OuterFloorSurface", "GroundSurface"}:
        return "Floor"
    if surface_type == "ClosureSurface":
        return "Closure"
    return "InteriorWall"


def _preferred_material_name(surface_type: str, effective_poly_id: str, ring_id: str, *, is_gml_interior_ring: bool) -> str:
    if is_gml_interior_ring:
        return f"{_interior_material_prefix(surface_type)}_{_compact_material_id(ring_id, effective_poly_id)}"
    if surface_type in {"Window", "Door", "WindowSurface", "DoorSurface"}:
        return f"{surface_type}_Opening_Exterior_{_compact_material_id(effective_poly_id)}"
    return f"{effective_poly_id}"


def _apply_surface_semantics(
    mat,
    surface_type: str,
    surface_id: str = "",
    parent_surface_id: str = "",
    parent_surface_type: str = "",
) -> None:
    mat["SurfaceTyp"] = surface_type
    mat["surface_type"] = surface_type
    mat["Typ"] = surface_type

    if surface_type not in {"Window", "Door", "WindowSurface", "DoorSurface"}:
        return

    opening_type = "Window" if "Window" in surface_type else "Door"
    mat["OpeningType"] = opening_type
    mat["opening_type"] = opening_type
    mat["CityGMLTarget"] = opening_type
    mat["is_opening"] = True
    if opening_type == "Window":
        mat["hasWindows"] = 1
    if surface_id:
        mat["opening_id"] = surface_id
        mat["opening_gml_id"] = surface_id
        mat["con_opening_id"] = surface_id
    if parent_surface_id:
        mat["filling_parent_surface_id"] = parent_surface_id
        mat["opening_surface_id"] = parent_surface_id
    if parent_surface_type:
        mat["BoundarySurfaceType"] = parent_surface_type
        mat["opening_surface_type"] = parent_surface_type

def get_or_create_material_by_name(name: str, texfile: str | None, color=None):
    # Konvertiere color zu RGBA
    if color:
        try:
            rgba = (float(color[0]), float(color[1]), float(color[2]), 1.0)
        except Exception:
            rgba = (0.8, 0.8, 0.8, 1.0)
    else:
        rgba = (0.8, 0.8, 0.8, 1.0)

    # Bei Import: Name ist Identität (keine Property-Deduplizierung).
    if name and name in bpy.data.materials:
        mat = bpy.data.materials[name]
        # Only set up nodes if texture or color is explicitly provided
        if texfile or color:
            try:
                mat.use_nodes = True
            except Exception:
                pass
        return mat

    mat = bpy.data.materials.new(name=name or "ring_unknown")

    # PERFORMANCE: Only create full node tree when textures or custom colors are used.
    # For default-colored materials (no texture, no color), skip the expensive node tree
    # setup. This reduces material creation from ~50ms to ~1ms per material.
    # Custom properties (gml_polygon_id, SurfaceTyp, etc.) work independently of nodes.
    if texfile:
        mat.use_nodes = True
        nt = mat.node_tree
        nt.nodes.clear()
        out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (0,0)
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (-200,0)
        nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
        tex = nt.nodes.new("ShaderNodeTexImage"); tex.location = (-600,0)
        try:
            img = bpy.data.images.load(bpy.path.abspath(texfile), check_existing=True)
            tex.image = img
        except Exception:
            pass
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    elif color:
        mat.use_nodes = True
        nt = mat.node_tree
        nt.nodes.clear()
        out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (0,0)
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (-200,0)
        nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
        try:
            bsdf.inputs["Base Color"].default_value = rgba
        except Exception:
            pass
    else:
        # Fast path: no texture, no custom color → lightweight material (no node tree)
        mat.diffuse_color = rgba

    # WICHTIG: Speichere Original-X3D-Farbe als Custom Properties für Export-Roundtrip
    if color:
        try:
            mat["x3d_diffuseColor_r"] = float(color[0])
            mat["x3d_diffuseColor_g"] = float(color[1])
            mat["x3d_diffuseColor_b"] = float(color[2])
        except Exception:
            pass

    return mat

def ensure_material_slot(obj, mat):
    ms = obj.data.materials
    # Fast path: check if material is already the last appended one (common case)
    count = len(ms)
    if count > 0 and ms[count - 1] and ms[count - 1].name == mat.name:
        return count - 1
    for idx, m in enumerate(ms):
        if m and m.name == mat.name:
            return idx
    ms.append(mat)
    return len(ms) - 1

def _order_uvs_for_face(face, ring_xyz, ring_uvs, tol=1e-6):
    """
    Ordnet ring_uvs (gleiche Länge wie ring_xyz) der tatsächlichen Loop-Reihenfolge des BMFace zu.
    Entfernt ggf. den duplizierten Schlussknoten im Ring.
    Korrigiert Winding, falls Ring-Orientierung vs. Face-Orientierung abweicht.
    """
    # 1) ggf. geschlossenen Ring öffnen
    def _eq(a,b):
        return (abs(a[0]-b[0])<=tol and abs(a[1]-b[1])<=tol and abs(a[2]-b[2])<=tol)
    if len(ring_xyz) >= 2 and _eq(ring_xyz[0], ring_xyz[-1]):
        ring_xyz = ring_xyz[:-1]
        ring_uvs = ring_uvs[:-1]

    # 2) Build KD-ähnliche Zuordnung: pro Loop den besten Ring-Index finden
    import math
    loop_order = []
    for l in face.loops:
        vx,vy,vz = float(l.vert.co.x), float(l.vert.co.y), float(l.vert.co.z)
        best_i = -1
        best_d = 1e30
        for i,(x,y,z) in enumerate(ring_xyz):
            d = (vx-x)*(vx-x) + (vy-y)*(vy-y) + (vz-z)*(vz-z)
            if d < best_d:
                best_d = d; best_i = i
        loop_order.append(best_i)

    # 3) Falls Anzahl nicht passt, retten wir minimal: clamp auf gültige Indizes
    n = len(ring_uvs)
    loop_order = [i if 0 <= i < n else 0 for i in loop_order]

    ordered_uvs = [ring_uvs[i] for i in loop_order]

    # 4) Winding-Prüfung: 2D-Flächenorientierung in UV vergleichen
    #   Wenn UV-Polygon negativ orientiert, invertieren
    def _signed_area(pts):
        s = 0.0
        for i in range(len(pts)):
            x1,y1 = pts[i]
            x2,y2 = pts[(i+1)%len(pts)]
            s += x1*y2 - x2*y1
        return 0.5*s
    if len(ordered_uvs) >= 3 and _signed_area(ordered_uvs) < 0:
        ordered_uvs.reverse()

    return ordered_uvs

def apply_materials_uvs(
    obj,
    mesh,
    surf_labels,
    matcolor_by_surface_id,
    ptex_by_ring,
    gtex_by_poly,
    poly_to_image,
    gml_path,
    ptex_by_poly=None,
    surface_attrs_by_poly=None,
    surface_id_by_poly=None,
    multisurface_id_by_poly=None,
    compositesurface_id_by_poly=None,
):
    ptex_by_poly = ptex_by_poly or {}
    surface_attrs_by_poly = surface_attrs_by_poly or {}
    surface_id_by_poly = surface_id_by_poly or {}
    multisurface_id_by_poly = multisurface_id_by_poly or {}
    compositesurface_id_by_poly = compositesurface_id_by_poly or {}

    # Sammelstrukturen für Interior-Surfaces (nur Flag pro Material)
    # Material-Level Interior-Markierungen.
    # Neben echten thematischen Interior-Surfaces markieren wir in CityGML 2
    # auch Polygone mit gml:interior-Ringen, damit die Custom Properties zum
    # Verhalten des CityGML-3-Imports passen.
    mats_with_interior = set()
    # Zuordnung: poly_id (Materialname) -> exterior gml:LinearRing-ID
    exterior_ring_by_poly = {}
    # Preserve original PTX ring start UV for stable roundtrip export (bit-identical TexCoordList order)
    ring_uv_start_by_ring_id = {}
    try:
        obj.data["cgml3_uv_start_by_ring"] = {}
    except Exception:
        pass

    # Zähle, wie viele Polygone zu jeder MultiSurface+Surface-Kombination gehören
    import collections
    multisurface_member_count = collections.defaultdict(int)
    for i, item in enumerate(surf_labels):
        poly_id = item[1]
        ms_id = multisurface_id_by_poly.get(poly_id, "")
        surf_id = surface_id_by_poly.get(poly_id, "")
        if ms_id:
            # Kombination aus MultiSurface-ID und Surface-ID als Schlüssel
            # Bei CityFurniture/Bridge ohne thematische Surfaces ist surf_id leer
            key = (ms_id, surf_id) if surf_id else (ms_id, "")
            multisurface_member_count[key] += 1

    def _strip_duplicate_close_uv(uvs_in, tol=1e-9):
        """Remove duplicate closing UV if first==last."""
        if not uvs_in:
            return uvs_in
        if len(uvs_in) >= 2:
            (u0, v0) = uvs_in[0]
            (u1, v1) = uvs_in[-1]
            if abs(u0 - u1) <= tol and abs(v0 - v1) <= tol:
                return uvs_in[:-1]
        return uvs_in

    def _adjust_uv_count_for_face(loops, uvs_in):
        """
        Adjust UV count to match loop count:
        - If UVs include a closing duplicate vertex: drop it.
        - If UVs are still short by exactly 1: assume missing closing vertex and append first UV.
        """
        uvs = list(uvs_in or [])
        if not uvs:
            return uvs
        uvs = _strip_duplicate_close_uv(uvs)
        if len(uvs) == len(loops):
            return uvs
        if len(uvs) + 1 == len(loops):
            uvs.append(uvs[0])
            return uvs
        return uvs

    bm = bmesh.new(); bm.from_mesh(mesh); bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv.verify()
    coords = [v.co.copy() for v in bm.verts]

    # ------------------------------------------------------------
    # ROBUST: surf_labels ↔ bm.faces nicht per Index, sondern per
    # Vertex-Index-Signatur (weil Blender Face-Reihenfolge ändern kann)
    # ------------------------------------------------------------
    entry_queue_by_key = collections.defaultdict(list)

    # surf_labels: (label, poly_id, ring_id, srs_poly, idxs, lod_num)
    # Wir müssen lod_num ebenfalls pro Face erhalten (für mat["lod"]).
    for order, item in enumerate(surf_labels):
        parts = list(item) + ["", "", None, None]  # garantiert mind. 6 Felder
        label, poly_id, ring_id, srs_poly, idxs, lod_num = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]

        if idxs:
            entry_queue_by_key[frozenset(idxs)].append((order, label, poly_id, ring_id, srs_poly, lod_num))

    # Map: face_index -> (order, label, poly_id, ring_id, srs_poly, lod_num)
    entry_by_face_index = {}

    for face_index, f in enumerate(bm.faces):
        key = frozenset(v.index for v in f.verts)
        if entry_queue_by_key.get(key):
            entry_by_face_index[face_index] = entry_queue_by_key[key].pop(0)

    # Fallback: falls für einzelne Faces keine Signatur vorhanden ist (alte Projekte / alte Imports)
    for face_index, f in enumerate(bm.faces):
        if face_index in entry_by_face_index:
            continue
        if face_index < len(surf_labels):
            parts = list(surf_labels[face_index]) + ["", "", None]
            entry_by_face_index[face_index] = (face_index, parts[0], parts[1], parts[2], parts[3], (parts[5] if len(parts) > 5 else None))

    # faces_by_poly in ring-order (order = Reihenfolge in surf_labels)
    faces_by_poly = collections.defaultdict(list)
    for face_index, (order, label, poly_id, ring_id, srs_poly, _lod_num) in entry_by_face_index.items():
        faces_by_poly[poly_id].append((order, face_index))

    for poly_id, lst in list(faces_by_poly.items()):
        lst.sort(key=lambda x: x[0])
        faces_by_poly[poly_id] = [fi for _, fi in lst]

    polys_with_gml_interior_rings = {
        poly_id for poly_id, face_indices in faces_by_poly.items()
        if len(face_indices) > 1
    }

    def _is_thematic_interior_surface(label: str) -> bool:
        st = _map_surface_type(label)
        return st in ("InteriorWallSurface", "CeilingSurface", "FloorSurface")

    
    def _register_interior_face(is_exterior, loops, uv_layer, mat):
        """Registriert, dass ein Material mindestens eine Interior-Surface besitzt.

        Die konkreten Geometrie- und TexCoord-Listen werden nicht mehr
        als Custom Properties gespeichert. gml:interior-Ringe werden
        separat über die Polygon-Gruppierung markiert.
        """
        if is_exterior:
            return
        mats_with_interior.add(mat.name)

    interior_ring_counter = {}  # poly_id -> nächster Interior-Ring-Index (ab 1)

    for i, f in enumerate(bm.faces):
        if i not in entry_by_face_index:
            continue

        order, label, poly_id, ring_id, srs_poly, lod_num = entry_by_face_index[i]

        # Robustere Key-Nutzung (falls irgendwo "#id" vorkommt)
        poly_key = poly_id or ""
        poly_key_norm = _norm_id(poly_key) if poly_key else ""

        def _get_by_poly(dct, key, key_norm):
            if not dct:
                return ""
            return dct.get(key, "") or (dct.get(key_norm, "") if key_norm else "")

        # IDs der zugehörigen Surface / MultiSurface / CompositeSurface (falls vorhanden)
        surf_id_norm = _get_by_poly(surface_id_by_poly, poly_key, poly_key_norm)
        ms_id_norm   = _get_by_poly(multisurface_id_by_poly, poly_key, poly_key_norm)

        # Exterior/Interior:
        # CityGML-Loch-Ringe (gml:interior) sind keine "InteriorSurface" im thematischen Sinn.
        # Sie sind lediglich innere Ringe desselben gml:Polygon (Hole). Für das Export-Flag
        # auf Material-Ebene ("Interior") dürfen wir sie daher NICHT als InteriorSurface zählen.
        # Deshalb: nie "interior" allein aus "mehrere Faces pro poly_id" ableiten.
        #
        # Stattdessen: "Interior" nur fÇ¬r echte thematische Interior-Surfaces setzen.
        is_exterior = not _is_thematic_interior_surface(label)

        color = matcolor_by_surface_id.get(poly_id)
        ring_id_norm = _norm_id(ring_id)
        # Exterior-Face → exterior gml:LinearRing-ID merken (für Interior-Material-Infos)
        if is_exterior and poly_id not in exterior_ring_by_poly:
            exterior_ring_by_poly[poly_id] = ring_id_norm or ring_id or ""
        ptex = ptex_by_ring.get(ring_id_norm) if ring_id_norm else None
        gtex = gtex_by_poly.get(poly_id)

        # NEU: Wenn PTX existiert (Ring oder Poly), GTX ignorieren
        if (ring_id_norm and ring_id_norm in ptex_by_ring) or ptex_by_poly.get(poly_id):
            gtex = None

        # Bilddatei priorisieren
        texfile = None
        if ptex and ptex.get("image"):
            texfile = ptex["image"]
        if not texfile:
            texfile = poly_to_image.get(poly_id)
        if texfile:
            texfile = _resolve_image_path(gml_path, texfile)

        # Prüfe ob dieses Face ein Interior-Ring (Loch) desselben Polygons ist
        _face_list = faces_by_poly.get(poly_id, [])
        is_gml_interior_ring = len(_face_list) > 1 and _face_list[0] != i

        if is_gml_interior_ring:
            # Interior-Ring bekommt eigene Polygon-ID die hochzählt
            ridx = interior_ring_counter.get(poly_id, 1)
            interior_ring_counter[poly_id] = ridx + 1
            effective_poly_id = f"{poly_id}_{ridx}"
        else:
            effective_poly_id = poly_id

        surface_type = _map_surface_type(label)
        surf_attrs = surface_attrs_by_poly.get(poly_id) or {}
        parent_surface_id = str(
            surf_attrs.get("filling_parent_surface_id")
            or surf_attrs.get("opening_surface_id")
            or ""
        ).strip()
        parent_surface_type = str(
            surf_attrs.get("BoundarySurfaceType")
            or surf_attrs.get("opening_surface_type")
            or ""
        ).strip()
        mat_name = _preferred_material_name(
            surface_type,
            effective_poly_id,
            ring_id_norm or ring_id or effective_poly_id,
            is_gml_interior_ring=is_gml_interior_ring,
        )
        mat = get_or_create_material_by_name(mat_name, texfile, color if not texfile else None)
        f.material_index = ensure_material_slot(obj, mat)

        # Surface-Typ (normalisiert) IMMER setzen (nicht im try-Block verstecken),
        # damit er nicht "verschwindet", falls später etwas fehlschlägt.
        try:
            _apply_surface_semantics(
                mat,
                surface_type,
                surf_id_norm or "",
                parent_surface_id,
                parent_surface_type,
            )
        except Exception:
            mat["SurfaceTyp"] = str(label or "Unknown")
        # LoD als Custom Property speichern, falls vorhanden
        if lod_num is not None:
            try:
                mat["lod"] = int(lod_num)
            except Exception:
                mat["lod"] = str(lod_num)

        # Pro-Material-EPSG speichern, wenn vorhanden
        try:
            epsg = _normalize_epsg(srs_poly)
            # Nur gültige EPSG-Codes speichern (nicht "Unknown CRS" oder leere Strings)
            # Bei lokalem Import (ohne Georeferenz) kein EPSG speichern
            if epsg and epsg != "Unknown CRS" and not epsg.startswith("Unknown"):
                _skip_epsg = False
                try:
                    _skip_epsg = bool(bpy.context.scene.cgml3.import_local)
                except Exception:
                    pass
                if not _skip_epsg:
                    mat["EPSG"] = epsg
        except Exception:
            pass

        try:
            # Linearring-ID wie bisher
            mat["gml_ring_id"] = ring_id_norm or ""

            # Polygon-ID explizit speichern (Interior-Ringe bekommen eigene hochgezählte ID)
            mat["gml_polygon_id"] = effective_poly_id or ""

            # Interior-Ring: Relation zum Exterior-Polygon speichern
            if is_gml_interior_ring:
                mat["ExteriorPolyId"] = poly_id
                mat["Interior"] = True

            if surf_id_norm:
                mat["con_surface_id"] = surf_id_norm

            if ms_id_norm:
                mat["gml_multisurface_id"] = ms_id_norm

                # Nur als MultiSurface-Member kennzeichnen, wenn MEHRERE Polygone
                # dieselbe MultiSurface+Surface-Kombination teilen
                key = (ms_id_norm, surf_id_norm) if surf_id_norm else (ms_id_norm, "")
                if multisurface_member_count.get(key, 0) > 1:
                    mat["is_multisurface_member"] = True

            # BuildingInstallation: immer als MultiSurface exportieren (auch bei nur 1 Polygon)
            if surface_type == "BuildingInstallation":
                # Falls die Datei keine MultiSurface-ID liefert: stabile ID aus der Installation ableiten
                if (not ms_id_norm) and surf_id_norm:
                    ms_id_norm = f"MS_{surf_id_norm}"
                    mat["gml_multisurface_id"] = ms_id_norm
                if ms_id_norm:
                    mat["is_multisurface_member"] = True

            # CompositeSurface-ID speichern (falls vorhanden) – Lookup auch über normierten Key
            cs_id = _get_by_poly(compositesurface_id_by_poly, poly_key, poly_key_norm)
            if cs_id:
                mat["gml_compositesurface_id"] = cs_id
                mat["is_compositesurface_member"] = True

            # Ziel-ID für Appearance (wie bisher)
            # Fallback: Immer poly_id als app_target_or_uri, wenn keine Texturzuordnung
            if ptex and ptex.get("poly"):
                target_id = ptex.get("poly")
            elif gtex or poly_to_image.get(poly_id):
                target_id = poly_id
            else:
                target_id = poly_id
            mat["app_target_or_uri"] = str(target_id)
        except Exception:
            pass

        # GenericAttributes der Surface → Material-Custom-Properties
        if surf_attrs:
            for key, val in surf_attrs.items():
                try:
                    mat[key] = val
                except Exception:
                    mat[key] = str(val)


        loops = list(f.loops)

        # ptex pro Polygon falls kein Ring-UV
        if not (ptex and ptex.get("uv")):
            ptex_poly = ptex_by_poly.get(poly_id)
        else:
            ptex_poly = None

        if ptex_poly:
            uvs = _adjust_uv_count_for_face(loops, list(ptex_poly.get("uv") or []))
            # Remember original start UV from the GML TexCoordList (poly-level) if no ring-level UV was present.
            # IMPORTANT: store from the raw GML order.
            try:
                rid_for_start = ""
                try:
                    rid_for_start = _norm_id(ptex_poly.get("ring", "")) if isinstance(ptex_poly, dict) else ""
                except Exception:
                    rid_for_start = ""
                start_src = None
                if isinstance(ptex_poly, dict):
                    start_src = ptex_poly.get("ring_uv_start", None)
                if start_src is None and uvs:
                    start_src = uvs[0]
                if rid_for_start and start_src:
                    ring_uv_start_by_ring_id[rid_for_start] = (float(start_src[0]), float(start_src[1]))
                    try:
                        d = dict(obj.data.get("cgml3_uv_start_by_ring", {}) or {})
                        d[rid_for_start] = (float(start_src[0]), float(start_src[1]))
                        obj.data["cgml3_uv_start_by_ring"] = d
                    except Exception:
                        pass
            except Exception:
                pass

            is_exterior = faces_by_poly[poly_id] and faces_by_poly[poly_id][0] == i
            if is_exterior and len(uvs) >= 3:
                ordered = uvs if len(uvs) == len(loops) else _order_uvs_for_face(
                    f,
                    [(float(l.vert.co.x), float(l.vert.co.y), float(l.vert.co.z)) for l in loops],
                    uvs
                )
                # Winding stabil halten
                def _sa(pts):
                    s=0.0
                    for k in range(len(pts)):
                        x1,y1=pts[k]; x2,y2=pts[(k+1)%len(pts)]
                        s += x1*y2 - x2*y1
                    return 0.5*s
                if len(ordered) >= 3 and _sa(ordered) < 0:
                    ordered = list(reversed(ordered))
                for l, (uu, vv) in zip(loops, ordered):
                    l[uv_layer].uv = (uu, vv)
            else:
                pass

                if not texfile:
                    tex_src = (ptex_poly.get("image") or poly_to_image.get(poly_id))
                    if tex_src:
                        texfile = _resolve_image_path(gml_path, tex_src)
                        mat = get_or_create_material_by_name(f"{poly_id}", texfile, color if not texfile else None)
                        f.material_index = ensure_material_slot(obj, mat)
                # Nur thematische Interior-Surfaces kennzeichnen, nicht gml:interior (Hole-Ringe).
                _register_interior_face(is_exterior, loops, uv_layer, mat)
                continue

        # PTX Ring → direkt mappen
        if ptex and ptex.get("uv"):
            """ if "poly" in ptex and ptex["poly"] != poly_id:
                print(f"[CityGML3] UV poly mismatch: face.poly={poly_id} vs ptex.poly={ptex['poly']} for ring_id={ring_id_norm}") """
            uvs = _adjust_uv_count_for_face(loops, list(ptex.get("uv") or []))
            ring_xyz = [(float(l.vert.co.x), float(l.vert.co.y), float(l.vert.co.z)) for l in loops]
            ordered = _order_uvs_for_face(f, ring_xyz, uvs)
            for l, (uu, vv) in zip(loops, ordered):
                l[uv_layer].uv = (uu, vv)
            # Remember original start UV from the GML TexCoordList (not the reordered loop UVs)
            try:
                if ring_id_norm and uvs:
                    ring_uv_start_by_ring_id[ring_id_norm] = (float(uvs[0][0]), float(uvs[0][1]))
                    try:
                        d = dict(obj.data.get("cgml3_uv_start_by_ring", {}) or {})
                        d[ring_id_norm] = (float(uvs[0][0]), float(uvs[0][1]))
                        obj.data["cgml3_uv_start_by_ring"] = d
                    except Exception:
                        pass
            except Exception:
                pass
            # Nur thematische Interior-Surfaces kennzeichnen, nicht gml:interior (Hole-Ringe).
            _register_interior_face(is_exterior, loops, uv_layer, mat)
            continue

        # GTX Fallback nur wenn KEIN PTX
        if gtex:
            x0, y0, _ = gtex["refpt"]; a,b,c,d = gtex["M"]
            uv_raw = []
            for l in loops:
                vco = coords[l.vert.index]
                uu = a*float(vco.x) + b*float(vco.y) - (a*x0 + b*y0)
                vv = c*float(vco.x) + d*float(vco.y) - (c*x0 + d*y0)
                uv_raw.append((uu, vv))
            ring_xyz = [(float(l.vert.co.x), float(l.vert.co.y), float(l.vert.co.z)) for l in loops]
            ordered = _order_uvs_for_face(f, ring_xyz, uv_raw)
            for l, (uu, vv) in zip(loops, ordered):
                l[uv_layer].uv = (uu, vv)
            # Nur thematische Interior-Surfaces kennzeichnen, nicht gml:interior (Hole-Ringe).
            _register_interior_face(is_exterior, loops, uv_layer, mat)
            continue

        # Hinweis, falls Exterior ohne TexCoordList
        is_exterior = faces_by_poly[poly_id] and faces_by_poly[poly_id][0] == i
        if texfile and is_exterior and not (ring_id_norm and ring_id_norm in ptex_by_ring):
            print(f"[CityGML3] Missing TexCoordList for ring_id={ring_id or '(empty)'} of exterior; poly_id={poly_id}; label={label} | known_rings={len(ptex_by_ring)}")
        # Fallback-Case: nur thematische Interior-Surfaces kennzeichnen.
        _register_interior_face(is_exterior, loops, uv_layer, mat)

    bm.to_mesh(mesh); bm.free()

   # Interior-Flags und Exterior-Bezug an die Materialien schreiben.
   # Nur für thematische Interior-Surfaces (mats_with_interior).
   # gml:interior-Ringe haben Interior/ExteriorPolyId bereits im Hauptloop gesetzt.
   # polys_with_gml_interior_rings NICHT verwenden — das würde das Exterior-Material
   # fälschlich als Interior markieren.
    for mat in obj.data.materials:
        if not mat:
            continue
        name = mat.name
        if name in mats_with_interior:
            try:
                mat["Interior"] = True

                if not mat.get("ExteriorPolyId"):
                    ext_ring_id = exterior_ring_by_poly.get(name, "")
                    mat["ExteriorPolyId"] = name          # = gml:id des Polygons
                    mat["ExteriorRingId"] = ext_ring_id   # = gml:id des Exterior-LinearRings
            except Exception:
                mat["Interior"] = True

    # Persist ring start UV on material for exporter (ring id is stored per material already)
    for mat in obj.data.materials:
        if not mat:
            continue
        try:
            rid = _norm_id(mat.get("gml_ring_id", ""))
        except Exception:
            rid = ""
        if not rid:
            continue
        try:
            if rid in ring_uv_start_by_ring_id:
                u, v = ring_uv_start_by_ring_id[rid]
                mat["cgml3_uv_start"] = (float(u), float(v))
        except Exception:
            pass
