# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
import bpy, bmesh
from .appearance import _resolve_image_path
from .xml_utils import _norm_id
from ...shared.materials_common import normalize_crs_to_epsg
import json
import re

# OPTIMIZATION: Global Material Cache für Deduplizierung
try:
    from ...shared.material_cache import get_global_material_cache
    MATERIAL_CACHE_AVAILABLE = True
except ImportError:
    MATERIAL_CACHE_AVAILABLE = False
    print("[CityGML] Warning: Material cache not available")

# Alias for backward compatibility
_normalize_epsg = normalize_crs_to_epsg

# OPTIMIZED: Statisches Dictionary statt if/elif-Kette für O(1) Lookup
_SURFACE_TYPE_MAP = {
    # CityGML 3.0 thematische Boundary-Surfaces
    "RoofSurface": "RoofSurface",
    "WallSurface": "WallSurface",
    "GroundSurface": "GroundSurface",
    "CeilingSurface": "CeilingSurface",
    "OuterCeilingSurface": "OuterCeilingSurface",
    "InteriorWallSurface": "InteriorWallSurface",
    "FloorSurface": "FloorSurface",
    "OuterFloorSurface": "OuterFloorSurface",
    "ClosureSurface": "ClosureSurface",
    "WindowSurface": "WindowSurface",
    "DoorSurface": "DoorSurface",
    "fillingSurface": "fillingSurface",
    # Building-Feature-Typen
    "BuildingFurniture": "BuildingFurniture",
    "IntBuildingFurniture": "BuildingFurniture",
    "BuildingInstallation": "BuildingInstallation",
    "IntBuildingInstallation": "BuildingInstallation",
    "BuildingRoom": "BuildingRoom",
    "Room": "BuildingRoom",
    "BuildingConstructiveElement": "BuildingElement",
    "Storey": "Storey",
    # Bridge-Feature-Typen
    "BridgeInstallation": "BridgeInstallation",
    "IntBridgeInstallation": "BridgeInstallation",
    "BridgeConstructiveElement": "BridgeElement",
    "IntBridgeConstructionElement": "BridgeElement",
    "BridgeRoofSurface": "RoofSurface",
    "BridgeWallSurface": "WallSurface",
    "BridgeGroundSurface": "GroundSurface",
    # Tunnel-Feature-Typen
    "TunnelInstallation": "TunnelInstallation",
    "IntTunnelInstallation": "TunnelInstallation",
    "TunnelRoofSurface": "RoofSurface",
    "TunnelWallSurface": "WallSurface",
    "TunnelGroundSurface": "GroundSurface",
    "TunnelCeilingSurface": "CeilingSurface",
    "TunnelFloorSurface": "FloorSurface",
    # Transportation
    "AuxiliaryTrafficArea": "AuxiliaryTrafficArea",
    "AuxiliaryTrafficSpace": "AuxiliaryTrafficSpace",
    "TrafficArea": "TrafficArea",
    "TrafficSpace": "TrafficSpace",
    # Sonstige
    "Marking": "Marking",
    "Hole": "Hole",
    "HoleSurface": "HoleSurface",
    "Window": "Window",
    "Door": "Door",
    # CompositeSurface → Unknown
    "CompositeSurface": "Unknown",
}

def _map_surface_type(label: str) -> str:
    l = (label or "").strip()
    if not l:
        return "Unknown"
    # O(1) dict lookup statt O(n) if/elif-Kette
    result = _SURFACE_TYPE_MAP.get(l)
    if result is not None:
        return result
    # Fallback-Pattern für unbekannte aber klassifizierbare Typen
    if l.endswith("RoofSurface"):
        return "RoofSurface"
    if l.endswith("WallSurface"):
        return "WallSurface"
    if l.endswith("GroundSurface"):
        return "GroundSurface"
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
    return f"{surface_type}_{effective_poly_id}"


