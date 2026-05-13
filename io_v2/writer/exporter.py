# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io/writer/exporter.py
from __future__ import annotations

import re
from collections import defaultdict

NON_EXPORT_GENERIC_ATTR_KEYS = {
    "image_path",
}

NON_EXPORT_GENERIC_ATTR_PREFIXES = (
    "apt_",
)


def _should_skip_generic_attr_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    if key in NON_EXPORT_GENERIC_ATTR_KEYS:
        return True
    return any(key.startswith(prefix) for prefix in NON_EXPORT_GENERIC_ATTR_PREFIXES)
 
def _matrix_from_object_no_translation(obj) -> list[float]:
    """
    Build a CityGML transformationMatrix (row-major 16 floats) from a Blender object.
    Uses object rotation_euler + scale; translation is always 0 because CityGML implicit
    geometries represent translation via referencePoint.
    """
    try:
        from math import cos, sin
        sx, sy, sz = (float(obj.scale[0]), float(obj.scale[1]), float(obj.scale[2]))
        rx, ry, rz = (float(obj.rotation_euler[0]), float(obj.rotation_euler[1]), float(obj.rotation_euler[2]))

        cx, cy, cz = cos(rx), cos(ry), cos(rz)
        sxn, syn, szn = sin(rx), sin(ry), sin(rz)

        # Blender default Euler order: XYZ. Build R = Rz * Ry * Rx (row-major).
        r00 = cz * cy
        r01 = cz * syn * sxn - szn * cx
        r02 = cz * syn * cx + szn * sxn

        r10 = szn * cy
        r11 = szn * syn * sxn + cz * cx
        r12 = szn * syn * cx - cz * sxn

        r20 = -syn
        r21 = cy * sxn
        r22 = cy * cx

        return [
            r00 * sx, r01 * sy, r02 * sz, 0.0,
            r10 * sx, r11 * sy, r12 * sz, 0.0,
            r20 * sx, r21 * sy, r22 * sz, 0.0,
            0.0, 0.0, 0.0, 1.0,
        ]
    except Exception:
        return [
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        ]

def _matrix_from_matrix_world_no_translation(obj) -> list[float]:
    """
    Build transformationMatrix from obj.matrix_world (includes applied Dimensions/constraints),
    but with translation forced to 0 (translation is encoded via referencePoint).
    """
    try:
        mw = obj.matrix_world.copy()
        mw[0][3] = 0.0
        mw[1][3] = 0.0
        mw[2][3] = 0.0
        out = []
        for r in range(4):
            for c in range(4):
                out.append(float(mw[r][c]))
        return out
    except Exception:
        return _matrix_from_object_no_translation(obj)

def _transform_xyz_by_inv_no_translation(obj, xyz_abs: tuple[float, float, float]) -> tuple[float, float, float]:
    """
    Convert an absolute (x,y,z) in target CRS into template-local coordinates by applying the
    inverse of obj.matrix_world with translation removed.
    This prevents double application of scale/rotation when also exporting transformationMatrix.
    """
    try:
        mw = obj.matrix_world.copy()
        mw[0][3] = 0.0
        mw[1][3] = 0.0
        mw[2][3] = 0.0
        inv = mw.inverted()
        x, y, z = xyz_abs
        v = inv @ Vector((float(x), float(y), float(z), 1.0))
        return (float(v[0]), float(v[1]), float(v[2]))
    except Exception:
        x, y, z = xyz_abs
        return (float(x), float(y), float(z))

def _subelement_same_ns(parent, local_name: str, attrib=None):
    """
    Create a SubElement using the same namespace URI as the parent tag (fallback: no namespace).
    This helper is used in multiple branches; it must exist at module scope.
    """
    attrib = attrib or {}
    tag = parent.tag
    if isinstance(tag, str) and tag.startswith("{") and "}" in tag:
        uri = tag[1:tag.index("}")]
        qtag = f"{{{uri}}}{local_name}"
    else:
        qtag = local_name
    return SubElement(parent, qtag, attrib)

def _write_generic_attributes(parent: Element, attrs: dict):
    """
    CityGML 2.0 Generic Attributes:
      <gen:stringAttribute name="..."><gen:value>...</gen:value></gen:stringAttribute>

    - KEIN <gen:name>
    - @name ist Pflicht
    - parent: Element, unter das die gen:*Attribute geschrieben werden (Feature oder Surface)
    - attrs: dict der Custom Properties (z.B. obj oder mat items als dict)
    """
    if not attrs:
        return

    for k, v in attrs.items():
        if k in INTERNAL_ATTR_KEYS or k in SPECIFIC_ATTR_KEYS:
            continue
        if _should_skip_generic_attr_key(k):
            continue
        # Never export internal roundtrip helpers (breaks CityGML 2 validation in FME)
        if str(k) in ("cgml3_uv_start_by_ring",):
            continue
        # Skip internal cgml3_ keys (they contain structured data, not direct attributes)
        if str(k).startswith("cgml3_"):
            continue
        if str(k).startswith("_"):
            continue

        name = str(k).strip()
        if not name:
            continue

        # Skip empty values
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue

        # Determine gen:*Attribute element based on python type / simple ISO date strings
        ln = None
        if isinstance(v, bool):
            ln = "intAttribute"
            v_out = "1" if v else "0"
        elif isinstance(v, int):
            ln = "intAttribute"
            v_out = str(v)
        elif isinstance(v, float):
            ln = "doubleAttribute"
            v_out = str(v)
        elif isinstance(v, str):
            s = v.strip()
            # CityGML2 example uses gen:dateAttribute for YYYY-MM-DD
            if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
                ln = "dateAttribute"
            else:
                ln = "stringAttribute"
            v_out = s
        else:
            ln = "stringAttribute"
            v_out = str(v)

        try:
            el = SubElement(parent, Q("gen", ln), {"name": name})
            SubElement(el, Q("gen", "value")).text = v_out
        except Exception:
            pass


def _get_generic_attributes_for_export(obj, target="OBJECT"):
    """
    Get generic attributes for export, preferring structured cgml3_generic_attributes
    over raw custom properties.
    
    Args:
        obj: Blender object or material
        target: "OBJECT" or "MATERIAL"
    
    Returns:
        dict of generic attributes to export
    """
    import json
    
    dict_key = "cgml3_generic_attributes" if target == "OBJECT" else "cgml3_surface_generic_attributes"
    json_key = dict_key + "_json"
    
    # Try structured format first
    try:
        d = obj.get(dict_key)
        if isinstance(d, dict) and d:
            return dict(d)
    except Exception:
        pass
    
    # Try JSON format
    try:
        s = obj.get(json_key)
        if isinstance(s, str) and s.strip():
            parsed = json.loads(s)
            if isinstance(parsed, dict) and parsed:
                return parsed
    except Exception:
        pass
    
    # Fallback: use all custom properties (legacy CityGML 2.0 import)
    return dict(getattr(obj, "items", lambda: [])())


def _order_uvs_for_loop_vertices(mesh, loop_indices, ring_xyz, ring_uvs, tol=1e-6, world_matrix=None):
    """
    Reorder ring_uvs to match the actual mesh loop vertex order.
    This mirrors the importer's vertex-matching approach and avoids UV sequences
    that appear 'shuffled' when loop/vertex orders diverge.
    """
    if not ring_uvs or not loop_indices:
        return ring_uvs

    def _eq3(a, b):
        return (abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol and abs(a[2] - b[2]) <= tol)

    ring_xyz_open = list(ring_xyz or [])
    ring_uvs_open = list(ring_uvs or [])
    if len(ring_xyz_open) >= 2 and _eq3(ring_xyz_open[0], ring_xyz_open[-1]):
        ring_xyz_open = ring_xyz_open[:-1]
    if len(ring_uvs_open) >= 2 and ring_uvs_open[0] == ring_uvs_open[-1]:
        ring_uvs_open = ring_uvs_open[:-1]

    if not ring_xyz_open or not ring_uvs_open or len(ring_xyz_open) != len(ring_uvs_open):
        return ring_uvs

    # Map each loop vertex position to a unique ring_xyz index.
    # IMPORTANT: do not allow reusing the same ring index multiple times (duplicate vertices),
    # otherwise UV order becomes a permutation that can be wrong for quads/ngons with repeated coords.
    used = set()
    loop_order = []
    for li in loop_indices:
        vi = mesh.loops[li].vertex_index
        vco = mesh.vertices[vi].co
        if world_matrix is not None:
            vco = world_matrix @ vco
        vx, vy, vz = float(vco.x), float(vco.y), float(vco.z)

        best_i = None
        best_d = None
        for i, (x, y, z) in enumerate(ring_xyz_open):
            if i in used:
                continue
            d = (vx - x) * (vx - x) + (vy - y) * (vy - y) + (vz - z) * (vz - z)
            if best_d is None or d < best_d:
                best_d = d
                best_i = i

        # Fallback: if everything is already used (shouldn't happen), allow reuse.
        if best_i is None:
            best_i = 0
            best_d = None
            for i, (x, y, z) in enumerate(ring_xyz_open):
                d = (vx - x) * (vx - x) + (vy - y) * (vy - y) + (vz - z) * (vz - z)
                if best_d is None or d < best_d:
                    best_d = d
                    best_i = i

        used.add(best_i)
        loop_order.append(best_i)

    ordered_uvs_open = [ring_uvs_open[i] for i in loop_order]
    if ordered_uvs_open and ordered_uvs_open[0] != ordered_uvs_open[-1]:
        ordered_uvs_open = ordered_uvs_open + [ordered_uvs_open[0]]
    return ordered_uvs_open


def _rotate_uvs_to_start(uvs, start_uv, tol=1e-6):
    """Rotate a closed UV ring so it starts at start_uv (within tolerance)."""
    if not uvs or start_uv is None:
        return uvs
    try:
        su, sv = float(start_uv[0]), float(start_uv[1])
    except Exception:
        return uvs

    open_uvs = list(uvs)
    if len(open_uvs) >= 2 and open_uvs[0] == open_uvs[-1]:
        open_uvs = open_uvs[:-1]
    if not open_uvs:
        return uvs

    def _eq(p):
        return abs(p[0] - su) <= tol and abs(p[1] - sv) <= tol

    idx = None
    for i, p in enumerate(open_uvs):
        if _eq(p):
            idx = i
            break
    if idx is None:
        # fallback: nearest point
        best_i = 0
        best_d = None
        for i, (u, v) in enumerate(open_uvs):
            d = (u - su) * (u - su) + (v - sv) * (v - sv)
            if best_d is None or d < best_d:
                best_d = d
                best_i = i
        idx = best_i

    rot = open_uvs[idx:] + open_uvs[:idx]
    if rot and rot[0] != rot[-1]:
        rot = rot + [rot[0]]
    return rot


def _as_uv_tuple2(v):
    try:
        if v is None:
            return None
        # Blender IDProperty arrays (e.g. <bpy id property array [2]>)
        try:
            if hasattr(v, "__len__") and len(v) >= 2 and not isinstance(v, (str, bytes)):
                return (float(v[0]), float(v[1]))
        except Exception:
            pass
        if hasattr(v, "__len__") and len(v) >= 2:
            return (float(v[0]), float(v[1]))
    except Exception:
        return None


def _uv_ring_sign(uvs):
    open_uvs = []
    for uv in list(uvs or []):
        uv2 = _as_uv_tuple2(uv)
        if uv2 is not None:
            open_uvs.append(uv2)
    if len(open_uvs) >= 2 and abs(open_uvs[0][0] - open_uvs[-1][0]) <= 1e-9 and abs(open_uvs[0][1] - open_uvs[-1][1]) <= 1e-9:
        open_uvs = open_uvs[:-1]
    if len(open_uvs) < 3:
        return 0

    area = 0.0
    for i, (u1, v1) in enumerate(open_uvs):
        u2, v2 = open_uvs[(i + 1) % len(open_uvs)]
        area += u1 * v2 - u2 * v1
    if area > 1e-12:
        return 1
    if area < -1e-12:
        return -1
    return 0


def _reverse_uv_ring(uvs):
    values = [_as_uv_tuple2(uv) for uv in list(uvs or [])]
    values = [uv for uv in values if uv is not None]
    if not values:
        return []
    closed = len(values) >= 2 and abs(values[0][0] - values[-1][0]) <= 1e-9 and abs(values[0][1] - values[-1][1]) <= 1e-9
    if closed:
        values = values[:-1]
    values = list(reversed(values))
    if closed and values:
        values.append(values[0])
    return values


def _correct_interior_uv_ring(uvs, exterior_sign: int):
    values = [_as_uv_tuple2(uv) for uv in list(uvs or [])]
    values = [uv for uv in values if uv is not None]
    if not values or exterior_sign == 0:
        return values, False
    ring_sign = _uv_ring_sign(values)
    if ring_sign != 0 and ring_sign == exterior_sign:
        return _reverse_uv_ring(values), True
    return values, False


def _maybe_debug_uv_rotation(ring_id: str, mat, before_uvs, after_uvs, start_uv):
    """Opt-in console debug. Enabled via env var `CGML3_DEBUG_UV_ROT=1`."""
    try:
        import os
        if not os.environ.get("CGML3_DEBUG_UV_ROT", ""):
            return
        watch = os.environ.get("CGML3_DEBUG_UV_RING", "")
        if watch and str(watch).strip() and str(watch).strip() not in str(ring_id):
            return

        def _head(seq, n=4):
            if not seq:
                return []
            return list(seq[:n])

        mname = getattr(mat, "name", None) if mat is not None else None
        gr = None
        try:
            gr = mat.get("gml_ring_id") if mat is not None else None
        except Exception:
            gr = None
        print(
            "[CityGML3][UV] ring_id=%s mat=%s gml_ring_id=%s start_uv=%s before_head=%s after_head=%s"
            % (ring_id, mname, gr, start_uv, _head(before_uvs), _head(after_uvs))
        )
    except Exception:
        return

            # (remaining branches continue below)

def _get_poly_gid(d: dict) -> str:
    """
    Robust poly gml:id getter for exporter pipeline.

    Some code paths store polygon ids as 'poly_id' (or other variants),
    while writer expects 'poly_gid'. This prevents KeyError and ensures
    stable references for xlink:href.
    """
    if not isinstance(d, dict):
        return f"UUID_{uuid4()}"

    gid = (
        d.get("poly_gid")
        or d.get("poly_id")
        or d.get("gml_id")
        or d.get("id")
        or d.get("gid")
    )

    if not gid:
        gid = f"UUID_{uuid4()}"
        d["poly_gid"] = gid  # persist for later references
    else:
        # normalize so later code can rely on poly_gid being present
        d.setdefault("poly_gid", gid)

    return gid


from typing import Dict, List, Tuple, Optional
import os
from uuid import uuid4
from datetime import datetime, date, timezone
import bpy
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import Element, SubElement, ElementTree
from mathutils import Vector

from .namespaces import NS, Q, GML_ID, register_export_namespaces
from .helpers import fmt, _ring_area_sign_xyz, make_gml_id, _envelope_elem, resolve_feature_tag, _read_world_crs, _extract_epsg, select_envelope_srs_name

from .geometry import write_polygon_with_ring_ids, write_compositesurface_with_polygons, format_poslist
from .appearance import add_x3d_materials, add_parameterized_textures, add_georeferenced_textures
from .materials import extract_base_color_rgba, first_image_path_from_material, extract_x3d_params_from_material
from .texio import _ensure_export_texture
from .document import create_citymodel_root, add_bounded_by, add_cityobject_member
from .curve_export import should_use_gml_curve, export_spline_as_curve_segment
from .grid_export import can_export_as_raster_relief, export_rectified_grid_coverage
import numpy as np
from .crs_transform import GeoTransformer
from ...ops.export_face_autofill import prepare_new_face_export_overrides
from ...shared.export_helpers import object_is_viewport_visible

SPECIFIC_ATTR_KEYS = {
    # Building
    "bldg:class",
    "bldg:function",
    "bldg:usage",
    "bldg:roofType",
    "bldg:yearOfConstruction",
    "bldg:yearOfDemolition",
    "bldg:storeyHeightsAboveGround",
    "bldg:storeyHeightsBelowGround",
    "bldg:storeysAboveGround",
    "bldg:storeysBelowGround",
    "bldg:measuredHeight",
    "bldg:measuredHeight:uom",
    "bldg:height:value",
    "bldg:height:highReference",
    "bldg:height:lowReference",
    "bldg:height:status",
    "bldg:height:uom",
    "bldg:address:country",
    "bldg:address:locality",
    "bldg:address:street",
    "bldg:address:streetNumber",
    "bldg:address:postCode",
    "bldg:address:point:x",
    "bldg:address:point:y",
    "bldg:address:point:z",

    # Transportation
    "tran:class",
    "tran:function",
    "tran:usage",
    "tran:surfaceMaterial",
    "tran:granularity",
    "tran:trafficDirection",

    # Tunnel
    "tun:class",
    "tun:function",
    "tun:usage",

    # Bridge
    "brid:class",
    "brid:function",
    "brid:usage",
    "brid:isMovable",

    # WaterBody
    "wtr:class",
    "wtr:function",
    "wtr:usage",

    # Vegetation
    "veg:class",
    "veg:function",
    "veg:usage",
    "veg:species",
    "veg:height",
    "veg:height:uom",
    "veg:trunkDiameter",
    "veg:trunkDiameter:uom",
    "veg:crownDiameter",
    "veg:crownDiameter:uom",

    # CityFurniture
    "frn:class",
    "frn:function",
    "frn:usage",

    # LandUse
    "luse:class",
    "luse:function",
    "luse:usage",

    # Generics
    "gen:class",
    "gen:function",
    "gen:usage",

    # CityObjectGroup
    "grp:class",
    "grp:function",
    "grp:usage",

    # core:AbstractFeatureWithLifespan (CityGML 3.0 mit Präfix)
    "core:creationDate",
    "core:terminationDate",
    "core:validFrom",
    "core:validTo",

    # CityGML 2.0: Life-cycle attributes OHNE core: Präfix
    # Diese müssen explizit geschrieben werden, nicht als Generic Attributes!
    "creationDate",
    "terminationDate",
    "validFrom",
    "validTo",

    # CityGML 2.0: measuredHeight statt con:height
    "bldg:measuredHeight",
    "bldg:measuredHeight:uom",
    
    # Relief / Digital Elevation Model
    "dem:lod",
    "dem:extent",
}

INTERNAL_ATTR_KEYS = {
    "cgml3_feature",
    "gml_id",
    "cgml3_implicit_template_id",
    "cgml_parent_id",
}

MATERIAL_INTERNAL_ATTR_KEYS = {
    "EPSG",
    "gml_ring_id",
    "gml_polygon_id",
    "gml_multisurface_id",
    "gml_compositesurface_id",
    "con_surface_id",
    "SurfaceTyp",
    "surface_type",
    "app_target_or_uri",
    "is_multisurface_member",
    "is_compositesurface_member",
    "Interior",
    "ExteriorPolyId",
    "ExteriorRingId",
    # User-requested internal attributes
    "face_index",
    "gml_surface_id",
    "multisurface_id",
    "source_material",
    "surface_id",
    "cgml3_subfeature_type",
    "cgml3_subfeature_id",
    "cgml3_subfeature_parent_id",
    "cgml3_subfeature_relation",
    "cgml3_subfeature_namespace",
    "cgml3_subfeature_name",
    "cgml_part_type",
    "cgml_part_id",
    "cgml_parent_id",
    "cgml_part_relation",
    "cgml_part_name",
    "part_type",
    "part_id",
    "parent_id",
    "part_relation",
    "part_name",
    "closure_parent_id",
    # Generic Attributes (gen:*) werden separat behandelt
}

def _iter_attr_values(val):
    if isinstance(val, (list, tuple, set)):
        for v in val:
            yield v
    else:
        yield val

