# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
CityGML 2.0 Reader - Importer
==============================

Imports CityGML 2.0 files into Blender.
Generic feature processing similar to CityGML 3.0.

Main differences from CityGML 3.0:
- GML 3.1.1 instead of GML 3.2.1
- cityObjectMember (without the "core:" prefix)
- Generic Attributes: "name" as an XML attribute instead of an element
- xs:date instead of xs:dateTime for creationDate/terminationDate
- No PointCloud, Dynamizer, Versioning, or Construction module
"""

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
from .geometry import (
    iter_features,
    build_geometry,
    build_geometry_hierarchical,
    citygml2_subfeature_excludes,
    parse_points,
    parse_linestrings,
)
from .appearance import parse_x3d_materials, parse_parameterized_textures, parse_georeferenced_textures
from .materials import apply_materials_uvs
from .xml_utils import localname, _norm_id
from .grid_coverage import parse_rectified_grid_coverage, create_mesh_from_grid
# PointCloud not in CityGML 2.0

# Shared modules for code deduplication
from ...shared.feature_parser import parse_all_feature_type_attributes
from ...shared.attribute_utils import parse_generic_attributes_common
from ...shared.materials_common import normalize_crs_to_epsg, extract_vertical_epsg
from ...common.filter_utils import resolve_scene_bbox_filter

import re

# Fingerprint -> canonical Template-ID
TEMPLATE_ID_BY_FP: dict[str, str] = {}

# Geometry caching for deduplication - replaced by LRU cache in memory_manager
# Kept for backward compatibility, but use memory_manager.LRUGeometryCache instead
GEOMETRY_CACHE: dict[str, bpy.types.Mesh] = {}

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
    # Backward compatible: Redirect to new logic
    return _normalize_epsg(srs_name)

def _crs_matches(a: str, b: str) -> bool:
    ea = _normalize_epsg(a or "")
    eb = _normalize_epsg(b or "")
    return ea != "Unknown CRS" and ea == eb

def _determine_file_origin_and_crs(root):
    srs, lc = _get_envelope_srs_and_corners(root)
    origin = lc if lc is not None else _scan_min_coords(root)
    crs = _normalize_epsg(srs) if srs else "Unknown CRS"

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
            "Envelope",
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
        # Handle both dict context and Blender context
        if isinstance(context, dict):
            bpy.context.scene.collection.children.link(coll)
        else:
            context.scene.collection.children.link(coll)
    return coll


_INLINE_SUBFEATURE_COLLECTION_NAMES = {
    "BuildingPart", "BuildingParts",
    "BridgePart", "BridgeParts",
    "TunnelPart", "TunnelParts",
    "BuildingInstallation", "BuildingInstallations",
    "IntBuildingInstallation", "IntBuildingInstallations",
    "BuildingFurniture", "BuildingFurnitures",
    "BridgeInstallation", "BridgeInstallations",
    "IntBridgeInstallation", "IntBridgeInstallations",
    "BridgeConstructionElement", "BridgeConstructionElements",
    "IntBridgeConstructionElement", "IntBridgeConstructionElements",
    "BridgeFurniture", "BridgeFurnitures",
    "TunnelInstallation", "TunnelInstallations",
    "IntTunnelInstallation", "IntTunnelInstallations",
    "TunnelFurniture", "TunnelFurnitures",
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

def _get_root_srs_streaming(path: str) -> str:
    """
    Quickly extract srsName from root element without parsing the entire document.
    Used for streaming import where we don't want to load the full tree.
    """
    try:
        for event, elem in ET.iterparse(path, events=('start',)):
            # Get srsName from the first/root element
            srs = elem.get("srsName")
            elem.clear()  # Clean up immediately
            return srs or ""
    except Exception:
        return ""


def _normalize_srs_name(srs: str) -> str:
    """
    Normalize common SRS representations to a stable label.

    Supports:
    - EPSG URNs (e.g., urn:ogc:def:crs:EPSG::25832)
    - ADV URNs used in DE (e.g., urn:adv:crs:EPSG::25832*DE_DHN92_NH)
    """
    if not srs:
        return ""
    s = str(srs).strip()
    normalized = _normalize_epsg(s)
    return normalized if normalized != "Unknown CRS" else s

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
    Returns a mapping: Dynamizer-gml:id -> gml:id of the surrounding CityObject.
    Assumes that the nearest ancestor in the CityGML model is the
    ‘owning’ CityObject.
    """
    owners = {}

    allowed_ns = (
        NS["core"], NS["bldg"], NS["brid"], NS["tun"], NS["tran"],
        NS["veg"], NS["wtr"], NS["con"], NS["frn"], NS["grp"],
        NS["luse"], NS["dem"], NS["pcl"], NS["tex"], NS["vers"], NS["gen"]
        # deliberately without NS[“dyn”]; the Dynamizer itself is not intended to become an “owner”
    )

    def recurse(elem, current_owner_id=None):
        if isinstance(elem.tag, str) and elem.tag.startswith("{"):
            ns_uri = elem.tag.split("}")[0].strip("{")
            ln = elem.tag.split("}")[-1]

            # Update CityObject owner (but not the Dynamizer itself)
            if ns_uri in allowed_ns and ln not in ("CityModel", "Appearance"):
                owner_id = elem.get(f"{{{NS['gml']}}}id") or current_owner_id
            else:
                owner_id = current_owner_id

            # Map Dynamizer to current owner
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

# [CityGML 2.0] Disabled - Dynamizer module doesn't exist in CityGML 2.0
_collect_dynamizer_owners = lambda root: {}