def _is_thematic_interior_surface(label: str) -> bool:
    st = _map_surface_type(label)
    return st in ("InteriorWallSurface", "CeilingSurface", "FloorSurface")


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

    # WICHTIG: Beim Import sollen unterschiedliche Surfaces (WallSurface, RoofSurface, ...)
    # eigene Materialien erhalten, auch wenn Farbe/Textur identisch sind.
    # Ein property-basierter Material-Cache würde verschiedene Surfaces fälschlich
    # auf dasselbe Material deduplizieren. Daher: Wenn ein Name vergeben ist, den
    # Namen als Identität behandeln und Material eindeutig pro Name anlegen.
    if name and name in bpy.data.materials:
        mat = bpy.data.materials[name]
        # Only set up nodes if texture or color is explicitly provided
        if texfile or color:
            try:
                mat.use_nodes = True
            except Exception:
                pass
        return mat

    # Hinweis: MaterialCache ist weiterhin nützlich für Exporte o.ä.,
    # aber beim Import hier nicht verwenden (würde Semantik zerstören).
    
    mat = bpy.data.materials.new(name=name or "ring_unknown")

    # PERFORMANCE: Only create full node tree when textures or custom colors are used.
    # For default-colored materials (no texture, no color), skip the expensive node tree
    # setup. Custom properties (gml_polygon_id, SurfaceTyp, etc.) work independently of nodes.
    if texfile:
        mat.use_nodes = True
        nt = mat.node_tree
        nt.nodes.clear()
        out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (0, 0)
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (-200, 0)
        nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
        tex = nt.nodes.new("ShaderNodeTexImage"); tex.location = (-600, 0)
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
        out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (0, 0)
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (-200, 0)
        nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
        try:
            bsdf.inputs["Base Color"].default_value = rgba
        except Exception:
            pass
    else:
        # Fast path: no texture, no custom color → lightweight material (no node tree)
        mat.diffuse_color = rgba
    return mat

def ensure_material_slot(obj, mat):
    ms = obj.data.materials
    # Fast path: check if material is already the last appended one (common case)
    count = len(ms)
    if count > 0 and ms[count - 1] and ms[count - 1].name == mat.name:
        return count - 1
    for i, slot in enumerate(obj.material_slots):
        if slot.material == mat:
            return i
    obj.data.materials.append(mat)
    return len(obj.material_slots) - 1


# OPTIMIZED: Cached Version von ensure_material_slot
# Die Standard-Version macht O(n) linear scan pro Aufruf.
# Bei 200+ Materialien wird das zu O(n²) Gesamtkomplexität.
# _mat_slot_cache wird pro apply_materials_uvs()-Aufruf frisch angelegt.
def _ensure_material_slot_cached(obj, mat, cache):
    """O(1) material slot lookup mit Dict-Cache."""
    mat_name = mat.name
    idx = cache.get(mat_name)
    if idx is not None:
        return idx
    # Slot noch nicht vorhanden – hinzufügen
    obj.data.materials.append(mat)
    idx = len(obj.material_slots) - 1
    cache[mat_name] = idx
    return idx


# OPTIMIZED: Template-basierte Material-Erstellung
# Materialien mit gleichen visuellen Eigenschaften (Textur + Farbe) teilen sich
# das gleiche Shader-Setup. Nur Metadaten (gml:id etc.) unterscheiden sich.
_node_tree_template_cache = {}  # (texfile, color_tuple) -> material_name (als Template)

