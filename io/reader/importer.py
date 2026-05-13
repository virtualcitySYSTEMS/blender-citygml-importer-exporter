# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
import bpy
from pathlib import Path
try:
    from lxml import etree as ET
except Exception:
    import xml.etree.ElementTree as ET

from mathutils import Matrix
import hashlib
import struct

from .namespaces import NS
from .geometry import iter_features, build_geometry, parse_points, parse_linestrings
from .appearance import parse_x3d_materials, parse_parameterized_textures, parse_georeferenced_textures
from .materials import apply_materials_uvs
from .xml_utils import localname, _norm_id
from .grid_coverage import parse_rectified_grid_coverage, create_mesh_from_grid
from .pointcloud import (
    parse_pointcloud_inline_points,
    parse_pointcloud_metadata,
    load_external_pointcloud,
    create_blender_pointcloud,
    resolve_external_file_path
)

# Shared modules for code deduplication
from ...shared.feature_parser import parse_all_feature_type_attributes
from ...shared.attribute_utils import parse_generic_attributes_common
from ...shared.materials_common import normalize_crs_to_epsg, extract_vertical_epsg
from ...common.filter_utils import resolve_scene_bbox_filter

import re
import os
import json
from uuid import uuid4

# Fingerprint -> kanonische Template-ID
TEMPLATE_ID_BY_FP: dict[str, str] = {}

# Geometry caching for deduplication - replaced by LRU cache in memory_manager
# Kept for backward compatibility, but use memory_manager.LRUGeometryCache instead
GEOMETRY_CACHE: dict[str, bpy.types.Mesh] = {}

# Optional import diagnostics:
# - Enabled via existing Scene property: context.scene.cgml3.export_debug
# - Optional watchlist via env var: CGML3_IMPORT_DIAG_IDS="id1,id2"
_IMPORT_DIAG = False
_IMPORT_DIAG_WATCH_IDS = {
    s.strip()
    for s in os.environ.get("CGML3_IMPORT_DIAG_IDS", "").split(",")
    if s.strip()
}

def _diag_is_watched_id(gml_id: str) -> bool:
    if not _IMPORT_DIAG or not gml_id:
        return False
    if not _IMPORT_DIAG_WATCH_IDS:
        return False
    return gml_id in _IMPORT_DIAG_WATCH_IDS

def _diag_count_generic_attribute_nodes(feat_el) -> int:
    try:
        # localname compare to be namespace-agnostic
        c = 0
        for el in feat_el.iter():
            tag = getattr(el, "tag", None)
            if not isinstance(tag, str):
                continue
            if localname(tag) == "genericAttribute":
                c += 1
        return c
    except Exception:
        return -1

def _safe_store_json_chunks(owner, base_key: str, data: dict, *, chunk_size: int = 16000) -> None:
    """
    Store JSON representation of `data` into IDProperties, splitting into chunks if needed.
    Blender IDProperties can reject very large strings; chunking keeps it visible/debuggable.
    """
    try:
        txt = json.dumps(data, ensure_ascii=False, sort_keys=True)
    except Exception:
        txt = str(data)

    # Clear old chunks if any
    try:
        for k in list(getattr(owner, "keys", lambda: [])()):
            if isinstance(k, str) and (k == base_key or k.startswith(base_key + "_")):
                try:
                    del owner[k]
                except Exception:
                    pass
    except Exception:
        pass

    if not isinstance(txt, str):
        txt = str(txt)

    if len(txt) <= chunk_size:
        owner[base_key] = txt
        return

    # Split into chunks
    for i in range(0, len(txt), chunk_size):
        owner[f"{base_key}_{i//chunk_size}"] = txt[i : i + chunk_size]

def _diag_should_log_feature(feat_id: str, ga_nodes: int) -> bool:
    """
    Dynamic logging policy:
    - If a watchlist is provided, only log watched IDs.
    - Otherwise, log features that have at least one genericAttribute node.
    """
    if not _IMPORT_DIAG:
        return False
    if _IMPORT_DIAG_WATCH_IDS:
        return _diag_is_watched_id(feat_id)
    return ga_nodes > 0


def _material_prop_text(mat, key: str) -> str:
    if mat is None:
        return ""
    try:
        value = mat.get(key, "")
    except Exception:
        try:
            value = mat.get(key)
        except Exception:
            value = ""
    return str(value or "").strip()


def _material_surface_type(mat) -> str:
    return _material_prop_text(mat, "SurfaceTyp") or _material_prop_text(mat, "surface_type")


def _material_polygon_id(mat) -> str:
    poly_id = _material_prop_text(mat, "gml_polygon_id")
    if poly_id:
        return poly_id
    name = str(getattr(mat, "name", "") or "")
    if "_" in name:
        return name.split("_", 1)[1].strip()
    return ""


def _material_semantic_score(mat) -> int:
    surface_type = _material_surface_type(mat)
    score = 0
    if surface_type:
        score += 1
    if surface_type in {
        "WallSurface",
        "RoofSurface",
        "GroundSurface",
        "CeilingSurface",
        "OuterCeilingSurface",
        "InteriorWallSurface",
        "FloorSurface",
        "OuterFloorSurface",
        "ClosureSurface",
        "WindowSurface",
        "DoorSurface",
    }:
        score += 6
    if surface_type in {"WindowSurface", "DoorSurface"}:
        score += 10
    if _material_prop_text(mat, "con_surface_id"):
        score += 2
    if _material_prop_text(mat, "filling_parent_surface_id"):
        score += 8
    try:
        if bool(mat.get("Interior", False)):
            score += 1
    except Exception:
        pass
    return score


def _should_prefer_semantic_source(candidate, current) -> bool:
    if candidate is None:
        return False
    if current is None:
        return True
    cand_score = _material_semantic_score(candidate)
    curr_score = _material_semantic_score(current)
    if cand_score != curr_score:
        return cand_score > curr_score
    cand_type = _material_surface_type(candidate)
    curr_type = _material_surface_type(current)
    if cand_type and not curr_type:
        return True
    return False


def _copy_material_custom_properties(source_mat, target_mat) -> bool:
    if source_mat is None or target_mat is None or source_mat == target_mat:
        return False

    copied = False
    for key in source_mat.keys():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        try:
            target_mat[key] = source_mat.get(key)
            copied = True
        except Exception:
            continue

    surface_type = _material_surface_type(source_mat)
    if surface_type:
        try:
            target_mat["SurfaceTyp"] = surface_type
            copied = True
        except Exception:
            pass
        try:
            target_mat["surface_type"] = surface_type
            copied = True
        except Exception:
            pass
    return copied


def _refresh_object_surface_types(obj) -> None:
    if obj is None:
        return
    surf_types = set()
    try:
        for slot in obj.material_slots:
            mat = getattr(slot, "material", None)
            surface_type = _material_surface_type(mat)
            if surface_type:
                surf_types.add(surface_type)
    except Exception:
        return

    if surf_types:
        try:
            obj["surface_types"] = ", ".join(sorted(surf_types))
        except Exception:
            pass


def _rename_outer_shell_material(target_mat, poly_id: str) -> None:
    if target_mat is None:
        return
    surface_type = _material_surface_type(target_mat)
    if not surface_type or not poly_id:
        return

    desired_name = f"{surface_type}_{poly_id}"
    if target_mat.name == desired_name:
        return
    if desired_name in bpy.data.materials:
        return
    try:
        target_mat.name = desired_name
    except Exception:
        pass


def _sync_outer_shell_surface_semantics(building_obj) -> int:
    """
    Hierarchical CityGML 3 imports can create an OuterShell mesh that mirrors Storey
    polygons but keeps only generic Building materials. Copy the richer per-polygon
    semantics from Storey meshes so export can reconstruct filling surfaces reliably.
    """
    if building_obj is None or building_obj.get("structure_type") != "hierarchical":
        return 0

    outer_shells = [
        child for child in building_obj.children
        if child.type == "MESH" and child.get("structure_part") == "outer_shell"
    ]
    if not outer_shells:
        return 0

    storey_meshes = [
        child for child in building_obj.children_recursive
        if child.type == "MESH" and child.get("structure_part") == "storey"
    ]
    if not storey_meshes:
        return 0

    source_by_poly = {}
    source_by_poly_ring = {}
    for storey_obj in storey_meshes:
        for slot in storey_obj.material_slots:
            mat = getattr(slot, "material", None)
            poly_id = _material_polygon_id(mat)
            if not poly_id:
                continue
            ring_id = _material_prop_text(mat, "gml_ring_id")
            if _should_prefer_semantic_source(mat, source_by_poly.get(poly_id)):
                source_by_poly[poly_id] = mat
            ring_key = (poly_id, ring_id)
            if _should_prefer_semantic_source(mat, source_by_poly_ring.get(ring_key)):
                source_by_poly_ring[ring_key] = mat

    if not source_by_poly:
        return 0

    changed = 0
    for outer_obj in outer_shells:
        touched = False
        for slot in outer_obj.material_slots:
            target_mat = getattr(slot, "material", None)
            poly_id = _material_polygon_id(target_mat)
            if not poly_id:
                continue

            ring_id = _material_prop_text(target_mat, "gml_ring_id")
            source_mat = source_by_poly_ring.get((poly_id, ring_id)) if ring_id else None
            if source_mat is None:
                source_mat = source_by_poly.get(poly_id)
            if source_mat is None:
                continue

            before_type = _material_surface_type(target_mat)
            before_parent = _material_prop_text(target_mat, "filling_parent_surface_id")
            copied = _copy_material_custom_properties(source_mat, target_mat)
            if not copied:
                continue

            _rename_outer_shell_material(target_mat, poly_id)
            after_type = _material_surface_type(target_mat)
            after_parent = _material_prop_text(target_mat, "filling_parent_surface_id")
            if after_type != before_type or after_parent != before_parent:
                changed += 1
                touched = True

        if touched:
            _refresh_object_surface_types(outer_obj)

    return changed

def _compute_geometry_hash(verts, faces):
    """Compute a hash for geometry deduplication based on vertices and faces.
    
    Uses binary packing for faster hashing compared to string formatting.
    """
    try:
        # Pack vertices as binary floats (3 floats per vertex)
        vert_bytes = b''.join(struct.pack('fff', *v) for v in verts)
        # Pack face indices as binary integers
        face_bytes = b''.join(struct.pack(f'{len(f)}I', *f) for f in faces)
        combined = vert_bytes + face_bytes
        return hashlib.sha256(combined).hexdigest()[:16]
    except (struct.error, TypeError, ValueError):
        return None

# Import context data (für externe Dateipfade, etc.)
_IMPORT_CONTEXT: dict[str, object] = {}

# Use shared CRS normalization functions (supports ADV-URNs and standard formats)
_normalize_epsg = normalize_crs_to_epsg
_extract_vertical_epsg = extract_vertical_epsg

def _extract_main_epsg(srs_name: str) -> str:
    # rückwärtskompatibel: auf neue Logik umleiten
    return _normalize_epsg(srs_name)

def _crs_matches(a: str, b: str) -> bool:
    ea = _normalize_epsg(a or "")
    eb = _normalize_epsg(b or "")
    return ea != "Unknown CRS" and ea == eb

def _determine_file_origin_and_crs(root):
    srs, lc = _get_envelope_srs_and_corners(root)
    origin = lc if lc is not None else _scan_min_coords(root)
    crs = _normalize_epsg(srs) if srs else "Unknown CRS"

    # Fallback: if no CRS was found on the CityModel/Envelope, try to infer it from
    # geometry elements that carry their own @srsName (e.g. gml:Solid, gml:MultiSurface, gml:Point).
    if crs == "Unknown CRS":
        try:
            geom_tags = (
                "Solid",
                "MultiSurface",
                "CompositeSurface",
                "Polygon",
                "MultiPolygon",
                "Point",
                "MultiPoint",
                "Curve",
                "LineString",
                "MultiCurve",
                "Surface",
            )
            for el in root.iter():
                tag = getattr(el, "tag", None)
                if not isinstance(tag, str):
                    continue
                ln = localname(tag)
                if ln not in geom_tags:
                    continue
                srs_name = el.get("srsName") or ""
                cand = _normalize_epsg(srs_name) if srs_name else "Unknown CRS"
                if cand != "Unknown CRS":
                    crs = cand
                    break
        except Exception:
            pass

    return origin, crs

def _infer_feature_crs(feat_el) -> str:
    """
    Infer per-feature CRS from geometry elements carrying @srsName.
    Returns normalized string like 'EPSG:3878' or 'Unknown CRS'.
    """
    try:
        geom_tags = (
            "Solid",
            "MultiSurface",
            "CompositeSurface",
            "Polygon",
            "MultiPolygon",
            "Point",
            "MultiPoint",
            "Curve",
            "LineString",
            "MultiCurve",
            "Surface",
        )
        for el in feat_el.iter():
            tag = getattr(el, "tag", None)
            if not isinstance(tag, str):
                continue
            ln = localname(tag)
            if ln not in geom_tags:
                continue
            srs_name = el.get("srsName") or ""
            cand = _normalize_epsg(srs_name) if srs_name else "Unknown CRS"
            if cand != "Unknown CRS":
                return cand
    except Exception:
        pass
    return "Unknown CRS"

def ensure_collection(context, name):
    coll = bpy.data.collections.get(name)
    if not coll:
        coll = bpy.data.collections.new(name)
        context.scene.collection.children.link(coll)
    return coll


_INLINE_SUBFEATURE_COLLECTION_NAMES = {
    "BuildingInstallation", "BuildingInstallations",
    "IntBuildingInstallation", "IntBuildingInstallations",
    "BuildingFurniture", "BuildingFurnitures",
    "BridgeInstallation", "BridgeInstallations",
    "IntBridgeInstallation", "IntBridgeInstallations",
    "BridgeConstructionElement", "BridgeConstructionElements",
    "BridgeFurniture", "BridgeFurnitures",
    "TunnelInstallation", "TunnelInstallations",
    "IntTunnelInstallation", "IntTunnelInstallations",
    "TunnelFurniture", "TunnelFurnitures",
}

_INLINE_PART_COLLECTION_NAMES = {
    "BuildingPart", "BuildingParts",
    "BridgePart", "BridgeParts",
    "TunnelPart", "TunnelParts",
}


def _collection_base_name(name: str) -> str:
    text = str(name or "").strip()
    if "." in text:
        base, suffix = text.rsplit(".", 1)
        if suffix.isdigit():
            return base
    return text


def _find_layer_collection(layer_collection, collection):
    if layer_collection is None or collection is None:
        return None
    try:
        if layer_collection.collection == collection:
            return layer_collection
    except Exception:
        pass
    try:
        children = list(layer_collection.children)
    except Exception:
        children = []
    for child in children:
        found = _find_layer_collection(child, collection)
        if found is not None:
            return found
    return None


def _ensure_inline_part_collection_visible(collection):
    if collection is None:
        return

    child_name = _collection_base_name(getattr(collection, "name", ""))
    if child_name not in _INLINE_PART_COLLECTION_NAMES:
        return

    try:
        collection.hide_viewport = False
    except Exception:
        pass
    try:
        collection.hide_render = False
    except Exception:
        pass
    try:
        if "cgml3_auto_disabled_subfeatures" in collection:
            del collection["cgml3_auto_disabled_subfeatures"]
    except Exception:
        pass

    try:
        layer_collection = _find_layer_collection(bpy.context.view_layer.layer_collection, collection)
        if layer_collection is not None:
            layer_collection.exclude = False
    except Exception:
        pass


def _disable_inline_subfeature_collections(root_collection):
    """Mark imported inline subfeature collections to avoid double export."""
    if root_collection is None:
        return
    try:
        children = list(root_collection.children)
    except Exception:
        children = []

    for child in children:
        child_name = _collection_base_name(getattr(child, "name", ""))
        if child_name not in _INLINE_SUBFEATURE_COLLECTION_NAMES:
            continue

        try:
            child["cgml3_auto_disabled_subfeatures"] = True
        except Exception:
            pass

def _set_world_prop_with_ui(world, key: str, value: float):
    world[key] = float(value)
    try: world.id_properties_ui(key).update(min=-1e12, max=1e12, default=0.0, precision=8, description=key)
    except Exception: pass

def _get_valid_scene_georeference(sc):
    if sc is None:
        return None

    try:
        if not all(key in sc for key in ("SRID", "crs x", "crs y")):
            return None
        srid = str(sc.get("SRID", "")).strip()
        if not srid:
            return None
        normalized_srid = _normalize_epsg(srid)
        if normalized_srid == "Unknown CRS" or srid.startswith("Unknown"):
            return None
        x_origin = float(sc["crs x"])
        y_origin = float(sc["crs y"])
    except Exception:
        return None

    if abs(x_origin) <= 1e-9 and abs(y_origin) <= 1e-9:
        return None

    return srid, x_origin, y_origin

def _copy_scene_georeference_to_world(context, world) -> bool:
    """
    Copy DB-import georeference properties from Scene to World before importing.

    This lets the existing World-based placement logic reuse Scene["SRID"],
    Scene["crs x"] and Scene["crs y"] when they are present.
    """
    if world is None:
        return False

    candidates = []
    try:
        candidates.append(context.scene if not isinstance(context, dict) else bpy.context.scene)
    except Exception:
        pass
    try:
        candidates.append(bpy.data.scenes.get("Scene"))
    except Exception:
        pass

    seen = set()
    for sc in candidates:
        if sc is None:
            continue
        ident = id(sc)
        if ident in seen:
            continue
        seen.add(ident)

        scene_georef = _get_valid_scene_georeference(sc)
        if scene_georef is None:
            continue
        srid, x_origin, y_origin = scene_georef

        world["CRS"] = srid
        _set_world_prop_with_ui(world, "X-Origin", x_origin)
        _set_world_prop_with_ui(world, "Y-Origin", y_origin)

        print(f"[CityGML Import] Scene georeference copied to World: CRS={srid}, X={x_origin}, Y={y_origin}")
        return True

    return False

def _get_envelope_srs_and_corners(root):
    env = root.find(".//gml:Envelope", NS)
    if env is None: return "", None
    srs = env.get("srsName", "") or ""
    lc = (env.findtext("./gml:lowerCorner", default="", namespaces=NS) or "").strip()
    def _parse_corner(txt):
        vals = [float(x) for x in " ".join(txt.split()).split()]
        if len(vals) >= 3: return (vals[0], vals[1], vals[2])
        if len(vals) == 2: return (vals[0], vals[1], 0.0)
        return None
    return srs, _parse_corner(lc)

def _scan_min_coords(root):
    mins = [float("inf")]*3; found=False
    for el in root.findall(".//gml:posList", NS) + root.findall(".//gml:pos", NS):
        txt = (el.text or "").strip()
        if not txt: continue
        vals = [float(x) for x in " ".join(txt.split()).split()]
        dim_attr = el.get("srsDimension")
        dim = int(dim_attr) if dim_attr and dim_attr.isdigit() else (3 if len(vals)%3==0 else 2)
        for i in range(0, len(vals), dim):
            x, y = vals[i], vals[i+1]; z = vals[i+2] if dim==3 else 0.0
            mins[0] = min(mins[0], x); mins[1] = min(mins[1], y); mins[2] = min(mins[2], z); found=True
    return (mins[0], mins[1], mins[2]) if found else (0.0,0.0,0.0)

def _collect_dynamizer_owners(root):
    """
    Liefert Mapping: Dynamizer-gml:id -> gml:id des umgebenden CityObjects.
    Geht davon aus, dass der nächstgelegene Vorfahr im CityGML-Modell das
    'besitzende' CityObject ist.
    """
    owners = {}

    allowed_ns = (
        NS["core"], NS["bldg"], NS["brid"], NS["tun"], NS["tran"],
        NS["veg"], NS["wtr"], NS["con"], NS["frn"], NS["grp"],
        NS["luse"], NS["dem"], NS["pcl"], NS["tex"], NS["vers"], NS["gen"]
        # bewusst ohne NS["dyn"], der Dynamizer selbst soll kein „Owner“ werden
    )

    def recurse(elem, current_owner_id=None):
        if isinstance(elem.tag, str) and elem.tag.startswith("{"):
            ns_uri = elem.tag.split("}")[0].strip("{")
            ln = elem.tag.split("}")[-1]

            # CityObject-Owner aktualisieren (aber keinen Dynamizer selbst)
            if ns_uri in allowed_ns and ln not in ("CityModel", "Appearance"):
                owner_id = elem.get(f"{{{NS['gml']}}}id") or current_owner_id
            else:
                owner_id = current_owner_id

            # Dynamizer → aktuellen Owner mappen
            if ns_uri == NS.get("dyn") and ln == "Dynamizer":
                dyn_id = elem.get(f"{{{NS['gml']}}}id")
                if dyn_id and owner_id:
                    owners[dyn_id] = owner_id
        else:
            owner_id = current_owner_id

        for child in list(elem):
            recurse(child, owner_id)

    recurse(root, None)
    return owners

# generische Attribute extrahieren
def _parse_generic_attributes(feat_el):
    """Parse generic attributes using shared module."""
    from .namespaces import NS
    return parse_generic_attributes_common(feat_el, NS) or {}

def _parse_gml_id_filter(filter_str: str) -> set[str]:
    """Parse GML ID filter string in format 'ID1,ID2,ID3-ID4,...'"""
    if not filter_str:
        return set()
    
    ids = set()
    for part in filter_str.split(','):
        if '-' in part:  # Range of IDs
            start, end = part.split('-', 1)
            # For now, just add start and end IDs since we don't have a way to generate IDs in between
            ids.add(start.strip())
            ids.add(end.strip())
        else:  # Single ID
            ids.add(part.strip())
    return ids