def export_blender_to_citygml3(
    filepath: str,
    context,
    srs_name: str,
    feature_type_prop: str,
    *,
    export_x3d: bool = True,
    export_ptx: bool = True,
    export_gtx: bool = True,
    use_inner_outer_script: bool = False,
    autofill_new_surfaces: bool = False,
    unclassified_surface_type: str = "WallSurface",
    split_wall_roof_surfaces: bool = False,  # <--- NEU
    write_lod_solid_refs: bool = False,
    export_types: Optional[List[str]] = None,
):

    default_unclassified_surface_type = str(unclassified_surface_type or "WallSurface").strip() or "WallSurface"

    # bevorzugt: übergebener srs_name -> World["CRS"] -> Fallback 25832
    _epsg_num = _extract_epsg(srs_name) or _extract_epsg(_read_world_crs()) or "25832"
    root = create_citymodel_root(_epsg_num)

    out_dir = os.path.dirname(os.path.abspath(filepath))
    os.makedirs(os.path.join(out_dir, "appearance"), exist_ok=True)

    # Global: eindeutige Textur-Dateinamen über ALLE Features hinweg
    used_tex_names_global: set[str] = set()
    image_uri_map: dict[str, str] = {}

    # Global: Appearance-Gruppen in Export-Reihenfolge
    appearance_groups: dict[str, dict] = {}
    appearance_order: list[str] = []

    THEME_FALLBACK = "Rotterdam"
    processed_interior_ring_uvs = {}

    def _remap_image_uri(src_path: str) -> str:
        """Kopiert Textur genau einmal nach /appearance und liefert rel_uri (appearance/xxx.ext)."""
        if not src_path:
            return ""
        if src_path in image_uri_map:
            return image_uri_map[src_path]
        _abs_dest, rel_uri = _ensure_export_texture(src_path, out_dir, used_tex_names_global)
        if rel_uri:
            image_uri_map[src_path] = rel_uri
        return rel_uri or ""

    def _get_app_group(app_id: str, theme: str = "") -> dict:
        if app_id not in appearance_groups:
            appearance_groups[app_id] = {
                "theme": (theme or "").strip(),
                "ptx_by_image": {},     # Dict[rel_uri, List[{poly_id, ring_id, uvs, ori?}]]
                "gtx_by_image": {},     # Dict[rel_uri, List[{poly_id, refpt, M}]]
                "x3d_by_key": {},       # Dict[(rgba_tuple, params_tuple), {rgba, params, poly_ids}]
            }
            appearance_order.append(app_id)
        elif theme and not appearance_groups[app_id].get("theme"):
            appearance_groups[app_id]["theme"] = theme.strip()
        return appearance_groups[app_id]


    def _norm_crs_for_pyproj(val: str, fallback_epsg: str) -> str:
        try:
            epsg = _extract_epsg(val)
        except Exception:
            epsg = ""
        if epsg and str(epsg).isdigit():
            return f"EPSG:{epsg}"
        if val and str(val).strip():
            return str(val).strip()
        return f"EPSG:{fallback_epsg}"

    def _export_wants_3d() -> bool:
        """
        3D-Export aktivieren, wenn ein Z-Origin vorhanden ist.
        Fallback True, falls World nicht lesbar.
        """
        try:
            w = bpy.data.worlds.get("World")
            return bool(w and "Z-Origin" in w)
        except Exception:
            return True

    # CRS vorbereiten
    src_crs = _norm_crs_for_pyproj(_read_world_crs(), _epsg_num)
    tgt_crs = _norm_crs_for_pyproj(srs_name, _epsg_num)
    trf = GeoTransformer(src_crs, tgt_crs, export_3d=_export_wants_3d())

    #com = add_cityobject_member(root)

    used_feat_ids = set()

    # Cache pro Ursprungs-Mesh für ImplicitGeometry-Instanzen
    implicit_meta_by_mesh: Dict[object, dict] = {}
    MAX_PROBE_POINTS = 8

    def _feature_id(obj_):
        """Stabiler Roundtrip: bevorzugt obj['gml_id'], sonst Name → make_gml_id."""
        # 1) gml_id aus Custom Property verwenden, falls vorhanden
        try:
            raw = obj_.get("gml_id", None)
        except Exception:
            raw = None

        if raw is not None:
            gid = str(raw).strip()
            if gid:
                # XSD-Konformität sicherstellen: xs:ID muss mit Buchstabe/_ beginnen
                # UUIDs aus 3DCityDB können mit Zahl beginnen → make_gml_id validiert
                return make_gml_id(gid, used_feat_ids)

        # 2) Fallback: Name → make_gml_id
        return make_gml_id(obj_.name, used_feat_ids)
    
    def _id_from_material(mat, key: str) -> str:
        try:
            v = mat.get(key, None) if mat else None
        except Exception:
            v = None
        if v is None:
            return ""
        s = str(v).strip()
        return s if s else ""

    def _resolve_export_offset(ctx, obj_):
        try:
            w = bpy.data.worlds.get("World")
            if w and all(k in w for k in ("X-Origin", "Y-Origin", "Z-Origin")):
                return float(w["X-Origin"]), float(w["Y-Origin"]), float(w["Z-Origin"])
        except Exception:
            pass
        # Fallback: Scene properties
        try:
            sc = ctx.scene
            if sc and "crs x" in sc and "crs y" in sc:
                z = float(sc.get("crs z", 0.0))
                return float(sc["crs x"]), float(sc["crs y"]), z
        except Exception:
            pass
        so = getattr(ctx.scene, "cgml3_offset", (0.0, 0.0, 0.0))
        return float(so[0]), float(so[1]), float(so[2])

    def _ensure_closed2d(seq):
        return seq if not seq or seq[0] == seq[-1] else (seq + [seq[0]])

    def _ensure_ring_closed(seq):
        """Ensures the ring is closed even if the last vertex is numerically very close to the first."""
        if not seq:
            return seq
        try:
            a = seq[0]
            b = seq[-1]
            if len(a) == 2:
                if abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9:
                    seq = list(seq)
                    seq[-1] = a
                    return seq
            elif len(a) == 3:
                if abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9 and abs(a[2] - b[2]) < 1e-9:
                    seq = list(seq)
                    seq[-1] = a
                    return seq
        except Exception:
            pass
        return _ensure_closed2d(seq)

    def _first_image_path_from_export_material(obj_, mat_index: int, mat_override=None):
        if mat_override is None:
            return first_image_path_from_material(obj_, mat_index)
        if not mat_override or not getattr(mat_override, "use_nodes", False) or not getattr(mat_override, "node_tree", None):
            return None
        for node in mat_override.node_tree.nodes:
            if node.type == "TEX_IMAGE" and getattr(node, "image", None):
                img = node.image
                if getattr(img, "filepath", ""):
                    return bpy.path.abspath(img.filepath)
        return None

    def _feature_tag(obj_):
        # 1) Konfiguriertes Property (z.B. "ModelType" oder etwas anderes)
        val = None
        try:
            if feature_type_prop:
                v = obj_.get(feature_type_prop, None)
                if v is not None and str(v).strip():
                    val = str(v).strip()
        except Exception:
            val = None

        # 2) Fallback: cgml3_feature, wie es der Importer setzt
        if not val:
            try:
                v = obj_.get("cgml3_feature", None)
                if v is not None and str(v).strip():
                    val = str(v).strip()
            except Exception:
                mat = face_override.get("mat") if face_override else None

        # 3) Fallback: Default "Building"
        if not val:
            val = "Building"

        ns_key, local_name = resolve_feature_tag(val, version="2.0")
        return ns_key, local_name

    RECOGNIZED_FACE_SURFACE_TYPES = {
        "RoofSurface",
        "WallSurface",
        "GroundSurface",
        "OuterCeilingSurface",
        "OuterFloorSurface",
        "ClosureSurface",
        "CeilingSurface",
        "InteriorWallSurface",
        "FloorSurface",
        "BuildingPart",
        "BuildingConstructiveElement",
        "BuildingInstallation",
        "IntBuildingInstallation",
        "BuildingFurniture",
        "Window",
        "Door",
        "WindowSurface",
        "DoorSurface",
        "BridgeConstructionElement",
        "IntBridgeConstructionElement",
        "BridgeInstallation",
        "IntBridgeInstallation",
        "BridgeFurniture",
        "Bridge",
        "BridgePart",
        "TunnelConstructiveElement",
        "TunnelInstallation",
        "IntTunnelInstallation",
        "TunnelFurniture",
        "Tunnel",
        "TunnelPart",
        "HollowSpace",
        "TrafficArea",
        "AuxiliaryTrafficArea",
        "TrafficSpace",
        "AuxiliaryTrafficSpace",
        "Marking",
        "Hole",
        "HoleSurface",
        "Road",
        "Railway",
        "Track",
        "Square",
        "Waterway",
        "Section",
        "Intersection",
        "ClearanceSpace",
        "CityFurniture",
        "SolitaryVegetationObject",
        "PlantCover",
        "WaterBody",
        "WaterSurface",
        "WaterGroundSurface",
        "WaterClosureSurface",
        "LandUse",
        "OtherConstruction",
        "TINRelief",
        "MassPointRelief",
        "BreaklineRelief",
        "RasterRelief",
        "ReliefFeature",
    }

    def _normalize_face_surface_type(raw_surface_type: object | None) -> str | None:
        raw = str(raw_surface_type or "").strip()
        if not raw:
            return None
        if raw == "Opening":
            return default_unclassified_surface_type
        if raw in RECOGNIZED_FACE_SURFACE_TYPES:
            return raw
        return None

    def _first_nonempty_prop(sources, keys: tuple[str, ...]) -> str:
        for source in sources:
            if source is None:
                continue
            getter = getattr(source, "get", None)
            if getter is None:
                continue
            for key in keys:
                try:
                    value = getter(key, None)
                except TypeError:
                    try:
                        value = getter(key)
                    except Exception:
                        value = None
                except Exception:
                    value = None
                if value is None:
                    continue
                text = str(value).strip()
                if text:
                    return text
        return ""

    def _normalize_opening_type(raw_opening_type: object | None) -> str:
        opening_type = str(raw_opening_type or "").strip().lower()
        if opening_type in ("window", "windowsurface", "fenster"):
            return "Window"
        if opening_type in ("door", "doorsurface", "tuer", "tur"):
            return "Door"
        return ""

    def _is_opening_face_surface_type(raw_surface_type: object | None) -> bool:
        raw = str(raw_surface_type or "").strip()
        return raw in ("Opening", "WindowSurface", "DoorSurface", "Window", "Door")

    def _build_face_opening_info(raw_surface_type, face_override, mat, obj_, poly_gid: str) -> dict | None:
        raw_surface_type = str(raw_surface_type or "").strip()
        opening_type = _normalize_opening_type(
            _first_nonempty_prop(
                (face_override, mat, obj_),
                ("opening_type", "OpeningTyp", "openingType", "cgml_opening_type"),
            )
        )
        if not opening_type and _is_opening_face_surface_type(raw_surface_type):
            opening_type = _normalize_opening_type(raw_surface_type)
        if opening_type not in ("Window", "Door"):
            return None

        opening_id = _first_nonempty_prop(
            (face_override, mat, obj_),
            ("opening_gml_id", "opening_id", "OpeningId", "openingId"),
        )
        if not opening_id:
            opening_id = f"{poly_gid}_{opening_type}"

        opening_name = _first_nonempty_prop(
            (face_override, mat, obj_),
            ("opening_name", "OpeningName", "openingName"),
        )

        return {
            "opening_type": opening_type,
            "opening_id": opening_id,
            "opening_name": opening_name,
        }

    def _truthy_prop_value(value) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        text = str(value).strip().lower()
        return text in ("1", "true", "yes", "ja", "y")

    def _is_openings_cutter_interior(face_override, mat, obj_) -> bool:
        for src in (face_override, mat, obj_):
            if src is None:
                continue
            getter = getattr(src, "get", None)
            if getter is None:
                continue
            for key in (
                "opening_cutter_interior",
                "created_by_openings_cutter",
                "OpeningsCutterInterior",
            ):
                try:
                    if _truthy_prop_value(getter(key, None)):
                        return True
                except Exception:
                    continue

        # Backwards compatibility for openings created before the explicit marker
        # existed: their helper ring references a boundary polygon that also has
        # an opening surface material pointing to the same boundary surface.
        ext_pid = _first_nonempty_prop((face_override, mat), ("ExteriorPolyId",))
        if not ext_pid or obj_ is None or getattr(obj_, "data", None) is None:
            return False

        parent_surface_ids = set()
        try:
            materials = list(getattr(obj_.data, "materials", []) or [])
        except Exception:
            materials = []

        for mat_ in materials:
            if mat_ is None:
                continue
            try:
                pid = str(mat_.get("gml_polygon_id", "") or "").strip()
            except Exception:
                pid = ""
            if pid != ext_pid:
                continue
            sid = _first_nonempty_prop((mat_,), ("con_surface_id",))
            if sid:
                parent_surface_ids.add(sid)

        if not parent_surface_ids:
            return False

        for mat_ in materials:
            if mat_ is None:
                continue
            try:
                is_opening_mat = bool(mat_.get("is_opening", False))
            except Exception:
                is_opening_mat = False
            if not is_opening_mat:
                surface_type = _normalize_face_surface_type(
                    _first_nonempty_prop((mat_,), ("surface_type", "SurfaceTyp", "Typ"))
                )
                is_opening_mat = surface_type in ("WindowSurface", "DoorSurface", "Window", "Door")
            if not is_opening_mat:
                continue
            parent_sid = _first_nonempty_prop((mat_,), ("filling_parent_surface_id", "opening_surface_id"))
            if parent_sid in parent_surface_ids:
                return True

        return False
    
    def _to_xs_datetime(value: datetime | str) -> str:
        """
        Konvertiert eine datetime-Instanz oder einen String in einen xs:dateTime-
        konformen String (YYYY-MM-DDThh:mm:ss[Z]). Ungültige Eingaben liefern
        einen leeren String.
        """
        if isinstance(value, datetime):
            dt = value
        else:
            s = str(value).strip()
            if not s:
                return ""
            # Leerzeichen statt 'T' erlauben, 'Z' in Offset überführen
            s_norm = s.replace(" ", "T")
            if s_norm.endswith("Z"):
                s_norm = s_norm[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(s_norm)
            except ValueError:
                # Fallback: reines Datum
                try:
                    dt = datetime.strptime(s_norm, "%Y-%m-%d")
                except ValueError:
                    return ""

        # Wenn Zeitzone vorhanden: nach UTC normalisieren und "Z" schreiben
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc)

        return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    
    def _to_xs_date(value: datetime | str) -> str:
        """
        Konvertiert datetime/ISO-String auf xs:date (YYYY-MM-DD).
        Akzeptiert auch 'YYYY-MM-DDThh:mm:ss[...]' und schneidet auf Datum ab.
        """
        if isinstance(value, datetime):
            return value.date().isoformat()

        s = str(value).strip()
        if not s:
            return ""

        # ISO-DateTime -> Datumsteil
        if "T" in s:
            s = s.split("T", 1)[0]
        if " " in s:
            s = s.split(" ", 1)[0]

        # Validieren
        try:
            datetime.strptime(s, "%Y-%m-%d")
            return s
        except ValueError:
            return ""

    def _write_lifespan_core(feature_el, obj_):
        """
        Write core life-cycle attributes (creationDate, terminationDate, validFrom, validTo)
        directly on the feature in the order defined by CityGML 3.0 (AbstractFeatureWithLifespan).
        This helper must be called immediately after gml:boundedBy has been written.
        """
        for key, tag in (
            ("core:creationDate", "creationDate"),
            ("core:terminationDate", "terminationDate"),
            ("core:validFrom", "validFrom"),
            ("core:validTo", "validTo"),
        ):
            val = obj_.get(key)
            if val is not None:
                dt_str = _to_xs_date(val)
                if dt_str:
                    el = SubElement(feature_el, Q("core", tag))
                    el.text = dt_str
    
    def _write_custom_attributes(feature_el, obj_, ns_key):
        """
        Writes custom attributes (excluding specific and internal keys) to the feature.
        Also writes specific attributes based on namespace (bldg:class, tran:function, etc.)
        """
        # Write specific attributes first (bldg:class, tran:function, etc.)
        for attr_key in SPECIFIC_ATTR_KEYS:
            val = obj_.get(attr_key)
            if val is not None:
                # Determine namespace and tag
                parts = attr_key.split(":", 1)
                if len(parts) == 2:
                    attr_ns, attr_tag = parts
                    # Handle nested attributes (e.g., bldg:address:point:x)
                    if ":" in attr_tag:
                        continue  # Skip nested, handled separately
                    el = SubElement(feature_el, Q(attr_ns, attr_tag))
                    if isinstance(val, bool):
                        el.text = "true" if val else "false"
                    else:
                        el.text = str(val)
        
        # Write generic attributes
        for k, v in getattr(obj_, "items", lambda: [])():
            # Skip internal and specific keys
            if k in INTERNAL_ATTR_KEYS or k in SPECIFIC_ATTR_KEYS:
                continue
            if str(k).startswith("_"):
                continue
            try:
                # String → gen:stringAttribute
                if isinstance(v, str):
                    a = SubElement(feature_el, Q("gen", "stringAttribute"))
                    a.set("name", str(k).strip())
                    SubElement(a, Q("gen", "value")).text = v
                # bool/int → gen:intAttribute
                elif isinstance(v, bool):
                    a = SubElement(feature_el, Q("gen", "intAttribute"))
                    a.set("name", str(k).strip())
                    SubElement(a, Q("gen", "value")).text = "1" if v else "0"
                elif isinstance(v, int):
                    a = SubElement(feature_el, Q("gen", "intAttribute"))
                    a.set("name", str(k).strip())
                    SubElement(a, Q("gen", "value")).text = str(v)
                # float → gen:doubleAttribute
                elif isinstance(v, (float,)):
                    a = SubElement(feature_el, Q("gen", "doubleAttribute"))
                    a.set("name", str(k).strip())
                    SubElement(a, Q("gen", "value")).text = str(v)
                # Fallback: everything else as stringAttribute
                else:
                    a = SubElement(feature_el, Q("gen", "stringAttribute"))
                    a.set("name", str(k).strip())
                    SubElement(a, Q("gen", "value")).text = str(v)
            except Exception:
                continue
    
    def _write_specific_attributes(feature_el, obj_):
        # Berechne ns_key und local_name für dieses Objekt
        ns_key, local_name = _feature_tag(obj_)
        
        # CityGML 2.0: measuredHeight (bldg:measuredHeight) statt con:height
        # Einfaches gml:LengthType mit uom-Attribut
        measured_height = obj_.get("bldg:measuredHeight", None)
        # Anpassung für ImplicitGeometry-Export (CityGML 2.0):
        # 1. <core:lod2ImplicitRepresentation /> entfernen (wird nicht geschrieben)
        # 2. <core:lod2ImplicitRepresentation> heißt <frn:lod2ImplicitRepresentation> in CityFurniture
        # 3. <core:ImplicitGeometry> heißt <ImplicitGeometry> (ohne Präfix)

        # Schreibe <frn:lod2ImplicitRepresentation> nur für CityFurniture (mit Präfix!)
        """ implicit_geom_data = obj_.get('implicit_geometry')
        if obj_.get('cgml3_feature') == 'CityFurniture' and implicit_geom_data:
            lod2_el = SubElement(feature_el, Q('frn', 'lod2ImplicitRepresentation'))
            imp_geom = SubElement(lod2_el, 'ImplicitGeometry') """
            # Hier Geometrie-Daten einfügen, z.B. Position, Transformation, Referenz
            # imp_geom.text = ...
        if measured_height is not None:
            mh_el = SubElement(feature_el, Q("bldg", "measuredHeight"))
            mh_el.text = str(measured_height).strip()
            uom = obj_.get("bldg:measuredHeight:uom", "m")
            if uom:
                mh_el.set("uom", str(uom).strip())

        # Building-Attribute (bldg:)
        for key, el_name in (
            ("bldg:class", "class"),
            ("bldg:function", "function"),
            ("bldg:usage", "usage"),
            ("bldg:roofType", "roofType"),
        ):
            val = obj_.get(key, None)
            if val is not None:
                for v in _iter_attr_values(val):
                    text = str(v).strip()
                    if key == "bldg:roofType":
                        el = SubElement(feature_el, Q("bldg", el_name))
                        if text:
                            el.text = text
                    elif text:
                        SubElement(feature_el, Q("bldg", el_name)).text = text

        # CityGML 2.0: yearOfConstruction/yearOfDemolition (xs:gYear Format: YYYY)
        for key, el_name in (
            ("bldg:yearOfConstruction", "yearOfConstruction"),
            ("bldg:yearOfDemolition", "yearOfDemolition"),
        ):
            val = obj_.get(key, None)
            if val is not None:
                text = str(val).strip()
                if text:
                    SubElement(feature_el, Q("bldg", el_name)).text = text

        # Building – Geschosshöhen (MeasureOrNilReasonListType: space-separated list with uom)
        for key, el_name, uom_key in (
            ("bldg:storeyHeightsAboveGround", "storeyHeightsAboveGround", "bldg:storeyHeightsAboveGround:uom"),
            ("bldg:storeyHeightsBelowGround", "storeyHeightsBelowGround", "bldg:storeyHeightsBelowGround:uom"),
        ):
            val = obj_.get(key, None)
            if val is not None:
                # Convert list to space-separated string
                if isinstance(val, (list, tuple)):
                    height_values = [fmt(v) for v in val]
                    if height_values:
                        el = SubElement(feature_el, Q("bldg", el_name))
                        el.text = " ".join(height_values)
                        # Add uom attribute (default to "m" if not specified)
                        uom = obj_.get(uom_key, "m")
                        if uom:
                            el.set("uom", str(uom).strip())
                elif str(val).strip():
                    # Single value or already formatted string
                    el = SubElement(feature_el, Q("bldg", el_name))
                    el.text = str(val).strip()
                    uom = obj_.get(uom_key, "m")
                    if uom:
                        el.set("uom", str(uom).strip())

        # Transportation (tran:)
        for key, el_name in (
            ("tran:class", "class"),
            ("tran:function", "function"),
            ("tran:usage", "usage"),
            ("tran:surfaceMaterial", "surfaceMaterial"),
            ("tran:trafficDirection", "trafficDirection"),
            ("tran:granularity", "granularity"),
        ):
            val = obj_.get(key, None)
            if val is not None:
                for v in _iter_attr_values(val):
                    text = str(v).strip()
                    if text:
                        SubElement(feature_el, Q("tran", el_name)).text = text

        # Bridge (brid:) - nur für Bridge-Features
        if ns_key == "brid" and local_name in ("Bridge", "BridgePart"):
            from ...shared.attribute_utils import write_feature_type_attributes_bridge
            write_feature_type_attributes_bridge(feature_el, obj_, "brid", version="2.0")

        # Tunnel (tun:) - nur für Tunnel-Features
        if ns_key == "tun" and local_name in ("Tunnel", "TunnelPart"):
            from ...shared.attribute_utils import write_feature_type_attributes_tunnel
            write_feature_type_attributes_tunnel(feature_el, obj_, "tun", version="2.0")
        
        # Transportation (tran:) - nur für Transportation-Features (Road, Railway, Track, Square)
        if ns_key == "tran" and local_name in ("Road", "Railway", "Track", "Square"):
            from ...shared.attribute_utils import write_feature_type_attributes_transportation
            write_feature_type_attributes_transportation(feature_el, obj_, "tran", version="2.0")

        # WaterBody (wtr:) - nur für WaterBody-Features
        if ns_key == "wtr" and local_name == "WaterBody":
            from ...shared.attribute_utils import write_feature_type_attributes_waterbody
            write_feature_type_attributes_waterbody(feature_el, obj_, "wtr", version="2.0")

        # Vegetation (veg:) - nur für Vegetation-Features (SolitaryVegetationObject, PlantCover)
        if ns_key == "veg" and local_name in ("SolitaryVegetationObject", "PlantCover"):
            from ...shared.attribute_utils import write_feature_type_attributes_vegetation
            write_feature_type_attributes_vegetation(feature_el, obj_, "veg", version="2.0")

        # CityFurniture (frn:) - nur für CityFurniture-Features
        if ns_key == "frn" and local_name == "CityFurniture":
            from ...shared.attribute_utils import write_feature_type_attributes_cityfurniture
            write_feature_type_attributes_cityfurniture(feature_el, obj_, "frn", version="2.0")

        # LandUse (luse:) - nur für LandUse-Features
        if ns_key == "luse" and local_name == "LandUse":
            from ...shared.attribute_utils import write_feature_type_attributes_landuse
            write_feature_type_attributes_landuse(feature_el, obj_, "luse", version="2.0")

        # Relief (dem:) - AbstractReliefComponent attributes
        # dem:lod (required for all relief components)
        dem_lod = obj_.get("dem:lod", None)
        if dem_lod is not None:
            try:
                SubElement(feature_el, Q("dem", "lod")).text = str(int(dem_lod))
            except (ValueError, TypeError):
                pass

        # CityGML 2.0: Keine con:isStructuralElement oder con:relationToConstruction
        # Diese Attribute existieren nicht in CityGML 2.0
        
        # bldg:storeysAboveGround
        storeys_above = obj_.get("bldg:storeysAboveGround", None)
        if storeys_above is not None:
            try:
                SubElement(feature_el, Q("bldg", "storeysAboveGround")).text = str(int(storeys_above))
            except (ValueError, TypeError):
                pass

        # bldg:storeysBelowGround
        storeys_below = obj_.get("bldg:storeysBelowGround", None)
        if storeys_below is not None:
            try:
                SubElement(feature_el, Q("bldg", "storeysBelowGround")).text = str(int(storeys_below))
            except (ValueError, TypeError):
                pass
        
        # BuildingUnit-specific: storey references
        # Format: comma-separated list of gml:id references
        storey_refs = obj_.get("bldg:storey_refs", None)
        if storey_refs is not None:
            storey_ids = [s.strip() for s in str(storey_refs).split(",") if s.strip()]
            for storey_id in storey_ids:
                storey_el = SubElement(feature_el, Q("bldg", "storey"))
                storey_el.set("{http://www.w3.org/1999/xlink}href", f"#{storey_id}")
        
        # BuildingRoom-specific: roomHeight measurements
        # Format: JSON-encoded list of roomHeight dictionaries
        room_height_json = obj_.get("bldg:roomHeight", None)
        if room_height_json is not None:
            try:
                import json
                room_heights = json.loads(room_height_json)
                for rh_data in room_heights:
                    rh_el = SubElement(feature_el, Q("bldg", "roomHeight"))
                    rh_obj = SubElement(rh_el, Q("bldg", "RoomHeight"))
                    
                    if "lowReference" in rh_data and rh_data["lowReference"]:
                        SubElement(rh_obj, Q("bldg", "lowReference")).text = str(rh_data["lowReference"])
                    
                    if "highReference" in rh_data and rh_data["highReference"]:
                        SubElement(rh_obj, Q("bldg", "highReference")).text = str(rh_data["highReference"])
                    
                    if "status" in rh_data and rh_data["status"]:
                        SubElement(rh_obj, Q("bldg", "status")).text = str(rh_data["status"])
                    
                    if "value" in rh_data:
                        value_el = SubElement(rh_obj, Q("bldg", "value"))
                        value_el.text = fmt(rh_data["value"])
                        if "uom" in rh_data and rh_data["uom"]:
                            value_el.set("uom", str(rh_data["uom"]))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        
        # Storey-specific: buildingUnit references
        # Format: comma-separated list of gml:id references
        unit_refs = obj_.get("bldg:buildingUnit_refs", None)
        if unit_refs is not None:
            unit_ids = [u.strip() for u in str(unit_refs).split(",") if u.strip()]
            for unit_id in unit_ids:
                unit_el = SubElement(feature_el, Q("bldg", "buildingUnit"))
                unit_el.set("{http://www.w3.org/1999/xlink}href", f"#{unit_id}")

        # --- CityGML2: bldg:address ist NUR für bldg:Building / bldg:BuildingPart zulässig ---
        if ns_key == "bldg" and feature_el.tag in (Q("bldg", "Building"), Q("bldg", "BuildingPart")):
            # bldg:address - sammle alle bldg:address:* Custom Properties
            addr_data = {}
            point_coords = {}
            
            for key in obj_.keys():
                if key.startswith("bldg:address:"):
                    field = key.replace("bldg:address:", "")
                    
                    # Spezialbehandlung für Koordinaten
                    if field.startswith("point:"):
                        axis = field.split(":")[-1]  # x, y, z
                        point_coords[axis] = obj_[key]
                    else:
                        addr_data[field] = obj_[key]
            
            # Prüfe ob Adressdaten vorhanden sind
            has_addr_data = any(v is not None and str(v).strip() for v in addr_data.values())
            has_point = all(k in point_coords for k in ('x', 'y', 'z'))

            if has_addr_data or has_point:
                from ...shared.xal_writer import write_xal_address
                
                bldg_addr = SubElement(feature_el, Q("bldg", "address"))
                addr = SubElement(bldg_addr, Q("core", "Address"))
                xal_addr = SubElement(addr, Q("core", "xalAddress"))
                ad = SubElement(xal_addr, Q("xAL", "AddressDetails"))

                # xAL-Writer mit allen Feldern aufrufen (version="2.0" für CityGML 2.0)
                write_xal_address(ad, addr_data, version="2.0", Q_func=Q, SubElement_func=SubElement)

                # MultiPoint mit Koordinaten (nur wenn vorhanden)
                if has_point:
                    mp_el = SubElement(addr, Q("core", "multiPoint"))
                    gml_mp = SubElement(mp_el, Q("gml", "MultiPoint"))
                    pm = SubElement(gml_mp, Q("gml", "pointMember"))
                    pt = SubElement(pm, Q("gml", "Point"))
                    pos = SubElement(pt, Q("gml", "pos"))
                    pos.set("srsDimension", "3")
                    pos.text = f"{fmt(point_coords['x'])} {fmt(point_coords['y'])} {fmt(point_coords['z'])}"
    
    all_bbox_coords = []

    def _should_export_obj(o) -> bool:
        if not export_types:
            return True
        try:
            v = o.get(feature_type_prop, None) if feature_type_prop else None
        except Exception:
            v = None
        if not v:
            try:
                v = o.get("cgml3_feature", None)
            except Exception:
                v = None
        if not v:
            v = "Building"
        try:
            ns_key, local = resolve_feature_tag(str(v), version="2.0")
        except Exception:
            return False
        return f"{ns_key}:{local}" in set(export_types)

    def _collect_export_scope():
        seed_objects = list(bpy.context.selected_objects or [])
        if not seed_objects:
            return list(bpy.context.scene.objects)

        seen = set()
        scope = []

        def _add(obj_):
            if obj_ is None:
                return False
            obj_id = id(obj_)
            if obj_id in seen:
                return False
            seen.add(obj_id)
            scope.append(obj_)
            return True

        def _is_hierarchy_anchor(obj_):
            try:
                if obj_.get("cgml3_feature"):
                    return True
            except Exception:
                pass
            try:
                if obj_.get("structure_type") == "hierarchical":
                    return True
            except Exception:
                pass
            try:
                if obj_.get("structure_part") in {"storey", "room"}:
                    return True
            except Exception:
                pass
            return False

        for obj_ in seed_objects:
            _add(obj_)
            parent = getattr(obj_, "parent", None)
            while parent is not None:
                _add(parent)
                parent = getattr(parent, "parent", None)

        changed = True
        while changed:
            changed = False
            for obj_ in list(scope):
                if not _is_hierarchy_anchor(obj_):
                    continue
                for child in getattr(obj_, "children", []):
                    if _add(child):
                        changed = True

        return scope

    export_scope = [
        o for o in _collect_export_scope()
        if object_is_viewport_visible(o, context)
    ]

    # Process mesh objects
    objs = [o for o in export_scope if o.type == "MESH" and _should_export_obj(o)]
    
    # Process Curve objects (for LineString geometry)
    curve_objs = [o for o in export_scope if o.type == "CURVE" and _should_export_obj(o)]
    
    # Process Empty objects (for Point geometry)
    empty_objs = [o for o in export_scope if o.type == "EMPTY" and _should_export_obj(o)]
    
    # ============================================================================
    # HIERARCHICAL EXPORT FIRST: Export hierarchical buildings before other objects
    # ============================================================================
    from .hierarchical_export import export_hierarchical_building
    
    # Track exported objects to avoid duplicates (use object name, not id(),
    # because Python wrapper id() can differ for the same Blender object)
    exported_object_names = set()
    
    # Find and export hierarchical buildings first
    hierarchical_buildings = [
        obj for obj in empty_objs 
        if obj.get("structure_type") == "hierarchical" 
        and obj.get("cgml3_feature") in (
            "Building", "BuildingPart",
            "Bridge", "BridgePart",
            "Tunnel", "TunnelPart",
        )
    ]
    
    for obj in hierarchical_buildings:
        print(f"[DEBUG export] Detected hierarchical building: {obj.name}")
        
        success = export_hierarchical_building(
            obj, root, context, trf, fmt,
            add_cityobject_member, Q, NS, GML_ID,
            _feature_id, _resolve_export_offset,
            _write_custom_attributes, _write_specific_attributes,
            all_bbox_coords,
            unclassified_surface_type=default_unclassified_surface_type,
            _get_app_group=_get_app_group,
            export_ptx=export_ptx,
            export_gtx=export_gtx,
            export_x3d=export_x3d,
            _remap_image_uri=_remap_image_uri
        )
        
        if success:
            # Mark this object and all children as exported
            exported_object_names.add(obj.name)
            for child in obj.children_recursive:
                if not object_is_viewport_visible(child, context):
                    continue
                exported_object_names.add(child.name)
                # Also track mesh data blocks to catch linked duplicates
                if child.type == 'MESH' and child.data:
                    exported_object_names.add(f"__mesh_data__{child.data.name}")
                    # Track polygon IDs from materials to catch separate objects
                    # with same polygon UUIDs (e.g. original + assigned copy)
                    for slot in child.material_slots:
                        if slot.material:
                            _pid = slot.material.get("gml_polygon_id")
                            if _pid:
                                exported_object_names.add(f"__poly_id__{str(_pid).strip()}")
            print(f"[DEBUG export] Marked {len(obj.children_recursive) + 1} objects as exported (hierarchical)")
    
    # Filter out already exported objects from lists
    objs = [o for o in objs if o.name not in exported_object_names]
    empty_objs = [o for o in empty_objs if o.name not in exported_object_names]
    curve_objs = [o for o in curve_objs if o.name not in exported_object_names]
    
    # ============================================================================
    # STANDARD EXPORT: Process remaining objects
    # ============================================================================
    
    # Export Curve objects as LineString geometries first
    for obj in curve_objs:
        # Check if this is a CityGML feature
        feat_ln = obj.get("cgml3_feature")
        if not feat_ln:
            continue
            
        ns_key, local_name = _feature_tag(obj)
        feat_id = _feature_id(obj)
        
        # Create feature element
        com = add_cityobject_member(root)
        feature = SubElement(com, Q(ns_key, local_name))
        feature.set(f"{{{NS['gml']}}}id", feat_id)

        # Erst die semantischen Attribute (yearOfConstruction, address etc.)
        # Für SolitaryVegetationObject schreiben wir veg:-Properties in korrekter Reihenfolge direkt im Branch.
        # Daher NICHT nochmal über _write_custom_attributes anhängen (würde nach der Geometrie landen).
        if not (ns_key == "veg" and local_name == "SolitaryVegetationObject"):
            _write_custom_attributes(feature, obj, ns_key)
        
        # Add gml:name if exists
        if obj.name and obj.name != feat_id:
            name_el = SubElement(feature, Q("gml", "name"))
            name_el.text = obj.name
        
        # Get curve data
        curve_data = obj.data
        if not curve_data or not hasattr(curve_data, 'splines'):
            continue
        
        ox, oy, oz = _resolve_export_offset(context, obj)
        
        # Check if we have multiple splines (MultiCurve) or just one (LineString/Curve)
        # Filter splines: POLY needs >=2 points, BEZIER/NURBS need >=2 control points
        splines = []
        for s in curve_data.splines:
            if s.type == 'POLY' and hasattr(s, 'points') and len(s.points) >= 2:
                splines.append(s)
            elif s.type == 'BEZIER' and hasattr(s, 'bezier_points') and len(list(s.bezier_points)) >= 2:
                splines.append(s)
            elif s.type == 'NURBS' and hasattr(s, 'points') and len(s.points) >= 2:
                splines.append(s)
        
        if not splines:
            continue
        
        # Check if this is BreaklineRelief (uses dem:breaklines or dem:ridgeOrValleyLines)
        is_breakline_relief = (ns_key, local_name) == ("dem", "BreaklineRelief")
        
        # Create lod0Geometry or dem:breaklines depending on feature type
        if is_breakline_relief:
            # BreaklineRelief: write dem:lod first (required)
            _write_specific_attributes(feature, obj)
            
            # Determine which type of lines (check custom property)
            has_ridge = obj.get("dem:hasRidgeOrValleyLines", False)
            
            if has_ridge:
                # dem:ridgeOrValleyLines
                lines_container = SubElement(feature, Q("dem", "ridgeOrValleyLines"))
            else:
                # dem:breaklines (default)
                lines_container = SubElement(feature, Q("dem", "breaklines"))
        else:
            # Standard: Create lod0MultiCurve
            lines_container = SubElement(feature, Q("core", "lod0MultiCurve"))
        
        if len(splines) == 1:
            # Single spline - could be LineString or Curve
            spline = splines[0]
            
            # Check if this should be exported as gml:Curve (BEZIER/NURBS) or gml:LineString (POLY)
            use_curve = should_use_gml_curve(spline)
            
            if use_curve:
                # Export as gml:Curve with segments
                curve_elem = SubElement(lines_container, Q("gml", "Curve"))
                curve_elem.set(f"{{{NS['gml']}}}id", f"UUID_{uuid4().hex}")
                
                segments_elem = SubElement(curve_elem, Q("gml", "segments"))
                
                # Export spline as curve segment
                segment_info = export_spline_as_curve_segment(spline, obj.matrix_world, trf, (ox, oy, oz))
                
                if segment_info:
                    seg_type = segment_info['type']
                    points = segment_info['points']
                    
                    if seg_type == 'Arc' and len(points) >= 3:
                        # gml:Arc - 3 points
                        arc_elem = SubElement(segments_elem, Q("gml", "Arc"))
                        for pt in points[:3]:
                            pos_elem = SubElement(arc_elem, Q("gml", "pos"))
                            pos_elem.set("srsDimension", "3")
                            pos_elem.text = f"{fmt(pt[0])} {fmt(pt[1])} {fmt(pt[2])}"
                            all_bbox_coords.append(pt)
                    
                    elif seg_type == 'ArcString' and len(points) >= 3:
                        # gml:ArcString - multiple arcs
                        arcstring_elem = SubElement(segments_elem, Q("gml", "ArcString"))
                        poslist_elem = SubElement(arcstring_elem, Q("gml", "posList"))
                        poslist_elem.set("srsDimension", "3")
                        poslist_text = []
                        for pt in points:
                            poslist_text.extend([fmt(pt[0]), fmt(pt[1]), fmt(pt[2])])
                            all_bbox_coords.append(pt)
                        poslist_elem.text = " ".join(poslist_text)
                    
                    else:
                        # Fallback: LineStringSegment
                        lss_elem = SubElement(segments_elem, Q("gml", "LineStringSegment"))
                        poslist_elem = SubElement(lss_elem, Q("gml", "posList"))
                        poslist_elem.set("srsDimension", "3")
                        poslist_text = []
                        for pt in points:
                            poslist_text.extend([fmt(pt[0]), fmt(pt[1]), fmt(pt[2])])
                            all_bbox_coords.append(pt)
                        poslist_elem.text = " ".join(poslist_text)
            
            else:
                # Export as simple gml:LineString (POLY spline)
                linestring = SubElement(lines_container, Q("gml", "LineString"))
                linestring.set(f"{{{NS['gml']}}}id", f"UUID_{uuid4().hex}")
                
                # Extract coordinates from spline points
                coords_local = np.asarray(
                    [(float(p.co[0]), float(p.co[1]), float(p.co[2])) for p in spline.points],
                    dtype=np.float64
                )
                coords_tgt, origin_tgt = trf.transform_with_origin(coords_local, (ox, oy, oz))
                
                # Write posList
                poslist = SubElement(linestring, Q("gml", "posList"))
                poslist.set("srsDimension", "3")
                poslist_text = []
                for coord in coords_tgt:
                    x = float(coord[0]) + origin_tgt[0]
                    y = float(coord[1]) + origin_tgt[1]
                    z = float(coord[2]) + origin_tgt[2]
                    poslist_text.extend([fmt(x), fmt(y), fmt(z)])
                    all_bbox_coords.append((x, y, z))
                poslist.text = " ".join(poslist_text)
        
        else:
            # Multiple splines → MultiCurve
            multicurve = SubElement(lines_container, Q("gml", "MultiCurve"))
            multicurve.set(f"{{{NS['gml']}}}id", f"UUID_{uuid4().hex}")
            
            for spline in splines:
                curve_member = SubElement(multicurve, Q("gml", "curveMember"))
                
                # Check if this spline should be a Curve or LineString
                use_curve = should_use_gml_curve(spline)
                
                if use_curve:
                    # Export as gml:Curve with segments
                    curve_elem = SubElement(curve_member, Q("gml", "Curve"))
                    curve_elem.set(f"{{{NS['gml']}}}id", f"UUID_{uuid4().hex}")
                    
                    segments_elem = SubElement(curve_elem, Q("gml", "segments"))
                    
                    segment_info = export_spline_as_curve_segment(spline, obj.matrix_world, trf, (ox, oy, oz))
                    
                    if segment_info:
                        seg_type = segment_info['type']
                        points = segment_info['points']
                        
                        if seg_type == 'Arc' and len(points) >= 3:
                            arc_elem = SubElement(segments_elem, Q("gml", "Arc"))
                            for pt in points[:3]:
                                pos_elem = SubElement(arc_elem, Q("gml", "pos"))
                                pos_elem.set("srsDimension", "3")
                                pos_elem.text = f"{fmt(pt[0])} {fmt(pt[1])} {fmt(pt[2])}"
                                all_bbox_coords.append(pt)
                        
                        elif seg_type == 'ArcString' and len(points) >= 3:
                            arcstring_elem = SubElement(segments_elem, Q("gml", "ArcString"))
                            poslist_elem = SubElement(arcstring_elem, Q("gml", "posList"))
                            poslist_elem.set("srsDimension", "3")
                            poslist_text = []
                            for pt in points:
                                poslist_text.extend([fmt(pt[0]), fmt(pt[1]), fmt(pt[2])])
                                all_bbox_coords.append(pt)
                            poslist_elem.text = " ".join(poslist_text)
                        
                        else:
                            lss_elem = SubElement(segments_elem, Q("gml", "LineStringSegment"))
                            poslist_elem = SubElement(lss_elem, Q("gml", "posList"))
                            poslist_elem.set("srsDimension", "3")
                            poslist_text = []
                            for pt in points:
                                poslist_text.extend([fmt(pt[0]), fmt(pt[1]), fmt(pt[2])])
                                all_bbox_coords.append(pt)
                            poslist_elem.text = " ".join(poslist_text)
                
                # removed stray else: that caused IndentationError
                    # Export as simple gml:LineString
                    linestring = SubElement(curve_member, Q("gml", "LineString"))
                    linestring.set(f"{{{NS['gml']}}}id", f"UUID_{uuid4().hex}")
                    
                    # Extract coordinates from spline points
                    coords_local = np.asarray(
                        [(float(p.co[0]), float(p.co[1]), float(p.co[2])) for p in spline.points],
                        dtype=np.float64
                    )
                    coords_tgt, origin_tgt = trf.transform_with_origin(coords_local, (ox, oy, oz))
                    
                    # Write posList
                    poslist = SubElement(linestring, Q("gml", "posList"))
                    poslist.set("srsDimension", "3")
                    poslist_text = []
                    for coord in coords_tgt:
                        x = float(coord[0]) + origin_tgt[0]
                        y = float(coord[1]) + origin_tgt[1]
                        z = float(coord[2]) + origin_tgt[2]
                        poslist_text.extend([fmt(x), fmt(y), fmt(z)])
                        all_bbox_coords.append((x, y, z))
                    poslist.text = " ".join(poslist_text)
        
        # Add custom attributes (skip if BreaklineRelief - already written)
        if not is_breakline_relief or (ns_key == "veg" and local_name == "SolitaryVegetationObject"):
            _write_custom_attributes(feature, obj, ns_key)
    
    # Export PointCloud objects (Blender 3.0+ native PointClouds)
    pointcloud_objs = [
        o for o in context.scene.objects
        if o.type == 'POINTCLOUD' and object_is_viewport_visible(o, context)
    ]
    
    for obj in pointcloud_objs:
        # Check if this is a CityGML PointCloud feature
        feat_ln = obj.get("cgml3_feature")
        if feat_ln != "PointCloud":
            continue
            
        ns_key, local_name = _feature_tag(obj)
        if (ns_key, local_name) != ("pcl", "PointCloud"):
            continue
            
        feat_id = _feature_id(obj)
        
        # Create pcl:PointCloud feature
        com = add_cityobject_member(root)
        feature = SubElement(com, Q("pcl", "PointCloud"))
        feature.set(f"{{{NS['gml']}}}id", feat_id)
        
        # Add gml:name if exists
        if obj.name and obj.name != feat_id:
            name_el = SubElement(feature, Q("gml", "name"))
            name_el.text = obj.name
        
        # Add lifespan attributes
        _write_lifespan_core(feature, obj)
        
        # Check if we should export as external file or inline points
        has_external_file = obj.get("pcl:pointFile") is not None
        export_inline = not has_external_file  # Export inline if no external file reference
        
        if export_inline:
            # Export inline gml:MultiPoint
            pointcloud_data = obj.data
            point_count = len(pointcloud_data.points)
            
            if point_count > 0:
                # Create pcl:points with gml:MultiPoint
                points_el = SubElement(feature, Q("pcl", "points"))
                multipoint = SubElement(points_el, Q("gml", "MultiPoint"))
                
                # Store multipoint_id if available
                mp_id = obj.get("pcl:multipoint_id")
                if mp_id:
                    multipoint.set(f"{{{NS['gml']}}}id", str(mp_id))
                else:
                    multipoint.set(f"{{{NS['gml']}}}id", f"MultiPoint_{feat_id}")
                
                multipoint.set("srsName", srs_name)
                multipoint.set("srsDimension", "3")
                
                # Get offset
                ox, oy, oz = _resolve_export_offset(context, obj)
                
                # Extract point coordinates
                coords_list = []
                try:
                    # Try foreach_get for performance (Blender 3.3+)
                    coords = np.zeros(point_count * 3, dtype=np.float32)
                    pointcloud_data.points.foreach_get("co", coords)
                    coords_list = coords.reshape((point_count, 3)).tolist()
                except:
                    # Fallback: individual access
                    coords_list = [
                        (float(p.co[0]), float(p.co[1]), float(p.co[2]))
                        for p in pointcloud_data.points
                    ]
                
                # Transform to target CRS
                coords_local = np.asarray(coords_list, dtype=np.float64)
                coords_tgt, origin_tgt = trf.transform_with_origin(coords_local, (ox, oy, oz))
                
                # Write as gml:posList (efficient for large point clouds)
                poslist_el = SubElement(multipoint, Q("gml", "posList"))
                poslist_el.set("srsDimension", "3")
                
                poslist_text = []
                for coord in coords_tgt:
                    x = float(coord[0]) + origin_tgt[0]
                    y = float(coord[1]) + origin_tgt[1]
                    z = float(coord[2]) + origin_tgt[2]
                    poslist_text.extend([fmt(x), fmt(y), fmt(z)])
                    all_bbox_coords.append((x, y, z))
                
                poslist_el.text = " ".join(poslist_text)
        
        else:
            # Export with external file reference
            # pcl:mimeType
            mime_type = obj.get("pcl:mimeType")
            if mime_type:
                mime_el = SubElement(feature, Q("pcl", "mimeType"))
                mime_el.text = str(mime_type)
            
            # pcl:pointFile
            point_file = obj.get("pcl:pointFile")
            if point_file:
                pf_el = SubElement(feature, Q("pcl", "pointFile"))
                pf_el.text = str(point_file)
            
            # pcl:pointFileSrsName
            point_file_srs = obj.get("pcl:pointFileSrsName")
            if point_file_srs:
                pfs_el = SubElement(feature, Q("pcl", "pointFileSrsName"))
                pfs_el.text = str(point_file_srs)
    
    # Export Empty objects as Point geometries or special features (CityObjectGroup)
    for obj in empty_objs:
        # Check if this is a CityGML feature
        feat_ln = obj.get("cgml3_feature")
        if not feat_ln:
            continue
            
        ns_key, local_name = _feature_tag(obj)
        feat_id = _feature_id(obj)
        
        # Special handling for CityObjectGroup (grp:CityObjectGroup)
        if (ns_key, local_name) == ("grp", "CityObjectGroup"):
            # Create CityObjectGroup feature
            com = add_cityobject_member(root)
            feature = SubElement(com, Q(ns_key, local_name))
            feature.set(f"{{{NS['gml']}}}id", feat_id)

            # Erst die semantischen Attribute (yearOfConstruction, address etc.)
            # Für SolitaryVegetationObject schreiben wir veg:-Properties in korrekter Reihenfolge direkt im Branch.
            # Daher NICHT nochmal über _write_custom_attributes anhängen (würde nach der Geometrie landen).
            if not (ns_key == "veg" and local_name == "SolitaryVegetationObject"):
                _write_custom_attributes(feature, obj, ns_key)
            
            # Add gml:name if exists
            if obj.name and obj.name != feat_id:
                name_el = SubElement(feature, Q("gml", "name"))
                name_el.text = obj.name
            
            # Add lifespan attributes (must come after gml:name, before specific attributes)
            _write_lifespan_core(feature, obj)
            
            # grp:class
            gclass = obj.get("grp:class")
            if gclass is not None:
                class_el = SubElement(feature, Q("grp", "class"))
                class_el.text = str(gclass).strip()
            
            # grp:function (can be multiple)
            gfunc = obj.get("grp:function")
            if gfunc is not None:
                for fval in _iter_attr_values(gfunc):
                    func_el = SubElement(feature, Q("grp", "function"))
                    func_el.text = str(fval).strip()
            
            # grp:usage (can be multiple)
            gusage = obj.get("grp:usage")
            if gusage is not None:
                for uval in _iter_attr_values(gusage):
                    usage_el = SubElement(feature, Q("grp", "usage"))
                    usage_el.text = str(uval).strip()
            
            # grp:groupMember - export children as group members
            for child in obj.children:
                if not object_is_viewport_visible(child, context):
                    continue
                # Get child's gml:id
                child_gml_id = _feature_id(child)
                
                # CityGML 2.0: role as attribute on groupMember (not nested)
                # Retrieve role with 3-level fallback:
                role_val = None
                # 1. Check group object: grp:role_<member_id>
                role_key = f"grp:role_{child_gml_id}"
                if role_key in obj:
                    role_val = obj.get(role_key)
                
                # 2. Fallback: grp:role_in_group on child
                if role_val is None:
                    role_val = child.get("grp:role_in_group")
                
                # 3. Legacy: grp:role on child
                if role_val is None:
                    role_val = child.get("grp:role")
                
                # Create groupMember with optional role attribute
                gm_el = SubElement(feature, Q("grp", "groupMember"))
                if role_val is not None and str(role_val).strip():
                    gm_el.set("role", str(role_val).strip())
                
                # groupMember reference (xlink:href)
                gm_el.set(Q("xlink", "href"), f"#{child_gml_id}")
            
            # grp:parent (parent-Referenz, falls vorhanden)
            parent_id = obj.get("grp:parent_id")
            if parent_id is not None and str(parent_id).strip():
                parent_el = SubElement(feature, Q("grp", "parent"))
                parent_el.set(Q("xlink", "href"), f"#{str(parent_id).strip()}")
            
            # Generics schreiben
            _write_generics(feature, obj)
            
            continue  # CityObjectGroup hat keine Geometrie
        
        # Default: Point geometry for other Empty objects
        # Create feature element
        com = add_cityobject_member(root)
        feature = SubElement(com, Q(ns_key, local_name))
        feature.set(f"{{{NS['gml']}}}id", feat_id)

        # Erst die semantischen Attribute (yearOfConstruction, address etc.)
        # Für SolitaryVegetationObject schreiben wir veg:-Properties in korrekter Reihenfolge direkt im Branch.
        # Daher NICHT nochmal über _write_custom_attributes anhängen (würde nach der Geometrie landen).
        if not (ns_key == "veg" and local_name == "SolitaryVegetationObject"):
            _write_custom_attributes(feature, obj, ns_key)
        
        # Add gml:name if exists
        if obj.name and obj.name != feat_id:
            name_el = SubElement(feature, Q("gml", "name"))
            name_el.text = obj.name
        
        # Add Point geometry (LOD0)
        ox, oy, oz = _resolve_export_offset(context, obj)
        
        # Get object location (world space)
        loc = obj.matrix_world.translation
        
        # Transform to target CRS if needed
        coords_local = np.asarray([(float(loc.x), float(loc.y), float(loc.z))], dtype=np.float64)
        coords_tgt, origin_tgt = trf.transform_with_origin(coords_local, (ox, oy, oz))
        
        x = float(coords_tgt[0][0]) + origin_tgt[0]
        y = float(coords_tgt[0][1]) + origin_tgt[1]
        z = float(coords_tgt[0][2]) + origin_tgt[2]
        
        # Create lod0Point
        lod0_point = SubElement(feature, Q("core", "lod0Point"))
        point = SubElement(lod0_point, Q("gml", "Point"))
        point.set(f"{{{NS['gml']}}}id", f"UUID_{uuid4().hex}")
        pos = SubElement(point, Q("gml", "pos"))
        pos.set("srsDimension", "3")
        pos.text = f"{fmt(x)} {fmt(y)} {fmt(z)}"
        
        # Add custom attributes
        # Für SolitaryVegetationObject schreiben wir veg:-Properties in korrekter Reihenfolge direkt im Branch.
        # Daher NICHT nochmal über _write_custom_attributes anhängen (würde nach der Geometrie landen).
        if not (ns_key == "veg" and local_name == "SolitaryVegetationObject"):
            _write_custom_attributes(feature, obj, ns_key)
        
        # Update bbox
        all_bbox_coords.extend([(x, y, z)])
    
    # Globales Appearance-Grouping (aus Import: poly['app_id'] / obj['app_id'])
    """ appearance_groups = {}
    appearance_order = [] """
    
    # Note: Hierarchical export already handled at the beginning of the function

    # Sub-feature types that should NOT become standalone cityObjectMembers
    # when they have a parent reference (cgml_parent_id).  Their geometry is
    # already exported inline within the parent feature.
    _INLINE_ONLY_FEATURES = {
        "BuildingPart", "BuildingInstallation", "IntBuildingInstallation", "BuildingFurniture",
        "BridgePart", "BridgeInstallation", "IntBridgeInstallation", "BridgeFurniture",
        "BridgeConstructionElement", "IntBridgeConstructionElement",
        "TunnelPart", "TunnelInstallation", "IntTunnelInstallation", "TunnelFurniture",
    }

    _INSTALLATION_FEATURES_V2 = {
        "BuildingInstallation", "IntBuildingInstallation",
        "BridgeInstallation", "IntBridgeInstallation",
        "TunnelInstallation", "IntTunnelInstallation",
    }

    _INLINE_SUBFEATURE_COLLECTION_NAMES = {
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

    def _object_in_disabled_inline_collection(o) -> bool:
        try:
            collections = list(o.users_collection)
        except Exception:
            collections = []
        for collection in collections:
            name = _collection_base_name(getattr(collection, "name", ""))
            if name not in _INLINE_SUBFEATURE_COLLECTION_NAMES:
                continue
            try:
                marked_inline = bool(collection.get("cgml3_auto_disabled_subfeatures", False))
            except Exception:
                marked_inline = False
            hidden = bool(
                getattr(collection, "hide_viewport", False)
                or getattr(collection, "hide_render", False)
            )
            if marked_inline or hidden:
                return True
        return False

    # ── Collect child sub-feature objects by parent gml_id ──
    # Objects imported as separate Blender objects (e.g. BuildingPart parented
    # under a Building) have cgml_parent_id set.  We collect them here so the
    # parent's export path can write them inline (consistsOfBuildingPart etc.).
    # Also detect children via Blender parenting (e.g. from "Join Object Parts"
    # or "Assign Object Part" operators which don't set cgml_parent_id).
    _child_objects_by_parent_id: Dict[str, list] = {}
    for _co in objs:
        _co_feat = str(_co.get("cgml3_feature", "") or "").strip()
        if _co_feat not in _INLINE_ONLY_FEATURES:
            continue
        _co_pid = str(_co.get("cgml_parent_id", "") or "").strip()
        if not _co_pid and _co.parent:
            # Derive parent ID from Blender parent's gml_id (supports both
            # MESH parents from standard import and EMPTY parents from
            # hierarchical import or manual assignment).
            _co_pid = str(_co.parent.get("gml_id", "") or "").strip()
            if _co_pid:
                _co["cgml_parent_id"] = _co_pid
        if _co_pid:
            _child_objects_by_parent_id.setdefault(_co_pid, []).append(_co)

    # Map parent feature gml_id → XML element, populated during export so
    # child objects can be written inline even if processed after the parent.
    _parent_feature_elements: Dict[str, Any] = {}

    # Mapping of sub-feature type → (namespace, relation element, feature element)
    # for writing child objects inline on the parent feature.
    # NOTE: This is also defined later inside the nested export scope for
    # the material-based inline-part system.  We duplicate it here so that the
    # child-object path (separate Blender objects) can look it up before that
    # inner definition is reached.
    _CHILD_OBJ_PART_RELATIONS = {
        "BuildingPart": ("bldg", "consistsOfBuildingPart", "BuildingPart"),
        "BuildingInstallation": ("bldg", "outerBuildingInstallation", "BuildingInstallation"),
        "IntBuildingInstallation": ("bldg", "interiorBuildingInstallation", "IntBuildingInstallation"),
        "BuildingFurniture": ("bldg", "interiorFurniture", "BuildingFurniture"),
        "BridgePart": ("brid", "consistsOfBridgePart", "BridgePart"),
        "BridgeInstallation": ("brid", "outerBridgeInstallation", "BridgeInstallation"),
        "IntBridgeInstallation": ("brid", "interiorBridgeInstallation", "IntBridgeInstallation"),
        "BridgeConstructionElement": ("brid", "outerBridgeConstruction", "BridgeConstructionElement"),
        "IntBridgeConstructionElement": ("brid", "interiorBridgeConstruction", "IntBridgeConstructionElement"),
        "BridgeFurniture": ("brid", "interiorBridgeFurniture", "BridgeFurniture"),
        "TunnelPart": ("tun", "consistsOfTunnelPart", "TunnelPart"),
        "TunnelInstallation": ("tun", "outerTunnelInstallation", "TunnelInstallation"),
        "IntTunnelInstallation": ("tun", "interiorTunnelInstallation", "IntTunnelInstallation"),
        "TunnelFurniture": ("tun", "interiorTunnelFurniture", "TunnelFurniture"),
    }

    # Sort objects so that parents are exported before their inline children.
    # Children with cgml_parent_id must come after the parent so that the
    # parent's feature element is available for inline writing.
    _child_ids = set()
    for _co_list in _child_objects_by_parent_id.values():
        for _co in _co_list:
            _child_ids.add(id(_co))
    objs = [o for o in objs if id(o) not in _child_ids] + [o for o in objs if id(o) in _child_ids]

    for obj in objs:
        # Skip if already exported as part of hierarchical structure
        if obj.name in exported_object_names:
            continue

        # Skip linked duplicates whose mesh data was already exported hierarchically
        if obj.type == 'MESH' and obj.data and f"__mesh_data__{obj.data.name}" in exported_object_names:
            continue

        # Skip objects whose polygon IDs were already exported hierarchically
        # (catches separate objects with same materials/polygon UUIDs)
        if obj.type == 'MESH' and obj.material_slots:
            _all_poly_ids_used = True
            _has_poly_ids = False
            for _slot in obj.material_slots:
                if _slot.material:
                    _pid = _slot.material.get("gml_polygon_id")
                    if _pid:
                        _has_poly_ids = True
                        if f"__poly_id__{str(_pid).strip()}" not in exported_object_names:
                            _all_poly_ids_used = False
                            break
            if _has_poly_ids and _all_poly_ids_used:
                continue

        # Skip sub-feature objects that belong to a parent (already written
        # inline via outerBuildingInstallation etc. on the parent feature).
        # If the parent feature element has been exported, export this child
        # object as an inline part on it instead of skipping.
        _obj_feat = str(obj.get("cgml3_feature", "") or "").strip()
        _is_inline_child = False
        _inline_parent_feature = None
        if _obj_feat in _INLINE_ONLY_FEATURES and obj.get("cgml_parent_id"):
            _parent_id = str(obj["cgml_parent_id"]).strip()
            _inline_parent_feature = _parent_feature_elements.get(_parent_id)
            if _inline_parent_feature is None:
                # Parent not (yet) exported – skip (matches old behaviour)
                continue
            _is_inline_child = True

        if _object_in_disabled_inline_collection(obj) and not _is_inline_child:
            continue

        # Skip mesh descendants (at any depth) of hierarchical buildings
        _ancestor = obj.parent
        _skip_hierarchical = False
        while _ancestor:
            if _ancestor.type == 'EMPTY' and _ancestor.get("structure_type") == "hierarchical":
                _skip_hierarchical = True
                break
            _ancestor = _ancestor.parent
        if _skip_hierarchical:
            continue
        
        # ========================================================================
        # STANDARD MESH EXPORT
        # ========================================================================
        
        if _is_inline_child:
            com = None  # not used for inline children
        else:
            com = add_cityobject_member(root)
        deps = context.evaluated_depsgraph_get()
        obj_eval = obj.evaluated_get(deps)
        mesh = obj_eval.to_mesh(preserve_all_data_layers=True, depsgraph=deps)
        
        # Check if mesh has vertices but no polygons (MultiPoint geometry)
        if mesh and len(mesh.vertices) > 0 and not mesh.polygons:
            if _is_inline_child:
                # Inline children without polygons cannot be exported meaningfully
                if obj_eval:
                    obj_eval.to_mesh_clear()
                continue
            ns_key, local_name = _feature_tag(obj)
            feat_id = _feature_id(obj)
            
            feature = SubElement(com, Q(ns_key, local_name))
            feature.set(f"{{{NS['gml']}}}id", feat_id)
            
            # Add gml:name if exists
            if obj.name and obj.name != feat_id:
                name_el = SubElement(feature, Q("gml", "name"))
                name_el.text = obj.name
            
            ox, oy, oz = _resolve_export_offset(context, obj)
            
            # Check if this is RasterRelief with grid topology
            is_raster_relief = (ns_key, local_name) == ("dem", "RasterRelief")
            if is_raster_relief and can_export_as_raster_relief(obj):
                # Export RasterRelief with RectifiedGridCoverage
                grid_data = export_rectified_grid_coverage(obj, obj.matrix_world, trf, (ox, oy, oz), srs_name)
                if grid_data:
                    # Write dem:lod (required)
                    _write_specific_attributes(feature, obj)
                    
                    # Create dem:grid element with RectifiedGridCoverage
                    grid_el = SubElement(feature, Q("dem", "grid"))
                    coverage = SubElement(grid_el, Q("gml", "RectifiedGridCoverage"))
                    coverage.set(f"{{{NS['gml']}}}id", grid_data.get('grid_id', f"UUID_{uuid4().hex}"))
                    
                    # Add gml:domainSet with RectifiedGrid
                    domain_set = SubElement(coverage, Q("gml", "domainSet"))
                    rectified_grid = SubElement(domain_set, Q("gml", "RectifiedGrid"))
                    rectified_grid.set(f"{{{NS['gml']}}}id", f"UUID_{uuid4().hex}")
                    rectified_grid.set("dimension", "2")
                    
                    # gml:limits with GridEnvelope
                    limits = SubElement(rectified_grid, Q("gml", "limits"))
                    envelope = SubElement(limits, Q("gml", "GridEnvelope"))
                    low_el = SubElement(envelope, Q("gml", "low"))
                    low_el.text = f"0 0"
                    high_el = SubElement(envelope, Q("gml", "high"))
                    high_el.text = f"{grid_data['cols']-1} {grid_data['rows']-1}"
                    
                    # gml:axisLabels
                    axis_labels = SubElement(rectified_grid, Q("gml", "axisLabels"))
                    axis_labels.text = "x y"
                    
                    # gml:origin
                    origin_el = SubElement(rectified_grid, Q("gml", "origin"))
                    origin_point = SubElement(origin_el, Q("gml", "Point"))
                    origin_point.set(f"{{{NS['gml']}}}id", f"UUID_{uuid4().hex}")
                    origin_point.set("srsName", srs_name)
                    origin_pos = SubElement(origin_point, Q("gml", "pos"))
                    origin_pos.set("srsDimension", "3")
                    ox_grid, oy_grid, oz_grid = grid_data['origin']
                    origin_pos.text = f"{fmt(ox_grid)} {fmt(oy_grid)} {fmt(oz_grid)}"
                    
                    # gml:offsetVector (X direction)
                    offset_x_el = SubElement(rectified_grid, Q("gml", "offsetVector"))
                    offset_x_el.set("srsName", srs_name)
                    offset_x = grid_data['offset_x']
                    offset_x_el.text = f"{fmt(offset_x[0])} {fmt(offset_x[1])} {fmt(offset_x[2])}"
                    
                    # gml:offsetVector (Y direction)
                    offset_y_el = SubElement(rectified_grid, Q("gml", "offsetVector"))
                    offset_y_el.set("srsName", srs_name)
                    offset_y = grid_data['offset_y']
                    offset_y_el.text = f"{fmt(offset_y[0])} {fmt(offset_y[1])} {fmt(offset_y[2])}"
                    
                    # gml:rangeSet with DataBlock
                    range_set = SubElement(coverage, Q("gml", "rangeSet"))
                    data_block = SubElement(range_set, Q("gml", "DataBlock"))
                    tuple_list = SubElement(data_block, Q("gml", "tupleList"))
                    tuple_list.set("cs", " ")
                    # Write height values as space-separated list
                    tuple_list.text = " ".join(fmt(v) for v in grid_data['values'])
                    
                    # Update bbox with grid bounds
                    for row in range(grid_data['rows']):
                        for col in range(grid_data['cols']):
                            idx = row * grid_data['cols'] + col
                            x = ox_grid + col * offset_x[0] + row * offset_y[0]
                            y = oy_grid + col * offset_x[1] + row * offset_y[1]
                            z = grid_data['values'][idx]
                            all_bbox_coords.append((x, y, z))
                    
                    if obj_eval:
                        obj_eval.to_mesh_clear()
                    continue
            
            # Transform vertices to target CRS
            coords_local = np.asarray(
                [(float(v.co.x), float(v.co.y), float(v.co.z)) for v in mesh.vertices],
                dtype=np.float64
            )
            coords_tgt, origin_tgt = trf.transform_with_origin(coords_local, (ox, oy, oz))
            
            # Check if this is MassPointRelief (uses dem:reliefPoints)
            is_mass_point_relief = (ns_key, local_name) == ("dem", "MassPointRelief")
            
            # Check if this is a PointCloud (marked by custom property or POINTCLOUD object type)
            is_pointcloud = (
                obj.get("cgml3_is_pointcloud", False) or
                (hasattr(obj, 'type') and obj.type == 'POINTCLOUD')
            )
            
            # Write custom attributes BEFORE geometry (correct XSD order)
            # Schema order: AbstractCityObject (genericAttribute) → AbstractSpace (LOD geoms) → AbstractPhysicalSpace (pointCloud) → BuildingRoom (class, function, etc.)
            if not (ns_key == "veg" and local_name == "SolitaryVegetationObject"):
                _write_custom_attributes(feature, obj, ns_key)
            
            if is_mass_point_relief:
                # MassPointRelief: write dem:lod (required, comes after genericAttribute)
                _write_specific_attributes(feature, obj)
                
                # Create dem:reliefPoints with MultiPoint
                relief_points_el = SubElement(feature, Q("dem", "reliefPoints"))
                multipoint = SubElement(relief_points_el, Q("gml", "MultiPoint"))
            else:
                # All MultiPoint objects should be exported as PointCloud
                # (AbstractSpace has no lod0MultiPoint element in CityGML 3.0)
                
                # Create core:pointCloud wrapper (comes BEFORE BuildingRoom-specific attributes)
                pointcloud_wrapper = SubElement(feature, Q("core", "pointCloud"))
                pcl_pointcloud = SubElement(pointcloud_wrapper, Q("pcl", "PointCloud"))
                pcl_points = SubElement(pcl_pointcloud, Q("pcl", "points"))
                multipoint = SubElement(pcl_points, Q("gml", "MultiPoint"))
                # Use gml:pointMembers (plural) as container for all points
                pointmembers = SubElement(multipoint, Q("gml", "pointMembers"))
                is_pointcloud = True  # Treat all MultiPoint as PointCloud
            
            # All MultiPoint objects are exported as PointCloud (no gml:id on MultiPoint)
            
            for i, coord in enumerate(coords_tgt):
                x = float(coord[0]) + origin_tgt[0]
                y = float(coord[1]) + origin_tgt[1]
                z = float(coord[2]) + origin_tgt[2]
                
                # All MultiPoint objects use gml:pointMembers container
                point = SubElement(pointmembers, Q("gml", "Point"))
                
                pos = SubElement(point, Q("gml", "pos"))
                pos.text = f"{fmt(x)} {fmt(y)} {fmt(z)}"
                
                # Update bbox
                all_bbox_coords.append((x, y, z))
            
            # Write BuildingRoom-specific attributes AFTER pointCloud (correct XSD order)
            if not is_mass_point_relief:
                _write_specific_attributes(feature, obj)
            
            if obj_eval:
                obj_eval.to_mesh_clear()
            continue
        
        if not mesh or not mesh.polygons:
            if obj_eval:
                obj_eval.to_mesh_clear()
            continue

        # Prüfen, ob dieses Objekt aus einer ImplicitGeometry stammt
        mesh_orig = obj.data
        try:
            is_implicit = bool(mesh_orig.get("ImplicitGeometry", False))
        except Exception:
            is_implicit = False

        meta_for_mesh = None
        is_imp_prototype = False
        if is_implicit:
            meta_for_mesh = implicit_meta_by_mesh.get(mesh_orig)
            if meta_for_mesh is None:
                meta_for_mesh = {
                    'ms_id': None,
                    'ref_base': None,
                    'probe_abs': None,
                    'app_id': None,
                }
                implicit_meta_by_mesh[mesh_orig] = meta_for_mesh
                is_imp_prototype = True

        ns_key, local_name = _feature_tag(obj)
        # Doppelseitige Appearance nur, wenn explizit gewünscht.
        # Siehe io/writer/exporter.py: Default-Doppelseitig erzeugt doppelte TextureAssociation
        # (isFront=true/false) und führt oft zu falscher/überschriebener Texture-Wirkung.
        double_sided = bool(
            obj.get("cgml3_double_sided", False)
            or obj.get("double_sided", False)
        )

        feat_id = _feature_id(obj)
        if _is_inline_child:
            # Write as inline child of the parent feature via the relation element
            _relation = _CHILD_OBJ_PART_RELATIONS.get(_obj_feat)
            if _relation:
                _rel_ns, _rel_local, _feat_local = _relation
                feature = SubElement(
                    SubElement(_inline_parent_feature, Q(_rel_ns, _rel_local)),
                    Q(_rel_ns, _feat_local),
                    {GML_ID: feat_id},
                )
            else:
                # Fallback: write as direct child (should not happen for known types)
                feature = SubElement(_inline_parent_feature, Q(ns_key, local_name))
                feature.set(f"{{{NS['gml']}}}id", feat_id)
        else:
            feature = SubElement(com, Q(ns_key, local_name))
            feature.set(f"{{{NS['gml']}}}id", feat_id)

        # Store parent feature element so child objects can be written inline later.
        # Use the raw gml_id (before make_gml_id processing) as key because
        # cgml_parent_id on child objects stores the raw imported ID.
        _raw_gml_id = str(obj.get("gml_id", "") or "").strip()
        if _raw_gml_id and _raw_gml_id in _child_objects_by_parent_id:
            _parent_feature_elements[_raw_gml_id] = feature

        # Spezielle Behandlung für ReliefFeature (Container für reliefComponent)
        # WICHTIG: ReliefFeature MUSS mindestens ein reliefComponent enthalten (laut XSD)
        # Wenn ein Objekt als ReliefFeature markiert ist ohne Blender-Kinder,
        # überspringen wir es, da es ungültig wäre.
        if (ns_key, local_name) == ("dem", "ReliefFeature"):
            # Prüfe ob das Objekt Kinder hat
            has_relief_children = False
            if obj.children:
                for child_obj in obj.children:
                    if not object_is_viewport_visible(child_obj, context):
                        continue
                    child_ns, child_local = _feature_tag(child_obj)
                    if child_ns == "dem" and child_local in ("TINRelief", "RasterRelief", "MassPointRelief", "BreaklineRelief"):
                        has_relief_children = True
                        break
            
            if not has_relief_children:
                # ReliefFeature ohne reliefComponent ist ungültig - überspringe es
                # oder exportiere es als TINRelief wenn es Geometrie hat
                if obj_eval:
                    obj_eval.to_mesh_clear()
                # Entferne das ungültige cityObjectMember
                root.remove(com)
                continue
            
            # ReliefFeature mit Kindern: dem:lod schreiben
            _write_specific_attributes(feature, obj)
            
            # Exportiere Kinder als inline reliefComponent
            for child_obj in obj.children:
                if not object_is_viewport_visible(child_obj, context):
                    continue
                child_ns, child_local = _feature_tag(child_obj)
                if child_ns == "dem" and child_local in ("TINRelief", "RasterRelief", "MassPointRelief", "BreaklineRelief"):
                    # Create reliefComponent wrapper
                    relief_comp = SubElement(feature, Q("dem", "reliefComponent"))
                    
                    # Create the actual relief element (TINRelief, etc.)
                    raw_child_id = child_obj.get("gml_id")
                    if raw_child_id:
                        # XSD-Konformität sicherstellen
                        child_gml_id = make_gml_id(str(raw_child_id), used_feat_ids)
                    else:
                        child_gml_id = f"ID_{uuid4()}"
                    relief_elem = SubElement(relief_comp, Q("dem", child_local), {GML_ID: child_gml_id})
                    
                    # Write dem:lod (required for all relief components)
                    try:
                        lod_val = child_obj.get("dem:lod")
                        if lod_val is not None:
                            SubElement(relief_elem, Q("dem", "lod")).text = str(lod_val)
                    except Exception:
                        pass
                    
                    # Write generic attributes for relief component
                    _write_generic_attributes(relief_elem, child_obj)
                    
                    # Write geometry based on relief type
                    child_mesh = child_obj.data
                    if child_mesh and hasattr(child_mesh, 'vertices'):
                        child_eval = child_obj.evaluated_get(depsgraph)
                        child_mesh_eval = child_eval.data if child_eval else child_mesh
                        
                        if child_local == "TINRelief":
                            # TINRelief uses dem:tin (TriangulatedSurface)
                            tin_elem = SubElement(relief_elem, Q("dem", "tin"))
                            tin_surf = SubElement(tin_elem, Q("gml", "TriangulatedSurface"), {GML_ID: f"ID_{uuid4()}"})
                            
                            # Export triangles
                            for poly in child_mesh_eval.polygons:
                                if len(poly.vertices) == 3:
                                    tri_patch = SubElement(tin_surf, Q("gml", "trianglePatches"))
                                    triangle = SubElement(tri_patch, Q("gml", "Triangle"))
                                    ext = SubElement(triangle, Q("gml", "exterior"))
                                    ring = SubElement(ext, Q("gml", "LinearRing"))
                                    posList = SubElement(ring, Q("gml", "posList"), {"srsDimension": "3"})
                                    
                                    coords = []
                                    for v_idx in poly.vertices:
                                        v = child_mesh_eval.vertices[v_idx]
                                        coords.extend([fmt(v.co.x + ox), fmt(v.co.y + oy), fmt(v.co.z + oz)])
                                    # Close ring
                                    v0 = child_mesh_eval.vertices[poly.vertices[0]]
                                    coords.extend([fmt(v0.co.x + ox), fmt(v0.co.y + oy), fmt(v0.co.z + oz)])
                                    posList.text = " ".join(coords)
                        
                        elif child_local == "MassPointRelief":
                            # MassPointRelief uses dem:reliefPoints (MultiPoint)
                            pts_elem = SubElement(relief_elem, Q("dem", "reliefPoints"))
                            mp = SubElement(pts_elem, Q("gml", "MultiPoint"), {GML_ID: f"ID_{uuid4()}"})
                            
                            for v in child_mesh_eval.vertices:
                                pt_member = SubElement(mp, Q("gml", "pointMember"))
                                pt = SubElement(pt_member, Q("gml", "Point"), {GML_ID: f"ID_{uuid4()}"})
                                pos = SubElement(pt, Q("gml", "pos"), {"srsDimension": "3"})
                                pos.text = f"{fmt(v.co.x + ox)} {fmt(v.co.y + oy)} {fmt(v.co.z + oz)}"
                        
                        elif child_local == "BreaklineRelief":
                            # BreaklineRelief uses dem:breaklines or dem:ridgeOrValleyLines (MultiCurve)
                            is_ridge = child_obj.get("dem:ridgeOrValleyLines", False)
                            tag_name = "ridgeOrValleyLines" if is_ridge else "breaklines"
                            
                            lines_elem = SubElement(relief_elem, Q("dem", tag_name))
                            mc = SubElement(lines_elem, Q("gml", "MultiCurve"), {GML_ID: f"ID_{uuid4()}"})
                            
                            # Export edges as LineStrings
                            for edge in child_mesh_eval.edges:
                                curve_member = SubElement(mc, Q("gml", "curveMember"))
                                ls = SubElement(curve_member, Q("gml", "LineString"), {GML_ID: f"ID_{uuid4()}"})
                                posList = SubElement(ls, Q("gml", "posList"), {"srsDimension": "3"})
                                
                                v1 = child_mesh_eval.vertices[edge.vertices[0]]
                                v2 = child_mesh_eval.vertices[edge.vertices[1]]
                                coords = [
                                    fmt(v1.co.x + ox), fmt(v1.co.y + oy), fmt(v1.co.z + oz),
                                    fmt(v2.co.x + ox), fmt(v2.co.y + oy), fmt(v2.co.z + oz)
                                ]
                                posList.text = " ".join(coords)
                        
                        if child_eval:
                            child_eval.to_mesh_clear()
            
            # ReliefFeature selbst hat keine Geometrie
            if obj_eval:
                obj_eval.to_mesh_clear()
            continue

        # Prüfen, ob dieses Objekt aus einer ImplicitGeometry stammt
        mesh_orig = obj.data
        try:
            is_implicit = bool(mesh_orig.get("ImplicitGeometry", False))
        except Exception:
            is_implicit = False

        # für welche Feature-Typen ist ein GTX-Fallback überhaupt sinnvoll?
        allow_gtx_fallback = export_gtx and (ns_key not in ("frn", "brid")) and (not is_implicit)

        ox, oy, oz = _resolve_export_offset(context, obj)
        effective_autofill_new_surfaces = bool(autofill_new_surfaces)

        pairs = []
        if use_inner_outer_script or effective_autofill_new_surfaces:
            from ...ops.get_inner_and_outer_rings import detect_face_groups
            pairs = detect_face_groups(obj_eval, mesh)
        if use_inner_outer_script:
            outer_to_inners = {}
            for outer, inner in pairs:
                outer_to_inners.setdefault(outer["index"], []).append(inner["index"])
        else:
            # Keine automatische Erkennung: keine Interior-Ringe verknüpfen
            outer_to_inners = {}

        face_overrides = {}
        if effective_autofill_new_surfaces:
            face_overrides = prepare_new_face_export_overrides(obj, obj_eval, mesh, pairs)

        # defaultdict verwenden, damit fehlende Keys automatisch als leere Listen angelegt werden
        geom_buf: Dict[str, List[Dict[str, object]]] = defaultdict(list)

        # Nur für den ID-basierten Weg (ohne get_inner_and_outer_rings):
        # Gruppiert alle Ringe (Faces) pro gml:Polygon-ID.
        poly_groups: Dict[str, dict] = {} if not use_inner_outer_script else {}

        # Ring-Index je Polygon-ID (für saubere _0_, _1_, _2_-IDs)
        ring_index_by_poly: Dict[str, int] = {}

        # Polygon-/Ring-IDs are written as gml:id and therefore must be unique
        # across the whole exported document, not only inside one Blender object.
        claimed_poly_ids_by_raw: Dict[str, str] = {}

        def _claim_poly_id(raw_poly_id: str) -> str:
            raw_poly_id = str(raw_poly_id or "").strip()
            if not raw_poly_id:
                return make_gml_id(f"UUID_{uuid4().hex}", used_feat_ids)
            claimed = claimed_poly_ids_by_raw.get(raw_poly_id)
            if claimed:
                return claimed
            claimed = make_gml_id(raw_poly_id, used_feat_ids)
            claimed_poly_ids_by_raw[raw_poly_id] = claimed
            return claimed

        def _claim_ring_id(raw_ring_id: str) -> str:
            raw_ring_id = str(raw_ring_id or "").strip()
            if not raw_ring_id:
                raw_ring_id = f"UUID_{uuid4().hex}"
            return make_gml_id(raw_ring_id, used_feat_ids)

        bbox_coords_abs = []
        ptx_by_image: Dict[str, List[dict]] = {}
        gtx_by_image: Dict[str, List[dict]] = {}
        x3d_by_key: Dict[Tuple[str, Tuple[float,float,float,float]], List[str]] = {}
        exported_faces = set()
        probe_abs_cur: List[Tuple[float, float, float]] = []
        # Track app_id per gml_polygon_id so that faces sharing a polygon
        # (exterior + interior rings) end up in the same Appearance group.
        _app_id_by_poly: Dict[str, str] = {}

        for fidx, poly in enumerate(mesh.polygons):
            # Nur beim Skript-Weg Innenflächen überspringen
            if use_inner_outer_script and fidx in exported_faces:
                continue

            face_override = face_overrides.get(fidx)
            raw_surface_type = face_override.get("surface_type") if face_override else None
            st = _normalize_face_surface_type(raw_surface_type) or default_unclassified_surface_type
            try:
                mi = poly.material_index
                mat = face_override.get("mat") if face_override else None
                if mat is None:
                    mat = obj.material_slots[mi].material if 0 <= mi < len(obj.material_slots) else None
                if mat and not (face_override and face_override.get("surface_type")):
                    # Unterstütze beide Property-Namen: 'SurfaceTyp' und 'surface_type'
                    v = mat.get("SurfaceTyp") or mat.get("surface_type")
                    normalized_surface_type = _normalize_face_surface_type(v)
                    if normalized_surface_type:
                        raw_surface_type = str(v).strip()
                        st = normalized_surface_type
                    elif v in (
                        # Construction Surfaces
                        "RoofSurface",
                        "WallSurface",
                        "GroundSurface",
                        "OuterCeilingSurface",
                        "OuterFloorSurface",
                        "ClosureSurface",
                        "CeilingSurface",
                        "InteriorWallSurface",
                        "FloorSurface",
                        # Building
                        "BuildingPart",
                        "BuildingConstructiveElement",
                        "BuildingInstallation",
                        "IntBuildingInstallation",
                        "BuildingFurniture",
                        # Filling Elements
                        "Window",
                        "Door",
                        "WindowSurface",
                        "DoorSurface",
                        # Bridge
                        "BridgeConstructionElement",
                        "IntBridgeConstructionElement",
                        "BridgeInstallation",
                        "IntBridgeInstallation",
                        "BridgeFurniture",
                        "Bridge",
                        "BridgePart",
                        # Tunnel
                        "TunnelConstructiveElement",
                        "TunnelInstallation",
                        "IntTunnelInstallation",
                        "TunnelFurniture",
                        "Tunnel",
                        "TunnelPart",
                        "HollowSpace",
                        # Transportation
                        "TrafficArea",
                        "AuxiliaryTrafficArea",
                        "TrafficSpace",
                        "AuxiliaryTrafficSpace",
                        "Marking",
                        "Hole",
                        "HoleSurface",
                        "Road",
                        "Railway",
                        "Track",
                        "Square",
                        "Waterway",
                        "Section",
                        "Intersection",
                        "ClearanceSpace",
                        # Feature-Level
                        "CityFurniture",
                        "SolitaryVegetationObject",
                        "PlantCover",
                        "WaterBody",
                        "LandUse",
                        "OtherConstruction",
                        # Relief
                        "TINRelief",
                        "MassPointRelief",
                        "BreaklineRelief",
                        "RasterRelief",
                        "ReliefFeature",
                    ):
                        st = v
            except Exception:
                mat = face_override.get("mat") if face_override else None

            subfeature_type = _normalize_face_surface_type(
                _first_nonempty_prop(
                    (face_override, mat, obj),
                    ("cgml3_subfeature_type", "cgml_part_type", "part_type"),
                )
            )
            if subfeature_type not in {
                "BuildingPart", "BridgePart", "TunnelPart",
                "BuildingInstallation", "IntBuildingInstallation", "BuildingFurniture",
                "BridgeInstallation", "IntBridgeInstallation", "BridgeConstructionElement",
                "IntBridgeConstructionElement", "BridgeFurniture",
                "TunnelInstallation", "IntTunnelInstallation", "TunnelFurniture",
            }:
                subfeature_type = ""
            subfeature_id = _first_nonempty_prop(
                (face_override, mat, obj),
                ("cgml3_subfeature_id", "cgml_part_id", "part_id"),
            )
            subfeature_parent_id = _first_nonempty_prop(
                (face_override, mat, obj),
                ("cgml3_subfeature_parent_id", "cgml_parent_id", "parent_id"),
            )
            subfeature_relation = _first_nonempty_prop(
                (face_override, mat, obj),
                ("cgml3_subfeature_relation", "cgml_part_relation", "part_relation"),
            )
            subfeature_name = _first_nonempty_prop(
                (face_override, mat, obj),
                ("cgml3_subfeature_name", "cgml_part_name", "part_name"),
            )

            # Roh-Vertices in Weltkoordinaten sammeln (float64, ohne Origin)
            ring_xyz_local = []
            for li in poly.loop_indices:
                vi = mesh.loops[li].vertex_index
                vco = obj_eval.matrix_world @ mesh.vertices[vi].co
                ring_xyz_local.append((float(vco.x), float(vco.y), float(vco.z)))
            ring_xyz_local = _ensure_closed2d(ring_xyz_local)

            origin_src = (ox, oy, oz)
            ring_xyz_arr = np.asarray(ring_xyz_local, dtype=np.float64)

            # Transformiere und gib ABSOLUT aus: T(P+O)
            ring_tgt_local, origin_tgt = trf.transform_with_origin(ring_xyz_arr, origin_src)
            ring_xyz_abs = (ring_tgt_local + np.asarray(origin_tgt, dtype=np.float64)).tolist()
            ring_xyz = [(float(p[0]), float(p[1]), float(p[2])) for p in ring_xyz_abs]

            if len(probe_abs_cur) < MAX_PROBE_POINTS:
                for pt in ring_xyz:
                    probe_abs_cur.append(pt)
                    if len(probe_abs_cur) >= MAX_PROBE_POINTS:
                        break

            bbox_coords_abs.extend(ring_xyz)

            uvs_ext = None
            if mesh.uv_layers.active and poly.loop_indices:
                uvs_ext = []
                for li in poly.loop_indices:
                    uv = mesh.uv_layers.active.data[li].uv
                    uvs_ext.append((float(uv.x), float(uv.y)))
                uvs_ext = _ensure_ring_closed(uvs_ext)
                try:
                    before_uvs = list(uvs_ext)
                    uvs_ext = _order_uvs_for_loop_vertices(mesh, poly.loop_indices, ring_xyz_local, uvs_ext, world_matrix=obj_eval.matrix_world)
                except Exception as e:
                    pass
                # If this scene was imported from CityGML, preserve the original TexCoordList start point
                # No in-place rotation here; TexCoordList rotation handled in appearance writer using uv_start.
                # Optional: V-Achse flippen, wenn Szene/Objekt es verlangt.
                # (siehe io/writer/exporter.py für Details)
                try:
                    if bool(obj.get("cgml3_flip_v", False)):
                        uvs_ext = [(u, 1.0 - v) for (u, v) in uvs_ext]
                except Exception:
                    pass

            if face_override and face_override.get("uvs"):
                uvs_ext = _ensure_ring_closed(list(face_override["uvs"]))
                try:
                    if bool(obj.get("cgml3_flip_v", False)):
                        uvs_ext = [(u, 1.0 - v) for (u, v) in uvs_ext]
                except Exception:
                    pass

            inner_ids = []
            interior_rings_xyz = []
            interior_uvs = []

            if use_inner_outer_script:
                inner_ids = outer_to_inners.get(fidx, [])
                for iid in inner_ids:
                    inner_override = face_overrides.get(iid)
                    ip = mesh.polygons[iid]
                    ir_xyz_local = []
                    for li in ip.loop_indices:
                        vi = mesh.loops[li].vertex_index
                        vco = obj_eval.matrix_world @ mesh.vertices[vi].co
                        ir_xyz_local.append((float(vco.x), float(vco.y), float(vco.z)))
                    ir_xyz_local = _ensure_closed2d(ir_xyz_local)

                    ir_arr = np.asarray(ir_xyz_local, dtype=np.float64)
                    ir_tgt_local, _ = trf.transform_with_origin(ir_arr, origin_src)
                    ir_xyz_abs = (ir_tgt_local + np.asarray(origin_tgt, dtype=np.float64)).tolist()
                    ir_xyz = [(float(x), float(y), float(z)) for (x, y, z) in ir_xyz_abs]

                    ir_uv = None
                    if mesh.uv_layers.active and ip.loop_indices:
                        ir_uv = []
                        for li in ip.loop_indices:
                            uv = mesh.uv_layers.active.data[li].uv
                            ir_uv.append((float(uv.x), float(uv.y)))
                        try:
                            if bool(obj.get("cgml3_flip_v", False)):
                                ir_uv = [(u, 1.0 - v) for (u, v) in ir_uv]
                        except Exception:
                            pass
                        ir_uv = _ensure_ring_closed(ir_uv)
                        try:
                            before_ir_uv = list(ir_uv)
                            ir_uv = _order_uvs_for_loop_vertices(mesh, ip.loop_indices, ir_xyz_local, ir_uv, world_matrix=obj_eval.matrix_world)
                        except Exception as e:
                            pass
                        # No in-place rotation here; TexCoordList rotation handled in appearance writer using uv_start.

                    if inner_override and inner_override.get("uvs"):
                        ir_uv = _ensure_ring_closed(list(inner_override["uvs"]))
                        try:
                            if bool(obj.get("cgml3_flip_v", False)):
                                ir_uv = [(u, 1.0 - v) for (u, v) in ir_uv]
                        except Exception:
                            pass

                    interior_rings_xyz.append(ir_xyz)
                    interior_uvs.append(ir_uv)

                exported_faces.update(inner_ids)

            # Polygon-ID bevorzugt aus Face oder Material übernehmen, sonst neue UUID
            try:
                gid_raw = poly.get("gml_id", None)
            except Exception:
                gid_raw = None

            poly_gid = ""
            if gid_raw:
                poly_gid = str(gid_raw).strip()

            if not poly_gid and mat:
                # Fallback: Material-Property aus Import
                try:
                    m_gid = mat.get("gml_polygon_id", None)
                except Exception:
                    m_gid = None
                if m_gid:
                    poly_gid = str(m_gid).strip()

            raw_poly_gid = poly_gid
            poly_gid = _claim_poly_id(raw_poly_gid)

            opening_info = _build_face_opening_info(raw_surface_type, face_override, mat, obj, poly_gid)
            interior_helper_target_poly_id = ""
            is_interior_helper_surface = False
            try:
                is_interior_helper_surface = bool(mat and mat.get("Interior", False))
            except Exception:
                is_interior_helper_surface = False
            if is_interior_helper_surface:
                interior_helper_target_poly_id = _first_nonempty_prop((face_override, mat, obj), ("ExteriorPolyId",))
                if interior_helper_target_poly_id:
                    interior_helper_target_poly_id = _claim_poly_id(interior_helper_target_poly_id)
                else:
                    is_interior_helper_surface = False
            is_openings_cutter_interior_surface = (
                is_interior_helper_surface
                and _is_openings_cutter_interior(face_override, mat, obj)
            )

            # Kanonische Nummerierung pro Polygon:
            # erster Ring -> _0_, zweiter -> _1_, usw.
            mat_ring_id = ""
            if is_openings_cutter_interior_surface and mat:
                try:
                    mat_ring_id = str(mat.get("gml_ring_id", "") or "").strip()
                except Exception:
                    mat_ring_id = ""
            if mat_ring_id:
                ring_id = mat_ring_id
            else:
                idx = ring_index_by_poly.get(poly_gid, 0)
                ring_id = f"{poly_gid}_{idx}_"
                ring_index_by_poly[poly_gid] = idx + 1
            if use_inner_outer_script:
                ring_id = _claim_ring_id(ring_id)

            # Interior-Ring-IDs: ebenfalls kanonisch pro Polygon durchzählen
            interior_ring_ids = []
            for _ in range(len(inner_ids)):
                idx_inner = ring_index_by_poly.get(poly_gid, 0)
                rid_inner = f"{poly_gid}_{idx_inner}_"
                ring_index_by_poly[poly_gid] = idx_inner + 1
                if use_inner_outer_script:
                    rid_inner = _claim_ring_id(rid_inner)
                interior_ring_ids.append(rid_inner)

            # Optional: bei Implicit-Prototypen die Appearance-ID auf die MultiSurface-ID legen,
            # damit Materialien/Targets stabil zum Prototyp gehören.
            appearance_id_override = None
            try:
                appearance_id_override = obj.get("_appearance_id_override", None)
            except Exception:
                appearance_id_override = None

            # Appearance-ID/Theme für dieses Polygon bestimmen (Roundtrip: bevorzugt importierte Werte)
            try:
                app_id_raw = poly.get("app_id", None)
            except Exception:
                app_id_raw = None

            if not app_id_raw and mat:
                try:
                    app_id_raw = mat.get("app_id", None)
                except Exception:
                    app_id_raw = None

            # Interior helper faces are merged into their exterior polygon later.
            # Keep their appearance entries in the exterior polygon's group so
            # one ParameterizedTexture/TexCoordList contains exterior + interior
            # rings together.
            if is_interior_helper_surface and interior_helper_target_poly_id:
                exterior_app_id = _app_id_by_poly.get(str(interior_helper_target_poly_id).strip())
                if exterior_app_id:
                    app_id_raw = exterior_app_id

            if not app_id_raw:
                # Reuse app_id from another face sharing the same gml_polygon_id
                # before falling back to object-level IDs.
                app_id_raw = _app_id_by_poly.get(poly_gid)

            if not app_id_raw:
                try:
                    app_id_raw = appearance_id_override or obj.get("app_id", None) or obj.get("gml_id", None)
                except Exception:
                    app_id_raw = appearance_id_override

            if not app_id_raw:
                app_id_raw = f"ID_{uuid4().hex}"

            app_id = str(app_id_raw).strip() or f"ID_{uuid4().hex}"

            # Remember this app_id for the polygon so interior rings reuse it
            if poly_gid and poly_gid not in _app_id_by_poly:
                _app_id_by_poly[poly_gid] = app_id

            try:
                theme = (obj.get("app_theme", "") if obj.get("app_theme", None) is not None else "")
            except Exception:
                theme = ""

            app_grp = _get_app_group(app_id, theme)

            # 1) Textur-Belegung (Parameterized / Georeferenced) bestimmen
            skip_texture_export = bool(face_override and face_override.get("skip_texture_export"))
            img_path_ext = None if skip_texture_export else _first_image_path_from_export_material(obj, poly.material_index, mat)
            has_texture = False
            ext_uv_sign = 0

            if export_ptx and img_path_ext and uvs_ext:
                # Orientierung des 3D-Rings bestimmen (für spätere UV-Korrektur)
                ori_ext = _ring_area_sign_xyz(ring_xyz) if ring_xyz else 0

                ext_uv_sign = _uv_ring_sign(uvs_ext)

                entry = {
                    "poly_id": str(interior_helper_target_poly_id).strip() if is_openings_cutter_interior_surface else poly_gid,
                    "ring_id": ring_id,
                    "uvs": uvs_ext,
                }
                if is_openings_cutter_interior_surface:
                    entry["is_interior"] = True
                    entry["preserve_uv_order"] = True
                # Do not attach `ori` to the exterior ring entry.
                # Interior faces can have inverted normals; using 3D ring orientation as a
                # heuristic can cause TexCoordList order to be reversed compared to import.gml.
                # Roundtrip: preserve original TexCoordList start point (first UV pair) from import.
                try:
                    rid_this = _norm_id(ring_id)
                    start_uv = None
                    # Prefer ring-specific mapping stored on object or mesh (bit-identical roundtrip)
                    try:
                        d = None
                        if obj is not None:
                            d = obj.get("cgml3_uv_start_by_ring", None)
                        if d is None and obj is not None and getattr(obj, "data", None) is not None:
                            # Prefer mesh custom property (import writes to mesh)
                            d = obj.data.get("cgml3_uv_start_by_ring", None)
                        if d is None and obj is not None and getattr(obj, "data", None) is not None:
                            # Fallback: some pipelines may attach it to the mesh's IDPropertyGroup explicitly
                            try:
                                d = getattr(obj.data, "data", None)
                            except Exception:
                                d = None
                            if d is not None:
                                try:
                                    d = d.get("cgml3_uv_start_by_ring", None)
                                except Exception:
                                    d = None
                        # v2 importer stores this mapping on the mesh datablock (obj.data).
                        if d is not None and rid_this:
                            # Blender stores custom dict-like props as IDPropertyGroup.
                            # Be tolerant w.r.t. key format: some import paths may store raw ids.
                            try:
                                start_uv = _as_uv_tuple2(d.get(rid_this, None))
                            except Exception:
                                start_uv = None
                            if start_uv is None:
                                try:
                                    start_uv = _as_uv_tuple2(d.get(str(ring_id), None))
                                except Exception:
                                    start_uv = None
                            if start_uv is None:
                                try:
                                    start_uv = _as_uv_tuple2(d.get(str(rid_this), None))
                                except Exception:
                                    start_uv = None
                            # Robust fallback: normalize all keys once and look up again.
                            if start_uv is None:
                                try:
                                    norm_map = {}
                                    for k in d.keys():
                                        nk = _norm_id(k)
                                        if not nk or nk in norm_map:
                                            continue
                                        norm_map[nk] = _as_uv_tuple2(d.get(k, None))
                                    start_uv = norm_map.get(rid_this, None)
                                except Exception:
                                    start_uv = None
                    except Exception:
                        start_uv = None
                    # Opt-in debug for one ring
                    try:
                        watch = os.environ.get("CGML3_DEBUG_TEXCOORD_RING", "").strip()
                        if watch and (watch in str(ring_id) or watch in str(rid_this)):
                            raw = None
                            try:
                                raw = d.get(rid_this, None) if d is not None else None
                            except Exception:
                                raw = None
                            keys_sample = None
                            try:
                                if d is not None:
                                    keys_sample = list(d.keys())[:20]
                                    keys_norm_sample = []
                                    for k in keys_sample:
                                        try:
                                            keys_norm_sample.append(_norm_id(k))
                                        except Exception:
                                            keys_norm_sample.append(None)
                            except Exception:
                                keys_sample = None
                                keys_norm_sample = None
                            print("[CityGML2][UVSTART-DBG] ring_id=", ring_id,
                                  " rid_this=", rid_this,
                                  " map_type=", type(d).__name__ if d is not None else None,
                                  " keys_head=", keys_sample,
                                  " keys_norm_head=", keys_norm_sample,
                                  " raw_type=", type(raw).__name__ if raw is not None else None,
                                  " raw=", raw,
                                  " start_uv=", start_uv)
                    except Exception:
                        pass
                    # Legacy fallback: per-material, only if it matches this ring id
                    if start_uv is None:
                        start_uv = _as_uv_tuple2(mat.get("cgml3_uv_start", None) if mat else None)
                        rid_mat = _norm_id(mat.get("gml_ring_id", "")) if mat else ""
                        if not (start_uv is not None and rid_mat and rid_this and rid_mat == rid_this):
                            start_uv = None
                    if start_uv is not None:
                        entry["uv_start"] = start_uv
                except Exception:
                    pass

                ptx_by_image.setdefault(img_path_ext, []).append(entry)
                rel_uri = _remap_image_uri(img_path_ext)
                if rel_uri:
                    app_grp["ptx_by_image"].setdefault(rel_uri, []).append(entry)
                has_texture = True

                # --- UVs pro Material, Polygon und Ring in separatem Dict speichern ---
                if 'uvs_by_material' not in locals():
                    uvs_by_material = {}
                mat_key = mat.name if mat else None
                if mat_key:
                    if mat_key not in uvs_by_material:
                        uvs_by_material[mat_key] = {}
                    if poly_gid not in uvs_by_material[mat_key]:
                        uvs_by_material[mat_key][poly_gid] = {}
                    uvs_by_material[mat_key][poly_gid][ring_id] = uvs_ext
            elif export_gtx and img_path_ext and (not mesh.uv_layers.active) and ring_xyz and allow_gtx_fallback:
                # Fallback: sehr einfache GeoreferencedTexture, nur wenn keine UVs vorhanden
                x0, y0, _ = ring_xyz[0]
                rel_uri = _remap_image_uri(img_path_ext)
                if rel_uri:
                    app_grp["gtx_by_image"].setdefault(rel_uri, []).append(
                        {"poly_id": poly_gid, "refpt": (x0, y0), "M": (1.0, 0.0, 0.0, 1.0)}
                    )
                    has_texture = True

            if export_ptx:
                # Innenringe
                for (iid, rid, uv_i, ir_xyz) in zip(inner_ids, interior_ring_ids, interior_uvs, interior_rings_xyz):
                    inner_override = face_overrides.get(iid)
                    mat_i = inner_override.get("mat") if inner_override else None
                    skip_inner_texture_export = bool(inner_override and inner_override.get("skip_texture_export"))
                    img_path_int = None if skip_inner_texture_export else _first_image_path_from_export_material(
                        obj, mesh.polygons[iid].material_index, mat_i
                    )
                    corrected_uv_i, uv_ring_corrected = _correct_interior_uv_ring(
                        uv_i,
                        ext_uv_sign,
                    )
                    if img_path_int and corrected_uv_i:
                        ori_int = _ring_area_sign_xyz(ir_xyz)
                        entry_i = {
                            "poly_id": poly_gid,
                            "ring_id": rid,
                            "uvs": corrected_uv_i,
                            "ori": ori_int,
                            "is_interior": True,
                        }
                        if mat_i is None:
                            mat_i = obj.data.materials[mesh.polygons[iid].material_index] if obj and obj.data else None
                        try:
                            rid_this_i = _norm_id(rid)
                            start_uv_i = None
                            if uv_ring_corrected and corrected_uv_i:
                                start_uv_i = corrected_uv_i[0]
                            try:
                                if start_uv_i is None:
                                    d = None
                                    if obj is not None:
                                        d = obj.get("cgml3_uv_start_by_ring", None)
                                    if d is None and obj is not None and getattr(obj, "data", None) is not None:
                                        d = obj.data.get("cgml3_uv_start_by_ring", None)
                                    if d is not None and rid_this_i:
                                        try:
                                            start_uv_i = _as_uv_tuple2(d.get(rid_this_i, None))
                                        except Exception:
                                            start_uv_i = None
                            except Exception:
                                start_uv_i = None
                            if start_uv_i is None:
                                if mat_i is None:
                                    mat_i = obj.data.materials[mesh.polygons[iid].material_index] if obj and obj.data else None
                                start_uv_i = _as_uv_tuple2(mat_i.get("cgml3_uv_start", None) if mat_i else None)
                                rid_mat_i = _norm_id(mat_i.get("gml_ring_id", "")) if mat_i else ""
                                if not (start_uv_i is not None and rid_mat_i and rid_this_i and rid_mat_i == rid_this_i):
                                    start_uv_i = None
                            if start_uv_i is not None:
                                entry_i["uv_start"] = start_uv_i
                        except Exception:
                            pass
                        ptx_by_image.setdefault(img_path_int, []).append(entry_i)
                        has_texture = True

                        rel_uri = _remap_image_uri(img_path_int)
                        if rel_uri:
                            app_grp["ptx_by_image"].setdefault(rel_uri, []).append(entry_i)

            # 2) X3DMaterial
            # Gold-Referenz (export_korrekt.gml): X3DMaterial-Targets existieren auch für Polygone,
            # die zusätzlich über Texturen gestylt sind. Daher nicht an "not has_texture" koppeln.
            if export_x3d and not is_openings_cutter_interior_surface:
                rgba = extract_base_color_rgba(mat)

                params = extract_x3d_params_from_material(mat)
                """ x3d_by_key.setdefault((st, rgba, tuple(sorted(params.items()))), []).append(poly_gid) """

                rgba = extract_base_color_rgba(mat)
                rgba_t = tuple(float(x) for x in rgba)
                params = extract_x3d_params_from_material(mat) or {}
                params_t = tuple(sorted(params.items()))

                key = (rgba_t, params_t)
                bundle = app_grp["x3d_by_key"].get(key)
                if bundle is None:
                    bundle = {"rgba": rgba_t, "params": params, "poly_ids": []}
                    app_grp["x3d_by_key"][key] = bundle
                bundle["poly_ids"].append(poly_gid)

            # Surface- und MultiSurface-IDs aus Material übernehmen
            surf_id = ""
            ms_id = ""
            cs_id = ""  # CompositeSurface-ID
            is_ms_member = False
            filling_parent_sid = ""
            try:
                if mat:
                    sid = mat.get("con_surface_id", None)
                    if sid:
                        surf_id = str(sid).strip()
                    mid = mat.get("gml_multisurface_id", None)
                    if mid:
                        ms_id = str(mid).strip()
                    cid = mat.get("gml_compositesurface_id", None)
                    if cid:
                        cs_id = str(cid).strip()
                    is_ms_member = bool(mat.get("is_multisurface_member", False))
                    _fp = mat.get("filling_parent_surface_id", None)
                    if not _fp:
                        _fp = mat.get("opening_surface_id", None)
                    if _fp:
                        filling_parent_sid = str(_fp).strip()
            except Exception:
                pass

            if not surf_id:
                surf_id = f"ID_{uuid4().hex}"
            if not ms_id:
                ms_id = f"ID_{uuid4().hex}"

            if use_inner_outer_script:
                geom_buf[st].append(
                    {
                        "poly_gid": poly_gid,
                        "ext_xyz": ring_xyz,
                        "int_xyz": interior_rings_xyz,
                        "ext_id": ring_id,
                        "int_ids": interior_ring_ids,
                        "mat": mat,
                        "surf_id": surf_id,
                        "ms_id": ms_id,
                        "cs_id": cs_id,  # CompositeSurface-ID
                        "opening_info": opening_info,
                        "filling_parent_sid": filling_parent_sid,
                        "subfeature_type": subfeature_type,
                        "subfeature_id": subfeature_id,
                        "subfeature_parent_id": subfeature_parent_id,
                        "subfeature_relation": subfeature_relation,
                        "subfeature_name": subfeature_name,
                    }
                )

            bbox_coords_abs.extend(ring_xyz)
            for ir in interior_rings_xyz:
                bbox_coords_abs.extend(ir)

            if not use_inner_outer_script:
                # Polygon-Gruppen nach gml_polygon_id aufbauen
                grp = poly_groups.get(poly_gid)
                if grp is None:
                    grp = {
                        "poly_gid": poly_gid,
                        "surf_type": st,
                        "mat": None,
                        "ext_xyz": None,
                        "ext_id": "",
                        "int_xyz": [],
                        "int_ids": [],
                        "surf_id": "",
                        "ms_id": "",
                        "cs_id": "",
                        "is_multisurface_member": False,
                        "is_compositesurface_member": False,
                        "opening_info": None,
                        "filling_parent_sid": "",
                        "subfeature_type": "",
                        "subfeature_id": "",
                        "subfeature_parent_id": "",
                        "subfeature_relation": "",
                        "subfeature_name": "",
                    }
                    poly_groups[poly_gid] = grp

                # Surface-/MultiSurface-/CompositeSurface-IDs möglichst aus Material nur einmal übernehmen
                if not grp["surf_id"] and surf_id:
                    grp["surf_id"] = surf_id
                if not grp["ms_id"] and ms_id:
                    grp["ms_id"] = ms_id
                if not grp["cs_id"] and cs_id:
                    grp["cs_id"] = cs_id
                if not grp["filling_parent_sid"] and filling_parent_sid:
                    grp["filling_parent_sid"] = filling_parent_sid
                if not grp["is_multisurface_member"] and is_ms_member:
                    grp["is_multisurface_member"] = is_ms_member
                if mat and not grp.get("is_compositesurface_member", False):
                    try:
                        grp["is_compositesurface_member"] = bool(mat.get("is_compositesurface_member", False))
                    except Exception:
                        pass

                # Material (für generische Attribute) – idealerweise das Exterior-Material
                if grp["mat"] is None:
                    grp["mat"] = mat
                if grp["opening_info"] is None and opening_info:
                    grp["opening_info"] = opening_info
                if not grp["subfeature_type"] and subfeature_type:
                    grp["subfeature_type"] = subfeature_type
                if not grp["subfeature_id"] and subfeature_id:
                    grp["subfeature_id"] = subfeature_id
                if not grp["subfeature_parent_id"] and subfeature_parent_id:
                    grp["subfeature_parent_id"] = subfeature_parent_id
                if not grp["subfeature_relation"] and subfeature_relation:
                    grp["subfeature_relation"] = subfeature_relation
                if not grp["subfeature_name"] and subfeature_name:
                    grp["subfeature_name"] = subfeature_name

                # Exterior-Ring: Suffix "_0_" oder erster Ring, falls keine Suffix-Info
                if ring_id.endswith("_0_") or not grp["ext_xyz"]:
                    grp["ext_xyz"] = ring_xyz
                    grp["ext_id"] = ring_id
                else:
                    grp["int_xyz"].append(ring_xyz)
                    grp["int_ids"].append(ring_id)

        # -------------------------------------------------------------------
        # Interior-Ring-Polygone mit ExteriorPolyId in ihr Exterior-Polygon mergen.
        # Der Openings-Cutter / Join-Object-Parts legt für Interior-Ringe eigene
        # gml_polygon_id-Einträge an (z.B. "{OriginalPolyId}_{N}").
        # Diese werden hier als gml:interior in das zugehörige Exterior-Polygon
        # eingefügt und danach aus poly_groups entfernt.
        # -------------------------------------------------------------------
        if not use_inner_outer_script and poly_groups:
            _interior_to_remove = []
            _merged_interior_poly_targets: Dict[str, str] = {}
            _merged_interior_preserve_uv: Dict[str, bool] = {}
            for _pg_id, _pg in poly_groups.items():
                _mat_pg = _pg.get("mat")
                if not _mat_pg:
                    continue
                try:
                    _is_int = bool(_mat_pg.get("Interior", False))
                except Exception:
                    _is_int = False
                if not _is_int:
                    continue
                try:
                    _ext_pid = str(_mat_pg.get("ExteriorPolyId", "")).strip()
                except Exception:
                    _ext_pid = ""
                if not _ext_pid:
                    continue
                _ext_pid = _claim_poly_id(_ext_pid)
                _ext_grp = poly_groups.get(_ext_pid)
                if _ext_grp and _pg.get("ext_xyz"):
                    _ext_grp["int_xyz"].append(_pg["ext_xyz"])
                    _ext_grp["int_ids"].append(_pg.get("ext_id", ""))
                    _merged_interior_poly_targets[_pg_id] = _ext_pid
                    _merged_interior_preserve_uv[_pg_id] = _is_openings_cutter_interior(None, _mat_pg, obj)
                    _interior_to_remove.append(_pg_id)
            for _pg_id in _interior_to_remove:
                del poly_groups[_pg_id]

            # Appearance-Targets (ptx_by_image, gtx_by_image, x3d_by_key) umschreiben:
            # Interior-Polygon-IDs → Exterior-Polygon-ID
            if _merged_interior_poly_targets:
                for _entries in ptx_by_image.values():
                    for _entry in _entries:
                        try:
                            _pid = str(_entry.get("poly_id") or "").strip()
                        except Exception:
                            _pid = ""
                        if _pid in _merged_interior_poly_targets:
                            _entry["poly_id"] = _merged_interior_poly_targets[_pid]
                            _entry["is_interior"] = True
                            if _merged_interior_preserve_uv.get(_pid, False):
                                _entry["preserve_uv_order"] = True
                            else:
                                _entry.pop("preserve_uv_order", None)
                for _entries in gtx_by_image.values():
                    for _entry in _entries:
                        try:
                            _pid = str(_entry.get("poly_id") or "").strip()
                        except Exception:
                            _pid = ""
                        if _pid in _merged_interior_poly_targets:
                            _entry["poly_id"] = _merged_interior_poly_targets[_pid]
                for _key in list(x3d_by_key.keys()):
                    _kept = []
                    for _pid in x3d_by_key.get(_key, []):
                        if str(_pid or "").strip() in _merged_interior_poly_targets:
                            continue
                        _kept.append(_pid)
                    if _kept:
                        x3d_by_key[_key] = _kept
                    else:
                        x3d_by_key.pop(_key, None)

            # Ring-IDs kanonisch durchnummerieren (ext=_0_, int=_1_, _2_, ...)
            # und Appearance ring_id-Referenzen anpassen.
            _ring_maps_by_poly: Dict[str, Dict[str, str]] = {}
            for _poly_id, _grp in poly_groups.items():
                _poly_txt = str(_poly_id or "").strip()
                if not _poly_txt:
                    continue
                _ext_new = _claim_ring_id(f"{_poly_txt}_0_")
                _ring_map: Dict[str, str] = {}
                _ext_old = str(_grp.get("ext_id") or "").strip()
                if _ext_old:
                    _ring_map[_ext_old] = _ext_new
                _ring_map[_ext_new] = _ext_new
                _grp["ext_id"] = _ext_new

                _int_new_ids: list = []
                for _idx, _old in enumerate(list(_grp.get("int_ids") or []), start=1):
                    _int_new = _claim_ring_id(f"{_poly_txt}_{_idx}_")
                    _old_txt = str(_old or "").strip()
                    if _old_txt:
                        _ring_map[_old_txt] = _int_new
                    _ring_map[_int_new] = _int_new
                    _int_new_ids.append(_int_new)
                _grp["int_ids"] = _int_new_ids
                _ring_maps_by_poly[_poly_txt] = _ring_map

            if _ring_maps_by_poly:
                for _entries in ptx_by_image.values():
                    for _entry in _entries:
                        try:
                            _pid = str(_entry.get("poly_id") or "").strip()
                        except Exception:
                            _pid = ""
                        _rmap = _ring_maps_by_poly.get(_pid)
                        if not _rmap:
                            continue
                        try:
                            _rid = str(_entry.get("ring_id") or "").strip()
                        except Exception:
                            _rid = ""
                        _mapped = _rmap.get(_rid)
                        if _mapped:
                            _entry["ring_id"] = _mapped

        # ID-basierter Weg: pro Polygon-ID genau ein Polygon mit exterior + interior schreiben
        # ODER: bei echten MultiSurfaces mehrere Polygone unter einer MultiSurface gruppieren
        if not use_inner_outer_script:
            # Gruppiere echte MultiSurface-Members nach (surf_id, ms_id)
            multisurface_groups = defaultdict(list)
            
            for poly_gid, grp in poly_groups.items():
                if not grp["ext_xyz"]:
                    # Ohne Exterior-Ring ist das Polygon nicht gültig
                    continue

                surf_type = grp["surf_type"]
                mat = grp["mat"]
                surf_id = grp["surf_id"] or f"ID_{uuid4().hex}"
                ms_id = grp["ms_id"] or f"ID_{uuid4().hex}"
                cs_id = grp.get("cs_id", "")
                ext_id = grp["ext_id"] or f"{poly_gid}_0_"
                is_ms_member = grp.get("is_multisurface_member", False)
                is_cs_member = grp.get("is_compositesurface_member", False)

                poly_data = {
                    "poly_gid": poly_gid,
                    "ext_xyz": grp["ext_xyz"],
                    "int_xyz": grp["int_xyz"],
                    "ext_id": ext_id,
                    "int_ids": grp["int_ids"],
                    "mat": mat,
                    "surf_id": surf_id,
                    "ms_id": ms_id,
                    "cs_id": cs_id,
                    "is_multisurface_member": is_ms_member,
                    "is_compositesurface_member": is_cs_member,
                    "opening_info": grp.get("opening_info"),
                    "filling_parent_sid": grp.get("filling_parent_sid", ""),
                    "subfeature_type": grp.get("subfeature_type", ""),
                    "subfeature_id": grp.get("subfeature_id", ""),
                    "subfeature_parent_id": grp.get("subfeature_parent_id", ""),
                    "subfeature_relation": grp.get("subfeature_relation", ""),
                    "subfeature_name": grp.get("subfeature_name", ""),
                }

                if split_wall_roof_surfaces and is_ms_member and surf_type in ("WallSurface", "RoofSurface"):
                    # Für FME: Wall/Roof nicht als EIN Surface mit vielen Polygonen (MultiPolygon) exportieren.
                    # Stattdessen: jedes Polygon als eigene Surface + eigene MultiSurface (mit genau 1 Polygon).
                    # Wichtig: surf_id/ms_id pro Polygon eindeutig machen, sonst doppelte gml:ids.
                    poly_data["surf_id"] = f"{surf_id}_{poly_gid}"
                    poly_data["ms_id"] = f"{ms_id}_{poly_gid}"
                    poly_data["is_multisurface_member"] = False
                    geom_buf[surf_type].append(poly_data)

                elif is_ms_member:
                    # Echte MultiSurface: nach (surf_id, ms_id) gruppieren
                    opening_key = ""
                    if poly_data.get("opening_info"):
                        opening_key = poly_data["opening_info"].get("opening_id") or poly_data["opening_info"].get("opening_type") or "__opening__"
                    key = (
                        surf_type,
                        surf_id,
                        ms_id,
                        opening_key,
                        poly_data.get("subfeature_type", ""),
                        poly_data.get("subfeature_id", ""),
                    )
                    multisurface_groups[key].append(poly_data)

                else:
                    # Einzelnes Polygon: direkt in geom_buf einfügen
                    geom_buf[surf_type].append(poly_data)
            
            # Echte MultiSurfaces als Gruppen in geom_buf einfügen
            for (surf_type, surf_id, ms_id, _opening_key, subfeature_type, subfeature_id), polys in multisurface_groups.items():
                if len(polys) > 0:
                    # Verwende erstes Material als Repräsentant für die Surface
                    geom_buf[surf_type].append({
                        "is_multisurface": True,
                        "polygons": polys,
                        "surf_id": surf_id,
                        "ms_id": ms_id,
                        "mat": polys[0]["mat"],
                        "opening_info": polys[0].get("opening_info"),
                        "filling_parent_sid": polys[0].get("filling_parent_sid", ""),
                        "subfeature_type": subfeature_type,
                        "subfeature_id": subfeature_id,
                        "subfeature_parent_id": polys[0].get("subfeature_parent_id", ""),
                        "subfeature_relation": polys[0].get("subfeature_relation", ""),
                        "subfeature_name": polys[0].get("subfeature_name", ""),
                    })

        try:
            # Effektives Ziel-CRS aus dem Transformer für das Envelope ableiten
            try:
                auth = trf.tgt.to_authority()  # z. B. ('EPSG','4979'); bei Compound ggf. None
                if auth and auth[0] == "EPSG" and auth[1]:
                    srs_for_env = f"EPSG:{auth[1]}"
                else:
                    # Fallback: String-Repräsentation (kann URN für Compound sein)
                    srs_for_env = trf.tgt.to_string()
            except Exception:
                # Rückfall auf bisherige Logik
                srs_for_env = f"EPSG:{_extract_epsg(srs_name) or _extract_epsg(_read_world_crs()) or '25832'}"

            _envelope_elem(feature, bbox_coords_abs, srs_for_env)
        except Exception:
            pass

        # CityGML 2.0: creationDate (und andere Life-cycle attributes) müssen
        # NACH boundedBy, aber VOR Generic Attributes kommen!
        # Schema-Reihenfolge: boundedBy → location? → creationDate? → terminationDate? → ...
        
        # CityGML 2.0: creationDate gehört zum core-Namespace (elementFormDefault="qualified")
        creation_date_val = obj.get("creationDate")
        if creation_date_val:
            dt_str = _to_xs_date(creation_date_val)
            if dt_str:
                SubElement(feature, Q("core", "creationDate")).text = dt_str
        
        # Weitere Life-cycle attributes (falls vorhanden)
        for key, tag in (
            ("terminationDate", "terminationDate"),
            ("validFrom", "validFrom"),
            ("validTo", "validTo"),
        ):
            val = obj.get(key)
            if val:
                dt_str = _to_xs_date(val)
                if dt_str:
                    SubElement(feature, Q("core", tag)).text = dt_str

        # CityGML 3.0 life-cycle attributes (creationDate, terminationDate, validFrom, validTo)
        # Hinweis: Diese Funktion schreibt core:* Präfixe (für CityGML 3.0)
        # Für CityGML 2.0 haben wir bereits creationDate ohne Präfix oben geschrieben
        # _write_lifespan_core(feature, obj)

        # Gold-Referenz (export_korrekt.gml): Objekt-Generics stehen direkt im Feature (ohne core:genericAttribute Wrapper).
        # Für bldg:Building braucht es diese Werte (WIJK, PANDSOORT, ...).
        if ns_key == "bldg" and local_name in ("Building", "BuildingPart"):
            _write_generic_attributes(feature, _get_generic_attributes_for_export(obj, target="OBJECT"))

        # Appearance wird global am Ende des CityModels geschrieben (siehe Ende der Funktion).

        def _write_generics(feature_el, obj_):
            """
            CityGML 2.0: generic attributes are written as direct gen:*Attribute children
            of the feature (NO <core:genericAttribute> wrapper).
            """
            if obj_ is None:
                return
            _write_generic_attributes(feature_el, _get_generic_attributes_for_export(obj_, target="OBJECT"))


        
        def _write_surface_generics_on_surface(surf_el, mat):
            """
            Exportiert Material-Custom-Properties als generische Attribute.

            WICHTIG (CityGML 2.0 XSD):
            - bldg:*Surface (z.B. bldg:WallSurface) erlaubt KEINE direkten gen:*Attribute-Kindelemente.
            - Zulässig ist nur der ADE-Slot (_GenericApplicationPropertyOfWallSurface etc.).
            - Daher werden gen:*Attribute in einen Container unterhalb von bldg:adeOfAbstractBoundarySurface geschrieben.
              (Dieser Container ist ein beliebiges Element; wir nutzen <gen:genericAttributeSet> als "Wrapper".)
            """
            if mat is None:
                return

            attr_items = []
            for k, v in getattr(mat, "items", lambda: [])():
                # interne / spezifische Keys nicht als generische Attribute exportieren
                if k in INTERNAL_ATTR_KEYS or k in SPECIFIC_ATTR_KEYS:
                    continue
                if k in MATERIAL_INTERNAL_ATTR_KEYS:
                    continue
                if str(k).startswith("_"):
                    continue
                attr_items.append((k, v))

            if not attr_items:
                return

            # CityGML 2.0: the schema only provides *abstract* ADE hook elements
            # (_GenericApplicationPropertyOfBoundarySurface / _GenericApplicationPropertyOfWallSurface).
            # Valid instances must use a *concrete* element from an application schema (ADE) that
            # substitutes these abstract elements. Since this exporter has no ADE schema, we must
            # not write any ADE hook element here.
            return

            for (k, v) in attr_items:
                try:
                    name = str(k).strip()
                    if not name:
                        continue

                    if isinstance(v, str):
                        a = SubElement(holder, Q("gen", "stringAttribute"))
                        a.set("name", name)
                        SubElement(a, Q("gen", "value")).text = v
                    elif isinstance(v, bool):
                        a = SubElement(holder, Q("gen", "intAttribute"))
                        a.set("name", name)
                        SubElement(a, Q("gen", "value")).text = "1" if v else "0"
                    elif isinstance(v, int):
                        a = SubElement(holder, Q("gen", "intAttribute"))
                        a.set("name", name)
                        SubElement(a, Q("gen", "value")).text = str(v)
                    elif isinstance(v, (float,)):
                        a = SubElement(holder, Q("gen", "doubleAttribute"))
                        a.set("name", name)
                        SubElement(a, Q("gen", "value")).text = str(v)
                    else:
                        a = SubElement(holder, Q("gen", "stringAttribute"))
                        a.set("name", name)
                        SubElement(a, Q("gen", "value")).text = str(v)
                except Exception:
                    continue

        def _normalize_lod_value(value):
            lod = str(value if value is not None else "").strip()
            return lod if lod in ("0", "1", "2", "3", "4") else ""

        def _material_lod_value(mat):
            if mat is None:
                return ""
            try:
                return _normalize_lod_value(mat.get("lod", None))
            except Exception:
                return ""

        def _entry_lod_value(entry, fallback=""):
            if not entry:
                return _normalize_lod_value(fallback)
            try:
                lod = _normalize_lod_value(entry.get("lod", None))
            except Exception:
                lod = ""
            if lod:
                return lod
            try:
                lod = _material_lod_value(entry.get("mat"))
            except Exception:
                lod = ""
            return lod or _normalize_lod_value(fallback)

        def _group_lod_value(gp, fallback="2"):
            lod = _entry_lod_value(gp, "")
            if lod:
                return lod

            lod_counts = {}
            try:
                polygons = list(gp.get("polygons", []) or [])
            except Exception:
                polygons = []
            for poly in polygons:
                poly_lod = _entry_lod_value(poly, "")
                if not poly_lod:
                    continue
                lod_counts[poly_lod] = lod_counts.get(poly_lod, 0) + 1

            if lod_counts:
                return max(lod_counts.items(), key=lambda item: (item[1], int(item[0])))[0]

            return _normalize_lod_value(fallback) or "2"

        def _entries_lod_value(entries, fallback="2"):
            lod_counts = {}
            for entry in entries or []:
                entry_lod = _group_lod_value(entry, "")
                if not entry_lod:
                    continue
                lod_counts[entry_lod] = lod_counts.get(entry_lod, 0) + 1

            if lod_counts:
                return max(lod_counts.items(), key=lambda item: (item[1], int(item[0])))[0]

            return _normalize_lod_value(fallback) or "2"

        def _opening_lod_value(entries, fallback="2"):
            # CityGML 2 AbstractOpeningType declares only lod3/lod4 geometry.
            lod = _entries_lod_value(entries, fallback)
            return "4" if lod == "4" else "3"

        def _write_inline_surface_opening(surf_el, opening_entries, lod="2"):
            if not opening_entries:
                return

            # Don't nest an opening inside a surface that is itself an opening type
            _parent_tag = getattr(surf_el, "tag", "") or ""
            _parent_local = _parent_tag.rsplit("}", 1)[-1] if "}" in _parent_tag else _parent_tag
            if _parent_local in ("WindowSurface", "DoorSurface", "Window", "Door"):
                return

            opening_info = (opening_entries[0] or {}).get("opening_info")
            if not opening_info:
                return

            opening_type = opening_info.get("opening_type")
            if opening_type not in ("Window", "Door"):
                return

            # Determine the correct module namespace from parent element (bldg/brid/tun)
            _opening_ns = ns_key  # default: same as feature namespace
            if _parent_tag.startswith("{"):
                _parent_uri = _parent_tag[1:_parent_tag.index("}")]
                for _pfx, _uri in NS.items():
                    if _uri == _parent_uri and _pfx in ("bldg", "brid", "tun"):
                        _opening_ns = _pfx
                        break

            opening_id = make_gml_id(
                str(opening_info.get("opening_id") or f"{opening_type}_{uuid4().hex}"),
                used_feat_ids,
            )
            opening_name = str(opening_info.get("opening_name") or "").strip()
            opening_lod = _opening_lod_value(opening_entries, lod)

            opening_container = SubElement(surf_el, Q(_opening_ns, "opening"))
            opening_el = SubElement(opening_container, Q(_opening_ns, opening_type), {GML_ID: opening_id})

            if opening_name and opening_name != opening_id:
                SubElement(opening_el, Q("gml", "name")).text = opening_name

            lod_geom = SubElement(opening_el, Q(_opening_ns, f"lod{opening_lod}MultiSurface"))
            multi_surf = SubElement(lod_geom, Q("gml", "MultiSurface"))
            opening_ms_id = ""
            try:
                opening_ms_id = str((opening_entries[0] or {}).get("ms_id") or "").strip()
            except Exception:
                opening_ms_id = ""
            multi_surf.set(
                GML_ID,
                make_gml_id(opening_ms_id, used_feat_ids)
                if opening_ms_id
                else make_gml_id(f"ID_{uuid4().hex}", used_feat_ids),
            )

            for poly_data in opening_entries:
                interior_rings = poly_data.get("int_xyz") or None
                poly_gid = poly_data.get("poly_gid") or _get_poly_gid(poly_data)
                exterior_ring_id = str(poly_data.get("ext_id") or "").strip() or f"ID_{uuid4().hex}"

                interior_ring_ids = None
                if interior_rings:
                    stored_ring_ids = list(poly_data.get("int_ids") or [])
                    interior_ring_ids = []
                    for idx, _ring in enumerate(interior_rings):
                        rid = ""
                        if idx < len(stored_ring_ids):
                            rid = str(stored_ring_ids[idx] or "").strip()
                        interior_ring_ids.append(rid or f"ID_{uuid4().hex}")
                write_polygon_with_ring_ids(
                    parent_ms=multi_surf,
                    poly_gid=poly_gid,
                    exterior_ring=poly_data["ext_xyz"],
                    interior_rings=interior_rings,
                    srs_name=srs_name,
                    exterior_ring_id=exterior_ring_id,
                    interior_ring_ids=interior_ring_ids,
                )

        def _surface_lod_with_openings(gp, base_lod, openings_by_parent_map):
            _ = openings_by_parent_map
            return _group_lod_value(gp, base_lod)

        # Feature-Generics (Objekt-Custom-Properties) - VOR der Geometrie
        # (Wrapper-Variante bleibt für andere Feature-Typen erhalten)
        _write_generics(feature, obj)

        def _determine_lod(obj_) -> str:
            """
            Bestimmt den LOD-Level für die Geometrie aus Object-Custom-Properties.
            Priorisiert:
            1. Explizites "lod" Property (z.B. "1", "2", "3", "4")
            2. Explizites "cgml3_lod" Property
            3. Fallback: "2" (Standard LOD2)
            """
            # Priorisiere "lod" Property
            lod_val = obj_.get("lod")
            if lod_val is not None:
                lod_str = str(lod_val).strip()
                if lod_str in ("0", "1", "2", "3", "4"):
                    return lod_str
            
            # Fallback: cgml3_lod
            lod_val = obj_.get("cgml3_lod")
            if lod_val is not None:
                lod_str = str(lod_val).strip()
                if lod_str in ("0", "1", "2", "3", "4"):
                    return lod_str
            
            # Default: LOD2
            return "2"

        lod_level = _determine_lod(obj)

        def _begin_bs(feature_el, surface_local, surf_id, ms_id, mat, lod="2"):
            # LOD aus Material-Properties verwenden, falls vorhanden
            # (wichtig für korrekte LOD bei IntBuildingInstallation: nur lod4Geometry erlaubt!)
            if mat and hasattr(mat, "get"):
                mat_lod = mat.get("lod")
                if mat_lod is not None:
                    lod_str = str(mat_lod).strip()
                    if lod_str in ("0", "1", "2", "3", "4"):
                        lod = lod_str

            _feat_local = feature_el.tag.rsplit("}", 1)[-1] if "}" in feature_el.tag else feature_el.tag
            _INSTALLATION_SELF_NS = {
                "BuildingInstallation": "bldg",
                "IntBuildingInstallation": "bldg",
                "BridgeInstallation": "brid",
                "IntBridgeInstallation": "brid",
                "TunnelInstallation": "tun",
                "IntTunnelInstallation": "tun",
            }
            if surface_local in _INSTALLATION_SELF_NS and _feat_local in _INSTALLATION_FEATURES_V2:
                # CityGML 2.0: an installation feature owns its geometry directly
                # via lod{N}Geometry. It must not create another nested installation.
                lod_tag = f"lod{lod}Geometry"
                lod_elem = SubElement(feature_el, Q(_INSTALLATION_SELF_NS[surface_local], lod_tag))
                ms_attrs = {
                    GML_ID: (
                        make_gml_id(str(ms_id), used_feat_ids)
                        if ms_id
                        else make_gml_id(f"ID_{uuid4().hex}", used_feat_ids)
                    )
                }
                ms = SubElement(lod_elem, Q("gml", "MultiSurface"), ms_attrs)
                _write_surface_generics_on_surface(feature_el, mat)
                return feature_el, ms

            if ns_key == "bldg" and surface_local == "BuildingPart":
                prop_el = SubElement(feature_el, Q("bldg", "consistsOfBuildingPart"))
                part_el = SubElement(prop_el, Q("bldg", "BuildingPart"))

                if surf_id:
                    validated_part_id = make_gml_id(str(surf_id), used_feat_ids)
                    part_el.set(GML_ID, validated_part_id)
                else:
                    part_el.set(GML_ID, f"ID_{uuid4().hex}")

                lod_tag = f"lod{lod}MultiSurface"
                lod_elem = SubElement(part_el, Q("bldg", lod_tag))
                ms_attrs = {
                    GML_ID: (
                        make_gml_id(str(ms_id), used_feat_ids)
                        if ms_id
                        else make_gml_id(f"ID_{uuid4().hex}", used_feat_ids)
                    )
                }
                ms = SubElement(lod_elem, Q("gml", "MultiSurface"), ms_attrs)

                _write_surface_generics_on_surface(part_el, mat)
                return part_el, ms
            
            # CityGML 2.0:
            # BuildingInstallation ist KEINE boundedBy-Surface, sondern ein untergeordnetes Objekt
            # unter outerBuildingInstallation / interiorBuildingInstallation und nutzt lod{N}Geometry.
            if surface_local in ("BuildingInstallation", "IntBuildingInstallation"):
                # Guard: Wenn das Feature selbst schon eine BuildingInstallation ist,
                # darf KEINE verschachtelte outerBuildingInstallation erzeugt werden.
                # In diesem Fall schreiben wir die Geometrie direkt auf das Feature.
                _feat_local = feature_el.tag.rsplit("}", 1)[-1] if "}" in feature_el.tag else feature_el.tag
                if _feat_local in ("BuildingInstallation", "IntBuildingInstallation"):
                    lod_tag = f"lod{lod}Geometry"
                    lod_elem = SubElement(feature_el, Q("bldg", lod_tag))
                    ms_attrs = {
                        GML_ID: (
                            make_gml_id(str(ms_id), used_feat_ids)
                            if ms_id
                            else make_gml_id(f"ID_{uuid4().hex}", used_feat_ids)
                        )
                    }
                    ms = SubElement(lod_elem, Q("gml", "MultiSurface"), ms_attrs)
                    _write_surface_generics_on_surface(feature_el, mat)
                    return feature_el, ms

                prop_local = (
                    "outerBuildingInstallation"
                    if surface_local == "BuildingInstallation"
                    else "interiorBuildingInstallation"
                )

                prop_el = SubElement(feature_el, Q("bldg", prop_local))
                inst_el = SubElement(prop_el, Q("bldg", surface_local))

                # gml:id der Installation
                if surf_id:
                    # XSD-Konformität: surf_id aus Material-Properties validieren
                    validated_inst_id = make_gml_id(str(surf_id), used_feat_ids)
                    inst_el.set(GML_ID, validated_inst_id)
                else:
                    inst_el.set(GML_ID, f"ID_{uuid4().hex}")

                # Material-Custom-Properties als genericAttribute an der Installation
                _write_surface_generics_on_surface(inst_el, mat)

                # Geometrie: lod{N}Geometry (nicht lod{N}MultiSurface!)
                lod_tag = f"lod{lod}Geometry"
                lod_elem = SubElement(inst_el, Q("bldg", lod_tag))

                # gml:MultiSurface innerhalb der GeometryProperty
                ms_attrs = {
                    GML_ID: (
                        make_gml_id(str(ms_id), used_feat_ids)
                        if ms_id
                        else make_gml_id(f"ID_{uuid4().hex}", used_feat_ids)
                    )
                }
                ms = SubElement(lod_elem, Q("gml", "MultiSurface"), ms_attrs)
                return inst_el, ms

            # Default: CityGML 2.0 boundedBy-Surfaces (WallSurface/RoofSurface/...)
            # Namespace muss zum Feature passen (bldg:boundedBy vs brid:boundedBy).
            # Für Bridges erwartet das Schema brid:boundedBy + brid:*Surface.
            bb = SubElement(feature_el, Q(ns_key, "boundedBy"))
            surf = SubElement(bb, Q(ns_key, surface_local))

            # gml:id der konkreten bldg:*Surface
            if surf_id:
                # XSD-Konformität: surf_id aus Material-Properties validieren
                validated_surf_id = make_gml_id(str(surf_id), used_feat_ids)
                surf.set(GML_ID, validated_surf_id)
            else:
                surf.set(GML_ID, f"ID_{uuid4().hex}")

            # LOD-flexibel: lod{N}MultiSurface
            # Namespace muss zum Feature passen (bldg/brid/...)
            lod_tag = f"lod{lod}MultiSurface"
            lod_elem = SubElement(surf, Q(ns_key, lod_tag))

            # gml:id der MultiSurface
            ms_attrs = {
                GML_ID: (
                    make_gml_id(str(ms_id), used_feat_ids)
                    if ms_id
                    else make_gml_id(f"ID_{uuid4().hex}", used_feat_ids)
                )
            }
            ms = SubElement(lod_elem, Q("gml", "MultiSurface"), ms_attrs)

            # CityGML 2.0 XSD-Reihenfolge (AbstractBoundarySurfaceType):
            # lod{N}MultiSurface* kommt VOR genericAttribute/externalReference/etc.
            # Daher Material-Generics erst NACH der Geometrie schreiben.
            _write_surface_generics_on_surface(surf, mat)
            return surf, ms

        _INLINE_PART_RELATIONS_V2 = {
            "BuildingPart": ("bldg", "consistsOfBuildingPart", "BuildingPart"),
            "BuildingInstallation": ("bldg", "outerBuildingInstallation", "BuildingInstallation"),
            "IntBuildingInstallation": ("bldg", "interiorBuildingInstallation", "IntBuildingInstallation"),
            "BuildingFurniture": ("bldg", "interiorFurniture", "BuildingFurniture"),
            "BridgePart": ("brid", "consistsOfBridgePart", "BridgePart"),
            "BridgeInstallation": ("brid", "outerBridgeInstallation", "BridgeInstallation"),
            "IntBridgeInstallation": ("brid", "interiorBridgeInstallation", "IntBridgeInstallation"),
            "BridgeConstructionElement": ("brid", "outerBridgeConstruction", "BridgeConstructionElement"),
            "IntBridgeConstructionElement": ("brid", "interiorBridgeConstruction", "IntBridgeConstructionElement"),
            "BridgeFurniture": ("brid", "interiorBridgeFurniture", "BridgeFurniture"),
            "TunnelPart": ("tun", "consistsOfTunnelPart", "TunnelPart"),
            "TunnelInstallation": ("tun", "outerTunnelInstallation", "TunnelInstallation"),
            "IntTunnelInstallation": ("tun", "interiorTunnelInstallation", "IntTunnelInstallation"),
            "TunnelFurniture": ("tun", "interiorTunnelFurniture", "TunnelFurniture"),
        }

        def _entry_value(entry, key: str) -> str:
            if not entry:
                return ""
            try:
                value = entry.get(key, "")
            except Exception:
                value = ""
            if value:
                return str(value).strip()
            try:
                mat = entry.get("mat")
            except Exception:
                mat = None
            try:
                value = mat.get(key, "") if mat else ""
            except Exception:
                value = ""
            return str(value).strip() if value else ""

        def _entry_inline_part_type(entry) -> str:
            part_type = _entry_value(entry, "subfeature_type")
            if not part_type:
                part_type = _entry_value(entry, "cgml3_subfeature_type")
            if part_type in _INLINE_PART_RELATIONS_V2:
                return part_type
            try:
                for poly_data in entry.get("polygons", []) or []:
                    part_type = _entry_inline_part_type(poly_data)
                    if part_type:
                        return part_type
            except Exception:
                pass
            return ""

        def _entry_inline_part_id(entry) -> str:
            part_id = _entry_value(entry, "subfeature_id")
            if not part_id:
                part_id = _entry_value(entry, "cgml3_subfeature_id")
            if part_id:
                return part_id
            try:
                for poly_data in entry.get("polygons", []) or []:
                    part_id = _entry_inline_part_id(poly_data)
                    if part_id:
                        return part_id
            except Exception:
                pass
            return ""

        def _entry_inline_part_name(entry) -> str:
            part_name = _entry_value(entry, "subfeature_name")
            if not part_name:
                part_name = _entry_value(entry, "cgml3_subfeature_name")
            if part_name:
                return part_name
            try:
                for poly_data in entry.get("polygons", []) or []:
                    part_name = _entry_inline_part_name(poly_data)
                    if part_name:
                        return part_name
            except Exception:
                pass
            return ""

        def _split_inline_part_surfaces(con_surfaces, allowed_part_types: set[str]):
            filtered_surfaces = {}
            part_groups = {}

            for surf_type, polys in con_surfaces.items():
                kept = []
                for gp in polys:
                    part_type = _entry_inline_part_type(gp)
                    if part_type in allowed_part_types:
                        part_id = _entry_inline_part_id(gp)
                        key = (part_type, part_id or f"{part_type}_{len(part_groups) + 1}")
                        group = part_groups.get(key)
                        if group is None:
                            group = {
                                "part_type": part_type,
                                "part_id": part_id,
                                "part_name": _entry_inline_part_name(gp),
                                "surfaces": {},
                            }
                            part_groups[key] = group
                        elif not group.get("part_name"):
                            group["part_name"] = _entry_inline_part_name(gp)
                        group["surfaces"].setdefault(surf_type, []).append(gp)
                    else:
                        kept.append(gp)
                if kept:
                    filtered_surfaces[surf_type] = kept

            return filtered_surfaces, list(part_groups.values())

        def _write_con_surface_entry(target_feature, surf_type, gp, openings_by_parent_map, base_lod):
            surface_lod = _surface_lod_with_openings(gp, base_lod, openings_by_parent_map)
            fp = gp.get("filling_parent_sid") or ""
            if fp and gp.get("opening_info"):
                return

            if gp.get("is_multisurface", False):
                surf_el, ms_parent = _begin_bs(
                    target_feature,
                    surf_type,
                    gp.get("surf_id"),
                    gp.get("ms_id"),
                    gp.get("mat"),
                    surface_lod,
                )
                for poly_data in gp.get("polygons", []):
                    write_polygon_with_ring_ids(
                        parent_ms=ms_parent,
                        poly_gid=poly_data.get("poly_gid") or _get_poly_gid(poly_data),
                        exterior_ring=poly_data["ext_xyz"],
                        interior_rings=poly_data["int_xyz"] if poly_data.get("int_xyz") else None,
                        srs_name=srs_name,
                        exterior_ring_id=poly_data.get("ext_id"),
                        interior_ring_ids=poly_data.get("int_ids") if poly_data.get("int_ids") else None,
                    )
                _write_inline_surface_opening(surf_el, gp.get("polygons", []), surface_lod)
            else:
                surf_el, ms_parent = _begin_bs(
                    target_feature,
                    surf_type,
                    gp.get("surf_id"),
                    gp.get("ms_id"),
                    gp.get("mat"),
                    surface_lod,
                )
                write_polygon_with_ring_ids(
                    parent_ms=ms_parent,
                    poly_gid=gp.get("poly_gid") or _get_poly_gid(gp),
                    exterior_ring=gp["ext_xyz"],
                    interior_rings=gp["int_xyz"] if gp.get("int_xyz") else None,
                    srs_name=srs_name,
                    exterior_ring_id=gp.get("ext_id"),
                    interior_ring_ids=gp.get("int_ids") if gp.get("int_ids") else None,
                )
                _write_inline_surface_opening(surf_el, [gp], surface_lod)

            parent_sid = gp.get("surf_id") or ""
            for opening_gp in openings_by_parent_map.pop(parent_sid, []):
                if opening_gp.get("is_multisurface", False):
                    _write_inline_surface_opening(
                        surf_el,
                        opening_gp.get("polygons", []),
                        surface_lod,
                    )
                else:
                    _write_inline_surface_opening(
                        surf_el,
                        [opening_gp],
                        surface_lod,
                    )

        def _collect_surface_polygon_ids(entries):
            poly_ids = []
            for entry in entries or []:
                if entry.get("is_multisurface", False):
                    for poly_data in entry.get("polygons", []) or []:
                        pid = poly_data.get("poly_gid") or _get_poly_gid(poly_data)
                        if pid:
                            poly_ids.append(pid)
                else:
                    pid = entry.get("poly_gid") or _get_poly_gid(entry)
                    if pid:
                        poly_ids.append(pid)
            return poly_ids

        def _write_inline_feature_geometry(feature_el, feature_local, surface_entries, base_lod):
            lod_value = _entries_lod_value(surface_entries, base_lod)
            polygon_ids = _collect_surface_polygon_ids(surface_entries)
            if not polygon_ids:
                return

            if feature_local in ("BuildingPart", "BridgePart", "TunnelPart"):
                lod_solid = SubElement(feature_el, Q(ns_key, f"lod{lod_value}Solid"))
                solid = SubElement(lod_solid, Q("gml", "Solid"), {GML_ID: f"UUID_{uuid4()}"})
                ext = SubElement(solid, Q("gml", "exterior"))
                comp = SubElement(ext, Q("gml", "CompositeSurface"), {GML_ID: f"UUID_{uuid4()}"})
                for pid in polygon_ids:
                    SubElement(comp, Q("gml", "surfaceMember"), {Q("xlink", "href"): f"#{pid}"})
                return

            if feature_local in _INSTALLATION_FEATURES_V2:
                lod_geom = SubElement(feature_el, Q(ns_key, f"lod{lod_value}Geometry"))
                ms = SubElement(
                    lod_geom,
                    Q("gml", "MultiSurface"),
                    {GML_ID: make_gml_id(f"ID_{uuid4().hex}", used_feat_ids)},
                )
                for pid in polygon_ids:
                    SubElement(ms, Q("gml", "surfaceMember"), {Q("xlink", "href"): f"#{pid}"})

        def _write_inline_part_features(parent_feature, part_groups, openings_by_parent_map, base_lod, closures_by_parent_map=None):
            if closures_by_parent_map is None:
                closures_by_parent_map = {}
            for group in part_groups:
                part_type = group.get("part_type", "")
                relation = _INLINE_PART_RELATIONS_V2.get(part_type)
                if not relation:
                    continue
                rel_ns, rel_local, feature_local = relation
                if rel_ns != ns_key:
                    continue

                raw_part_id = str(group.get("part_id") or f"{part_type}_{uuid4().hex}").strip()
                part_el = SubElement(
                    SubElement(parent_feature, Q(rel_ns, rel_local)),
                    Q(rel_ns, feature_local),
                    {GML_ID: make_gml_id(raw_part_id, used_feat_ids)},
                )

                part_name = str(group.get("part_name") or "").strip()
                if part_name and part_name != raw_part_id:
                    SubElement(part_el, Q("gml", "name")).text = part_name

                geometry_entries = []
                for _entries in (group.get("surfaces", {}) or {}).values():
                    geometry_entries.extend(list(_entries or []))
                if closures_by_parent_map and raw_part_id:
                    geometry_entries.extend(list(closures_by_parent_map.get(raw_part_id, []) or []))
                _write_inline_feature_geometry(part_el, feature_local, geometry_entries, base_lod)

                for surf_type, polys in group.get("surfaces", {}).items():
                    for gp in polys:
                        _write_con_surface_entry(
                            part_el,
                            surf_type,
                            gp,
                            openings_by_parent_map,
                            base_lod,
                        )

                # ClosureSurface-Kinder des Part/Installation schreiben
                if closures_by_parent_map and raw_part_id:
                    for c_gp in closures_by_parent_map.pop(raw_part_id, []):
                        _write_con_surface_entry(
                            part_el,
                            "ClosureSurface",
                            c_gp,
                            openings_by_parent_map,
                            base_lod,
                        )

                if feature_local in (
                    "BuildingPart",
                    "BridgePart",
                    "TunnelPart",
                    *_INSTALLATION_FEATURES_V2,
                ):
                    _reorder_children_for_xsd(part_el, ns_prefix=rel_ns)

        def _reorder_children_for_xsd(feature_el, *, ns_prefix: str):
            """
            Ensure XSD order for brid:Bridge (and similar) where some properties must
            appear before others. ElementTree preserves insertion order, so we
            explicitly reorder children when needed.
            """
            try:
                kids = list(feature_el)
                if not kids:
                    return

                def _lname(tag):
                    if not isinstance(tag, str):
                        return ""
                    if tag.startswith("{") and "}" in tag:
                        return tag.split("}", 1)[1]
                    return tag.split(":", 1)[-1]

                def _ns(tag):
                    if not isinstance(tag, str):
                        return ""
                    if tag.startswith("{") and "}" in tag:
                        return tag[1:tag.index("}")]
                    return ""

                feature_ns = _ns(feature_el.tag)
                wants_bridge = (ns_prefix == "brid")
                wants_building = (ns_prefix == "bldg")
                wants_tunnel = (ns_prefix == "tun")
                feature_local = _lname(feature_el.tag)

                if feature_local in _INSTALLATION_FEATURES_V2:
                    original_index = {id(k): i for i, k in enumerate(kids)}

                    def _installation_order(child):
                        child_ns = _ns(child.tag)
                        child_local = _lname(child.tag)
                        if child_ns == feature_ns and child_local in ("class", "function", "usage"):
                            return 20
                        if child_ns == feature_ns and re.match(r"^lod[234]Geometry$", child_local):
                            return 30
                        if child_ns == feature_ns and re.match(r"^lod[234]ImplicitRepresentation$", child_local):
                            return 40
                        if child_ns == feature_ns and child_local == "boundedBy":
                            return 50
                        return 10

                    ordered = sorted(kids, key=lambda k: (_installation_order(k), original_index.get(id(k), 0)))
                    if ordered != kids:
                        for k in kids:
                            feature_el.remove(k)
                        for k in ordered:
                            feature_el.append(k)
                    return

                def _construction_order(child):
                    child_ns = _ns(child.tag)
                    child_local = _lname(child.tag)
                    if child_ns != feature_ns:
                        return 10

                    if wants_building:
                        if child_local in ("lod0FootPrint", "lod0RoofEdge"):
                            return 40
                        if re.match(r"^lod[12](Solid|MultiSurface|MultiCurve|TerrainIntersection)$", child_local):
                            return 40
                        if child_local in ("outerBuildingInstallation", "interiorBuildingInstallation"):
                            return 50
                        if child_local == "boundedBy":
                            return 60
                        if re.match(r"^lod[34](Solid|MultiSurface|MultiCurve|TerrainIntersection)$", child_local):
                            return 70
                        if child_local == "interiorRoom":
                            return 80
                        if child_local == "consistsOfBuildingPart":
                            return 90
                        if child_local == "address":
                            return 100
                        return 10

                    if wants_bridge:
                        if re.match(r"^lod[12](Solid|MultiSurface|MultiCurve|TerrainIntersection)$", child_local):
                            return 40
                        if child_local in (
                            "outerBridgeConstruction",
                            "outerBridgeInstallation",
                            "interiorBridgeInstallation",
                        ):
                            return 50
                        if child_local == "boundedBy":
                            return 60
                        if re.match(r"^lod[34](Solid|MultiSurface|MultiCurve|TerrainIntersection)$", child_local):
                            return 70
                        if child_local == "interiorBridgeRoom":
                            return 80
                        if child_local == "consistsOfBridgePart":
                            return 90
                        if child_local == "address":
                            return 100
                        return 10

                    if wants_tunnel:
                        if re.match(r"^lod[12](Solid|MultiSurface|MultiCurve|TerrainIntersection)$", child_local):
                            return 40
                        if child_local in ("outerTunnelInstallation", "interiorTunnelInstallation"):
                            return 50
                        if child_local == "boundedBy":
                            return 60
                        if re.match(r"^lod[34](Solid|MultiSurface|MultiCurve|TerrainIntersection)$", child_local):
                            return 70
                        if child_local == "interiorHollowSpace":
                            return 80
                        if child_local == "consistsOfTunnelPart":
                            return 90
                        return 10

                    return 10

                if wants_bridge or wants_tunnel or wants_building:
                    original_index = {id(k): i for i, k in enumerate(kids)}
                    ordered = sorted(kids, key=lambda k: (_construction_order(k), original_index.get(id(k), 0)))
                    if ordered != kids:
                        for k in kids:
                            feature_el.remove(k)
                        for k in ordered:
                            feature_el.append(k)
            except Exception:
                # Best-effort only: never fail the export due to ordering.
                return

        if is_implicit:
            # ImplicitGeometry mit Unterstützung für Mesh-Instanzen:
            # genau ein Prototyp pro Mesh enthält die Geometrie, alle weiteren Objekte
            # verweisen per xlink:href auf dessen MultiSurface.

            # Fallback, falls aus irgendeinem Grund keine Metadaten verfügbar sind:
            if meta_for_mesh is None:
                if (ns_key, local_name) == ("veg", "SolitaryVegetationObject"):
                    lod_imp = SubElement(feature, Q("veg", "lod2ImplicitRepresentation"))
                else:
                    if (ns_key, local_name) == ("veg", "SolitaryVegetationObject"):
                        lod_imp = SubElement(feature, Q("veg", "lod2ImplicitRepresentation"))
                        imp = SubElement(lod_imp, Q("core", "ImplicitGeometry"))
                    else:
                        lod_imp = SubElement(feature, Q(ns_key, "lod2ImplicitRepresentation"))
                        imp = SubElement(lod_imp, Q("core", "ImplicitGeometry"))
                tm  = SubElement(imp, Q("core", "transformationMatrix"))
                tm.text = " ".join(fmt(v) for v in _matrix_from_matrix_world_no_translation(obj))
                # relativeGeometry mit MultiSurface (Prototyp-Geometrie relativ zu ref_base)
                rel = SubElement(imp, Q("core", "relativeGMLGeometry"))

                # ms MUSS IMMER definiert sein, bevor wir Polygone schreiben
                ms = SubElement(rel, Q("gml", "MultiSurface"), {GML_ID: ms_id})

                # Polygone relativ zu ref_base schreiben (immer nur, wenn ms existiert)
                for polys in geom_buf.values():
                    for gp in polys:
                        # MultiSurface-Gruppe?
                        if gp.get("is_multisurface", False):
                            for poly_data in gp.get("polygons", []):
                                ext_rel = [
                                    (x - ref_base[0], y - ref_base[1], z - ref_base[2])
                                    for (x, y, z) in poly_data["ext_xyz"]
                                ]
                                if poly_data.get("int_xyz"):
                                    ints_rel = [[
                                        (x - ref_base[0], y - ref_base[1], z - ref_base[2])
                                        for (x, y, z) in ring
                                    ] for ring in poly_data["int_xyz"]]
                                else:
                                    ints_rel = None

                                write_polygon_with_ring_ids(
                                    parent_ms=ms,
                                    poly_gid=_get_poly_gid(poly_data),
                                    exterior_ring=ext_rel,
                                    interior_rings=ints_rel,
                                    srs_name=srs_name,
                                    exterior_ring_id=poly_data.get("ext_id"),
                                    interior_ring_ids=poly_data.get("int_ids") if poly_data.get("int_ids") else None,
                                )
                        else:
                            # Einzelnes Polygon
                            ext_rel = [
                                (x - ref_base[0], y - ref_base[1], z - ref_base[2])
                                for (x, y, z) in gp["ext_xyz"]
                            ]
                            if gp.get("int_xyz"):
                                ints_rel = [[
                                    (x - ref_base[0], y - ref_base[1], z - ref_base[2])
                                    for (x, y, z) in ring
                                ] for ring in gp["int_xyz"]]
                            else:
                                ints_rel = None

                            write_polygon_with_ring_ids(
                                parent_ms=ms,
                                poly_gid=_get_poly_gid(gp),
                                exterior_ring=ext_rel,
                                interior_rings=ints_rel,
                                srs_name=srs_name,
                                exterior_ring_id=gp.get("ext_id"),
                                interior_ring_ids=gp.get("int_ids") if gp.get("int_ids") else None,
                            )

                ref = SubElement(imp, Q("core", "referencePoint"))
                pt = SubElement(ref, Q("gml", "Point"))
                pos = SubElement(pt, Q("gml", "pos"))
                pos.set("srsDimension", "3")
                pos.text = "0 0 0"
            else:
                # Prototyp für dieses Mesh? -> Geometrie anlegen
                if is_imp_prototype:
                    if (ns_key, local_name) == ("veg", "SolitaryVegetationObject"):
                        lod_imp = SubElement(feature, Q("veg", "lod2ImplicitRepresentation"))
                    else:
                        lod_imp = SubElement(feature, Q(ns_key, "lod2ImplicitRepresentation"))
                    ref_base = meta_for_mesh.get("ref_base")
                    if ref_base is None:
                        # Verwende die transformierte Objekt-Position als Referenzpunkt
                        obj_loc_world = obj.matrix_world.translation
                        obj_loc_local = np.asarray([(float(obj_loc_world.x), float(obj_loc_world.y), float(obj_loc_world.z))], dtype=np.float64)
                        obj_loc_tgt, origin_tgt_obj = trf.transform_with_origin(obj_loc_local, (ox, oy, oz))
                        # obj_loc_tgt is LOCAL relative to origin_tgt_obj, so absolute is origin_tgt_obj + obj_loc_tgt.
                        ref_base = (
                            float(obj_loc_tgt[0][0]) + origin_tgt_obj[0],
                            float(obj_loc_tgt[0][1]) + origin_tgt_obj[1],
                            float(obj_loc_tgt[0][2]) + origin_tgt_obj[2],
                        )
                        meta_for_mesh["ref_base"] = ref_base

                    # einige Beispielpunkte der Prototyp-Geometrie merken (für Klone)
                    if not meta_for_mesh.get("probe_abs"):
                        meta_for_mesh["probe_abs"] = probe_abs_cur[:] if probe_abs_cur else [ref_base]

                    ms_id = meta_for_mesh.get("ms_id")
                    if ms_id is None:
                        ms_id = f"UUID_{uuid4()}"
                        meta_for_mesh["ms_id"] = ms_id

                    # Appearance an die Prototyp-Geometrie koppeln (MultiSurface-ID)
                    try:
                        obj["_appearance_id_override"] = ms_id
                    except Exception:
                        pass

                    # lod2ImplicitRepresentation / ImplicitGeometry-Knoten für den Prototyp
                    if (ns_key, local_name) == ("veg", "SolitaryVegetationObject"):
                        lod_imp = SubElement(feature, Q("veg", "lod2ImplicitRepresentation"))
                        imp = SubElement(lod_imp, Q("core", "ImplicitGeometry"))
                    else:
                        lod_imp = SubElement(feature, Q(ns_key, "lod2ImplicitRepresentation"))
                        imp = SubElement(lod_imp, Q("core", "ImplicitGeometry"))

                    tm  = SubElement(imp, Q("core", "transformationMatrix"))
                    tm.text = " ".join(fmt(v) for v in _matrix_from_matrix_world_no_translation(obj))

                    # relativeGMLGeometry MUSS vor referencePoint kommen (CityGML 2.0 / core namespace)
                    rel = SubElement(imp, Q("core", "relativeGMLGeometry"))

                    # Import.gml: CompositeSurface inline (wir schreiben die Polygone direkt hinein)
                    cs_id = f"UUID_{uuid4()}"
                    cs = SubElement(rel, Q("gml", "CompositeSurface"), {GML_ID: cs_id})
                    meta_for_mesh["cs_id"] = cs_id

                    # write_polygon_with_ring_ids erwartet ein Parent mit gml:surfaceMember
                    # CompositeSurface ist dafür korrekt → wir verwenden cs als "ms"
                    ms = cs

                    # Deine Polygon-Schreibung: hier müssen die Polygon-IDs (poly_gid) stabil sein,
                    # denn genau diese IDs werden später als app:target referenziert.
                    for polys in geom_buf.values():
                        for gp in polys:
                            SubElement(cs, Q("gml", "surfaceMember"), {Q("xlink", "href"): f"#{_get_poly_gid(gp)}"})

                    # Referenzpunkt des Prototypen (nach relativeGeometry)
                    ref = SubElement(imp, Q("core", "referencePoint"))
                    pt = SubElement(ref, Q("gml", "Point"))
                    pos = SubElement(pt, Q("gml", "pos"))
                    pos.set("srsDimension", "3")
                    pos.text = " ".join(fmt(v) for v in ref_base)

                    for polys in geom_buf.values():
                        for gp in polys:
                            # Prüfen ob es ein MultiSurface-Gruppen-Dictionary ist
                            if gp.get("is_multisurface", False):
                                # MultiSurface-Gruppe: Polygone sind in "polygons" Liste
                                for poly_data in gp.get("polygons", []):
                                    ext_rel = [
                                        _transform_xyz_by_inv_no_translation(
                                            obj,
                                            (x - ref_base[0], y - ref_base[1], z - ref_base[2]),
                                        )
                                        for (x, y, z) in poly_data["ext_xyz"]
                                    ]
                                    if poly_data["int_xyz"]:
                                        ints_rel = [[
                                            _transform_xyz_by_inv_no_translation(
                                                obj,
                                                (x - ref_base[0], y - ref_base[1], z - ref_base[2]),
                                            )
                                            for (x, y, z) in ring
                                        ] for ring in poly_data["int_xyz"]]
                                    else:
                                        ints_rel = None
                                    write_polygon_with_ring_ids(
                                        parent_ms=ms,
                                        poly_gid=poly_data["poly_gid"],
                                        exterior_ring=ext_rel,
                                        interior_rings=ints_rel,
                                        srs_name=srs_name,
                                        exterior_ring_id=poly_data["ext_id"],
                                        interior_ring_ids=poly_data["int_ids"] if poly_data["int_ids"] else None,
                                    )
                            else:
                                # Einzelnes Polygon
                                ext_rel = [
                                    _transform_xyz_by_inv_no_translation(
                                        obj,
                                        (x - ref_base[0], y - ref_base[1], z - ref_base[2]),
                                    )
                                    for (x, y, z) in gp["ext_xyz"]
                                ]
                                if gp["int_xyz"]:
                                    ints_rel = [[
                                        _transform_xyz_by_inv_no_translation(
                                            obj,
                                            (x - ref_base[0], y - ref_base[1], z - ref_base[2]),
                                        )
                                        for (x, y, z) in ring
                                    ] for ring in gp["int_xyz"]]
                                else:
                                    ints_rel = None
                                write_polygon_with_ring_ids(
                                    parent_ms=ms,
                                    poly_gid=gp["poly_gid"],
                                    exterior_ring=ext_rel,
                                    interior_rings=ints_rel,
                                    srs_name=srs_name,
                                    exterior_ring_id=gp["ext_id"],
                                    interior_ring_ids=gp["int_ids"] if gp["int_ids"] else None,
                                )

                    # Nach Prototyp-Export: _appearance_id_override aufräumen
                    try:
                        if "_appearance_id_override" in obj:
                            del obj["_appearance_id_override"]
                    except Exception:
                        pass
                else:
                    # Klon eines ImplicitGeometry-Prototyps: nur Referenz + eigener Referenzpunkt
                    if (ns_key, local_name) == ("veg", "SolitaryVegetationObject"):
                        lod_imp = SubElement(feature, Q("veg", "lod2ImplicitRepresentation"))
                    else:
                        lod_imp = SubElement(feature, Q(ns_key, "lod2ImplicitRepresentation"))
                    ref_base0 = meta_for_mesh.get("ref_base")
                    ms_id = meta_for_mesh.get("ms_id")
                    proto_probe = meta_for_mesh.get("probe_abs") or []

                    if not ref_base0 or not ms_id:
                        # Sicherheits-Fallback: Klon wie früher voll exportieren
                        if (ns_key, local_name) == ("veg", "SolitaryVegetationObject"):
                            lod_imp = SubElement(feature, Q("veg", "lod2ImplicitRepresentation"))
                            imp = SubElement(lod_imp, Q("core", "ImplicitGeometry"))
                        else:
                            lod_imp = SubElement(feature, Q(ns_key, "lod2ImplicitRepresentation"))
                            imp = SubElement(lod_imp, Q("core", "ImplicitGeometry"))
                        tm  = SubElement(imp, Q("core", "transformationMatrix"))
                        tm.text = " ".join(fmt(v) for v in _matrix_from_matrix_world_no_translation(obj))
                        # relativeGeometry mit MultiSurface (Prototyp-Geometrie relativ zu ref_base)
                        rel = SubElement(imp, Q("core", "relativeGMLGeometry"))

                        # ms MUSS IMMER definiert sein, bevor wir Polygone schreiben
                        ms = SubElement(rel, Q("gml", "MultiSurface"), {GML_ID: ms_id})

                        # Polygone relativ zu ref_base schreiben (immer nur, wenn ms existiert)
                        for polys in geom_buf.values():
                            for gp in polys:
                                # MultiSurface-Gruppe?
                                if gp.get("is_multisurface", False):
                                    for poly_data in gp.get("polygons", []):
                                        ext_rel = [
                                            (x - ref_base[0], y - ref_base[1], z - ref_base[2])
                                            for (x, y, z) in poly_data["ext_xyz"]
                                        ]
                                        if poly_data.get("int_xyz"):
                                            ints_rel = [[
                                                (x - ref_base[0], y - ref_base[1], z - ref_base[2])
                                                for (x, y, z) in ring
                                            ] for ring in poly_data["int_xyz"]]
                                        else:
                                            ints_rel = None

                                        write_polygon_with_ring_ids(
                                            parent_ms=ms,
                                            poly_gid=_get_poly_gid(poly_data),
                                            exterior_ring=ext_rel,
                                            interior_rings=ints_rel,
                                            srs_name=srs_name,
                                            exterior_ring_id=poly_data.get("ext_id"),
                                            interior_ring_ids=poly_data.get("int_ids") if poly_data.get("int_ids") else None,
                                        )
                                else:
                                    # Einzelnes Polygon
                                    ext_rel = [
                                        (x - ref_base[0], y - ref_base[1], z - ref_base[2])
                                        for (x, y, z) in gp["ext_xyz"]
                                    ]
                                    if gp.get("int_xyz"):
                                        ints_rel = [[
                                            (x - ref_base[0], y - ref_base[1], z - ref_base[2])
                                            for (x, y, z) in ring
                                        ] for ring in gp["int_xyz"]]
                                    else:
                                        ints_rel = None

                                    write_polygon_with_ring_ids(
                                        parent_ms=ms,
                                        poly_gid=_get_poly_gid(gp),
                                        exterior_ring=ext_rel,
                                        interior_rings=ints_rel,
                                        srs_name=srs_name,
                                        exterior_ring_id=gp.get("ext_id"),
                                        interior_ring_ids=gp.get("int_ids") if gp.get("int_ids") else None,
                                    )

                        ref = SubElement(imp, Q("core", "referencePoint"))
                        pt = SubElement(ref, Q("gml", "Point"))
                        pos = SubElement(pt, Q("gml", "pos"))
                        pos.set("srsDimension", "3")
                        pos.text = "0 0 0"
                    else:
                        # IMPORTANT: For correct roundtrip with Blender transforms (incl. Dimensions/constraints),
                        # do not derive referencePoint via probe-based deltas to the prototype. Always use the
                        # actual object origin in target CRS.
                        try:
                            loc = obj.matrix_world.translation
                            coords_local = np.asarray([(float(loc.x), float(loc.y), float(loc.z))], dtype=np.float64)
                            coords_tgt, origin_tgt = trf.transform_with_origin(coords_local, (ox, oy, oz))
                            ref_base = (
                                float(coords_tgt[0][0]) + origin_tgt[0],
                                float(coords_tgt[0][1]) + origin_tgt[1],
                                float(coords_tgt[0][2]) + origin_tgt[2],
                            )
                        except Exception:
                            ref_base = ref_base0

                        lod_imp = SubElement(feature, Q(ns_key, "lod2ImplicitRepresentation"))
                        imp = SubElement(lod_imp, Q("core", "ImplicitGeometry"))

                        tm  = SubElement(imp, Q("core", "transformationMatrix"))
                        tm.text = " ".join(fmt(v) for v in _matrix_from_matrix_world_no_translation(obj))

                        # CityGML 2.0: relativeGMLGeometry referenziert eine Geometrie (z.B. gml:MultiSurface),
                        # nicht die MultiSurface-ID selbst (die ist nur der Container). In unseren Prototypen
                        # schreiben wir gml:CompositeSurface mit eigener gml:id (cs_id). Darauf müssen Klone verweisen.
                        cs_id_ref = meta_for_mesh.get("cs_id") if meta_for_mesh else None
                        href_id = cs_id_ref or ms_id
                        rel = SubElement(imp, Q("core", "relativeGMLGeometry"))
                        rel.set(Q("xlink", "href"), f"#{href_id}")

                        ref = SubElement(imp, Q("core", "referencePoint"))
                        pt = SubElement(ref, Q("gml", "Point"))
                        pos = SubElement(pt, Q("gml", "pos"))
                        pos.set("srsDimension", "3")
                        pos.text = " ".join(fmt(v) for v in ref_base)

        else:
            # explizite Geometrie
            if (ns_key, local_name) == ("brid", "Bridge"):
                # Separate Polygone nach Typ: con:*Surface vs. direkte Bridge-MultiSurface
                con_surfaces = {}  # thematische Surfaces (RoofSurface, WallSurface, etc.)
                openings_by_parent = {}
                bridge_multisurface = []  # direkte lod2MultiSurface Polygone (ohne spezifische Surface)
                _OPENING_SURF_TYPES_BRIDGE = {"Window", "Door"}
                
                for surf_type, polys in geom_buf.items():
                    filtered_polys = []
                    for gp in polys:
                        fp = gp.get("filling_parent_sid") or ""
                        if fp and (gp.get("opening_info") or surf_type in _OPENING_SURF_TYPES_BRIDGE):
                            openings_by_parent.setdefault(fp, []).append(gp)
                            continue
                        # Skip orphaned openings without parent
                        if surf_type in _OPENING_SURF_TYPES_BRIDGE:
                            continue
                        filtered_polys.append(gp)
                    if surf_type in ("BridgeConstructionElement", "BridgeInstallation"):
                        continue  # werden separat behandelt
                    elif surf_type == "Bridge":
                        # Direkte Bridge-Polygone ohne spezifische Surface → lod2MultiSurface
                        bridge_multisurface.extend(filtered_polys)
                    else:
                        # Thematische Surfaces (RoofSurface, WallSurface, etc.)
                        con_surfaces[surf_type] = filtered_polys
                    # NOTE: lod{N}Solid muss vor boundedBy* kommen (XSD-Reihenfolge).
                    # Solid-Export wird weiter unten geschrieben (vor den boundedBy-Surfaces),
                    # nicht innerhalb dieser Schleife, sonst kann es an einer unzulässigen Position landen.

                _bridge_inline_types = {"BridgePart"}
                _bridge_child_types = set()
                _raw_gid = str(obj.get("gml_id", "") or "").strip()
                if _raw_gid and _raw_gid in _child_objects_by_parent_id:
                    for _ch in _child_objects_by_parent_id[_raw_gid]:
                        _ch_feat = str(_ch.get("cgml3_feature", "") or "").strip()
                        if _ch_feat:
                            _bridge_child_types.add(_ch_feat)
                            _bridge_inline_types.discard(_ch_feat)
                if _is_inline_child and _obj_feat:
                    _bridge_child_types.add(_obj_feat)
                    _bridge_inline_types.discard(_obj_feat)
                for _cft in _bridge_child_types:
                    if _is_inline_child and _cft == _obj_feat and _cft in _INSTALLATION_FEATURES_V2:
                        continue
                    con_surfaces.pop(_cft, None)
                con_surfaces, inline_part_groups = _split_inline_part_surfaces(con_surfaces, _bridge_inline_types)

                # 1) Thematische con:*Surface-Boundaries schreiben (falls vorhanden)
                #
                # WICHTIG (CityGML 2.0 Bridge XSD Reihenfolge):
                # In brid:AbstractBridgeType müssen lod{N}Solid/lod{N}MultiSurface/etc. VOR
                # outerBridgeConstruction*/outerBridgeInstallation* und VOR boundedBy* stehen.
                # Daher dürfen wir hier KEINE solids mehr "nachträglich" erzeugen.
                if con_surfaces:
                    # CityGML 2.0: lod{N}Solid MUSS vor boundedBy* geschrieben werden

                    # 1) lod{N}Solid zuerst (nur Referenzen auf die Polygone via xlink:href)
                    lod_solid_tag = f"lod{lod_level}Solid"
                    lod_solid = _subelement_same_ns(feature, lod_solid_tag)

                    solid = SubElement(lod_solid, Q("gml", "Solid"), {GML_ID: f"UUID_{uuid4()}"})
                    ext = SubElement(solid, Q("gml", "exterior"))

                    # CityGML2/GML3.1.1: CompositeSurface ist kompatibler als Shell (wie in import.gml)
                    comp = SubElement(ext, Q("gml", "CompositeSurface"), {GML_ID: f"UUID_{uuid4()}"})

                    for polys in con_surfaces.values():
                        for gp in polys:
                            if gp.get("is_multisurface", False):
                                for poly in gp.get("polygons", []):
                                    pid = poly.get("poly_gid") or _get_poly_gid(poly)
                                    SubElement(comp, Q("gml", "surfaceMember"), {Q("xlink", "href"): f"#{pid}"})
                            else:
                                pid = gp.get("poly_gid") or _get_poly_gid(gp)
                                SubElement(comp, Q("gml", "surfaceMember"), {Q("xlink", "href"): f"#{pid}"})

                    # 2) Danach die thematischen boundedBy-Surfaces (WallSurface/RoofSurface/...)
                    # ClosureSurfaces with subfeature metadata are split into
                    # inline Part/Installation features below.

                    for surf_type, polys in con_surfaces.items():
                        for gp in polys:
                            surface_lod = _surface_lod_with_openings(gp, lod_level, openings_by_parent)
                            # Prüfen, ob dies eine echte MultiSurface ist
                            if gp.get("is_multisurface", False):
                                # MultiSurface mit mehreren surfaceMembers
                                surf_el, ms_parent = _begin_bs(
                                    feature,
                                    surf_type,
                                    gp.get("surf_id"),
                                    gp.get("ms_id"),
                                    gp.get("mat"),
                                    surface_lod,
                                )

                                # Jedes Polygon als surfaceMember schreiben
                                for poly_data in gp.get("polygons", []):
                                    write_polygon_with_ring_ids(
                                        parent_ms=ms_parent,
                                        poly_gid=poly_data.get("poly_gid") or _get_poly_gid(poly_data),
                                        exterior_ring=poly_data["ext_xyz"],
                                        interior_rings=poly_data["int_xyz"] or None,
                                        srs_name=srs_name,
                                        exterior_ring_id=poly_data.get("ext_id"),
                                        interior_ring_ids=poly_data.get("int_ids") or None,
                                    )
                                _write_inline_surface_opening(surf_el, gp.get("polygons", []), surface_lod)
                            else:
                                # Einzelnes Polygon
                                surf_el, ms_parent = _begin_bs(
                                    feature,
                                    surf_type,
                                    gp.get("surf_id"),
                                    gp.get("ms_id"),
                                    gp.get("mat"),
                                    surface_lod,
                                )
                                write_polygon_with_ring_ids(
                                    parent_ms=ms_parent,
                                    poly_gid=gp.get("poly_gid") or _get_poly_gid(gp),
                                    exterior_ring=gp["ext_xyz"],
                                    interior_rings=gp["int_xyz"] or None,
                                    srs_name=srs_name,
                                    exterior_ring_id=gp.get("ext_id"),
                                    interior_ring_ids=gp.get("int_ids") or None,
                                )
                                _write_inline_surface_opening(surf_el, [gp], surface_lod)

                            parent_sid = gp.get("surf_id") or ""
                            for opening_gp in openings_by_parent.pop(parent_sid, []):
                                if opening_gp.get("is_multisurface", False):
                                    _write_inline_surface_opening(
                                        surf_el,
                                        opening_gp.get("polygons", []),
                                        surface_lod,
                                    )
                                else:
                                    _write_inline_surface_opening(
                                        surf_el,
                                        [opening_gp],
                                        surface_lod,
                                    )

                    # (No additional lodXSolid here: it would end up after boundedBy and violate the XSD.)

                _write_inline_part_features(feature, inline_part_groups, openings_by_parent, lod_level)

                # 2) Direkte LOD-flexible MultiSurface für Bridge (falls vorhanden)
                if bridge_multisurface:
                    # Bridge-MultiSurface: direkt unter dem Feature
                    lod_ms_tag = f"lod{lod_level}MultiSurface"
                    # CityGML 2.0: Bridge LOD geometry properties are in the bridge namespace (brid:*), not core:*.
                    lod_ms = SubElement(feature, Q("brid", lod_ms_tag))
                    
                    # MultiSurface-ID aus den Polygonen extrahieren
                    ms_id = None
                    for gp in bridge_multisurface:
                        if gp.get("ms_id"):
                            ms_id = gp["ms_id"]
                            break
                    
                    if not ms_id:
                        ms_id = f"ID_{uuid4()}"
                    
                    ms = SubElement(
                        lod_ms,
                        Q("gml", "MultiSurface"),
                        {GML_ID: ms_id}
                    )
                    
                    # Gruppiere Bridge-Polygone nach CompositeSurface-ID
                    bridge_composite_groups = defaultdict(list)
                    bridge_standalone_polygons = []
                    
                    for gp in bridge_multisurface:
                        # Prüfen, ob es eine MultiSurface-Gruppe ist oder ein einzelnes Polygon
                        if gp.get("is_multisurface", False):
                            # MultiSurface-Gruppe: jedes Polygon einzeln prüfen
                            for poly in gp.get("polygons", []):
                                cs_id = poly.get("cs_id", "")
                                if cs_id:
                                    bridge_composite_groups[cs_id].append(poly)
                                else:
                                    bridge_standalone_polygons.append(poly)
                        else:
                            # Einzelnes Polygon
                            cs_id = gp.get("cs_id", "")
                            if cs_id:
                                bridge_composite_groups[cs_id].append(gp)
                            else:
                                bridge_standalone_polygons.append(gp)
                    
                    # 1) CompositeSurfaces schreiben
                    for cs_id, cs_polys in bridge_composite_groups.items():
                        write_compositesurface_with_polygons(
                            parent_ms=ms,
                            cs_id=cs_id,
                            polygons=cs_polys,
                            srs_name=srs_name,
                        )
                    
                    # 2) Standalone Polygone schreiben
                    for gp in bridge_standalone_polygons:
                        # Hier sind es nur noch einfache Polygon-Dicts
                        write_polygon_with_ring_ids(
                            parent_ms=ms,
                            poly_gid=gp["poly_gid"],
                            exterior_ring=gp["ext_xyz"],
                            interior_rings=gp["int_xyz"] if gp.get("int_xyz") else None,
                            srs_name=srs_name,
                            exterior_ring_id=gp["ext_id"],
                            interior_ring_ids=gp["int_ids"] if gp.get("int_ids") else None,
                        )
                
                # 3) BridgeConstructionElement
                # CityGML 2.0 XSD: outerBridgeConstruction* muss VOR boundedBy* kommen.
                # Daher schreiben wir diese Elemente in die gleiche Phase wie die anderen "early" Properties
                # und NICHT nach den boundedBy-Surfaces.
                for gp in geom_buf["BridgeConstructionElement"]:
                    wrapper = _subelement_same_ns(feature, "outerBridgeConstruction")
                    elem = SubElement(
                        wrapper,
                        Q("brid", "BridgeConstructionElement"),
                        {GML_ID: make_gml_id(gp.get("surf_id") or f"ID_{uuid4()}", used_feat_ids)}
                    )
                    # creationDate für BridgeConstructionElement
                    # (Untergeordnete Objekte haben eigene creationDate, nicht aus Parent)
                    creation_date = gp.get("creationDate")
                    if not creation_date:
                        creation_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    SubElement(elem, Q("core", "creationDate")).text = creation_date

                    # BridgeConstructiveElement-spezifische Attribute (analog zu BridgeInstallation)
                    from ...shared.attribute_utils import write_feature_type_attributes_bridge
                    write_feature_type_attributes_bridge(elem, gp, "brid")

                    # Geometrie unter brid:lod2Geometry (nicht core:lod2MultiSurface)
                    lod_geom_tag = f"lod{lod_level}Geometry"
                    lod_geom_elem = SubElement(elem, Q("brid", lod_geom_tag))
                    ms = SubElement(
                        lod_geom_elem,
                        Q("gml", "MultiSurface"),
                        {GML_ID: make_gml_id(gp.get("ms_id") or f"ID_{uuid4()}", used_feat_ids)}
                    )

                    # Prüfen, ob MultiSurface-Gruppe oder einzelnes Polygon
                    if gp.get("is_multisurface", False):
                        # MultiSurface: mehrere Polygone schreiben
                        for poly in gp.get("polygons", []):
                            write_polygon_with_ring_ids(
                                parent_ms=ms,
                                poly_gid=_get_poly_gid(poly),
                                exterior_ring=poly["ext_xyz"],
                                interior_rings=poly["int_xyz"] or None,
                                srs_name=srs_name,
                                exterior_ring_id=poly["ext_id"],
                                interior_ring_ids=poly["int_ids"] or None,
                            )
                    else:
                        # Einzelnes Polygon
                        write_polygon_with_ring_ids(
                            parent_ms=ms,
                            poly_gid=gp["poly_gid"],
                            exterior_ring=gp["ext_xyz"],
                            interior_rings=gp["int_xyz"] or None,
                            srs_name=srs_name,
                            exterior_ring_id=gp["ext_id"],
                            interior_ring_ids=gp["int_ids"] or None,
                        )

                # 4) BridgeInstallation
                for gp in geom_buf["BridgeInstallation"]:
                    # CityGML 2.0 XSD: outerBridgeInstallation* muss VOR boundedBy* kommen.
                    wrapper = _subelement_same_ns(feature, "outerBridgeInstallation")
                    elem = SubElement(
                        wrapper,
                        Q("brid", "BridgeInstallation"),
                        {GML_ID: make_gml_id(gp.get("surf_id") or f"ID_{uuid4()}", used_feat_ids)}
                    )
                    # creationDate für BridgeInstallation
                    # (Untergeordnete Objekte haben eigene creationDate, nicht aus Parent)
                    creation_date = gp.get("creationDate")
                    if not creation_date:
                        creation_date = getattr(obj, 'creationDate', datetime.now(timezone.utc).strftime("%Y-%m-%d"))
                    SubElement(elem, Q("core", "creationDate")).text = creation_date

                    # BridgeInstallation-spezifische Attribute (analog zu BridgeConstructionElement, falls benötigt)
                    from ...shared.attribute_utils import write_feature_type_attributes_bridge
                    write_feature_type_attributes_bridge(elem, gp, "brid")

                    # Geometrie unter brid:lod2Geometry (nicht core:lod2MultiSurface)
                    lod_geom_tag = f"lod{lod_level}Geometry"
                    lod_geom_elem = SubElement(elem, Q("brid", lod_geom_tag))
                    ms = SubElement(
                        lod_geom_elem,
                        Q("gml", "MultiSurface"),
                        {GML_ID: make_gml_id(gp.get("ms_id") or f"ID_{uuid4()}", used_feat_ids)}
                    )

                    # Prüfen, ob MultiSurface-Gruppe oder einzelnes Polygon
                    if gp.get("is_multisurface", False):
                        # MultiSurface: mehrere Polygone schreiben
                        for poly in gp.get("polygons", []):
                            write_polygon_with_ring_ids(
                                parent_ms=ms,
                                poly_gid=_get_poly_gid(poly),
                                exterior_ring=poly["ext_xyz"],
                                interior_rings=poly["int_xyz"] or None,
                                srs_name=srs_name,
                                exterior_ring_id=poly["ext_id"],
                                interior_ring_ids=poly["int_ids"] or None,
                            )
                    else:
                        # Einzelnes Polygon
                        write_polygon_with_ring_ids(
                            parent_ms=ms,
                            poly_gid=gp["poly_gid"],
                            exterior_ring=gp["ext_xyz"],
                            interior_rings=gp["int_xyz"] or None,
                            srs_name=srs_name,
                            exterior_ring_id=gp["ext_id"],
                            interior_ring_ids=gp["int_ids"] or None,
                        )

                    # relationToConstruction aus Custom Property (falls gewünscht)
                    rel = None
                    try:
                        mat = gp.get("mat")
                        if mat:
                            rtc = mat.get("relationToConstruction")
                            if rtc:
                                rel = SubElement(elem, Q("con", "relationToConstruction"))
                                rel.text = str(rtc)
                    except Exception:
                        pass

                # Abschließend: Reihenfolge im brid:Bridge-Element fixieren (XSD-konform),
                # damit outerBridgeConstruction*/outerBridgeInstallation* sicher vor boundedBy* stehen.
                _reorder_children_for_xsd(feature, ns_prefix=ns_key)

            else:
                # CityFurniture, SolitaryVegetationObject, PlantCover: CityGML 2.0-konforme Struktur
                # Für Solid-Referenzen: nur thematische BoundarySurfaces, keine Installationen
                con_surfaces = {
                    k: v for k, v in geom_buf.items()
                    if k not in ("BuildingInstallation", "IntBuildingInstallation")
                }
                # NOTE: For Buildings, we add BuildingInstallation objects while writing boundedBy
                # (via _begin_bs). Therefore, ordering MUST be fixed at the very end of building export,
                # not here (where outerBuildingInstallation may not exist yet).
                # --- CityFurniture (ersetzen) ---
                if (ns_key, local_name) == ("frn", "CityFurniture"):
                    elem = feature  # <frn:CityFurniture>

                    # CityGML 2.0 Schema-Reihenfolge für frn:CityFurniture:
                    # 1. boundedBy (bereits geschrieben durch _envelope_elem)
                    # 2. creationDate (bereits geschrieben oben, direkt nach boundedBy)
                    # 3. class, function, usage (HIER)
                    # 4. Generic Attributes (bereits geschrieben durch _write_generics)
                    # 5. lod*ImplicitRepresentation / lod*Geometry

                    # HINWEIS: creationDate wurde bereits direkt nach boundedBy geschrieben
                    # (siehe Code nach _envelope_elem)
                    # Daher NICHT nochmal hier schreiben!

                    # (Optional) CityFurniture hat in CityGML2 typ. class/function/usage – nur wenn vorhanden
                    # (Import.gml kann das enthalten oder nicht; die Reihenfolge wäre vor lod*Geometry/lod*Implicit)
                    for key, tag in (
                        ("frn:class", "class"),
                        ("frn:function", "function"),
                        ("frn:usage", "usage"),
                    ):
                        v = obj.get(key) if hasattr(obj, "get") else None
                        if v is None:
                            continue
                        # Listen unterstützen (mehrfach vorkommend)
                        if isinstance(v, (list, tuple)):
                            for it in v:
                                if it is not None and str(it).strip():
                                    SubElement(elem, Q("frn", tag)).text = str(it).strip()
                        else:
                            if str(v).strip():
                                SubElement(elem, Q("frn", tag)).text = str(v).strip()

                    # Generic Attributes (gen:*) werden bereits durch _write_generics() geschrieben
                    # WICHTIG: NICHT nochmal hier schreiben (sonst Duplikate)!
                    # _write_generic_attributes(elem, _get_generic_attributes_for_export(obj, target="OBJECT"))

                    # Geometrie unter frn:lodXImplicitRepresentation
                    lod_tag = f"lod{lod_level}ImplicitRepresentation"
                    lod_elem = SubElement(elem, Q("frn", lod_tag))

                    # CityGML2: ImplicitGeometry ist core-Element, im Import.gml unpräfixiert → literal schreiben
                    imp = SubElement(lod_elem, "ImplicitGeometry")

                    # transformationMatrix (1.0..16)
                    tm = SubElement(imp, "transformationMatrix")
                    tm.text = (getattr(obj, "transformationMatrix", None) or 
                        obj.get("transformationMatrix", "1 0 0 0  0 1 0 0  0 0 1 0  0 0 0 1"))

                    # WICHTIG: Reihenfolge: relativeGMLGeometry VOR referencePoint
                    # Wenn du für Instanzen xlink:href nutzt, dann setze das Attribut hier.
                    # Sonst: schreibe inline MultiSurface wie bisher.
                    # relativeGMLGeometry (ms nur bei Inline-Geometrie)
                    rel = SubElement(imp, "relativeGMLGeometry")

                    ms = None  # <-- WICHTIG: immer initialisieren

                    href = (
                        getattr(obj, "relativeGMLGeometry_href", None)
                        or obj.get("relativeGMLGeometry_href")
                    )

                    if href:
                        # Instanz verweist nur auf Prototyp → KEIN Inline-MultiSurface schreiben
                        rel.set(Q("xlink", "href"), str(href))
                    else:
                        # Inline MultiSurface schreiben
                        ms_id = (
                            getattr(obj, "implicit_ms_id", None)
                            or obj.get("implicit_ms_id")
                            or f"UUID_{uuid4()}"
                        )
                        ms = SubElement(rel, Q("gml", "MultiSurface"), {GML_ID: str(ms_id)})

                        for polys in geom_buf.values():
                            for gp in polys:
                                # Handle MultiSurface groups
                                if gp.get("is_multisurface"):
                                    for poly in gp.get("polygons", []):
                                        write_polygon_with_ring_ids(
                                            parent_ms=ms,
                                            poly_gid=_get_poly_gid(poly),
                                            exterior_ring=poly["ext_xyz"],
                                            interior_rings=poly["int_xyz"] if poly.get("int_xyz") else None,
                                            srs_name=srs_name,
                                            exterior_ring_id=poly["ext_id"],
                                            interior_ring_ids=poly["int_ids"] if poly.get("int_ids") else None,
                                        )
                                else:
                                    # Handle single polygons
                                    write_polygon_with_ring_ids(
                                        parent_ms=ms,
                                        poly_gid=_get_poly_gid(gp),
                                        exterior_ring=gp["ext_xyz"],
                                        interior_rings=gp["int_xyz"] if gp.get("int_xyz") else None,
                                        srs_name=srs_name,
                                        exterior_ring_id=gp["ext_id"],
                                        interior_ring_ids=gp["int_ids"] if gp.get("int_ids") else None,
                                    )

                    # referencePoint NACH relativeGMLGeometry
                    ref = SubElement(imp, "referencePoint")
                    pt = SubElement(ref, Q("gml", "Point"))
                    pos = SubElement(pt, Q("gml", "pos"))
                    pos.set("srsDimension", "3")
                    # Wenn du world/origin already abziehst: hier den lokalen Punkt.
                    # Wenn du den echten Weltpunkt willst: entsprechend rechnen.
                    ref_point = getattr(obj, "referencePoint", None) or obj.get("referencePoint") or "0 0 0"
                    pos.text = str(ref_point).strip()

                elif (ns_key, local_name) == ("veg", "SolitaryVegetationObject"):
                    elem = feature  # <veg:SolitaryVegetationObject>

                    # creationDate wurde bereits direkt nach boundedBy geschrieben
                    # Daher NICHT nochmal hier schreiben!
                    # SubElement(elem, "creationDate").text = obj.get(
                    #     "creationDate",
                    #     datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    # )

                    # CityGML2 Reihenfolge bei SolitaryVegetationObject:
                    # class?, function*, usage*, species?, height?, trunkDiameter?, crownDiameter?, lod*Geometry?, lod*ImplicitRepresentation?
                    # => Diese Attribute müssen VOR der lod2ImplicitRepresentation stehen.

                    # class / function / usage (optional)
                    for key, tag in (
                        ("veg:class", "class"),
                        ("veg:function", "function"),
                        ("veg:usage", "usage"),
                    ):
                        v = obj.get(key) if hasattr(obj, "get") else None
                        if v is None:
                            continue
                        if isinstance(v, (list, tuple)):
                            for it in v:
                                if it is not None and str(it).strip():
                                    SubElement(elem, Q("veg", tag)).text = str(it).strip()
                        else:
                            if str(v).strip():
                                SubElement(elem, Q("veg", tag)).text = str(v).strip()

                    # --- Vegetation properties MUST come before lod*Geometry / lod*ImplicitRepresentation ---
                    species = obj.get("veg:species", None)
                    if species is not None and str(species).strip():
                        SubElement(feature, Q("veg", "species")).text = str(species).strip()

                    height = obj.get("veg:height") if hasattr(obj, "get") else None
                    if height is not None and str(height).strip():
                        h_el = SubElement(feature, Q("veg", "height"))
                        # uom aus Property oder fallback wie in import.gml
                        uom = (obj.get("veg:height:uom") if hasattr(obj, "get") else None) or "m"
                        if uom:
                            h_el.set("uom", str(uom).strip())
                        h_el.text = str(height).strip()

                    trunk = obj.get("veg:trunkDiameter", None)
                    if trunk is not None and str(trunk).strip():
                        td = SubElement(feature, Q("veg", "trunkDiameter"))
                        uom = obj.get("veg:trunkDiameter:uom", None)
                        if uom is not None and str(uom).strip():
                            td.set("uom", str(uom).strip())
                        td.text = str(trunk).strip()

                    crown = obj.get("veg:crownDiameter", None)
                    if crown is not None and str(crown).strip():
                        cd = SubElement(feature, Q("veg", "crownDiameter"))
                        uom = obj.get("veg:crownDiameter:uom", None)
                        if uom is not None and str(uom).strip():
                            cd.set("uom", str(uom).strip())
                        cd.text = str(crown).strip()

                    # Generic Attributes (gen:*) – in export_korrekt.gml stehen sie direkt unter dem
                    # SolitaryVegetationObject (ohne core:genericAttribute wrapper).
                    # Quelle: Blender Custom Properties direkt am Objekt.
                    _write_generic_attributes(elem, _get_generic_attributes_for_export(obj, target="OBJECT"))

                    # Geometrie unter veg:lod2ImplicitRepresentation
                    lod_elem = SubElement(elem, Q("veg", "lod2ImplicitRepresentation"))

                    # CityGML2: ImplicitGeometry ist core-Element, im Import.gml unpräfixiert → literal
                    imp = SubElement(lod_elem, "ImplicitGeometry")

                    # transformationMatrix
                    tm = SubElement(imp, "transformationMatrix")
                    tm.text = (getattr(obj, "transformationMatrix", None) or 
                        obj.get("transformationMatrix", "1 0 0 0  0 1 0 0  0 0 1 0  0 0 0 1"))

                    # Reihenfolge: relativeGMLGeometry VOR referencePoint
                    # relativeGMLGeometry (ms nur bei Inline-Geometrie)
                    rel = SubElement(imp, "relativeGMLGeometry")

                    ms = None  # <-- WICHTIG: immer initialisieren

                    href = (
                        getattr(obj, "relativeGMLGeometry_href", None)
                        or obj.get("relativeGMLGeometry_href")
                    )

                    if href:
                        # Instanz verweist nur auf Prototyp → KEIN Inline-MultiSurface schreiben
                        rel.set(Q("xlink", "href"), str(href))
                    else:
                        # Inline MultiSurface schreiben
                        ms_id = (
                            getattr(obj, "implicit_ms_id", None)
                            or obj.get("implicit_ms_id")
                            or f"UUID_{uuid4()}"
                        )
                        ms = SubElement(rel, Q("gml", "MultiSurface"), {GML_ID: str(ms_id)})

                        for polys in geom_buf.values():
                            for gp in polys:
                                # Handle MultiSurface groups
                                if gp.get("is_multisurface"):
                                    for poly in gp.get("polygons", []):
                                        write_polygon_with_ring_ids(
                                            parent_ms=ms,
                                            poly_gid=_get_poly_gid(poly),
                                            exterior_ring=poly["ext_xyz"],
                                            interior_rings=poly["int_xyz"] if poly.get("int_xyz") else None,
                                            srs_name=srs_name,
                                            exterior_ring_id=poly["ext_id"],
                                            interior_ring_ids=poly["int_ids"] if poly.get("int_ids") else None,
                                        )
                                else:
                                    # Handle single polygons
                                    write_polygon_with_ring_ids(
                                        parent_ms=ms,
                                        poly_gid=_get_poly_gid(gp),
                                        exterior_ring=gp["ext_xyz"],
                                        interior_rings=gp["int_xyz"] if gp.get("int_xyz") else None,
                                        srs_name=srs_name,
                                        exterior_ring_id=gp["ext_id"],
                                        interior_ring_ids=gp["int_ids"] if gp.get("int_ids") else None,
                                    )

                    ref = SubElement(imp, "referencePoint")
                    pt = SubElement(ref, Q("gml", "Point"))
                    pos = SubElement(pt, Q("gml", "pos"))
                    pos.set("srsDimension", "3")
                    ref_point = getattr(obj, "referencePoint", None) or obj.get("referencePoint") or "0 0 0"
                    pos.text = str(ref_point).strip()
                elif (ns_key, local_name) == ("veg", "PlantCover"):
                    # Korrekte CityGML 2.0 Struktur: KEIN veg:plantCover-Wrapper, Attribute als direkte gen:*Attribute, Geometrie unter veg:lod1MultiSurface
                    elem = feature  # feature ist bereits <veg:PlantCover>
                    # Attribute direkt als gen:*Attribute
                    _write_generic_attributes(elem, _get_generic_attributes_for_export(obj, target="OBJECT"))
                    # Geometrie unter veg:lod1MultiSurface
                    lod_ms_tag = "lod1MultiSurface"
                    lod_ms = SubElement(elem, Q("veg", lod_ms_tag))
                    ms_id = None
                    for polys in geom_buf.values():
                        for gp in polys:
                            if gp.get("ms_id"):
                                ms_id = gp["ms_id"]
                                break
                        if ms_id:
                            break
                    if not ms_id:
                        ms_id = f"ID_{uuid4()}"
                    ms = SubElement(lod_ms, Q("gml", "MultiSurface"), {GML_ID: ms_id})
                    for polys in geom_buf.values():
                        for gp in polys:
                            fp = gp.get("filling_parent_sid") or ""
                            if fp and gp.get("opening_info"):
                                continue
                            if gp.get("is_multisurface", False):
                                for poly in gp.get("polygons", []):
                                    write_polygon_with_ring_ids(
                                        parent_ms=ms,
                                        poly_gid=_get_poly_gid(poly),
                                        exterior_ring=poly["ext_xyz"],
                                        interior_rings=poly["int_xyz"] if poly["int_xyz"] else None,
                                        srs_name=srs_name,
                                        exterior_ring_id=poly["ext_id"],
                                        interior_ring_ids=poly["int_ids"] if poly["int_ids"] else None,
                                    )
                            else:
                                write_polygon_with_ring_ids(
                                    parent_ms=ms,
                                    poly_gid=gp["poly_gid"],
                                    exterior_ring=gp["ext_xyz"],
                                    interior_rings=gp["int_xyz"] if gp["int_xyz"] else None,
                                    srs_name=srs_name,
                                    exterior_ring_id=gp["ext_id"],
                                    interior_ring_ids=gp["int_ids"] if gp["int_ids"] else None,
                                )
                else:
                    # Buildings etc.: thematische Surfaces (boundedBy*) und optionales lod{N}Solid.
                    #
                    # Wichtig (CityGML 2.0 XSD): lod{N}Solid muss VOR boundedBy* im Building stehen.
                    # Daher: zuerst (falls vorhanden) lod{N}Solid schreiben, danach erst boundedBy-Surfaces.

                    con_surfaces = {}
                    openings_by_parent = {}
                    solid_groups = geom_buf.get("Solid", []) or geom_buf.get("lodSolid", []) or []

                    # CityGML 2.0: Window/Door are NOT valid as standalone boundedBy
                    # surfaces. They MUST be nested inside <bldg:opening> within a
                    # parent BoundarySurface. Filter them into openings_by_parent.
                    _OPENING_SURF_TYPES_V2 = {"Window", "Door"}

                    for surf_type, polys in geom_buf.items():
                        if surf_type in ("Solid", "lodSolid"):
                            continue
                        filtered_polys = []
                        for gp in polys:
                            fp = gp.get("filling_parent_sid") or ""
                            if fp and (gp.get("opening_info") or surf_type in _OPENING_SURF_TYPES_V2):
                                openings_by_parent.setdefault(fp, []).append(gp)
                                continue
                            # Skip orphaned openings without parent (can't be boundedBy)
                            if surf_type in _OPENING_SURF_TYPES_V2:
                                continue
                            filtered_polys.append(gp)
                        con_surfaces[surf_type] = filtered_polys

                    if ns_key == "bldg":
                        _allowed_inline_parts = {
                            "BuildingPart",
                            "BuildingInstallation",
                            "IntBuildingInstallation",
                            "BuildingFurniture",
                        }
                    elif ns_key == "tun":
                        _allowed_inline_parts = {
                            "TunnelPart",
                            "TunnelInstallation",
                            "IntTunnelInstallation",
                            "TunnelFurniture",
                        }
                    elif ns_key == "brid":
                        _allowed_inline_parts = {
                            "BridgePart",
                            "BridgeInstallation",
                            "IntBridgeInstallation",
                            "BridgeConstructionElement",
                            "IntBridgeConstructionElement",
                            "BridgeFurniture",
                        }
                    else:
                        _allowed_inline_parts = set()

                    # Suppress material-based inline parts for types that will
                    # be exported via child objects (prevents duplication).
                    _child_feat_types = set()
                    _raw_gid = str(obj.get("gml_id", "") or "").strip()
                    if _raw_gid and _raw_gid in _child_objects_by_parent_id:
                        for _ch in _child_objects_by_parent_id[_raw_gid]:
                            _ch_feat = str(_ch.get("cgml3_feature", "") or "").strip()
                            if _ch_feat:
                                _child_feat_types.add(_ch_feat)
                                _allowed_inline_parts.discard(_ch_feat)
                    # When exporting an inline child, suppress self-referencing
                    # inline parts (a BuildingPart should not write another
                    # BuildingPart inside itself from its own faces).
                    if _is_inline_child and _obj_feat:
                        _child_feat_types.add(_obj_feat)
                        _allowed_inline_parts.discard(_obj_feat)

                    # Also remove these types from con_surfaces so that
                    # _begin_bs() does not write them (it handles
                    # BuildingPart/Installation surface types directly).
                    for _cft in _child_feat_types:
                        if _is_inline_child and _cft == _obj_feat and _cft in _INSTALLATION_FEATURES_V2:
                            continue
                        con_surfaces.pop(_cft, None)

                    if _allowed_inline_parts:
                        con_surfaces, inline_part_groups = _split_inline_part_surfaces(con_surfaces, _allowed_inline_parts)
                    else:
                        inline_part_groups = []

                    # In CityGML 2.0 muss measuredHeight vor den LOD-Geometrien kommen.
                    # Falls wir eine echte Solid-Geometrie exportieren, setzen wir measuredHeight,
                    # wenn es am Objekt vorhanden ist (Custom Property bldg:measuredHeight).
                    if solid_groups:
                        mh = obj.get("bldg:measuredHeight", None)
                        if mh is not None:
                            try:
                                SubElement(feature, Q("bldg", "measuredHeight")).text = fmt(float(mh))
                            except Exception:
                                pass

                    # 1) Optional: lod{N}Solid (reference-only from boundary surfaces).
                    # When disabled, the export behaves like before (no extra lod{N}Solid created).
                    try:
                        solid_lod = int((solid_groups[0] or {}).get("solid_lod", lod_level)) if solid_groups else int(lod_level)
                    except Exception:
                        solid_lod = int(lod_level)

                    solid_member_ids = []

                    if write_lod_solid_refs:
                        if solid_groups:
                            for sg in solid_groups:
                                for poly in (sg.get("solid_polys", []) or []):
                                    pid = _get_poly_gid(poly)
                                    if pid:
                                        solid_member_ids.append(pid)
                        else:
                            # Build solid surfaceMember references from con_surfaces, excluding installations.
                            # Installations must still be exported (as outer*/interior*Installation), but must
                            # not be referenced by the parent feature's lod{N}Solid.
                            for surf_type, polys in con_surfaces.items():
                                if surf_type in ("BuildingInstallation", "IntBuildingInstallation"):
                                    continue
                                for gp in polys:
                                    if gp.get("is_multisurface", False):
                                        for poly_data in gp.get("polygons", []) or []:
                                            pid = poly_data.get("poly_gid") or _get_poly_gid(poly_data)
                                            if pid:
                                                solid_member_ids.append(pid)
                                    else:
                                        pid = gp.get("poly_gid") or _get_poly_gid(gp)
                                        if pid:
                                            solid_member_ids.append(pid)

                    # Only write lod{N}Solid if we have something to reference.
                    if solid_member_ids:
                        lod_solid_tag = f"lod{solid_lod}Solid"
                        lod_solid = SubElement(feature, Q(ns_key, lod_solid_tag))

                        solid = SubElement(lod_solid, Q("gml", "Solid"), {GML_ID: f"UUID_{uuid4()}"})
                        ext = SubElement(solid, Q("gml", "exterior"))
                        comp = SubElement(ext, Q("gml", "CompositeSurface"), {GML_ID: f"UUID_{uuid4()}"})

                        for pid in solid_member_ids:
                            SubElement(comp, Q("gml", "surfaceMember"), {Q("xlink", "href"): f"#{pid}"})

                    # 2) Danach boundedBy-Surfaces (WallSurface/RoofSurface/...)
                    # ClosureSurfaces with subfeature metadata are split into
                    # inline Part/Installation features below.

                    for surf_type, polys in con_surfaces.items():
                        # Window/Door must never appear as standalone boundedBy
                        if surf_type in _OPENING_SURF_TYPES_V2:
                            continue
                        for gp in polys:
                            surface_lod = _surface_lod_with_openings(gp, lod_level, openings_by_parent)
                            fp = gp.get("filling_parent_sid") or ""
                            if fp and gp.get("opening_info"):
                                continue
                            if gp.get("is_multisurface", False):
                                surf_el, ms_parent = _begin_bs(
                                    feature,
                                    surf_type,
                                    gp.get("surf_id"),
                                    gp.get("ms_id"),
                                    gp.get("mat"),
                                    surface_lod,
                                )
                                for poly in gp.get("polygons", []):
                                    write_polygon_with_ring_ids(
                                        parent_ms=ms_parent,
                                        poly_gid=_get_poly_gid(poly),
                                        exterior_ring=poly["ext_xyz"],
                                        interior_rings=poly["int_xyz"] if poly["int_xyz"] else None,
                                        srs_name=srs_name,
                                        exterior_ring_id=poly["ext_id"],
                                        interior_ring_ids=poly["int_ids"] if poly["int_ids"] else None,
                                    )
                                _write_inline_surface_opening(surf_el, gp.get("polygons", []), surface_lod)
                            else:
                                surf_el, ms_parent = _begin_bs(
                                    feature,
                                    surf_type,
                                    gp.get("surf_id"),
                                    gp.get("ms_id"),
                                    gp.get("mat"),
                                    surface_lod,
                                )
                                write_polygon_with_ring_ids(
                                    parent_ms=ms_parent,
                                    poly_gid=gp["poly_gid"],
                                    exterior_ring=gp["ext_xyz"],
                                    interior_rings=gp["int_xyz"] if gp["int_xyz"] else None,
                                    srs_name=srs_name,
                                    exterior_ring_id=gp["ext_id"],
                                    interior_ring_ids=gp["int_ids"] if gp["int_ids"] else None,
                                )
                                _write_inline_surface_opening(surf_el, [gp], surface_lod)

                            parent_sid = gp.get("surf_id") or ""
                            for opening_gp in openings_by_parent.pop(parent_sid, []):
                                if opening_gp.get("is_multisurface", False):
                                    _write_inline_surface_opening(
                                        surf_el,
                                        opening_gp.get("polygons", []),
                                        surface_lod,
                                    )
                                else:
                                    _write_inline_surface_opening(
                                        surf_el,
                                        [opening_gp],
                                        surface_lod,
                                    )

                    _write_inline_part_features(feature, inline_part_groups, openings_by_parent, lod_level)

                    # Finalize: reorder Building/BuildingPart children to satisfy CityGML 2.0 XSD
                    if (ns_key, local_name) in (("bldg", "Building"), ("bldg", "BuildingPart")):
                        if getattr(bpy.context.scene, "cgml3", None) and bpy.context.scene.cgml3.export_debug:
                            try:
                                kids = list(feature)
                                def _lname(tag):
                                    if not isinstance(tag, str):
                                        return ""
                                    if tag.startswith("{") and "}" in tag:
                                        return tag.split("}", 1)[1]
                                    return tag.split(":", 1)[-1]
                                names = [_lname(getattr(k, "tag", "")) for k in kids]
                                bb = names.index("boundedBy") if "boundedBy" in names else -1
                                obi = names.index("outerBuildingInstallation") if "outerBuildingInstallation" in names else -1
                                print(f"[CityGML2][DIAG] final-before-reorder bldg feature={feature.get(GML_ID,'')} boundedBy_idx={bb} outerBuildingInstallation_idx={obi}")
                            except Exception:
                                pass

                        _reorder_children_for_xsd(feature, ns_prefix=ns_key)

                        if getattr(bpy.context.scene, "cgml3", None) and bpy.context.scene.cgml3.export_debug:
                            try:
                                kids = list(feature)
                                def _lname(tag):
                                    if not isinstance(tag, str):
                                        return ""
                                    if tag.startswith("{") and "}" in tag:
                                        return tag.split("}", 1)[1]
                                    return tag.split(":", 1)[-1]
                                names = [_lname(getattr(k, "tag", "")) for k in kids]
                                bb = names.index("boundedBy") if "boundedBy" in names else -1
                                obi = names.index("outerBuildingInstallation") if "outerBuildingInstallation" in names else -1
                                print(f"[CityGML2][DIAG] final-after-reorder bldg feature={feature.get(GML_ID,'')} boundedBy_idx={bb} outerBuildingInstallation_idx={obi}")
                            except Exception:
                                pass
                    
                    elif (ns_key, local_name) in (("tun", "Tunnel"), ("tun", "TunnelPart")):
                        _reorder_children_for_xsd(feature, ns_prefix=ns_key)

                    # Appearance-Zuordnung: pro Polygon die importierte Appearance-ID übernehmen
                    """ try:
                        app_id = poly.get("app_id", None) or obj.get("app_id", None) or obj.get("gml_id", None)
                    except Exception:
                        app_id = None
                    if not app_id:
                        app_id = f"ID_{uuid4().hex}"
                    app_id = str(app_id)

                    theme = ""
                    try:
                        theme = str(obj.get("app_theme", "")) if obj.get("app_theme", None) is not None else ""
                    except Exception:
                        theme = ""

                    grp = appearance_groups.get(app_id)
                    if grp is None:
                        appearance_groups[app_id] = grp = {"theme": theme, "ptx_by_image": {}, "x3d_by_rgba": {}}
                        appearance_order.append(app_id)
                    elif not grp.get("theme") and theme:
                        grp["theme"] = theme """


        # CityGML-spezifische Attribute exportieren
        #
        # Gold-Referenz/XSD: Für bldg:Building müssen Attribute wie yearOfConstruction/measuredHeight
        # VOR den boundedBy*-Surfaces stehen. Daher NICHT nach der Geometrie anhängen.
        #
        # Für veg:SolitaryVegetationObject werden veg:-Properties (inkl. height) bereits VOR der
        # Geometrie im speziellen Branch geschrieben.
        if not (
            (ns_key, local_name) == ("dem", "TINRelief")
            or (ns_key, local_name) == ("veg", "SolitaryVegetationObject")
            or (ns_key, local_name) in (("bldg", "Building"), ("bldg", "BuildingPart"))
        ):
            _write_specific_attributes(feature, obj)

        if local_name in _INSTALLATION_FEATURES_V2:
            _reorder_children_for_xsd(feature, ns_prefix=ns_key)

        obj_eval.to_mesh_clear()
        all_bbox_coords.extend(bbox_coords_abs)

    # Re-reorder parent features whose children were added after the parent's
    # own reorder pass (inline child objects processed later in the loop).
    if _parent_feature_elements:
        for _pid, _pfe in _parent_feature_elements.items():
            _pfe_tag = getattr(_pfe, "tag", "")
            if isinstance(_pfe_tag, str) and "}" in _pfe_tag:
                _pfe_local = _pfe_tag.split("}", 1)[1]
            else:
                _pfe_local = str(_pfe_tag).split(":", 1)[-1] if _pfe_tag else ""
            _pfe_ns = ""
            if _pfe_local in ("Building", "BuildingPart"):
                _pfe_ns = "bldg"
            elif _pfe_local in ("Bridge", "BridgePart"):
                _pfe_ns = "brid"
            elif _pfe_local in ("Tunnel", "TunnelPart"):
                _pfe_ns = "tun"
            if _pfe_ns:
                _reorder_children_for_xsd(_pfe, ns_prefix=_pfe_ns)

    # Appearance-Objekte als <app:appearanceMember> am Ende des CityModels exportieren
    # Ziel: mehrere Appearance je CityModel, gruppiert nach importierter app_id
    from .appearance import add_x3d_materials, add_parameterized_textures

    THEME_FALLBACK = "Rotterdam"

    # --- Appearance als <app:appearanceMember> am Ende des CityModels schreiben ---
    # Reihenfolge stabil halten (appearance_order)
    for app_id in appearance_order:
        grp = appearance_groups.get(app_id) or {}
        ptx_by_image_app = grp.get("ptx_by_image") or {}
        gtx_by_image_app = grp.get("gtx_by_image") or {}
        x3d_by_key_app = grp.get("x3d_by_key") or {}

        if not ptx_by_image_app and not gtx_by_image_app and not x3d_by_key_app:
            continue

        app_member = SubElement(root, Q("app", "appearanceMember"))
        app_gid = make_gml_id(f"APP_{app_id}", used_feat_ids)
        app_appearance = SubElement(app_member, Q("app", "Appearance"), {GML_ID: app_gid})

        theme = (grp.get("theme") or "").strip() or THEME_FALLBACK
        SubElement(app_appearance, Q("app", "theme")).text = theme

        # X3D: mehrere Bundles möglich (unterschiedliche params bei gleicher Farbe)
        # X3DMaterial (Materialfarben) schreiben
        if x3d_by_key_app:
            add_x3d_materials(app_appearance, x3d_by_key_app)
        """ for _k, b in x3d_by_key_app.items():
            add_x3d_materials(
                app_appearance,
                {b["rgba"]: {"poly_ids": b["poly_ids"], "params": b["params"]}},
                double_sided=False,
            ) """

        if ptx_by_image_app:
            # Last-mile uv_start injection for bit-identical roundtrip:
            # Some collection paths don't propagate uv_start into entries; read from ANY mesh map.
            try:
                global_uv = {}
                for me in getattr(bpy.data, "meshes", []):
                    try:
                        d = me.get("cgml3_uv_start_by_ring", None)
                    except Exception:
                        d = None
                    if not d:
                        continue
                    try:
                        for k in d.keys():
                            rid = _norm_id(k)
                            if not rid or rid in global_uv:
                                continue
                            uv0 = _as_uv_tuple2(d.get(k, None))
                            if uv0 is not None:
                                global_uv[rid] = uv0
                    except Exception:
                        continue

                if global_uv:
                    for _img, _entries in (ptx_by_image_app or {}).items():
                        if not isinstance(_entries, list):
                            continue
                        for e in _entries:
                            if not isinstance(e, dict):
                                continue
                            if e.get("uv_start", None) is not None:
                                continue
                            rid = _norm_id(e.get("ring_id", ""))
                            if not rid:
                                continue
                            uv0 = global_uv.get(rid, None)
                            if uv0 is not None:
                                e["uv_start"] = uv0
            except Exception:
                pass
            add_parameterized_textures(
                app_appearance,
                ptx_by_image_app,
                double_sided=False,
                processed_interior_ring_uvs=processed_interior_ring_uvs,
            )

        if gtx_by_image_app:
            add_georeferenced_textures(app_appearance, gtx_by_image_app)

    if all_bbox_coords:
        _srs_name_env, _srs_dim_env = select_envelope_srs_name(_epsg_num)
        add_bounded_by(root, all_bbox_coords, _epsg_num, int(_srs_dim_env))


    out_dir = os.path.dirname(os.path.abspath(filepath))
    os.makedirs(os.path.join(out_dir, "appearance"), exist_ok=True)

    # stabile Namespace-Präfixe erzwingen (core, gml, bldg, ...)
    register_export_namespaces()

    tree = ElementTree(root)

    # Pretty-print ohne Re-Serialisierung (behält Prefix-Mappings)
    try:
        ET.indent(tree, space="  ", level=0)  # Python >= 3.9
    except Exception:
        pass
    
    def _localname(tag: str) -> str:
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    def _prune_empty_lod_implicit(root_el: Element) -> None:
        for parent in root_el.iter():
            for child in list(parent):
                ln = _localname(child.tag)
                if ln.startswith("lod") and ln.endswith("ImplicitRepresentation"):
                    if len(child) == 0 and not (child.text or "").strip() and not child.attrib:
                        parent.remove(child)

    _prune_empty_lod_implicit(root)

    # Defensive cleanup for legacy outputs:
    # CityGML 2 Appearance schema does not declare an <app:ring> element.
    # Ring references must be encoded via the `ring` attribute on
    # app:textureCoordinates inside app:TexCoordList.
    try:
        for parent in root.iter():
            for child in list(parent):
                if getattr(child, "tag", None) == Q("app", "ring"):
                    parent.remove(child)
    except Exception:
        pass
    
    # Semantische Validierung (optional, bei aktiviertem Debug-Modus)
    validation_warnings = []
    try:
        scene = getattr(context, "scene", None)
        if scene and hasattr(scene, "cgml3") and getattr(scene.cgml3, "xsd_validate", False):
            # Importiere validate_semantic_rules aus io.writer.helpers (shared)
            # CityGML 2.0 nutzt dieselbe Validierungslogik
            import sys
            if "io.writer.helpers" in sys.modules:
                from io.writer.helpers import validate_semantic_rules
                validate_semantic_rules(root, validation_warnings)
                if validation_warnings:
                    print("[CityGML2] Semantische Validierungs-Warnungen:")
                    for warn in validation_warnings:
                        print(f"  - {warn}")
    except Exception as e:
        print(f"[CityGML2] Semantische Validierung fehlgeschlagen: {e}")
    
    tree.write(filepath, encoding="utf-8", xml_declaration=True)