# Extract generic attributes (CityGML 2.0 + 3.0 compatible)
def _parse_generic_attributes(feat_el):
    """Parse generic attributes using shared module."""
    from .namespaces import NS
    return parse_generic_attributes_common(feat_el, NS, version="2.0")

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
    Returns (template_id, M_eff) for features whose geometry is defined exclusively
    via a single ImplicitGeometry (lodXImplicitRepresentation).

    Applies to all feature types (Building, Bridge, CityFurniture, Vegetation, Generic, ...).

    template_id : gml:id of the template geometry (e.g., from relativeGeometry@xlink:href)
    M_eff       : 4x4 matrix (mathutils.Matrix) representing the same transformation
                  applied by build_geometry to the vertices
                  (transformationMatrix + referencePoint + file offset).

    Supports both:
    - <ImplicitGeometry> as well as <core:ImplicitGeometry>
    - <transformationMatrix> / <core:transformationMatrix>
    - <referencePoint> / <core:referencePoint>
    - <relativeGeometry> / <core:relativeGeometry>

    Only instantiated if:
    - there is at least one <ImplicitGeometry>…</ImplicitGeometry>,
    - there is EXACTLY ONE ImplicitGeometry in this feature,
    - the geometry comes via lodXImplicitRepresentation (no parallel lodXGeometry).
    """

    # 1) check whether an ImplicitRepresentation exists at all
    has_lod_impl = False
    has_lod_geom = False
    for el in feat_el.iter():
        tag = getattr(el, "tag", None)
        if not isinstance(tag, str):
            continue
        ln = localname(tag)
        # NEW: also consider LoD0
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

    # Only instantiate pure ImplicitGeometry cases (no parallel lodXGeometry)
    if not has_lod_impl or has_lod_geom:
        return None, None, None

    # 2) choose the "active" ImplicitGeometry:
    #    we take – analogous to the usual LoD strategy – the highest available LoD.
    impl = None

    def _find_impl_for_lod(lod_tag: str):
        # CityGML 2.0: search by localname (without namespace prefix)
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

    # LoD priority: 4 → 3 → 2 → 1 → 0
    for lod_tag in (
        "lod4ImplicitRepresentation",
        "lod3ImplicitRepresentation",
        "lod2ImplicitRepresentation",
        "lod1ImplicitRepresentation",
        "lod0ImplicitRepresentation",
    ):
        impl = _find_impl_for_lod(lod_tag)
        if impl is not None:
            break

    # Fallback: first ImplicitGeometry anywhere in the feature, if the above finds nothing
    if impl is None:
        for el in feat_el.iter():
            tag = getattr(el, "tag", None)
            if not isinstance(tag, str):
                continue
            if localname(tag) == "ImplicitGeometry":
                impl = el
                break

    if impl is None:
        # should practically not occur, but for safety:
        return None, None, None

    # 3) determine template ID (relativeGeometry, possibly with xlink:href) + fingerprint
    xlink_href = "{http://www.w3.org/1999/xlink}href"
    template_id = ""
    fp = None  # geometry fingerprint (via pos/posList)

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
            # referenced template (xlink:href="#...") → search geometry via GML_ID_LOOKUP
            gid = _norm_id(href)
            try:
                from . import geometry as _geom_mod
                geom_root = _geom_mod.GML_ID_LOOKUP.get(gid)
            except Exception:
                geom_root = None
            # preferred template ID: normalized href ID
            template_id = gid
        else:
            # inline template: the geometry is directly under <relativeGeometry>
            geom_root = rel

        # 3a) attempt: read gml:id from the geometry, if available
        if not template_id and geom_root is not None:
            gml_ns = NS.get("gml")
            if gml_ns:
                gml_id_attr = f"{{{gml_ns}}}id"
                for el2 in geom_root.iter():
                    gid = el2.get(gml_id_attr)
                    if gid:
                        template_id = gid
                        break

        # 3b) create fingerprint from all pos/posList texts
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
                # if no template ID exists yet, use fingerprint as fallback ID
                if not template_id:
                    template_id = f"geomfp:{fp}"

    if not template_id:
        return None, None

    # 3c) Canonicalize fingerprint globally:
    #     If two different gml:ids have the same geometry (same FP),
    #     they will share the same template ID afterwards.
    if fp:
        global TEMPLATE_ID_BY_FP
        canon = TEMPLATE_ID_BY_FP.get(fp)
        if canon is None:
            TEMPLATE_ID_BY_FP[fp] = template_id
        else:
            template_id = canon

    # 4) evaluate transformationMatrix + referencePoint as in geometry._implicit_transform_for

    # transformationMatrix: CityGML 2.0 has no namespace prefix
    tm_el = None
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

    # search for referencePoint (localname == "referencePoint" → with/without namespace)
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
        # only translation through referencePoint
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

    # 5) consider file offset (ref_origin) as in build_geometry
    rx, ry, rz = ref_origin
    R = [
        [1.0, 0.0, 0.0, -rx],
        [0.0, 1.0, 0.0, -ry],
        [0.0, 0.0, 1.0, -rz],
        [0.0, 0.0, 0.0, 1.0],
    ]

    M_eff = Matrix(R) @ Matrix(M)

    return template_id, M_eff, None  # CityGML 2.0 has no LoD-specific implicit representation


def _create_hierarchical_building_objects(feat, feat_id, feat_ln, hier_data, ref_origin, default_srs, coll, id_to_obj):
    """
    Creates hierarchically structured Blender objects from hierarchical geometry data.
    
    Hybrid approach:
    - Outer building shell → a single mesh with material slots
    - Rooms → separate mesh objects as children of the Building-Empty
    - Furniture/Installations → separate mesh objects as children of the Room object
    
    Args:
        feat: XML-Element of the feature
        feat_id: gml:id of the feature
        feat_ln: Feature type (e.g., "Building")
        hier_data: Hierarchical geometry data from build_geometry_hierarchical()
        ref_origin: Reference origin
        default_srs: Default CRS
        coll: Blender Collection
        id_to_obj: Mapping from gml:id to Blender object
    
    Returns:
        Building-Empty object (Parent of all sub-objects)
    """
    import sys
    
    # 1. Create Building-Empty as Parent
    building_empty = bpy.data.objects.new(feat_id, None)
    building_empty.empty_display_type = 'CUBE'
    building_empty.empty_display_size = 2.0
    coll.objects.link(building_empty)
    
    building_empty["cgml3_feature"] = feat_ln
    building_empty["gml_id"] = feat_id
    building_empty["structure_type"] = "hierarchical"
    
    _store_common_attributes(building_empty, feat)
    id_to_obj[feat_id] = building_empty
    
    # Remove debug outputs from earlier development stages (without user option).
    
    # 2. Create Outer Shell Mesh (outer building shell)
    outer_data = hier_data.get("outer_shell", {})
    outer_verts = outer_data.get("verts", [])
    outer_faces = outer_data.get("faces", [])
    outer_labels = outer_data.get("surf_labels", [])
    
    if outer_verts and outer_faces:
        outer_mesh = bpy.data.meshes.new(f"{feat_id}_OuterShell")
        outer_mesh.from_pydata(outer_verts, [], outer_faces)
        
        outer_obj = bpy.data.objects.new(outer_mesh.name, outer_mesh)
        outer_obj.parent = building_empty
        coll.objects.link(outer_obj)
        
        outer_obj["cgml3_feature"] = f"{feat_ln}_OuterShell"
        outer_obj["gml_id"] = f"{feat_id}_outer"
        outer_obj["structure_part"] = "outer_shell"
        
        # Per-Surface Mapping for Export: face_index → surface_id
        import json as _json
        _outer_sid = outer_data.get("surface_ids", {})
        if _outer_sid and outer_labels:
            _sid_map = {}
            for fi, lbl in enumerate(outer_labels):
                poly_id = lbl[1]  # (surf_type, poly_id, ring_id, ...)
                if poly_id in _outer_sid:
                    _sid_map[str(fi)] = _outer_sid[poly_id]
            if _sid_map:
                outer_obj["surface_id_map"] = _json.dumps(_sid_map)
        _outer_sinfo = outer_data.get("surfaces_info", {})
        if _outer_sinfo:
            outer_obj["surfaces_info"] = _json.dumps(_outer_sinfo)
        
        # Surface-Labels as Material Slots or Custom Properties
        _apply_surface_labels_to_mesh(outer_mesh, outer_labels, outer_obj)
        
        # (debug removed) created outer shell
    
    # Openings (Doors/Windows) of the outer shell
    outer_openings = outer_data.get("openings", {})
    for opening_id, opening_info in outer_openings.items():
        opening_name = opening_info.get("name", opening_id)
        opening_type = opening_info.get("opening_type", "Opening")
        opening_verts = opening_info.get("verts", [])
        opening_faces = opening_info.get("faces", [])
        opening_labels = opening_info.get("surf_labels", [])
        
        surface_id = opening_info.get("surface_id")
        surface_type = opening_info.get("surface_type")
        surface_name = opening_info.get("surface_name")
        xlink_refs = opening_info.get("xlink_refs", [])
        
        if not (opening_verts and opening_faces) and not xlink_refs:
            continue
        
        if opening_verts and opening_faces:
            opening_mesh = bpy.data.meshes.new(opening_id)
            opening_mesh.from_pydata(opening_verts, [], opening_faces)
            
            opening_obj = bpy.data.objects.new(opening_mesh.name, opening_mesh)
        else:
            # Xlink-based Opening: only EMPTY (geometry is on the counterpart)
            opening_obj = bpy.data.objects.new(opening_id, None)
            opening_obj.empty_display_type = 'PLAIN_AXES'
            opening_obj.empty_display_size = 0.3
        
        opening_obj.parent = building_empty
        coll.objects.link(opening_obj)
        
        opening_obj["cgml3_feature"] = opening_type
        opening_obj["gml_id"] = opening_id
        opening_obj["gml_name"] = opening_name
        opening_obj["structure_part"] = "opening"
        opening_obj["opening_type"] = opening_type
        
        if surface_id:
            opening_obj["opening_surface_id"] = surface_id
        if surface_type:
            opening_obj["opening_surface_type"] = surface_type
        if surface_name:
            opening_obj["opening_surface_name"] = surface_name
        
        if xlink_refs:
            import json
            opening_obj["xlink_refs"] = json.dumps(xlink_refs)
        
        if opening_obj.type == 'MESH':
            _apply_surface_labels_to_mesh(opening_obj.data, opening_labels, opening_obj)
        id_to_obj[opening_id] = opening_obj
    
    # 3. Create Rooms
    rooms_data = hier_data.get("rooms", {})
    for room_id, room_info in rooms_data.items():
        room_name = room_info.get("name", room_id)
        room_verts = room_info.get("verts", [])
        room_faces = room_info.get("faces", [])
        room_labels = room_info.get("surf_labels", [])
        
        # Room-Empty as Sub-Parent
        room_empty = bpy.data.objects.new(f"{room_id}", None)
        room_empty.empty_display_type = 'PLAIN_AXES'
        room_empty.empty_display_size = 1.0
        room_empty.parent = building_empty
        coll.objects.link(room_empty)
        
        room_empty["cgml3_feature"] = "Room"
        room_empty["gml_id"] = room_id
        room_empty["gml_name"] = room_name
        room_empty["structure_part"] = "room"
        
        id_to_obj[room_id] = room_empty
        
        # Room Mesh (Walls, Floor, Ceiling)
        if room_verts and room_faces:
            room_mesh = bpy.data.meshes.new(f"{room_id}_Geometry")
            room_mesh.from_pydata(room_verts, [], room_faces)
            
            room_obj = bpy.data.objects.new(room_mesh.name, room_mesh)
            room_obj.parent = room_empty
            coll.objects.link(room_obj)
            
            room_obj["cgml_id"] = f"{room_id}_geom"
            room_obj["structure_part"] = "room_geometry"
            
            # Per-Surface Mapping for Export: face_index → surface_id
            import json as _json
            _room_sid = room_info.get("surface_ids", {})
            if _room_sid and room_labels:
                _sid_map = {}
                for fi, lbl in enumerate(room_labels):
                    poly_id = lbl[1]  # (surf_type, poly_id, ring_id, ...)
                    if poly_id in _room_sid:
                        _sid_map[str(fi)] = _room_sid[poly_id]
                if _sid_map:
                    room_obj["surface_id_map"] = _json.dumps(_sid_map)
            _room_sinfo = room_info.get("surfaces_info", {})
            if _room_sinfo:
                room_obj["surfaces_info"] = _json.dumps(_room_sinfo)
            
            _apply_surface_labels_to_mesh(room_mesh, room_labels, room_obj)
        
        # Furniture
        furniture_data = room_info.get("furniture", {})
        for furn_id, furn_info in furniture_data.items():
            furn_name = furn_info.get("name", furn_id)
            furn_verts = furn_info.get("verts", [])
            furn_faces = furn_info.get("faces", [])
            furn_labels = furn_info.get("surf_labels", [])
            
            if not (furn_verts and furn_faces):
                continue
            
            furn_mesh = bpy.data.meshes.new(furn_id)
            furn_mesh.from_pydata(furn_verts, [], furn_faces)
            
            furn_obj = bpy.data.objects.new(furn_mesh.name, furn_mesh)
            furn_obj.parent = room_empty
            coll.objects.link(furn_obj)
            
            furn_obj["cgml3_feature"] = "BuildingFurniture"
            furn_obj["gml_id"] = furn_id
            furn_obj["gml_name"] = furn_name
            furn_obj["structure_part"] = "furniture"
            
            _apply_surface_labels_to_mesh(furn_mesh, furn_labels, furn_obj)
            id_to_obj[furn_id] = furn_obj
            
            # (debug removed) created furniture
        
        # Installations
        installations_data = room_info.get("installations", {})
        for inst_id, inst_info in installations_data.items():
            inst_name = inst_info.get("name", inst_id)
            inst_verts = inst_info.get("verts", [])
            inst_faces = inst_info.get("faces", [])
            inst_labels = inst_info.get("surf_labels", [])
            
            if not (inst_verts and inst_faces):
                continue
            
            inst_mesh = bpy.data.meshes.new(inst_id)
            inst_mesh.from_pydata(inst_verts, [], inst_faces)
            
            inst_obj = bpy.data.objects.new(inst_mesh.name, inst_mesh)
            inst_obj.parent = room_empty
            coll.objects.link(inst_obj)
            
            inst_obj["cgml3_feature"] = "IntBuildingInstallation"
            inst_obj["gml_id"] = inst_id
            inst_obj["gml_name"] = inst_name
            inst_obj["structure_part"] = "installation"
            
            _apply_surface_labels_to_mesh(inst_mesh, inst_labels, inst_obj)
            id_to_obj[inst_id] = inst_obj
            
            # (debug removed) created installation
        
        # Openings (Doors/Windows)
        openings_data = room_info.get("openings", {})
        for opening_id, opening_info in openings_data.items():
            opening_name = opening_info.get("name", opening_id)
            opening_type = opening_info.get("opening_type", "Opening")
            opening_verts = opening_info.get("verts", [])
            opening_faces = opening_info.get("faces", [])
            opening_labels = opening_info.get("surf_labels", [])
            
            # Surface Mapping
            surface_id = opening_info.get("surface_id")
            surface_type = opening_info.get("surface_type")
            surface_name = opening_info.get("surface_name")
            xlink_refs = opening_info.get("xlink_refs", [])
            
            if not (opening_verts and opening_faces) and not xlink_refs:
                continue
            
            if opening_verts and opening_faces:
                opening_mesh = bpy.data.meshes.new(opening_id)
                opening_mesh.from_pydata(opening_verts, [], opening_faces)
                
                opening_obj = bpy.data.objects.new(opening_mesh.name, opening_mesh)
            else:
                # Xlink-based Opening: only EMPTY (geometry is on the counterpart)
                opening_obj = bpy.data.objects.new(opening_id, None)
                opening_obj.empty_display_type = 'PLAIN_AXES'
                opening_obj.empty_display_size = 0.3
            
            opening_obj.parent = room_empty
            coll.objects.link(opening_obj)
            
            opening_obj["cgml3_feature"] = opening_type
            opening_obj["gml_id"] = opening_id
            opening_obj["gml_name"] = opening_name
            opening_obj["structure_part"] = "opening"
            opening_obj["opening_type"] = opening_type
            
            # Store Surface Mapping for Export
            if surface_id:
                opening_obj["opening_surface_id"] = surface_id
            if surface_type:
                opening_obj["opening_surface_type"] = surface_type
            if surface_name:
                opening_obj["opening_surface_name"] = surface_name
            
            if xlink_refs:
                import json
                opening_obj["xlink_refs"] = json.dumps(xlink_refs)
            
            if opening_obj.type == 'MESH':
                _apply_surface_labels_to_mesh(opening_obj.data, opening_labels, opening_obj)
            id_to_obj[opening_id] = opening_obj
            
            # (debug removed) created opening
    
    return building_empty


def _apply_surface_labels_to_mesh(mesh, surf_labels, obj):
    """
    Wendet Surface-Labels auf Mesh an (als Custom Properties auf Faces).
    surf_labels Format: [(surf_type, poly_id, ring_id, srs, vert_indices, lod_num), ...]
    """
    if not surf_labels:
        return
    
    # Collect unique surface types
    surf_types = set()
    for label_entry in surf_labels:
        surf_type = label_entry[0]
        surf_types.add(surf_type)
    
    # Store surface types as custom property
    if surf_types:
        obj["surface_types"] = ", ".join(sorted(surf_types))
    
    # Store face-level data (if needed)
    # TODO: BMesh-based face custom properties


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

    # Lifespan fields from core:AbstractFeatureWithLifespan
    try:
        for tag in ("creationDate", "terminationDate", "validFrom", "validTo"):
            dtxt = feat_el.findtext(f".//core:{tag}", namespaces=NS)
            if dtxt and dtxt.strip():
                obj[f"core:{tag}"] = dtxt.strip()
    except Exception:
        pass

    # Generic attributes + Feature-type-specific attributes
    try:
        attrs = _parse_generic_attributes(feat_el)
        # Add feature-type-specific attributes (Bridge, Tunnel, WaterBody, Vegetation, etc.)
        attrs.update(parse_all_feature_type_attributes(feat_el, NS))
        for k, v in attrs.items():
            try:
                obj[k] = v
            except (TypeError, ValueError):
                # Fallback: convert to string if type incompatible
                obj[k] = str(v)

        roof_type_el = feat_el.find(".//bldg:roofType", namespaces=NS)
        if roof_type_el is not None:
            obj["bldg:roofType"] = (roof_type_el.text or "").strip()
    except (AttributeError, KeyError):
        pass

def _feature_has_implicit_geometry(feat_el) -> bool:
    """
    Checks whether a (core:)ImplicitGeometry appears anywhere in the feature,
    regardless of whether it is used exclusively via lodXImplicitRepresentation
    or exists alongside lodXGeometry.
    """
    for el in feat_el.iter():
        tag = getattr(el, "tag", None)
        if not isinstance(tag, str):
            continue
        if localname(tag) == "ImplicitGeometry":
            return True
    return False


def import_citygml2_into_blender(
    path: str,
    context,
    *,
    gml_path_for_textures: str | None = None,
    import_appearance: bool = True,
):
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
    
    # Clear geometry cache from previous imports
    global GEOMETRY_CACHE, TEMPLATE_ID_BY_FP
    GEOMETRY_CACHE.clear()
    TEMPLATE_ID_BY_FP.clear()
    
    # Initialize progress tracking
    wm = context.window_manager if hasattr(context, 'window_manager') else None
    if wm:
        wm.progress_begin(0, 100)
        wm.progress_update(5)  # 5%: Starting import
    print_console_progress("Import is starting...", percentage=5)
    
    _IMPORT_CONTEXT["citygml_filepath"] = str(path)
    _IMPORT_CONTEXT["import_appearance"] = bool(import_appearance)
    try:
        if not isinstance(context, dict):
            _IMPORT_CONTEXT["mesh_validate"] = bool(context.scene.cgml3.mesh_validate)
    except Exception:
        _IMPORT_CONTEXT["mesh_validate"] = True

    # Check if streaming import should be used (threshold configurable in UI)
    # NOTE: Streaming has issues with element cleanup, so we use regular import for most files
    from .streaming_import import should_use_streaming
    try:
        threshold_mb = float(getattr(getattr(context, "scene", None), "cgml3", None).import_streaming_threshold_mb)
    except Exception:
        threshold_mb = 30.0
    use_streaming = should_use_streaming(path, threshold_mb=threshold_mb)
    
    if use_streaming:
        print("[CityGML2] Using streaming import for large file")
        # Streaming import for very large files (CityGML 2.0)
        return _import_citygml2_streaming(
            path,
            context,
            wm,
            gml_path_for_textures=gml_path_for_textures,
            import_appearance=import_appearance,
        )
    
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
    print_console_progress("XML file parsed", percentage=15)

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

    # [CityGML 2.0] Dynamizer module doesn't exist - skip owner mapping
    # Dynamizer-Owner-Mapping (vor dem Feature-Sammeln)
    # dyn_owner_by_id = _collect_dynamizer_owners(root)
    dyn_owner_by_id = {}  # Empty dict for CityGML 2.0 compatibility

    # Get filter settings from context (handle both dict and Blender context)
    gml_id_filter = None
    bbox_coords = None
    lod_filter = None
    feature_type_filter = None
    
    if isinstance(context, dict):
        # Called from __init__.py with options dict - no filters for CityGML 2.0 yet
        pass
    else:
        # Called with Blender context object
        if context.scene.cgml3.use_gmlid_filter:
            filter_str = context.scene.cgml3.gmlid_filter.strip()
            if filter_str.startswith('[') and filter_str.endswith(']'):
                filter_str = filter_str[1:-1]  # Remove square brackets
            gml_id_filter = _parse_gml_id_filter(filter_str)
        
        bbox_coords = resolve_scene_bbox_filter(context.scene.cgml3)
        
        # LOD filter
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
        if context.scene.cgml3.use_feature_type_filter:
            feature_type_filter = set()
            if context.scene.cgml3.import_buildings:
                feature_type_filter.update(["Building", "BuildingPart", "BuildingInstallation", "IntBuildingInstallation"])
            if context.scene.cgml3.import_bridges:
                feature_type_filter.update(["Bridge", "BridgePart"])
            if context.scene.cgml3.import_tunnels:
                feature_type_filter.update(["Tunnel", "TunnelPart"])
            if context.scene.cgml3.import_vegetation:
                feature_type_filter.update(["SolitaryVegetationObject", "PlantCover"])
            if context.scene.cgml3.import_water:
                feature_type_filter.update(["WaterBody", "WaterSurface"])
            if context.scene.cgml3.import_transportation:
                feature_type_filter.update(["Road", "Railway", "Track", "Square"])
            if context.scene.cgml3.import_cityfurniture:
                feature_type_filter.add("CityFurniture")
            if context.scene.cgml3.import_landuse:
                feature_type_filter.add("LandUse")
            if context.scene.cgml3.import_relief:
                feature_type_filter.update(["TINRelief", "MassPointRelief", "BreaklineRelief", "RasterRelief"])
            if context.scene.cgml3.import_generics:
                feature_type_filter.add("GenericCityObject")

    # Create the Appearance folder only if you want to import the Appearance.
    if import_appearance:
        appearance_dir = (Path(path).parent / "Appearance")
        try:
            appearance_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    coll = ensure_collection(context, f"CityGML2:{Path(path).stem}")

    # Sub-collections per feature type (Building, Bridge, SolitaryVegetationObject, ...)
    type_collections = {}

    def get_type_collection(feat_ln: str):
        """
        Returns a sub-collection within the main collection `coll`,
        named after the feature type (e.g., 'Building', 'Bridge', 'PlantCover', ...).
        Created once if needed and reused for subsequent objects.
        """
        name = feat_ln or "UnknownType"

        # already cached?
        sub = type_collections.get(name)
        if sub is not None:
            return sub

        # check if a child collection with this name already exists under coll
        for c in coll.children:
            if c.name == name:
                sub = c
                break

        # if not found: create a new collection and link it under coll
        if sub is None:
            sub = bpy.data.collections.new(name)
            coll.children.link(sub)

        type_collections[name] = sub
        return sub

    # srs from the root attribute; for many GMLs, only the Envelope contains it
    default_srs = root.get("srsName")
    # Read Envelope SRS and corner
    env_srs, lower_corner = _get_envelope_srs_and_corners(root)

    # Extract vertical EPSG (Compound-CRS) and write to World
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
                w["Z-EPSG"] = z_epsg  # automatically set
            # Z-Origin from Envelope, if 3D
            if lower_corner and len(lower_corner) == 3:
                _set_world_prop_with_ui(w, "Z-Origin", float(lower_corner[2]))
        except Exception:
            pass

    # PERFORMANCE: Quick check if Appearance data exists
    has_appearance = False
    if import_appearance:
        for elem in root.iter():
            if elem.tag and isinstance(elem.tag, str) and elem.tag.endswith('Appearance'):
                has_appearance = True
                break

    if import_appearance and has_appearance:
        matcolor_by_surface_id = parse_x3d_materials(root)
        ptex_by_ring, poly_to_image, ptex_by_poly, uv_start_by_ring = parse_parameterized_textures(root, gml_path_for_textures or path)
        gtex_by_poly = parse_georeferenced_textures(root, path)
    else:
        # No Appearance data → use empty dicts (saves a lot of time)
        matcolor_by_surface_id = {}
        ptex_by_ring = {}
        poly_to_image = {}
        ptex_by_poly = {}
        uv_start_by_ring = {}
        gtex_by_poly = {}

    # Appearance grouping (Appearance gml:id + theme) for roundtrip export
    def _collect_poly_to_app_theme_citygml2(root_el):
        poly_to_app = {}
        theme_by_app = {}
        gml_id_attr = f"{{{NS['gml']}}}id"

        # All Appearance nodes (CityGML 2: app:appearanceMember/app:Appearance)
        for app_el in root_el.findall(".//app:Appearance", namespaces=NS):
            app_id = app_el.get(gml_id_attr)
            if not app_id:
                continue
            theme_txt = (app_el.findtext("./app:theme", default="", namespaces=NS) or "").strip()
            theme_by_app[app_id] = theme_txt

            # Targets from X3DMaterial + ParameterizedTexture (sufficient for the plugin)
            for t in app_el.findall(".//app:target", namespaces=NS):
                href = t.get("{http://www.w3.org/1999/xlink}href") or (t.text or "")
                pid = _norm_id(href.strip())
                if pid:
                    poly_to_app[pid] = app_id
            for t in app_el.findall(".//app:uri", namespaces=NS):
                href = t.get("{http://www.w3.org/1999/xlink}href") or (t.text or "")
                pid = _norm_id(href.strip())
                if pid:
                    poly_to_app[pid] = app_id

        return poly_to_app, theme_by_app

    if import_appearance:
        poly_to_app, theme_by_app = _collect_poly_to_app_theme_citygml2(root)
    else:
        poly_to_app, theme_by_app = {}, {}

    
    if wm:
        wm.progress_update(25)  # 25%: Appearance parsed
    if import_appearance:
        print_console_progress("Appearance-Daten verarbeitet", percentage=25)
    else:
        print_console_progress("Appearance-Import übersprungen", percentage=25)

    # Handle both dict context and Blender context
    if isinstance(context, dict):
        world = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
        bpy.context.scene.world = world
    else:
        world = context.scene.world or bpy.data.worlds.new("World")
        context.scene.world = world
    scene_georef_copied = False
    if not _is_local_import:
        scene_georef_copied = _copy_scene_georeference_to_world(context, world)
    file_origin, file_crs = _determine_file_origin_and_crs(root)
    if not _is_local_import:
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

    if _is_local_import:
        # Lokaler Import: Envelope lower corner als Offset, kein CRS in World
        if lower_corner and len(lower_corner) >= 2:
            ref_origin = (float(lower_corner[0]), float(lower_corner[1]),
                         float(lower_corner[2]) if len(lower_corner) == 3 else 0.0)
            print(f"[CityGML Import] Local import active - Offset from Envelope: {ref_origin}")
        else:
            ref_origin = file_origin
            print(f"[CityGML Import] Local import active - Offset from file_origin: {ref_origin}")
    else:
        ref_origin = (float(world["X-Origin"]), float(world["Y-Origin"]), float(world.get("Z-Origin", file_origin[2])))

    # Groups for implicit geometries:
    # template_id -> list of (feat_id, object, M_eff)
    implicit_groups = {}

    # Helper function: extract sortKey (int or large fallback)
    def _get_sort_key(el):
        try:
            s = el.findtext(".//bldg:sortKey", namespaces=NS)
            return int(s.strip()) if s and s.strip().isdigit() else 10**9
        except Exception:
            return 10**9

    # Spatial Index for BBOX filter (optional, significantly faster for large datasets)
    spatial_index = None
    gml_id_filter_from_spatial = None
    
    if bbox_coords:
        try:
            from .spatial_index import SpatialIndex, RTREE_AVAILABLE
            
            # Build spatial index if rtree available and beneficial
            feature_count_estimate = sum(1 for _ in root if str(getattr(_, 'tag', '')).endswith('cityObjectMember'))
            use_spatial_index = RTREE_AVAILABLE and feature_count_estimate > 500
            
            if use_spatial_index:
                print(f"Building spatial index for {feature_count_estimate} features...")
                # CityGML 2.0 namespaces
                ns_2_0 = {
                    'gml': 'http://www.opengis.net/gml',
                    'core': 'http://www.opengis.net/citygml/2.0',
                    'bldg': 'http://www.opengis.net/citygml/building/2.0',
                }
                spatial_index = SpatialIndex.from_gml_file(path, namespaces=ns_2_0)
                
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
    for member in root:
        tag = member.tag if isinstance(member.tag, str) else ""
        if tag == "cityObjectMember" or tag.endswith("}cityObjectMember"):
            for child in member:
                if isinstance(child.tag, str) and child.tag.startswith("{"):
                    total_features_before_filter += 1
    
    # Initialize Dynamic Namespace Manager for CityGML 2.0
    ns_manager = None
    try:
        from ...common.dynamic_namespaces import DynamicNamespaceManager
        ns_manager = DynamicNamespaceManager(root)
        print("[CityGML2] Using dynamic namespace detection")
        if context.scene.cgml3.export_debug:
            print(ns_manager.get_info_string())
    except Exception as e:
        print(f"[CityGML2] Dynamic namespace detection failed: {e}")
        print("[CityGML2] Falling back to static namespaces")
    
    # Pass gml_id_filter (potentially enriched by spatial index) and bbox_coords for double-check
    feats = list(iter_features(root, gml_id_filter=gml_id_filter, bbox_filter=bbox_coords if not spatial_index else None, feature_type_filter=feature_type_filter, ns_manager=ns_manager))
    feats.sort(key=_get_sort_key)
    
    total_feats = len(feats)
    features_filtered = total_features_before_filter - total_feats
    if wm:
        wm.progress_update(30)  # 30%: Features collected
    print_console_progress("Features collected", percentage=30)

    # Global mappings for subsequent relationships
    id_to_obj = {}          # gml:id -> Blender object
    building_part_edges = []  # List of (parent_building_id, part_id)
    building_installation_edges = []  # List of (parent_building_id, inst_id)
    building_furniture_edges = []  # List of (parent_id, furn_id)
    bridge_part_edges = []    # List of (parent_bridge_id, part_id)
    bridge_installation_edges = []  # List of (parent_bridge_id, inst_id)
    bridge_furniture_edges = []  # List of (parent_bridge_id, furn_id)
    tunnel_part_edges = []    # List of (parent_tunnel_id, part_id)
    tunnel_installation_edges = []  # List of (parent_tunnel_id, inst_id)
    tunnel_furniture_edges = []  # List of (parent_tunnel_id, furn_id)
    group_defs = []         # CityObjectGroup-Infos
    version_defs = []       # Version-Infos
    transition_defs = []    # VersionTransition-Infos
    dynamizer_defs = []     # Dynamizer-Owner-Infos 
    xlink_href = "{http://www.w3.org/1999/xlink}href"

    # Pre-scan: Part relationships (CityGML 2.0)
    # This allows us to establish correct parenting after importing the objects,
    # without duplicating geometry and without import order constraints.
    try:
        gml_id_attr = f"{{{NS['gml']}}}id"

        for b in root.findall(".//bldg:Building", NS):
            parent_id = b.get(gml_id_attr)
            if not parent_id:
                continue
            for rel in b.findall("./bldg:consistsOfBuildingPart", NS):
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
            for rel in b.findall("./bldg:outerBuildingInstallation", NS):
                inst_el = rel.find("./bldg:BuildingInstallation", NS)
                if inst_el is None:
                    continue
                inst_id = inst_el.get(gml_id_attr)
                if inst_id:
                    building_installation_edges.append((parent_id, inst_id))
            for rel in b.findall("./bldg:interiorBuildingInstallation", NS):
                inst_el = rel.find("./bldg:IntBuildingInstallation", NS)
                if inst_el is None:
                    continue
                inst_id = inst_el.get(gml_id_attr)
                if inst_id:
                    building_installation_edges.append((parent_id, inst_id))

        for b in root.findall(".//bldg:Building", NS) + root.findall(".//bldg:BuildingPart", NS):
            parent_id = b.get(gml_id_attr)
            if not parent_id:
                continue
            for rel in b.findall("./bldg:interiorFurniture", NS):
                furn_el = rel.find("./bldg:BuildingFurniture", NS)
                if furn_el is None:
                    continue
                furn_id = furn_el.get(gml_id_attr)
                if furn_id:
                    building_furniture_edges.append((parent_id, furn_id))

        for br in root.findall(".//brid:Bridge", NS) + root.findall(".//brid:BridgePart", NS):
            parent_id = br.get(gml_id_attr)
            if not parent_id:
                continue
            for rel in br.findall("./brid:consistsOfBridgePart", NS):
                part_el = rel.find("./brid:BridgePart", NS)
                if part_el is None:
                    continue
                part_id = part_el.get(gml_id_attr)
                if part_id:
                    bridge_part_edges.append((parent_id, part_id))
            for rel in br.findall("./brid:outerBridgeInstallation", NS):
                inst_el = rel.find("./brid:BridgeInstallation", NS)
                if inst_el is None:
                    continue
                inst_id = inst_el.get(gml_id_attr)
                if inst_id:
                    bridge_installation_edges.append((parent_id, inst_id))
            for rel in br.findall("./brid:interiorBridgeInstallation", NS):
                inst_el = rel.find("./brid:IntBridgeInstallation", NS)
                if inst_el is None:
                    continue
                inst_id = inst_el.get(gml_id_attr)
                if inst_id:
                    bridge_installation_edges.append((parent_id, inst_id))
            for rel in br.findall("./brid:interiorBridgeFurniture", NS):
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
            for rel in tn.findall("./tun:consistsOfTunnelPart", NS):
                part_el = rel.find("./tun:TunnelPart", NS)
                if part_el is None:
                    continue
                part_id = part_el.get(gml_id_attr)
                if part_id:
                    tunnel_part_edges.append((parent_id, part_id))
            for rel in tn.findall("./tun:outerTunnelInstallation", NS):
                inst_el = rel.find("./tun:TunnelInstallation", NS)
                if inst_el is None:
                    continue
                inst_id = inst_el.get(gml_id_attr)
                if inst_id:
                    tunnel_installation_edges.append((parent_id, inst_id))
            for rel in tn.findall("./tun:interiorTunnelInstallation", NS):
                inst_el = rel.find("./tun:IntTunnelInstallation", NS)
                if inst_el is None:
                    continue
                inst_id = inst_el.get(gml_id_attr)
                if inst_id:
                    tunnel_installation_edges.append((parent_id, inst_id))
            for rel in tn.findall("./tun:interiorTunnelFurniture", NS):
                furn_el = rel.find("./tun:TunnelFurniture", NS)
                if furn_el is None:
                    continue
                furn_id = furn_el.get(gml_id_attr)
                if furn_id:
                    tunnel_furniture_edges.append((parent_id, furn_id))
    except Exception as _prescan_err:
        print(f"[DEBUG prescan] Exception during pre-scan: {_prescan_err}")
        building_part_edges = []
        building_installation_edges = []
        building_furniture_edges = []
        bridge_part_edges = []
        bridge_installation_edges = []
        bridge_furniture_edges = []
        tunnel_part_edges = []
        tunnel_installation_edges = []
        tunnel_furniture_edges = []

    print(f"[DEBUG prescan] building_part_edges: {building_part_edges}")
    print(f"[DEBUG prescan] building_installation_edges: {building_installation_edges}")

    for feat in feats:
        feat_ln = feat.tag.split("}")[-1]
        feat_id = feat.get(f"{{{NS['gml']}}}id") or f"feat_{abs(hash(ET.tostring(feat)))}"
        
        # Update progress (30% to 90% for feature processing)
        if wm and total_feats > 0:
            feat_idx = feats.index(feat)
            progress = 30 + int((feat_idx / total_feats) * 60)
            wm.progress_update(progress)
            print_console_progress("Processing features", current=feat_idx+1, total=total_feats)

        # NEW: Treat CityObjectGroup as an empty without a mesh
        feat_tag = feat.tag
        feat_uri = feat_tag.split("}")[0].strip("{") if isinstance(feat_tag, str) and "}" in feat_tag else ""
        if feat_uri == NS.get("grp") and feat_ln == "CityObjectGroup":
            # Create empty
            grp_obj = bpy.data.objects.new(feat_id, None)
            grp_obj.empty_display_type = 'PLAIN_AXES'
            get_type_collection(feat_ln).objects.link(grp_obj)

            grp_obj["cgml3_feature"] = feat_ln
            grp_obj["gml_id"] = feat_id

            # optional: use gml:name as object name
            try:
                name_txt = feat.findtext(".//gml:name", namespaces=NS)
                if name_txt and name_txt.strip():
                    grp_obj.name = name_txt.strip()
            except Exception:
                pass

            # inherit grp:class / grp:function / grp:usage
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

            # Group members (xlink:href in grp:groupMember)
            member_ids = []
            for gm in feat.findall(".//grp:groupMember", NS):
                href = gm.get(xlink_href)
                if not href:
                    continue
                href = href.strip()
                if href.startswith("#"):
                    href = href[1:]
                if href:
                    member_ids.append(href)

            # Parent reference (grp:parent@xlink:href), if available
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

            # inherit generic attributes of the group
            gen_attrs = _parse_generic_attributes(feat)
            if gen_attrs is None:
                gen_attrs = {}
            for k, v in gen_attrs.items():
                try:
                    grp_obj[k] = v
                except Exception:
                    grp_obj[k] = str(v)

            # register group in mapping and remember for second phase
            id_to_obj[feat_id] = grp_obj
            group_defs.append((grp_obj, member_ids, parent_id))

            # CityObjectGroup gets no mesh → next feature
            continue

        # PointCloud does not exist in CityGML 2.0 - skip
        if feat_uri == NS.get("pcl") and feat_ln == "PointCloud":
            print(f"[CityGML 2.0] Skipping PointCloud feature (not in CityGML 2.0): {feat_id}")
            continue

        # [CityGML 2.0] Versioning module doesn't exist - skip if encountered
        if feat_uri == NS.get("vers"):
            print(f"[CityGML 2.0] Skipping Versioning feature: {feat_ln} (not supported in CityGML 2.0)")
            continue

            id_to_obj[feat_id] = trans_obj
            transition_defs.append((trans_obj, from_id, to_id))

            # no geometry
            continue

        # [CityGML 2.0] Dynamizer module doesn't exist - skip if encountered
        if feat_uri == NS.get("dyn"):
            print(f"[CityGML 2.0] Skipping Dynamizer feature: {feat_ln} (not supported in CityGML 2.0)")
            continue

            # no geometry for Dynamizer
            continue

        # NORMAL GEOMETRY PATH (all other features)

        # If this feature comes exclusively via an ImplicitGeometry,
        # we build the mesh as a template (locally) and apply the complete affine transformation
        # (including individual scaling) at the object level.
        tpl_id, M_eff, implicit_lod_level = _implicit_instance_info(feat, ref_origin)
        is_implicit_instance = bool(tpl_id and M_eff is not None)
        
        # HIERARCHISCHER IMPORT für Buildings mit interiorRoom
        # Prüfe, ob dies ein Building mit Rooms ist
        use_hierarchical = (feat_ln in ("Building", "BuildingPart") and 
                           len(feat.findall(".//bldg:Room", NS)) > 0)
        
        if use_hierarchical:
            # Nutze hierarchischen Import
            
            hier_data = build_geometry_hierarchical(
                feat, default_srs, ref_origin, 
                include_surface_attrs=True, 
                lod_filter=lod_filter, 
                bake_implicit=(not is_implicit_instance)
            )
            
            # Erstelle hierarchische Objekt-Struktur
            building_obj = _create_hierarchical_building_objects(
                feat, feat_id, feat_ln, hier_data, ref_origin, default_srs, coll, id_to_obj
            )
            
            # Wende Materialien auf alle erstellten Objekte an
            # Sammle alle Mesh-Objekte in der Hierarchie mit ihren surf_labels
            mesh_objects_with_labels = []
            
            # Outer Shell
            outer_data = hier_data.get("outer_shell", {})
            if building_obj.children:
                for child in building_obj.children:
                    if child.get("structure_part") == "outer_shell" and child.type == 'MESH':
                        mesh_objects_with_labels.append((child, outer_data.get("surf_labels", [])))
            
            # Outer Shell Openings (Doors/Windows)
            outer_openings = outer_data.get("openings", {})
            for child in building_obj.children:
                if child.get("structure_part") == "opening" and child.type == 'MESH':
                    oid = child.get("gml_id")
                    if oid and oid in outer_openings:
                        mesh_objects_with_labels.append((child, outer_openings[oid].get("surf_labels", [])))
            
            # Rooms und deren Children
            rooms_data = hier_data.get("rooms", {})
            for room_id, room_info in rooms_data.items():
                # Finde Room-Objekte
                for child in building_obj.children:
                    if child.get("gml_id") == room_id:
                        # Room-Geometrie
                        for room_child in child.children:
                            if room_child.get("structure_part") == "room_geometry" and room_child.type == 'MESH':
                                mesh_objects_with_labels.append((room_child, room_info.get("surf_labels", [])))
                            
                            # Furniture
                            furn_id = room_child.get("gml_id")
                            if furn_id and room_child.get("structure_part") == "furniture" and room_child.type == 'MESH':
                                furn_data = room_info.get("furniture", {}).get(furn_id, {})
                                mesh_objects_with_labels.append((room_child, furn_data.get("surf_labels", [])))
                            
                            # Installations
                            if furn_id and room_child.get("structure_part") == "installation" and room_child.type == 'MESH':
                                inst_data = room_info.get("installations", {}).get(furn_id, {})
                                mesh_objects_with_labels.append((room_child, inst_data.get("surf_labels", [])))
                            
                            # Room Openings
                            if furn_id and room_child.get("structure_part") == "opening" and room_child.type == 'MESH':
                                room_openings = room_info.get("openings", {})
                                if furn_id in room_openings:
                                    mesh_objects_with_labels.append((room_child, room_openings[furn_id].get("surf_labels", [])))
            
            # Material-Anwendung
            for mesh_obj, obj_surf_labels in mesh_objects_with_labels:
                if mesh_obj.data and obj_surf_labels:
                    try:
                        # Store per-ring UV start points
                        try:
                            uv_map = dict(uv_start_by_ring) if isinstance(uv_start_by_ring, dict) else {}
                            if not uv_map and isinstance(ptex_by_ring, dict):
                                for rid, entry in ptex_by_ring.items():
                                    try:
                                        uvs = entry.get("uv") if isinstance(entry, dict) else None
                                        if uvs and isinstance(uvs, list) and len(uvs) >= 1:
                                            uv0 = uvs[0]
                                            uv_map[str(rid)] = (float(uv0[0]), float(uv0[1]))
                                    except Exception:
                                        continue
                            mesh_obj.data["cgml3_uv_start_by_ring"] = uv_map
                        except Exception:
                            pass
                        
                        apply_materials_uvs(
                            mesh_obj,
                            mesh_obj.data,
                            obj_surf_labels,
                            matcolor_by_surface_id,
                            ptex_by_ring,
                            gtex_by_poly,
                            poly_to_image,
                            path,
                            ptex_by_poly,
                            hier_data.get("poly_surface_attrs", {}),
                            hier_data.get("surface_ids", {}),
                            hier_data.get("multisurface_ids", {}),
                            hier_data.get("compositesurface_ids", {}),
                        )
                    except Exception as e:
                        import sys
                        print(f"[WARNING] Material application failed for {mesh_obj.name}: {e}", file=sys.stderr)
            
            # Überspringe restliche Standard-Verarbeitung
            continue
        
        # STANDARD IMPORT (nicht-hierarchisch)
        (
            faces,
            verts,
            surf_labels,
            surf_attrs_by_poly,
            surface_id_by_poly,
            multisurface_id_by_poly,
            compositesurface_id_by_poly,
        ) = build_geometry(
            feat,
            default_srs,
            ref_origin,
            include_surface_attrs=True,
            lod_filter=lod_filter,
            bake_implicit=(not is_implicit_instance),
            exclude_subtrees=citygml2_subfeature_excludes(feat, feat_ln),
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

                # Store per-object CRS (prefer point's srs, else infer from feature)
                try:
                    obj_crs = _normalize_epsg(srs) if srs else "Unknown CRS"
                except Exception:
                    obj_crs = "Unknown CRS"
                if obj_crs == "Unknown CRS":
                    obj_crs = _infer_feature_crs(feat)
                if obj_crs != "Unknown CRS":
                    empty_obj["CRS"] = obj_crs
                
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

                # Store per-object CRS (use first point srs if available, else infer)
                try:
                    first_srs = points[0][4] if points and len(points[0]) >= 5 else ""
                except Exception:
                    first_srs = ""
                try:
                    obj_crs = _normalize_epsg(first_srs) if first_srs else "Unknown CRS"
                except Exception:
                    obj_crs = "Unknown CRS"
                if obj_crs == "Unknown CRS":
                    obj_crs = _infer_feature_crs(feat)
                if obj_crs != "Unknown CRS":
                    obj["CRS"] = obj_crs
                
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
            print(f"[DEBUG import] SKIP (no faces): feat_ln={feat_ln}, feat_id={feat_id}")
            continue
        print(f"[DEBUG import] OK faces={len(faces)}: feat_ln={feat_ln}, feat_id={feat_id}")

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
        
        if geom_hash and geom_hash in GEOMETRY_CACHE:
            # Reuse existing mesh (instance sharing)
            mesh = GEOMETRY_CACHE[geom_hash]
            obj = bpy.data.objects.new(feat_id, mesh)
            is_mesh_instance = True
        else:
            # Create new mesh
            mesh = bpy.data.meshes.new(feat_id)
            mesh.from_pydata(verts, [], faces)
            obj = bpy.data.objects.new(mesh.name, mesh)
            is_mesh_instance = False
            
            # Cache for future reuse (only for non-trivial meshes)
            if geom_hash and len(faces) > 5:
                GEOMETRY_CACHE[geom_hash] = mesh

        get_type_collection(feat_ln).objects.link(obj)

        # Implizite Instanz: Affin-Transformation (inkl. individueller Skalierung) auf Objekt-Ebene setzen
        if is_implicit_instance:
            try:
                obj.matrix_world = M_eff
            except Exception:
                pass

        # Mapping gml:id → Objekt
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

        # UVs und Materialien zuweisen, solange Face-Reihenfolge noch 1:1 zu surf_labels passt
        # Skip UV/material assignment for mesh instances (already set on original)
        if not is_mesh_instance:
            # Store per-ring UV start points (bit-identical TexCoordList roundtrip).
            try:
                uv_map = dict(uv_start_by_ring) if isinstance(uv_start_by_ring, dict) else {}
                # Fallback: if parser didn't populate uv_start_by_ring, derive from ptex_by_ring entries.
                if not uv_map and isinstance(ptex_by_ring, dict):
                    for rid, entry in ptex_by_ring.items():
                        try:
                            uvs = entry.get("uv") if isinstance(entry, dict) else None
                            if uvs and isinstance(uvs, list) and len(uvs) >= 1:
                                uv0 = uvs[0]
                                uv_map[str(rid)] = (float(uv0[0]), float(uv0[1]))
                        except Exception:
                            continue
                mesh["cgml3_uv_start_by_ring"] = uv_map
            except Exception:
                pass
            apply_materials_uvs(
                obj,
                mesh,
                surf_labels,
                matcolor_by_surface_id,
                ptex_by_ring,
                gtex_by_poly,
                poly_to_image,
                path,
                ptex_by_poly,
                surf_attrs_by_poly,
                surface_id_by_poly,
                multisurface_id_by_poly,
                compositesurface_id_by_poly,
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

        obj["cgml3_feature"] = feat_ln
        obj["gml_id"] = feat_id

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

        # [CityGML 2.0] con:isStructuralElement doesn't exist - skip
        # Store structural element info if exists (CityGML 3.0 only)
        # try:
        #     is_structural = feat.findtext(".//con:isStructuralElement", namespaces=NS)
        #     if is_structural is not None:
        #         obj["con:isStructuralElement"] = is_structural.lower() == "true"
        # except Exception:
        #     pass

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
                
            # Preserve empty roofType elements so imports keep the original
            # attribute presence for roundtrips.
            roof_type_el = feat.find(".//bldg:roofType", namespaces=NS)
            if roof_type_el is not None:
                obj["bldg:roofType"] = (roof_type_el.text or "").strip()
                
            # Store yearOfConstruction if exists
            yearOfConstruction = feat.findtext(".//bldg:yearOfConstruction", namespaces=NS)
            if yearOfConstruction and yearOfConstruction.strip():
                obj["bldg:yearOfConstruction"] = int(yearOfConstruction.strip())
                
            # Store yearOfDemolition if exists
            yearOfDemolition = feat.findtext(".//bldg:yearOfDemolition", namespaces=NS)
            if yearOfDemolition and yearOfDemolition.strip():
                obj["bldg:yearOfDemolition"] = int(yearOfDemolition.strip())
                
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
        
        # Import Bridge-specific attributes (CityGML 2.0)
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
        
        # Import Tunnel-specific attributes (CityGML 2.0)
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
        
        # Import BridgeRoom-specific attributes (CityGML 2.0)
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
        
        # Import BridgeFurniture-specific attributes (CityGML 2.0)
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
        
        # Import BridgeInstallation-specific attributes (CityGML 2.0)
        if feat_ln in ("BridgeInstallation", "IntBridgeInstallation"):
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
        
        # Import BuildingFurniture-specific attributes (CityGML 2.0)
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
        
        # Import TunnelInstallation-specific attributes (CityGML 2.0)
        if feat_ln in ("TunnelInstallation", "IntTunnelInstallation"):
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
        
        # Import TunnelFurniture-specific attributes (CityGML 2.0)
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
        
        # Import WaterBody-specific attributes (CityGML 2.0)
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
        
        # Import Vegetation-specific attributes (CityGML 2.0)
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
        
        # Import CityFurniture-specific attributes (CityGML 2.0)
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
        
        # [CityGML 2.0] con:height structure doesn't exist - skip
        # Import con:height structure (CityGML 3.0 only)
        # try:
        #     height_el = feat.find(".//con:height/con:Height", namespaces=NS)
        #     if height_el is not None:
        #         high_ref = height_el.findtext(".//con:highReference", namespaces=NS)
        #         if high_ref and high_ref.strip():
        #             obj["con:height:highReference"] = high_ref.strip()
        #         
        #         low_ref = height_el.findtext(".//con:lowReference", namespaces=NS)
        #         if low_ref and low_ref.strip():
        #             obj["con:height:lowReference"] = low_ref.strip()
        #         
        #         status = height_el.findtext(".//con:status", namespaces=NS)
        #         if status and status.strip():
        #             obj["con:height:status"] = status.strip()
        #         
        #         value_el = height_el.find(".//con:value", namespaces=NS)
        #         if value_el is not None and value_el.text and value_el.text.strip():
        #             obj["con:height:value"] = float(value_el.text.strip())
        #             uom = value_el.get("uom")
        #             if uom:
        #                 obj["con:height:uom"] = uom
        # except Exception:
        #     pass
        
        # Import bldg:address structure with full xAL 2.0 parsing
        try:
            address_el = feat.find(".//bldg:address/core:Address", namespaces=NS)
            if address_el is not None:
                # xAL Address structure - use comprehensive xAL parser
                xal_addr = address_el.find(".//core:xalAddress/xAL:Address", namespaces=NS)
                if xal_addr is not None:
                    from ...shared.xal_parser import parse_xal_address
                    
                    # Parse xAL 2.0 (CityGML 2.0)
                    xal_data = parse_xal_address(xal_addr, NS, version="2.0")
                    
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
        if gen_attrs is None:
            gen_attrs = {}
        for k, v in gen_attrs.items():
            try:
                obj[k] = v
            except Exception:
                obj[k] = str(v)
        
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
                base_obj.matrix_world = base_Meff
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
                    obj.matrix_world = M_eff
                except Exception:
                    pass

                # nicht mehr benutzte Mesh-Datenblöcke aufräumen
                try:
                    if old_mesh and old_mesh.users == 0:
                        bpy.data.meshes.remove(old_mesh)
                except Exception:
                    pass

    # Nachträglich Part-Parenting herstellen (CityGML 2.0)
    # - Keine Geometrie-Duplikate: es werden nur Parent-Links gesetzt.
    # - Hierarchie bleibt erhalten für Export (consistsOf*Part).
    def _apply_part_parenting(edges):
        for parent_id, part_id in edges:
            parent_obj = id_to_obj.get(parent_id)
            part_obj = id_to_obj.get(part_id)
            if parent_obj is None or part_obj is None:
                print(f"[DEBUG parenting] SKIP edge ({parent_id} -> {part_id}): "
                      f"parent_obj={'FOUND' if parent_obj else 'MISSING'}, "
                      f"part_obj={'FOUND' if part_obj else 'MISSING'}")
                continue
            if parent_obj is part_obj:
                continue
            try:
                part_obj.parent = parent_obj
                print(f"[DEBUG parenting] OK: {part_obj.name} -> parent {parent_obj.name}")
            except Exception as e:
                print(f"[DEBUG parenting] FAILED: {part_obj.name} -> parent {parent_obj.name}: {e}")
            try:
                part_obj["cgml_parent_id"] = parent_id
            except Exception:
                pass

    print(f"[DEBUG parenting] id_to_obj keys ({len(id_to_obj)}): {list(id_to_obj.keys())[:20]}")
    if building_part_edges:
        print(f"[DEBUG parenting] Applying building_part_edges ({len(building_part_edges)} edges)")
        _apply_part_parenting(building_part_edges)
    if building_installation_edges:
        print(f"[DEBUG parenting] Applying building_installation_edges ({len(building_installation_edges)} edges)")
        _apply_part_parenting(building_installation_edges)
    if building_furniture_edges:
        _apply_part_parenting(building_furniture_edges)
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
    _disable_inline_subfeature_collections(coll)

    # Nachträglich Gruppen-Hierarchien in Blender herstellen
    for grp_obj, member_ids, parent_id in group_defs:
        # 1) Parent der Gruppe setzen (falls eine übergeordnete Gruppe oder ein Objekt referenziert ist)
        if parent_id:
            parent_obj = id_to_obj.get(parent_id)
            if parent_obj is not None and parent_obj is not grp_obj:
                try:
                    grp_obj.parent = parent_obj
                except Exception:
                    pass

        # 2) Eigene Collection für die Gruppe anlegen (unterhalb der Import-Collection)
        #    Name z.B. "CityGML2:<Dateiname>::Group:<gml_id>"
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
        for mid in member_ids:
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

    # [CityGML 2.0] Versioning module doesn't exist - skip version processing
    # Versionen als Collections + Rückverweise auf Versionen
    # for ver_obj, member_ids in version_defs:
    #     ver_id = ver_obj.get("gml_id", ver_obj.name)
    # 
    #     # eigene Collection für die Version
    #     coll_name = f"{coll.name}::Version::{ver_id}"
    #     ver_coll = bpy.data.collections.get(coll_name)
    #     if not ver_coll:
    #         ver_coll = bpy.data.collections.new(coll_name)
    #         if ver_coll not in coll.children:
    #             coll.children.link(ver_coll)
    # 
    #     # Version-Empty in die Versions-Collection
    #     if ver_obj.name not in ver_coll.objects:
    #         ver_coll.objects.link(ver_obj)
    # 
    #     # Mitglieder der Version zuordnen (nur Collection, kein Parenting)
    #     for mid in member_ids:
    #         memb_obj = id_to_obj.get(mid)
    #         if memb_obj is None or memb_obj is ver_obj:
    #             continue
    # 
    #         # in Versions-Collection aufnehmen
    #         if memb_obj.name not in ver_coll.objects:
    #             ver_coll.objects.link(memb_obj)
    # 
    #         # Rückverweis am Mitglied: Liste aller Versionen
    #         try:
    #             existing = list(memb_obj.get("vers:version_ids", []))
    #         except Exception:
    #             existing = []
    #         if ver_id not in existing:
    #             existing.append(ver_id)
    #             memb_obj["vers:version_ids"] = existing
    # 
    # # Transitionen an den beteiligten Versionen registrieren
    # for trans_obj, from_id, to_id in transition_defs:
    #     trans_id = trans_obj.get("gml_id", trans_obj.name)
    # 
    #     if from_id:
    #         from_obj = id_to_obj.get(from_id)
    #         if from_obj is not None and from_obj is not trans_obj:
    #             try:
    #                 out_list = list(from_obj.get("vers:transitions_out", []))
    #             except Exception:
    #                 out_list = []
    #             if trans_id not in out_list:
    #                 out_list.append(trans_id)
    #                 from_obj["vers:transitions_out"] = out_list
    # 
    #     if to_id:
    #         to_obj = id_to_obj.get(to_id)
    #         if to_obj is not None and to_obj is not trans_obj:
    #             try:
    #                 in_list = list(to_obj.get("vers:transitions_in", []))
    #             except Exception:
    #                 in_list = []
    #             if trans_id not in in_list:
    #                 in_list.append(trans_id)
    #                 to_obj["vers:transitions_in"] = in_list
    #             
    # # [CityGML 2.0] Dynamizer module doesn't exist - skip dynamizer processing
    # # Dynamizer-Owner-Verknüpfungen herstellen
    # for dyn_obj, owner_id in dynamizer_defs:
    #     if not owner_id:
    #         continue
    #     owner_obj = id_to_obj.get(owner_id)
    #     if owner_obj is None or owner_obj is dyn_obj:
    #         continue
    # 
    #     # Parent-Beziehung (Dynamizer → Owner-Objekt)
    #     try:
    #         dyn_obj.parent = owner_obj
    #     except Exception:
    #         pass


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


def _import_citygml2_streaming(
    path: str,
    context,
    wm=None,
    *,
    gml_path_for_textures: str | None = None,
    import_appearance: bool = True,
):
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
    Streaming import for large CityGML 2.0 files using iterparse.
    Similar to CityGML 3.0 streaming but adapted for CityGML 2.0 structure.
    """
    from pathlib import Path
    
    # Initial progress
    print_console_progress("Initialisierung...", percentage=0)
    from .streaming_import import iter_features_streaming
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
    
    # Setup collections
    coll = ensure_collection(context, f"CityGML2:{Path(path).stem}")
    type_collections = {}
    
    def get_type_collection(feat_ln: str):
        name = feat_ln or "UnknownType"
        sub = type_collections.get(name)
        if sub is not None:
            return sub
        for c in coll.children:
            if c.name == name:
                sub = c
                break
        if sub is None:
            sub = bpy.data.collections.new(name)
            coll.children.link(sub)
        type_collections[name] = sub
        return sub
    
    # World setup
    if isinstance(context, dict):
        world = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
        bpy.context.scene.world = world
    else:
        world = context.scene.world or bpy.data.worlds.new("World")
        context.scene.world = world
    
    # Get default SRS from root element (for coordinate transformations)
    default_srs = _normalize_srs_name(_get_root_srs_streaming(path))
    
    # Build GML ID lookup for xlink resolution (required for lod4Solid references)
    # For files using xlink:href references (like FZK-Haus), we need to parse the document
    # once to build the ID lookup. This is still memory-efficient for geometry processing.
    if wm:
        wm.progress_update(5)  # 5%: Building ID lookup
    
    try:
        from . import geometry as _geom_mod
        
        id_lookup = {}
        gml_id_attr = f"{{{NS['gml']}}}id"
        
        # Parse document to build ID lookup
        # This loads the tree structure but geometry processing is still done in streaming batches
        tree = ET.parse(path)
        root = tree.getroot()
        
        for el in root.iter():
            gid = el.get(gml_id_attr)
            if gid:
                id_lookup[gid] = el
        
        _geom_mod.GML_ID_LOOKUP = id_lookup
        print(f"[CityGML2 Streaming] Built GML ID lookup with {len(id_lookup)} entries")
    except Exception as e:
        print(f"[CityGML2 Streaming] Warning: Failed to build GML ID lookup: {e}")
        print(f"[CityGML2 Streaming] Error details: {type(e).__name__}")
        import traceback
        traceback.print_exc()
        try:
            from . import geometry as _geom_mod
            _geom_mod.GML_ID_LOOKUP = {}
        except:
            pass
        root = None
    
    # Determine origin from (root) gml:Envelope if available (we already parsed full tree for ID lookup).
    # This prevents huge coordinate offsets (e.g. UTM) from placing objects far from Blender origin.
    _is_local_import_s = False
    try:
        _sc_s = context.scene if not isinstance(context, dict) else bpy.context.scene
        _is_local_import_s = bool(getattr(_sc_s.cgml3, "import_local", False))
    except Exception:
        pass

    ref_origin = (0.0, 0.0, 0.0)
    env_srs = ""
    lower_corner = None
    scene_georef_copied = False
    try:
        env_srs, lower_corner = _get_envelope_srs_and_corners(root)
        if env_srs and not default_srs:
            default_srs = _normalize_srs_name(env_srs)
        if not _is_local_import_s:
            if lower_corner and len(lower_corner) == 3:
                _set_world_prop_with_ui(world, "X-Origin", float(lower_corner[0]))
                _set_world_prop_with_ui(world, "Y-Origin", float(lower_corner[1]))
                _set_world_prop_with_ui(world, "Z-Origin", float(lower_corner[2]))
    except Exception:
        pass

    if not _is_local_import_s:
        scene_georef_copied = _copy_scene_georeference_to_world(context, world)

    # Store CRS info for UI/debug
    if not _is_local_import_s:
        try:
            if default_srs and not scene_georef_copied:
                world["CRS"] = str(default_srs)
        except Exception:
            pass

    if _is_local_import_s:
        # Lokaler Import: Envelope lower corner als Offset, kein CRS in World
        if lower_corner and len(lower_corner) >= 2:
            ref_origin = (float(lower_corner[0]), float(lower_corner[1]),
                         float(lower_corner[2]) if len(lower_corner) == 3 else 0.0)
            print(f"[CityGML2 Streaming] Lokaler Import aktiv - Offset von Envelope: {ref_origin}")
        else:
            print(f"[CityGML2 Streaming] Lokaler Import aktiv - kein Envelope, Origin=(0,0,0)")
    elif "X-Origin" in world and "Y-Origin" in world:
        ref_origin = (float(world["X-Origin"]), float(world["Y-Origin"]), float(world.get("Z-Origin", 0.0)))
    else:
        # Fallback: Scene properties
        try:
            sc = context.scene if not isinstance(context, dict) else bpy.context.scene
            if sc and "crs x" in sc and "crs y" in sc:
                ref_origin = (float(sc["crs x"]), float(sc["crs y"]), float(sc.get("crs z", 0.0)))
        except Exception:
            pass
    
    # Parse appearance data using streaming (memory-efficient for large files)
    appearance_processor = None
    if import_appearance:
        print_console_progress("Appearance-Daten parsen...", percentage=10)
        if wm:
            wm.progress_update(10)  # 10%: Parsing appearance data

        x3d_by_poly, ptex_by_ring, poly_to_image, ptex_by_poly, poly_to_app, theme_by_app = parse_appearance_streaming(
            path,
            gml_path_for_textures or path,
        )
    else:
        if wm:
            wm.progress_update(10)
        x3d_by_poly = {}
        ptex_by_ring = {}
        poly_to_image = {}
        ptex_by_poly = {}
        poly_to_app = {}
        theme_by_app = {}
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
            feature_type_filter.update(["Building", "BuildingPart", "BuildingInstallation", "IntBuildingInstallation"])
        if context.scene.cgml3.import_bridges:
            feature_type_filter.update(["Bridge", "BridgePart"])
        if context.scene.cgml3.import_tunnels:
            feature_type_filter.update(["Tunnel", "TunnelPart"])
        if context.scene.cgml3.import_vegetation:
            feature_type_filter.update(["SolitaryVegetationObject", "PlantCover"])
        if context.scene.cgml3.import_water:
            feature_type_filter.update(["WaterBody", "WaterSurface"])
        if context.scene.cgml3.import_transportation:
            feature_type_filter.update(["Road", "Railway", "Track", "Square"])
        if context.scene.cgml3.import_cityfurniture:
            feature_type_filter.add("CityFurniture")
        if context.scene.cgml3.import_landuse:
            feature_type_filter.add("LandUse")
        if context.scene.cgml3.import_relief:
            feature_type_filter.update(["TINRelief", "MassPointRelief", "BreaklineRelief", "RasterRelief"])
        if context.scene.cgml3.import_generics:
            feature_type_filter.add("GenericCityObject")
    
    if wm:
        wm.progress_update(20)  # 20%: Setup complete
    
    print_console_progress("Setup abgeschlossen, starte Streaming...", percentage=20)
    
    # Stream features
    id_to_obj = {}
    building_part_edges = []  # List of (parent_building_id, part_id)
    building_installation_edges = []  # List of (parent_building_id, inst_id)
    building_furniture_edges = []  # List of (parent_id, furn_id)
    bridge_part_edges = []    # List of (parent_bridge_id, part_id)
    bridge_installation_edges = []  # List of (parent_bridge_id, inst_id)
    bridge_furniture_edges = []  # List of (parent_bridge_id, furn_id)
    tunnel_part_edges = []    # List of (parent_tunnel_id, part_id)
    tunnel_installation_edges = []  # List of (parent_tunnel_id, inst_id)
    tunnel_furniture_edges = []  # List of (parent_tunnel_id, furn_id)
    feat_count = 0
    feat_imported = 0
    
    # Error tracking
    error_tracker = ImportErrorTracker()
    
    # Memory management
    # OPTIMIZED: Cache auf 2000 Einträge erhöht für besseres Mesh-Sharing
    geometry_cache = LRUGeometryCache(max_entries=2000, max_memory_mb=500.0)
    memory_monitor = MemoryMonitor()
    
    # Batch processing optimization with auto-tuning
    feature_batch = []
    batch_size = auto_tune_batch_size(250)  # Erhöht von 100 für bessere Performance
    
    print(f"[CityGML2 Streaming] Starting feature iteration...")
    print(f"[CityGML2 Streaming] LOD filter: {lod_filter}")
    print(f"[CityGML2 Streaming] Feature type filter: {feature_type_filter}")
    
    try:
        for feat in iter_features_streaming(path, gml_id_filter, bbox_coords, feature_type_filter):
            feat_count += 1
            feature_batch.append(feat)

            # Streaming: Collecting Part Relationships (CityGML 2.0)
            # Part elements may appear inline under the parent.
            try:
                gml_id_attr = f"{{{NS['gml']}}}id"
                ln = feat.tag.split("}")[-1]
                parent_id = feat.get(gml_id_attr)
                if parent_id and ln in ("Building", "BuildingPart"):
                    for rel in feat.findall("./bldg:consistsOfBuildingPart", NS):
                        part_el = rel.find("./bldg:BuildingPart", NS)
                        if part_el is not None:
                            part_id = part_el.get(gml_id_attr)
                            if part_id:
                                building_part_edges.append((parent_id, part_id))
                    for rel in feat.findall("./bldg:outerBuildingInstallation", NS):
                        inst_el = rel.find("./bldg:BuildingInstallation", NS)
                        if inst_el is not None:
                            inst_id = inst_el.get(gml_id_attr)
                            if inst_id:
                                building_installation_edges.append((parent_id, inst_id))
                    for rel in feat.findall("./bldg:interiorBuildingInstallation", NS):
                        inst_el = rel.find("./bldg:IntBuildingInstallation", NS)
                        if inst_el is not None:
                            inst_id = inst_el.get(gml_id_attr)
                            if inst_id:
                                building_installation_edges.append((parent_id, inst_id))
                    for rel in feat.findall("./bldg:interiorFurniture", NS):
                        furn_el = rel.find("./bldg:BuildingFurniture", NS)
                        if furn_el is not None:
                            furn_id = furn_el.get(gml_id_attr)
                            if furn_id:
                                building_furniture_edges.append((parent_id, furn_id))
                if parent_id and ln in ("Bridge", "BridgePart"):
                    for rel in feat.findall("./brid:consistsOfBridgePart", NS):
                        part_el = rel.find("./brid:BridgePart", NS)
                        if part_el is not None:
                            part_id = part_el.get(gml_id_attr)
                            if part_id:
                                bridge_part_edges.append((parent_id, part_id))
                    for rel in feat.findall("./brid:outerBridgeInstallation", NS):
                        inst_el = rel.find("./brid:BridgeInstallation", NS)
                        if inst_el is not None:
                            inst_id = inst_el.get(gml_id_attr)
                            if inst_id:
                                bridge_installation_edges.append((parent_id, inst_id))
                    for rel in feat.findall("./brid:interiorBridgeInstallation", NS):
                        inst_el = rel.find("./brid:IntBridgeInstallation", NS)
                        if inst_el is not None:
                            inst_id = inst_el.get(gml_id_attr)
                            if inst_id:
                                bridge_installation_edges.append((parent_id, inst_id))
                    for rel in feat.findall("./brid:interiorBridgeFurniture", NS):
                        furn_el = rel.find("./brid:BridgeFurniture", NS)
                        if furn_el is not None:
                            furn_id = furn_el.get(gml_id_attr)
                            if furn_id:
                                bridge_furniture_edges.append((parent_id, furn_id))
                if parent_id and ln in ("Tunnel", "TunnelPart"):
                    for rel in feat.findall("./tun:consistsOfTunnelPart", NS):
                        part_el = rel.find("./tun:TunnelPart", NS)
                        if part_el is not None:
                            part_id = part_el.get(gml_id_attr)
                            if part_id:
                                tunnel_part_edges.append((parent_id, part_id))
                    for rel in feat.findall("./tun:outerTunnelInstallation", NS):
                        inst_el = rel.find("./tun:TunnelInstallation", NS)
                        if inst_el is not None:
                            inst_id = inst_el.get(gml_id_attr)
                            if inst_id:
                                tunnel_installation_edges.append((parent_id, inst_id))
                    for rel in feat.findall("./tun:interiorTunnelInstallation", NS):
                        inst_el = rel.find("./tun:IntTunnelInstallation", NS)
                        if inst_el is not None:
                            inst_id = inst_el.get(gml_id_attr)
                            if inst_id:
                                tunnel_installation_edges.append((parent_id, inst_id))
                    for rel in feat.findall("./tun:interiorTunnelFurniture", NS):
                        furn_el = rel.find("./tun:TunnelFurniture", NS)
                        if furn_el is not None:
                            furn_id = furn_el.get(gml_id_attr)
                            if furn_id:
                                tunnel_furniture_edges.append((parent_id, furn_id))
            except Exception:
                pass
            
            if feat_count == 1:
                feat_ln = feat.tag.split("}")[-1]
                feat_id = feat.get(f"{{{NS['gml']}}}id") or "unknown"
                print(f"[CityGML2 Streaming] First feature found: {feat_ln} (ID: {feat_id})")
            
            # Process batch when full or periodically update progress
            if len(feature_batch) >= batch_size or feat_count % 10 == 0:
                # Progress update (max 85% during feature processing)
                # Use logarithmic scaling for unknown total count
                # This gives smoother progress: fast initially, then slower
                progress_increment = 65 * (1 - 1 / (1 + feat_count / 50))
                progress_pct = min(20 + progress_increment, 85)
                print_console_progress(f"Features verarbeiten", feat_imported, feat_count, progress_pct)
                if wm:
                    progress = min(20 + int(progress_increment), 85)
                    wm.progress_update(progress)
                
                # Process accumulated batch
                if feature_batch:
                    batch_results = list(batch_process_geometries(
                        feature_batch, default_srs, ref_origin, lod_filter, batch_size, error_tracker
                    ))
                    
                    if feat_count <= 10:
                        print(f"[CityGML2 Streaming] Batch {feat_count//batch_size + 1}: {len(batch_results)} geometries extracted from {len(feature_batch)} features")
                    
                    for feat_elem, verts, faces, surf_labels, surf_attrs_by_poly, surface_id_by_poly, multisurface_id_by_poly, compositesurface_id_by_poly in batch_results:
                        feat_ln = feat_elem.tag.split("}")[-1]
                        feat_id = feat_elem.get(f"{{{NS['gml']}}}id") or f"feat_{abs(hash(ET.tostring(feat_elem)))}"
                        
                        sub_coll = get_type_collection(feat_ln)
                        
                        if not verts:
                            if feat_count <= 3:
                                print(f"[CityGML2 Streaming] Warning: No vertices for feature {feat_id} ({feat_ln})")
                            obj = bpy.data.objects.new(feat_id, None)
                            obj["gml_id"] = feat_id
                            obj["feature_type"] = feat_ln
                            obj["cgml3_feature"] = feat_ln
                            sub_coll.objects.link(obj)
                            id_to_obj[feat_id] = obj
                            continue
                        
                        if feat_count <= 3:
                            print(f"[CityGML2 Streaming] Creating mesh for {feat_id}: {len(verts)} verts, {len(faces)} faces")
                        
                        # Check for geometry deduplication with LRU cache
                        # PERFORMANCE: Nur hashen wenn Cache schon Einträge hat
                        geom_hash = None
                        mesh_data = None
                        
                        # LRUGeometryCache exposes `sizes` dict and `cache` OrderedDict; older code used `.size()`.
                        if len(getattr(geometry_cache, "cache", {})) > 0 and verts and faces:
                            geom_hash = _compute_geometry_hash(verts, faces)
                            if geom_hash:
                                mesh_data = geometry_cache.get(geom_hash)
                        
                        is_mesh_instance = mesh_data is not None
                        
                        if mesh_data is None:
                            # Create new mesh
                            mesh_data = bpy.data.meshes.new(feat_id)
                            mesh_data.from_pydata(verts, [], faces)
                            # mesh_data.update()  # Deaktiviert für Performance
                            
                            # Cache if significant size (using LRU cache)
                            if geom_hash and len(faces) > 5:
                                geometry_cache.put(geom_hash, mesh_data)
                            is_mesh_instance = False
                        else:
                            is_mesh_instance = True
                        
                        obj = bpy.data.objects.new(feat_id, mesh_data)
                        obj["gml_id"] = feat_id
                        obj["feature_type"] = feat_ln
                        obj["cgml3_feature"] = feat_ln
                        sub_coll.objects.link(obj)
                        id_to_obj[feat_id] = obj
                        feat_imported += 1
                        
                        # IMPORTANT: also run without textures, so that IDs / MultiSurface flags are set
                        # and export can correctly group.
                        if not is_mesh_instance:
                            # PATCH: always set material slots + surface metadata (even without appearance),
                            # otherwise e.g. is_multisurface_member / gml_multisurface_id are missing

                            # Set polygon custom properties (exporter uses poly.get("gml_id") / poly.get("app_id"))
                            try:
                                if surf_labels and len(mesh_data.polygons) == len(surf_labels):
                                    app_ids = set()
                                    for poly, pid in zip(mesh_data.polygons, surf_labels):
                                        if not pid:
                                            continue
                                        poly["gml_id"] = str(pid)
                                        aid = poly_to_app.get(str(pid)) if 'poly_to_app' in locals() else None
                                        if aid:
                                            poly["app_id"] = str(aid)
                                            app_ids.add(str(aid))
                                    if len(app_ids) == 1:
                                        only = next(iter(app_ids))
                                        obj["app_id"] = only
                                        theme = (theme_by_app.get(only) if 'theme_by_app' in locals() else None) or ""
                                        if theme:
                                            obj["app_theme"] = theme
                                    elif len(app_ids) > 1:
                                        obj["app_id_multi"] = ";".join(sorted(app_ids))
                            except Exception:
                                pass
                            apply_materials_uvs(
                                obj,
                                mesh_data,
                                surf_labels,
                                x3d_by_poly or {},     # matcolor_by_surface_id (streaming liefert poly->color)
                                ptex_by_ring or {},
                                {},                    # gtex_by_poly (streaming appearance liefert das aktuell nicht)
                                poly_to_image or {},
                                str(path),
                                ptex_by_poly or {},
                                surf_attrs_by_poly,
                                surface_id_by_poly,
                                multisurface_id_by_poly,
                                compositesurface_id_by_poly,
                            )
                        
                        _store_common_attributes(obj, feat_elem)
                    
                    # Clear batch
                    feature_batch = []
        
        # Process remaining features in batch
        if feature_batch:
            for feat_elem, verts, faces, surf_labels, surf_attrs_by_poly, surface_id_by_poly, multisurface_id_by_poly, compositesurface_id_by_poly in batch_process_geometries(
                feature_batch, default_srs, ref_origin, lod_filter, batch_size, error_tracker
            ):
                feat_ln = feat_elem.tag.split("}")[-1]
                feat_id = feat_elem.get(f"{{{NS['gml']}}}id") or f"feat_{abs(hash(ET.tostring(feat_elem)))}"
                
                sub_coll = get_type_collection(feat_ln)
                
                if not verts:
                    obj = bpy.data.objects.new(feat_id, None)
                    obj["gml_id"] = feat_id
                    obj["feature_type"] = feat_ln
                    obj["cgml3_feature"] = feat_ln
                    sub_coll.objects.link(obj)
                    id_to_obj[feat_id] = obj
                    continue
                
                # Check for geometry deduplication with LRU cache
                geom_hash = _compute_geometry_hash(verts, faces)
                mesh_data = None
                
                if geom_hash:
                    mesh_data = geometry_cache.get(geom_hash)
                
                is_mesh_instance = mesh_data is not None

                if mesh_data is None:
                    # Create new mesh
                    mesh_data = bpy.data.meshes.new(feat_id)
                    mesh_data.from_pydata(verts, [], faces)
                    mesh_data.update()
                    
                    # Cache if significant size (using LRU cache)
                    if geom_hash and len(faces) > 5:
                        geometry_cache.put(geom_hash, mesh_data)
                
                is_mesh_instance = False
                
                obj = bpy.data.objects.new(feat_id, mesh_data)
                obj["gml_id"] = feat_id
                obj["feature_type"] = feat_ln
                obj["cgml3_feature"] = feat_ln
                sub_coll.objects.link(obj)
                id_to_obj[feat_id] = obj
                feat_imported += 1
                
                # IMPORTANT: also run without textures, so that IDs / MultiSurface flags are set
                # and export can correctly group.
                if not is_mesh_instance:
                    # Set polygon custom properties (exporter uses poly.get("gml_id") / poly.get("app_id"))
                    try:
                        if surf_labels and len(mesh.polygons) == len(surf_labels):
                            app_ids = set()
                            for poly, pid in zip(mesh.polygons, surf_labels):
                                if not pid:
                                    continue
                                poly["gml_id"] = str(pid)
                                aid = poly_to_app.get(str(pid)) if 'poly_to_app' in locals() else None
                                if aid:
                                    poly["app_id"] = str(aid)
                                    app_ids.add(str(aid))
                            # Save additionally for the entire object (as a fallback if polygon properties are missing)
                            if len(app_ids) == 1:
                                only = next(iter(app_ids))
                                obj["app_id"] = only
                                theme = (theme_by_app.get(only) if 'theme_by_app' in locals() else None) or ""
                                if theme:
                                    obj["app_theme"] = theme
                            elif len(app_ids) > 1:
                                obj["app_id_multi"] = ";".join(sorted(app_ids))
                    except Exception:
                        pass

                    apply_materials_uvs(
                        obj,
                        mesh_data,
                        surf_labels,
                        x3d_by_poly or {},     # matcolor_by_surface_id (streaming liefert poly->color)
                        ptex_by_ring or {},
                        {},                    # gtex_by_poly (streaming appearance liefert das aktuell nicht)
                        poly_to_image or {},
                        str(path),
                        ptex_by_poly or {},
                        surf_attrs_by_poly,
                        surface_id_by_poly,
                        multisurface_id_by_poly,
                        compositesurface_id_by_poly,
                    )
                
                _store_common_attributes(obj, feat_elem)
                
                # Periodic memory monitoring and cleanup
                if feat_imported % 100 == 0:
                    memory_monitor.update()
                    if feat_imported % 500 == 0:
                        trigger_garbage_collection()

        # After importing all features: apply part-parenting
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

        print_console_progress("Build a hierarchy...", percentage=88)
        _apply_part_parenting(building_part_edges)
        _apply_part_parenting(building_installation_edges)
        _apply_part_parenting(building_furniture_edges)
        _apply_part_parenting(bridge_part_edges)
        _apply_part_parenting(bridge_installation_edges)
        _apply_part_parenting(bridge_furniture_edges)
        _apply_part_parenting(tunnel_part_edges)
        _apply_part_parenting(tunnel_installation_edges)
        _apply_part_parenting(tunnel_furniture_edges)
        _disable_inline_subfeature_collections(coll)
    
    except Exception as e:
        if wm:
            wm.progress_end()
        raise RuntimeError(f"Streaming import failed: {e}")
    
    # Summary
    print(f"\n[CityGML2 Streaming] Import Summary:")
    print(f"  Total features found: {feat_count}")
    print(f"  Successfully imported: {feat_imported}")
    print(f"  Features with geometry: {feat_imported}")
    print(f"  Empty features: {feat_count - feat_imported}")
    
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
    
    # Display memory statistics
    print_console_progress("Collecting statistics...", percentage=92)
    print()  # Newline
    print(get_memory_stats_summary(geometry_cache, memory_monitor))
    
    # Display appearance statistics
    if 'appearance_processor' in locals() and appearance_processor:
        print(get_parallel_stats_summary(appearance_processor))
    
    # Update import statistics (for streaming we can't count before filtering)
    if hasattr(context.scene, 'cgml3'):
        context.scene.cgml3.last_import_total = feat_count
        context.scene.cgml3.last_import_imported = feat_imported
        context.scene.cgml3.last_import_filtered = feat_count - feat_imported
    
    # Finalize Blender scene (this is what causes the "freeze" - force it now with progress)
    print_console_progress("Updating Blender View-Layer...", percentage=95)
    try:
        # Force view layer update to finalize all objects
        if hasattr(context, 'view_layer'):
            context.view_layer.update()
    except Exception as e:
        print(f"[Warning] View layer update failed: {e}")
    
    print_console_progress("Finalizing Dependency Graph...", percentage=97)
    try:
        # Force dependency graph evaluation
        if hasattr(context, 'evaluated_depsgraph_get'):
            context.evaluated_depsgraph_get()
    except Exception as e:
        print(f"[Warning] Depsgraph evaluation failed: {e}")
    
    print_console_progress("Finalizing scene...", percentage=99)
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
    print_console_progress("Import completed", percentage=100)
    print()  # Newline
    
    # Log import time
    elapsed_time = time.time() - start_time
    print(f"[CityGML Streaming Import] Completed in {elapsed_time:.2f} seconds ({elapsed_time/60:.1f} minutes)")
    print(f"\n{'='*60}")
    print(f"✓ IMPORT COMPLETED")
    print(f"  Imported Features: {feat_imported} of {feat_count}")
    print(f"  Blender is now up and running again.")
    print(f"{'='*60}\n")