def _get_or_create_material_fast(name, texfile, color):
    """Erstellt Material mit Node-Tree-Reuse für gleiche visuelle Eigenschaften."""
    # Prüfe ob Material mit diesem Namen bereits existiert
    if name and name in bpy.data.materials:
        mat = bpy.data.materials[name]
        try:
            mat.use_nodes = True
        except Exception:
            pass
        return mat

    # Farbe normalisieren
    if color:
        try:
            rgba = (float(color[0]), float(color[1]), float(color[2]), 1.0)
        except Exception:
            rgba = (0.8, 0.8, 0.8, 1.0)
    else:
        rgba = (0.8, 0.8, 0.8, 1.0)

    # Visueller Schlüssel: (Textur, Farbe)
    visual_key = (texfile or "", rgba)
    template_name = _node_tree_template_cache.get(visual_key)

    if template_name and template_name in bpy.data.materials:
        # Template existiert – neues Material als Kopie erstellen (schneller als Node-Tree-Aufbau)
        template_mat = bpy.data.materials[template_name]
        mat = template_mat.copy()
        mat.name = name or "ring_unknown"
        return mat

    # Kein Template vorhanden – Material komplett neu erstellen
    mat = bpy.data.materials.new(name=name or "ring_unknown")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (0, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (-200, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    if texfile:
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.location = (-600, 0)
        try:
            img = bpy.data.images.load(bpy.path.abspath(texfile), check_existing=True)
            tex.image = img
        except Exception:
            pass
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    else:
        try:
            bsdf.inputs["Base Color"].default_value = rgba
        except Exception:
            pass

    # Als Template registrieren für spätere Materialien mit gleichen visuellen Eigenschaften
    _node_tree_template_cache[visual_key] = mat.name
    return mat

def _order_uvs_for_face(face, ring_xyz, ring_uvs, tol=1e-6):
    """
    Ordnet die UVs in ring_uvs passend zur Loop-Reihenfolge des BMFace zu.
    Entfernt ggf. den duplizierten Schlussknoten im Ring.
    Korrigiert Winding, falls Ring-Orientierung vs. Face-Orientierung abweicht.

    OPTIMIZED: Hash-basiertes Lookup statt O(n*m) Brute-Force-Distanzberechnung.
    """
    def _eq(a,b):
        return (abs(a[0]-b[0])<=tol and abs(a[1]-b[1])<=tol and abs(a[2]-b[2])<=tol)
    if len(ring_xyz) >= 2 and _eq(ring_xyz[0], ring_xyz[-1]):
        ring_xyz = ring_xyz[:-1]
        ring_uvs = ring_uvs[:-1]

    # OPTIMIZED: Koordinaten-Dictionary für O(1) Lookup statt O(n*m)
    # Runde auf 5 Dezimalstellen (~0.01mm Genauigkeit) für robustes Matching
    _ROUND = 5
    coord_map = {}
    for i, (x, y, z) in enumerate(ring_xyz):
        key = (round(x, _ROUND), round(y, _ROUND), round(z, _ROUND))
        coord_map[key] = i

    loop_order = []
    for l in face.loops:
        vx, vy, vz = float(l.vert.co.x), float(l.vert.co.y), float(l.vert.co.z)
        key = (round(vx, _ROUND), round(vy, _ROUND), round(vz, _ROUND))
        best_i = coord_map.get(key, -1)
        if best_i == -1:
            # Fallback: Brute-Force-Suche bei Hash-Miss (z.B. bei numerischem Rauschen)
            best_d = 1e30
            for i, (x, y, z) in enumerate(ring_xyz):
                d = (vx-x)*(vx-x) + (vy-y)*(vy-y) + (vz-z)*(vz-z)
                if d < best_d:
                    best_d = d; best_i = i
        loop_order.append(best_i)

    n = len(ring_uvs)
    loop_order = [i if 0 <= i < n else 0 for i in loop_order]
    ordered_uvs = [ring_uvs[i] for i in loop_order]

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
    poly_also_in_solid=None,  # NEU: Markierung für Polygone die auch in Solid vorkommen
    filling_parent_surface_id_by_poly=None,  # NEU: Parent-Surface-ID für fillingSurface-Kinder
):
    ptex_by_poly = ptex_by_poly or {}
    surface_attrs_by_poly = surface_attrs_by_poly or {}
    surface_id_by_poly = surface_id_by_poly or {}
    multisurface_id_by_poly = multisurface_id_by_poly or {}
    compositesurface_id_by_poly = compositesurface_id_by_poly or {}
    poly_also_in_solid = poly_also_in_solid or {}  # NEU
    filling_parent_surface_id_by_poly = filling_parent_surface_id_by_poly or {}  # NEU

    mats_with_interior = set()
    exterior_ring_by_poly = {}
    # NEW: ring-specific UV start for bit-identical TexCoordList
    ring_uv_start_by_ring_id = {}
    ring_uv_sign_by_ring_id = {}
    try:
        obj.data["cgml3_uv_start_by_ring"] = {}
    except Exception:
        pass
    try:
        obj.data["cgml3_uv_sign_by_ring"] = {}
    except Exception:
        pass

    import collections
    multisurface_member_count = collections.defaultdict(int)
    for i, item in enumerate(surf_labels):
        poly_id = item[1]
        ms_id = multisurface_id_by_poly.get(poly_id, "")
        surf_id = surface_id_by_poly.get(poly_id, "")
        if ms_id:
            key = (ms_id, surf_id) if surf_id else (ms_id, "")
            multisurface_member_count[key] += 1

    bm = bmesh.new(); bm.from_mesh(mesh); bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv.verify()
    coords = [v.co.copy() for v in bm.verts]

    # OPTIMIZED: Material-Slot-Cache für O(1) Lookup (statt O(n) pro Face)
    _slot_cache = {}

    faces_by_poly = collections.defaultdict(list)
    for i, item in enumerate(surf_labels):
        poly_id = item[1]
        faces_by_poly[poly_id].append(i)

    non_hole_faces_by_poly = collections.defaultdict(list)
    for i, item in enumerate(surf_labels):
        poly_id = item[1]
        is_gml_interior_ring = bool(item[7]) if len(item) > 7 else False
        if poly_id and not is_gml_interior_ring:
            non_hole_faces_by_poly[poly_id].append(i)
    duplicate_non_hole_poly_ids = {
        poly_id for poly_id, face_ids in non_hole_faces_by_poly.items() if len(face_ids) > 1
    }

    def _register_interior_face(is_exterior, loops, uv_layer, mat):
        if is_exterior:
            return
        mats_with_interior.add(mat.name)

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
            # Very common in GML rings: geometry has closing coordinate, UV list omits it.
            uvs.append(uvs[0])
            return uvs
        return uvs

    def _uv_sign(uvs_in):
        uvs = _strip_duplicate_close_uv(list(uvs_in or []))
        if len(uvs) < 3:
            return 0
        area = 0.0
        for i, (x1, y1) in enumerate(uvs):
            x2, y2 = uvs[(i + 1) % len(uvs)]
            area += x1 * y2 - x2 * y1
        if area > 1e-12:
            return 1
        if area < -1e-12:
            return -1
        return 0

    interior_ring_counter = {}  # poly_id -> nächster Interior-Ring-Index (ab 1)

    for i, f in enumerate(bm.faces):
        # surf_labels: (label, poly_id, ring_id, srs_poly, idxs, lod_num, in_shell, is_gml_interior_ring)
        parts_raw = list(surf_labels[i])
        has_explicit_ring_flag = len(parts_raw) > 7
        parts = parts_raw + ["", None, False, False]  # Padding für ältere Formate
        label, poly_id, ring_id, srs_poly, idxs, lod_num, in_shell, surf_label_is_gml_interior = (
            parts[0],
            parts[1],
            parts[2],
            parts[3],
            parts[4],
            parts[5],
            parts[6],
            parts[7],
        )
        # io/reader/geometry.py may provide a lod_num as 6th element (like io_v2).
        lod_num = parts[5] if len(parts) > 5 else None
        surf_id_norm = surface_id_by_poly.get(poly_id, "")
        ms_id_norm = multisurface_id_by_poly.get(poly_id, "")

        faces_for_poly = faces_by_poly[poly_id]
        is_gml_interior_ring = bool(surf_label_is_gml_interior)
        if not is_gml_interior_ring:
            # Fallback für ältere surf_labels ohne explizites Innenring-Flag.
            is_gml_interior_ring = (not has_explicit_ring_flag) and len(faces_for_poly) > 1 and faces_for_poly[0] != i
        is_thematic_interior = _is_thematic_interior_surface(label)
        is_exterior = not (is_thematic_interior or is_gml_interior_ring)

        if is_gml_interior_ring:
            ridx = interior_ring_counter.get(poly_id, 1)
            interior_ring_counter[poly_id] = ridx + 1
            effective_poly_id = f"{poly_id}_{ridx}"
        else:
            effective_poly_id = poly_id

        color = matcolor_by_surface_id.get(poly_id)
        ring_id_norm = _norm_id(ring_id)
        if is_exterior and poly_id not in exterior_ring_by_poly:
            exterior_ring_by_poly[poly_id] = ring_id_norm or ring_id or ""
        ptex = ptex_by_ring.get(ring_id_norm) if ring_id_norm else None
        gtex = gtex_by_poly.get(poly_id)
        if (ring_id_norm and ring_id_norm in ptex_by_ring) or ptex_by_poly.get(poly_id):
            gtex = None

        texfile = None
        if ptex and ptex.get("image"):
            texfile = ptex["image"]
        if not texfile:
            texfile = poly_to_image.get(poly_id)
        if texfile:
            texfile = _resolve_image_path(gml_path, texfile)

        # Material-Name basiert auf SurfaceTyp + poly_id
        # WICHTIG: Jedes Face braucht sein eigenes Material mit richtiger SurfaceTyp-Zuordnung
        surface_type = _map_surface_type(label)
        surf_attrs = surface_attrs_by_poly.get(poly_id) or {}
        attr_surface_type = str(
            surf_attrs.get("SurfaceTyp")
            or surf_attrs.get("surface_type")
            or surf_attrs.get("Typ")
            or ""
        ).strip()
        if attr_surface_type and surface_type in {
            "Building",
            "BuildingRoom",
            "Storey",
            "Bridge",
            "BridgePart",
            "Tunnel",
            "TunnelPart",
            "Unknown",
        }:
            surface_type = _map_surface_type(attr_surface_type)
        _fp_sid = str(
            filling_parent_surface_id_by_poly.get(poly_id, "")
            or surf_attrs.get("filling_parent_surface_id")
            or surf_attrs.get("opening_surface_id")
            or ""
        ).strip()
        parent_surface_type = str(
            surf_attrs.get("BoundarySurfaceType")
            or surf_attrs.get("opening_surface_type")
            or ""
        ).strip()
        
        # Neue Cutter-Openings und Interior-Ringe behalten einen sprechenden
        # Namen mit kurzer ID, statt in das generische SurfaceTyp_PolyID-Schema
        # zurueckzufallen.
        mat_name = _preferred_material_name(
            surface_type,
            effective_poly_id,
            ring_id_norm or ring_id or effective_poly_id,
            is_gml_interior_ring=is_gml_interior_ring,
        )
        
        # NOTE: avoid noisy debug prints in normal operation
        
        # OPTIMIZED: Template-basierte Material-Erstellung + cached Slot-Lookup
        mat = _get_or_create_material_fast(mat_name, texfile, color if not texfile else None)
        f.material_index = _ensure_material_slot_cached(obj, mat, _slot_cache)

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
            mat["gml_ring_id"] = ring_id_norm or ""
            mat["gml_polygon_id"] = effective_poly_id or ""
            if poly_id in duplicate_non_hole_poly_ids and not is_gml_interior_ring:
                mat["cgml3_duplicate_poly_id"] = True

            # Interior-Ring: Relation zum Exterior-Polygon speichern
            if is_gml_interior_ring:
                mat["ExteriorPolyId"] = poly_id
                mat["Interior"] = True

            if surf_id_norm:
                mat["con_surface_id"] = surf_id_norm

            # fillingSurface-Parent: Parent-Surface-ID speichern (z.B. WallSurface-ID für Door/Window)
            if _fp_sid:
                mat["filling_parent_surface_id"] = _fp_sid

            if ms_id_norm:
                mat["gml_multisurface_id"] = ms_id_norm
                key = (ms_id_norm, surf_id_norm) if surf_id_norm else (ms_id_norm, "")
                if multisurface_member_count.get(key, 0) > 1:
                    mat["is_multisurface_member"] = True

            cs_id = compositesurface_id_by_poly.get(poly_id, "")
            if cs_id:
                mat["gml_compositesurface_id"] = cs_id
                mat["is_compositesurface_member"] = True

            _apply_surface_semantics(
                mat,
                surface_type,
                surf_id_norm or "",
                _fp_sid,
                parent_surface_type,
            )
            target_id = (
                ptex.get("poly")
                if (ptex and ptex.get("poly"))
                else (poly_id if gtex or poly_to_image.get(poly_id) else "")
            )
            mat["app_target_or_uri"] = str(target_id)
        except Exception:
            pass

        # LoD als Custom Property speichern, falls vorhanden (wie CityGML2 Import)
        if lod_num is not None:
            try:
                mat["lod"] = int(lod_num)
            except Exception:
                mat["lod"] = str(lod_num)

        # NEU: Markierung, wenn Polygon auch in lod*Solid vorkommt
        if poly_also_in_solid.get(poly_id):
            try:
                mat["also_in_solid"] = True
            except Exception:
                pass
        
        # NEU: Markierung, wenn Polygon aus Solid/Shell-Struktur kommt (z.B. BuildingInstallation)
        if in_shell:
            try:
                mat["in_shell"] = True
            except Exception:
                pass

        if surf_attrs:
            # Lossless roundtrip storage (collect across faces/material uses).
            try:
                existing = mat.get("cgml3_surface_generic_attributes", None)
                if not isinstance(existing, dict):
                    existing = {}
                merged = dict(existing)
                for key, val in surf_attrs.items():
                    merged[str(key)] = val
                mat["cgml3_surface_generic_attributes"] = merged
                try:
                    mat["cgml3_surface_generic_attributes_json"] = json.dumps(
                        merged, ensure_ascii=False, sort_keys=True
                    )
                except Exception:
                    mat["cgml3_surface_generic_attributes_json"] = str(merged)
            except Exception:
                pass

            # Also mirror into direct material custom properties (legacy behavior)
            for key, val in surf_attrs.items():
                try:
                    mat[key] = val
                except Exception:
                    mat[key] = str(val)

        loops = list(f.loops)

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
                sign_src = None
                try:
                    raw_uvs = list(ptex_poly.get("uv") or []) if isinstance(ptex_poly, dict) else []
                    sign_src = _uv_sign(raw_uvs)
                except Exception:
                    sign_src = None
                if rid_for_start and sign_src in (-1, 1):
                    ring_uv_sign_by_ring_id[rid_for_start] = int(sign_src)
                    try:
                        d = dict(obj.data.get("cgml3_uv_sign_by_ring", {}) or {})
                        d[rid_for_start] = int(sign_src)
                        obj.data["cgml3_uv_sign_by_ring"] = d
                    except Exception:
                        pass
                    try:
                        if mat is not None and _norm_id(mat.get("gml_ring_id", "")) == rid_for_start:
                            mat["cgml3_uv_sign"] = int(sign_src)
                    except Exception:
                        pass
            except Exception:
                pass
            if is_exterior and len(uvs) >= 3:
                ordered = uvs if len(uvs) == len(loops) else _order_uvs_for_face(
                    f,
                    [(float(l.vert.co.x), float(l.vert.co.y), float(l.vert.co.z)) for l in loops],
                    uvs
                )
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
                _register_interior_face(is_exterior, loops, uv_layer, mat)
                continue

        if ptex and ptex.get("uv"):
            uvs = _adjust_uv_count_for_face(loops, list(ptex.get("uv") or []))
            ring_xyz = [(float(l.vert.co.x), float(l.vert.co.y), float(l.vert.co.z)) for l in loops]
            ordered = _order_uvs_for_face(f, ring_xyz, uvs)
            # NEW: remember original TexCoordList start UV per ring
            try:
                if ring_id_norm and uvs:
                    ring_uv_start_by_ring_id[ring_id_norm] = (float(uvs[0][0]), float(uvs[0][1]))
                    try:
                        d = dict(obj.data.get("cgml3_uv_start_by_ring", {}) or {})
                        d[ring_id_norm] = (float(uvs[0][0]), float(uvs[0][1]))
                        obj.data["cgml3_uv_start_by_ring"] = d
                    except Exception:
                        pass
                raw_sign = _uv_sign(list(ptex.get("uv") or [])) if isinstance(ptex, dict) else 0
                if ring_id_norm and raw_sign in (-1, 1):
                    ring_uv_sign_by_ring_id[ring_id_norm] = int(raw_sign)
                    try:
                        d = dict(obj.data.get("cgml3_uv_sign_by_ring", {}) or {})
                        d[ring_id_norm] = int(raw_sign)
                        obj.data["cgml3_uv_sign_by_ring"] = d
                    except Exception:
                        pass
                    try:
                        mat["cgml3_uv_sign"] = int(raw_sign)
                    except Exception:
                        pass
            except Exception:
                pass
            for l, (uu, vv) in zip(loops, ordered):
                l[uv_layer].uv = (uu, vv)
            _register_interior_face(is_exterior, loops, uv_layer, mat)
            continue

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
            _register_interior_face(is_exterior, loops, uv_layer, mat)
            continue

        if texfile and is_exterior and not (ring_id_norm and ring_id_norm in ptex_by_ring):
            print(f"[CityGML3] Missing TexCoordList for ring_id={ring_id or '(empty)'} of exterior; poly_id={poly_id}; label={label} | known_rings={len(ptex_by_ring)}")
        _register_interior_face(is_exterior, loops, uv_layer, mat)

    bm.to_mesh(mesh); bm.free()

   # Interior-Flags und Exterior-Bezug an die Materialien schreiben
   # ACHTUNG: Interior-Ring-Materialien haben ExteriorPolyId bereits im Hauptloop
   # korrekt gesetzt — hier nur setzen wenn noch nicht vorhanden.
    for mat in obj.data.materials:
        if not mat:
            continue
        name = mat.name
        if name in mats_with_interior:
            try:
                mat["Interior"] = True
                if not mat.get("ExteriorPolyId"):
                    ext_ring_id = exterior_ring_by_poly.get(name, "")
                    mat["ExteriorPolyId"] = name
                    mat["ExteriorRingId"] = ext_ring_id
            except Exception:
                mat["Interior"] = True

    # Legacy fallback: keep per-material uv_start too (may be overwritten when a material spans multiple rings)
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