def _implicit_instance_info(feat_el, ref_origin):
    """
    Liefert (template_id, M_eff) für Features, deren Geometrie ausschließlich
    über eine einzige ImplicitGeometry (lodXImplicitRepresentation) definiert ist.

    Gilt für alle Feature-Typen (Building, Bridge, CityFurniture, Vegetation, Generic, ...).

    template_id : gml:id der Template-Geometrie (z.B. aus relativeGeometry@xlink:href)
    M_eff       : 4x4-Matrix (mathutils.Matrix), die die gleiche Transformation
                  repräsentiert, die build_geometry auf die Vertices anwendet
                  (transformationMatrix + referencePoint + Datei-Offset).

    Unterstützt sowohl:
    - <ImplicitGeometry> als auch <core:ImplicitGeometry>
    - <transformationMatrix> / <core:transformationMatrix>
    - <referencePoint> / <core:referencePoint>
    - <relativeGeometry> / <core:relativeGeometry>

    Nur dann wird instanziert:
    - es gibt mindestens ein <ImplicitGeometry>…</ImplicitGeometry>,
    - es gibt GENAU EINE ImplicitGeometry in diesem Feature,
    - die Geometrie kommt über lodXImplicitRepresentation (kein paralleles lodXGeometry).
    """

    # 1) prüfen, ob überhaupt eine ImplicitRepresentation vorhanden ist
    has_lod_impl = False
    has_lod_geom = False
    for el in feat_el.iter():
        tag = getattr(el, "tag", None)
        if not isinstance(tag, str):
            continue
        ln = localname(tag)
        # NEU: auch LoD0 berücksichtigen
        if ln in (
            "lod0ImplicitRepresentation",
            "lod1ImplicitRepresentation",
            "lod2ImplicitRepresentation",
            "lod3ImplicitRepresentation",
            "lod4ImplicitRepresentation",
        ):
            has_lod_impl = True
        if ln in (
            "lod0Geometry",
            "lod1Geometry",
            "lod2Geometry",
            "lod3Geometry",
            "lod4Geometry",
        ):
            has_lod_geom = True

    # Nur reine ImplicitGeometry-Fälle instanzieren (keine parallele lodXGeometry)
    if not has_lod_impl or has_lod_geom:
        return None, None, None

    # 2) die „aktive“ ImplicitGeometry wählen:
    #    wir nehmen – analog zur üblichen LoD-Strategie – die höchste vorhandene LoD.
    impl = None
    core_ns = NS.get("core")

    def _find_impl_for_lod(lod_tag: str):
        # erst versuchen: core-Namespace explizit
        if core_ns:
            path = f".//{{{core_ns}}}{lod_tag}//{{{core_ns}}}ImplicitGeometry"
            el = feat_el.find(path, NS)
            if el is not None:
                return el
        # generischer Fallback (falls kein core:-Präfix oder anderes Präfix)
        for el in feat_el.iter():
            tag = getattr(el, "tag", None)
            if not isinstance(tag, str):
                continue
            if localname(tag) == lod_tag:
                for ig in el.iter():
                    t2 = getattr(ig, "tag", None)
                    if not isinstance(t2, str):
                        continue
                    if localname(t2) == "ImplicitGeometry":
                        return ig
        return None

    # LoD-Priorität: 4 → 3 → 2 → 1 → 0
    found_lod_level = None
    for lod_tag in (
        "lod4ImplicitRepresentation",
        "lod3ImplicitRepresentation",
        "lod2ImplicitRepresentation",
        "lod1ImplicitRepresentation",
        "lod0ImplicitRepresentation",
    ):
        impl = _find_impl_for_lod(lod_tag)
        if impl is not None:
            # Extract LoD level from tag name (e.g., "lod2ImplicitRepresentation" → 2)
            found_lod_level = int(lod_tag[3])  # Extract digit after "lod"
            break

    # Fallback: erste ImplicitGeometry irgendwo im Feature, falls obiges nichts findet
    if impl is None:
        for el in feat_el.iter():
            tag = getattr(el, "tag", None)
            if not isinstance(tag, str):
                continue
            if localname(tag) == "ImplicitGeometry":
                impl = el
                break

    if impl is None:
        # sollte praktisch nicht vorkommen, aber zur Sicherheit:
        return None, None, None

    # 3) Template-ID bestimmen (relativeGeometry, ggf. mit xlink:href) + Fingerprint
    xlink_href = "{http://www.w3.org/1999/xlink}href"
    template_id = ""
    fp = None  # Geometrie-Fingerprint (über pos/posList)

    rel = None
    for el in impl.iter():
        tag = getattr(el, "tag", None)
        if not isinstance(tag, str):
            continue
        # CityGML 3.0: relativeGeometry, CityGML 2.0: relativeGMLGeometry
        ln = localname(tag)
        if ln == "relativeGeometry" or ln == "relativeGMLGeometry":
            rel = el
            break

    if rel is not None:
        href = rel.get(xlink_href)
        geom_root = None

        if href:
            # referenziertes Template (xlink:href="#...") → Geometrie über GML_ID_LOOKUP suchen
            gid = _norm_id(href)
            try:
                from . import geometry as _geom_mod
                geom_root = _geom_mod.GML_ID_LOOKUP.get(gid)
            except Exception:
                geom_root = None
            # bevorzugte Template-ID: normalisierte href-ID
            template_id = gid
        else:
            # Template inline: die Geometrie steckt direkt unterhalb von <relativeGeometry>
            geom_root = rel

        # 3a) Versuch: gml:id aus der Geometrie lesen, falls vorhanden
        if not template_id and geom_root is not None:
            gml_ns = NS.get("gml")
            if gml_ns:
                gml_id_attr = f"{{{gml_ns}}}id"
                for el2 in geom_root.iter():
                    gid = el2.get(gml_id_attr)
                    if gid:
                        template_id = gid
                        break

        # 3b) Fingerprint aus allen pos/posList-Texten bilden
        if geom_root is not None:
            parts = []
            for el2 in geom_root.iter():
                tag2 = getattr(el2, "tag", None)
                if not isinstance(tag2, str):
                    continue
                ln2 = localname(tag2)
                if ln2 in ("posList", "pos"):
                    txt_vals = " ".join((el2.text or "").split())
                    if txt_vals:
                        parts.append(txt_vals)
            if parts:
                fp = hashlib.sha1(" | ".join(parts).encode("utf-8")).hexdigest()
                # falls noch keine Template-ID existiert, Fingerprint als Fallback-ID verwenden
                if not template_id:
                    template_id = f"geomfp:{fp}"

    if not template_id:
        return None, None, None

    # 3c) Fingerprint global kanonisieren:
    #     Wenn zwei verschiedene gml:ids dieselbe Geometrie (denselben FP) haben,
    #     teilen sie sich danach dieselbe Template-ID.
    if fp:
        global TEMPLATE_ID_BY_FP
        canon = TEMPLATE_ID_BY_FP.get(fp)
        if canon is None:
            TEMPLATE_ID_BY_FP[fp] = template_id
        else:
            template_id = canon

    # 4) transformationMatrix + referencePoint wie in geometry._implicit_transform_for auswerten

    # transformationMatrix: CityGML-URI, core:transformationMatrix ODER unpräfixter Tag
    tm_el = impl.find(".//{http://www.opengis.net/citygml/3.0}transformationMatrix")
    if tm_el is None:
        tm_el = impl.find(".//core:transformationMatrix", NS)
    if tm_el is None:
        for el in impl.iter():
            tag = getattr(el, "tag", None)
            if not isinstance(tag, str):
                continue
            if localname(tag) == "transformationMatrix":
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
            M = [
                list(vals[0:4]),
                list(vals[4:8]),
                list(vals[8:12]),
                list(vals[12:16]),
            ]

    # referencePoint suchen (localname == "referencePoint" → mit/ohne Namespace)
    ref_x = ref_y = ref_z = 0.0
    ref_el = None
    for rp in impl.iter():
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

    if M is None:
        # nur Translation durch referencePoint
        M = [
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
        if len(M) < 4:
            M.append([0.0, 0.0, 0.0, 1.0])
        elif len(M[3]) < 4:
            M[3] = [
                M[3][0],
                M[3][1] if len(M[3]) > 1 else 0.0,
                M[3][2] if len(M[3]) > 2 else 0.0,
                1.0,
            ]

    # 5) Datei-Offset (ref_origin) wie in build_geometry berücksichtigen
    rx, ry, rz = ref_origin
    R = [
        [1.0, 0.0, 0.0, -rx],
        [0.0, 1.0, 0.0, -ry],
        [0.0, 0.0, 1.0, -rz],
        [0.0, 0.0, 0.0, 1.0],
    ]

    M_eff = Matrix(R) @ Matrix(M)

    return template_id, M_eff, found_lod_level

def _get_world_origin_and_crs():
    """
    Reads world origin + CRS from bpy.data.worlds["World"].
    Returns ((x,y,z), crs_str) where values may be None if not available.
    """
    try:
        w = bpy.data.worlds.get("World")
        if not w:
            return (None, None, None), None
        ox = float(w.get("X-Origin")) if "X-Origin" in w else None
        oy = float(w.get("Y-Origin")) if "Y-Origin" in w else None
        oz = float(w.get("Z-Origin")) if "Z-Origin" in w else None
        crs = str(w.get("CRS")).strip() if "CRS" in w else None
        return (ox, oy, oz), (crs or None)
    except Exception:
        return (None, None, None), None

def _implicit_world_matrix_from_citygml(M_eff: Matrix, ref_origin, feature_crs: str | None):
    """
    Converts the effective implicit transform matrix (as used by build_geometry) into a Blender world matrix.

    CityGML implicit referencePoint is defined in the dataset CRS and must be related to the scene origin
    via World X/Y/Z-Origin (and potentially CRS transformation).

    Current implementation supports:
    - Same-CRS case: keep rotation/scale from M_eff and shift translation by +ref_origin so objects end up
      in local Blender coordinates around (0,0,0).
    - Mismatching CRS: fall back to same-CRS behavior (no reprojection) to avoid silently wrong transforms.
      The object stores its original CRS in obj["CRS"] elsewhere during import.
    """
    try:
        (ox, oy, oz), world_crs = _get_world_origin_and_crs()
        # Default: treat ref_origin as the dataset origin in the same CRS.
        same_crs = True
        if feature_crs and world_crs and feature_crs != "Unknown CRS" and world_crs != "Unknown CRS":
            same_crs = (str(feature_crs).strip() == str(world_crs).strip())

        # M_eff already includes the file offset subtraction (R @ M, where R translates by -ref_origin),
        # matching build_geometry's baked behavior. For object-level instancing we must *not* add ref_origin
        # back, otherwise implicit instances end up shifted by +ref_origin.
        Mw = M_eff.copy()

        # If a world origin exists, the import pipeline already uses it to derive ref_origin.
        # Keep Mw as local Blender-space matrix (do not add ox/oy/oz again).
        return Mw
    except Exception:
        return M_eff

def _store_common_attributes(obj, feat_el):
    """
    Stores common CityGML attributes (name, lifecycle, etc.) in Blender object custom properties.
    
    Args:
        obj: Blender object to store attributes in
        feat_el: XML element of the CityGML feature
    """
    # Store name if exists
    try:
        name = feat_el.findtext(".//gml:name", namespaces=NS)
        if name and name.strip():
            obj.name = name.strip()
    except Exception:
        pass

    # Lebensdauer-Felder aus core:AbstractFeatureWithLifespan
    try:
        for tag in ("creationDate", "terminationDate", "validFrom", "validTo"):
            dtxt = feat_el.findtext(f".//core:{tag}", namespaces=NS)
            if dtxt and dtxt.strip():
                obj[f"core:{tag}"] = dtxt.strip()
    except Exception:
        pass

      # Generic attributes + Feature-type-specific attributes
    try:
        feat_id_dbg = None
        try:
            feat_id_dbg = feat_el.get(f"{{{NS['gml']}}}id")
        except Exception:
            feat_id_dbg = None

        attrs = _parse_generic_attributes(feat_el)
        # Add feature-type-specific attributes (Bridge, Tunnel, WaterBody, Vegetation, etc.)
        attrs.update(parse_all_feature_type_attributes(feat_el, NS))

        # Store raw generic attributes as a dict for lossless roundtrip (incl. keys like "_lod").
        # This avoids Blender IDProperty edge-cases for certain key names and preserves types as strings/ints/floats.
        gen_only = {}
        try:
            gen_only = _parse_generic_attributes(feat_el) or {}
        except Exception as e:
            if _IMPORT_DIAG:
                print(f"[CityGML3][DIAG] genericAttribute parse failed id={feat_id_dbg}: {e}")
            gen_only = {}

        if isinstance(gen_only, dict) and gen_only:
            # Store as dict for exporter (lossless) + JSON string for visibility/debugging.
            try:
                obj["cgml3_generic_attributes"] = dict(gen_only)
            except Exception as e:
                if _IMPORT_DIAG:
                    print(f"[CityGML3][DIAG] failed to set obj['cgml3_generic_attributes'] id={feat_id_dbg}: {e}")

            try:
                _safe_store_json_chunks(obj, "cgml3_generic_attributes_json", gen_only)
            except Exception as e:
                if _IMPORT_DIAG:
                    print(f"[CityGML3][DIAG] failed to set obj JSON dump id={feat_id_dbg}: {e}")

        try:
            fid = str(feat_id_dbg or "")
            ga_nodes = _diag_count_generic_attribute_nodes(feat_el)
            if _diag_should_log_feature(fid, ga_nodes):
                keys_preview = list(sorted((gen_only or {}).keys()))[:40] if isinstance(gen_only, dict) else []
                stored_count = None
                try:
                    stored = obj.get("cgml3_generic_attributes", None)
                    stored_count = len(stored) if isinstance(stored, dict) else 0
                except Exception:
                    stored_count = None
                print(
                    "[CityGML3][DIAG] feature=%s genericAttribute_nodes=%s parsed=%d stored=%s keys=%s"
                    % (fid, ga_nodes, len(gen_only) if isinstance(gen_only, dict) else 0, stored_count, keys_preview)
                )
        except Exception as e:
            if _IMPORT_DIAG:
                print(f"[CityGML3][DIAG] feature={feat_id_dbg} logging failed: {e}")

        for k, v in attrs.items():
            try:
                obj[k] = v
            except (TypeError, ValueError):
                # Fallback: convert to string if type incompatible
                obj[k] = str(v)

        roof_type_el = feat_el.find(".//bldg:roofType", namespaces=NS)
        if roof_type_el is not None:
            obj["bldg:roofType"] = (roof_type_el.text or "").strip()

        height_el = feat_el.find(".//con:height/con:Height", namespaces=NS)
        if height_el is not None:
            obj["con:height"] = ""

            high_ref_el = height_el.find("./con:highReference", namespaces=NS)
            if high_ref_el is not None:
                obj["con:height:highReference"] = (high_ref_el.text or "").strip()

            low_ref_el = height_el.find("./con:lowReference", namespaces=NS)
            if low_ref_el is not None:
                obj["con:height:lowReference"] = (low_ref_el.text or "").strip()

            status_el = height_el.find("./con:status", namespaces=NS)
            if status_el is not None:
                obj["con:height:status"] = (status_el.text or "").strip()

            value_el = height_el.find("./con:value", namespaces=NS)
            if value_el is not None:
                value_text = (value_el.text or "").strip()
                if value_text:
                    obj["con:height:value"] = float(value_text)
                else:
                    obj["con:height:value"] = ""
                uom = value_el.get("uom")
                if uom is not None:
                    obj["con:height:uom"] = uom
    except (AttributeError, KeyError):
        pass

def _feature_has_implicit_geometry(feat_el) -> bool:
    """
    Prüft, ob irgendwo im Feature eine (core:)ImplicitGeometry vorkommt,
    unabhängig davon, ob sie exklusiv über lodXImplicitRepresentation
    verwendet wird oder parallel zu lodXGeometry existiert.
    """
    for el in feat_el.iter():
        tag = getattr(el, "tag", None)
        if not isinstance(tag, str):
            continue
        if localname(tag) == "ImplicitGeometry":
            return True
    return False


def import_citygml3_into_blender(path: str, context, *, import_appearance: bool = True):
    import time
    import sys
    start_time = time.time()
    
    def print_console_progress(phase: str, current: int = 0, total: int = 0, percentage: float = 0):
        """Print progress bar to PowerShell console."""
        bar_length = 40
        if total > 0:
            filled = int(bar_length * current / total)
            percentage = 100 * current / total
        else:
            filled = int(bar_length * percentage / 100)
        
        bar = '█' * filled + '░' * (bar_length - filled)
        if total > 0:
            info = f"{current}/{total}"
        else:
            info = ""
        
        # Pad with spaces to clear previous longer messages (80 chars total line length)
        line = f'\r[{bar}] {percentage:5.1f}% | {phase} {info}'
        sys.stdout.write(line + ' ' * max(0, 100 - len(line)))
        sys.stdout.flush()
    
    global _IMPORT_DIAG
    try:
        _IMPORT_DIAG = bool(getattr(getattr(context, "scene", None), "cgml3", None) and context.scene.cgml3.export_debug)
    except Exception:
        _IMPORT_DIAG = False
    # Clear geometry cache from previous imports
    global GEOMETRY_CACHE, TEMPLATE_ID_BY_FP
    GEOMETRY_CACHE.clear()
    TEMPLATE_ID_BY_FP.clear()
    
    # Initialize progress tracking
    wm = context.window_manager if hasattr(context, 'window_manager') else None
    if wm:
        wm.progress_begin(0, 100)
        wm.progress_update(5)  # 5%: Starting import
    print_console_progress("Import wird gestartet...", percentage=5)
    
    _IMPORT_CONTEXT["citygml_filepath"] = str(path)
    _IMPORT_CONTEXT["import_appearance"] = bool(import_appearance)
    try:
        if not isinstance(context, dict):
            _IMPORT_CONTEXT["mesh_validate"] = bool(context.scene.cgml3.mesh_validate)
    except Exception:
        _IMPORT_CONTEXT["mesh_validate"] = True

    # Check if streaming import should be used (threshold configurable in UI)
    from .streaming_import import should_use_streaming, is_streaming_available
    try:
        threshold_mb = float(getattr(getattr(context, "scene", None), "cgml3", None).import_streaming_threshold_mb)
    except Exception:
        threshold_mb = 30.0
    use_streaming = should_use_streaming(path, threshold_mb=threshold_mb)
    if _IMPORT_DIAG:
        try:
            print(f"[CityGML3][DIAG] use_streaming={use_streaming} streaming_available={is_streaming_available()}")
        except Exception:
            pass
    
    if use_streaming:
        # Streaming import for large files
        return _import_citygml3_streaming(path, context, wm, import_appearance=import_appearance)
    
    # Standard import for smaller files
    # Optimized XML parsing for large files
    try:
        # lxml: use huge_tree for files >100MB
        parser = ET.XMLParser(huge_tree=True, remove_blank_text=False)
        tree = ET.parse(path, parser=parser)
    except TypeError:
        # Fallback for xml.etree.ElementTree (no huge_tree parameter)
        tree = ET.parse(path)
    root = tree.getroot()
    
    if wm:
        wm.progress_update(15)  # 15%: XML parsed
    print_console_progress("XML-Datei eingelesen", percentage=15)

    # Store CityGML filepath in module context for PointCloud external file resolution
    _IMPORT_CONTEXT["citygml_filepath"] = str(path)
    try:
        if not isinstance(context, dict):
            _IMPORT_CONTEXT["mesh_validate"] = bool(context.scene.cgml3.mesh_validate)
    except Exception:
        _IMPORT_CONTEXT["mesh_validate"] = True

    # GML-ID-Lookup für relativeGeometry/xlink:href (ImplicitGeometry-Templates)
    # PERFORMANCE: Nur wenn ImplicitGeometry tatsächlich existiert
    try:
        from . import geometry as _geom_mod
        id_lookup = {}
        
        # Schnell-Check: Gibt es überhaupt ImplicitGeometry?
        has_implicit = False
        for elem in root.iter():
            if elem.tag and isinstance(elem.tag, str) and elem.tag.endswith('ImplicitGeometry'):
                has_implicit = True
                break
        
        if has_implicit:
            gml_ns = NS.get("gml")
            if gml_ns:
                gml_id_attr = f"{{{gml_ns}}}id"
                for el in root.iter():
                    gid = el.get(gml_id_attr)
                    if gid:
                        id_lookup[gid] = el
        _geom_mod.GML_ID_LOOKUP = id_lookup
    except Exception:
        _geom_mod = None

    # Dynamizer-Owner-Mapping (vor dem Feature-Sammeln)
    # OPTIMIZED: Schnell-Check ob Dynamizer überhaupt existieren, bevor der
    # gesamte Baum rekursiv traversiert wird.
    has_dynamizer = False
    for _dyn_check_el in root.iter():
        _tag = getattr(_dyn_check_el, 'tag', None)
        if isinstance(_tag, str) and _tag.endswith('Dynamizer'):
            has_dynamizer = True
            break
    dyn_owner_by_id = _collect_dynamizer_owners(root) if has_dynamizer else {}

    # Get filter settings from context
    gml_id_filter = None
    if context.scene.cgml3.use_gmlid_filter:
        filter_str = context.scene.cgml3.gmlid_filter.strip()
        if filter_str.startswith('[') and filter_str.endswith(']'):
            filter_str = filter_str[1:-1]  # Remove square brackets
        gml_id_filter = _parse_gml_id_filter(filter_str)
    
    bbox_coords = resolve_scene_bbox_filter(context.scene.cgml3)
    
    # LOD filter
    lod_filter = None
    if context.scene.cgml3.use_lod_filter:
        lod_filter = set()
        if context.scene.cgml3.lod_0:
            lod_filter.add(0)
        if context.scene.cgml3.lod_1:
            lod_filter.add(1)
        if context.scene.cgml3.lod_2:
            lod_filter.add(2)
        if context.scene.cgml3.lod_3:
            lod_filter.add(3)
        if context.scene.cgml3.lod_4:
            lod_filter.add(4)
    
    # Feature type filter - map UI properties to XML tag names
    feature_type_filter = None
    if context.scene.cgml3.use_feature_type_filter:
        feature_type_filter = set()
        if context.scene.cgml3.import_buildings:
            feature_type_filter.update(["Building", "BuildingPart", "Storey", "BuildingRoom", "BuildingInstallation"])
        if context.scene.cgml3.import_bridges:
            feature_type_filter.update(["Bridge", "BridgePart"])
        if context.scene.cgml3.import_tunnels:
            feature_type_filter.update(["Tunnel", "TunnelPart"])
        if context.scene.cgml3.import_vegetation:
            feature_type_filter.update(["SolitaryVegetationObject", "PlantCover"])
        if context.scene.cgml3.import_water:
            feature_type_filter.update(["WaterBody", "WaterSurface"])
        if context.scene.cgml3.import_transportation:
            feature_type_filter.update(["Road", "Railway", "Track", "Square", "Intersection"])
        if context.scene.cgml3.import_cityfurniture:
            feature_type_filter.add("CityFurniture")
        if context.scene.cgml3.import_landuse:
            feature_type_filter.add("LandUse")
        if context.scene.cgml3.import_relief:
            feature_type_filter.update(["ReliefFeature", "TINRelief", "MassPointRelief", "BreaklineRelief", "RasterRelief"])
        if context.scene.cgml3.import_generics:
            # CityGML 3.0: "GenericCityObject" is CityGML 2.0 terminology.
            # In CityGML 3.0, generic features are represented as Generic*Space and
            # GenericThematicSurface. Keep the UI label but map to the actual 3.0 tags.
            feature_type_filter.update([
                "GenericOccupiedSpace",
                "GenericLogicalSpace",
                "GenericUnoccupiedSpace",
                "GenericThematicSurface",
            ])

    # Appearance-Ordner nur anlegen, wenn Appearance mit importiert werden soll.
    if import_appearance:
        appearance_dir = (Path(path).parent / "Appearance")
        try:
            appearance_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    coll = ensure_collection(context, f"CityGML3:{Path(path).stem}")

    # Unter-Collections pro Feature-Typ (Building, Bridge, SolitaryVegetationObject, ...)
    type_collections = {}

    def get_type_collection(feat_ln: str):
        """
        Liefert eine Unter-Collection innerhalb der Haupt-Collection `coll`,
        benannt nach dem Feature-Typ (z.B. 'Building', 'Bridge', 'PlantCover', ...).
        Wird bei Bedarf einmalig erzeugt und für weitere Objekte wiederverwendet.
        """
        name = feat_ln or "UnknownType"

        # schon gecacht?
        sub = type_collections.get(name)
        if sub is not None:
            _ensure_inline_part_collection_visible(sub)
            return sub

        # prüfen, ob unter coll schon eine Child-Collection mit diesem Namen hängt
        for c in coll.children:
            if c.name == name:
                sub = c
                break

        # falls nicht vorhanden: neue Collection erzeugen und unter coll einhängen
        if sub is None:
            sub = bpy.data.collections.new(name)
            coll.children.link(sub)

        _ensure_inline_part_collection_visible(sub)
        type_collections[name] = sub
        return sub

    # srs aus dem Wurzel-Attribut; für viele GMLs steht nur im Envelope etwas
    default_srs = root.get("srsName")
    # Envelope-SRS und -Ecke lesen
    env_srs, lower_corner = _get_envelope_srs_and_corners(root)

    # vertikales EPSG extrahieren (Compound-CRS) und in World schreiben
    if not context.scene.cgml3.import_local:
        try:
            w = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
            z_epsg = _extract_vertical_epsg(env_srs or default_srs or "")
            if z_epsg:
                w["Z-EPSG"] = z_epsg  # automatisch setzen
            # Z-Origin aus Envelope übernehmen, falls 3D
            if lower_corner and len(lower_corner) == 3:
                _set_world_prop_with_ui(w, "Z-Origin", float(lower_corner[2]))
        except Exception:
            pass

    # PERFORMANCE: Schnell-Check ob Appearance-Daten existieren
    has_appearance = False
    if import_appearance:
        for elem in root.iter():
            if elem.tag and isinstance(elem.tag, str) and elem.tag.endswith('Appearance'):
                has_appearance = True
                break
    
    if import_appearance and has_appearance:
        matcolor_by_surface_id = parse_x3d_materials(root)
        ptex_by_ring, poly_to_image, ptex_by_poly = parse_parameterized_textures(root, path)
        gtex_by_poly = parse_georeferenced_textures(root, path)
    else:
        # Keine Appearance-Daten → leere Dicts verwenden (spart viel Zeit)
        matcolor_by_surface_id = {}
        ptex_by_ring = {}
        poly_to_image = {}
        ptex_by_poly = {}
        gtex_by_poly = {}
    
    if wm:
        wm.progress_update(25)  # 25%: Appearance parsed
    if import_appearance:
        print_console_progress("Appearance-Daten verarbeitet", percentage=25)
    else:
        print_console_progress("Appearance-Import übersprungen", percentage=25)

    world = context.scene.world or bpy.data.worlds.new("World")
    context.scene.world = world
    scene_georef_copied = False
    if not context.scene.cgml3.import_local:
        scene_georef_copied = _copy_scene_georeference_to_world(context, world)
    file_origin, file_crs = _determine_file_origin_and_crs(root)
    if scene_georef_copied:
        try:
            if not _crs_matches(str(world.get("CRS", "")), file_crs):
                scene_georef_copied = False
        except Exception:
            scene_georef_copied = False
    if not context.scene.cgml3.import_local:
        if scene_georef_copied and "Z-Origin" not in world.keys():
            _set_world_prop_with_ui(world, "Z-Origin", file_origin[2])
        if (not scene_georef_copied) and (("CRS" not in world.keys()) or world["CRS"] == "Unknown CRS" or not _crs_matches(world["CRS"], file_crs)):
            # Also check Scene["SRID"] before overwriting
            sc = context.scene if not isinstance(context, dict) else bpy.context.scene
            scene_georef = _get_valid_scene_georeference(sc)
            scene_crs = scene_georef[0] if scene_georef else ""
            if scene_georef and _crs_matches(scene_crs, file_crs):
                # Scene has matching CRS - copy origin from Scene to World
                _, scene_x_origin, scene_y_origin = scene_georef
                world["CRS"] = file_crs
                _set_world_prop_with_ui(world, "X-Origin", scene_x_origin)
                _set_world_prop_with_ui(world, "Y-Origin", scene_y_origin)
                _set_world_prop_with_ui(world, "Z-Origin", file_origin[2])
            else:
                world["CRS"] = file_crs
                _set_world_prop_with_ui(world, "X-Origin", file_origin[0])
                _set_world_prop_with_ui(world, "Y-Origin", file_origin[1])
                _set_world_prop_with_ui(world, "Z-Origin", file_origin[2])
    if context.scene.cgml3.import_local:
        # Verwende die lower corner des Envelope als Offset
        # Dadurch bleiben alle Objekte relativ zueinander, aber am Origin (0,0,0)
        if lower_corner and len(lower_corner) >= 2:
            ref_origin = (float(lower_corner[0]), float(lower_corner[1]), 
                         float(lower_corner[2]) if len(lower_corner) == 3 else 0.0)
            print(f"[CityGML Import] Lokaler Import aktiv - Offset von Envelope: {ref_origin}")
        else:
            # Fallback: verwende file_origin
            ref_origin = file_origin
            print(f"[CityGML Import] Lokaler Import aktiv - Offset von file_origin: {ref_origin}")
    else:
        ref_origin = (float(world["X-Origin"]), float(world["Y-Origin"]), float(world.get("Z-Origin", file_origin[2])))

    # Gruppen für implizite Geometrien:
    # template_id -> Liste von (feat_id, Objekt, M_eff)
    implicit_groups = {}

    # Hilfsfunktion: sortKey extrahieren (int oder großer Fallback)
    def _get_sort_key(el):
        try:
            s = el.findtext(".//bldg:sortKey", namespaces=NS)
            return int(s.strip()) if s and s.strip().isdigit() else 10**9
        except Exception:
            return 10**9

    # Spatial Index für BBOX-Filter (optional, deutlich schneller für große Datensätze)
    spatial_index = None
    gml_id_filter_from_spatial = None
    
    if bbox_coords:
        try:
            from .spatial_index import SpatialIndex, RTREE_AVAILABLE
            
            # Build spatial index if rtree available and beneficial
            feature_count_estimate = sum(1 for _ in root.findall(".//core:cityObjectMember", NS))
            use_spatial_index = RTREE_AVAILABLE and feature_count_estimate > 500
            
            if use_spatial_index:
                print(f"Building spatial index for {feature_count_estimate} features...")
                spatial_index = SpatialIndex.from_gml_file(path)
                
                # Query spatial index for features in BBOX
                xmin, ymin, xmax, ymax = bbox_coords
                gml_id_filter_from_spatial = set(spatial_index.query_bbox(xmin, ymin, xmax, ymax))
                
                # Combine with existing gml_id_filter if present
                if gml_id_filter:
                    gml_id_filter = gml_id_filter.intersection(gml_id_filter_from_spatial)
                else:
                    gml_id_filter = gml_id_filter_from_spatial
                
                stats = spatial_index.get_stats()
                print(f"Spatial index: {stats['feature_count']} features indexed, "
                      f"{len(gml_id_filter)} in query bbox ({stats['backend']} backend)")
        except ImportError:
            # Spatial indexing not available, fall back to traditional filtering
            pass
        except Exception as e:
            print(f"Warning: Spatial index failed, using traditional BBOX filter: {e}")

    # Features einsammeln (inkl. optionalem GML-ID/BBOX/Feature-Type-Filter) und stabil nach sortKey sortieren
    # Zähle Features OHNE Filter für Statistik
    total_features_before_filter = 0
    for member in root.findall(".//core:cityObjectMember", NS):
        for child in member:
            if isinstance(child.tag, str) and child.tag.startswith("{"):
                total_features_before_filter += 1
    
    # Pass gml_id_filter (potentially enriched by spatial index) and bbox_coords for double-check
    feats = list(iter_features(root, gml_id_filter=gml_id_filter, bbox_filter=bbox_coords if not spatial_index else None, feature_type_filter=feature_type_filter))
    feats.sort(key=_get_sort_key)
    
    total_feats = len(feats)
    features_filtered = total_features_before_filter - total_feats
    if wm:
        wm.progress_update(30)  # 30%: Features collected
    print_console_progress("Features gesammelt", percentage=30)

    # Globale Mappings für nachträgliche Beziehungen
    id_to_obj = {}          # gml:id -> Blender-Objekt
    building_part_edges = []  # List of (parent_building_id, part_id) via bldg:buildingPart (CityGML 3)
    building_installation_edges = []  # List of (parent_building_id, inst_id) via bldg:buildingInstallation (CityGML 3)
    building_furniture_edges = []  # List of (parent_id, furn_id) via bldg:buildingFurniture (CityGML 3)
    building_storey_edges = []  # List of (parent_building_id, storey_id) via bldg:buildingSubdivision/Storey (CityGML 3)
    bridge_part_edges = []    # List of (parent_bridge_id, part_id) via brid:bridgePart (CityGML 3)
    bridge_installation_edges = []  # List of (parent_bridge_id, inst_id) via brid:bridgeInstallation (CityGML 3)
    bridge_furniture_edges = []  # List of (parent_bridge_id, furn_id) via brid:bridgeFurniture (CityGML 3)
    tunnel_part_edges = []    # List of (parent_tunnel_id, part_id) via tun:tunnelPart (CityGML 3)
    tunnel_installation_edges = []  # List of (parent_tunnel_id, inst_id) via tun:tunnelInstallation (CityGML 3)
    tunnel_furniture_edges = []  # List of (parent_tunnel_id, furn_id) via tun:tunnelFurniture (CityGML 3)
    group_defs = []         # CityObjectGroup-Infos
    version_defs = []       # Version-Infos
    transition_defs = []    # VersionTransition-Infos
    dynamizer_defs = []     # Dynamizer-Owner-Infos 
    xlink_href = "{http://www.w3.org/1999/xlink}href"

    # Pre-scan: Part-Beziehungen (CityGML 3.0)
    # - Building -> buildingPart -> BuildingPart
    # - Bridge -> bridgePart -> BridgePart
    # - Tunnel -> tunnelPart -> TunnelPart
    try:
        gml_id_attr = f"{{{NS['gml']}}}id"

        for b in root.findall(".//bldg:Building", NS):
            parent_id = b.get(gml_id_attr)
            if not parent_id:
                continue
            for rel in b.findall("./bldg:buildingPart", NS):
                part_el = rel.find("./bldg:BuildingPart", NS)
                if part_el is None:
                    continue
                part_id = part_el.get(gml_id_attr)
                if part_id:
                    building_part_edges.append((parent_id, part_id))

        for b in root.findall(".//bldg:Building", NS) + root.findall(".//bldg:BuildingPart", NS):
            parent_id = b.get(gml_id_attr)
            if not parent_id:
                continue
            for rel in b.findall("./bldg:buildingInstallation", NS):
                inst_el = rel.find("./bldg:BuildingInstallation", NS)
                if inst_el is None:
                    continue
                inst_id = inst_el.get(gml_id_attr)
                if inst_id:
                    building_installation_edges.append((parent_id, inst_id))

        for b in root.findall(".//bldg:Building", NS) + root.findall(".//bldg:BuildingPart", NS):
            parent_id = b.get(gml_id_attr)
            if not parent_id:
                continue
            for rel in b.findall("./bldg:buildingFurniture", NS):
                furn_el = rel.find("./bldg:BuildingFurniture", NS)
                if furn_el is None:
                    continue
                furn_id = furn_el.get(gml_id_attr)
                if furn_id:
                    building_furniture_edges.append((parent_id, furn_id))

        for b in root.findall(".//bldg:Building", NS):
            parent_id = b.get(gml_id_attr)
            if not parent_id:
                continue
            for rel in b.findall("./bldg:buildingSubdivision", NS):
                storey_el = rel.find("./bldg:Storey", NS)
                if storey_el is None:
                    continue
                storey_id = storey_el.get(gml_id_attr)
                if not storey_id:
                    storey_id = f"Storey_{uuid4().hex[:12]}"
                    storey_el.set(gml_id_attr, storey_id)
                building_storey_edges.append((parent_id, storey_id))

        for br in root.findall(".//brid:Bridge", NS) + root.findall(".//brid:BridgePart", NS):
            parent_id = br.get(gml_id_attr)
            if not parent_id:
                continue
            for rel in br.findall("./brid:bridgePart", NS):
                part_el = rel.find("./brid:BridgePart", NS)
                if part_el is None:
                    continue
                part_id = part_el.get(gml_id_attr)
                if part_id:
                    bridge_part_edges.append((parent_id, part_id))
            for rel in br.findall("./brid:bridgeInstallation", NS):
                inst_el = rel.find("./brid:BridgeInstallation", NS)
                if inst_el is None:
                    continue
                inst_id = inst_el.get(gml_id_attr)
                if inst_id:
                    bridge_installation_edges.append((parent_id, inst_id))
            for rel in br.findall("./brid:bridgeFurniture", NS):
                furn_el = rel.find("./brid:BridgeFurniture", NS)
                if furn_el is None:
                    continue
                furn_id = furn_el.get(gml_id_attr)
                if furn_id:
                    bridge_furniture_edges.append((parent_id, furn_id))

        for tn in root.findall(".//tun:Tunnel", NS) + root.findall(".//tun:TunnelPart", NS):
            parent_id = tn.get(gml_id_attr)
            if not parent_id:
                continue
            for rel in tn.findall("./tun:tunnelPart", NS):
                part_el = rel.find("./tun:TunnelPart", NS)
                if part_el is None:
                    continue
                part_id = part_el.get(gml_id_attr)
                if part_id:
                    tunnel_part_edges.append((parent_id, part_id))
            for rel in tn.findall("./tun:tunnelInstallation", NS):
                inst_el = rel.find("./tun:TunnelInstallation", NS)
                if inst_el is None:
                    continue
                inst_id = inst_el.get(gml_id_attr)
                if inst_id:
                    tunnel_installation_edges.append((parent_id, inst_id))
            for rel in tn.findall("./tun:tunnelFurniture", NS):
                furn_el = rel.find("./tun:TunnelFurniture", NS)
                if furn_el is None:
                    continue
                furn_id = furn_el.get(gml_id_attr)
                if furn_id:
                    tunnel_furniture_edges.append((parent_id, furn_id))
    except Exception:
        building_part_edges = []
        building_installation_edges = []
        building_furniture_edges = []
        building_storey_edges = []
        bridge_part_edges = []
        bridge_installation_edges = []
        bridge_furniture_edges = []
        tunnel_part_edges = []
        tunnel_installation_edges = []
        tunnel_furniture_edges = []

    # Pre-scan: BuildingRoom relationships (CityGML 3.0)
    # Rooms can be children of Building, BuildingPart, or Storey via buildingRoom wrapper.
    building_room_edges = []
    try:
        # Rooms directly under Building/BuildingPart
        for b_tag in ("bldg:Building", "bldg:BuildingPart"):
            for b in root.findall(f".//{b_tag}", NS):
                parent_id = b.get(gml_id_attr)
                if not parent_id:
                    continue
                for rel in b.findall("./bldg:buildingRoom", NS):
                    room_el = rel.find("./bldg:BuildingRoom", NS)
                    if room_el is None:
                        continue
                    room_id = room_el.get(gml_id_attr)
                    if not room_id:
                        room_id = f"Room_{uuid4().hex[:12]}"
                        room_el.set(gml_id_attr, room_id)
                    building_room_edges.append((parent_id, room_id))

        # Rooms under Storey (bldg:Storey > bldg:buildingRoom > bldg:BuildingRoom)
        for storey_el in root.findall(".//bldg:Storey", NS):
            parent_id = storey_el.get(gml_id_attr)
            if not parent_id:
                continue
            for rel in storey_el.findall("./bldg:buildingRoom", NS):
                room_el = rel.find("./bldg:BuildingRoom", NS)
                if room_el is None:
                    continue
                room_id = room_el.get(gml_id_attr)
                if not room_id:
                    room_id = f"Room_{uuid4().hex[:12]}"
                    room_el.set(gml_id_attr, room_id)
                building_room_edges.append((parent_id, room_id))
    except Exception:
        building_room_edges = []

    # Helper lookups for hierarchical Storey import
    _hierarchical_building_ids = set()
    _storey_to_building_id = {}
    for _parent_id, _storey_id in building_storey_edges:
        _hierarchical_building_ids.add(_parent_id)
        _storey_to_building_id[_storey_id] = _parent_id

    # Helper lookup for BuildingRoom import
    _room_to_parent_id = {}
    for _parent_id, _room_id in building_room_edges:
        _room_to_parent_id[_room_id] = _parent_id
        # Buildings with rooms are also hierarchical
        if _parent_id not in _storey_to_building_id:
            _hierarchical_building_ids.add(_parent_id)

    for feat_idx, feat in enumerate(feats):
        feat_ln = feat.tag.split("}")[-1]
        feat_id = feat.get(f"{{{NS['gml']}}}id") or f"feat_{abs(hash(ET.tostring(feat)))}"

        if _IMPORT_DIAG:
            try:
                ga_nodes = _diag_count_generic_attribute_nodes(feat)
                if _diag_should_log_feature(str(feat_id or ""), ga_nodes):
                    print(f"[CityGML3][DIAG] pre-build feature={feat_id} type={feat_ln} genericAttribute_nodes={ga_nodes}")
            except Exception:
                pass
        
        # Update progress (30% to 90% for feature processing)
        # OPTIMIZED: use enumerate index instead of O(n) feats.index(feat)
        if wm and total_feats > 0:
            progress = 30 + int((feat_idx / total_feats) * 60)
            wm.progress_update(progress)
            print_console_progress("Features verarbeiten", current=feat_idx+1, total=total_feats)

        # NEU: CityObjectGroup als Empty ohne Mesh behandeln
        feat_tag = feat.tag
        feat_uri = feat_tag.split("}")[0].strip("{") if isinstance(feat_tag, str) and "}" in feat_tag else ""
        if feat_uri == NS.get("grp") and feat_ln == "CityObjectGroup":
            # Empty erzeugen
            grp_obj = bpy.data.objects.new(feat_id, None)
            grp_obj.empty_display_type = 'PLAIN_AXES'
            get_type_collection(feat_ln).objects.link(grp_obj)

            grp_obj["cgml3_feature"] = feat_ln
            grp_obj["gml_id"] = feat_id

            # optional: gml:name als Objektname verwenden
            try:
                name_txt = feat.findtext(".//gml:name", namespaces=NS)
                if name_txt and name_txt.strip():
                    grp_obj.name = name_txt.strip()
            except Exception:
                pass

            # grp:class / grp:function / grp:usage übernehmen
            try:
                gclass = feat.findtext(".//grp:class", namespaces=NS)
                if gclass and gclass.strip():
                    grp_obj["grp:class"] = gclass.strip()

                gfuncs = feat.findall(".//grp:function", namespaces=NS)
                if gfuncs:
                    grp_obj["grp:function"] = [
                        f.text.strip()
                        for f in gfuncs
                        if f.text and f.text.strip()
                    ]

                gusages = feat.findall(".//grp:usage", namespaces=NS)
                if gusages:
                    grp_obj["grp:usage"] = [
                        u.text.strip()
                        for u in gusages
                        if u.text and u.text.strip()
                    ]
            except Exception:
                pass

            # Gruppen-Mitglieder mit Role-System (CityGML 3.0) oder einfache XLinks (CityGML 2.0)
            # Format: List of (member_id, role_string)
            member_roles = []
            
            for gm_el in feat.findall(".//grp:groupMember", NS):
                # CityGML 3.0: groupMember enthält Role-Element
                role_el = gm_el.find("grp:Role", NS)
                
                if role_el is not None:
                    # CityGML 3.0 mit Role
                    role_str = role_el.findtext("grp:role", default=None, namespaces=NS)
                    if role_str:
                        role_str = role_str.strip()
                    
                    # XLink ist im inneren groupMember des Role-Elements
                    inner_gm = role_el.find("grp:groupMember", NS)
                    if inner_gm is not None:
                        href = inner_gm.get(xlink_href)
                        if href:
                            href = href.strip()
                            if href.startswith("#"):
                                href = href[1:]
                            if href:
                                member_roles.append((href, role_str))
                else:
                    # CityGML 2.0: direkter XLink im groupMember
                    href = gm_el.get(xlink_href)
                    if href:
                        href = href.strip()
                        if href.startswith("#"):
                            href = href[1:]
                        if href:
                            member_roles.append((href, None))

            # Parent-Referenz (grp:parent@xlink:href), falls vorhanden
            parent_id = None
            parent_el = feat.find(".//grp:parent", NS)
            if parent_el is not None:
                href = parent_el.get(xlink_href)
                if href:
                    href = href.strip()
                    if href.startswith("#"):
                        href = href[1:]
                    if href:
                        parent_id = href

            # generische Attribute der Gruppe übernehmen
            gen_attrs = _parse_generic_attributes(feat)
            for k, v in gen_attrs.items():
                try:
                    grp_obj[k] = v
                except Exception:
                    grp_obj[k] = str(v)

            # Gruppe im Mapping registrieren und für zweite Phase merken
            # Format: (group_obj, [(member_id, role), ...], parent_id)
            id_to_obj[feat_id] = grp_obj
            group_defs.append((grp_obj, member_roles, parent_id))

            # CityObjectGroup bekommt kein Mesh → nächstes Feature
            continue

        # pcl:PointCloud - CityGML 3.0 Point Cloud Feature
        if feat_uri == NS.get("pcl") and feat_ln == "PointCloud":
            try:
                # Parse PointCloud metadata
                metadata = parse_pointcloud_metadata(feat)
                
                points = []
                attributes = None
                
                # Option 1: Inline points (pcl:points/gml:MultiPoint)
                inline_points = parse_pointcloud_inline_points(feat)
                if inline_points:
                    points = inline_points
                    print(f"PointCloud {feat_id}: Loaded {len(points)} inline points")
                
                # Option 2: External file (pcl:pointFile)
                elif metadata.get("pointFile"):
                    external_path = resolve_external_file_path(
                        metadata["pointFile"],
                        _IMPORT_CONTEXT.get("citygml_filepath")
                    )
                    if external_path:
                        print(f"PointCloud {feat_id}: Loading from {external_path}")
                        points, attributes = load_external_pointcloud(
                            external_path,
                            metadata.get("mimeType")
                        )
                    else:
                        print(f"PointCloud {feat_id}: External file not found: {metadata['pointFile']}")
                
                # Create Blender PointCloud object
                if points:
                    pc_obj = create_blender_pointcloud(
                        name=feat_id,
                        points=points,
                        attributes=attributes,
                        ref_origin=ref_origin
                    )
                    
                    if pc_obj:
                        get_type_collection(feat_ln).objects.link(pc_obj)
                        
                        # Store standard attributes
                        pc_obj["cgml3_feature"] = feat_ln
                        pc_obj["gml_id"] = feat_id
                        pc_obj["geometry_type"] = "PointCloud"
                        pc_obj["point_count"] = len(points)
                        pc_obj["cgml3_is_pointcloud"] = True  # Mark as PointCloud for export
                        
                        # Store PointCloud metadata
                        if metadata.get("mimeType"):
                            pc_obj["pcl:mimeType"] = metadata["mimeType"]
                        if metadata.get("pointFile"):
                            pc_obj["pcl:pointFile"] = metadata["pointFile"]
                        if metadata.get("pointFileSrsName"):
                            pc_obj["pcl:pointFileSrsName"] = metadata["pointFileSrsName"]
                        if metadata.get("multipoint_id"):
                            pc_obj["pcl:multipoint_id"] = metadata["multipoint_id"]
                        
                        # Store attribute flags
                        if attributes:
                            if attributes.get("colors"):
                                pc_obj["pcl:has_colors"] = True
                            if attributes.get("intensity"):
                                pc_obj["pcl:has_intensity"] = True
                            if attributes.get("classification"):
                                pc_obj["pcl:has_classification"] = True
                        
                        id_to_obj[feat_id] = pc_obj
                        
                        # Store common CityGML attributes
                        _store_common_attributes(pc_obj, feat)
                        
                        continue
                else:
                    print(f"PointCloud {feat_id}: No points found (inline or external)")
                    
                    # Create empty object as fallback
                    empty_obj = bpy.data.objects.new(feat_id, None)
                    empty_obj.empty_display_type = 'CUBE'
                    get_type_collection(feat_ln).objects.link(empty_obj)
                    
                    empty_obj["cgml3_feature"] = feat_ln
                    empty_obj["gml_id"] = feat_id
                    empty_obj["geometry_type"] = "PointCloud"
                    empty_obj["point_count"] = 0
                    
                    if metadata.get("pointFile"):
                        empty_obj["pcl:pointFile"] = metadata["pointFile"]
                        empty_obj["pcl:file_not_found"] = True
                    
                    id_to_obj[feat_id] = empty_obj
                    _store_common_attributes(empty_obj, feat)
                    
                    continue
                    
            except Exception as e:
                print(f"Error parsing PointCloud {feat_id}: {e}")
                # Continue with normal feature parsing as fallback
                pass

        # vers:Version als Empty ohne Mesh importieren
        if feat_uri == NS.get("vers") and feat_ln == "Version":
            ver_obj = bpy.data.objects.new(feat_id, None)
            ver_obj.empty_display_type = 'PLAIN_AXES'
            get_type_collection(feat_ln).objects.link(ver_obj)

            ver_obj["cgml3_feature"] = feat_ln
            ver_obj["gml_id"] = feat_id

            # optional: gml:name als Objektname
            try:
                name_txt = feat.findtext(".//gml:name", namespaces=NS)
                if name_txt and name_txt.strip():
                    ver_obj.name = name_txt.strip()
            except Exception:
                pass

            # Lebensdauer aus core:AbstractFeatureWithLifespan
            try:
                for tag in ("creationDate", "terminationDate", "validFrom", "validTo"):
                    dtxt = feat.findtext(f".//core:{tag}", namespaces=NS)
                    if dtxt and dtxt.strip():
                        ver_obj[f"core:{tag}"] = dtxt.strip()
            except Exception:
                pass

            # Version-Members: vers:versionMember @xlink:href
            member_ids = []
            for vm in feat.findall(".//vers:versionMember", NS):
                href = vm.get(xlink_href)
                if not href:
                    continue
                href = href.strip()
                if href.startswith("#"):
                    href = href[1:]
                if href:
                    member_ids.append(href)
            if member_ids:
                ver_obj["vers:member_ids"] = member_ids

            # generische Attribute übernehmen
            gen_attrs = _parse_generic_attributes(feat)
            for k, v in gen_attrs.items():
                try:
                    ver_obj[k] = v
                except Exception:
                    ver_obj[k] = str(v)

            # für zweite Phase merken
            id_to_obj[feat_id] = ver_obj
            version_defs.append((ver_obj, member_ids))

            # keine Geometrie erzeugen
            continue

        # vers:VersionTransition als Empty ohne Mesh
        if feat_uri == NS.get("vers") and feat_ln == "VersionTransition":
            trans_obj = bpy.data.objects.new(feat_id, None)
            trans_obj.empty_display_type = 'PLAIN_AXES'
            get_type_collection(feat_ln).objects.link(trans_obj)

            trans_obj["cgml3_feature"] = feat_ln
            trans_obj["gml_id"] = feat_id

            # optional: gml:name
            try:
                name_txt = feat.findtext(".//gml:name", namespaces=NS)
                if name_txt and name_txt.strip():
                    trans_obj.name = name_txt.strip()
            except Exception:
                pass

            # Lebensdauer (optional, gleiche Felder wie oben)
            try:
                for tag in ("creationDate", "terminationDate", "validFrom", "validTo"):
                    dtxt = feat.findtext(f".//core:{tag}", namespaces=NS)
                    if dtxt and dtxt.strip():
                        trans_obj[f"core:{tag}"] = dtxt.strip()
            except Exception:
                pass

            # von / nach Version (xlink:href in vers:from / vers:to)
            from_id = None
            to_id = None

            from_el = feat.find(".//vers:from", NS)
            if from_el is not None:
                href = from_el.get(xlink_href)
                if href:
                    href = href.strip()
                    if href.startswith("#"):
                        href = href[1:]
                    if href:
                        from_id = href

            to_el = feat.find(".//vers:to", NS)
            if to_el is not None:
                href = to_el.get(xlink_href)
                if href:
                    href = href.strip()
                    if href.startswith("#"):
                        href = href[1:]
                    if href:
                        to_id = href

            if from_id:
                trans_obj["vers:from_id"] = from_id
            if to_id:
                trans_obj["vers:to_id"] = to_id

            # optional: Typ/Grund (nur wenn im Dataset vorhanden)
            try:
                ttype = feat.findtext(".//vers:transitionType", namespaces=NS)
                if ttype and ttype.strip():
                    trans_obj["vers:transitionType"] = ttype.strip()
                treason = feat.findtext(".//vers:reason", namespaces=NS)
                if treason and treason.strip():
                    trans_obj["vers:reason"] = treason.strip()
            except Exception:
                pass

            # generische Attribute
            gen_attrs = _parse_generic_attributes(feat)
            for k, v in gen_attrs.items():
                try:
                    trans_obj[k] = v
                except Exception:
                    trans_obj[k] = str(v)

            id_to_obj[feat_id] = trans_obj
            transition_defs.append((trans_obj, from_id, to_id))

            # keine Geometrie
            continue

        # dyn:Dynamizer als Empty ohne Mesh
        if feat_uri == NS.get("dyn") and feat_ln == "Dynamizer":
            dyn_obj = bpy.data.objects.new(feat_id, None)
            dyn_obj.empty_display_type = 'SPHERE'
            get_type_collection(feat_ln).objects.link(dyn_obj)

            dyn_obj["cgml3_feature"] = feat_ln
            dyn_obj["gml_id"] = feat_id

            # optional: gml:name als Objektname
            try:
                name_txt = feat.findtext(".//gml:name", namespaces=NS)
                if name_txt and name_txt.strip():
                    dyn_obj.name = name_txt.strip()
            except Exception:
                pass

            # Lebensdauer-Felder aus core:AbstractFeatureWithLifespan
            try:
                for tag in ("creationDate", "terminationDate", "validFrom", "validTo"):
                    dtxt = feat.findtext(f".//core:{tag}", namespaces=NS)
                    if dtxt and dtxt.strip():
                        dyn_obj[f"core:{tag}"] = dtxt.strip()
            except Exception:
                pass

            # Dynamizer-spezifische Felder
            try:
                attr_ref = feat.findtext(".//dyn:attributeRef", namespaces=NS)
                if attr_ref and attr_ref.strip():
                    dyn_obj["dyn:attributeRef"] = attr_ref.strip()

                start_time = feat.findtext(".//dyn:startTime", namespaces=NS)
                if start_time and start_time.strip():
                    dyn_obj["dyn:startTime"] = start_time.strip()

                end_time = feat.findtext(".//dyn:endTime", namespaces=NS)
                if end_time and end_time.strip():
                    dyn_obj["dyn:endTime"] = end_time.strip()
            except Exception:
                pass

            # Timeseries-Typ und Datenquellen grob klassifizieren
            try:
                if feat.find(".//dyn:AtomicTimeseries", NS) is not None:
                    dyn_obj["dyn:timeseriesType"] = "AtomicTimeseries"
                elif feat.find(".//dyn:CompositeTimeseries", NS) is not None:
                    dyn_obj["dyn:timeseriesType"] = "CompositeTimeseries"

                if feat.find(".//dyn:AtomicTimeseries//dyn:dynamicDataDR", NS) is not None:
                    dyn_obj["dyn:hasDynamicDataDR"] = True
                if feat.find(".//dyn:AtomicTimeseries//dyn:dynamicDataTVP", NS) is not None:
                    dyn_obj["dyn:hasDynamicDataTVP"] = True
                if feat.find(".//dyn:AtomicTimeseries//dyn:observationData", NS) is not None:
                    dyn_obj["dyn:hasObservationData"] = True

                if feat.find(".//dyn:SensorConnection", NS) is not None:
                    dyn_obj["dyn:hasSensorConnection"] = True
            except Exception:
                pass

            # generische Attribute übernehmen (falls welche dranhängen)
            gen_attrs = _parse_generic_attributes(feat)
            for k, v in gen_attrs.items():
                try:
                    dyn_obj[k] = v
                except Exception:
                    dyn_obj[k] = str(v)

            id_to_obj[feat_id] = dyn_obj

            # Owner aus Mapping (falls vorhanden) merken
            owner_id = dyn_owner_by_id.get(feat_id)
            if owner_id:
                dyn_obj["dyn:owner_id"] = owner_id
            dynamizer_defs.append((dyn_obj, owner_id))

            # keine Geometrie für Dynamizer
            continue

        # NORMALER GEOMETRIEPFAD (alle anderen Features)

        # Falls dieses Feature ausschließlich über eine ImplicitGeometry kommt,
        # bauen wir das Mesh als Template (lokal) und legen die komplette Affin-Transformation
        # (inkl. individueller Skalierung) auf Objekt-Ebene ab.
        tpl_id, M_eff, implicit_lod_level = _implicit_instance_info(feat, ref_origin)
        is_implicit_instance = bool(tpl_id and M_eff is not None)

        # Exclude BuildingPart, Storey and BuildingRoom sub-features from Building geometry so they become separate objects
        _exclude_st = None
        if feat_ln in ("Building", "BuildingPart"):
            _sub_els = feat.findall(".//bldg:BuildingPart", NS) + feat.findall(".//bldg:Storey", NS) + feat.findall(".//bldg:BuildingRoom", NS) + feat.findall(".//bldg:BuildingInstallation", NS)
            if _sub_els:
                _exclude_st = _sub_els
        elif feat_ln in ("Bridge", "BridgePart"):
            _sub_els = feat.findall(".//brid:BridgePart", NS) + feat.findall(".//brid:BridgeInstallation", NS)
            if _sub_els:
                _exclude_st = _sub_els
        elif feat_ln in ("Tunnel", "TunnelPart"):
            _sub_els = feat.findall(".//tun:TunnelPart", NS) + feat.findall(".//tun:TunnelInstallation", NS)
            if _sub_els:
                _exclude_st = _sub_els

        (
            faces,
            verts,
            surf_labels,
            surf_attrs_by_poly,
            surface_id_by_poly,
            multisurface_id_by_poly,
            compositesurface_id_by_poly,
            poly_also_in_solid,  # NEU: Tracking für Polygone die auch in Solid vorkommen
            filling_parent_surface_id_by_poly,  # NEU: Parent-Surface-ID für fillingSurface
        ) = build_geometry(
            feat, default_srs, ref_origin, include_surface_attrs=True, lod_filter=lod_filter, bake_implicit=(not is_implicit_instance),
            exclude_subtrees=_exclude_st
        )

        # Prüfe auch nach Point-Geometrien (LOD0)
        points = parse_points(feat, default_srs, ref_origin)

        # Falls keine Faces aber Points vorhanden: Empty-Objekt(e) erstellen
        if not faces and points:
            # Bei mehreren Points: erstelle mehrere Empty-Objekte oder ein Mesh mit Vertices
            if len(points) == 1:
                # Einzelner Point: Empty-Objekt (SPHERE für bessere Sichtbarkeit)
                x, y, z, pt_id, srs = points[0]
                empty_obj = bpy.data.objects.new(feat_id, None)
                empty_obj.empty_display_type = 'SPHERE'
                empty_obj.empty_display_size = 1.0
                empty_obj.location = (x, y, z)
                get_type_collection(feat_ln).objects.link(empty_obj)
                
                empty_obj["cgml3_feature"] = feat_ln
                empty_obj["gml_id"] = feat_id
                empty_obj["geometry_type"] = "Point"
                if pt_id:
                    empty_obj["point_id"] = pt_id
                if srs:
                    empty_obj["srs"] = srs
                
                # Store coordinates in custom properties
                empty_obj["point:x"] = x + ref_origin[0]
                empty_obj["point:y"] = y + ref_origin[1]
                empty_obj["point:z"] = z + ref_origin[2]
                
                id_to_obj[feat_id] = empty_obj
                
                # Store attributes
                _store_common_attributes(empty_obj, feat)
                
                continue
            
            else:
                # Mehrere Points: erstelle Mesh mit Vertices (keine Faces)
                mesh = bpy.data.meshes.new(feat_id)
                point_verts = [(p[0], p[1], p[2]) for p in points]
                mesh.from_pydata(point_verts, [], [])
                
                obj = bpy.data.objects.new(mesh.name, mesh)
                get_type_collection(feat_ln).objects.link(obj)
                
                obj["cgml3_feature"] = feat_ln
                obj["gml_id"] = feat_id
                obj["geometry_type"] = "MultiPoint"
                obj["point_count"] = len(points)
                
                # Store coordinates in custom properties (first point as reference)
                if points:
                    obj["point:x"] = points[0][0] + ref_origin[0]
                    obj["point:y"] = points[0][1] + ref_origin[1]
                    obj["point:z"] = points[0][2] + ref_origin[2]
                
                # For MassPointRelief: Copy relief-specific properties
                if feat_ln == "MassPointRelief":
                    # Copy all dem: properties from the temporary obj dict
                    for key in list(obj.keys()):
                        if isinstance(key, str) and key.startswith("dem:"):
                            try:
                                # Properties already set in obj dict, just ensure they're copied
                                pass
                            except:
                                pass
                
                id_to_obj[feat_id] = obj

                # Store per-object CRS (if the feature carries an explicit srsName on geometry)
                try:
                    obj_crs = _infer_feature_crs(feat)
                except Exception:
                    obj_crs = "Unknown CRS"
                if obj_crs != "Unknown CRS":
                    try:
                        obj["CRS"] = obj_crs
                    except Exception:
                        pass
                
                # Store attributes
                _store_common_attributes(obj, feat)
                
                continue

        # Prüfe auch nach LineString-Geometrien (LOD0, BreaklineRelief)
        linestrings = parse_linestrings(feat, default_srs, ref_origin)
        
        # Falls keine Faces aber LineStrings vorhanden: Curve-Objekt(e) erstellen
        if not faces and linestrings:
            # Bei mehreren LineStrings oder einem mit vielen Punkten: Curve-Objekt erstellen
            curve_data = bpy.data.curves.new(feat_id, type='CURVE')
            curve_data.dimensions = '3D'
            curve_data.resolution_u = 2
            
            for coords, ls_id, srs in linestrings:
                # Neues Spline für jede LineString
                spline = curve_data.splines.new('POLY')
                spline.points.add(len(coords) - 1)  # -1 weil ein Punkt schon existiert
                
                for i, (x, y, z) in enumerate(coords):
                    spline.points[i].co = (x, y, z, 1.0)  # 4. Wert ist weight
            
            curve_obj = bpy.data.objects.new(feat_id, curve_data)
            get_type_collection(feat_ln).objects.link(curve_obj)
            
            curve_obj["cgml3_feature"] = feat_ln
            curve_obj["gml_id"] = feat_id
            curve_obj["geometry_type"] = "LineString" if len(linestrings) == 1 else "MultiCurve"
            curve_obj["linestring_count"] = len(linestrings)
            
            # Store first linestring info
            if linestrings:
                coords, ls_id, srs = linestrings[0]
                if ls_id:
                    curve_obj["linestring_id"] = ls_id
                if srs:
                    curve_obj["srs"] = srs
                # Store first coordinate as reference
                if coords:
                    curve_obj["linestring:start_x"] = coords[0][0] + ref_origin[0]
                    curve_obj["linestring:start_y"] = coords[0][1] + ref_origin[1]
                    curve_obj["linestring:start_z"] = coords[0][2] + ref_origin[2]
            
            # For BreaklineRelief: Copy relief-specific properties
            if feat_ln == "BreaklineRelief":
                # Copy all dem: properties from the temporary obj dict
                # These properties were set earlier when parsing the feature
                curve_obj["dem:lod"] = obj.get("dem:lod", 0)
                if obj.get("dem:extent_id"):
                    curve_obj["dem:extent_id"] = obj["dem:extent_id"]
                if obj.get("dem:hasRidgeOrValleyLines"):
                    curve_obj["dem:hasRidgeOrValleyLines"] = True
                if obj.get("dem:hasBreaklines"):
                    curve_obj["dem:hasBreaklines"] = True
                if obj.get("dem:ridgeOrValleyLines_id"):
                    curve_obj["dem:ridgeOrValleyLines_id"] = obj["dem:ridgeOrValleyLines_id"]
                if obj.get("dem:breaklines_id"):
                    curve_obj["dem:breaklines_id"] = obj["dem:breaklines_id"]
            
            id_to_obj[feat_id] = curve_obj
            
            # Store attributes
            _store_common_attributes(curve_obj, feat)
            
            continue

        # Check for RasterRelief with grid data (create mesh from heightmap)
        if feat_ln == "RasterRelief" and not faces:
            # Try to create mesh from grid data
            grid_data = obj.get("dem:grid_data")
            if grid_data:
                try:
                    # Create mesh from grid
                    mesh, verts, grid_faces = create_mesh_from_grid(grid_data, feat_id)
                    
                    # Create Blender object
                    grid_obj = bpy.data.objects.new(feat_id, mesh)
                    get_type_collection(feat_ln).objects.link(grid_obj)
                    
                    # Copy all custom properties from obj dict to grid_obj
                    for key, value in obj.items():
                        if key != "dem:grid_data":  # Don't copy the grid_data itself
                            try:
                                grid_obj[key] = value
                            except:
                                pass
                    
                    grid_obj["cgml3_feature"] = feat_ln
                    grid_obj["gml_id"] = feat_id
                    grid_obj["geometry_type"] = "RasterRelief_Grid"
                    
                    id_to_obj[feat_id] = grid_obj
                    
                    # Store common attributes
                    _store_common_attributes(grid_obj, feat)
                    
                    continue
                except Exception as e:
                    # Fallback: continue with normal processing
                    pass

        if not faces:
            # Hierarchical buildings need an EMPTY even without own geometry
            # (all geometry may reside in Storey children)
            _is_hierarchical_building_nogeom = (
                feat_ln in ("Building", "BuildingPart") and
                feat_id in _hierarchical_building_ids
            )
            if _is_hierarchical_building_nogeom:
                _target_coll = get_type_collection(feat_ln)
                building_empty = bpy.data.objects.new(feat_id, None)
                building_empty.empty_display_type = 'CUBE'
                building_empty.empty_display_size = 2.0
                _target_coll.objects.link(building_empty)
                building_empty["cgml3_feature"] = feat_ln
                building_empty["gml_id"] = feat_id
                building_empty["structure_type"] = "hierarchical"
                id_to_obj[feat_id] = building_empty

                # Store feature-level attributes on the EMPTY
                try:
                    name = feat.findtext(".//gml:name", namespaces=NS)
                    if name and name.strip():
                        building_empty.name = name.strip()
                except Exception:
                    pass
                _store_common_attributes(building_empty, feat)

                # Store CRS
                try:
                    obj_crs = _infer_feature_crs(feat)
                except Exception:
                    obj_crs = "Unknown CRS"
                if obj_crs != "Unknown CRS":
                    try:
                        building_empty["CRS"] = obj_crs
                    except Exception:
                        pass

                # Generic attributes
                gen_attrs = _parse_generic_attributes(feat)
                for k, v in gen_attrs.items():
                    try:
                        building_empty[k] = v
                    except Exception:
                        building_empty[k] = str(v)

                continue

            # Storey children without own geometry still need an object for parenting
            _is_storey_nogeom = (
                feat_ln == "Storey" and
                feat_id in _storey_to_building_id
            )
            if _is_storey_nogeom:
                _target_coll = get_type_collection("Building")
                storey_obj = bpy.data.objects.new(feat_id, None)
                storey_obj.empty_display_type = 'PLAIN_AXES'
                storey_obj.empty_display_size = 1.0
                _target_coll.objects.link(storey_obj)
                storey_obj["cgml3_feature"] = "Storey"
                storey_obj["gml_id"] = feat_id
                storey_obj["structure_part"] = "storey"
                id_to_obj[feat_id] = storey_obj

                # Store feature-level attributes on the EMPTY
                try:
                    name = feat.findtext(".//gml:name", namespaces=NS)
                    if name and name.strip():
                        storey_obj.name = name.strip()
                except Exception:
                    pass
                _store_common_attributes(storey_obj, feat)

                # Generic attributes
                gen_attrs = _parse_generic_attributes(feat)
                for k, v in gen_attrs.items():
                    try:
                        storey_obj[k] = v
                    except Exception:
                        storey_obj[k] = str(v)

                continue

            # BuildingRoom children without own geometry still need an object for parenting
            _is_room_nogeom = (
                feat_ln == "BuildingRoom" and
                feat_id in _room_to_parent_id
            )
            if _is_room_nogeom:
                _target_coll = get_type_collection("Building")
                room_obj = bpy.data.objects.new(feat_id, None)
                room_obj.empty_display_type = 'PLAIN_AXES'
                room_obj.empty_display_size = 1.0
                _target_coll.objects.link(room_obj)
                room_obj["cgml3_feature"] = "BuildingRoom"
                room_obj["gml_id"] = feat_id
                room_obj["structure_part"] = "room"
                id_to_obj[feat_id] = room_obj

                try:
                    name = feat.findtext(".//gml:name", namespaces=NS)
                    if name and name.strip():
                        room_obj.name = name.strip()
                except Exception:
                    pass
                _store_common_attributes(room_obj, feat)

                gen_attrs = _parse_generic_attributes(feat)
                for k, v in gen_attrs.items():
                    try:
                        room_obj[k] = v
                    except Exception:
                        room_obj[k] = str(v)

                continue
            continue

        # CityGML 3: gen:GenericAttributeSet vor <core:boundary>,
        # das via gen:codeSpace auf eine con:*Surface gml:id verweist.
        # Diese Attribute werden auf alle Polygone dieser Surface abgebildet,
        # sodass sie wie „klassische“ Surface-GenericAttributes im Material landen.
        try:
            surf_attrs_by_poly = surf_attrs_by_poly or {}
            surface_polys = {}

            # Surface-ID -> Liste von Polygon-IDs aufbauen
            for poly_id, surf_id in (surface_id_by_poly or {}).items():
                if not surf_id:
                    continue
                surface_polys.setdefault(surf_id, []).append(poly_id)

            if surface_polys:
                # alle GenericAttributeSets im Feature durchgehen
                for ga_set in feat.findall(".//gen:GenericAttributeSet", NS):
                    code_space = (ga_set.findtext(".//gen:codeSpace", namespaces=NS) or "").strip()
                    target_surf_id = _norm_id(code_space)
                    if not target_surf_id or target_surf_id not in surface_polys:
                        continue

                    # vorhandene Logik zum Auslesen von GenericAttributes wiederverwenden
                    attrs = _parse_generic_attributes(ga_set)
                    if not attrs:
                        continue

                    # Attribute auf alle Polygone der referenzierten Surface anwenden
                    for pid in surface_polys[target_surf_id]:
                        prev = surf_attrs_by_poly.get(pid, {})
                        merged = dict(prev)
                        merged.update(attrs)  # Set-Attribute überschreiben ggf. Surface-Attribute
                        surf_attrs_by_poly[pid] = merged
        except Exception as e:
            print(f"[CityGML3] Warning: failed to map GenericAttributeSet to surfaces: {e}")


        # Objekt- und Meshnamen exakt auf gml:id setzen, ohne Feature-Präfix
        # Performance: Check for duplicate geometry and reuse mesh if possible
        # Nur hashen wenn Cache schon befüllt ist (spart bei ersten Objekten Zeit)
        geom_hash = None
        if GEOMETRY_CACHE and verts and faces:
            geom_hash = _compute_geometry_hash(verts, faces)

        # --- Hierarchical Building import (CityGML 3 with Storey children) ---
        _is_hierarchical_building = (feat_ln in ("Building", "BuildingPart") and
                                     feat_id in _hierarchical_building_ids)
        _is_storey_child = (feat_ln == "Storey" and feat_id in _storey_to_building_id)
        _is_room_child = (feat_ln == "BuildingRoom" and feat_id in _room_to_parent_id)
        
        # Determine which collection to use
        if _is_storey_child or _is_room_child:
            _target_coll = get_type_collection("Building")
        else:
            _target_coll = get_type_collection(feat_ln)

        if _is_hierarchical_building:
            # Create Building EMPTY as parent for hierarchical structure
            building_empty = bpy.data.objects.new(feat_id, None)
            building_empty.empty_display_type = 'CUBE'
            building_empty.empty_display_size = 2.0
            _target_coll.objects.link(building_empty)
            building_empty["cgml3_feature"] = feat_ln
            building_empty["gml_id"] = feat_id
            building_empty["structure_type"] = "hierarchical"
            id_to_obj[feat_id] = building_empty

            # Create outer shell mesh as child of the Building EMPTY
            mesh = bpy.data.meshes.new(f"{feat_id}_OuterShell")
            mesh.from_pydata(verts, [], faces)
            obj = bpy.data.objects.new(mesh.name, mesh)
            obj.parent = building_empty
            is_mesh_instance = False
            _target_coll.objects.link(obj)
            obj["structure_part"] = "outer_shell"
            obj["cgml3_feature"] = feat_ln
            obj["gml_id"] = f"{feat_id}_outer"

            # Cache for future reuse (only for non-trivial meshes)
            if geom_hash and len(faces) > 5:
                GEOMETRY_CACHE[geom_hash] = mesh
        elif geom_hash and geom_hash in GEOMETRY_CACHE:
            # Reuse existing mesh (standard mesh sharing without Instance Manager)
            mesh = GEOMETRY_CACHE[geom_hash]
            obj = bpy.data.objects.new(feat_id, mesh)
            is_mesh_instance = True
            _target_coll.objects.link(obj)
        else:
            # Create new mesh
            mesh = bpy.data.meshes.new(feat_id)
            mesh.from_pydata(verts, [], faces)
            obj = bpy.data.objects.new(mesh.name, mesh)
            is_mesh_instance = False
            _target_coll.objects.link(obj)
            
            # Cache for future reuse (only for non-trivial meshes)
            if geom_hash and len(faces) > 5:
                GEOMETRY_CACHE[geom_hash] = mesh

        # Mark Storey children with structure_part
        if _is_storey_child:
            obj["structure_part"] = "storey"

        # Mark BuildingRoom children with structure_part
        if _is_room_child:
            obj["structure_part"] = "room"

        # Implizite Instanz: Affin-Transformation (inkl. individueller Skalierung) auf Objekt-Ebene setzen
        if is_implicit_instance:
            try:
                obj.matrix_world = M_eff
            except Exception:
                pass

        # Mapping gml:id → Objekt (for hierarchical buildings the EMPTY is already stored)
        if not _is_hierarchical_building:
            id_to_obj[feat_id] = obj

        # Store per-object CRS (if the feature carries an explicit srsName on geometry)
        try:
            obj_crs = _infer_feature_crs(feat)
        except Exception:
            obj_crs = "Unknown CRS"
        if obj_crs != "Unknown CRS":
            try:
                obj["CRS"] = obj_crs
                # Also store on EMPTY for hierarchical buildings
                if _is_hierarchical_building:
                    building_empty["CRS"] = obj_crs
            except Exception:
                pass

        # UVs und Materialien zuweisen, solange Face-Reihenfolge noch 1:1 zu surf_labels passt
        # Skip UV/material assignment for mesh instances (already set on original)
        if not is_mesh_instance:
            apply_materials_uvs(
                obj,
                mesh,
                surf_labels,
                matcolor_by_surface_id,
                ptex_by_ring,
                gtex_by_poly,
                poly_to_image,
                path,
                ptex_by_poly=ptex_by_poly,
                surface_attrs_by_poly=surf_attrs_by_poly,
                surface_id_by_poly=surface_id_by_poly,
                multisurface_id_by_poly=multisurface_id_by_poly,
                compositesurface_id_by_poly=compositesurface_id_by_poly,
                poly_also_in_solid=poly_also_in_solid,  # NEU: Markierung für Solid-Duplikate
                filling_parent_surface_id_by_poly=filling_parent_surface_id_by_poly,
            )


            # Danach darf validate/update die interne Reihenfolge ändern – die UVs hängen schon an den Loops
            # PERFORMANCE: validate/update deaktiviert, da from_pydata bereits valide Meshes erzeugt
            # und calc_edges automatisch gesetzt wird. Bei 100+ Objekten spart das erheblich Zeit.
            do_validate = False
            try:
                do_validate = bool(_IMPORT_CONTEXT.get("mesh_validate", False))
            except Exception:
                do_validate = False
            if do_validate:
                mesh.validate(verbose=False)
            # mesh.update(calc_edges=True)  # Deaktiviert für Performance

        # For hierarchical buildings, redirect obj to the EMPTY for attribute assignment.
        # The mesh reference (outer shell) is preserved as _mesh_obj.
        _mesh_obj = obj
        if _is_hierarchical_building:
            obj = building_empty

        obj["cgml3_feature"] = feat_ln
        obj["gml_id"] = feat_id

        # Store implicit LoD level if available (for export roundtrip)
        if is_implicit_instance and implicit_lod_level is not None:
            obj["cgml3_implicit_lod"] = implicit_lod_level

        # Store name if exists
        try:
            name = feat.findtext(".//gml:name", namespaces=NS)
            if name and name.strip():
                obj.name = name.strip()
        except Exception:
            pass

        # Lebensdauer-Felder aus core:AbstractFeatureWithLifespan
        try:
            for tag in ("creationDate", "terminationDate", "validFrom", "validTo"):
                dtxt = feat.findtext(f".//core:{tag}", namespaces=NS)
                if dtxt and dtxt.strip():
                    obj[f"core:{tag}"] = dtxt.strip()
        except Exception:
            pass

        # Store remaining attributes (genericAttribute + feature-type-specific)
        _store_common_attributes(obj, feat)

        # Store structural element info if exists
        try:
            is_structural = feat.findtext(".//con:isStructuralElement", namespaces=NS)
            if is_structural is not None:
                obj["con:isStructuralElement"] = is_structural.lower() == "true"
        except Exception:
            pass

        try:
            # Transportation-Klasse/Funktion/Material, falls vorhanden
            tclass = feat.findtext(".//tran:class", namespaces=NS)
            tfunc  = feat.findtext(".//tran:function", namespaces=NS)
            tmat   = feat.findtext(".//tran:surfaceMaterial", namespaces=NS)
            tdir   = feat.findtext(".//tran:trafficDirection", namespaces=NS)
            tgran  = feat.findtext(".//tran:granularity", namespaces=NS)
            if tclass and tclass.strip(): obj["tran:class"] = tclass.strip()
            if tfunc  and tfunc.strip():  obj["tran:function"] = tfunc.strip()
            if tmat   and tmat.strip():   obj["tran:surfaceMaterial"] = tmat.strip()
            if tdir   and tdir.strip():   obj["tran:trafficDirection"] = tdir.strip()
            if tgran  and tgran.strip():  obj["tran:granularity"] = tgran.strip()
        except Exception:
            pass

        try:
            # LandUse-Klasse/Funktion/Nutzung, falls vorhanden
            lclass = feat.findtext(".//luse:class", namespaces=NS)
            if lclass and lclass.strip():
                obj["luse:class"] = lclass.strip()
            
            # function (kann mehrfach vorkommen)
            lfuncs = feat.findall(".//luse:function", namespaces=NS)
            if lfuncs:
                func_vals = [f.text.strip() for f in lfuncs if f.text and f.text.strip()]
                if len(func_vals) == 1:
                    obj["luse:function"] = func_vals[0]
                elif len(func_vals) > 1:
                    obj["luse:function"] = func_vals
            
            # usage (kann mehrfach vorkommen)
            lusages = feat.findall(".//luse:usage", namespaces=NS)
            if lusages:
                usage_vals = [u.text.strip() for u in lusages if u.text and u.text.strip()]
                if len(usage_vals) == 1:
                    obj["luse:usage"] = usage_vals[0]
                elif len(usage_vals) > 1:
                    obj["luse:usage"] = usage_vals
        except Exception:
            pass

        # bldg:sortKey als Custom Property mitschreiben (falls vorhanden)
        try:
            sk_txt = feat.findtext(".//bldg:sortKey", namespaces=NS)
            if sk_txt and sk_txt.strip():
                obj["bldg:sortKey"] = int(sk_txt.strip()) if sk_txt.strip().isdigit() else sk_txt.strip()
        except Exception:
            pass
            
        # Store building class if exists
        try:
            bclass = feat.findtext(".//bldg:class", namespaces=NS)
            if bclass and bclass.strip():
                obj["bldg:class"] = bclass.strip()
                
            # Store function if exists
            function = feat.findtext(".//bldg:function", namespaces=NS)
            if function and function.strip():
                obj["bldg:function"] = function.strip()
                
            # Store usage if exists
            usage = feat.findtext(".//bldg:usage", namespaces=NS)
            if usage and usage.strip():
                obj["bldg:usage"] = usage.strip()
                
            # Preserve empty roofType elements so streaming/non-streaming imports
            # keep the original attribute presence for roundtrips.
            roof_type_el = feat.find(".//bldg:roofType", namespaces=NS)
            if roof_type_el is not None:
                obj["bldg:roofType"] = (roof_type_el.text or "").strip()
                
            # Store dateOfConstruction if exists (CityGML 3.0: con:dateOfConstruction, format: YYYY-MM-DD)
            dateOfConstruction = feat.findtext(".//con:dateOfConstruction", namespaces=NS)
            if dateOfConstruction and dateOfConstruction.strip():
                obj["con:dateOfConstruction"] = dateOfConstruction.strip()
                
            # Store dateOfDemolition if exists (CityGML 3.0: con:dateOfDemolition, format: YYYY-MM-DD)
            dateOfDemolition = feat.findtext(".//con:dateOfDemolition", namespaces=NS)
            if dateOfDemolition and dateOfDemolition.strip():
                obj["con:dateOfDemolition"] = dateOfDemolition.strip()
                
            # Store storeyHeightsAboveGround if exists (gml:MeasureOrNilReasonListType)
            # This is a space-separated list with a single uom attribute
            storeyHeightsAboveGround_el = feat.find(".//bldg:storeyHeightsAboveGround", namespaces=NS)
            if storeyHeightsAboveGround_el is not None and storeyHeightsAboveGround_el.text and storeyHeightsAboveGround_el.text.strip():
                # Parse space-separated values
                values = [float(v) for v in storeyHeightsAboveGround_el.text.strip().split() if v.strip()]
                if values:
                    obj["bldg:storeyHeightsAboveGround"] = values
                    # Store uom if present
                    uom = storeyHeightsAboveGround_el.get("uom")
                    if uom:
                        obj["bldg:storeyHeightsAboveGround:uom"] = uom
                
            # Store storeyHeightsBelowGround if exists (gml:MeasureOrNilReasonListType)
            storeyHeightsBelowGround_el = feat.find(".//bldg:storeyHeightsBelowGround", namespaces=NS)
            if storeyHeightsBelowGround_el is not None and storeyHeightsBelowGround_el.text and storeyHeightsBelowGround_el.text.strip():
                # Parse space-separated values
                values = [float(v) for v in storeyHeightsBelowGround_el.text.strip().split() if v.strip()]
                if values:
                    obj["bldg:storeyHeightsBelowGround"] = values
                    # Store uom if present
                    uom = storeyHeightsBelowGround_el.get("uom")
                    if uom:
                        obj["bldg:storeyHeightsBelowGround:uom"] = uom
            
            # Store storeysAboveGround if exists
            storeysAboveGround = feat.findtext(".//bldg:storeysAboveGround", namespaces=NS)
            if storeysAboveGround and storeysAboveGround.strip():
                obj["bldg:storeysAboveGround"] = int(storeysAboveGround.strip())
            
            # Store storeysBelowGround if exists
            storeysBelowGround = feat.findtext(".//bldg:storeysBelowGround", namespaces=NS)
            if storeysBelowGround and storeysBelowGround.strip():
                obj["bldg:storeysBelowGround"] = int(storeysBelowGround.strip())
        except Exception:
            pass
        
        # Import BuildingUnit-specific attributes (CityGML 3.0)
        if feat_ln == "BuildingUnit":
            try:
                # type (e.g., apartment, office)
                unit_type = feat.findtext(".//bldg:type", namespaces=NS)
                if unit_type and unit_type.strip():
                    obj["bldg:type"] = unit_type.strip()
                
                # ownerName
                owner_name = feat.findtext(".//bldg:ownerName", namespaces=NS)
                if owner_name and owner_name.strip():
                    obj["bldg:ownerName"] = owner_name.strip()
                
                # numberOfRooms
                num_rooms = feat.findtext(".//bldg:numberOfRooms", namespaces=NS)
                if num_rooms and num_rooms.strip():
                    obj["bldg:numberOfRooms"] = int(num_rooms.strip())
                
                # numberOfBedRooms
                num_bedrooms = feat.findtext(".//bldg:numberOfBedRooms", namespaces=NS)
                if num_bedrooms and num_bedrooms.strip():
                    obj["bldg:numberOfBedRooms"] = int(num_bedrooms.strip())
                
                # numberOfBathRooms
                num_bathrooms = feat.findtext(".//bldg:numberOfBathRooms", namespaces=NS)
                if num_bathrooms and num_bathrooms.strip():
                    obj["bldg:numberOfBathRooms"] = int(num_bathrooms.strip())
            except Exception:
                pass
        
        # Import Storey-specific attributes (CityGML 3.0)
        if feat_ln == "Storey":
            try:
                # type (e.g., basement, ground floor, floor)
                storey_type = feat.findtext(".//bldg:type", namespaces=NS)
                if storey_type and storey_type.strip():
                    obj["bldg:type"] = storey_type.strip()
                
                # storeysAboveGround (index)
                index_above = feat.findtext(".//bldg:storeysAboveGround", namespaces=NS)
                if index_above and index_above.strip():
                    obj["bldg:storeysAboveGround"] = int(index_above.strip())
                
                # storeysBelowGround (index)
                index_below = feat.findtext(".//bldg:storeysBelowGround", namespaces=NS)
                if index_below and index_below.strip():
                    obj["bldg:storeysBelowGround"] = int(index_below.strip())
                
                # bldg:elevation / con:Elevation
                elev_el = feat.find("./bldg:elevation/con:Elevation", NS)
                if elev_el is not None:
                    elev_ref = elev_el.findtext("con:elevationReference", namespaces=NS)
                    if elev_ref and elev_ref.strip():
                        obj["bldg:elevation:elevationReference"] = elev_ref.strip()
                    elev_val = elev_el.findtext("con:elevationValue", namespaces=NS)
                    if elev_val and elev_val.strip():
                        obj["bldg:elevation:elevationValue"] = elev_val.strip()
            except Exception:
                pass
        
        # Import Bridge-specific attributes
        if feat_ln in ("Bridge", "BridgePart"):
            try:
                bclass = feat.findtext(".//brid:class", namespaces=NS)
                if bclass and bclass.strip():
                    obj["brid:class"] = bclass.strip()
                
                function = feat.findtext(".//brid:function", namespaces=NS)
                if function and function.strip():
                    obj["brid:function"] = function.strip()
                
                usage = feat.findtext(".//brid:usage", namespaces=NS)
                if usage and usage.strip():
                    obj["brid:usage"] = usage.strip()
                
                isMovable = feat.findtext(".//brid:isMovable", namespaces=NS)
                if isMovable and isMovable.strip():
                    obj["brid:isMovable"] = isMovable.strip().lower() == "true"
            except Exception:
                pass
        
        # Import Tunnel-specific attributes
        if feat_ln in ("Tunnel", "TunnelPart"):
            try:
                tclass = feat.findtext(".//tun:class", namespaces=NS)
                if tclass and tclass.strip():
                    obj["tun:class"] = tclass.strip()
                
                function = feat.findtext(".//tun:function", namespaces=NS)
                if function and function.strip():
                    obj["tun:function"] = function.strip()
                
                usage = feat.findtext(".//tun:usage", namespaces=NS)
                if usage and usage.strip():
                    obj["tun:usage"] = usage.strip()
            except Exception:
                pass
        
        # Import BridgeRoom-specific attributes (CityGML 3.0)
        if feat_ln == "BridgeRoom":
            try:
                bclass = feat.findtext(".//brid:class", namespaces=NS)
                if bclass and bclass.strip():
                    obj["brid:class"] = bclass.strip()
                
                function = feat.findtext(".//brid:function", namespaces=NS)
                if function and function.strip():
                    obj["brid:function"] = function.strip()
                
                usage = feat.findtext(".//brid:usage", namespaces=NS)
                if usage and usage.strip():
                    obj["brid:usage"] = usage.strip()
            except Exception:
                pass
        
        # Import BridgeFurniture-specific attributes (CityGML 3.0)
        if feat_ln == "BridgeFurniture":
            try:
                bclass = feat.findtext(".//brid:class", namespaces=NS)
                if bclass and bclass.strip():
                    obj["brid:class"] = bclass.strip()
                
                function = feat.findtext(".//brid:function", namespaces=NS)
                if function and function.strip():
                    obj["brid:function"] = function.strip()
                
                usage = feat.findtext(".//brid:usage", namespaces=NS)
                if usage and usage.strip():
                    obj["brid:usage"] = usage.strip()
            except Exception:
                pass
        
        # Import BridgeInstallation-specific attributes (CityGML 3.0)
        if feat_ln == "BridgeInstallation":
            try:
                bclass = feat.findtext(".//brid:class", namespaces=NS)
                if bclass and bclass.strip():
                    obj["brid:class"] = bclass.strip()
                
                function = feat.findtext(".//brid:function", namespaces=NS)
                if function and function.strip():
                    obj["brid:function"] = function.strip()
                
                usage = feat.findtext(".//brid:usage", namespaces=NS)
                if usage and usage.strip():
                    obj["brid:usage"] = usage.strip()
            except Exception:
                pass
        
        # Import BuildingFurniture-specific attributes (CityGML 3.0)
        if feat_ln == "BuildingFurniture":
            try:
                bclass = feat.findtext(".//bldg:class", namespaces=NS)
                if bclass and bclass.strip():
                    obj["bldg:class"] = bclass.strip()
                
                function = feat.findtext(".//bldg:function", namespaces=NS)
                if function and function.strip():
                    obj["bldg:function"] = function.strip()
                
                usage = feat.findtext(".//bldg:usage", namespaces=NS)
                if usage and usage.strip():
                    obj["bldg:usage"] = usage.strip()
            except Exception:
                pass
        
        # Import TunnelInstallation-specific attributes (CityGML 3.0)
        if feat_ln == "TunnelInstallation":
            try:
                tclass = feat.findtext(".//tun:class", namespaces=NS)
                if tclass and tclass.strip():
                    obj["tun:class"] = tclass.strip()
                
                function = feat.findtext(".//tun:function", namespaces=NS)
                if function and function.strip():
                    obj["tun:function"] = function.strip()
                
                usage = feat.findtext(".//tun:usage", namespaces=NS)
                if usage and usage.strip():
                    obj["tun:usage"] = usage.strip()
            except Exception:
                pass
        
        # Import TunnelFurniture-specific attributes (CityGML 3.0)
        if feat_ln == "TunnelFurniture":
            try:
                tclass = feat.findtext(".//tun:class", namespaces=NS)
                if tclass and tclass.strip():
                    obj["tun:class"] = tclass.strip()
                
                function = feat.findtext(".//tun:function", namespaces=NS)
                if function and function.strip():
                    obj["tun:function"] = function.strip()
                
                usage = feat.findtext(".//tun:usage", namespaces=NS)
                if usage and usage.strip():
                    obj["tun:usage"] = usage.strip()
            except Exception:
                pass
        
        # Import WaterBody-specific attributes
        if feat_ln in ("WaterBody", "WaterSurface"):
            try:
                wclass = feat.findtext(".//wtr:class", namespaces=NS)
                if wclass and wclass.strip():
                    obj["wtr:class"] = wclass.strip()
                
                function = feat.findtext(".//wtr:function", namespaces=NS)
                if function and function.strip():
                    obj["wtr:function"] = function.strip()
                
                usage = feat.findtext(".//wtr:usage", namespaces=NS)
                if usage and usage.strip():
                    obj["wtr:usage"] = usage.strip()
                
                waterLevel = feat.findtext(".//wtr:waterLevel", namespaces=NS)
                if waterLevel and waterLevel.strip():
                    obj["wtr:waterLevel"] = waterLevel.strip()
            except Exception:
                pass
        
        # Import Vegetation-specific attributes
        if feat_ln in ("SolitaryVegetationObject", "PlantCover"):
            try:
                vclass = feat.findtext(".//veg:class", namespaces=NS)
                if vclass and vclass.strip():
                    obj["veg:class"] = vclass.strip()
                
                function = feat.findtext(".//veg:function", namespaces=NS)
                if function and function.strip():
                    obj["veg:function"] = function.strip()
                
                usage = feat.findtext(".//veg:usage", namespaces=NS)
                if usage and usage.strip():
                    obj["veg:usage"] = usage.strip()
                
                species = feat.findtext(".//veg:species", namespaces=NS)
                if species and species.strip():
                    obj["veg:species"] = species.strip()
                
                # Measurements with uom
                height_el = feat.find(".//veg:height", namespaces=NS)
                if height_el is not None and height_el.text and height_el.text.strip():
                    obj["veg:height"] = float(height_el.text.strip())
                    uom = height_el.get("uom")
                    if uom:
                        obj["veg:height:uom"] = uom
                
                trunkDiameter_el = feat.find(".//veg:trunkDiameter", namespaces=NS)
                if trunkDiameter_el is not None and trunkDiameter_el.text and trunkDiameter_el.text.strip():
                    obj["veg:trunkDiameter"] = float(trunkDiameter_el.text.strip())
                    uom = trunkDiameter_el.get("uom")
                    if uom:
                        obj["veg:trunkDiameter:uom"] = uom
                
                crownDiameter_el = feat.find(".//veg:crownDiameter", namespaces=NS)
                if crownDiameter_el is not None and crownDiameter_el.text and crownDiameter_el.text.strip():
                    obj["veg:crownDiameter"] = float(crownDiameter_el.text.strip())
                    uom = crownDiameter_el.get("uom")
                    if uom:
                        obj["veg:crownDiameter:uom"] = uom
            except Exception:
                pass
        
        # Import CityFurniture-specific attributes
        if feat_ln == "CityFurniture":
            try:
                fclass = feat.findtext(".//frn:class", namespaces=NS)
                if fclass and fclass.strip():
                    obj["frn:class"] = fclass.strip()
                
                function = feat.findtext(".//frn:function", namespaces=NS)
                if function and function.strip():
                    obj["frn:function"] = function.strip()
                
                usage = feat.findtext(".//frn:usage", namespaces=NS)
                if usage and usage.strip():
                    obj["frn:usage"] = usage.strip()
            except Exception:
                pass
        
        # Import Transportation-specific attributes (Section, Intersection, Road, Railway, etc.)
        if feat_ln in ("Road", "Railway", "Track", "Square", "Waterway", "Section", "Intersection"):
            try:
                # class attribute
                tclass = feat.findtext(".//tran:class", namespaces=NS)
                if tclass and tclass.strip():
                    obj["tran:class"] = tclass.strip()
                
                # function attribute
                function = feat.findtext(".//tran:function", namespaces=NS)
                if function and function.strip():
                    obj["tran:function"] = function.strip()
                
                # usage attribute
                usage = feat.findtext(".//tran:usage", namespaces=NS)
                if usage and usage.strip():
                    obj["tran:usage"] = usage.strip()
                
                # Section-specific: predecessor/successor references
                if feat_ln == "Section":
                    # predecessorOf (list of gml:id references)
                    predecessor_refs = []
                    for pred_el in feat.findall(".//tran:predecessorOf", namespaces=NS):
                        href = pred_el.get("{http://www.w3.org/1999/xlink}href")
                        if href and href.startswith("#"):
                            predecessor_refs.append(href[1:])
                    if predecessor_refs:
                        obj["tran:predecessorOf"] = ",".join(predecessor_refs)
                    
                    # successorOf (list of gml:id references)
                    successor_refs = []
                    for succ_el in feat.findall(".//tran:successorOf", namespaces=NS):
                        href = succ_el.get("{http://www.w3.org/1999/xlink}href")
                        if href and href.startswith("#"):
                            successor_refs.append(href[1:])
                    if successor_refs:
                        obj["tran:successorOf"] = ",".join(successor_refs)
                
                # Intersection-specific: connectedSections
                if feat_ln == "Intersection":
                    section_refs = []
                    for sect_el in feat.findall(".//tran:connectedSections", namespaces=NS):
                        href = sect_el.get("{http://www.w3.org/1999/xlink}href")
                        if href and href.startswith("#"):
                            section_refs.append(href[1:])
                    if section_refs:
                        obj["tran:connectedSections"] = ",".join(section_refs)
            except Exception:
                pass
        
        # Import TrafficSpace & AuxiliaryTrafficSpace attributes
        if feat_ln in ("TrafficSpace", "AuxiliaryTrafficSpace"):
            try:
                # granularity (lane, way, etc.)
                granularity = feat.findtext(".//tran:granularity", namespaces=NS)
                if granularity and granularity.strip():
                    obj["tran:granularity"] = granularity.strip()
                
                # trafficDirection
                traffic_dir = feat.findtext(".//tran:trafficDirection", namespaces=NS)
                if traffic_dir and traffic_dir.strip():
                    obj["tran:trafficDirection"] = traffic_dir.strip()
                
                # surfaceMaterial (code list)
                surf_material = feat.findtext(".//tran:surfaceMaterial", namespaces=NS)
                if surf_material and surf_material.strip():
                    obj["tran:surfaceMaterial"] = surf_material.strip()
            except Exception:
                pass
        
        # Import BuildingUnit-specific attributes
        if feat_ln == "BuildingUnit":
            try:
                # BuildingUnit inherits class/function/usage from AbstractBuildingSubdivision
                # (already parsed above in bldg:class/function/usage block)
                
                # BuildingUnit-specific: storey references (gml:id references to Storey elements)
                storey_refs = feat.findall(".//bldg:storey", namespaces=NS)
                if storey_refs:
                    storey_ids = []
                    for storey_ref in storey_refs:
                        # Extract xlink:href reference
                        href = storey_ref.get("{http://www.w3.org/1999/xlink}href")
                        if href:
                            # Remove leading # from internal reference
                            storey_id = href.lstrip("#")
                            storey_ids.append(storey_id)
                    if storey_ids:
                        obj["bldg:storey_refs"] = ",".join(storey_ids)
                
                # Address structure is already parsed in the generic bldg:address block above
                # but we can add BuildingUnit-specific address handling if needed
                
            except Exception:
                pass
        
        # Import BuildingRoom-specific attributes
        if feat_ln == "BuildingRoom":
            try:
                # BuildingRoom inherits class/function/usage from AbstractUnoccupiedSpace
                # (already parsed in the generic bldg:class/function/usage block)
                
                # BuildingRoom-specific: roomHeight (qualified height measurements)
                room_heights = feat.findall(".//bldg:roomHeight/bldg:RoomHeight", namespaces=NS)
                if room_heights:
                    room_height_list = []
                    for rh in room_heights:
                        rh_data = {}
                        
                        # Parse lowReference
                        low_ref = rh.findtext(".//bldg:lowReference", namespaces=NS)
                        if low_ref and low_ref.strip():
                            rh_data["lowReference"] = low_ref.strip()
                        
                        # Parse highReference
                        high_ref = rh.findtext(".//bldg:highReference", namespaces=NS)
                        if high_ref and high_ref.strip():
                            rh_data["highReference"] = high_ref.strip()
                        
                        # Parse status
                        status = rh.findtext(".//bldg:status", namespaces=NS)
                        if status and status.strip():
                            rh_data["status"] = status.strip()
                        
                        # Parse value with uom
                        value_el = rh.find(".//bldg:value", namespaces=NS)
                        if value_el is not None and value_el.text and value_el.text.strip():
                            rh_data["value"] = float(value_el.text.strip())
                            uom = value_el.get("uom")
                            if uom:
                                rh_data["uom"] = uom
                        
                        if rh_data:
                            room_height_list.append(rh_data)
                    
                    if room_height_list:
                        # Store as JSON string for simplicity
                        import json
                        obj["bldg:roomHeight"] = json.dumps(room_height_list)
                
                # buildingFurniture and buildingInstallation should be parsed as child features
                # (handled by the generic nested feature parsing already present)
                
            except Exception:
                pass
        
        # Import Storey-specific attributes
        if feat_ln == "Storey":
            try:
                # Storey inherits class/function/usage/elevation/sortKey from AbstractBuildingSubdivision
                # (already parsed in the generic blocks)
                
                # Storey-specific: buildingUnit references
                unit_refs = feat.findall(".//bldg:buildingUnit", namespaces=NS)
                if unit_refs:
                    unit_ids = []
                    for unit_ref in unit_refs:
                        # Extract xlink:href reference
                        href = unit_ref.get("{http://www.w3.org/1999/xlink}href")
                        if href:
                            # Remove leading # from internal reference
                            unit_id = href.lstrip("#")
                            unit_ids.append(unit_id)
                    if unit_ids:
                        obj["bldg:buildingUnit_refs"] = ",".join(unit_ids)
                
                # sortKey (from AbstractBuildingSubdivision) is particularly important for Storey ordering
                # Already handled in the generic parsing above
                
            except Exception:
                pass
        
        # Import Window-specific attributes
        if feat_ln == "Window":
            try:
                # Window inherits from AbstractFillingElement → AbstractOccupiedSpace
                # Common attributes: class, function, usage (already parsed in generic blocks)
                
                # Window-specific: No additional attributes beyond class/function/usage
                # Geometry is handled through lod*MultiSurface (standard parsing)
                # WindowSurface elements are parsed as child surfaces
                pass
                
            except Exception:
                pass
        
        # Import Door-specific attributes
        if feat_ln == "Door":
            try:
                # Door inherits from AbstractFillingElement → AbstractOccupiedSpace
                # Common attributes: class, function, usage (already parsed in generic blocks)
                
                # Door-specific: address (can have multiple addresses)
                # Note: Address parsing is already handled generically in the bldg:address block above
                # The Door.address uses the same core:AddressPropertyType structure
                
                # Additional door-specific address handling (if needed beyond generic parsing)
                # would go here, but the standard address parsing should suffice
                pass
                
            except Exception:
                pass
        
        # Import Relief-specific attributes (MassPointRelief, BreaklineRelief, RasterRelief)
        # All relief components inherit from AbstractReliefComponent (lod, extent)
        if feat_ln in ("MassPointRelief", "BreaklineRelief", "RasterRelief", "TINRelief"):
            try:
                # AbstractReliefComponent attributes
                
                # dem:lod (required for all relief components)
                lod_el = feat.findtext(".//dem:lod", namespaces=NS)
                if lod_el and lod_el.strip():
                    obj["dem:lod"] = int(lod_el.strip())
                
                # dem:extent (optional 2D surface geometry)
                # Store extent gml:id if present
                extent_el = feat.find(".//dem:extent", namespaces=NS)
                if extent_el is not None:
                    # Look for Polygon or other surface geometry
                    polygon = extent_el.find(".//gml:Polygon", namespaces=NS)
                    if polygon is not None:
                        polygon_id = polygon.get("{http://www.opengis.net/gml/3.2}id")
                        if polygon_id:
                            obj["dem:extent_id"] = polygon_id
                
                # MassPointRelief-specific: reliefPoints (MultiPoint geometry)
                # Parse dem:reliefPoints/gml:MultiPoint explicitly
                if feat_ln == "MassPointRelief":
                    relief_points_el = feat.find(".//dem:reliefPoints/gml:MultiPoint", namespaces=NS)
                    if relief_points_el is not None:
                        multipoint_id = relief_points_el.get("{http://www.opengis.net/gml/3.2}id")
                        if multipoint_id:
                            obj["dem:reliefPoints_id"] = multipoint_id
                        # Points will be parsed by generic point parser
                        # Mark for special handling
                        obj["dem:uses_reliefPoints"] = True
                
                # BreaklineRelief-specific: ridgeOrValleyLines and breaklines (MultiCurve geometry)
                # Parse dem:ridgeOrValleyLines and dem:breaklines explicitly
                if feat_ln == "BreaklineRelief":
                    # Check for ridgeOrValleyLines
                    ridge_el = feat.find(".//dem:ridgeOrValleyLines", namespaces=NS)
                    if ridge_el is not None:
                        obj["dem:hasRidgeOrValleyLines"] = True
                        # Get MultiCurve ID if present
                        multicurve = ridge_el.find(".//gml:MultiCurve", namespaces=NS)
                        if multicurve is not None:
                            mc_id = multicurve.get("{http://www.opengis.net/gml/3.2}id")
                            if mc_id:
                                obj["dem:ridgeOrValleyLines_id"] = mc_id
                    
                    # Check for breaklines
                    breaklines_el = feat.find(".//dem:breaklines", namespaces=NS)
                    if breaklines_el is not None:
                        obj["dem:hasBreaklines"] = True
                        # Get MultiCurve ID if present
                        multicurve = breaklines_el.find(".//gml:MultiCurve", namespaces=NS)
                        if multicurve is not None:
                            mc_id = multicurve.get("{http://www.opengis.net/gml/3.2}id")
                            if mc_id:
                                obj["dem:breaklines_id"] = mc_id
                
                # RasterRelief-specific: grid (RectifiedGridCoverage)
                if feat_ln == "RasterRelief":
                    # Parse full RectifiedGridCoverage
                    grid_el = feat.find(".//dem:grid/gml:RectifiedGridCoverage", namespaces=NS)
                    if grid_el is not None:
                        try:
                            grid_data = parse_rectified_grid_coverage(grid_el, default_srs, ref_origin)
                            if grid_data:
                                # Store grid metadata in custom properties
                                obj["dem:grid_id"] = grid_data.get('grid_id', '')
                                
                                dims = grid_data.get('dimensions', {})
                                obj["dem:grid_rows"] = dims.get('rows', 0)
                                obj["dem:grid_cols"] = dims.get('cols', 0)
                                
                                origin = grid_data.get('origin', (0, 0, 0))
                                obj["dem:grid_origin_x"] = origin[0] + ref_origin[0]
                                obj["dem:grid_origin_y"] = origin[1] + ref_origin[1]
                                obj["dem:grid_origin_z"] = origin[2] + ref_origin[2]
                                
                                offsets = grid_data.get('offsets', {})
                                offset_x = offsets.get('offset_x', (1, 0, 0))
                                offset_y = offsets.get('offset_y', (0, 1, 0))
                                obj["dem:grid_offset_x"] = f"{offset_x[0]} {offset_x[1]} {offset_x[2]}"
                                obj["dem:grid_offset_y"] = f"{offset_y[0]} {offset_y[1]} {offset_y[2]}"
                                
                                # Mark that we have grid data for later mesh creation
                                obj["dem:has_grid_data"] = True
                                obj["dem:grid_data"] = grid_data  # Store for mesh creation
                        except Exception as e:
                            # Fallback: store basic metadata only
                            grid_id = grid_el.get("{http://www.opengis.net/gml/3.2}id")
                            if grid_id:
                                obj["dem:grid_id"] = grid_id
                            
                            limits = grid_el.find(".//gml:limits/gml:GridEnvelope", namespaces=NS)
                            if limits is not None:
                                low = limits.findtext(".//gml:low", namespaces=NS)
                                high = limits.findtext(".//gml:high", namespaces=NS)
                                if low and high:
                                    obj["dem:grid_low"] = low.strip()
                                    obj["dem:grid_high"] = high.strip()
                
            except Exception:
                pass
        
        # Import con:height structure
        try:
            height_el = feat.find(".//con:height/con:Height", namespaces=NS)
            if height_el is not None:
                obj["con:height"] = ""

                high_ref_el = height_el.find("./con:highReference", namespaces=NS)
                if high_ref_el is not None:
                    obj["con:height:highReference"] = (high_ref_el.text or "").strip()
                
                low_ref_el = height_el.find("./con:lowReference", namespaces=NS)
                if low_ref_el is not None:
                    obj["con:height:lowReference"] = (low_ref_el.text or "").strip()
                
                status_el = height_el.find("./con:status", namespaces=NS)
                if status_el is not None:
                    obj["con:height:status"] = (status_el.text or "").strip()
                
                value_el = height_el.find("./con:value", namespaces=NS)
                if value_el is not None:
                    value_text = (value_el.text or "").strip()
                    if value_text:
                        obj["con:height:value"] = float(value_text)
                    else:
                        obj["con:height:value"] = ""
                    uom = value_el.get("uom")
                    if uom is not None:
                        obj["con:height:uom"] = uom
        except Exception:
            pass
        
        # Import bldg:address structure with full xAL parsing
        try:
            address_el = feat.find(".//bldg:address/core:Address", namespaces=NS)
            if address_el is not None:
                # xAL Address structure - use comprehensive xAL parser
                xal_addr = address_el.find(".//core:xalAddress/xAL:Address", namespaces=NS)
                if xal_addr is not None:
                    from ...shared.xal_parser import parse_xal_address
                    
                    # Parse xAL 3.0 (CityGML 3.0)
                    xal_data = parse_xal_address(xal_addr, NS, version="3.0")
                    
                    # Store all xAL fields with bldg:address: prefix
                    for key, value in xal_data.items():
                        if value:  # Only store non-empty values
                            obj[f"bldg:address:{key}"] = value
                    
                    # LEGACY: Keep old format for backwards compatibility
                    if "country" in xal_data:
                        obj["bldg:address:country"] = xal_data["country"]
                    if "locality" in xal_data:
                        obj["bldg:address:locality"] = xal_data["locality"]
                    if "street" in xal_data:
                        obj["bldg:address:street"] = xal_data["street"]
                    if "streetNumber" in xal_data:
                        obj["bldg:address:streetNumber"] = xal_data["streetNumber"]
                    if "postCode" in xal_data:
                        obj["bldg:address:postCode"] = xal_data["postCode"]
                
                # MultiPoint
                multipoint_el = address_el.find(".//core:multiPoint/gml:MultiPoint", namespaces=NS)
                if multipoint_el is not None:
                    pos_text = multipoint_el.findtext(".//gml:Point/gml:pos", namespaces=NS)
                    if pos_text and pos_text.strip():
                        coords = pos_text.strip().split()
                        if len(coords) >= 2:
                            obj["bldg:address:point:x"] = float(coords[0])
                            obj["bldg:address:point:y"] = float(coords[1])
                            obj["bldg:address:point:z"] = float(coords[2]) if len(coords) >= 3 else 0.0
        except Exception as e:
            pass

        # generische Attribute in Custom Properties schreiben
        gen_attrs = _parse_generic_attributes(feat)
        for k, v in gen_attrs.items():
            try:
                obj[k] = v
            except Exception:
                obj[k] = str(v)
        
        # Restore mesh reference for geometry operations (implicit geometry, etc.)
        if _is_hierarchical_building:
            obj = _mesh_obj

        # falls dieses Feature über eine ImplicitGeometry kommt:
        # - für spätere Instanzierung vormerken
        # - Mesh als ImplicitGeometry markieren (auch bei nur EINEM Objekt)
        # tpl_id / M_eff wurden bereits vor build_geometry berechnet (siehe oben)
        if is_implicit_instance:
            implicit_groups.setdefault(tpl_id, []).append((feat_id, obj, M_eff))
            try:
                mesh["ImplicitGeometry"] = True
            except Exception:
                mesh["ImplicitGeometry"] = str(tpl_id)

            # Persist the per-feature implicit transform for CityGML3 export:
            # Store row-major 4x4 transformationMatrix values with translation cleared
            # (translation is represented via referencePoint in CityGML implicit geometries).
            try:
                Mw = M_eff.copy()
                Mw[0][3] = 0.0
                Mw[1][3] = 0.0
                Mw[2][3] = 0.0
                vals = []
                for r in range(4):
                    for c in range(4):
                        vals.append(float(Mw[r][c]))
                obj["cgml3_transformationMatrix"] = " ".join(str(v) for v in vals)
            except Exception:
                pass

        # für CityFurniture das Mesh als ImplicitGeometry kennzeichnen,
        # auch wenn _implicit_instance_info wegen strenger Bedingungen (z.B. lodXGeometry)
        # kein tpl_id zurückliefert.
        if feat_ln == "CityFurniture" and _feature_has_implicit_geometry(feat):
            try:
                mesh["ImplicitGeometry"] = True
            except Exception:
                mesh["ImplicitGeometry"] = "True"

    # Implizite Geometrien: viele Features → ein Mesh + Instanzen
    if implicit_groups:
        for template_id, entries in implicit_groups.items():
            if len(entries) <= 1:
                # nur eine Instanz → kein Vorteil
                continue

            # Erstes Objekt als Basis-Mesh verwenden
            base_feat_id, base_obj, base_Meff = entries[0]
            base_mesh = base_obj.data

            # Basis-Objekt ebenfalls korrekt positionieren
            try:
                base_obj.matrix_world = _implicit_world_matrix_from_citygml(
                    base_Meff, ref_origin, base_obj.get("CRS", None)
                )
            except Exception:
                pass

            # Mesh-Datenblock als aus ImplicitGeometry stammend markieren
            try:
                base_mesh["ImplicitGeometry"] = True
            except Exception:
                # Fallback, falls aus irgendeinem Grund kein bool erlaubt ist
                base_mesh["ImplicitGeometry"] = str(template_id)

            # optional kennzeichnen
            try:
                base_obj["cgml3_implicit_template_id"] = template_id
            except Exception:
                pass

            for feat_id, obj, M_eff in entries[1:]:
                # Objekt-Transformation direkt setzen: Template-Mesh bleibt lokal,
                # jede Instanz trägt ihre eigene Weltmatrix (inkl. individueller Skalierung).
                old_mesh = obj.data

                # alle Klone teilen sich das Basis-Mesh
                obj.data = base_mesh
                try:
                    obj.matrix_world = _implicit_world_matrix_from_citygml(
                        M_eff, ref_origin, obj.get("CRS", None)
                    )
                except Exception:
                    pass

                # Persist per-instance transform for CityGML3 export:
                # Store row-major 4x4 transformationMatrix values with translation cleared
                # (translation is represented via referencePoint in CityGML implicit geometries).
                try:
                    Tw = M_eff.copy()
                    Tw[0][3] = 0.0
                    Tw[1][3] = 0.0
                    Tw[2][3] = 0.0
                    vals = []
                    for r in range(4):
                        for c in range(4):
                            vals.append(float(Tw[r][c]))
                    obj["cgml3_transformationMatrix"] = " ".join(str(v) for v in vals)
                except Exception:
                    pass

                # nicht mehr benutzte Mesh-Datenblöcke aufräumen
                try:
                    if old_mesh and old_mesh.users == 0:
                        bpy.data.meshes.remove(old_mesh)
                except Exception:
                    pass

    # Nachträglich Part-Parenting herstellen (CityGML 3.0)
    # - Keine Geometrie-Duplikate: es werden nur Parent-Links gesetzt.
    # - Hierarchie bleibt erhalten für Export (buildingPart/bridgePart/tunnelPart).
    def _apply_part_parenting(edges):
        for parent_id, part_id in edges:
            parent_obj = id_to_obj.get(parent_id)
            part_obj = id_to_obj.get(part_id)
            if parent_obj is None or part_obj is None:
                continue
            if parent_obj is part_obj:
                continue
            try:
                part_obj.parent = parent_obj
            except Exception:
                pass
            try:
                part_obj["cgml_parent_id"] = parent_id
            except Exception:
                pass

    if building_part_edges:
        _apply_part_parenting(building_part_edges)
    if building_installation_edges:
        _apply_part_parenting(building_installation_edges)
    if building_furniture_edges:
        _apply_part_parenting(building_furniture_edges)
    if building_storey_edges:
        _apply_part_parenting(building_storey_edges)
    if building_room_edges:
        _apply_part_parenting(building_room_edges)
    if bridge_part_edges:
        _apply_part_parenting(bridge_part_edges)
    if bridge_installation_edges:
        _apply_part_parenting(bridge_installation_edges)
    if bridge_furniture_edges:
        _apply_part_parenting(bridge_furniture_edges)
    if tunnel_part_edges:
        _apply_part_parenting(tunnel_part_edges)
    if tunnel_installation_edges:
        _apply_part_parenting(tunnel_installation_edges)
    if tunnel_furniture_edges:
        _apply_part_parenting(tunnel_furniture_edges)

    outer_shell_updates = 0
    for imported_obj in id_to_obj.values():
        if getattr(imported_obj, "type", None) != "EMPTY":
            continue
        outer_shell_updates += _sync_outer_shell_surface_semantics(imported_obj)
    if outer_shell_updates:
        print(f"[CityGML3] Synchronized OuterShell surface semantics on {outer_shell_updates} materials")

    _disable_inline_subfeature_collections(coll)

    # Nachträglich Gruppen-Hierarchien in Blender herstellen
    # member_roles ist jetzt: [(member_id, role_string), ...]
    for grp_obj, member_roles, parent_id in group_defs:
        # 1) Parent der Gruppe setzen (falls eine übergeordnete Gruppe oder ein Objekt referenziert ist)
        if parent_id:
            parent_obj = id_to_obj.get(parent_id)
            if parent_obj is not None and parent_obj is not grp_obj:
                try:
                    grp_obj.parent = parent_obj
                except Exception:
                    pass

        # 2) Eigene Collection für die Gruppe anlegen (unterhalb der Import-Collection)
        #    Name z.B. "CityGML3:<Dateiname>::Group:<gml_id>"
        coll_name = f"{coll.name}::Group::{grp_obj.get('gml_id', grp_obj.name)}"
        grp_coll = bpy.data.collections.get(coll_name)
        if not grp_coll:
            grp_coll = bpy.data.collections.new(coll_name)
            # als Unter-Collection der Import-Collection einhängen
            if grp_coll not in coll.children:
                coll.children.link(grp_coll)

        # Group-Empty in die Gruppen-Collection verlinken (Mehrfach-Collections sind erlaubt)
        if grp_obj.name not in grp_coll.objects:
            grp_coll.objects.link(grp_obj)

        # 3) Mitglieder zuordnen: parent = Gruppe, zusätzlich in Gruppen-Collection aufnehmen
        # Rollen-Mapping speichern: grp:role_<member_id> = role_string
        for mid, role_str in member_roles:
            memb_obj = id_to_obj.get(mid)
            if memb_obj is None or memb_obj is grp_obj:
                continue

            # Parent-Beziehung (Member → Group-Empty)
            try:
                memb_obj.parent = grp_obj
            except Exception:
                pass

            # Mitglied in Gruppen-Collection aufnehmen (zusätzlich zu bestehender Import-Collection)
            if memb_obj.name not in grp_coll.objects:
                grp_coll.objects.link(memb_obj)
            
            # Rolle als Custom Property speichern (CityGML 3.0)
            if role_str:
                try:
                    # Auf dem Group-Objekt: grp:role_<member_id> = "role_string"
                    grp_obj[f"grp:role_{mid}"] = role_str
                    # Auch auf dem Member-Objekt für schnellen Zugriff
                    memb_obj["grp:role_in_group"] = role_str
                except Exception:
                    pass

    # Versionen als Collections + Rückverweise auf Versionen
    for ver_obj, member_ids in version_defs:
        ver_id = ver_obj.get("gml_id", ver_obj.name)

        # eigene Collection für die Version
        coll_name = f"{coll.name}::Version::{ver_id}"
        ver_coll = bpy.data.collections.get(coll_name)
        if not ver_coll:
            ver_coll = bpy.data.collections.new(coll_name)
            if ver_coll not in coll.children:
                coll.children.link(ver_coll)

        # Version-Empty in die Versions-Collection
        if ver_obj.name not in ver_coll.objects:
            ver_coll.objects.link(ver_obj)

        # Mitglieder der Version zuordnen (nur Collection, kein Parenting)
        for mid in member_ids:
            memb_obj = id_to_obj.get(mid)
            if memb_obj is None or memb_obj is ver_obj:
                continue

            # in Versions-Collection aufnehmen
            if memb_obj.name not in ver_coll.objects:
                ver_coll.objects.link(memb_obj)

            # Rückverweis am Mitglied: Liste aller Versionen
            try:
                existing = list(memb_obj.get("vers:version_ids", []))
            except Exception:
                existing = []
            if ver_id not in existing:
                existing.append(ver_id)
                memb_obj["vers:version_ids"] = existing

    # Transitionen an den beteiligten Versionen registrieren
    for trans_obj, from_id, to_id in transition_defs:
        trans_id = trans_obj.get("gml_id", trans_obj.name)

        if from_id:
            from_obj = id_to_obj.get(from_id)
            if from_obj is not None and from_obj is not trans_obj:
                try:
                    out_list = list(from_obj.get("vers:transitions_out", []))
                except Exception:
                    out_list = []
                if trans_id not in out_list:
                    out_list.append(trans_id)
                    from_obj["vers:transitions_out"] = out_list

        if to_id:
            to_obj = id_to_obj.get(to_id)
            if to_obj is not None and to_obj is not trans_obj:
                try:
                    in_list = list(to_obj.get("vers:transitions_in", []))
                except Exception:
                    in_list = []
                if trans_id not in in_list:
                    in_list.append(trans_id)
                    to_obj["vers:transitions_in"] = in_list
                
    # Dynamizer-Owner-Verknüpfungen herstellen
    for dyn_obj, owner_id in dynamizer_defs:
        if not owner_id:
            continue
        owner_obj = id_to_obj.get(owner_id)
        if owner_obj is None or owner_obj is dyn_obj:
            continue

        # Parent-Beziehung (Dynamizer → Owner-Objekt)
        try:
            dyn_obj.parent = owner_obj
        except Exception:
            pass

        # Liste aller Dynamizer am Owner pflegen
        try:
            existing = list(owner_obj.get("dyn:dynamizer_ids", []))
        except Exception:
            existing = []
        dyn_id = dyn_obj.get("gml_id", dyn_obj.name)
        if dyn_id not in existing:
            existing.append(dyn_id)
        owner_obj["dyn:dynamizer_ids"] = existing
    
    # Update import statistics
    if not isinstance(context, dict) and hasattr(context.scene, 'cgml3'):
        context.scene.cgml3.last_import_total = total_features_before_filter
        context.scene.cgml3.last_import_imported = total_feats
        context.scene.cgml3.last_import_filtered = features_filtered
    
    # Finalize progress
    if wm:
        wm.progress_update(100)
        wm.progress_end()
    print_console_progress("Import abgeschlossen", percentage=100)
    print()  # New line after progress bar
    
    # Log import time
    elapsed_time = time.time() - start_time
    print(f"[CityGML Import] Completed in {elapsed_time:.2f} seconds ({elapsed_time/60:.1f} minutes)")


def _import_citygml3_streaming(path: str, context, wm=None, *, import_appearance: bool = True):
    import time
    import sys
    start_time = time.time()
    
    def print_console_progress(phase: str, current: int = 0, total: int = 0, percentage: float = 0):
        """Print progress bar to PowerShell console."""
        bar_length = 40
        if total > 0:
            filled = int(bar_length * current / total)
            percentage = 100 * current / total
        else:
            filled = int(bar_length * percentage / 100)
        
        bar = '█' * filled + '░' * (bar_length - filled)
        if total > 0:
            info = f"{current}/{total}"
        else:
            info = ""
        
        # Pad with spaces to clear previous longer messages (80 chars total line length)
        line = f'\r[{bar}] {percentage:5.1f}% | {phase} {info}'
        sys.stdout.write(line + ' ' * max(0, 100 - len(line)))
        sys.stdout.flush()
    
    """
    Streaming import for large CityGML 3.0 files using iterparse.
    
    Processes features incrementally to minimize memory usage.
    Suitable for files >100 MB where full XML tree loading would be prohibitive.
    """
    from pathlib import Path
    
    # Initial progress
    print_console_progress("Initialisierung...", percentage=0)
    from .streaming_import import iter_features_streaming, get_root_attributes_streaming
    from .geometry import build_geometry
    from .appearance_streaming import parse_appearance_streaming
    from .materials import apply_materials_uvs
    from .parallel_geometry import batch_process_geometries, ImportErrorTracker
    from .memory_manager import (
        LRUGeometryCache, MemoryMonitor, auto_tune_batch_size,
        cleanup_xml_element, trigger_garbage_collection, get_memory_stats_summary
    )
    from .parallel_appearance import (
        ParallelAppearanceProcessor, collect_texture_paths, get_parallel_stats_summary
    )
    
    # Get root attributes without loading full tree
    print_console_progress("Root-Attribute laden...", percentage=5)
    root_attrs = get_root_attributes_streaming(path)
    default_srs = root_attrs.get('srsName')
    env_srs = root_attrs.get('envelope_srs')
    lower_corner_str = root_attrs.get('lower_corner')
    
    # Parse lower corner
    lower_corner = None
    if lower_corner_str:
        try:
            lower_corner = [float(x) for x in lower_corner_str.split()]
        except Exception:
            pass
    
    # Setup World CRS
    _is_local_import = False
    try:
        _sc = context.scene if not isinstance(context, dict) else bpy.context.scene
        _is_local_import = bool(getattr(_sc.cgml3, "import_local", False))
    except Exception:
        pass

    if not _is_local_import:
        try:
            w = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
            z_epsg = _extract_vertical_epsg(env_srs or default_srs or "")
            if z_epsg:
                w["Z-EPSG"] = z_epsg
            if lower_corner and len(lower_corner) == 3:
                _set_world_prop_with_ui(w, "Z-Origin", float(lower_corner[2]))
        except Exception:
            pass
    
    if wm:
        wm.progress_update(10)  # 10%: Root attributes parsed
    
    # Get filters
    gml_id_filter = None
    if context.scene.cgml3.use_gmlid_filter:
        filter_str = context.scene.cgml3.gmlid_filter.strip()
        if filter_str.startswith('[') and filter_str.endswith(']'):
            filter_str = filter_str[1:-1]
        gml_id_filter = _parse_gml_id_filter(filter_str)
    
    bbox_coords = resolve_scene_bbox_filter(context.scene.cgml3)
    
    # LOD filter
    lod_filter = None
    if context.scene.cgml3.use_lod_filter:
        lod_filter = set()
        if context.scene.cgml3.lod_0:
            lod_filter.add(0)
        if context.scene.cgml3.lod_1:
            lod_filter.add(1)
        if context.scene.cgml3.lod_2:
            lod_filter.add(2)
        if context.scene.cgml3.lod_3:
            lod_filter.add(3)
        if context.scene.cgml3.lod_4:
            lod_filter.add(4)
    
    # Feature type filter
    feature_type_filter = None
    if context.scene.cgml3.use_feature_type_filter:
        feature_type_filter = set()
        if context.scene.cgml3.import_buildings:
            feature_type_filter.update(["Building", "BuildingPart", "Storey", "BuildingRoom", "BuildingInstallation"])
        if context.scene.cgml3.import_bridges:
            feature_type_filter.update(["Bridge", "BridgePart"])
        if context.scene.cgml3.import_tunnels:
            feature_type_filter.update(["Tunnel", "TunnelPart"])
        if context.scene.cgml3.import_vegetation:
            feature_type_filter.update(["SolitaryVegetationObject", "PlantCover"])
        if context.scene.cgml3.import_water:
            feature_type_filter.update(["WaterBody", "WaterSurface"])
        if context.scene.cgml3.import_transportation:
            feature_type_filter.update(["Road", "Railway", "Track", "Square", "Intersection"])
        if context.scene.cgml3.import_cityfurniture:
            feature_type_filter.add("CityFurniture")
        if context.scene.cgml3.import_landuse:
            feature_type_filter.add("LandUse")
        if context.scene.cgml3.import_relief:
            feature_type_filter.update(["ReliefFeature", "TINRelief", "MassPointRelief", "BreaklineRelief", "RasterRelief"])
        if context.scene.cgml3.import_generics:
            # CityGML 3.0: "GenericCityObject" is CityGML 2.0 terminology.
            # In CityGML 3.0, generic features are represented as Generic*Space and
            # GenericThematicSurface. Keep the UI label but map to the actual 3.0 tags.
            feature_type_filter.update([
                "GenericOccupiedSpace",
                "GenericLogicalSpace",
                "GenericUnoccupiedSpace",
                "GenericThematicSurface",
            ])
    
    # Setup collections
    coll = ensure_collection(context, f"CityGML3:{Path(path).stem}")
    type_collections = {}
    
    def get_type_collection(feat_ln: str):
        name = feat_ln or "UnknownType"
        sub = type_collections.get(name)
        if sub is not None:
            _ensure_inline_part_collection_visible(sub)
            return sub
        for c in coll.children:
            if c.name == name:
                sub = c
                break
        if sub is None:
            sub = bpy.data.collections.new(name)
            coll.children.link(sub)
        _ensure_inline_part_collection_visible(sub)
        type_collections[name] = sub
        return sub
    
    # World setup
    if isinstance(context, dict):
        world = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
        bpy.context.scene.world = world
    else:
        world = context.scene.world or bpy.data.worlds.new("World")
        context.scene.world = world
    
    # Use envelope_srs as fallback if root CityModel has no srsName attribute
    scene_georef_copied = False
    if not _is_local_import:
        scene_georef_copied = _copy_scene_georeference_to_world(context, world)
    file_origin, file_crs = _determine_file_origin_and_crs_from_attrs(default_srs or env_srs, lower_corner)
    if scene_georef_copied:
        try:
            if not _crs_matches(str(world.get("CRS", "")), file_crs):
                scene_georef_copied = False
        except Exception:
            scene_georef_copied = False
    if not _is_local_import:
        if scene_georef_copied and "Z-Origin" not in world.keys():
            _set_world_prop_with_ui(world, "Z-Origin", file_origin[2])
        if (not scene_georef_copied) and (("CRS" not in world.keys()) or world["CRS"] == "Unknown CRS" or not _crs_matches(world["CRS"], file_crs)):
            # Also check Scene["SRID"] before overwriting
            sc = context.scene if not isinstance(context, dict) else bpy.context.scene
            scene_crs = str(sc.get("SRID", "")).strip() if sc else ""
            if scene_crs and _crs_matches(scene_crs, file_crs):
                world["CRS"] = file_crs
                _set_world_prop_with_ui(world, "X-Origin", float(sc.get("crs x", file_origin[0])))
                _set_world_prop_with_ui(world, "Y-Origin", float(sc.get("crs y", file_origin[1])))
                _set_world_prop_with_ui(world, "Z-Origin", float(sc.get("crs z", file_origin[2])))
            else:
                world["CRS"] = file_crs
                _set_world_prop_with_ui(world, "X-Origin", file_origin[0])
                _set_world_prop_with_ui(world, "Y-Origin", file_origin[1])
                _set_world_prop_with_ui(world, "Z-Origin", file_origin[2])
    
    if _is_local_import:
        # Lokaler Import: Envelope lower corner als Offset, kein CRS in World
        if lower_corner and len(lower_corner) >= 2:
            ref_origin = (float(lower_corner[0]), float(lower_corner[1]),
                         float(lower_corner[2]) if len(lower_corner) == 3 else 0.0)
            print(f"[CityGML Import] Lokaler Import aktiv - Offset von Envelope: {ref_origin}")
        else:
            ref_origin = file_origin
            print(f"[CityGML Import] Lokaler Import aktiv - Offset von file_origin: {ref_origin}")
    else:
        ref_origin = (float(world["X-Origin"]), float(world["Y-Origin"]), float(world.get("Z-Origin", file_origin[2])))
    
    # Parse appearance data using streaming (memory-efficient for large files)
    appearance_processor = None
    if import_appearance:
        print_console_progress("Appearance-Daten parsen...", percentage=10)
        if wm:
            wm.progress_update(10)  # 10%: Parsing appearance data

        x3d_by_poly, ptex_by_ring, poly_to_image, ptex_by_poly = parse_appearance_streaming(path, path)
    else:
        if wm:
            wm.progress_update(10)
        x3d_by_poly = {}
        ptex_by_ring = {}
        poly_to_image = {}
        ptex_by_poly = {}
        print_console_progress("Appearance-Import übersprungen", percentage=10)
        print()
    
    # Preload textures in parallel for better performance
    # OPTIMIZED: Nur wenn Texturen tatsächlich vorhanden
    if import_appearance:
        texture_paths = collect_texture_paths(ptex_by_ring, ptex_by_poly, poly_to_image)
        if texture_paths:
            print_console_progress(f"Texturen vorladen ({len(texture_paths)})...", percentage=15)
            appearance_processor = ParallelAppearanceProcessor(max_workers=4)
            appearance_processor.preload_textures(texture_paths, path)
            print()  # Newline after progress bar
            print(f"Preloaded {len(texture_paths)} unique textures")
        else:
            print()  # Newline after progress bar
            print("No textures found, skipping texture preloading")
    
    print_console_progress("Setup abgeschlossen, starte Streaming...", percentage=20)
    if wm:
        wm.progress_update(20)  # 20%: Setup complete, starting streaming
    
    # Optional: pre-scan expected cityObjectMember IDs for diagnostics.
    diag_expected_ids = set()
    diag_expected_types = {}
    if _IMPORT_DIAG:
        try:
            # Parse root element for diagnostics (reuse from dynamic namespace detection if available)
            if 'root' not in locals():
                try:
                    from lxml import etree as ET
                except ImportError:
                    import xml.etree.ElementTree as ET
                parser = ET.XMLParser(huge_tree=True) if hasattr(ET, 'XMLParser') else None
                if parser:
                    tree = ET.parse(path, parser=parser)
                else:
                    tree = ET.parse(path)
                root = tree.getroot()
            
            gml_id_attr = f"{{{NS['gml']}}}id"
            # match both prefixed and default-ns variants
            members = (
                root.findall(".//core:cityObjectMember", NS)
                + root.findall(".//cityObjectMember", NS)
            )
            for mem in members:
                for child in list(mem):
                    if not isinstance(child.tag, str):
                        continue
                    gid = (child.get(gml_id_attr) or "").strip()
                    if not gid:
                        continue
                    diag_expected_ids.add(gid)
                    diag_expected_types[gid] = localname(child.tag)
            print(f"[CityGML3][DIAG] expected cityObjectMember IDs: {len(diag_expected_ids)}")
        except Exception as e:
            print(f"[CityGML3][DIAG] pre-scan failed: {e}")
    diag_imported_ids = set()

    # Stream features one by one
    id_to_obj = {}
    building_part_edges = []  # List of (parent_building_id, part_id) via bldg:buildingPart (CityGML 3)
    building_installation_edges = []  # List of (parent_building_id, inst_id) via bldg:buildingInstallation (CityGML 3)
    building_furniture_edges = []  # List of (parent_id, furn_id) via bldg:buildingFurniture (CityGML 3)
    building_storey_edges = []  # List of (parent_building_id, storey_id) via bldg:buildingSubdivision/Storey (CityGML 3)
    bridge_part_edges = []    # List of (parent_bridge_id, part_id) via brid:bridgePart (CityGML 3)
    bridge_installation_edges = []  # List of (parent_bridge_id, inst_id) via brid:bridgeInstallation (CityGML 3)
    bridge_furniture_edges = []  # List of (parent_bridge_id, furn_id) via brid:bridgeFurniture (CityGML 3)
    tunnel_part_edges = []    # List of (parent_tunnel_id, part_id) via tun:tunnelPart (CityGML 3)
    tunnel_installation_edges = []  # List of (parent_tunnel_id, inst_id) via tun:tunnelInstallation (CityGML 3)
    tunnel_furniture_edges = []  # List of (parent_tunnel_id, furn_id) via tun:tunnelFurniture (CityGML 3)
    feat_count = 0
    feat_imported = 0
    
    # Error tracking
    error_tracker = ImportErrorTracker()
    
    # Performance Profiler (aktiviert bei export_debug)
    profiler = None
    if context.scene.cgml3.export_debug:
        try:
            from ...shared.performance_profiler import PerformanceProfiler
            profiler = PerformanceProfiler()
            print("[CityGML3] Performance profiling enabled")
        except Exception as e:
            print(f"[CityGML3] Could not load profiler: {e}")
    
    # Initialize Dynamic Namespace Manager for flexible schema handling
    ns_manager = None
    try:
        if profiler:
            profiler.start("dynamic_namespace_init")
        
        from ...common.dynamic_namespaces import DynamicNamespaceManager
        
        # For streaming import, we need to parse just the root element
        # without loading the entire tree
        try:
            from lxml import etree as ET
        except ImportError:
            import xml.etree.ElementTree as ET
        
        # Quick parse to get root element for namespace detection
        parser = ET.XMLParser(huge_tree=True) if hasattr(ET, 'XMLParser') else None
        if parser:
            tree = ET.parse(path, parser=parser)
        else:
            tree = ET.parse(path)
        root = tree.getroot()
        
        ns_manager = DynamicNamespaceManager(root)
        print("[CityGML3] Using dynamic namespace detection")
        if context.scene.cgml3.export_debug:
            print(ns_manager.get_info_string())
        
        if profiler:
            profiler.end("dynamic_namespace_init")
    except Exception as e:
        print(f"[CityGML3] Dynamic namespace detection failed: {e}")
        print("[CityGML3] Falling back to static namespaces")
        if profiler:
            profiler.end("dynamic_namespace_init")
    
    # Build GML ID lookup for XLink resolution in streaming mode
    # CRITICAL: Streaming import needs this to resolve geometry XLinks (baseSurface, etc.)
    id_lookup = {}
    _geom_mod = None
    try:
        from . import geometry as _geom_mod
        
        # Build lookup table from root (already loaded above)
        if 'root' in locals() and root is not None:
            gml_ns = NS.get("gml")
            if gml_ns:
                gml_id_attr = f"{{{gml_ns}}}id"
                print("[CityGML3] Building GML ID lookup for XLink resolution...")
                element_count = 0
                for el in root.iter():
                    gid = el.get(gml_id_attr)
                    if gid:
                        id_lookup[gid] = el
                        element_count += 1
                print(f"[CityGML3] Registered {element_count} elements with gml:id attributes")
        _geom_mod.GML_ID_LOOKUP = id_lookup
    except Exception as e:
        print(f"[CityGML3] Failed to build GML ID lookup: {e}")
        _geom_mod = None

    
    # Cache commonly used namespaces to avoid repeated dict lookups
    # OPTIMIZATION: ~5-10% faster by avoiding NS.get() calls in hot loops
    core_ns = NS.get('core')
    gml_ns = NS.get('gml')
    bldg_ns = NS.get('bldg')
    grp_ns = NS.get('grp')
    dyn_ns = NS.get('dyn')
    vers_ns = NS.get('vers')
    
    # Memory management
    # OPTIMIZED: Cache auf 2000 Einträge erhöht für besseres Mesh-Sharing
    geometry_cache = LRUGeometryCache(max_entries=2000, max_memory_mb=500.0)
    memory_monitor = MemoryMonitor()
    
    # Batch processing optimization with auto-tuning
    feature_batch = []
    batch_size = auto_tune_batch_size(250)  # Erhöht von 100 für bessere Performance
    
    # Hierarchical building tracking for streaming import
    _hierarchical_building_ids_s = set()
    _storey_to_building_id_s = {}

    try:
        for feat in iter_features_streaming(path, gml_id_filter, bbox_coords, feature_type_filter, ns_manager=ns_manager):
            feat_count += 1
            
            # NOTE: Feature type derived here; avoid noisy debug prints in normal operation
            feat_ln = feat.tag.split("}")[-1]
            feat_id = feat.get(f"{{{NS['gml']}}}id") or f"feat_{feat_count}"

            # Streaming: Part-Relationen einsammeln (CityGML 3.0)
            # Parts können inline unter dem Parent vorkommen.
            try:
                gml_id_attr = f"{{{NS['gml']}}}id"
                parent_id = feat.get(gml_id_attr)
                if parent_id and feat_ln == "Building":
                    for rel in feat.findall("./bldg:buildingPart", NS):
                        part_el = rel.find("./bldg:BuildingPart", NS)
                        if part_el is not None:
                            part_id = part_el.get(gml_id_attr)
                            if part_id:
                                building_part_edges.append((parent_id, part_id))
                    for rel in feat.findall("./bldg:buildingInstallation", NS):
                        inst_el = rel.find("./bldg:BuildingInstallation", NS)
                        if inst_el is not None:
                            inst_id = inst_el.get(gml_id_attr)
                            if inst_id:
                                building_installation_edges.append((parent_id, inst_id))
                    for rel in feat.findall("./bldg:buildingFurniture", NS):
                        furn_el = rel.find("./bldg:BuildingFurniture", NS)
                        if furn_el is not None:
                            furn_id = furn_el.get(gml_id_attr)
                            if furn_id:
                                building_furniture_edges.append((parent_id, furn_id))
                    for rel in feat.findall("./bldg:buildingSubdivision", NS):
                        storey_el = rel.find("./bldg:Storey", NS)
                        if storey_el is not None:
                            storey_id = storey_el.get(gml_id_attr)
                            if not storey_id:
                                storey_id = f"Storey_{uuid4().hex[:12]}"
                                storey_el.set(gml_id_attr, storey_id)
                            building_storey_edges.append((parent_id, storey_id))
                            _hierarchical_building_ids_s.add(parent_id)
                            _storey_to_building_id_s[storey_id] = parent_id
                if parent_id and feat_ln in ("Bridge", "BridgePart"):
                    for rel in feat.findall("./brid:bridgePart", NS):
                        part_el = rel.find("./brid:BridgePart", NS)
                        if part_el is not None:
                            part_id = part_el.get(gml_id_attr)
                            if part_id:
                                bridge_part_edges.append((parent_id, part_id))
                    for rel in feat.findall("./brid:bridgeInstallation", NS):
                        inst_el = rel.find("./brid:BridgeInstallation", NS)
                        if inst_el is not None:
                            inst_id = inst_el.get(gml_id_attr)
                            if inst_id:
                                bridge_installation_edges.append((parent_id, inst_id))
                    for rel in feat.findall("./brid:bridgeFurniture", NS):
                        furn_el = rel.find("./brid:BridgeFurniture", NS)
                        if furn_el is not None:
                            furn_id = furn_el.get(gml_id_attr)
                            if furn_id:
                                bridge_furniture_edges.append((parent_id, furn_id))
                if parent_id and feat_ln == "BuildingPart":
                    for rel in feat.findall("./bldg:buildingInstallation", NS):
                        inst_el = rel.find("./bldg:BuildingInstallation", NS)
                        if inst_el is not None:
                            inst_id = inst_el.get(gml_id_attr)
                            if inst_id:
                                building_installation_edges.append((parent_id, inst_id))
                    for rel in feat.findall("./bldg:buildingFurniture", NS):
                        furn_el = rel.find("./bldg:BuildingFurniture", NS)
                        if furn_el is not None:
                            furn_id = furn_el.get(gml_id_attr)
                            if furn_id:
                                building_furniture_edges.append((parent_id, furn_id))
                if parent_id and feat_ln in ("Tunnel", "TunnelPart"):
                    for rel in feat.findall("./tun:tunnelPart", NS):
                        part_el = rel.find("./tun:TunnelPart", NS)
                        if part_el is not None:
                            part_id = part_el.get(gml_id_attr)
                            if part_id:
                                tunnel_part_edges.append((parent_id, part_id))
                    for rel in feat.findall("./tun:tunnelInstallation", NS):
                        inst_el = rel.find("./tun:TunnelInstallation", NS)
                        if inst_el is not None:
                            inst_id = inst_el.get(gml_id_attr)
                            if inst_id:
                                tunnel_installation_edges.append((parent_id, inst_id))
                    for rel in feat.findall("./tun:tunnelFurniture", NS):
                        furn_el = rel.find("./tun:TunnelFurniture", NS)
                        if furn_el is not None:
                            furn_id = furn_el.get(gml_id_attr)
                            if furn_id:
                                tunnel_furniture_edges.append((parent_id, furn_id))
            except Exception:
                pass
            # NOTE: avoid noisy per-feature debug prints in normal operation
            
            # Check for embedded PointClouds in core:boundary elements (e.g., WallSurface with core:pointCloud)
            # This handles cases like Storey boundaries with PointCloud data
            has_embedded_pointcloud = False
            try:
                pc_test = feat.find(".//core:pointCloud/pcl:PointCloud", namespaces=NS)
                if pc_test is not None:
                    has_embedded_pointcloud = True
                    # NOTE: avoid noisy debug prints; keep behavior
            except Exception as e:
                # NOTE: avoid noisy debug prints; keep behavior
                pass
            
            if has_embedded_pointcloud:
                # NOTE: avoid noisy debug prints; keep behavior
                # Process PointCloud boundaries immediately (don't batch with geometry)
                feat_ln = feat.tag.split("}")[-1]
                feat_id = feat.get(f"{{{NS['gml']}}}id") or f"feat_{abs(hash(ET.tostring(feat)))}"
                # NOTE: avoid noisy debug prints; keep behavior
                
                # Import boundary PointClouds as separate objects
                from .pointcloud import parse_pointcloud_inline_points, create_blender_pointcloud
                
                boundary_idx = 0
                boundaries = feat.findall(".//core:boundary", namespaces=NS)
                # NOTE: avoid noisy debug prints; keep behavior
                
                for boundary_el in boundaries:
                    for surface_el in boundary_el:
                        # Find embedded PointCloud
                        pc_el = surface_el.find(".//core:pointCloud/pcl:PointCloud", namespaces=NS)
                        if pc_el is not None:
                            points = parse_pointcloud_inline_points(pc_el)
                            if points:
                                # Get surface name/type
                                surface_name = surface_el.findtext(".//gml:name", namespaces=NS)
                                surface_type = surface_el.tag.split("}")[-1]
                                
                                # Create unique name
                                pc_name = f"{feat_id}_{surface_type}_{boundary_idx}"
                                if surface_name:
                                    pc_name = f"{feat_id}_{surface_name.replace(' ', '_')}"
                                
                                # Create PointCloud object
                                pc_obj = create_blender_pointcloud(
                                    name=pc_name,
                                    points=points,
                                    attributes=None,
                                    ref_origin=ref_origin
                                )
                                
                                if pc_obj:
                                    sub_coll = get_type_collection(feat_ln)
                                    sub_coll.objects.link(pc_obj)
                                    
                                    pc_obj["cgml3_feature"] = feat_ln
                                    pc_obj["gml_id"] = pc_name
                                    pc_obj["parent_feature_id"] = feat_id
                                    pc_obj["surface_type"] = surface_type
                                    if surface_name:
                                        pc_obj["surface_name"] = surface_name
                                    pc_obj["point_count"] = len(points)
                                    pc_obj["cgml3_is_pointcloud"] = True
                                    
                                    id_to_obj[pc_name] = pc_obj
                                    feat_imported += 1
                                    
                                    print(f"Imported PointCloud: {pc_name} ({len(points)} points) from {surface_type}")
                            
                            boundary_idx += 1
                
                # Continue to next feature (don't add to geometry batch)
                continue
            
            feature_batch.append(feat)
            
            # Process batch when full or periodically update progress
            if len(feature_batch) >= batch_size or feat_count % 10 == 0:
                # Progress update (max 85% during feature processing)
                progress_pct = min(20 + (feat_count / max(1, feat_count + 100)) * 65, 85)
                print_console_progress(f"Features verarbeiten", feat_imported, feat_count, progress_pct)
                if wm:
                    progress = min(20 + int((feat_count / 100) * 65), 85)
                    wm.progress_update(progress)
                
                # Process accumulated batch
                if feature_batch:
                    for feat_elem, verts, faces, surf_labels, surf_attrs_by_poly, surface_id_by_poly, multisurface_id_by_poly, compositesurface_id_by_poly, poly_also_in_solid, filling_parent_surface_id_by_poly in batch_process_geometries(
                        feature_batch, default_srs, ref_origin, lod_filter, batch_size, error_tracker
                    ):
                        feat_ln = feat_elem.tag.split("}")[-1]
                        feat_id = feat_elem.get(f"{{{NS['gml']}}}id") or f"feat_{abs(hash(ET.tostring(feat_elem)))}"
                        
                        # Extract object name from gml:name (preferred) or gml:id (fallback)
                        feat_name_elem = feat_elem.find(".//gml:name", NS)
                        if feat_name_elem is not None and feat_name_elem.text:
                            obj_name = feat_name_elem.text.strip()
                        else:
                            obj_name = feat_id
                        
                        if _IMPORT_DIAG and feat_id:
                            diag_imported_ids.add(feat_id)
                            if _IMPORT_DIAG_WATCH_IDS and feat_id in _IMPORT_DIAG_WATCH_IDS:
                                print(f"[CityGML3][DIAG] importing watched id={feat_id} type={feat_ln}")
                        
                        # Get feature type collection
                        _is_hier_bldg_s = (feat_ln in ("Building", "BuildingPart") and
                                           feat_id in _hierarchical_building_ids_s)
                        _is_storey_child_s = (feat_ln == "Storey" and feat_id in _storey_to_building_id_s)
                        if _is_storey_child_s:
                            sub_coll = get_type_collection("Building")
                        else:
                            sub_coll = get_type_collection(feat_ln)
                        
                        if not verts:
                            # Create empty object for features without geometry
                            obj = bpy.data.objects.new(obj_name, None)
                            obj["gml_id"] = feat_id
                            obj["gml_name"] = obj_name
                            obj["feature_type"] = feat_ln
                            if _is_hier_bldg_s:
                                obj["structure_type"] = "hierarchical"
                                obj["cgml3_feature"] = feat_ln
                                obj.empty_display_type = 'CUBE'
                                obj.empty_display_size = 2.0
                            if _is_storey_child_s:
                                obj["cgml3_feature"] = "Storey"
                                obj["structure_part"] = "storey"
                            sub_coll.objects.link(obj)
                            id_to_obj[feat_id] = obj
                            if _IMPORT_DIAG and feat_id:
                                diag_imported_ids.add(feat_id)
                            continue
                        
                        # Check for geometry deduplication with LRU cache
                        # PERFORMANCE: Nur hashen wenn Cache schon Einträge hat
                        geom_hash = None
                        mesh_data = None
                        
                        if len(geometry_cache.cache) > 0 and verts and faces:
                            geom_hash = _compute_geometry_hash(verts, faces)
                            if geom_hash:
                                mesh_data = geometry_cache.get(geom_hash)
                        
                        if mesh_data is None:
                            # Create new mesh
                            if _is_hier_bldg_s:
                                mesh_data = bpy.data.meshes.new(f"{obj_name}_OuterShell")
                            else:
                                mesh_data = bpy.data.meshes.new(obj_name)
                            mesh_data.from_pydata(verts, [], faces)
                            # mesh_data.update()  # Deaktiviert für Performance
                            
                            # Cache if significant size (using LRU cache)
                            if geom_hash and len(faces) > 5:
                                geometry_cache.put(geom_hash, mesh_data)
                        
                        if _is_hier_bldg_s:
                            # Create Building EMPTY as parent
                            building_empty_s = bpy.data.objects.new(obj_name, None)
                            building_empty_s.empty_display_type = 'CUBE'
                            building_empty_s.empty_display_size = 2.0
                            sub_coll.objects.link(building_empty_s)
                            building_empty_s["gml_id"] = feat_id
                            building_empty_s["gml_name"] = obj_name
                            building_empty_s["feature_type"] = feat_ln
                            building_empty_s["structure_type"] = "hierarchical"
                            id_to_obj[feat_id] = building_empty_s

                            # Outer shell mesh as child
                            obj = bpy.data.objects.new(mesh_data.name, mesh_data)
                            obj.parent = building_empty_s
                            sub_coll.objects.link(obj)
                            obj["structure_part"] = "outer_shell"
                            obj["gml_id"] = f"{feat_id}_outer"
                            obj["feature_type"] = feat_ln
                        else:
                            # Create object with name from gml:name
                            obj = bpy.data.objects.new(obj_name, mesh_data)
                            obj["gml_id"] = feat_id
                            obj["gml_name"] = obj_name
                            obj["feature_type"] = feat_ln
                            sub_coll.objects.link(obj)
                            id_to_obj[feat_id] = obj
                            if _is_storey_child_s:
                                obj["structure_part"] = "storey"
                        feat_imported += 1
                        
                        # Apply appearance (materials and textures)
                        # IMPORTANT: Always apply materials, even without appearance data
                        # This creates materials based on surface type for all faces
                        apply_materials_uvs(
                            obj,
                            mesh_data,
                            surf_labels,
                            x3d_by_poly,
                            ptex_by_ring,
                            {},  # gtex_by_poly (not used in streaming)
                            poly_to_image,
                            path,
                            ptex_by_poly=ptex_by_poly,
                            surface_attrs_by_poly=surf_attrs_by_poly,
                            surface_id_by_poly=surface_id_by_poly,
                            multisurface_id_by_poly=multisurface_id_by_poly,
                            compositesurface_id_by_poly=compositesurface_id_by_poly,
                            poly_also_in_solid=poly_also_in_solid,
                            filling_parent_surface_id_by_poly=filling_parent_surface_id_by_poly,
                        )
                        
                        # Store common attributes (on EMPTY for hierarchical buildings)
                        if _is_hier_bldg_s:
                            _store_common_attributes(building_empty_s, feat_elem)
                        else:
                            _store_common_attributes(obj, feat_elem)
                    
                    # Clear batch
                    feature_batch = []
        
        # Process remaining features in batch
        if feature_batch:
            for feat_elem, verts, faces, surf_labels, surf_attrs_by_poly, surface_id_by_poly, multisurface_id_by_poly, compositesurface_id_by_poly, poly_also_in_solid, filling_parent_surface_id_by_poly in batch_process_geometries(
                feature_batch, default_srs, ref_origin, lod_filter, batch_size
            ):
                feat_ln = feat_elem.tag.split("}")[-1]
                feat_id = feat_elem.get(f"{{{NS['gml']}}}id") or f"feat_{abs(hash(ET.tostring(feat_elem)))}"
                
                # Extract object name from gml:name (preferred) or gml:id (fallback)
                feat_name_elem = feat_elem.find(".//gml:name", NS)
                if feat_name_elem is not None and feat_name_elem.text:
                    obj_name = feat_name_elem.text.strip()
                else:
                    obj_name = feat_id
                
                if _IMPORT_DIAG and feat_id:
                    diag_imported_ids.add(feat_id)
                
                _is_hier_bldg_r = (feat_ln in ("Building", "BuildingPart") and
                                   feat_id in _hierarchical_building_ids_s)
                _is_storey_child_r = (feat_ln == "Storey" and feat_id in _storey_to_building_id_s)
                if _is_storey_child_r:
                    sub_coll = get_type_collection("Building")
                else:
                    sub_coll = get_type_collection(feat_ln)
                
                if not verts:
                    obj = bpy.data.objects.new(obj_name, None)
                    obj["gml_id"] = feat_id
                    obj["gml_name"] = obj_name
                    obj["feature_type"] = feat_ln
                    if _is_hier_bldg_r:
                        obj["structure_type"] = "hierarchical"
                        obj["cgml3_feature"] = feat_ln
                        obj.empty_display_type = 'CUBE'
                        obj.empty_display_size = 2.0
                    if _is_storey_child_r:
                        obj["cgml3_feature"] = "Storey"
                        obj["structure_part"] = "storey"
                    sub_coll.objects.link(obj)
                    id_to_obj[feat_id] = obj
                    if _IMPORT_DIAG and feat_id:
                        diag_imported_ids.add(feat_id)
                    continue
                
                # PERFORMANCE: Nur hashen wenn Cache schon befüllt
                geom_hash = None
                mesh_data = None
                
                if GEOMETRY_CACHE:
                    geom_hash = _compute_geometry_hash(verts, faces)
                    if geom_hash and geom_hash in GEOMETRY_CACHE:
                        mesh_data = GEOMETRY_CACHE[geom_hash]
                
                if mesh_data is None:
                    if _is_hier_bldg_r:
                        mesh_data = bpy.data.meshes.new(f"{obj_name}_OuterShell")
                    else:
                        mesh_data = bpy.data.meshes.new(obj_name)
                    mesh_data.from_pydata(verts, [], faces)
                    mesh_data.update()
                    
                    if geom_hash and len(faces) > 5:
                        GEOMETRY_CACHE[geom_hash] = mesh_data
                
                if _is_hier_bldg_r:
                    # Create Building EMPTY as parent
                    building_empty_r = bpy.data.objects.new(obj_name, None)
                    building_empty_r.empty_display_type = 'CUBE'
                    building_empty_r.empty_display_size = 2.0
                    sub_coll.objects.link(building_empty_r)
                    building_empty_r["gml_id"] = feat_id
                    building_empty_r["gml_name"] = obj_name
                    building_empty_r["feature_type"] = feat_ln
                    building_empty_r["structure_type"] = "hierarchical"
                    id_to_obj[feat_id] = building_empty_r

                    obj = bpy.data.objects.new(mesh_data.name, mesh_data)
                    obj.parent = building_empty_r
                    sub_coll.objects.link(obj)
                    obj["structure_part"] = "outer_shell"
                    obj["gml_id"] = f"{feat_id}_outer"
                    obj["feature_type"] = feat_ln
                else:
                    obj = bpy.data.objects.new(obj_name, mesh_data)
                    obj["gml_id"] = feat_id
                    obj["gml_name"] = obj_name
                    obj["feature_type"] = feat_ln
                    sub_coll.objects.link(obj)
                    id_to_obj[feat_id] = obj
                    if _is_storey_child_r:
                        obj["structure_part"] = "storey"
                feat_imported += 1
                
                # Apply appearance (materials and textures)
                # IMPORTANT: Always apply materials, even without appearance data
                # This creates materials based on surface type for all faces
                apply_materials_uvs(
                    obj,
                    mesh_data,
                    surf_labels,
                    x3d_by_poly,
                    ptex_by_ring,
                    {},  # gtex_by_poly (not used in streaming)
                    poly_to_image,
                    path,
                    ptex_by_poly=ptex_by_poly,
                    surface_attrs_by_poly=surf_attrs_by_poly,
                    surface_id_by_poly=surface_id_by_poly,
                    multisurface_id_by_poly=multisurface_id_by_poly,
                    compositesurface_id_by_poly=compositesurface_id_by_poly,
                    poly_also_in_solid=poly_also_in_solid,
                    filling_parent_surface_id_by_poly=filling_parent_surface_id_by_poly,
                )
                
                if _is_hier_bldg_r:
                    _store_common_attributes(building_empty_r, feat_elem)
                else:
                    _store_common_attributes(obj, feat_elem)
                
                # Periodic memory monitoring and cleanup
                if feat_imported % 100 == 0:
                    memory_monitor.update()
                    if feat_imported % 500 == 0:
                        trigger_garbage_collection()

        # After importing all features: apply part-parenting (CityGML 3.0)
        def _apply_part_parenting(edges):
            for parent_id, part_id in edges:
                parent_obj = id_to_obj.get(parent_id)
                part_obj = id_to_obj.get(part_id)
                if parent_obj is None or part_obj is None:
                    continue
                if parent_obj is part_obj:
                    continue
                try:
                    part_obj.parent = parent_obj
                except Exception:
                    pass
                try:
                    part_obj["cgml_parent_id"] = parent_id
                except Exception:
                    pass

        print_console_progress("Hierarchie aufbauen...", percentage=88)
        _apply_part_parenting(building_part_edges)
        _apply_part_parenting(building_installation_edges)
        _apply_part_parenting(building_furniture_edges)
        _apply_part_parenting(building_storey_edges)
        _apply_part_parenting(bridge_part_edges)
        _apply_part_parenting(bridge_installation_edges)
        _apply_part_parenting(bridge_furniture_edges)
        _apply_part_parenting(tunnel_part_edges)
        _apply_part_parenting(tunnel_installation_edges)
        _apply_part_parenting(tunnel_furniture_edges)

        outer_shell_updates = 0
        for imported_obj in id_to_obj.values():
            if getattr(imported_obj, "type", None) != "EMPTY":
                continue
            outer_shell_updates += _sync_outer_shell_surface_semantics(imported_obj)
        if outer_shell_updates:
            print(f"[CityGML3] Synchronized OuterShell surface semantics on {outer_shell_updates} materials")

        _disable_inline_subfeature_collections(coll)
    
    except Exception as e:
        if wm:
            wm.progress_end()
        raise RuntimeError(f"Streaming import failed: {e}")
    
    # Display error summary if any errors occurred
    error_summary = error_tracker.get_summary()
    if error_summary['total_errors'] > 0:
        print(f"\n=== Import Error Summary ===")
        print(f"Total features processed: {feat_count}")
        print(f"Successfully imported: {feat_imported}")
        print(f"Failed features: {error_summary['failed_features']}")
        print(f"Total errors: {error_summary['total_errors']}")
        print(f"Total warnings: {error_summary['total_warnings']}")
        
        print(f"\nError types:")
        for error_type, count in error_summary['error_types'].items():
            print(f"  {error_type}: {count}")
        
        if error_summary['total_warnings'] > 0:
            print(f"\nWarning types:")
            for warning_type, count in error_summary['warning_types'].items():
                print(f"  {warning_type}: {count}")
        
        # Show sample errors (first 5)
        if error_tracker.errors:
            print(f"\nSample errors (first 5):")
            for error in error_tracker.errors[:5]:
                print(f"  Feature '{error['feature_id']}': {error['error_type']} - {error['message']}")
        
        print(f"===========================\n")
    
    # Post-process: Import embedded PointClouds from boundaries
    try:
        print("[CityGML] Searching for embedded PointClouds in boundaries...")
        from .pointcloud import parse_pointcloud_inline_points, create_blender_pointcloud
        import xml.etree.ElementTree as ET_std
        
        # Parse file to find embedded PointClouds
        tree = ET_std.parse(path)
        root = tree.getroot()
        
        pointcloud_count = 0
        
        # Build full namespace map including pcl and core
        ns_map = {}
        for key, value in NS.items():
            ns_map[key] = value
        
        # Search for all PointCloud elements in the document
        pcl_ns = NS.get("pcl")
        core_ns = NS.get("core")
        gml_ns = NS.get("gml")
        
        print(f"[CityGML] Searching with namespaces: pcl={pcl_ns}, core={core_ns}")
        
        # Find all pcl:PointCloud elements
        pc_elements = root.findall(f".//{{{pcl_ns}}}PointCloud", ns_map)
        print(f"[CityGML] Found {len(pc_elements)} PointCloud elements")
        
        for pc_el in pc_elements:
            # Parse the points
            points = parse_pointcloud_inline_points(pc_el)
            
            if points:
                # Try to find parent surface by going up the tree
                # ElementTree doesn't have parent access, so we search differently
                surface_type = "Unknown"
                surface_name = ""
                
                # Create unique name
                pc_name = f"PointCloud_{pointcloud_count}"
                
                # Create Blender object
                pc_obj = create_blender_pointcloud(
                    name=pc_name,
                    points=points,
                    attributes=None,
                    ref_origin=ref_origin
                )
                
                if pc_obj:
                    # Add to appropriate collection
                    pc_coll = get_type_collection("PointCloud")
                    pc_coll.objects.link(pc_obj)
                    
                    pc_obj["cgml3_feature"] = "PointCloud"
                    pc_obj["gml_id"] = pc_name
                    pc_obj["surface_type"] = surface_type
                    pc_obj["point_count"] = len(points)
                    pc_obj["cgml3_is_pointcloud"] = True
                    
                    pointcloud_count += 1
                    print(f"  Imported PointCloud: {pc_name} ({len(points)} points)")
        
        if pointcloud_count > 0:
            print(f"[CityGML] Imported {pointcloud_count} embedded PointClouds")
        else:
            print("[CityGML] No embedded PointClouds found")
            
    except Exception as e:
        print(f"[CityGML] Error processing embedded PointClouds: {e}")
        import traceback
        traceback.print_exc()
    
    # Finalize progress
    print_console_progress("Statistiken sammeln...", percentage=92)
    
    # Display memory statistics
    print()  # Newline
    print(get_memory_stats_summary(geometry_cache, memory_monitor))
    
    # Display appearance statistics
    if 'appearance_processor' in locals() and appearance_processor:
        print(get_parallel_stats_summary(appearance_processor))

    # Diagnostics summary: expected cityObjectMember IDs vs actually imported IDs
    if _IMPORT_DIAG and diag_expected_ids:
        missing = sorted(diag_expected_ids - diag_imported_ids)
        print(f"[CityGML3][DIAG] imported cityObjectMember IDs: {len(diag_imported_ids)}")
        print(f"[CityGML3][DIAG] missing cityObjectMember IDs after import: {len(missing)}")
        if _IMPORT_DIAG_WATCH_IDS:
            for gid in sorted(_IMPORT_DIAG_WATCH_IDS):
                if gid in diag_expected_ids and gid not in diag_imported_ids:
                    print(f"[CityGML3][DIAG] WATCH missing id={gid} type={diag_expected_types.get(gid)}")
        else:
            for gid in missing[:100]:
                print(f"[CityGML3][DIAG] missing id={gid} type={diag_expected_types.get(gid)}")
    
    # Update import statistics (for streaming we can't count before filtering)
    if hasattr(context.scene, 'cgml3'):
        context.scene.cgml3.last_import_total = feat_count
        context.scene.cgml3.last_import_imported = feat_imported
        context.scene.cgml3.last_import_filtered = feat_count - feat_imported
    
    # Finalize Blender scene (this is what causes the "freeze" - force it now with progress)
    print_console_progress("Blender View-Layer aktualisieren...", percentage=95)
    try:
        # Force view layer update to finalize all objects
        if hasattr(context, 'view_layer'):
            context.view_layer.update()
    except Exception as e:
        print(f"[Warning] View layer update failed: {e}")
    
    print_console_progress("Dependency-Graph finalisieren...", percentage=97)
    try:
        # Force dependency graph evaluation
        if hasattr(context, 'evaluated_depsgraph_get'):
            context.evaluated_depsgraph_get()
    except Exception as e:
        print(f"[Warning] Depsgraph evaluation failed: {e}")
    
    print_console_progress("Szene fertigstellen...", percentage=99)
    try:
        # Trigger one final scene update to ensure everything is ready
        if hasattr(bpy.context, 'scene'):
            bpy.context.scene.update_tag()
    except Exception:
        pass
    
    if wm:
        wm.progress_update(100)
        wm.progress_end()
    
    # Final progress - NOW Blender is truly ready
    print_console_progress("Import abgeschlossen", percentage=100)
    print()  # Newline
    
    # Log import time
    elapsed_time = time.time() - start_time
    print(f"[CityGML Streaming Import] Completed in {elapsed_time:.2f} seconds ({elapsed_time/60:.1f} minutes)")
    print(f"\n{'='*60}")
    print(f"✓ IMPORT VOLLSTÄNDIG ABGESCHLOSSEN")
    print(f"  Importierte Features: {feat_imported} von {feat_count}")
    print(f"  Blender ist jetzt wieder einsatzbereit.")
    print(f"{'='*60}\n")
    
    # Print profiling report if enabled
    if profiler:
        print("\n" + "="*80)
        print("PERFORMANCE PROFILING REPORT")
        print("="*80)
        profiler.print_report(sort_by='total')
        
        # Material-Cache-Statistiken
        try:
            from ...shared.material_cache import print_global_cache_stats
            print_global_cache_stats()
        except Exception as e:
            print(f"[CityGML] Material cache stats unavailable: {e}")
        
        # Save detailed report to file
        try:
            profile_path = str(path).replace('.gml', '.profile.json').replace('.xml', '.profile.json')
            profiler.save_to_file(profile_path)
            print(f"\nDetailed profile saved to: {profile_path}")
        except Exception as e:
            print(f"Could not save profile: {e}")
            profile_path = str(path).replace('.gml', '.profile.json').replace('.xml', '.profile.json')
            profiler.save_to_file(profile_path)
            print(f"\nDetailed profile saved to: {profile_path}")
        except Exception as e:
            print(f"Could not save profile: {e}")


def _determine_file_origin_and_crs_from_attrs(srs_name, lower_corner):
    """
    Helper to determine CRS and origin from root attributes.
    
    Args:
        srs_name: SRS name (typically from CityModel's srsName or Envelope's srsName as fallback)
        lower_corner: Lower corner coordinates from Envelope
    
    Returns:
        Tuple of (origin, crs)
    """
    crs = "Unknown CRS"
    origin = [0.0, 0.0, 0.0]
    
    if srs_name:
        normalized = _normalize_epsg(srs_name)
        crs = normalized if normalized != "Unknown CRS" else str(srs_name).strip()
    
    if lower_corner and len(lower_corner) >= 2:
        origin[0] = lower_corner[0]
        origin[1] = lower_corner[1]
        if len(lower_corner) >= 3:
            origin[2] = lower_corner[2]
    
    return origin, crs
