# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io/writer/exporter.py
# Unveränderte öffentliche Funktion. Nur Aufteilung und korrekte Imports.

from __future__ import annotations
from typing import Any, Dict, List, Tuple, Optional
import collections
import os
import re
import json
from collections import defaultdict, OrderedDict
from uuid import uuid4
from datetime import datetime, timezone
import bpy
from xml.etree.ElementTree import Element, SubElement, ElementTree

from .namespaces import NS, Q, GML_ID
from .helpers import ensure_xs_id
from .helpers import fmt, _ring_area_sign_xyz, pretty_xml, make_gml_id, _envelope_elem, resolve_feature_tag, _read_world_crs, _extract_epsg
from .geometry import write_polygon_with_ring_ids, write_compositesurface_with_polygons, format_poslist
from .appearance import add_x3d_materials, add_parameterized_textures, add_georeferenced_textures
from .materials import extract_base_color_rgba, first_image_path_from_material, extract_x3d_params_from_material
from .texio import _ensure_export_texture
from .document import create_citymodel_root, add_bounded_by, add_cityobject_member
from .curve_export import should_use_gml_curve, export_spline_as_curve_segment
from .grid_export import can_export_as_raster_relief, export_rectified_grid_coverage
import numpy as np
from .crs_transform import GeoTransformer
from ..reader.xml_utils import get_attr
from ...ops.export_face_autofill import prepare_new_face_export_overrides
from ...shared.export_helpers import object_is_viewport_visible

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

# CityGML 3 core is the default namespace on the document root. To match import__v3.gml,
# write core elements unprefixed (ElementTree will place them in the default namespace).
def _C(local: str) -> str:
    return local

def _new_gml_id(prefix: str = "id", used: set | None = None) -> str:
    """
    Generate an xs:ID-compatible identifier for gml:id.

    Requirements (xs:ID / NCName):
    - must start with a letter or underscore
    - must not contain ':' and spaces
    """
    if used is None:
        return f"{prefix}_{uuid4().hex}"
    return make_gml_id(f"{prefix}_{uuid4().hex}", used)

def _fmt_creation_date(val: object | None) -> str:
    """
    Match import__v3.gml style where possible (timezone offset, not forced Z).
    Accepts Blender custom property values (string/datetime/date).
    Converts CityGML 2 date-only format (YYYY-MM-DD) to CityGML 3 dateTime format (YYYY-MM-DDTHH:MM:SS+TZ).
    """
    if val is None:
        return ""
    if isinstance(val, str):
        s = val.strip()
        # Check if this is a date-only string (YYYY-MM-DD) without time component
        if re.match(r'^\d{4}-\d{2}-\d{2}$', s):
            # Convert to datetime with midnight and local timezone
            try:
                dt = datetime.strptime(s, "%Y-%m-%d")
                # Add local timezone info - replace() makes it timezone-aware with local offset
                dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
                # Format as CityGML 3 compliant dateTime with timezone offset
                return dt.isoformat()
            except (ValueError, AttributeError):
                pass  # If parsing fails, return as-is
        return s
    try:
        # datetime/date-like
        return str(val)
    except Exception:
        return ""

def _matrix_world_to_citygml_tm(mw, *, eps: float = 1e-12) -> list[float]:
    """
    Convert Blender 4x4 matrix to CityGML transformationMatrix (row-major).
    Translation should be handled via referencePoint; callers typically zero out mw[0..2][3].
    """
    try:
        rows: list[float] = []
        for r in range(4):
            for c in range(4):
                v = float(mw[r][c])
                if abs(v) < eps:
                    v = 0.0
                rows.append(v)
        return rows
    except Exception:
        return [
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        ]

def _matrix_from_object_no_translation(obj) -> list[list[float]]:
    """
    Build a 4x4 matrix for CityGML transformationMatrix from the Blender object.
    Uses rotation + scale (object-level), but forces translation to 0 because CityGML
    uses referencePoint for translation in implicit geometries.
    Returns a nested python list (4x4) compatible with _matrix_world_to_citygml_tm.
    """
    try:
        from math import cos, sin
        sx, sy, sz = (float(obj.scale[0]), float(obj.scale[1]), float(obj.scale[2]))
        rx, ry, rz = (float(obj.rotation_euler[0]), float(obj.rotation_euler[1]), float(obj.rotation_euler[2]))

        cx, cy, cz = cos(rx), cos(ry), cos(rz)
        sxn, syn, szn = sin(rx), sin(ry), sin(rz)

        # Blender's default Euler order is XYZ.
        # R = Rz * Ry * Rx (applied to column vectors); build as row-major.
        r00 = cz * cy
        r01 = cz * syn * sxn - sz * cx
        r02 = cz * syn * cx + sz * sxn

        r10 = sz * cy
        r11 = sz * syn * sxn + cz * cx
        r12 = sz * syn * cx - cz * sxn

        r20 = -syn
        r21 = cy * sxn
        r22 = cy * cx

        # Apply scale on columns => row-major multiply by diag(s) on the right.
        return [
            [r00 * sx, r01 * sy, r02 * sz, 0.0],
            [r10 * sx, r11 * sy, r12 * sz, 0.0],
            [r20 * sx, r21 * sy, r22 * sz, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
    except Exception:
        return [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]

def _matrix_from_matrix_world_no_translation(obj) -> list[list[float]]:
    """
    Build a 4x4 matrix for CityGML transformationMatrix from obj.matrix_world (so it reflects
    applied Dimensions/constraints), but force translation to 0 because translation is
    represented via referencePoint in implicit geometries.
    """
    try:
        mw = obj.matrix_world.copy()
        mw[0][3] = 0.0
        mw[1][3] = 0.0
        mw[2][3] = 0.0
        return [[float(mw[r][c]) for c in range(4)] for r in range(4)]
    except Exception:
        return _matrix_from_object_no_translation(obj)

def _transform_xyz_by_inv_no_translation(obj, xyz: tuple[float, float, float]) -> tuple[float, float, float]:
    """
    Transform a vector by inverse(obj.matrix_world with translation removed).
    Used to export prototype template geometry in local coordinates so that scale/rotation
    is not applied twice (once in vertices, once in transformationMatrix).
    """
    try:
        from mathutils import Vector
        mw = obj.matrix_world.copy()
        mw[0][3] = 0.0
        mw[1][3] = 0.0
        mw[2][3] = 0.0
        inv = mw.inverted()
        x, y, z = xyz
        v = inv @ Vector((float(x), float(y), float(z), 1.0))
        return (float(v[0]), float(v[1]), float(v[2]))
    except Exception:
        x, y, z = xyz
        return (float(x), float(y), float(z))

SPECIFIC_ATTR_KEYS = {
    # Building
    "bldg:class",
    "bldg:function",
    "bldg:usage",
    "bldg:roofType",
    "bldg:storeyHeightsAboveGround",
    "bldg:storeyHeightsBelowGround",
    "bldg:storeysAboveGround",
    "bldg:storeysBelowGround",
    "bldg:sortKey",
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

    # Bridge
    "brid:class",
    "brid:function",
    "brid:usage",
    "brid:isMovable",
    
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
    "veg:trunkDiameter",
    "veg:crownDiameter",
    
    # LandUse
    "luse:class",
    "luse:function",
    "luse:usage",
    
    # CityFurniture
    "frn:class",
    "frn:function",
    "frn:usage",

    # Generics
    "gen:class",
    "gen:function",
    "gen:usage",

    # CityObjectGroup
    "grp:class",
    "grp:function",
    "grp:usage",

    # Construction Module (Window, Door)
    "con:class",
    "con:function",
    "con:usage",

    # core:AbstractFeatureWithLifespan
    "core:creationDate",
    "core:terminationDate",
    "core:validFrom",
    "core:validTo",

    # Construction / Structural
    "con:isStructuralElement",
    "con:relationToConstruction",
    "relationToConstruction",
    "con:height:value",
    "con:height:highReference",
    "con:height:lowReference",
    "con:height:status",
    "con:height:uom",
    "con:dateOfConstruction",
    "con:dateOfDemolition",
    
    # Relief / Digital Elevation Model
    "dem:lod",
    "dem:extent",
}

INTERNAL_ATTR_KEYS = {
    "cgml3_feature",
    "gml_id",
    "cgml3_implicit_template_id",
    "structure_type",
    "structure_part",
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
    "relationToConstruction",
    "con:relationToConstruction",
    "is_multisurface_member",
    "is_compositesurface_member",
    "Interior",
    "ExteriorPolyId",
    "ExteriorRingId",
    "cgml3_uv_sign",
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
    split_wall_roof_surfaces: bool = False,
    write_lod_solid_refs: bool = False,
    export_types: Optional[List[str]] = None,
):
    export_debug = False
    try:
        export_debug = bool(getattr(getattr(context, "scene", None), "cgml3", None) and context.scene.cgml3.export_debug)
    except Exception:
        export_debug = False
    processed_interior_ring_uvs: Dict[str, Dict[str, object]] = {}
    queued_ptx_ring_counts: Dict[str, int] = {}
    default_unclassified_surface_type = str(unclassified_surface_type or "WallSurface").strip() or "WallSurface"

    def _load_json_chunks(owner, base_key: str) -> dict | None:
        try:
            direct = owner.get(base_key, None)
        except Exception:
            direct = None

        txt = None
        if isinstance(direct, str) and direct.strip():
            txt = direct
        else:
            chunks: list[str] = []
            try:
                keys = list(getattr(owner, "keys", lambda: [])())
            except Exception:
                keys = []
            for k in sorted([k for k in keys if isinstance(k, str) and k.startswith(base_key + "_")]):
                try:
                    part = owner.get(k, "")
                except Exception:
                    part = ""
                if isinstance(part, str):
                    chunks.append(part)
            if chunks:
                txt = "".join(chunks)

        if not txt:
            return None
        try:
            data = json.loads(txt)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    # bevorzugt: übergebener srs_name -> World["CRS"] -> Fallback 25832
    _epsg_num = _extract_epsg(srs_name) or _extract_epsg(_read_world_crs()) or "25832"
    effective_srs_name = str(srs_name or _read_world_crs() or f"EPSG:{_epsg_num}").strip()
    if not effective_srs_name or effective_srs_name == "Unknown CRS":
        effective_srs_name = f"EPSG:{_epsg_num}"
    root = create_citymodel_root(_epsg_num)

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
    used_gml_ids = set()

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

    def _alloc_gml_id(prefix: str = "ID") -> str:
        return make_gml_id(f"{prefix}_{uuid4().hex}", used_gml_ids)
    
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
        """Ensures the ring is closed even if the last vertex is numerically very close to the first.

        Some imports keep rings topologically closed but the last coordinate may differ by tiny
        floating-point noise. CityGML exporters typically repeat the first coordinate at the end.
        """
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

    def _order_uvs_for_loop_vertices(mesh_, loop_indices, ring_xyz, ring_uvs, tol=1e-6, world_matrix=None):
        if not ring_uvs or not loop_indices:
            return ring_uvs

        def _eq3(a, b):
            return (
                abs(a[0] - b[0]) <= tol
                and abs(a[1] - b[1]) <= tol
                and abs(a[2] - b[2]) <= tol
            )

        ring_xyz_open = list(ring_xyz or [])
        ring_uvs_open = list(ring_uvs or [])
        if len(ring_xyz_open) >= 2 and _eq3(ring_xyz_open[0], ring_xyz_open[-1]):
            ring_xyz_open = ring_xyz_open[:-1]
        if len(ring_uvs_open) >= 2 and ring_uvs_open[0] == ring_uvs_open[-1]:
            ring_uvs_open = ring_uvs_open[:-1]

        if not ring_xyz_open or not ring_uvs_open or len(ring_xyz_open) != len(ring_uvs_open):
            return ring_uvs

        used = set()
        loop_order = []
        for li in loop_indices:
            vi = mesh_.loops[li].vertex_index
            vco = mesh_.vertices[vi].co
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

    def _as_uv_tuple2(v):
        try:
            if v is None:
                return None
            if hasattr(v, "__len__") and len(v) >= 2 and not isinstance(v, (str, bytes)):
                return (float(v[0]), float(v[1]))
        except Exception:
            return None
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

    def _orient_uv_ring_to_expected_sign(uvs, expected_sign: int | None):
        values = [_as_uv_tuple2(uv) for uv in list(uvs or [])]
        values = [uv for uv in values if uv is not None]
        if not values or expected_sign not in (-1, 1):
            return values, False
        ring_sign = _uv_ring_sign(values)
        if ring_sign != 0 and ring_sign != expected_sign:
            return _reverse_uv_ring(values), True
        return values, False

    def _norm_id(v):
        s = str(v or "").strip()
        if s.startswith("#"):
            s = s[1:]
        return s

    def _stored_uv_ring_sign(obj_, mat_, ring_id: str) -> int | None:
        rid = _norm_id(ring_id)
        if not rid:
            return None

        raw_sign = None
        try:
            d = None
            if obj_ is not None:
                d = obj_.get("cgml3_uv_sign_by_ring", None)
            if d is None and obj_ is not None and getattr(obj_, "data", None) is not None:
                d = obj_.data.get("cgml3_uv_sign_by_ring", None)
            if d is not None:
                raw_sign = d.get(rid, None)
        except Exception:
            raw_sign = None

        if raw_sign is None:
            try:
                raw_sign = mat_.get("cgml3_uv_sign", None) if mat_ else None
                rid_mat = _norm_id(mat_.get("gml_ring_id", "")) if mat_ else ""
                if not (raw_sign is not None and rid_mat and rid_mat == rid):
                    raw_sign = None
            except Exception:
                raw_sign = None

        try:
            sign = int(raw_sign)
        except Exception:
            return None
        return sign if sign in (-1, 1) else None

    def _expected_uv_ring_sign(obj_, mat_, ring_id: str, uvs=None) -> int | None:
        stored_sign = _stored_uv_ring_sign(obj_, mat_, ring_id)
        if stored_sign in (-1, 1):
            return stored_sign

        exterior_ring_id = ""
        try:
            exterior_ring_id = _norm_id(_first_nonempty_prop((mat_,), ("ExteriorRingId",)))
        except Exception:
            exterior_ring_id = ""

        if exterior_ring_id:
            exterior_sign = _stored_uv_ring_sign(obj_, None, exterior_ring_id)
            if exterior_sign in (-1, 1):
                return -exterior_sign

        ring_sign = _uv_ring_sign(uvs)
        return ring_sign if ring_sign in (-1, 1) else None

    _descendant_feature_poly_id_cache: Dict[int, set[str]] = {}

    def _collect_descendant_feature_polygon_ids(parent_obj) -> set[str]:
        if parent_obj is None:
            return set()

        cache_key = id(parent_obj)
        cached = _descendant_feature_poly_id_cache.get(cache_key)
        if cached is not None:
            return set(cached)

        collected: set[str] = set()

        try:
            descendants = list(getattr(parent_obj, "children_recursive", []) or [])
        except Exception:
            descendants = []

        if not descendants:
            pending = list(getattr(parent_obj, "children", []) or [])
            while pending:
                child = pending.pop(0)
                descendants.append(child)
                try:
                    pending.extend(list(getattr(child, "children", []) or []))
                except Exception:
                    pass

        for child in descendants:
            if not object_is_viewport_visible(child, context):
                continue
            if getattr(child, "type", None) != "MESH":
                continue

            try:
                structure_part = str(child.get("structure_part") or "").strip()
            except Exception:
                structure_part = ""
            try:
                feature_name = str(child.get("cgml3_feature") or "").strip()
            except Exception:
                feature_name = ""

            parent_part = ""
            try:
                parent_part = str(getattr(child, "parent", None).get("structure_part") or "").strip() if getattr(child, "parent", None) else ""
            except Exception:
                parent_part = ""

            is_subfeature_mesh = (
                structure_part in {"storey", "room_geometry"}
                or feature_name in {"Storey", "BuildingRoom"}
                or parent_part == "room"
            )
            if not is_subfeature_mesh or structure_part == "outer_shell":
                continue

            for slot in getattr(child, "material_slots", []) or []:
                mat_child = getattr(slot, "material", None)
                if not mat_child:
                    continue
                try:
                    pid = _norm_id(mat_child.get("gml_polygon_id", None))
                except Exception:
                    pid = ""
                if pid:
                    collected.add(pid)

            mesh_child = getattr(child, "data", None)
            if mesh_child is not None and hasattr(mesh_child, "polygons"):
                for poly_child in mesh_child.polygons:
                    try:
                        pid = _norm_id(poly_child.get("gml_id", None))
                    except Exception:
                        pid = ""
                    if pid:
                        collected.add(pid)

        _descendant_feature_poly_id_cache[cache_key] = set(collected)
        return set(collected)

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
                val = None

        # 3) Fallback: Default "Building"
        if not val:
            val = "Building"

        ns_key, local_name = resolve_feature_tag(val)
        return ns_key, local_name

    def _dbg(msg: str) -> None:
        if export_debug:
            print(f"[CityGML3 Export][DEBUG] {msg}")

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
        "BridgeConstructiveElement",
        "BridgeInstallation",
        "BridgeFurniture",
        "BridgePart",
        "Bridge",
        "TunnelConstructiveElement",
        "TunnelInstallation",
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
        "LandUse",
        "GenericThematicSurface",
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

    def _normalize_filling_surface_type(raw_surface_type: object | None, opening_type: str) -> str:
        raw = str(raw_surface_type or "").strip()
        if raw in ("WindowSurface", "DoorSurface"):
            return raw
        if opening_type == "Window":
            return "WindowSurface"
        if opening_type == "Door":
            return "DoorSurface"
        return ""

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
        filling_surface_type = _normalize_filling_surface_type(raw_surface_type, opening_type)
        if filling_surface_type not in ("WindowSurface", "DoorSurface"):
            return None

        return {
            "opening_type": opening_type,
            "filling_surface_type": filling_surface_type,
            "opening_id": opening_id,
            "opening_name": opening_name,
        }

    def _explicit_face_identity(poly_, mat_) -> tuple[str, str]:
        poly_gid = ""
        try:
            gid_raw = poly_.get("gml_id", None)
        except Exception:
            gid_raw = None
        if gid_raw:
            poly_gid = str(gid_raw).strip()
        if not poly_gid and mat_:
            try:
                m_gid = mat_.get("gml_polygon_id", None)
            except Exception:
                m_gid = None
            if m_gid:
                poly_gid = str(m_gid).strip()

        ring_id = ""
        if mat_:
            try:
                mrid = mat_.get("gml_ring_id", None)
            except Exception:
                mrid = None
            if mrid:
                ring_id = str(mrid).strip()
        return poly_gid, ring_id

    def _duplicate_face_semantic_score(mat_, face_override_) -> int:
        score = 0
        sources = tuple(src for src in (face_override_, mat_) if src is not None)
        surface_type = _normalize_face_surface_type(
            _first_nonempty_prop(sources, ("surface_type", "SurfaceTyp", "Typ"))
        )
        if surface_type:
            score += 2
        if surface_type in {
            "RoofSurface",
            "WallSurface",
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
            score += 4
        try:
            if mat_ and bool(mat_.get("Interior", False)):
                score += 40
        except Exception:
            pass
        if _first_nonempty_prop(sources, ("ExteriorPolyId",)):
            score += 30
        if _first_nonempty_prop(sources, ("con_surface_id",)):
            score += 10
        if _first_nonempty_prop(sources, ("gml_multisurface_id",)):
            score += 6
        if _first_nonempty_prop(sources, ("filling_parent_surface_id", "opening_surface_id")):
            score += 6
        if _first_nonempty_prop(sources, ("opening_type", "OpeningType", "opening_gml_id", "opening_id")):
            score += 6
        return score

    def _ln(tag: str) -> str:
        try:
            return tag.rsplit("}", 1)[-1]
        except Exception:
            return str(tag)
    
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
                dt_str = _to_xs_datetime(val)
                if dt_str:
                    # Match import__v3.gml: write core elements unprefixed (core is default NS).
                    el = SubElement(feature_el, _C(tag))
                    # Prefer a user-provided string (may include timezone offset), else fallback to normalized value.
                    el.text = _fmt_creation_date(val) or dt_str

    def _write_core(feature_el, obj_):
        """
        Write generic GML core metadata that must appear before lifespan/module content.
        Currently used by feature branches that create no geometry and therefore bypass the
        standard feature-writing flow.
        """
        existing_direct_children = {
            child.tag.rsplit("}", 1)[-1] if "}" in child.tag else str(child.tag)
            for child in list(feature_el)
        }

        desc = obj_.get("gml:description")
        if desc is None:
            desc = obj_.get("description")
        if "description" not in existing_direct_children and desc is not None:
            text = str(desc).strip()
            if text:
                SubElement(feature_el, Q("gml", "description")).text = text

        desc_ref = obj_.get("gml:descriptionReference")
        if desc_ref is None:
            desc_ref = obj_.get("descriptionReference")
        if "descriptionReference" not in existing_direct_children and desc_ref is not None:
            text = str(desc_ref).strip()
            if text:
                el = SubElement(feature_el, Q("gml", "descriptionReference"))
                el.set(Q("xlink", "href"), text)

        identifier = obj_.get("gml:identifier")
        if identifier is None:
            identifier = obj_.get("identifier")
        if "identifier" not in existing_direct_children and identifier is not None:
            text = str(identifier).strip()
            if text:
                el = SubElement(feature_el, Q("gml", "identifier"))
                code_space = obj_.get("gml:identifier:codeSpace")
                if code_space is None:
                    code_space = obj_.get("identifier:codeSpace")
                if code_space:
                    el.set("codeSpace", str(code_space).strip())
                el.text = text

        if "name" not in existing_direct_children:
            feat_id = _feature_id(obj_)
            name_val = obj_.get("gml:name")
            if name_val is None and obj_.name and obj_.name != feat_id:
                name_val = obj_.name
            if name_val is not None:
                text = str(name_val).strip()
                if text and text != feat_id:
                    SubElement(feature_el, Q("gml", "name")).text = text

    def _write_lifespan_attributes(feature_el, obj_):
        """Backward-compatible alias for older export branches."""
        _write_lifespan_core(feature_el, obj_)

    def _write_address(feature_el, obj_, ns_key="bldg"):
        """
        Write a structured xAL address once if `bldg:address:*` custom properties exist.
        Several newer export branches still call this helper directly.
        """
        if ns_key != "bldg":
            return

        existing_direct_children = {
            child.tag.rsplit("}", 1)[-1] if "}" in child.tag else str(child.tag)
            for child in list(feature_el)
        }
        if "address" in existing_direct_children:
            return

        addr_data = {}
        point_coords = {}
        try:
            keys_iter = list(obj_.keys())
        except Exception:
            keys_iter = []

        for key in keys_iter:
            key_str = str(key)
            if not key_str.startswith("bldg:address:"):
                continue

            field = key_str.replace("bldg:address:", "", 1)
            try:
                value = obj_[key]
            except Exception:
                value = obj_.get(key, None)

            if field.startswith("point:"):
                axis = field.split(":")[-1]
                point_coords[axis] = value
            else:
                addr_data[field] = value

        has_addr_data = any(v is not None and str(v).strip() for v in addr_data.values())
        has_point = all(
            axis in point_coords and point_coords[axis] is not None and str(point_coords[axis]).strip()
            for axis in ("x", "y", "z")
        )
        if not (has_addr_data or has_point):
            return

        from ...shared.xal_writer import write_xal_address

        addr_rel = SubElement(feature_el, Q("bldg", "address"))
        addr = SubElement(addr_rel, _C("Address"))
        xal_addr = SubElement(addr, _C("xalAddress"))
        xal_root = SubElement(xal_addr, Q("xAL", "Address"))

        write_xal_address(xal_root, addr_data, version="3.0", Q_func=Q, SubElement_func=SubElement)

        if has_point:
            mp_el = SubElement(addr, _C("multiPoint"))
            gml_mp = SubElement(mp_el, Q("gml", "MultiPoint"))
            pm = SubElement(gml_mp, Q("gml", "pointMember"))
            pt = SubElement(pm, Q("gml", "Point"))
            pos = SubElement(pt, Q("gml", "pos"))
            pos.set("srsDimension", "3")
            pos.text = f"{fmt(point_coords['x'])} {fmt(point_coords['y'])} {fmt(point_coords['z'])}"

    def _write_generic_attributes(feature_el, obj_):
        """Backward-compatible alias for branches renamed to `_write_custom_attributes`."""
        _write_custom_attributes(feature_el, obj_, None)
    
    def _write_custom_attributes(feature_el, obj_, ns_key):
        """
        Writes generic attributes (genericAttribute) to the feature element.
        Specific namespace attributes (bldg:function, etc.) are handled by
        _write_specific_attributes to ensure correct XSD element ordering.
        """
        # Prefer lossless roundtrip dict from importer if present; fallback to JSON chunks.
        raw_generic = None
        try:
            raw_generic = obj_.get("cgml3_generic_attributes", None)
        except Exception:
            raw_generic = None
        if not (isinstance(raw_generic, dict) and raw_generic):
            raw_generic = _load_json_chunks(obj_, "cgml3_generic_attributes_json") or raw_generic

        if isinstance(raw_generic, dict) and raw_generic:
            items_iter = list(raw_generic.items())
        else:
            items_iter = list(getattr(obj_, "items", lambda: [])())

        # Write generic attributes
        for k, v in items_iter:
            # Skip internal and specific keys
            if k in INTERNAL_ATTR_KEYS or k in SPECIFIC_ATTR_KEYS:
                continue
            if _should_skip_generic_attr_key(k):
                continue
            if str(k).startswith("_"):
                # For raw dict roundtrip we *do* want underscore keys (e.g. "_lod").
                if not (isinstance(raw_generic, dict) and raw_generic):
                    continue

            # Skip empty values (import__v3.gml typically omits empty genericAttribute blocks)
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue

            # CityGML3 typing: ISO Date / DateTime
            # - gen:DateAttribute expects YYYY-MM-DD (date)
            # - gen:DateTimeAttribute expects ISO 8601 date-time
            v_str = None
            if isinstance(v, str):
                v_str = v.strip()
            
            try:
                ga = SubElement(feature_el, _C("genericAttribute"))
                
                # String → gen:StringAttribute
                if isinstance(v, str):
                    # DateTime first (more specific)
                    if v_str and re.match(r"^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}(:\\d{2}(?:\\.\\d+)?)?(Z|[+-]\\d{2}:\\d{2})?$", v_str):
                        a = SubElement(ga, Q("gen", "DateTimeAttribute"))
                        SubElement(a, Q("gen", "name")).text = str(k)
                        SubElement(a, Q("gen", "value")).text = v_str
                    elif v_str and re.match(r"^\\d{4}-\\d{2}-\\d{2}$", v_str):
                        a = SubElement(ga, Q("gen", "DateAttribute"))
                        SubElement(a, Q("gen", "name")).text = str(k)
                        SubElement(a, Q("gen", "value")).text = v_str
                    else:
                        a = SubElement(ga, Q("gen", "StringAttribute"))
                        SubElement(a, Q("gen", "name")).text = str(k)
                        SubElement(a, Q("gen", "value")).text = v
                
                # bool/int → gen:IntAttribute
                elif isinstance(v, bool):
                    a = SubElement(ga, Q("gen", "IntAttribute"))
                    SubElement(a, Q("gen", "name")).text = str(k)
                    SubElement(a, Q("gen", "value")).text = "1" if v else "0"
                
                elif isinstance(v, int):
                    a = SubElement(ga, Q("gen", "IntAttribute"))
                    SubElement(a, Q("gen", "name")).text = str(k)
                    SubElement(a, Q("gen", "value")).text = str(v)
                
                # float → gen:DoubleAttribute
                elif isinstance(v, (float,)):
                    a = SubElement(ga, Q("gen", "DoubleAttribute"))
                    SubElement(a, Q("gen", "name")).text = str(k)
                    SubElement(a, Q("gen", "value")).text = fmt(v)
                
                # Fallback: everything else as StringAttribute
                else:
                    a = SubElement(ga, Q("gen", "StringAttribute"))
                    SubElement(a, Q("gen", "name")).text = str(k)
                    SubElement(a, Q("gen", "value")).text = str(v)
            
            except Exception:
                continue
    
    # ── XSD element ordering for CityGML 3.0 post-processing ──
    # Position numbers reflect the flattened sequence from the XSD inheritance chain.
    # Lower numbers come first; elements not listed default to 9999 (preserving relative order).
    _XSD_CHILD_ORDER = {
        # gml:AbstractFeature / gml:AbstractGML
        "description": 1, "descriptionReference": 2, "identifier": 3,
        "name": 4, "boundedBy": 5,
        # core:AbstractFeatureWithLifespan
        "creationDate": 10, "terminationDate": 11, "validFrom": 12, "validTo": 13,
        # core:AbstractCityObject
        "externalReference": 20, "generalizesTo": 21,
        "relativeToTerrain": 22, "relativeToWater": 23, "relatedTo": 24,
        "appearance": 25, "genericAttribute": 26, "dynamizer": 27,
        "adeOfAbstractCityObject": 28,
        # core:AbstractSpace
        "spaceType": 30, "volume": 31, "area": 32,
        "boundary": 33,
        "lod0Point": 40, "lod0MultiSurface": 41, "lod0MultiCurve": 42,
        "lod1Solid": 43,
        "lod2Solid": 44, "lod2MultiSurface": 45, "lod2MultiCurve": 46,
        "lod3Solid": 47, "lod3MultiSurface": 48, "lod3MultiCurve": 49,
        "adeOfAbstractSpace": 50,
        # core:AbstractPhysicalSpace
        "adeOfAbstractPhysicalSpace": 51,
        # core:AbstractOccupiedSpace
        "lod1ImplicitRepresentation": 55, "lod2ImplicitRepresentation": 56,
        "lod3ImplicitRepresentation": 57, "adeOfAbstractOccupiedSpace": 58,
        # con:AbstractConstruction
        "conditionOfConstruction": 60, "dateOfConstruction": 61,
        "dateOfDemolition": 62, "constructionEvent": 63,
        "elevation": 64, "height": 65, "occupancy": 66,
        "adeOfAbstractConstruction": 67,
        # bldg:AbstractBuilding  (Building/BuildingPart)
        "class": 70, "function": 71, "usage": 72, "roofType": 73,
        "storeysAboveGround": 74, "storeysBelowGround": 75,
        "storeyHeightsAboveGround": 76, "storeyHeightsBelowGround": 77,
        "buildingConstructiveElement": 78,
        "buildingInstallation": 79, "buildingRoom": 80,
        "buildingFurniture": 81, "buildingSubdivision": 82,
        "address": 83, "adeOfAbstractBuilding": 84,
        # bldg:Building
        "buildingPart": 85, "adeOfBuilding": 86,
        # brid:AbstractBridge / tun:AbstractTunnel
        "bridgeConstructiveElement": 78,
        "bridgeInstallation": 79,
        "bridgeRoom": 80,
        "bridgeFurniture": 81,
        "adeOfAbstractBridge": 84,
        "bridgePart": 85,
        "adeOfBridge": 86,
        "tunnelConstructiveElement": 78,
        "tunnelInstallation": 79,
        "hollowSpace": 80,
        "tunnelFurniture": 81,
        "adeOfAbstractTunnel": 84,
        "tunnelPart": 85,
        "adeOfTunnel": 86,
        # bldg:Storey (AbstractBuildingSubdivision)
        "sortKey": 73, "roomHeight": 73,
        # dem:TINRelief
        "tin": 90,
    }
    # BuildingRoom overrides: buildingFurniture comes BEFORE buildingInstallation
    _XSD_ROOM_OVERRIDES = {"buildingFurniture": 79, "buildingInstallation": 80}
    _XSD_STOREY_OVERRIDES = {
        ("bldg", "class"): 70,
        ("bldg", "function"): 71,
        ("bldg", "usage"): 72,
        ("bldg", "elevation"): 73,
        ("bldg", "sortKey"): 74,
        ("bldg", "buildingConstructiveElement"): 78,
        ("bldg", "buildingFurniture"): 79,
        ("bldg", "buildingInstallation"): 80,
        ("bldg", "buildingRoom"): 81,
        ("bldg", "adeOfAbstractBuildingSubdivision"): 82,
        ("bldg", "buildingUnit"): 83,
        ("bldg", "adeOfStorey"): 84,
    }

    def _xsd_sort_key(child_el, feature_local="", is_room=False):
        tag = getattr(child_el, "tag", "") or ""
        ns_uri = ""
        local = tag
        if "}" in tag:
            ns_uri, local = tag[1:].split("}", 1)
        order = _XSD_CHILD_ORDER.get(local, 9999)
        if is_room and local in _XSD_ROOM_OVERRIDES:
            order = _XSD_ROOM_OVERRIDES[local]
        if feature_local == "Storey":
            ns_key = None
            for _k, _uri in NS.items():
                if _uri == ns_uri:
                    ns_key = _k
                    break
            if ns_key is not None:
                order = _XSD_STOREY_OVERRIDES.get((ns_key, local), order)
        return order

    def _reorder_feature_children(feat_el, is_room=False):
        """Sort direct children of a feature element according to XSD sequence (stable sort)."""
        feat_tag = getattr(feat_el, "tag", "") or ""
        feat_local = feat_tag.split("}")[-1] if "}" in feat_tag else feat_tag
        children = list(feat_el)
        sorted_children = sorted(children, key=lambda c: _xsd_sort_key(c, feat_local, is_room))
        # Only rearrange if order changed
        if any(a is not b for a, b in zip(children, sorted_children)):
            for c in children:
                feat_el.remove(c)
            for c in sorted_children:
                feat_el.append(c)

    def _write_specific_attributes(feature_el, obj_):
        # WICHTIG: Element-Reihenfolge muss XSD entsprechen!
        # AbstractConstruction sequence: conditionOfConstruction, dateOfConstruction, dateOfDemolition,
        # constructionEvent, elevation, height, occupancy, adeOfAbstractConstruction
        # DANN erst Building-spezifische Elemente
        
        # con:dateOfConstruction and con:dateOfDemolition (BEFORE con:height!)
        date_construction = obj_.get("con:dateOfConstruction", None)
        if date_construction is not None:
            text = str(date_construction).strip()
            if text:
                SubElement(feature_el, Q("con", "dateOfConstruction")).text = text
        
        date_demolition = obj_.get("con:dateOfDemolition", None)
        if date_demolition is not None:
            text = str(date_demolition).strip()
            if text:
                SubElement(feature_el, Q("con", "dateOfDemolition")).text = text
        
        # con:height Structure. Preserve explicitly imported empty containers
        # so a roundtrip does not silently drop existing con:Height nodes.
        has_height_structure = any(
            key in obj_
            for key in (
                "con:height",
                "con:height:value",
                "con:height:highReference",
                "con:height:lowReference",
                "con:height:status",
                "con:height:uom",
            )
        )
        if has_height_structure:
            height_el = SubElement(feature_el, Q("con", "height"))
            height_inner = SubElement(height_el, Q("con", "Height"))
            height_val = obj_.get("con:height:value", None)
            
            # highReference
            high_ref = obj_.get("con:height:highReference", None)
            if high_ref is not None:
                high_ref_el = SubElement(height_inner, Q("con", "highReference"))
                text = str(high_ref).strip()
                if text:
                    high_ref_el.text = text
            
            # lowReference
            low_ref = obj_.get("con:height:lowReference", None)
            if low_ref is not None:
                low_ref_el = SubElement(height_inner, Q("con", "lowReference"))
                text = str(low_ref).strip()
                if text:
                    low_ref_el.text = text
            
            # status
            status = obj_.get("con:height:status", None)
            if status is not None:
                status_el = SubElement(height_inner, Q("con", "status"))
                text = str(status).strip()
                if text:
                    status_el.text = text
            
            # value with uom
            if height_val is not None:
                val_el = SubElement(height_inner, Q("con", "value"))
                text = str(height_val).strip()
                if text:
                    val_el.text = text
                uom = obj_.get("con:height:uom", "m")
                if uom is not None:
                    uom_text = str(uom).strip()
                    if uom_text:
                        val_el.set("uom", uom_text)

        # WICHTIG: Element-Reihenfolge entsprechend XSD!
        # dateOfConstruction/dateOfDemolition wurden VOR con:height verschoben (siehe oben vor con:height Block)
        # Dieser Block wurde entfernt, da die Elemente jetzt in korrekter Reihenfolge oben stehen

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

        # Transportation (tran:) attributes differ significantly by feature type in CityGML 3.0.
        transport_attr_specs = {
            "Road": (
                ("tran:class", "class"),
                ("tran:function", "function"),
                ("tran:usage", "usage"),
                ("tran:trafficDirection", "trafficDirection"),
            ),
            "Railway": (
                ("tran:class", "class"),
                ("tran:function", "function"),
                ("tran:usage", "usage"),
                ("tran:trafficDirection", "trafficDirection"),
            ),
            "Track": (
                ("tran:class", "class"),
                ("tran:function", "function"),
                ("tran:usage", "usage"),
                ("tran:trafficDirection", "trafficDirection"),
            ),
            "Square": (
                ("tran:class", "class"),
                ("tran:function", "function"),
                ("tran:usage", "usage"),
                ("tran:trafficDirection", "trafficDirection"),
            ),
            "Waterway": (
                ("tran:class", "class"),
                ("tran:function", "function"),
                ("tran:usage", "usage"),
                ("tran:trafficDirection", "trafficDirection"),
            ),
            "Section": (
                ("tran:trafficDirection", "trafficDirection"),
            ),
            "Intersection": (
                ("tran:trafficDirection", "trafficDirection"),
            ),
            "TrafficSpace": (
                ("tran:class", "class"),
                ("tran:function", "function"),
                ("tran:usage", "usage"),
                ("tran:granularity", "granularity"),
                ("tran:trafficDirection", "trafficDirection"),
            ),
            "AuxiliaryTrafficSpace": (
                ("tran:class", "class"),
                ("tran:function", "function"),
                ("tran:usage", "usage"),
                ("tran:granularity", "granularity"),
            ),
            "TrafficArea": (
                ("tran:class", "class"),
                ("tran:function", "function"),
                ("tran:usage", "usage"),
                ("tran:surfaceMaterial", "surfaceMaterial"),
            ),
            "AuxiliaryTrafficArea": (
                ("tran:class", "class"),
                ("tran:function", "function"),
                ("tran:usage", "usage"),
                ("tran:surfaceMaterial", "surfaceMaterial"),
            ),
            "ClearanceSpace": (
                ("tran:class", "class"),
            ),
            "Marking": (
                ("tran:class", "class"),
            ),
            "Hole": (
                ("tran:class", "class"),
            ),
        }
        for key, el_name in transport_attr_specs.get(local_name, ()):
            val = obj_.get(key, None)
            if val is not None:
                for v in _iter_attr_values(val):
                    text = str(v).strip()
                    if text:
                        SubElement(feature_el, Q("tran", el_name)).text = text

        # Bridge (brid:) - nur für Bridge-Features
        if ns_key == "brid" and local_name in ("Bridge", "BridgePart"):
            from ...shared.attribute_utils import write_feature_type_attributes_bridge
            write_feature_type_attributes_bridge(feature_el, obj_, "brid")
        
        # BridgeRoom (brid:) - nur für BridgeRoom-Features
        if ns_key == "brid" and local_name == "BridgeRoom":
            from ...shared.attribute_utils import write_feature_type_attributes_bridgeroom
            write_feature_type_attributes_bridgeroom(feature_el, obj_, "brid")
        
        # BridgeFurniture (brid:) - nur für BridgeFurniture-Features
        if ns_key == "brid" and local_name == "BridgeFurniture":
            from ...shared.attribute_utils import write_feature_type_attributes_bridgefurniture
            write_feature_type_attributes_bridgefurniture(feature_el, obj_, "brid")

        # Tunnel (tun:) - nur für Tunnel-Features
        if ns_key == "tun" and local_name in ("Tunnel", "TunnelPart"):
            from ...shared.attribute_utils import write_feature_type_attributes_tunnel
            write_feature_type_attributes_tunnel(feature_el, obj_, "tun")
        
        # HollowSpace (tun:) - nur für HollowSpace-Features
        if ns_key == "tun" and local_name == "HollowSpace":
            from ...shared.attribute_utils import write_feature_type_attributes_hollowspace
            write_feature_type_attributes_hollowspace(feature_el, obj_, "tun")
        
        # TunnelFurniture (tun:) - nur für TunnelFurniture-Features
        if ns_key == "tun" and local_name == "TunnelFurniture":
            from ...shared.attribute_utils import write_feature_type_attributes_tunnelfurniture
            write_feature_type_attributes_tunnelfurniture(feature_el, obj_, "tun")
        
        # NOTE: BuildingUnit & Storey attributes are handled inline below (lines 891-960)
        # No separate function needed due to complex cross-referencing logic
        
        # WaterBody (wtr:) - nur für WaterBody-Features
        if ns_key == "wtr" and local_name == "WaterBody":
            from ...shared.attribute_utils import write_feature_type_attributes_waterbody
            write_feature_type_attributes_waterbody(feature_el, obj_, "wtr")

        # Vegetation (veg:) - nur für Vegetation-Features (SolitaryVegetationObject, PlantCover)
        if ns_key == "veg" and local_name in ("SolitaryVegetationObject", "PlantCover"):
            from ...shared.attribute_utils import write_feature_type_attributes_vegetation
            write_feature_type_attributes_vegetation(feature_el, obj_, "veg")

        # CityFurniture (frn:) - nur für CityFurniture-Features
        if ns_key == "frn" and local_name == "CityFurniture":
            from ...shared.attribute_utils import write_feature_type_attributes_cityfurniture
            write_feature_type_attributes_cityfurniture(feature_el, obj_, "frn")

        # LandUse (luse:) - nur für LandUse-Features
        if ns_key == "luse" and local_name == "LandUse":
            from ...shared.attribute_utils import write_feature_type_attributes_landuse
            write_feature_type_attributes_landuse(feature_el, obj_, "luse")

        # Relief (dem:) - AbstractReliefComponent attributes
        # dem:lod (required for all relief components)
        dem_lod = obj_.get("dem:lod", None)
        if dem_lod is not None:
            try:
                SubElement(feature_el, Q("dem", "lod")).text = str(int(dem_lod))
            except (ValueError, TypeError):
                pass

        # Structural Flag (con:)
        val = obj_.get("con:isStructuralElement", None)
        if val is not None:
            text = str(val).strip()
            if text:
                SubElement(feature_el, Q("con", "isStructuralElement")).text = text
        # Relation to construction (con:)
        rel_val = obj_.get("con:relationToConstruction", None)
        if rel_val is None:
            # Fallback auf unprefixed key, falls du nur "relationToConstruction" hinterlegt hast
            rel_val = obj_.get("relationToConstruction", None)

        if rel_val is not None:
            text = str(rel_val).strip()
            if text:
                SubElement(feature_el, Q("con", "relationToConstruction")).text = text

        # bldg:storeysAboveGround / bldg:storeysBelowGround are valid for
        # AbstractBuilding features, but not for bldg:Storey in CityGML 3.0.
        if ns_key == "bldg" and local_name in ("Building", "BuildingPart"):
            storeys_above = obj_.get("bldg:storeysAboveGround", None)
            if storeys_above is not None:
                try:
                    SubElement(feature_el, Q("bldg", "storeysAboveGround")).text = str(int(storeys_above))
                except (ValueError, TypeError):
                    pass

            storeys_below = obj_.get("bldg:storeysBelowGround", None)
            if storeys_below is not None:
                try:
                    SubElement(feature_el, Q("bldg", "storeysBelowGround")).text = str(int(storeys_below))
                except (ValueError, TypeError):
                    pass
        
        # BuildingUnit-specific attributes (CityGML 3.0)
        if ns_key == "bldg" and local_name == "BuildingUnit":
            # type (e.g., apartment, office)
            unit_type = obj_.get("bldg:type", None)
            if unit_type is not None and str(unit_type).strip():
                SubElement(feature_el, Q("bldg", "type")).text = str(unit_type).strip()
            
            # ownerName
            owner_name = obj_.get("bldg:ownerName", None)
            if owner_name is not None and str(owner_name).strip():
                SubElement(feature_el, Q("bldg", "ownerName")).text = str(owner_name).strip()
            
            # numberOfRooms
            num_rooms = obj_.get("bldg:numberOfRooms", None)
            if num_rooms is not None:
                try:
                    SubElement(feature_el, Q("bldg", "numberOfRooms")).text = str(int(num_rooms))
                except Exception:
                    pass
            
            # numberOfBedRooms
            num_bedrooms = obj_.get("bldg:numberOfBedRooms", None)
            if num_bedrooms is not None:
                try:
                    SubElement(feature_el, Q("bldg", "numberOfBedRooms")).text = str(int(num_bedrooms))
                except Exception:
                    pass
            
            # numberOfBathRooms
            num_bathrooms = obj_.get("bldg:numberOfBathRooms", None)
            if num_bathrooms is not None:
                try:
                    SubElement(feature_el, Q("bldg", "numberOfBathRooms")).text = str(int(num_bathrooms))
                except Exception:
                    pass
        
        # Storey-specific attributes (CityGML 3.0)
        if ns_key == "bldg" and local_name == "Storey":
            # bldg:elevation / con:Elevation
            elev_ref = obj_.get("bldg:elevation:elevationReference", None)
            elev_val = obj_.get("bldg:elevation:elevationValue", None)
            if elev_ref or elev_val:
                elev_el = SubElement(feature_el, Q("bldg", "elevation"))
                con_elev = SubElement(elev_el, Q("con", "Elevation"))
                if elev_ref:
                    SubElement(con_elev, Q("con", "elevationReference")).text = str(elev_ref).strip()
                if elev_val:
                    SubElement(con_elev, Q("con", "elevationValue")).text = str(elev_val).strip()
            
            # bldg:sortKey
            sort_key = obj_.get("bldg:sortKey", None)
            if sort_key is not None:
                text = str(sort_key).strip()
                if text:
                    SubElement(feature_el, Q("bldg", "sortKey")).text = text
        
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

        _write_address(feature_el, obj_, "bldg")
    
    all_bbox_coords = []

    export_types_set = set(export_types or [])
    if export_types_set:
        if export_types_set.intersection({"bldg:Building", "bldg:BuildingPart"}):
            export_types_set.update({"bldg:Storey", "bldg:BuildingRoom"})
        if "bldg:Storey" in export_types_set:
            export_types_set.add("bldg:BuildingRoom")

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
        obj for obj in _collect_export_scope()
        if object_is_viewport_visible(obj, context)
    ]

    def _hierarchical_parent_has_inline_children(parent_obj) -> bool:
        if parent_obj is None:
            return False
        try:
            children = list(getattr(parent_obj, "children_recursive", []) or [])
        except Exception:
            children = []
        if not children:
            children = list(getattr(parent_obj, "children", []) or [])
        for child in children:
            if not object_is_viewport_visible(child, context):
                continue
            try:
                c_feat = str(child.get("cgml3_feature") or "").strip()
            except Exception:
                c_feat = ""
            try:
                c_part = str(child.get("structure_part") or "").strip()
            except Exception:
                c_part = ""
            if c_feat in {"Storey", "BuildingRoom"} or c_part in {"storey", "room"}:
                return True
        return False

    def _should_export_obj(o) -> bool:
        if not export_types_set:
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
            ns_key, local = resolve_feature_tag(str(v))
        except Exception:
            return False
        tag = f"{ns_key}:{local}"
        return tag in export_types_set

    # Process mesh objects
    objs = [o for o in export_scope if o.type == "MESH" and _should_export_obj(o)]
    
    # Process Curve objects (for LineString geometry)
    curve_objs = [o for o in export_scope if o.type == "CURVE" and _should_export_obj(o)]
    
    # Process Empty objects (for Point geometry)
    empty_objs = [o for o in export_scope if o.type == "EMPTY" and _should_export_obj(o)]

    # ============================================================================
    # CITYGML 3 PARTS (buildingPart/bridgePart/tunnelPart)
    #
    # If parts are modeled as parented feature-objects in Blender (via importer),
    # the standard export would otherwise output them as independent cityObjectMember
    # features and lose the semantic relation to their parent.
    #
    # Approach:
    # - Export top-level parent features (EMPTYs) first.
    # - Export their child parts inline via buildingPart/bridgePart/tunnelPart.
    # - Mark inline-exported children with _inline_exported to avoid duplicates.
    # ============================================================================

    # Mapping für die spätere Inline-Einbettung von Storey-Features
    # storey_gml_id → building feature Element
    _storey_to_building_el: Dict[str, Any] = {}
    # Zwischenspeicher: Blender-Objekt-ID → Building-XML-Element
    # (wird in Part-Export / Mesh-Loop befüllt; vermeidet doppelten _feature_id-Aufruf)
    _storey_obj_to_building_el: Dict[int, Any] = {}
    # Mapping für die spätere Inline-Einbettung von BuildingPart-Features
    # part_gml_id → parent Building/BuildingPart feature Element
    _part_to_building_el: Dict[str, Any] = {}
    # Zwischenspeicher: Blender-Objekt-ID → Parent Building-XML-Element
    _part_obj_to_building_el: Dict[int, Any] = {}
    # Mapping für die spätere Inline-Einbettung von BuildingInstallation-Features
    # inst_gml_id → parent Building/BuildingPart feature Element
    _inst_to_building_el: Dict[str, Any] = {}
    # Zwischenspeicher: Blender-Objekt-ID → Parent Building-XML-Element
    _inst_obj_to_building_el: Dict[int, Any] = {}

    _inline_relation_obj_to_parent_el: Dict[int, Tuple[Any, str, str, str, str]] = {}
    _inline_relation_to_parent_el: Dict[Tuple[str, str, str], Tuple[Any, str, str]] = {}

    _INLINE_RELATION_SPECS = {
        ("brid", "Bridge"): {
            ("brid", "BridgePart"): ("brid", "bridgePart"),
            ("brid", "BridgeInstallation"): ("brid", "bridgeInstallation"),
            ("brid", "BridgeFurniture"): ("brid", "bridgeFurniture"),
        },
        ("brid", "BridgePart"): {
            ("brid", "BridgeInstallation"): ("brid", "bridgeInstallation"),
            ("brid", "BridgeFurniture"): ("brid", "bridgeFurniture"),
        },
        ("tun", "Tunnel"): {
            ("tun", "TunnelPart"): ("tun", "tunnelPart"),
            ("tun", "TunnelInstallation"): ("tun", "tunnelInstallation"),
            ("tun", "TunnelFurniture"): ("tun", "tunnelFurniture"),
            ("tun", "HollowSpace"): ("tun", "hollowSpace"),
        },
        ("tun", "TunnelPart"): {
            ("tun", "TunnelInstallation"): ("tun", "tunnelInstallation"),
            ("tun", "TunnelFurniture"): ("tun", "tunnelFurniture"),
            ("tun", "HollowSpace"): ("tun", "hollowSpace"),
        },
    }

    def _register_inline_relation_children(parent_obj, parent_feature_el, parent_ns, parent_local):
        """Remember direct child features that must be moved under parent_feature_el."""
        relation_specs = _INLINE_RELATION_SPECS.get((parent_ns, parent_local), {})
        if not relation_specs:
            return
        for child in getattr(parent_obj, "children", []) or []:
            if not object_is_viewport_visible(child, context):
                continue
            if getattr(child, "type", None) not in ("EMPTY", "MESH"):
                continue
            try:
                if child.get("structure_part") == "outer_shell":
                    continue
            except Exception:
                pass
            c_ns, c_local = _feature_tag(child)
            rel_spec = relation_specs.get((c_ns, c_local))
            if not rel_spec:
                continue
            rel_ns, rel_name = rel_spec
            _inline_relation_obj_to_parent_el[id(child)] = (
                parent_feature_el, rel_ns, rel_name, c_ns, c_local
            )

    part_specs = {
        ("bldg", "Building"): ("bldg", "buildingPart", ("bldg", "BuildingPart")),
        ("bldg", "BuildingPart"): ("bldg", "buildingPart", ("bldg", "BuildingPart")),
        ("brid", "Bridge"): ("brid", "bridgePart", ("brid", "BridgePart")),
        ("brid", "BridgePart"): ("brid", "bridgePart", ("brid", "BridgePart")),
        ("tun", "Tunnel"): ("tun", "tunnelPart", ("tun", "TunnelPart")),
        ("tun", "TunnelPart"): ("tun", "tunnelPart", ("tun", "TunnelPart")),
    }

    def _export_feature_empty_inline(obj, parent_el):
        """Export an EMPTY feature object as an inline feature element under parent_el."""
        feat_ln = obj.get("cgml3_feature")
        if not feat_ln:
            return None
        ns_key, local_name = _feature_tag(obj)
        feat_id = _feature_id(obj)

        feature = SubElement(parent_el, Q(ns_key, local_name))
        feature.set(f"{{{NS['gml']}}}id", feat_id)

        # gml:name
        if obj.name and obj.name != feat_id:
            name_el = SubElement(feature, Q("gml", "name"))
            name_el.text = obj.name

        _write_lifespan_core(feature, obj)
        _write_custom_attributes(feature, obj, ns_key)
        _write_specific_attributes(feature, obj)
        return feature

    def _write_feature_level_closure_surfaces(feature_el, closures_by_parent, parent_ids, lod_level):
        """Write ClosureSurfaces directly under a Part/Installation feature."""
        for parent_id in parent_ids:
            parent_id = str(parent_id or "").strip()
            if not parent_id:
                continue
            for c_gp in closures_by_parent.pop(parent_id, []):
                c_surf_el, c_ms = _begin_bs(
                    feature_el,
                    "ClosureSurface",
                    c_gp.get("surf_id"),
                    c_gp.get("ms_id"),
                    c_gp.get("mat"),
                    lod_level,
                )
                if c_gp.get("is_multisurface", False):
                    for c_poly in c_gp.get("polygons", []):
                        write_polygon_with_ring_ids(
                            parent_ms=c_ms,
                            poly_gid=c_poly["poly_gid"],
                            exterior_ring=c_poly["ext_xyz"],
                            interior_rings=c_poly["int_xyz"] if c_poly["int_xyz"] else None,
                            srs_name=srs_name,
                            exterior_ring_id=c_poly["ext_id"],
                            interior_ring_ids=c_poly["int_ids"] if c_poly["int_ids"] else None,
                        )
                else:
                    write_polygon_with_ring_ids(
                        parent_ms=c_ms,
                        poly_gid=c_gp["poly_gid"],
                        exterior_ring=c_gp["ext_xyz"],
                        interior_rings=c_gp["int_xyz"] if c_gp["int_xyz"] else None,
                        srs_name=srs_name,
                        exterior_ring_id=c_gp["ext_id"],
                        interior_ring_ids=c_gp["int_ids"] if c_gp["int_ids"] else None,
                    )

    def _export_parts_recursive(parent_obj, parent_feature_el):
        """Recursively export child parts as buildingPart/bridgePart/tunnelPart."""
        ns_key, local_name = _feature_tag(parent_obj)
        spec = part_specs.get((ns_key, local_name))
        if not spec:
            return
        container_ns, container_name, (child_ns, child_local) = spec

        for child in parent_obj.children:
            if not object_is_viewport_visible(child, context):
                continue
            if child.type not in ("EMPTY", "MESH"):
                continue
            if child.get("_inline_exported"):
                continue
            c_ns, c_local = _feature_tag(child)
            if (c_ns, c_local) != (child_ns, child_local):
                continue

            # container element (e.g., bldg:buildingPart)
            rel_el = SubElement(parent_feature_el, Q(container_ns, container_name))

            # child feature element (inline)
            child_feat_el = _export_feature_empty_inline(child, rel_el)
            if child_feat_el is None:
                continue

            # mark as inline-exported to prevent later cityObjectMember export
            try:
                child["_inline_exported"] = True
            except Exception:
                mat = face_override.get("mat") if face_override else None

            # recurse (parts can contain parts)
            _export_parts_recursive(child, child_feat_el)

    # Export top-level part-parents first (and inline their parts)
    for obj in empty_objs:
        if obj.get("_inline_exported"):
            continue
        feat_ln = obj.get("cgml3_feature")
        if not feat_ln:
            continue
        # Only consider top-level objects (not already parented by another CityGML feature)
        if obj.parent is not None and obj.parent.get("cgml3_feature"):
            continue
        ns_key, local_name = _feature_tag(obj)
        if (ns_key, local_name) not in part_specs:
            continue

        # Hierarchical Buildings: OuterShell mesh path handles everything
        # (feature element + boundary surfaces + inline rooms/storeys).
        if obj.get("structure_type") == "hierarchical":
            _has_outer_shell = any(
                c.get("structure_part") == "outer_shell"
                and object_is_viewport_visible(c, context)
                for c in obj.children
            )
            if _has_outer_shell:
                continue

        com = add_cityobject_member(root)
        parent_feature = SubElement(com, Q(ns_key, local_name))
        parent_feature.set(f"{{{NS['gml']}}}id", _feature_id(obj))

        if obj.name and obj.name != _feature_id(obj):
            name_el = SubElement(parent_feature, Q("gml", "name"))
            name_el.text = obj.name

        _write_lifespan_core(parent_feature, obj)
        _write_custom_attributes(parent_feature, obj, ns_key)
        _write_specific_attributes(parent_feature, obj)

        # Collect Storey children for inline embedding (hierarchical Building without OuterShell)
        if (ns_key, local_name) in (("bldg", "Building"), ("bldg", "BuildingPart")):
            for child in obj.children:
                if not object_is_viewport_visible(child, context):
                    continue
                c_feat = child.get("cgml3_feature")
                if c_feat == "Storey":
                    _storey_obj_to_building_el[id(child)] = parent_feature

        _register_inline_relation_children(obj, parent_feature, ns_key, local_name)

        _export_parts_recursive(obj, parent_feature)
        try:
            obj["_inline_exported"] = True
        except Exception:
            pass

    # Refresh lists after inline export marking
    empty_objs = [o for o in empty_objs if not o.get("_inline_exported")]
    objs = [o for o in objs if not o.get("_inline_exported")]

    # Ensure outer_shell meshes are processed before their sibling parts/installations
    # so that the Building feature element exists when children register themselves.
    def _mesh_sort_key(o):
        if o.get("structure_part") == "outer_shell":
            return 0
        return 1
    objs.sort(key=_mesh_sort_key)
    
    # Export Curve objects as LineString geometries first
    for obj in curve_objs:
        # Check if this is a CityGML feature
        feat_ln = obj.get("cgml3_feature")
        if not feat_ln:
            continue
            
        ns_key, local_name = _feature_tag(obj)
        _dbg(f"obj={obj.name} ns={ns_key}:{local_name} mesh={getattr(obj,'data',None).name if getattr(obj,'data',None) else None}")
        feat_id = _feature_id(obj)
        
        # Create feature element
        com = add_cityobject_member(root)
        feature = SubElement(com, Q(ns_key, local_name))
        feature.set(f"{{{NS['gml']}}}id", feat_id)
        
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
            lines_container = SubElement(feature, _C("lod0MultiCurve"))
        
        if len(splines) == 1:
            # Single spline - could be LineString or Curve
            spline = splines[0]
            
            # Check if this should be exported as gml:Curve (BEZIER/NURBS) or gml:LineString (POLY)
            use_curve = should_use_gml_curve(spline)
            
            if use_curve:
                # Export as gml:Curve with segments
                curve_elem = SubElement(lines_container, Q("gml", "Curve"))
                curve_elem.set(f"{{{NS['gml']}}}id", _new_gml_id("UUID", used_gml_ids))
                
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
                linestring.set(f"{{{NS['gml']}}}id", _new_gml_id("UUID", used_gml_ids))
                
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
            multicurve.set(f"{{{NS['gml']}}}id", _new_gml_id("UUID", used_gml_ids))
            
            for spline in splines:
                curve_member = SubElement(multicurve, Q("gml", "curveMember"))
                
                # Check if this spline should be a Curve or LineString
                use_curve = should_use_gml_curve(spline)
                
                if use_curve:
                    # Export as gml:Curve with segments
                    curve_elem = SubElement(curve_member, Q("gml", "Curve"))
                    curve_elem.set(f"{{{NS['gml']}}}id", _new_gml_id("UUID", used_gml_ids))
                    
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
                
                else:
                    # Export as simple gml:LineString
                    linestring = SubElement(curve_member, Q("gml", "LineString"))
                    linestring.set(f"{{{NS['gml']}}}id", _new_gml_id("UUID", used_gml_ids))
                    
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
        if not is_breakline_relief:
            _write_custom_attributes(feature, obj, ns_key)
            _write_specific_attributes(feature, obj)
    
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
        # Skip if already exported inline (e.g., TrafficSpace children of Section/Intersection)
        if obj.get("_inline_exported"):
            continue

        # Skip room EMPTYs of hierarchical buildings or Storeys (exported inline)
        if obj.get("structure_part") == "room" and obj.parent:
            _rp = obj.parent
            if (_rp.get("structure_type") == "hierarchical"
                    or _rp.get("cgml3_feature") == "Storey"):
                continue

        # Skip hierarchical Building EMPTYs with outer_shell children
        # (their geometry is exported via the outer_shell mesh in the main loop)
        if obj.get("structure_type") == "hierarchical":
            if any(
                c.get("structure_part") == "outer_shell"
                and object_is_viewport_visible(c, context)
                for c in obj.children
            ):
                continue
        
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
                
                # Create groupMember
                gm_el = SubElement(feature, Q("grp", "groupMember"))
                role_el = SubElement(gm_el, Q("grp", "Role"))
                role_el.set(f"{{{NS['gml']}}}id", _new_gml_id("UUID", used_gml_ids))
                
                # Role attribute: Check multiple sources (CityGML 3.0 preferred)
                role_val = None
                
                # 1. Check group object for role mapping: grp:role_<member_id>
                role_key = f"grp:role_{child_gml_id}"
                if role_key in obj:
                    role_val = obj.get(role_key)
                
                # 2. Fallback: Check child for role (from import)
                if role_val is None:
                    role_val = child.get("grp:role_in_group")
                
                # 3. Legacy fallback: grp:role on child
                if role_val is None:
                    role_val = child.get("grp:role")
                
                # Write role if present
                if role_val is not None and str(role_val).strip():
                    role_text_el = SubElement(role_el, Q("grp", "role"))
                    role_text_el.text = str(role_val).strip()
                
                # groupMember reference (xlink:href)
                gm_ref_el = SubElement(role_el, Q("grp", "groupMember"))
                gm_ref_el.set(Q("xlink", "href"), f"#{child_gml_id}")
            
            # grp:parent (parent-Referenz, falls vorhanden)
            parent_id = obj.get("grp:parent_id")
            if parent_id is not None and str(parent_id).strip():
                parent_el = SubElement(feature, Q("grp", "parent"))
                parent_el.set(Q("xlink", "href"), f"#{str(parent_id).strip()}")
            
            # Generics schreiben
            _write_custom_attributes(feature, obj, ns_key)

            continue  # CityObjectGroup hat keine Geometrie
        
        # Default: Point geometry for other Empty objects
        # Create feature element
        com = add_cityobject_member(root)
        feature = SubElement(com, Q(ns_key, local_name))
        feature.set(f"{{{NS['gml']}}}id", feat_id)
        
        # Add gml:name if exists
        if obj.name and obj.name != feat_id:
            name_el = SubElement(feature, Q("gml", "name"))
            name_el.text = obj.name
        
        # XSD order: AbstractCityObject (genericAttribute) → AbstractSpace (lod0Point) → specific attrs
        _write_custom_attributes(feature, obj, ns_key)
        
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
        lod0_point = SubElement(feature, _C("lod0Point"))
        point = SubElement(lod0_point, Q("gml", "Point"))
        point.set(f"{{{NS['gml']}}}id", _new_gml_id("UUID", used_gml_ids))
        pos = SubElement(point, Q("gml", "pos"))
        pos.set("srsDimension", "3")
        pos.text = f"{fmt(x)} {fmt(y)} {fmt(z)}"
        
        # Specific attributes AFTER geometry (XSD: AbstractBuilding etc.)
        _write_specific_attributes(feature, obj)
        
        # Storey → Building Zuordnung registrieren (EMPTY-Pfad)
        if (ns_key, local_name) == ("bldg", "Storey"):
            _bld_el = _storey_obj_to_building_el.get(id(obj))
            if _bld_el is not None:
                _storey_to_building_el[feat_id] = _bld_el
        
        # Update bbox
        all_bbox_coords.extend([(x, y, z)])

    # ------------------------------------------------------------------
    # Hierarchical room export helper (CityGML 3.0)
    # ------------------------------------------------------------------
    def _export_hier_room_v3(
        room_empty, building_el, context, trf, fmt,
        Q, NS, GML_ID, _feature_id,
        ox, oy, oz, all_bbox_coords,
        lod,
    ):
        """Export a hierarchical Room EMPTY as inline bldg:buildingRoom > bldg:BuildingRoom."""
        if not object_is_viewport_visible(room_empty, context):
            return



        room_id = room_empty.get("gml_id") or room_empty.name
        room_name = room_empty.get("gml_name") or room_empty.name

        br_el = SubElement(building_el, Q("bldg", "buildingRoom"))
        room_el = SubElement(br_el, Q("bldg", "BuildingRoom"))
        room_el.set(GML_ID, make_gml_id(str(room_id), used_gml_ids))

        if room_name and room_name != room_id:
            SubElement(room_el, Q("gml", "name")).text = room_name

        # Find room_geometry child mesh, or use room object itself if it IS a mesh
        room_geom_obj = None
        for child in room_empty.children:
            if not object_is_viewport_visible(child, context):
                continue
            if child.get("structure_part") == "room_geometry" and child.type == 'MESH':
                room_geom_obj = child
                break
        if room_geom_obj is None and getattr(room_empty, 'type', None) == 'MESH':
            room_geom_obj = room_empty

        if room_geom_obj:
            deps = context.evaluated_depsgraph_get()
            obj_eval = room_geom_obj.evaluated_get(deps)
            mesh = obj_eval.to_mesh(preserve_all_data_layers=True, depsgraph=deps)

            if mesh and mesh.polygons:
                # Load surface_id_map and surfaces_info
                surface_id_map = {}
                surfaces_info = {}
                sid_json = room_geom_obj.get("surface_id_map", "")
                if sid_json:
                    try:
                        surface_id_map = {int(k): v for k, v in json.loads(sid_json).items()}
                    except (ValueError, TypeError):
                        pass
                sinfo_json = room_geom_obj.get("surfaces_info", "")
                if sinfo_json:
                    try:
                        surfaces_info = json.loads(sinfo_json)
                    except (ValueError, TypeError):
                        pass

                # Group faces by surface identity
                faces_by_surface = OrderedDict()
                surface_type_by_key = {}
                surface_name_by_key = {}

                for fidx, poly in enumerate(mesh.polygons):
                    surf_type = default_unclassified_surface_type
                    try:
                        mi = poly.material_index
                        mat = room_geom_obj.material_slots[mi].material if 0 <= mi < len(room_geom_obj.material_slots) else None
                        if mat:
                            v = mat.get("surface_type") or mat.get("SurfaceTyp")
                            if v:
                                surf_type = str(v)
                    except Exception:
                        pass

                    sid = surface_id_map.get(fidx)
                    if sid:
                        group_key = sid
                        sinfo = surfaces_info.get(sid, {})
                        if group_key not in surface_type_by_key:
                            surface_type_by_key[group_key] = sinfo.get("type", surf_type)
                            surface_name_by_key[group_key] = sinfo.get("name", "")
                    else:
                        group_key = f"type_{surf_type}"
                        if group_key not in surface_type_by_key:
                            surface_type_by_key[group_key] = surf_type
                            surface_name_by_key[group_key] = ""

                    faces_by_surface.setdefault(group_key, []).append(fidx)

                # Collect openings by surface_id and surface_type
                openings_by_surface_id = defaultdict(list)
                openings_by_surface_type = defaultdict(list)
                for child in room_empty.children:
                    if not object_is_viewport_visible(child, context):
                        continue
                    if child.get("structure_part") == "opening":
                        o_sid = child.get("opening_surface_id")
                        o_stype = child.get("opening_surface_type")
                        if o_sid:
                            openings_by_surface_id[o_sid].append(child)
                        elif o_stype:
                            openings_by_surface_type[o_stype].append(child)

                # Export each surface group as core:boundary > con:*Surface
                # Valid Room boundary types in CityGML 3.0:
                _VALID_ROOM_BOUNDARIES = {
                    "InteriorWallSurface", "CeilingSurface", "FloorSurface",
                    "WallSurface", "GroundSurface", "RoofSurface",
                    "OuterCeilingSurface", "OuterFloorSurface",
                }
                _INSTALLATION_TYPES_ROOM = {
                    "BuildingInstallation", "IntBuildingInstallation",
                }
                _FILLING_TYPES_ROOM = {"WindowSurface", "DoorSurface", "Window", "Door"}
                # Remap invalid boundary types for rooms
                _ROOM_BOUNDARY_REMAP = {
                    "ClosureSurface": "InteriorWallSurface",
                }

                # Separate surfaces by category
                installation_face_indices = []
                filling_groups = {}  # surf_type -> [face_indices]
                boundary_groups = OrderedDict()  # group_key -> face_indices (only valid boundaries)

                for group_key, face_indices in faces_by_surface.items():
                    surf_type = surface_type_by_key.get(group_key, default_unclassified_surface_type)
                    if surf_type in _INSTALLATION_TYPES_ROOM:
                        installation_face_indices.extend(face_indices)
                    elif surf_type in _FILLING_TYPES_ROOM:
                        filling_groups.setdefault(surf_type, []).extend(face_indices)
                    else:
                        # Remap invalid types
                        if surf_type in _ROOM_BOUNDARY_REMAP:
                            surface_type_by_key[group_key] = _ROOM_BOUNDARY_REMAP[surf_type]
                        boundary_groups[group_key] = face_indices

                # Write valid boundary surfaces
                for group_key, face_indices in boundary_groups.items():
                    surf_type = surface_type_by_key.get(group_key, default_unclassified_surface_type)
                    surf_name = surface_name_by_key.get(group_key, "")

                    bb = SubElement(room_el, _C("boundary"))
                    surf_el = SubElement(bb, Q("con", surf_type))

                    if not group_key.startswith("type_"):
                        surf_el.set(GML_ID, make_gml_id(str(group_key), used_gml_ids))
                    else:
                        surf_el.set(GML_ID, f"ID_{uuid4().hex}")

                    if surf_name:
                        SubElement(surf_el, Q("gml", "name")).text = surf_name

                    # Openings as con:filling (CityGML 3.0)
                    openings_for_surf = openings_by_surface_id.get(group_key, [])
                    if not openings_for_surf and group_key.startswith("type_"):
                        openings_for_surf = openings_by_surface_type.get(surf_type, [])
                    for op_obj in openings_for_surf:
                        _export_hier_opening_v3(
                            op_obj, surf_el, context, trf, fmt,
                            Q, NS, GML_ID, ox, oy, oz, all_bbox_coords, lod,
                        )

                    lod_el = SubElement(surf_el, _C(f"lod{lod}MultiSurface"))
                    ms_el = SubElement(lod_el, Q("gml", "MultiSurface"))
                    ms_el.set(GML_ID, f"ID_{uuid4().hex}")

                    for fidx in face_indices:
                        poly = mesh.polygons[fidx]
                        ring_local = []
                        for li in poly.loop_indices:
                            vi = mesh.loops[li].vertex_index
                            vco = obj_eval.matrix_world @ mesh.vertices[vi].co
                            ring_local.append((float(vco.x), float(vco.y), float(vco.z)))
                        # Close ring
                        if ring_local and ring_local[0] != ring_local[-1]:
                            ring_local.append(ring_local[0])

                        arr = np.asarray(ring_local, dtype=np.float64)
                        ring_tgt, origin_tgt = trf.transform_with_origin(arr, (ox, oy, oz))
                        ring_abs = (ring_tgt + np.asarray(origin_tgt, dtype=np.float64)).tolist()

                        sm = SubElement(ms_el, Q("gml", "surfaceMember"))
                        poly_el = SubElement(sm, Q("gml", "Polygon"))
                        poly_gid = f"ID_{uuid4().hex}"
                        poly_el.set(GML_ID, poly_gid)

                        ext = SubElement(poly_el, Q("gml", "exterior"))
                        lr = SubElement(ext, Q("gml", "LinearRing"))

                        pos_list = []
                        for pt in ring_abs:
                            x, y, z = float(pt[0]), float(pt[1]), float(pt[2])
                            pos_list.append(f"{fmt(x)} {fmt(y)} {fmt(z)}")
                            all_bbox_coords.append((x, y, z))

                        pl = SubElement(lr, Q("gml", "posList"))
                        pl.set("srsDimension", "3")
                        pl.text = " ".join(pos_list)

                # Helper to write face indices as a MultiSurface geometry
                def _write_room_faces_ms(parent_el, lod_tag, fidx_list):
                    lod_el2 = SubElement(parent_el, _C(lod_tag))
                    ms_el2 = SubElement(lod_el2, Q("gml", "MultiSurface"))
                    ms_el2.set(GML_ID, f"ID_{uuid4().hex}")
                    for fidx2 in fidx_list:
                        poly2 = mesh.polygons[fidx2]
                        ring_local2 = []
                        for li2 in poly2.loop_indices:
                            vi2 = mesh.loops[li2].vertex_index
                            vco2 = obj_eval.matrix_world @ mesh.vertices[vi2].co
                            ring_local2.append((float(vco2.x), float(vco2.y), float(vco2.z)))
                        if ring_local2 and ring_local2[0] != ring_local2[-1]:
                            ring_local2.append(ring_local2[0])
                        arr2 = np.asarray(ring_local2, dtype=np.float64)
                        ring_tgt2, origin_tgt2 = trf.transform_with_origin(arr2, (ox, oy, oz))
                        ring_abs2 = (ring_tgt2 + np.asarray(origin_tgt2, dtype=np.float64)).tolist()
                        sm2 = SubElement(ms_el2, Q("gml", "surfaceMember"))
                        poly_el2 = SubElement(sm2, Q("gml", "Polygon"))
                        poly_el2.set(GML_ID, f"ID_{uuid4().hex}")
                        ext2 = SubElement(poly_el2, Q("gml", "exterior"))
                        lr2 = SubElement(ext2, Q("gml", "LinearRing"))
                        pos_list2 = []
                        for pt2 in ring_abs2:
                            x2, y2, z2 = float(pt2[0]), float(pt2[1]), float(pt2[2])
                            pos_list2.append(f"{fmt(x2)} {fmt(y2)} {fmt(z2)}")
                            all_bbox_coords.append((x2, y2, z2))
                        pl2 = SubElement(lr2, Q("gml", "posList"))
                        pl2.set("srsDimension", "3")
                        pl2.text = " ".join(pos_list2)

                # Write BuildingInstallation faces as bldg:buildingInstallation
                if installation_face_indices:
                    inst_wrapper = SubElement(room_el, Q("bldg", "buildingInstallation"))
                    inst_el = SubElement(inst_wrapper, Q("bldg", "BuildingInstallation"))
                    inst_el.set(GML_ID, f"ID_{uuid4().hex}")
                    _write_room_faces_ms(inst_el, f"lod{lod}MultiSurface", installation_face_indices)

                # Write DoorSurface/WindowSurface faces as con:fillingSurface
                # under the last InteriorWallSurface, or as standalone boundaries if no wall found
                for fill_type, fill_indices in filling_groups.items():
                    # Find the last wall boundary to attach fillings to
                    # If none, export as standalone boundary (best effort)
                    # CityGML 3.0: fillingSurface is valid on any AbstractConstructionSurface
                    # For rooms, attach to the room element directly as boundary
                    bb_f = SubElement(room_el, _C("boundary"))
                    fill_surf_type = "DoorSurface" if "Door" in fill_type else "WindowSurface"
                    surf_el_f = SubElement(bb_f, Q("con", fill_surf_type))
                    surf_el_f.set(GML_ID, f"ID_{uuid4().hex}")
                    _write_room_faces_ms(surf_el_f, f"lod{lod}MultiSurface", fill_indices)

            obj_eval.to_mesh_clear()

        # Furniture children
        for child in room_empty.children:
            if not object_is_viewport_visible(child, context):
                continue
            if child.get("structure_part") == "furniture" and child.type == 'MESH':
                _export_hier_furnishing_v3(
                    child, room_el, "buildingFurniture", "BuildingFurniture",
                    context, trf, fmt, Q, NS, GML_ID,
                    ox, oy, oz, all_bbox_coords, lod,
                )

        # Installations children
        for child in room_empty.children:
            if not object_is_viewport_visible(child, context):
                continue
            if child.get("structure_part") == "installation" and child.type == 'MESH':
                _export_hier_furnishing_v3(
                    child, room_el, "buildingInstallation", "BuildingInstallation",
                    context, trf, fmt, Q, NS, GML_ID,
                    ox, oy, oz, all_bbox_coords, lod,
                )

    def _export_hier_furnishing_v3(
        mesh_obj, parent_el, wrapper_name, feature_name,
        context, trf, fmt, Q, NS, GML_ID,
        ox, oy, oz, all_bbox_coords, lod,
    ):
        """Export a furniture/installation mesh as inline bldg:buildingFurniture or bldg:buildingInstallation."""
        if not object_is_viewport_visible(mesh_obj, context):
            return

        fid = mesh_obj.get("gml_id") or mesh_obj.name
        fname = mesh_obj.get("gml_name") or mesh_obj.name

        wrap_el = SubElement(parent_el, Q("bldg", wrapper_name))
        feat_el = SubElement(wrap_el, Q("bldg", feature_name))
        feat_el.set(GML_ID, make_gml_id(str(fid), used_gml_ids))

        if fname and fname != fid:
            SubElement(feat_el, Q("gml", "name")).text = fname

        deps = context.evaluated_depsgraph_get()
        obj_eval = mesh_obj.evaluated_get(deps)
        mesh = obj_eval.to_mesh(preserve_all_data_layers=True, depsgraph=deps)

        if mesh and mesh.polygons:
            lod_el = SubElement(feat_el, _C(f"lod{lod}MultiSurface"))
            ms_el = SubElement(lod_el, Q("gml", "MultiSurface"))
            ms_el.set(GML_ID, f"ID_{uuid4().hex}")

            for poly in mesh.polygons:
                ring_local = []
                for li in poly.loop_indices:
                    vi = mesh.loops[li].vertex_index
                    vco = obj_eval.matrix_world @ mesh.vertices[vi].co
                    ring_local.append((float(vco.x), float(vco.y), float(vco.z)))
                if ring_local and ring_local[0] != ring_local[-1]:
                    ring_local.append(ring_local[0])

                arr = np.asarray(ring_local, dtype=np.float64)
                ring_tgt, origin_tgt = trf.transform_with_origin(arr, (ox, oy, oz))
                ring_abs = (ring_tgt + np.asarray(origin_tgt, dtype=np.float64)).tolist()

                sm = SubElement(ms_el, Q("gml", "surfaceMember"))
                poly_el = SubElement(sm, Q("gml", "Polygon"))
                poly_el.set(GML_ID, f"ID_{uuid4().hex}")

                ext = SubElement(poly_el, Q("gml", "exterior"))
                lr = SubElement(ext, Q("gml", "LinearRing"))

                pos_list = []
                for pt in ring_abs:
                    x, y, z = float(pt[0]), float(pt[1]), float(pt[2])
                    pos_list.append(f"{fmt(x)} {fmt(y)} {fmt(z)}")
                    all_bbox_coords.append((x, y, z))

                pl = SubElement(lr, Q("gml", "posList"))
                pl.set("srsDimension", "3")
                pl.text = " ".join(pos_list)

        obj_eval.to_mesh_clear()

    def _export_hier_opening_v3(
        opening_obj, surface_el, context, trf, fmt,
        Q, NS, GML_ID, ox, oy, oz, all_bbox_coords, lod,
    ):
        """Export an opening (Door/Window) as inline con:filling under a surface."""
        if not object_is_viewport_visible(opening_obj, context):
            return

        o_type = opening_obj.get("opening_type") or opening_obj.get("cgml3_feature") or "Door"
        filling_surface_type = "WindowSurface" if str(o_type) in ("Window", "WindowSurface") else "DoorSurface"
        o_id = opening_obj.get("gml_id") or opening_obj.name

        fill_el = SubElement(surface_el, Q("con", "fillingSurface"))
        opening_el = SubElement(fill_el, Q("con", filling_surface_type))
        opening_el.set(GML_ID, make_gml_id(str(o_id), used_gml_ids))

        o_name = opening_obj.get("gml_name")
        if o_name and o_name != o_id:
            SubElement(opening_el, Q("gml", "name")).text = o_name

        if opening_obj.type != 'MESH':
            return

        deps = context.evaluated_depsgraph_get()
        obj_eval = opening_obj.evaluated_get(deps)
        mesh = obj_eval.to_mesh(preserve_all_data_layers=True, depsgraph=deps)

        if mesh and mesh.polygons:
            lod_el = SubElement(opening_el, _C(f"lod{lod}MultiSurface"))
            ms_el = SubElement(lod_el, Q("gml", "MultiSurface"))
            ms_el.set(GML_ID, f"ID_{uuid4().hex}")

            for poly in mesh.polygons:
                ring_local = []
                for li in poly.loop_indices:
                    vi = mesh.loops[li].vertex_index
                    vco = obj_eval.matrix_world @ mesh.vertices[vi].co
                    ring_local.append((float(vco.x), float(vco.y), float(vco.z)))
                if ring_local and ring_local[0] != ring_local[-1]:
                    ring_local.append(ring_local[0])

                arr = np.asarray(ring_local, dtype=np.float64)
                ring_tgt, origin_tgt = trf.transform_with_origin(arr, (ox, oy, oz))
                ring_abs = (ring_tgt + np.asarray(origin_tgt, dtype=np.float64)).tolist()

                sm = SubElement(ms_el, Q("gml", "surfaceMember"))
                poly_el = SubElement(sm, Q("gml", "Polygon"))
                poly_el.set(GML_ID, f"ID_{uuid4().hex}")

                ext = SubElement(poly_el, Q("gml", "exterior"))
                lr = SubElement(ext, Q("gml", "LinearRing"))

                pos_list = []
                for pt in ring_abs:
                    x, y, z = float(pt[0]), float(pt[1]), float(pt[2])
                    pos_list.append(f"{fmt(x)} {fmt(y)} {fmt(z)}")
                    all_bbox_coords.append((x, y, z))

                pl = SubElement(lr, Q("gml", "posList"))
                pl.set("srsDimension", "3")
                pl.text = " ".join(pos_list)

        obj_eval.to_mesh_clear()

    # Global: eindeutige Textur-Dateinamen über ALLE Features hinweg
    used_tex_names_global: set[str] = set()
    image_uri_map_global: dict[str, str] = {}

    for obj in objs:
        # Skip if already exported inline (e.g., TrafficSpace children of Section/Intersection)
        if obj.get("_inline_exported"):
            continue

        # Skip room-related children of hierarchical buildings or Storeys (exported inline)
        _sp = obj.get("structure_part")
        if _sp in ("room_geometry", "furniture", "installation", "opening"):
            _sp_parent = obj.parent
            if _sp_parent and _sp_parent.get("structure_part") == "room":
                _sp_gp = _sp_parent.parent
                if _sp_gp and (_sp_gp.get("structure_type") == "hierarchical"
                               or _sp_gp.get("cgml3_feature") == "Storey"):
                    continue

        # Skip room meshes of hierarchical buildings (exported inline via _export_hier_room_v3)
        if _sp == "room" and obj.parent:
            if (obj.parent.get("structure_type") == "hierarchical"
                    or obj.parent.get("cgml3_feature") == "Storey"):
                continue

        # Hierarchical building: OuterShell mesh exports boundary surfaces under the
        # Building feature. Room polygons are exported with fresh IDs by _export_hier_room_v3,
        # so no deduplication needed.
        _hier_parent = None
        if (obj.get("structure_part") == "outer_shell"
                and obj.parent
                and obj.parent.get("structure_type") == "hierarchical"):
            _hier_parent = obj.parent
        descendant_feature_poly_ids = set()
        
        com = add_cityobject_member(root) 
        deps = context.evaluated_depsgraph_get()
        obj_eval = obj.evaluated_get(deps)
        mesh = obj_eval.to_mesh(preserve_all_data_layers=True, depsgraph=deps)
        
        # Check if mesh has vertices but no polygons (MultiPoint geometry)
        if mesh and len(mesh.vertices) > 0 and not mesh.polygons:
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
                    coverage.set(f"{{{NS['gml']}}}id", grid_data.get('grid_id', _new_gml_id("UUID", used_gml_ids)))
                    
                    # Add gml:domainSet with RectifiedGrid
                    domain_set = SubElement(coverage, Q("gml", "domainSet"))
                    rectified_grid = SubElement(domain_set, Q("gml", "RectifiedGrid"))
                    rectified_grid.set(f"{{{NS['gml']}}}id", _new_gml_id("UUID", used_gml_ids))
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
                    origin_point.set(f"{{{NS['gml']}}}id", _new_gml_id("UUID", used_gml_ids))
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
                pointcloud_wrapper = SubElement(feature, _C("pointCloud"))
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
            # Still export feature metadata for meshes without faces, but they won't have
            # a renderable geometry in CityGML. For CityFurniture/Vegetation objects we
            # at least write a lod0Point fallback so FME doesn't classify them as xml_no_geom.
            ns_key, local_name = _feature_tag(obj)
            if ns_key in ("frn", "veg"):
                try:
                    # Ensure ox/oy/oz exist in this early-exit branch
                    ox, oy, oz = _resolve_export_offset(context, obj)
                    loc = obj.matrix_world.translation
                    coords_local = np.asarray([(float(loc.x), float(loc.y), float(loc.z))], dtype=np.float64)
                    coords_tgt, origin_tgt = trf.transform_with_origin(coords_local, (ox, oy, oz))
                    x = float(coords_tgt[0][0]) + origin_tgt[0]
                    y = float(coords_tgt[0][1]) + origin_tgt[1]
                    z = float(coords_tgt[0][2]) + origin_tgt[2]

                    feat_id = _feature_id(obj)
                    com = add_cityobject_member(root)
                    feature = SubElement(com, Q(ns_key, local_name))
                    feature.set(f"{{{NS['gml']}}}id", feat_id)

                    lod0_point = SubElement(feature, _C("lod0Point"))
                    point = SubElement(lod0_point, Q("gml", "Point"))
                    point.set(f"{{{NS['gml']}}}id", _new_gml_id("UUID", used_gml_ids))
                    pos = SubElement(point, Q("gml", "pos"))
                    pos.set("srsDimension", "3")
                    pos.text = f"{fmt(x)} {fmt(y)} {fmt(z)}"

                    _write_lifespan_core(feature, obj)
                    all_bbox_coords.append((x, y, z))
                except Exception:
                    pass

            if obj_eval:
                obj_eval.to_mesh_clear()
            continue

        # Prüfen, ob dieses Objekt aus einer ImplicitGeometry stammt.
        # NOTE: Blender may produce a different evaluated data-block; support both.
        mesh_orig = obj.data
        try:
            is_implicit = bool(mesh_orig.get("ImplicitGeometry", False))
        except Exception:
            is_implicit = False
        if not is_implicit:
            try:
                eval_mesh = getattr(obj_eval, "data", None)
                if eval_mesh is not None:
                    is_implicit = bool(eval_mesh.get("ImplicitGeometry", False))
                    if is_implicit:
                        mesh_orig = eval_mesh
            except Exception:
                mat = face_override.get("mat") if face_override else None
        _dbg(f"obj={obj.name} is_implicit={is_implicit} polys={(len(mesh.polygons) if mesh else None)}")

        meta_for_mesh = None
        is_imp_prototype = False
        if is_implicit:
            meta_for_mesh = implicit_meta_by_mesh.get(mesh_orig)
            # IMPORTANT: Distinguish between "no meta present yet" (prototype writes inline geometry)
            # and "meta present" (clones reference the prototype). Do not auto-create a dict here.
            if meta_for_mesh is None:
                if export_debug:
                    _dbg(f"obj={obj.name} implicit meta: first time for this mesh -> prototype")
                is_imp_prototype = True
            else:
                is_imp_prototype = False

        ns_key, local_name = _feature_tag(_hier_parent or obj)
        # Doppelseitige Appearance nur, wenn explizit gewünscht.
        # In vielen CityGML-Datasets (und auch in deinen Vergleichsdateien) sind
        # ParameterizedTexture-Einträge nur einmal vorhanden. Doppelseitiges Schreiben
        # verdoppelt dann jede TextureAssociation (isFront=true/false) und kann zu
        # "doppelten Appearances" bzw. überschriebenen/inkonsistenten Texturen führen.
        double_sided = bool(
            obj.get("cgml3_double_sided", False)
            or obj.get("double_sided", False)
        )

        feat_id = _feature_id(_hier_parent or obj)
        feature = SubElement(com, Q(ns_key, local_name))
        feature.set(f"{{{NS['gml']}}}id", feat_id)

        # gml:name from hierarchical parent EMPTY (standard mesh path has none)
        if _hier_parent:
            _hp_name = _hier_parent.name
            if _hp_name and _hp_name != feat_id:
                _n_el = SubElement(feature, Q("gml", "name"))
                _n_el.text = _hp_name

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
                    child_gml_id = child_obj.get("gml_id") or f"ID_{uuid4()}"
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
                            # Check which type based on custom property
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

        # Spezielle Behandlung für Window & Door (Construction Module - CityGML 3.0)
        # Diese Features werden als con:filling exportiert wenn sie Kinder eines ConstructiveSurface sind
        # Hier behandeln wir sie als standalone Features (z.B. Top-Level oder unter BuildingRoom)
        if (ns_key, local_name) in [("con", "Window"), ("con", "Door")]:
            # Write gml:name
            if obj.name and obj.name != feat_id:
                name_el = SubElement(feature, Q("gml", "name"))
                name_el.text = obj.name
            
            # Write standard CityObject attributes (creationDate, terminationDate, etc.)
            _write_lifespan_attributes(feature, obj)
            
            # Write Construction Module attributes (class, function, usage)
            _write_specific_attributes(feature, obj)
            
            # Write generic attributes
            _write_generic_attributes(feature, obj)
            
            # Write address for Door (optional)
            if local_name == "Door":
                # Door can have bldg:address (inherited from AbstractFillingElement)
                _write_address(feature, obj, "bldg")
            
            # Export geometry (lod*MultiSurface)
            # Window/Door typically use lod3MultiSurface or lod4MultiSurface
            ox, oy, oz = _resolve_export_offset(context, obj)
            
            # Determine LOD level from custom property
            lod_level = obj.get("lod", "3")  # Default to LOD3
            lod_key = f"lod{lod_level}MultiSurface"
            
            # Create lod*MultiSurface element
            lod_geom = SubElement(feature, Q("con", lod_key))
            multi_surf = SubElement(lod_geom, Q("gml", "MultiSurface"))
            multi_surf.set(f"{{{NS['gml']}}}id", _new_gml_id("UUID", used_gml_ids))
            
            # Export all polygons as surfaceMember
            for poly in mesh.polygons:
                surf_member = SubElement(multi_surf, Q("gml", "surfaceMember"))
                poly_elem = SubElement(surf_member, Q("gml", "Polygon"))
                poly_elem.set(f"{{{NS['gml']}}}id", _new_gml_id("UUID", used_gml_ids))
                
                # Exterior ring
                exterior = SubElement(poly_elem, Q("gml", "exterior"))
                linear_ring = SubElement(exterior, Q("gml", "LinearRing"))
                
                # Get vertex coordinates (transformed)
                ring_xyz_local = []
                for li in poly.loop_indices:
                    vi = mesh.loops[li].vertex_index
                    vco = obj_eval.matrix_world @ mesh.vertices[vi].co
                    ring_xyz_local.append((float(vco.x), float(vco.y), float(vco.z)))
                ring_xyz_local = _ensure_closed2d(ring_xyz_local)
                
                origin_src = (ox, oy, oz)
                ring_xyz_arr = np.asarray(ring_xyz_local, dtype=np.float64)
                
                # Transform
                ring_tgt_local, origin_tgt = trf.transform_with_origin(ring_xyz_arr, origin_src)
                ring_xyz_abs = (ring_tgt_local + np.asarray(origin_tgt, dtype=np.float64)).tolist()
                
                # Write posList
                pos_list = SubElement(linear_ring, Q("gml", "posList"))
                pos_list.set("srsDimension", "3")
                flat = []
                for x, y, z in ring_xyz_abs:
                    flat.extend([fmt(x), fmt(y), fmt(z)])
                    all_bbox_coords.append((x, y, z))
                pos_list.text = " ".join(flat)
            
            # Cleanup
            if obj_eval:
                obj_eval.to_mesh_clear()
            continue

        # Spezielle Behandlung für Dynamizer (Empty-Objekte mit dynamischen Daten)
        if (ns_key, local_name) == ("dyn", "Dynamizer"):
            # Dynamizer-spezifische Attribute exportieren
            
            # dyn:attributeRef (required)
            attr_ref = obj.get("dyn:attributeRef")
            if attr_ref:
                SubElement(feature, Q("dyn", "attributeRef")).text = str(attr_ref)
            
            # dyn:startTime / dyn:endTime (optional)
            start_time = obj.get("dyn:startTime")
            if start_time:
                SubElement(feature, Q("dyn", "startTime")).text = str(start_time)
            
            end_time = obj.get("dyn:endTime")
            if end_time:
                SubElement(feature, Q("dyn", "endTime")).text = str(end_time)
            
            # Timeseries-Typ und Daten
            ts_type = obj.get("dyn:timeseriesType")
            
            if ts_type == "AtomicTimeseries":
                ts_elem = SubElement(feature, Q("dyn", "dynamicData"))
                atomic_ts = SubElement(ts_elem, Q("dyn", "AtomicTimeseries"), {GML_ID: _new_gml_id("UUID", used_gml_ids)})
                
                # Observation type (required)
                obs_prop = obj.get("dyn:observationProperty", "unknown")
                SubElement(atomic_ts, Q("dyn", "observationProperty")).text = str(obs_prop)
                
                # UoM (unit of measurement, optional)
                uom = obj.get("dyn:uom")
                if uom:
                    SubElement(atomic_ts, Q("dyn", "uom")).text = str(uom)
                
                # Data sources
                if obj.get("dyn:hasDynamicDataDR"):
                    dr_elem = SubElement(atomic_ts, Q("dyn", "dynamicDataDR"))
                    # Placeholder - real data would come from external source
                    SubElement(dr_elem, Q("dyn", "reference")).text = obj.get("dyn:dynamicDataDR_ref", "")
                
                if obj.get("dyn:hasDynamicDataTVP"):
                    tvp_elem = SubElement(atomic_ts, Q("dyn", "dynamicDataTVP"))
                    # Placeholder - real TVP data would be stored elsewhere
                    
                if obj.get("dyn:hasObservationData"):
                    obs_elem = SubElement(atomic_ts, Q("dyn", "observationData"))
                    # Placeholder - Observation&Measurements data
            
            elif ts_type == "CompositeTimeseries":
                ts_elem = SubElement(feature, Q("dyn", "dynamicData"))
                comp_ts = SubElement(ts_elem, Q("dyn", "CompositeTimeseries"), {GML_ID: _new_gml_id("UUID", used_gml_ids)})
                # CompositeTimeseries contains multiple components
                # Would need nested AtomicTimeseries references
            
            # SensorConnection (optional)
            if obj.get("dyn:hasSensorConnection"):
                sensor_elem = SubElement(feature, Q("dyn", "sensorConnection"))
                sensor_conn = SubElement(sensor_elem, Q("dyn", "SensorConnection"), {GML_ID: _new_gml_id("UUID", used_gml_ids)})
                # Sensor details would be stored in additional properties
            
            # Lifespan attributes (inherited from AbstractFeatureWithLifespan)
            _write_lifespan_core(feature, obj)
            
            # Generic attributes
            _write_generic_attributes(feature, obj)
            
            # Dynamizer hat keine Geometrie
            if obj_eval:
                obj_eval.to_mesh_clear()
            continue

        # Transportation Section & Intersection: export with trafficSpace children
        if (ns_key, local_name) in [("tran", "Section"), ("tran", "Intersection")]:
            # CityGML 3.0 sequence for Section/Intersection:
            # GML/core metadata -> genericAttribute -> inherited transportation relations -> class.
            _write_core(feature, obj)
            _write_lifespan_core(feature, obj)
            _write_generic_attributes(feature, obj)

            traffic_direction = obj.get("tran:trafficDirection", None)
            if traffic_direction is not None:
                for value in _iter_attr_values(traffic_direction):
                    text = str(value).strip()
                    if text:
                        SubElement(feature, Q("tran", "trafficDirection")).text = text

            # Export relation members allowed by AbstractTransportationSpaceType.
            if obj.children:
                for child_obj in obj.children:
                    if not object_is_viewport_visible(child_obj, context):
                        continue
                    child_ns_key, child_local_name = _feature_tag(child_obj)

                    if child_ns_key == "tran" and child_local_name in ("TrafficSpace", "AuxiliaryTrafficSpace", "Hole", "Marking"):
                        child_gml_id = _feature_id(child_obj)

                        if child_local_name == "TrafficSpace":
                            container_name = "trafficSpace"
                        elif child_local_name == "AuxiliaryTrafficSpace":
                            container_name = "auxiliaryTrafficSpace"
                        elif child_local_name == "Hole":
                            container_name = "hole"
                        else:
                            container_name = "marking"

                        space_container = SubElement(feature, Q("tran", container_name))
                        space_container.set(Q("xlink", "href"), f"#{child_gml_id}")

            class_val = obj.get("tran:class", None)
            if class_val is not None:
                for value in _iter_attr_values(class_val):
                    text = str(value).strip()
                    if text:
                        SubElement(feature, Q("tran", "class")).text = text
                        break

            if obj_eval:
                obj_eval.to_mesh_clear()
            continue

        # Versioning: Version & VersionTransition export
        if (ns_key, local_name) == ("vers", "Version"):
            # Write core attributes (gml:name, description, creationDate, etc.)
            _write_core(feature, obj)
            
            # Lifespan attributes (creationDate, terminationDate, validFrom, validTo)
            _write_lifespan_core(feature, obj)
            
            # vers:versionMember - export references to city objects in this version
            member_ids = obj.get("vers:member_ids")
            if member_ids:
                # Parse comma-separated list or list
                if isinstance(member_ids, str):
                    member_list = [m.strip() for m in member_ids.split(",") if m.strip()]
                else:
                    member_list = member_ids
                
                for member_id in member_list:
                    vm_el = SubElement(feature, Q("vers", "versionMember"))
                    vm_el.set("{http://www.w3.org/1999/xlink}href", f"#{member_id}")
            
            # Generic attributes
            _write_generic_attributes(feature, obj)
            
            # Version has no geometry
            if obj_eval:
                obj_eval.to_mesh_clear()
            continue

        if (ns_key, local_name) == ("vers", "VersionTransition"):
            # Write core attributes
            _write_core(feature, obj)
            
            # Lifespan attributes
            _write_lifespan_core(feature, obj)
            
            # vers:from - reference to source version
            from_id = obj.get("vers:from_id")
            if from_id:
                from_el = SubElement(feature, Q("vers", "from"))
                from_el.set("{http://www.w3.org/1999/xlink}href", f"#{from_id}")
            
            # vers:to - reference to target version
            to_id = obj.get("vers:to_id")
            if to_id:
                to_el = SubElement(feature, Q("vers", "to"))
                to_el.set("{http://www.w3.org/1999/xlink}href", f"#{to_id}")
            
            # vers:transitionType (optional, e.g., "replace", "insert", "delete")
            transition_type = obj.get("vers:transitionType")
            if transition_type:
                tt_el = SubElement(feature, Q("vers", "transitionType"))
                tt_el.text = str(transition_type).strip()
            
            # vers:reason (optional, free text describing reason for transition)
            reason = obj.get("vers:reason")
            if reason:
                reason_el = SubElement(feature, Q("vers", "reason"))
                reason_el.text = str(reason).strip()
            
            # Generic attributes
            _write_generic_attributes(feature, obj)
            
            # VersionTransition has no geometry
            if obj_eval:
                obj_eval.to_mesh_clear()
            continue

        # Prüfen, ob dieses Objekt aus einer ImplicitGeometry stammt (see note above)
        mesh_orig = obj.data
        try:
            is_implicit = bool(mesh_orig.get("ImplicitGeometry", False))
        except Exception:
            is_implicit = False
        if not is_implicit:
            try:
                eval_mesh = getattr(obj_eval, "data", None)
                if eval_mesh is not None:
                    is_implicit = bool(eval_mesh.get("ImplicitGeometry", False))
                    if is_implicit:
                        mesh_orig = eval_mesh
            except Exception:
                pass

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

        preferred_face_index_by_identity: Dict[tuple[str, str], tuple[int, int]] = {}
        for fidx_pre, poly_pre in enumerate(mesh.polygons):
            face_override_pre = face_overrides.get(fidx_pre)
            try:
                mi_pre = poly_pre.material_index
                mat_pre = face_override_pre.get("mat") if face_override_pre else None
                if mat_pre is None:
                    mat_pre = obj.material_slots[mi_pre].material if 0 <= mi_pre < len(obj.material_slots) else None
            except Exception:
                mat_pre = face_override_pre.get("mat") if face_override_pre else None
            poly_gid_pre, ring_id_pre = _explicit_face_identity(poly_pre, mat_pre)
            if not poly_gid_pre or not ring_id_pre:
                continue
            identity_key = (poly_gid_pre, ring_id_pre)
            score_pre = _duplicate_face_semantic_score(mat_pre, face_override_pre)
            prev = preferred_face_index_by_identity.get(identity_key)
            if prev is None or score_pre > prev[1]:
                preferred_face_index_by_identity[identity_key] = (fidx_pre, score_pre)

        # defaultdict verwenden, damit fehlende Keys automatisch als leere Listen angelegt werden
        geom_buf: Dict[str, List[Dict[str, object]]] = defaultdict(list)

        # Nur für den ID-basierten Weg (ohne get_inner_and_outer_rings):
        # Gruppiert alle Ringe (Faces) pro gml:Polygon-ID.
        poly_groups: Dict[str, dict] = {} if not use_inner_outer_script else {}

        # Ring-Index je Polygon-ID (für saubere _0_, _1_, _2_-IDs)
        ring_index_by_poly: Dict[str, int] = {}
        claimed_poly_ids_by_raw: Dict[str, str] = {}
        claimed_ring_ids_by_raw: Dict[str, str] = {}

        # Importierte Polygon-/Ring-IDs kÃ¶nnen in mehreren exportierten Features erneut
        # auftauchen. Pro Objekt halten wir das raw->claimed Mapping stabil, wÃ¤hrend
        # make_gml_id die Dokument-weite Eindeutigkeit garantiert.
        def _claim_poly_id(raw_poly_id: str) -> str:
            raw_poly_id = str(raw_poly_id or "").strip()
            if not raw_poly_id:
                return _new_gml_id("UUID", used_gml_ids)
            claimed = claimed_poly_ids_by_raw.get(raw_poly_id)
            if claimed:
                return claimed
            claimed = make_gml_id(raw_poly_id, used_gml_ids)
            claimed_poly_ids_by_raw[raw_poly_id] = claimed
            return claimed

        def _claim_ring_id(raw_ring_id: str) -> str:
            raw_ring_id = str(raw_ring_id or "").strip()
            if not raw_ring_id:
                return _new_gml_id("UUID", used_gml_ids)
            claimed = claimed_ring_ids_by_raw.get(raw_ring_id)
            if claimed:
                return claimed
            claimed = make_gml_id(raw_ring_id, used_gml_ids)
            claimed_ring_ids_by_raw[raw_ring_id] = claimed
            return claimed

        bbox_coords_abs = []
        ptx_by_image: Dict[str, List[dict]] = {}
        gtx_by_image: Dict[str, List[dict]] = {}
        x3d_by_key: Dict[Tuple[str, Tuple[float,float,float,float]], List[str]] = {}
        exported_faces = set()
        seen_export_poly_ring_keys: set[tuple[str, str]] = set()
        probe_abs_cur: List[Tuple[float, float, float]] = []

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
                    # Unterstütze die importierten Varianten: 'SurfaceTyp', 'surface_type' und 'Typ'
                    v = mat.get("SurfaceTyp") or mat.get("surface_type") or mat.get("Typ")
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
                        "BridgeConstructiveElement",
                        "BridgeInstallation",
                        "BridgeFurniture",
                        "BridgePart",
                        "Bridge",
                        # Tunnel
                        "TunnelConstructiveElement",
                        "TunnelInstallation",
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
                "BridgeInstallation", "IntBridgeInstallation", "BridgeFurniture",
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

            # Imported hierarchical Buildings can keep a visual OuterShell that mirrors
            # Storey/Room polygons for Blender editing. On export these child polygons
            # must only be written once, under their Storey/BuildingRoom feature.
            try:
                gid_raw = poly.get("gml_id", None)
            except Exception:
                gid_raw = None

            poly_gid = ""
            if gid_raw:
                poly_gid = str(gid_raw).strip()

            if not poly_gid and mat:
                try:
                    m_gid = mat.get("gml_polygon_id", None)
                except Exception:
                    m_gid = None
                if m_gid:
                    poly_gid = str(m_gid).strip()

            raw_poly_gid = poly_gid

            if _hier_parent and poly_gid and poly_gid in descendant_feature_poly_ids:
                continue

            poly_gid = _claim_poly_id(raw_poly_gid)

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
                    uvs_ext = _order_uvs_for_loop_vertices(mesh, poly.loop_indices, ring_xyz_local, uvs_ext, world_matrix=obj_eval.matrix_world)
                except Exception:
                    pass
                # CityGML ParameterizedTexture erwartet (u,v) im Texturraum.
                # Blender-UVs sind bereits Texturkoordinaten, aber einige Pipelines
                # (z.B. aus Fremdimporten) haben die V-Achse invertiert. Der Importer
                # arbeitet robust über die tatsächliche Zuordnung Ring->Loops; beim
                # Export korrigieren wir daher optional eine durchgängige V-Flip-
                # Konvention, falls das Custom-Property gesetzt ist.
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
                            ir_uv = _order_uvs_for_loop_vertices(mesh, ip.loop_indices, ir_xyz_local, ir_uv, world_matrix=obj_eval.matrix_world)
                        except Exception:
                            pass

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

            # Ring-ID bevorzugt aus Material übernehmen (Import-Roundtrip),
            # sonst kanonisch nummerieren: _0_, _1_, usw.
            _mat_ring_id = ""
            if mat:
                try:
                    _mr = mat.get("gml_ring_id", None)
                    if _mr:
                        _mat_ring_id = str(_mr).strip()
                except Exception:
                    pass

            raw_ring_id = _mat_ring_id
            ring_lookup_id = ""
            if _mat_ring_id:
                preferred = preferred_face_index_by_identity.get((raw_poly_gid, _mat_ring_id))
                if preferred is not None and preferred[0] != fidx:
                    continue
                ring_lookup_id = _mat_ring_id
            if use_inner_outer_script:
                idx = ring_index_by_poly.get(poly_gid, 0)
                ring_id = _claim_ring_id(f"{poly_gid}_{idx}_")
                ring_index_by_poly[poly_gid] = idx + 1
                if not ring_lookup_id:
                    ring_lookup_id = ring_id
            elif _mat_ring_id:
                ring_id = _claim_ring_id(_mat_ring_id)
            else:
                idx = ring_index_by_poly.get(poly_gid, 0)
                ring_id = _claim_ring_id(f"{poly_gid}_{idx}_")
                ring_index_by_poly[poly_gid] = idx + 1
                ring_lookup_id = ring_id

            face_identity_key = (poly_gid, ring_id)
            if raw_poly_gid or raw_ring_id:
                face_identity_key = (raw_poly_gid or poly_gid, raw_ring_id or ring_id)
            if ring_id and face_identity_key in seen_export_poly_ring_keys:
                continue
            if ring_id:
                seen_export_poly_ring_keys.add(face_identity_key)

            # Interior-Ring-IDs: bevorzugt aus Material übernehmen, sonst kanonisch pro Polygon durchzählen
            interior_ring_ids = []
            interior_ring_lookup_ids = []
            deduped_inner_ids = []
            deduped_interior_rings_xyz = []
            deduped_interior_uvs = []
            deduped_interior_ring_lookup_ids = []
            for iid, ir_xyz, ir_uv in zip(inner_ids, interior_rings_xyz, interior_uvs):
                inner_override = face_overrides.get(iid)
                mat_i = inner_override.get("mat") if inner_override else None
                if mat_i is None:
                    mat_i = obj.data.materials[mesh.polygons[iid].material_index] if obj and obj.data else None
                rid_inner = ""
                try:
                    if mat_i:
                        _rid_inner = mat_i.get("gml_ring_id", None)
                        if _rid_inner:
                            rid_inner = str(_rid_inner).strip()
                except Exception:
                    rid_inner = ""
                raw_rid_inner = rid_inner
                if rid_inner:
                    preferred_inner = preferred_face_index_by_identity.get((raw_poly_gid, rid_inner))
                    if preferred_inner is not None and preferred_inner[0] != iid:
                        continue
                if use_inner_outer_script or not rid_inner:
                    idx_inner = ring_index_by_poly.get(poly_gid, 0)
                    rid_inner = f"{poly_gid}_{idx_inner}_"
                    ring_index_by_poly[poly_gid] = idx_inner + 1
                rid_inner = _claim_ring_id(rid_inner)
                inner_identity_key = (raw_poly_gid or poly_gid, raw_rid_inner or rid_inner)
                if rid_inner and inner_identity_key in seen_export_poly_ring_keys:
                    continue
                if rid_inner:
                    seen_export_poly_ring_keys.add(inner_identity_key)
                deduped_inner_ids.append(iid)
                deduped_interior_rings_xyz.append(ir_xyz)
                deduped_interior_uvs.append(ir_uv)
                deduped_interior_ring_lookup_ids.append(raw_rid_inner or rid_inner)
                interior_ring_ids.append(rid_inner)
            inner_ids = deduped_inner_ids
            interior_rings_xyz = deduped_interior_rings_xyz
            interior_uvs = deduped_interior_uvs
            interior_ring_lookup_ids = deduped_interior_ring_lookup_ids

            # 1) Textur-Belegung (Parameterized / Georeferenced) bestimmen
            skip_texture_export = bool(face_override and face_override.get("skip_texture_export"))
            img_path_ext = None if skip_texture_export else _first_image_path_from_export_material(obj, poly.material_index, mat)
            has_texture = False
            ext_uv_sign = 0

            if export_ptx and img_path_ext and uvs_ext:
                # Orientierung des 3D-Rings bestimmen (für spätere UV-Korrektur)
                ori_ext = _ring_area_sign_xyz(ring_xyz) if ring_xyz else 0
                ext_uv_sign = _uv_ring_sign(uvs_ext)

                entry_uvs = list(uvs_ext)
                expected_sign = _expected_uv_ring_sign(obj, mat, ring_lookup_id or ring_id, entry_uvs)
                uv_ring_corrected = False
                if is_interior_helper_surface:
                    entry_uvs, uv_ring_corrected = _orient_uv_ring_to_expected_sign(
                        entry_uvs,
                        expected_sign,
                    )

                entry = {
                    "poly_id": interior_helper_target_poly_id or poly_gid,
                    "ring_id": ring_id,
                    "uvs": entry_uvs,
                    "is_interior": True if is_interior_helper_surface else False,
                }
                if expected_sign in (-1, 1):
                    entry["expected_sign"] = expected_sign
                # Keep `ori` only for interior rings; the exterior ring must never be reversed
                # based on a normal sign, otherwise UV order flips compared to import.gml.
                # (Interior faces can have inverted normals relative to exterior surfaces.)
                # Roundtrip: preserve original TexCoordList start point (first UV pair) from import.
                try:
                    rid_this = _norm_id(ring_lookup_id or ring_id)
                    start_uv = None
                    # Prefer ring-specific mapping stored on object.data (import writes to object.data).
                    try:
                        d = None
                        if obj is not None:
                            d = obj.get("cgml3_uv_start_by_ring", None)
                        if d is None and obj is not None and getattr(obj, "data", None) is not None:
                            d = obj.data.get("cgml3_uv_start_by_ring", None)
                        if d is not None and rid_this:
                            start_uv = d.get(rid_this, None)
                    except Exception:
                        start_uv = None
                    # Fallback: per-material (legacy)
                    if start_uv is None:
                        start_uv = mat.get("cgml3_uv_start", None) if mat else None
                        rid_mat = _norm_id(mat.get("gml_ring_id", "")) if mat else ""
                        if not (start_uv is not None and rid_mat and rid_this and rid_mat == rid_this):
                            start_uv = None
                    if start_uv is None and uv_ring_corrected and uvs_ext:
                        start_uv = _as_uv_tuple2(uvs_ext[0])
                    if start_uv is not None:
                        entry["uv_start"] = (float(start_uv[0]), float(start_uv[1]))
                except Exception:
                    pass

                ptx_by_image.setdefault(img_path_ext, []).append(entry)
                if export_debug and ring_id:
                    queued_ptx_ring_counts[ring_id] = queued_ptx_ring_counts.get(ring_id, 0) + 1
                    if is_interior_helper_surface:
                        _dbg(
                            "queued interior-helper ptx "
                            f"obj={obj.name} source_poly_id={poly_gid} target_poly_id={entry['poly_id']} "
                            f"ring_id={ring_id} queue_count={queued_ptx_ring_counts[ring_id]} image={img_path_ext}"
                        )
                    else:
                        _dbg(
                            "queued exterior ptx "
                            f"obj={obj.name} poly_id={poly_gid} ring_id={ring_id} "
                            f"queue_count={queued_ptx_ring_counts[ring_id]} image={img_path_ext}"
                        )
                has_texture = True
            elif export_ptx and img_path_ext and not mesh.uv_layers.active and ring_xyz and export_gtx:
                # Fallback: sehr einfache GeoreferencedTexture, nur wenn keine UVs vorhanden
                x0, y0, _ = ring_xyz[0]
                gtx_by_image.setdefault(img_path_ext, []).append(
                    {"poly_id": poly_gid, "refpt": (x0, y0), "M": (1.0, 0.0, 0.0, 1.0)}
                )
                has_texture = True

            if export_ptx:
                # Innenringe
                for (iid, rid, rid_lookup, uv_i, ir_xyz) in zip(
                    inner_ids,
                    interior_ring_ids,
                    interior_ring_lookup_ids,
                    interior_uvs,
                    interior_rings_xyz,
                ):
                    inner_override = face_overrides.get(iid)
                    mat_i = inner_override.get("mat") if inner_override else None
                    if mat_i is None:
                        mat_i = obj.data.materials[mesh.polygons[iid].material_index] if obj and obj.data else None
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
                        entry_i = {"poly_id": poly_gid, "ring_id": rid, "uvs": corrected_uv_i, "ori": ori_int, "is_interior": True}
                        expected_sign_i = _expected_uv_ring_sign(obj, mat_i, rid_lookup or rid, corrected_uv_i)
                        if expected_sign_i in (-1, 1):
                            entry_i["expected_sign"] = expected_sign_i
                        try:
                            rid_this_i = _norm_id(rid_lookup or rid)
                            start_uv_i = None
                            try:
                                d = None
                                if obj is not None:
                                    d = obj.get("cgml3_uv_start_by_ring", None)
                                if d is None and obj is not None and getattr(obj, "data", None) is not None:
                                    d = obj.data.get("cgml3_uv_start_by_ring", None)
                                if d is not None and rid_this_i:
                                    start_uv_i = d.get(rid_this_i, None)
                            except Exception:
                                start_uv_i = None
                            if uv_ring_corrected and corrected_uv_i:
                                start_uv_i = corrected_uv_i[0]
                            if start_uv_i is None:
                                start_uv_i = mat_i.get("cgml3_uv_start", None) if mat_i else None
                                rid_mat_i = _norm_id(mat_i.get("gml_ring_id", "")) if mat_i else ""
                                if not (start_uv_i is not None and rid_mat_i and rid_this_i and rid_mat_i == rid_this_i):
                                    start_uv_i = None
                            if start_uv_i is None and corrected_uv_i:
                                start_uv_i = corrected_uv_i[0]
                            if start_uv_i is not None:
                                entry_i["uv_start"] = (float(start_uv_i[0]), float(start_uv_i[1]))
                        except Exception:
                            pass
                        if export_debug:
                            queued_ptx_ring_counts[rid] = queued_ptx_ring_counts.get(rid, 0) + 1
                            _dbg(
                                "queued interior ptx "
                                f"obj={obj.name} poly_id={poly_gid} ring_id={rid} "
                                f"uv_count={len(corrected_uv_i)} queue_count={queued_ptx_ring_counts[rid]} image={img_path_int}"
                            )
                        ptx_by_image.setdefault(img_path_int, []).append(entry_i)
                        has_texture = True

            # 2) X3DMaterial NUR für Faces ohne Textur
            if export_x3d and not has_texture:
                rgba = extract_base_color_rgba(mat)
                params = extract_x3d_params_from_material(mat)
                x3d_by_key.setdefault((st, rgba, tuple(sorted(params.items()))), []).append(poly_gid)

            # Surface- und MultiSurface-IDs aus Material übernehmen
            surf_id = ""
            ms_id = ""
            cs_id = ""  # CompositeSurface-ID
            is_ms_member = False
            filling_parent_sid = ""  # NEU: Parent-Surface-ID für fillingSurface
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

            surf_id = ensure_xs_id(surf_id, prefix="ID")
            ms_id = ensure_xs_id(ms_id, prefix="ID")
            if filling_parent_sid:
                filling_parent_sid = ensure_xs_id(filling_parent_sid, prefix="ID")

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

            def _remap_appearance_targets_for_merged_interiors(poly_id_map: Dict[str, str]):
                if not poly_id_map:
                    return

                for entries in ptx_by_image.values():
                    for entry in entries:
                        try:
                            pid = str(entry.get("poly_id") or "").strip()
                        except Exception:
                            pid = ""
                        if pid in poly_id_map:
                            entry["poly_id"] = poly_id_map[pid]
                            entry["is_interior"] = True

                for entries in gtx_by_image.values():
                    for entry in entries:
                        try:
                            pid = str(entry.get("poly_id") or "").strip()
                        except Exception:
                            pid = ""
                        if pid in poly_id_map:
                            entry["poly_id"] = poly_id_map[pid]

                for key in list(x3d_by_key.keys()):
                    kept_ids = []
                    for pid in x3d_by_key.get(key, []):
                        pid_txt = str(pid or "").strip()
                        if pid_txt in poly_id_map:
                            continue
                        kept_ids.append(pid)
                    if kept_ids:
                        x3d_by_key[key] = kept_ids
                    else:
                        x3d_by_key.pop(key, None)

            def _canonicalize_poly_group_ring_ids():
                if not poly_groups:
                    return

                ring_maps_by_poly: Dict[str, Dict[str, str]] = {}
                for _poly_id, _grp in poly_groups.items():
                    poly_txt = str(_poly_id or "").strip()
                    if not poly_txt:
                        continue

                    ext_new = f"{poly_txt}_0_"
                    ring_map: Dict[str, str] = {}

                    ext_old = str(_grp.get("ext_id") or "").strip()
                    if ext_old:
                        ring_map[_norm_id(ext_old)] = ext_new
                    ring_map[_norm_id(ext_new)] = ext_new
                    _grp["ext_id"] = ext_new

                    int_new_ids: List[str] = []
                    for _idx, _old in enumerate(list(_grp.get("int_ids") or []), start=1):
                        int_new = f"{poly_txt}_{_idx}_"
                        old_txt = str(_old or "").strip()
                        if old_txt:
                            ring_map[_norm_id(old_txt)] = int_new
                        ring_map[_norm_id(int_new)] = int_new
                        int_new_ids.append(int_new)
                    _grp["int_ids"] = int_new_ids

                    ring_maps_by_poly[poly_txt] = ring_map

                if not ring_maps_by_poly:
                    return

                for entries in ptx_by_image.values():
                    for entry in entries:
                        try:
                            pid = str(entry.get("poly_id") or "").strip()
                        except Exception:
                            pid = ""
                        ring_map = ring_maps_by_poly.get(pid)
                        if not ring_map:
                            continue
                        try:
                            rid = str(entry.get("ring_id") or "").strip()
                        except Exception:
                            rid = ""
                        rid_norm = _norm_id(rid)
                        mapped = ring_map.get(rid_norm)
                        if mapped:
                            entry["ring_id"] = mapped

            # ---------------------------------------------------------------
            # Interior-Ring-Polygone mit ExteriorPolyId in ihr Exterior-Polygon mergen.
            # Der Importer legt für Interior-Ringe eigene gml_polygon_id-Einträge an
            # (z.B. "{OriginalPolyId}_{N}"). Diese werden hier als gml:interior
            # in das zugehörige Exterior-Polygon eingefügt und danach entfernt.
            # ---------------------------------------------------------------
            _interior_to_remove = []
            _merged_interior_poly_targets: Dict[str, str] = {}
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
                    _interior_to_remove.append(_pg_id)
            for _pg_id in _interior_to_remove:
                del poly_groups[_pg_id]
            _remap_appearance_targets_for_merged_interiors(_merged_interior_poly_targets)
            _canonicalize_poly_group_ring_ids()

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

        # FME visibility fallback:
        # If no geometry ended up in geom_buf, the feature becomes xml_no_geom in FME.
        # For CityFurniture and Vegetation features, write a simple lod0Point at the object
        # location as a pragmatic fallback (so at least a geometry exists).
        try:
            has_any_geom = any(geom_buf.values())
        except Exception:
            has_any_geom = False

        # NOTE: ImplicitGeometry branch below writes lod2ImplicitRepresentation. We only want this
        # fallback for explicit-geometry features that truly produced no geometry.
        if (not is_implicit) and (not has_any_geom) and ns_key in ("frn", "veg"):
            try:
                loc = obj.matrix_world.translation
                coords_local = np.asarray([(float(loc.x), float(loc.y), float(loc.z))], dtype=np.float64)
                coords_tgt, origin_tgt = trf.transform_with_origin(coords_local, (ox, oy, oz))
                x = float(coords_tgt[0][0]) + origin_tgt[0]
                y = float(coords_tgt[0][1]) + origin_tgt[1]
                z = float(coords_tgt[0][2]) + origin_tgt[2]

                lod0_point = SubElement(feature, _C("lod0Point"))
                point = SubElement(lod0_point, Q("gml", "Point"))
                point.set(f"{{{NS['gml']}}}id", _new_gml_id("UUID", used_gml_ids))
                pos = SubElement(point, Q("gml", "pos"))
                pos.set("srsDimension", "3")
                pos.text = f"{fmt(x)} {fmt(y)} {fmt(z)}"

                bbox_coords_abs.append((x, y, z))
            except Exception:
                pass

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

        # CityGML 3.0 life-cycle attributes (creationDate, terminationDate, validFrom, validTo)
        _write_lifespan_core(feature, _hier_parent or obj)

        out_dir = os.path.dirname(os.path.abspath(filepath))

        def _remap_image_uri(src_path: str) -> str:
            """Copy texture once per export session; reuse rel_uri for same source path."""
            if not src_path:
                return ""
            if src_path in image_uri_map_global:
                return image_uri_map_global[src_path]
            _abs_dest, rel_uri = _ensure_export_texture(src_path, out_dir, used_tex_names_global)
            if rel_uri:
                image_uri_map_global[src_path] = rel_uri
            return rel_uri or ""

        # Texturbilder kopieren und direkt ihre spätere imageURI (rel_uri) als Key verwenden
        remapped_ptx: Dict[str, List[dict]] = {}
        if export_ptx and ptx_by_image:
            for src_img, entries in ptx_by_image.items():
                rel_uri = _remap_image_uri(src_img)
                if rel_uri:
                    remapped_ptx.setdefault(rel_uri, []).extend(entries)

        remapped_gtx: Dict[str, List[dict]] = {}
        if export_gtx and gtx_by_image:
            for src_img, entries in gtx_by_image.items():
                rel_uri = _remap_image_uri(src_img)
                if rel_uri:
                    remapped_gtx.setdefault(rel_uri, []).extend(entries)

        # Texturierte Polygone nicht zusätzlich mit X3D-Material beschreiben, damit Texturen sichtbar bleiben
        textured_ids = set()
        for entries in ptx_by_image.values():
            textured_ids.update(e["poly_id"] for e in entries)
        for entries in gtx_by_image.values():
            textured_ids.update(e["poly_id"] for e in entries)
        if textured_ids and x3d_by_key:
            for key in list(x3d_by_key.keys()):
                x3d_by_key[key] = [pid for pid in x3d_by_key[key] if pid not in textured_ids]
                if not x3d_by_key[key]:
                    x3d_by_key.pop(key, None)

        have_x3d = export_x3d and bool(x3d_by_key)
        have_tex = (export_ptx and bool(remapped_ptx)) or (export_gtx and bool(remapped_gtx))
        pending_feature_appearance = None

        # Appearance für dieses Feature:
        # - normale Objekte: bisherige Logik
        # - ImplicitGeometry-Klone: nur Verknüpfung auf Appearance des Prototyps
        app_appear = None

        if is_implicit and not is_imp_prototype:
            # Klon eines ImplicitGeometry-Prototyps: Appearance vom Prototyp übernehmen,
            # ggf. nur minimalen Default-Knoten anlegen, falls keine globale Appearance existiert.
            app_id = None
            if isinstance(meta_for_mesh, dict):
                app_id = meta_for_mesh.get('app_id')
            # FME compatibility: avoid feature-local <core:appearance> for clones.
            pending_feature_appearance = None
            app_appear = None
        else:
            # FME compatibility (CityGML 3): export appearance as CityModel-level <appearanceMember>
            # like import__v3.gml, and avoid feature-local <core:appearance>.
            pending_feature_appearance = None
            if have_x3d or have_tex:
                # CityGML 3 core namespace is the default namespace on <CityModel>,
                # so appearanceMember is written without prefix.
                app_member = SubElement(root, "appearanceMember")
                app_appear = SubElement(app_member, Q("app","Appearance"))
                app_appear.set(GML_ID, f"ID_{uuid4().hex}")
                SubElement(app_appear, Q("app","theme")).text = "Default"
            else:
                app_appear = None

            # Nur wenn ein Appearance-Element existiert, Material/Textur-Informationen schreiben
            if app_appear is not None:
                if export_x3d and x3d_by_key:
                    by_surface: Dict[str, Dict[Tuple[float,float,float,float], Dict[str,object]]] = {}
                    for (surf, rgba, params_tuple), ids in x3d_by_key.items():
                        params = dict(params_tuple)
                        by_surface.setdefault(surf, {})[rgba] = {"poly_ids": ids, "params": params}
                    for rgba_map in by_surface.values():
                        add_x3d_materials(app_appear, rgba_map, double_sided=double_sided)

                if export_ptx and remapped_ptx:
                    if export_debug:
                        _dbg(
                            "writing parameterized textures "
                            f"images={len(remapped_ptx)} entries={sum(len(v) for v in remapped_ptx.values())}"
                        )
                    add_parameterized_textures(
                        app_appear,
                        remapped_ptx,
                        double_sided=double_sided,
                        processed_interior_ring_uvs=processed_interior_ring_uvs,
                        export_debug=export_debug,
                    )

                if export_gtx and remapped_gtx:
                    add_georeferenced_textures(app_appear, remapped_gtx)

        # If we deferred an inline feature-local appearance, it doesn't exist yet here.
        # It will be created after the boundary blocks, so defer writing appearance content too.
        pending_feature_appearance_content = None
        if pending_feature_appearance and app_appear is None and (have_x3d or have_tex):
            pending_feature_appearance_content = {
                "x3d_by_key": x3d_by_key if export_x3d else None,
                "ptx": remapped_ptx if export_ptx else None,
                "gtx": remapped_gtx if export_gtx else None,
            }


        def _write_generics(feature_el, obj_):
            """Exportiert Blender-Custom-Properties als CityGML-3-Generics.

            Struktur gemäß CityGML 3.0:
              <core:genericAttribute>
                <gen:StringAttribute>
                  <gen:name>foo</gen:name>
                  <gen:value>bar</gen:value>
                </gen:StringAttribute>
              </core:genericAttribute>
            """
            raw = _load_json_chunks(obj_, "cgml3_generic_attributes_json")
            if isinstance(raw, dict) and raw:
                items_iter = list(raw.items())
                allow_underscore = True
            else:
                items_iter = list(getattr(obj_, "items", lambda: [])())
                allow_underscore = False

            for k, v in items_iter:
                # interne und spezifische Keys nicht als generische Attribute exportieren
                if k in INTERNAL_ATTR_KEYS or k in SPECIFIC_ATTR_KEYS:
                    continue
                if _should_skip_generic_attr_key(k):
                    continue
                if str(k).startswith("_") and not allow_underscore:
                    continue

                # Skip empty values
                if v is None:
                    continue
                if isinstance(v, str) and not v.strip():
                    continue

                try:
                    ga = SubElement(feature_el, _C("genericAttribute"))

                    if isinstance(v, str):
                        v_str = v.strip()
                        # DateTime first (more specific)
                        if v_str and re.match(r"^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}(:\\d{2}(?:\\.\\d+)?)?(Z|[+-]\\d{2}:\\d{2})?$", v_str):
                            a = SubElement(ga, Q("gen", "DateTimeAttribute"))
                            SubElement(a, Q("gen", "name")).text = str(k)
                            SubElement(a, Q("gen", "value")).text = v_str
                        elif v_str and re.match(r"^\\d{4}-\\d{2}-\\d{2}$", v_str):
                            a = SubElement(ga, Q("gen", "DateAttribute"))
                            SubElement(a, Q("gen", "name")).text = str(k)
                            SubElement(a, Q("gen", "value")).text = v_str
                        else:
                            a = SubElement(ga, Q("gen", "StringAttribute"))
                            SubElement(a, Q("gen", "name")).text = str(k)
                            SubElement(a, Q("gen", "value")).text = v_str

                    elif isinstance(v, bool):
                        a = SubElement(ga, Q("gen", "IntAttribute"))
                        SubElement(a, Q("gen", "name")).text = str(k)
                        SubElement(a, Q("gen", "value")).text = "1" if v else "0"

                    elif isinstance(v, int):
                        a = SubElement(ga, Q("gen", "IntAttribute"))
                        SubElement(a, Q("gen", "name")).text = str(k)
                        SubElement(a, Q("gen", "value")).text = str(v)

                    elif isinstance(v, (float,)):
                        a = SubElement(ga, Q("gen", "DoubleAttribute"))
                        SubElement(a, Q("gen", "name")).text = str(k)
                        SubElement(a, Q("gen", "value")).text = fmt(v)

                    else:
                        a = SubElement(ga, Q("gen", "StringAttribute"))
                        SubElement(a, Q("gen", "name")).text = str(k)
                        SubElement(a, Q("gen", "value")).text = str(v)

                except Exception:
                    pass

        # Defer writing feature-local appearance until after boundary blocks were created.
        def _flush_pending_feature_appearance(feature_el):
            nonlocal app_appear, pending_feature_appearance, pending_feature_appearance_content
            if not pending_feature_appearance:
                return
            kind, payload = pending_feature_appearance
            pending_feature_appearance = None
            if kind == "link":
                core_app = SubElement(feature_el, _C("appearance"))
                core_app.set(Q("xlink", "href"), f"#{payload}")
                return
            if kind == "inline":
                core_app = SubElement(feature_el, _C("appearance"))
                app_appear = SubElement(core_app, Q("app", "Appearance"))
                app_appear.set(GML_ID, f"ID_{uuid4().hex}")
                SubElement(app_appear, Q("app", "theme")).text = "Default"

                # Now that the inline <app:Appearance> exists, write its content.
                if pending_feature_appearance_content:
                    try:
                        if export_x3d and pending_feature_appearance_content.get("x3d_by_key"):
                            by_surface: Dict[str, Dict[Tuple[float,float,float,float], Dict[str,object]]] = {}
                            for (surf, rgba, params_tuple), ids in pending_feature_appearance_content["x3d_by_key"].items():
                                params = dict(params_tuple)
                                by_surface.setdefault(surf, {})[rgba] = {"poly_ids": ids, "params": params}
                            for rgba_map in by_surface.values():
                                add_x3d_materials(app_appear, rgba_map, double_sided=double_sided)
                        if export_ptx and pending_feature_appearance_content.get("ptx"):
                            if export_debug:
                                _dbg(
                                    "writing deferred parameterized textures "
                                    f"images={len(pending_feature_appearance_content['ptx'])} "
                                    f"entries={sum(len(v) for v in pending_feature_appearance_content['ptx'].values())}"
                                )
                            add_parameterized_textures(
                                app_appear,
                                pending_feature_appearance_content["ptx"],
                                double_sided=double_sided,
                                processed_interior_ring_uvs=processed_interior_ring_uvs,
                                export_debug=export_debug,
                            )
                        if export_gtx and pending_feature_appearance_content.get("gtx"):
                            add_georeferenced_textures(app_appear, pending_feature_appearance_content["gtx"])
                    except Exception:
                        pass
                    pending_feature_appearance_content = None
                return
                # optionale interne / Hilfs-Keys ausfiltern
                if str(k).startswith("_"):
                    pass

                try:
                    ga = SubElement(feature_el, _C("genericAttribute"))

                    # String → gen:StringAttribute
                    if isinstance(v, str):
                        a = SubElement(ga, Q("gen", "StringAttribute"))
                        SubElement(a, Q("gen", "name")).text = str(k)
                        SubElement(a, Q("gen", "value")).text = v

                    # bool/int → gen:IntAttribute
                    elif isinstance(v, bool):
                        a = SubElement(ga, Q("gen", "IntAttribute"))
                        SubElement(a, Q("gen", "name")).text = str(k)
                        SubElement(a, Q("gen", "value")).text = "1" if v else "0"

                    elif isinstance(v, int):
                        a = SubElement(ga, Q("gen", "IntAttribute"))
                        SubElement(a, Q("gen", "name")).text = str(k)
                        SubElement(a, Q("gen", "value")).text = str(v)

                    # float → gen:DoubleAttribute
                    elif isinstance(v, (float,)):
                        a = SubElement(ga, Q("gen", "DoubleAttribute"))
                        SubElement(a, Q("gen", "name")).text = str(k)
                        SubElement(a, Q("gen", "value")).text = fmt(v)

                    # Fallback: alles andere als StringAttribute
                    else:
                        a = SubElement(ga, Q("gen", "StringAttribute"))
                        SubElement(a, Q("gen", "name")).text = str(k)
                        SubElement(a, Q("gen", "value")).text = str(v)

                except Exception:
                    pass
        
        def _write_filling_elements(surf_el, surf_id, lod="2"):
            """Exportiert Window/Door Kinder einer Surface als con:filling Elements.
            
            Args:
                surf_el: Das Surface-Element (con:WallSurface, con:RoofSurface, etc.)
                surf_id: gml:id der Surface (für Blender-Objekt-Lookup)
                lod: LOD-Level für Geometrie
            """
            if not surf_id:
                return
            
            # Finde das Blender-Objekt, das diese Surface repräsentiert
            # Normalerweise haben Surfaces keine eigenen Blender-Objekte,
            # aber Window/Door sind als Kinder des Parent-Features organisiert
            # Wir müssen alle Mesh-Objekte durchsuchen, die Window/Door sind
            # und als Parent die Surface-ID haben
            
            filling_objs = []
            for obj in bpy.data.objects:
                if not object_is_viewport_visible(obj, context):
                    continue
                if obj.type != "MESH":
                    continue
                    
                # Check if this is a Window or Door
                feat_type = obj.get("cgml3_feature")
                if feat_type not in ("Window", "Door"):
                    continue
                
                # Check if parent references this surface
                parent_id = obj.get("gml_parent")
                if parent_id == surf_id:
                    filling_objs.append(obj)
            
            # Export each filling element
            for fill_obj in filling_objs:
                feat_type = fill_obj.get("cgml3_feature")
                fill_id = _feature_id(fill_obj)
                
                # Create con:filling wrapper
                filling_wrapper = SubElement(surf_el, Q("con", "fillingSurface"))
                
                # Create con:Window or con:Door element
                filling_elem = SubElement(filling_wrapper, Q("con", feat_type))
                filling_elem.set(f"{{{NS['gml']}}}id", fill_id)
                
                # Add gml:name
                if fill_obj.name and fill_obj.name != fill_id:
                    name_el = SubElement(filling_elem, Q("gml", "name"))
                    name_el.text = fill_obj.name
                
                # Add lifespan attributes
                _write_lifespan_attributes(filling_elem, fill_obj)
                
                # Add Construction Module attributes (class, function, usage)
                _write_specific_attributes(filling_elem, fill_obj)
                
                # Add generic attributes
                _write_generic_attributes(filling_elem, fill_obj)
                
                # Add address for Door (optional)
                if feat_type == "Door":
                    _write_address(filling_elem, fill_obj, "bldg")
                
                # Export geometry (lod*MultiSurface)
                ox, oy, oz = _resolve_export_offset(context, fill_obj)
                lod_key = f"lod{lod}MultiSurface"
                
                # Create lod*MultiSurface element
                lod_geom = SubElement(filling_elem, Q("con", lod_key))
                multi_surf = SubElement(lod_geom, Q("gml", "MultiSurface"))
                multi_surf.set(f"{{{NS['gml']}}}id", _new_gml_id("UUID", used_gml_ids))
                
                # Get evaluated mesh
                try:
                    fill_eval = fill_obj.evaluated_get(depsgraph)
                    fill_mesh = fill_eval.data if fill_eval else fill_obj.data
                except:
                    fill_mesh = fill_obj.data
                
                if fill_mesh and hasattr(fill_mesh, 'polygons'):
                    # Export all polygons as surfaceMember
                    for poly in fill_mesh.polygons:
                        surf_member = SubElement(multi_surf, Q("gml", "surfaceMember"))
                        poly_elem = SubElement(surf_member, Q("gml", "Polygon"))
                        poly_elem.set(f"{{{NS['gml']}}}id", _new_gml_id("UUID", used_gml_ids))
                        
                        # Exterior ring
                        exterior = SubElement(poly_elem, Q("gml", "exterior"))
                        linear_ring = SubElement(exterior, Q("gml", "LinearRing"))
                        
                        # Get vertex coordinates (transformed)
                        ring_xyz_local = []
                        for li in poly.loop_indices:
                            vi = fill_mesh.loops[li].vertex_index
                            vco = fill_obj.matrix_world @ fill_mesh.vertices[vi].co
                            ring_xyz_local.append((float(vco.x), float(vco.y), float(vco.z)))
                        ring_xyz_local = _ensure_closed2d(ring_xyz_local)
                        
                        origin_src = (ox, oy, oz)
                        ring_xyz_arr = np.asarray(ring_xyz_local, dtype=np.float64)
                        
                        # Transform
                        ring_tgt_local, origin_tgt = trf.transform_with_origin(ring_xyz_arr, origin_src)
                        ring_xyz_abs = (ring_tgt_local + np.asarray(origin_tgt, dtype=np.float64)).tolist()
                        
                        # Write posList
                        pos_list = SubElement(linear_ring, Q("gml", "posList"))
                        pos_list.set("srsDimension", "3")
                        flat = []
                        for x, y, z in ring_xyz_abs:
                            flat.extend([fmt(x), fmt(y), fmt(z)])
                            all_bbox_coords.append((x, y, z))
                        pos_list.text = " ".join(flat)
                
                # Cleanup evaluated mesh
                try:
                    if fill_eval:
                        fill_eval.to_mesh_clear()
                except:
                    pass
        
        def _write_surface_generics_on_surface(surf_el, mat):
            """Exportiert Material-Custom-Properties direkt als genericAttribute
            innerhalb eines con:*Surface-Elements.
            """
            if mat is None:
                return

            # Prefer lossless roundtrip dict from importer if present.
            raw_surf = None
            try:
                raw_surf = mat.get("cgml3_surface_generic_attributes", None)
            except Exception:
                raw_surf = None

            if isinstance(raw_surf, dict) and raw_surf:
                items_iter = list(raw_surf.items())
            else:
                items_iter = list(getattr(mat, "items", lambda: [])())

            attr_items = []
            for k, v in items_iter:
                # interne / spezifische Keys nicht als generische Attribute exportieren
                if k in INTERNAL_ATTR_KEYS or k in SPECIFIC_ATTR_KEYS:
                    continue
                if k in MATERIAL_INTERNAL_ATTR_KEYS:
                    continue
                if _should_skip_generic_attr_key(k):
                    continue
                if str(k).startswith("_"):
                    # For raw dict roundtrip we *do* want underscore keys (e.g. "_lod").
                    if not (isinstance(raw_surf, dict) and raw_surf):
                        continue
                if v is None:
                    continue
                if isinstance(v, str) and not v.strip():
                    continue
                attr_items.append((k, v))

            if not attr_items:
                return

            for (k, v) in attr_items:
                try:
                    ga = SubElement(surf_el, _C("genericAttribute"))

                    if isinstance(v, str):
                        v_str = v.strip()
                        if not v_str:
                            continue
                        if re.match(r"^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}(:\\d{2}(?:\\.\\d+)?)?(Z|[+-]\\d{2}:\\d{2})?$", v_str):
                            a = SubElement(ga, Q("gen", "DateTimeAttribute"))
                            SubElement(a, Q("gen", "name")).text = str(k)
                            SubElement(a, Q("gen", "value")).text = v_str
                        elif re.match(r"^\\d{4}-\\d{2}-\\d{2}$", v_str):
                            a = SubElement(ga, Q("gen", "DateAttribute"))
                            SubElement(a, Q("gen", "name")).text = str(k)
                            SubElement(a, Q("gen", "value")).text = v_str
                        else:
                            a = SubElement(ga, Q("gen", "StringAttribute"))
                            SubElement(a, Q("gen", "name")).text = str(k)
                            SubElement(a, Q("gen", "value")).text = v_str

                    elif isinstance(v, bool):
                        a = SubElement(ga, Q("gen", "IntAttribute"))
                        SubElement(a, Q("gen", "name")).text = str(k)
                        SubElement(a, Q("gen", "value")).text = "1" if v else "0"

                    elif isinstance(v, int):
                        a = SubElement(ga, Q("gen", "IntAttribute"))
                        SubElement(a, Q("gen", "name")).text = str(k)
                        SubElement(a, Q("gen", "value")).text = str(v)

                    elif isinstance(v, (float,)):
                        a = SubElement(ga, Q("gen", "DoubleAttribute"))
                        SubElement(a, Q("gen", "name")).text = str(k)
                        SubElement(a, Q("gen", "value")).text = fmt(v)

                    else:
                        a = SubElement(ga, Q("gen", "StringAttribute"))
                        SubElement(a, Q("gen", "name")).text = str(k)
                        SubElement(a, Q("gen", "value")).text = str(v)

                except Exception:
                    continue

        def _write_inline_surface_opening(surf_el, opening_entries, lod="2"):
            if not opening_entries:
                return

            # Don't nest an opening inside a surface that is itself an opening type
            _parent_tag = getattr(surf_el, "tag", "") or ""
            _parent_local = _parent_tag.rsplit("}", 1)[-1] if "}" in _parent_tag else _parent_tag
            if _parent_local in ("WindowSurface", "DoorSurface", "Window", "Door"):
                return

            def _reuse_or_claim_gml_id(raw_id: str, prefix: str = "UUID") -> str:
                raw_id = str(raw_id or "").strip()
                if not raw_id:
                    return _new_gml_id(prefix, used_gml_ids)
                normalized = ensure_xs_id(raw_id, prefix=prefix)
                if normalized in used_gml_ids:
                    return normalized
                used_gml_ids.add(normalized)
                return normalized

            def _canonicalize_filling_polygon_ids(poly_gid: str, exterior_ring_id: str, interior_ring_ids=None):
                poly_gid = str(poly_gid or "").strip()
                exterior_ring_id = str(exterior_ring_id or "").strip()
                interior_ring_ids = list(interior_ring_ids or [])

                canonical_poly_gid = poly_gid
                canonical_ext_ring_id = exterior_ring_id
                canonical_int_ring_ids = [str(rid or "").strip() for rid in interior_ring_ids]

                poly_match = re.match(r"^(.*)_2$", poly_gid)
                ext_match = re.match(r"^(.*_\d+_)_2$", exterior_ring_id)
                if poly_match and ext_match:
                    candidate_poly_gid = poly_match.group(1)
                    candidate_ext_ring_id = ext_match.group(1)
                    if candidate_ext_ring_id.startswith(candidate_poly_gid + "_"):
                        canonical_poly_gid = candidate_poly_gid
                        canonical_ext_ring_id = candidate_ext_ring_id
                        remapped_int_ring_ids = []
                        for rid in canonical_int_ring_ids:
                            rid_match = re.match(r"^(.*_\d+_)_2$", rid)
                            remapped_int_ring_ids.append(rid_match.group(1) if rid_match else rid)
                        canonical_int_ring_ids = remapped_int_ring_ids

                return canonical_poly_gid, canonical_ext_ring_id, canonical_int_ring_ids

            opening_info = (opening_entries[0] or {}).get("opening_info")
            if not opening_info:
                return

            opening_type = opening_info.get("opening_type")
            filling_surface_type = str(opening_info.get("filling_surface_type") or "").strip()
            if opening_type not in ("Window", "Door"):
                return
            if filling_surface_type not in ("WindowSurface", "DoorSurface"):
                return

            opening_id = make_gml_id(
                str(opening_info.get("opening_id") or f"{opening_type}_{uuid4().hex}"),
                used_gml_ids,
            )
            opening_name = str(opening_info.get("opening_name") or "").strip()

            filling_wrapper = SubElement(surf_el, Q("con", "fillingSurface"))
            opening_el = SubElement(filling_wrapper, Q("con", filling_surface_type))
            opening_el.set(GML_ID, opening_id)

            if opening_name and opening_name != opening_id:
                SubElement(opening_el, Q("gml", "name")).text = opening_name

            lod_geom = SubElement(opening_el, _C(f"lod{lod}MultiSurface"))
            multi_surf = SubElement(lod_geom, Q("gml", "MultiSurface"))
            opening_ms_id = ""
            try:
                opening_ms_id = str((opening_entries[0] or {}).get("ms_id") or "").strip()
            except Exception:
                opening_ms_id = ""
            if opening_ms_id:
                multi_surf.set(GML_ID, make_gml_id(opening_ms_id, used_gml_ids))
            else:
                multi_surf.set(GML_ID, _new_gml_id("UUID", used_gml_ids))

            for poly_data in opening_entries:
                interior_rings = poly_data.get("int_xyz") or None
                poly_gid = str(poly_data.get("poly_gid") or "").strip()
                exterior_ring_id = str(poly_data.get("ext_id") or "").strip()
                stored_ring_ids = list(poly_data.get("int_ids") or [])
                poly_gid, exterior_ring_id, stored_ring_ids = _canonicalize_filling_polygon_ids(
                    poly_gid,
                    exterior_ring_id,
                    stored_ring_ids,
                )
                if poly_gid:
                    poly_gid = _reuse_or_claim_gml_id(poly_gid, prefix="ID")
                else:
                    poly_gid = _new_gml_id("UUID", used_gml_ids)

                if exterior_ring_id:
                    exterior_ring_id = _reuse_or_claim_gml_id(exterior_ring_id, prefix="ID")
                else:
                    exterior_ring_id = _new_gml_id("UUID", used_gml_ids)

                interior_ring_ids = None
                if interior_rings:
                    interior_ring_ids = []
                    for idx, _ring in enumerate(interior_rings):
                        rid = ""
                        if idx < len(stored_ring_ids):
                            rid = str(stored_ring_ids[idx] or "").strip()
                        if rid:
                            interior_ring_ids.append(_reuse_or_claim_gml_id(rid, prefix="ID"))
                        else:
                            interior_ring_ids.append(_new_gml_id("UUID", used_gml_ids))
                write_polygon_with_ring_ids(
                    parent_ms=multi_surf,
                    poly_gid=poly_gid,
                    exterior_ring=poly_data["ext_xyz"],
                    interior_rings=interior_rings,
                    srs_name=srs_name,
                    exterior_ring_id=exterior_ring_id,
                    interior_ring_ids=interior_ring_ids,
                )
        
        # Feature-Generics (Objekt-Custom-Properties) - VOR der Geometrie
        _write_generics(feature, _hier_parent or obj)

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

        def _determine_lod_from_materials(mesh_obj) -> str | None:
            """
            Determine LoD from imported per-surface materials.
            Import stores LoD like: bpy.data.materials["ID..."]["lod"].
            If multiple LoDs exist, prefer the highest (matches importer strategy).
            Returns a digit string "0".."4" or None.
            """
            try:
                lods = set()
                for slot in getattr(mesh_obj, "material_slots", []) or []:
                    mat = getattr(slot, "material", None)
                    if not mat:
                        continue
                    v = mat.get("lod", None)
                    if v is None:
                        continue
                    s = str(v).strip().lower()
                    if s.startswith("lod"):
                        s = s[3:].strip()
                    if s in ("0", "1", "2", "3", "4"):
                        lods.add(int(s))
                if lods:
                    return str(max(lods))
            except Exception:
                return None
            return None

        lod_level = _determine_lod(obj)
        lod_from_mats = _determine_lod_from_materials(obj)
        if lod_from_mats is not None:
            lod_level = lod_from_mats

        def _begin_bs(feature_el, surface_local, surf_id, ms_id, mat, lod="2"):
            if ns_key == "bldg" and surface_local == "BuildingPart":
                # CityGML 3.0: BuildingPart darf NICHT rekursiv verschachtelt werden.
                # Wenn das Feature selbst ein BuildingPart ist, lod*MultiSurface direkt schreiben.
                if local_name == "BuildingPart":
                    lod_tag = f"lod{lod}MultiSurface"
                    lod_elem = SubElement(feature_el, _C(lod_tag))
                    ms_attrs = {
                        GML_ID: (
                            make_gml_id(str(ms_id), used_gml_ids)
                            if ms_id
                            else _new_gml_id("UUID", used_gml_ids)
                        )
                    }
                    ms = SubElement(lod_elem, Q("gml", "MultiSurface"), ms_attrs)
                    return feature_el, ms

                # Feature ist ein Building: BuildingPart als Kind einbetten
                rel_el = SubElement(feature_el, Q("bldg", "buildingPart"))
                part_el = SubElement(rel_el, Q("bldg", "BuildingPart"))
                if surf_id:
                    validated_part_id = make_gml_id(str(surf_id), used_feat_ids)
                    part_el.set(GML_ID, validated_part_id)
                else:
                    part_el.set(GML_ID, f"ID_{uuid4().hex}")

                SubElement(
                    part_el,
                    _C("creationDate")
                ).text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

                _write_surface_generics_on_surface(part_el, mat)

                lod_tag = f"lod{lod}MultiSurface"
                lod_elem = SubElement(part_el, _C(lod_tag))
                ms_attrs = {
                    GML_ID: (
                        make_gml_id(str(ms_id), used_gml_ids)
                        if ms_id
                        else _new_gml_id("UUID", used_gml_ids)
                    )
                }
                ms = SubElement(lod_elem, Q("gml", "MultiSurface"), ms_attrs)
                return part_el, ms

            # Installation-Typen: <ns:xxxInstallation><ns:XxxInstallation> statt <boundary><con:*>
            _INST_TAG_MAP = {
                "BuildingInstallation":    ("bldg", "buildingInstallation", "bldg", "BuildingInstallation"),
                "IntBuildingInstallation": ("bldg", "buildingInstallation", "bldg", "BuildingInstallation"),
                "BridgeInstallation":      ("brid", "bridgeInstallation",  "brid", "BridgeInstallation"),
                "IntBridgeInstallation":   ("brid", "bridgeInstallation",  "brid", "BridgeInstallation"),
                "TunnelInstallation":      ("tun",  "tunnelInstallation",  "tun",  "TunnelInstallation"),
                "IntTunnelInstallation":   ("tun",  "tunnelInstallation",  "tun",  "TunnelInstallation"),
            }
            inst_spec = _INST_TAG_MAP.get(surface_local)
            if inst_spec:
                wrap_ns, wrap_ln, feat_ns, feat_ln = inst_spec
                if (ns_key, local_name) == (feat_ns, feat_ln):
                    lod_tag = f"lod{lod}MultiSurface"
                    lod_elem = SubElement(feature_el, _C(lod_tag))
                    ms_attrs = {
                        GML_ID: (
                            make_gml_id(str(ms_id), used_gml_ids)
                            if ms_id
                            else _new_gml_id("UUID", used_gml_ids)
                        )
                    }
                    ms = SubElement(lod_elem, Q("gml", "MultiSurface"), ms_attrs)
                    return feature_el, ms

                rel_el = SubElement(feature_el, Q(wrap_ns, wrap_ln))
                inst_el = SubElement(rel_el, Q(feat_ns, feat_ln))
                if surf_id:
                    validated_inst_id = make_gml_id(str(surf_id), used_gml_ids)
                    inst_el.set(GML_ID, validated_inst_id)
                else:
                    inst_el.set(GML_ID, f"ID_{uuid4().hex}")

                SubElement(
                    inst_el,
                    _C("creationDate")
                ).text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

                _write_surface_generics_on_surface(inst_el, mat)

                lod_tag = f"lod{lod}MultiSurface"
                lod_elem = SubElement(inst_el, _C(lod_tag))
                ms_attrs = {
                    GML_ID: (
                        make_gml_id(str(ms_id), used_gml_ids)
                        if ms_id
                        else _new_gml_id("UUID", used_gml_ids)
                    )
                }
                ms = SubElement(lod_elem, Q("gml", "MultiSurface"), ms_attrs)
                return inst_el, ms

            bb = SubElement(feature_el, _C("boundary"))
            # ClosureSurface gehört zum core-Namespace, alle anderen zum con-Namespace
            boundary_ns = {
                "ClosureSurface": None,
                "TrafficArea": "tran",
                "AuxiliaryTrafficArea": "tran",
                "Marking": "tran",
                "WaterSurface": "wtr",
                "WaterGroundSurface": "wtr",
                "GenericThematicSurface": "gen",
            }.get(surface_local, "con")
            if boundary_ns is None:
                surf = SubElement(bb, _C(surface_local))
            else:
                surf = SubElement(bb, Q(boundary_ns, surface_local))
            SubElement(
                surf,
                _C("creationDate")
            ).text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            # gml:id der konkreten BoundarySurface
            if surf_id:
                # XSD-Konformität: surf_id aus Material-Properties validieren
                validated_surf_id = make_gml_id(str(surf_id), used_gml_ids)
                surf.set(GML_ID, validated_surf_id)
            else:
                surf.set(GML_ID, f"ID_{uuid4().hex}")

            # NEU: Material-Custom-Properties als genericAttribute direkt an der Surface
            _write_surface_generics_on_surface(surf, mat)

            # LOD-flexibel: lod{N}MultiSurface
            lod_tag = f"lod{lod}MultiSurface"
            lod_elem = SubElement(surf, _C(lod_tag))

            # gml:id der MultiSurface
            ms_attrs = {
                GML_ID: (
                    make_gml_id(str(ms_id), used_gml_ids)
                    if ms_id
                    else _new_gml_id("UUID", used_gml_ids)
                )
            }
            ms = SubElement(lod_elem, Q("gml", "MultiSurface"), ms_attrs)
            return surf, ms

        _INLINE_PART_RELATIONS_V3 = {
            "BuildingPart": ("bldg", "buildingPart", "BuildingPart"),
            "BuildingInstallation": ("bldg", "buildingInstallation", "BuildingInstallation"),
            "IntBuildingInstallation": ("bldg", "buildingInstallation", "BuildingInstallation"),
            "BuildingFurniture": ("bldg", "buildingFurniture", "BuildingFurniture"),
            "BridgePart": ("brid", "bridgePart", "BridgePart"),
            "BridgeInstallation": ("brid", "bridgeInstallation", "BridgeInstallation"),
            "IntBridgeInstallation": ("brid", "bridgeInstallation", "BridgeInstallation"),
            "BridgeFurniture": ("brid", "bridgeFurniture", "BridgeFurniture"),
            "TunnelPart": ("tun", "tunnelPart", "TunnelPart"),
            "TunnelInstallation": ("tun", "tunnelInstallation", "TunnelInstallation"),
            "IntTunnelInstallation": ("tun", "tunnelInstallation", "TunnelInstallation"),
            "TunnelFurniture": ("tun", "tunnelFurniture", "TunnelFurniture"),
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
            if part_type in _INLINE_PART_RELATIONS_V3:
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

        def _allowed_inline_part_types_for_parent(parent_ns: str, parent_local: str) -> set[str]:
            if (parent_ns, parent_local) == ("bldg", "Building"):
                return {"BuildingPart", "BuildingInstallation", "IntBuildingInstallation", "BuildingFurniture"}
            if (parent_ns, parent_local) == ("bldg", "BuildingPart"):
                return {"BuildingPart", "BuildingInstallation", "IntBuildingInstallation", "BuildingFurniture"}
            if (parent_ns, parent_local) == ("brid", "Bridge"):
                return {"BridgePart", "BridgeInstallation", "IntBridgeInstallation", "BridgeFurniture"}
            if (parent_ns, parent_local) == ("brid", "BridgePart"):
                return {"BridgePart", "BridgeInstallation", "IntBridgeInstallation", "BridgeFurniture"}
            if (parent_ns, parent_local) == ("tun", "Tunnel"):
                return {"TunnelPart", "TunnelInstallation", "IntTunnelInstallation", "TunnelFurniture"}
            if (parent_ns, parent_local) == ("tun", "TunnelPart"):
                return {"TunnelPart", "TunnelInstallation", "IntTunnelInstallation", "TunnelFurniture"}
            return set()

        def _split_inline_part_surfaces(surface_groups, allowed_part_types: set[str]):
            filtered_surfaces = {}
            part_groups = {}

            for surf_type, polys in surface_groups.items():
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

        def _write_surface_entry(target_feature, surf_type, gp, openings_by_parent_map, base_lod):
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
                    base_lod,
                )
                for poly_data in gp.get("polygons", []):
                    write_polygon_with_ring_ids(
                        parent_ms=ms_parent,
                        poly_gid=poly_data.get("poly_gid"),
                        exterior_ring=poly_data["ext_xyz"],
                        interior_rings=poly_data["int_xyz"] if poly_data.get("int_xyz") else None,
                        srs_name=srs_name,
                        exterior_ring_id=poly_data.get("ext_id"),
                        interior_ring_ids=poly_data.get("int_ids") if poly_data.get("int_ids") else None,
                    )
                _write_inline_surface_opening(surf_el, gp.get("polygons", []), base_lod)
            else:
                surf_el, ms_parent = _begin_bs(
                    target_feature,
                    surf_type,
                    gp.get("surf_id"),
                    gp.get("ms_id"),
                    gp.get("mat"),
                    base_lod,
                )
                write_polygon_with_ring_ids(
                    parent_ms=ms_parent,
                    poly_gid=gp["poly_gid"],
                    exterior_ring=gp["ext_xyz"],
                    interior_rings=gp["int_xyz"] if gp.get("int_xyz") else None,
                    srs_name=srs_name,
                    exterior_ring_id=gp["ext_id"],
                    interior_ring_ids=gp["int_ids"] if gp.get("int_ids") else None,
                )
                _write_inline_surface_opening(surf_el, [gp], base_lod)

            parent_sid = gp.get("surf_id") or ""
            for opening_gp in openings_by_parent_map.pop(parent_sid, []):
                if opening_gp.get("is_multisurface", False):
                    _write_inline_surface_opening(
                        surf_el,
                        opening_gp.get("polygons", []),
                        base_lod,
                    )
                else:
                    _write_inline_surface_opening(
                        surf_el,
                        [opening_gp],
                        base_lod,
                    )

        def _write_inline_part_features(parent_feature, part_groups, openings_by_parent_map, base_lod):
            for group in part_groups:
                part_type = group.get("part_type", "")
                relation = _INLINE_PART_RELATIONS_V3.get(part_type)
                if not relation:
                    continue
                rel_ns, rel_local, feature_local = relation
                raw_part_id = str(group.get("part_id") or f"{part_type}_{uuid4().hex}").strip()
                part_el = SubElement(
                    SubElement(parent_feature, Q(rel_ns, rel_local)),
                    Q(rel_ns, feature_local),
                    {GML_ID: make_gml_id(raw_part_id, used_gml_ids)},
                )

                part_name = str(group.get("part_name") or "").strip()
                if part_name and part_name != raw_part_id:
                    SubElement(part_el, Q("gml", "name")).text = part_name

                for surf_type, polys in group.get("surfaces", {}).items():
                    for gp in polys:
                        _write_surface_entry(
                            part_el,
                            surf_type,
                            gp,
                            openings_by_parent_map,
                            base_lod,
                        )

                _reorder_feature_children(part_el)

        if is_implicit:
            _dbg(f"obj={obj.name} entering implicit export; lod_level={lod_level} is_imp_prototype={is_imp_prototype}")
            # ImplicitGeometry mit Unterstützung für Mesh-Instanzen:
            # genau ein Prototyp pro Mesh enthält die Geometrie, alle weiteren Objekte
            # verweisen per xlink:href auf dessen MultiSurface.

            # Handle implicit geometry with support for mesh instances:
            # - prototype (first time per mesh): writes inline relativeGeometry
            # - clones: reference prototype via xlink:href
            #
            # NOTE: meta_for_mesh can be None for the prototype (by design).

            # BUGFIX: meta_for_mesh exists for both prototype and clones. The old code treated
            # everything here as a clone and therefore did not write any inline implicit geometry
            # for the prototype (is_imp_prototype=True), resulting in features with only
            # boundedBy + creationDate and no geometry.
            if is_imp_prototype:
                # Prototyp: Inline relativeGeometry schreiben (import__v3.gml style)
                    # - referencePoint: object origin in target CRS
                    # - relativeGeometry: MultiSurface with coords relative to referencePoint
                ref_x = ref_y = ref_z = None
                try:
                    loc = obj.matrix_world.translation
                    coords_local = np.asarray([(float(loc.x), float(loc.y), float(loc.z))], dtype=np.float64)
                    coords_tgt, origin_tgt = trf.transform_with_origin(coords_local, (ox, oy, oz))
                    # coords_tgt is LOCAL relative to origin_tgt (see GeoTransformer.transform_with_origin),
                    # so absolute coordinates are origin_tgt + coords_tgt.
                    ref_x = float(coords_tgt[0][0]) + origin_tgt[0]
                    ref_y = float(coords_tgt[0][1]) + origin_tgt[1]
                    ref_z = float(coords_tgt[0][2]) + origin_tgt[2]
                except Exception:
                    ref_x, ref_y, ref_z = 0.0, 0.0, 0.0

                ref_base = (ref_x, ref_y, ref_z)
                ms_id = _new_gml_id("UUID", used_gml_ids)

                # Use stored LoD level from import if available, otherwise default to lod_level
                implicit_lod = obj.get("cgml3_implicit_lod")
                if implicit_lod is not None:
                    export_lod = int(implicit_lod)
                else:
                    export_lod = lod_level

                lod_imp = SubElement(feature, _C(f"lod{export_lod}ImplicitRepresentation"))
                imp = SubElement(lod_imp, _C("ImplicitGeometry"))
                if export_debug:
                    _dbg("obj=%s implicit prototype: attached lod_imp/imp, feature_children_now=%d" % (obj.name, len(list(feature))))

                # transformationMatrix: include rotation/scale from Blender matrix_world.
                # Translation is represented by referencePoint; keep matrix translation at 0.
                # Prefer persisted CityGML3 instance transform from importer (row-major 16 values),
                # since Blender matrix_world may end up identity after instancing/apply operations.
                persisted_tm = None
                try:
                    persisted_tm = obj.get("cgml3_transformationMatrix")
                except Exception:
                    persisted_tm = None

                mw = None
                if not persisted_tm:
                    # Use object-level rotation/scale to reconstruct a stable instance transform,
                    # independent of mesh instancing/evaluated matrix_world quirks.
                    mw = _matrix_from_matrix_world_no_translation(obj)

                tm = SubElement(imp, _C("transformationMatrix"))
                if persisted_tm:
                    tm.text = " ".join(str(persisted_tm).split())
                else:
                    tm_vals = _matrix_world_to_citygml_tm(mw)
                    tm.text = " ".join(fmt(v) for v in tm_vals)
                if export_debug:
                    if persisted_tm:
                        _dbg("obj=%s implicit prototype: tm=persisted %s" % (obj.name, " ".join(str(persisted_tm).split()[:8])))
                    else:
                        _dbg("obj=%s implicit prototype: tm=%s" % (obj.name, " ".join(str(v) for v in tm_vals[:8])))

                ref = SubElement(imp, _C("referencePoint"))
                pt = SubElement(ref, Q("gml", "Point"))
                pos = SubElement(pt, Q("gml", "pos"))
                pos.set("srsDimension", "3")
                pos.text = " ".join(fmt(v) for v in ref_base)

                # Inline relativeGeometry: MultiSurface with coords relative to referencePoint.
                # Do not write standalone gml:MultiSurface/CompositeSurface elements at CityModel-level.
                ms_id = str(ms_id)
                rel = SubElement(imp, _C("relativeGeometry"))
                ms = SubElement(rel, Q("gml", "MultiSurface"), {GML_ID: ms_id})

                for polys in geom_buf.values():
                    for gp in polys:
                        if gp.get("is_multisurface", False):
                            for poly_data in gp.get("polygons", []) or []:
                                ext_rel = [
                                    _transform_xyz_by_inv_no_translation(
                                        obj,
                                        (x - ref_base[0], y - ref_base[1], z - ref_base[2]),
                                    )
                                    for (x, y, z) in poly_data["ext_xyz"]
                                ]
                                if poly_data.get("int_xyz"):
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
                                    exterior_ring_id=poly_data.get("ext_id"),
                                    interior_ring_ids=poly_data.get("int_ids") or None,
                                )
                        else:
                            ext_rel = [
                                _transform_xyz_by_inv_no_translation(
                                    obj,
                                    (x - ref_base[0], y - ref_base[1], z - ref_base[2]),
                                )
                                for (x, y, z) in gp["ext_xyz"]
                            ]
                            if gp.get("int_xyz"):
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
                                exterior_ring_id=gp.get("ext_id"),
                                interior_ring_ids=gp.get("int_ids") or None,
                            )


                    # Cache for clones (href target) in the per-mesh dictionary.
                    # Since meta_for_mesh is None in this branch (prototype), store it into the cache map.
                try:
                    implicit_meta_by_mesh[mesh_orig] = {
                        "ref_base": ref_base,
                        "ms_id": str(ms_id),
                        "probe_abs": probe_abs_cur or [],
                        "app_id": None,
                    }
                except Exception:
                    pass
            else:
                # Clone path: write per-instance transformationMatrix from obj.matrix_world and
                # reference the prototype geometry via xlink:href (CityGML 3 / import__v3.gml style).
                ref_base0 = meta_for_mesh.get("ref_base") if isinstance(meta_for_mesh, dict) else None
                ms_id = meta_for_mesh.get("ms_id") if isinstance(meta_for_mesh, dict) else None
                proto_probe = (meta_for_mesh.get("probe_abs") or []) if isinstance(meta_for_mesh, dict) else []

                if (not is_imp_prototype) and (not ref_base0 or not ms_id):
                    # Sicherheits-Fallback: Klon wie früher voll exportieren
                    # Use stored LoD level from import if available
                    implicit_lod = obj.get("cgml3_implicit_lod")
                    if implicit_lod is not None:
                        export_lod = int(implicit_lod)
                    else:
                        export_lod = lod_level
                    lod_imp = SubElement(feature, _C(f"lod{export_lod}ImplicitRepresentation"))
                    imp = SubElement(lod_imp, _C("ImplicitGeometry"))
                    tm = SubElement(imp, _C("transformationMatrix"))
                    tm.text = " ".join(
                        fmt(v)
                        for v in (
                            1.0, 0.0, 0.0, 0.0,
                            0.0, 1.0, 0.0, 0.0,
                            0.0, 0.0, 1.0, 0.0,
                            0.0, 0.0, 0.0, 1.0,
                        )
                    )
                    ref = SubElement(imp, _C("referencePoint"))
                    pt = SubElement(ref, Q("gml", "Point"))
                    pos = SubElement(pt, Q("gml", "pos"))
                    pos.set("srsDimension", "3")
                    pos.text = "0 0 0"
                    rel = SubElement(imp, _C("relativeGeometry"))
                    ms = SubElement(rel, Q("gml", "MultiSurface"), {GML_ID: _new_gml_id("UUID", used_gml_ids)})
                    for polys in geom_buf.values():
                        for gp in polys:
                            write_polygon_with_ring_ids(
                                parent_ms=ms,
                                poly_gid=gp["poly_gid"],
                                exterior_ring=gp["ext_xyz"],
                                interior_rings=gp["int_xyz"] if gp["int_xyz"] else None,
                                srs_name=srs_name,
                                exterior_ring_id=gp["ext_id"],
                                interior_ring_ids=gp["int_ids"] if gp["int_ids"] else None,
                            )
                elif not is_imp_prototype:
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

                    # Use stored LoD level from import if available
                    implicit_lod = obj.get("cgml3_implicit_lod")
                    if implicit_lod is not None:
                        export_lod = int(implicit_lod)
                    else:
                        export_lod = lod_level

                    lod_imp = SubElement(feature, _C(f"lod{export_lod}ImplicitRepresentation"))
                    imp = SubElement(lod_imp, _C("ImplicitGeometry"))

                    # transformationMatrix: include rotation/scale from this instance's matrix_world.
                    persisted_tm = None
                    try:
                        persisted_tm = obj.get("cgml3_transformationMatrix")
                    except Exception:
                        persisted_tm = None

                    mw = None
                    if not persisted_tm:
                        mw = _matrix_from_matrix_world_no_translation(obj)

                    tm = SubElement(imp, _C("transformationMatrix"))
                    if persisted_tm:
                        tm.text = " ".join(str(persisted_tm).split())
                    else:
                        tm.text = " ".join(fmt(v) for v in _matrix_world_to_citygml_tm(mw))

                    ref = SubElement(imp, _C("referencePoint"))
                    pt = SubElement(ref, Q("gml", "Point"))
                    pos = SubElement(pt, Q("gml", "pos"))
                    pos.set("srsDimension", "3")
                    pos.text = " ".join(fmt(v) for v in ref_base)

                    # Clone: reference prototype geometry via xlink:href (CityGML 3 style)
                    rel = SubElement(imp, _C("relativeGeometry"))
                    rel.set(Q("xlink", "href"), f"#{ms_id}")

            # Debug: confirm that implicit nodes are attached to the feature element
            try:
                if export_debug:
                    # Also log which implicit metadata branch we are in; this is the key for debugging
                    # why no implicit XML nodes show up in the final document.
                    _dbg("obj=%s implicit debug: meta_for_mesh_is_none=%s is_imp_prototype=%s" % (obj.name, meta_for_mesh is None, is_imp_prototype))

                    child_tags = [getattr(ch, "tag", None) for ch in list(feature)]
                    child_lns = [_ln(t) for t in child_tags if isinstance(t, str)]
                    hits = [n for n in child_lns if ("Implicit" in n) or ("relativeGeometry" in n) or n.startswith("lod")]
                    raw_hits = [t for t in child_tags if isinstance(t, str) and (("Implicit" in t) or ("relativeGeometry" in t) or ("lod" in t))]
                    _dbg(
                        "obj=%s feature_children=%d implicitish=%s raw=%s"
                        % (obj.name, len(child_lns), hits[:12], raw_hits[:6])
                    )
            except Exception:
                pass

        else:
            # explizite Geometrie
            # CityFurniture + Vegetation should be exported as CityGML 3 LoD2 ImplicitGeometry
            # with inline relativeGeometry (see import__v3.gml), because these object types
            # are often represented as instances in Blender and FME expects a geometry carrier.
            #
            # IMPORTANT: do not add an extra implicit representation if the object is already
            # marked as implicit; that branch above already writes lod{N}ImplicitRepresentation.
            if (not is_implicit) and (ns_key, local_name) in (
                ("frn", "CityFurniture"),
                ("veg", "SolitaryVegetationObject"),
                ("veg", "PlantCover"),
            ):
                try:
                    # Reference point = object origin in target CRS
                    loc = obj.matrix_world.translation
                    coords_local = np.asarray([(float(loc.x), float(loc.y), float(loc.z))], dtype=np.float64)
                    coords_tgt, origin_tgt = trf.transform_with_origin(coords_local, (ox, oy, oz))
                    ref_x = float(coords_tgt[0][0]) + origin_tgt[0]
                    ref_y = float(coords_tgt[0][1]) + origin_tgt[1]
                    ref_z = float(coords_tgt[0][2]) + origin_tgt[2]

                    # Use stored LoD level from import if available
                    implicit_lod = obj.get("cgml3_implicit_lod")
                    if implicit_lod is not None:
                        export_lod = int(implicit_lod)
                    else:
                        export_lod = lod_level

                    lod_imp = SubElement(feature, _C(f"lod{export_lod}ImplicitRepresentation"))
                    imp = SubElement(lod_imp, _C("ImplicitGeometry"))

                    # transformationMatrix: include rotation/scale from Blender matrix_world.
                    # Translation is still represented by referencePoint; keep matrix translation at 0.
                    mw = obj.matrix_world.copy()
                    try:
                        mw[0][3] = 0.0
                        mw[1][3] = 0.0
                        mw[2][3] = 0.0
                    except Exception:
                        pass

                    tm = SubElement(imp, _C("transformationMatrix"))
                    tm.text = " ".join(
                        fmt(v)
                        for v in _matrix_world_to_citygml_tm(mw)
                    )

                    ref = SubElement(imp, _C("referencePoint"))
                    pt = SubElement(ref, Q("gml", "Point"))
                    pos = SubElement(pt, Q("gml", "pos"))
                    pos.set("srsDimension", "3")
                    pos.text = f"{fmt(ref_x)} {fmt(ref_y)} {fmt(ref_z)}"

                    rel = SubElement(imp, _C("relativeGeometry"))
                    ms = SubElement(rel, Q("gml", "MultiSurface"), {GML_ID: _new_gml_id("UUID", used_gml_ids)})

                    # Write all polygons as relative coordinates to the reference point
                    for polys in geom_buf.values():
                        for gp in polys:
                            if gp.get("is_multisurface", False):
                                for poly_data in gp.get("polygons", []):
                                    ext_rel = [
                                        (x - ref_x, y - ref_y, z - ref_z)
                                        for (x, y, z) in poly_data["ext_xyz"]
                                    ]
                                    ints_rel = None
                                    if poly_data.get("int_xyz"):
                                        ints_rel = [
                                            [(x - ref_x, y - ref_y, z - ref_z) for (x, y, z) in ring]
                                            for ring in poly_data["int_xyz"]
                                        ]
                                    write_polygon_with_ring_ids(
                                        parent_ms=ms,
                                        poly_gid=poly_data["poly_gid"],
                                        exterior_ring=ext_rel,
                                        interior_rings=ints_rel,
                                        srs_name=srs_name,
                                        exterior_ring_id=poly_data["ext_id"],
                                        interior_ring_ids=poly_data["int_ids"] if poly_data.get("int_ids") else None,
                                    )
                            else:
                                ext_rel = [(x - ref_x, y - ref_y, z - ref_z) for (x, y, z) in gp["ext_xyz"]]
                                ints_rel = None
                                if gp.get("int_xyz"):
                                    ints_rel = [
                                        [(x - ref_x, y - ref_y, z - ref_z) for (x, y, z) in ring]
                                        for ring in gp["int_xyz"]
                                    ]
                                write_polygon_with_ring_ids(
                                    parent_ms=ms,
                                    poly_gid=gp["poly_gid"],
                                    exterior_ring=ext_rel,
                                    interior_rings=ints_rel,
                                    srs_name=srs_name,
                                    exterior_ring_id=gp["ext_id"],
                                    interior_ring_ids=gp["int_ids"] if gp.get("int_ids") else None,
                                )

                    # This feature's bbox already includes all points collected above.
                except Exception:
                    # If anything goes wrong, fall back to the existing explicit-geometry path.
                    pass

                # Skip the explicit geometry writer below for these feature types.
                if obj_eval:
                    obj_eval.to_mesh_clear()
                continue

            if (ns_key, local_name) in (("brid", "Bridge"), ("brid", "BridgePart")):
                # Separate Polygone nach Typ: con:*Surface vs. direkte Bridge-MultiSurface
                con_surfaces = {}  # thematische Surfaces (RoofSurface, WallSurface, etc.)
                bridge_multisurface = []  # direkte lod2MultiSurface Polygone (ohne spezifische Surface)

                for surf_type, polys in geom_buf.items():
                    if surf_type in ("BridgeConstructiveElement", "BridgeInstallation"):
                        continue  # werden separat behandelt
                    elif surf_type == "Bridge":
                        # Direkte Bridge-Polygone ohne spezifische Surface → lod2MultiSurface
                        bridge_multisurface.extend(polys)
                    else:
                        # Thematische Surfaces (RoofSurface, WallSurface, etc.)
                        con_surfaces[surf_type] = polys

                _allowed_inline_parts = _allowed_inline_part_types_for_parent(ns_key, local_name)
                if _allowed_inline_parts:
                    con_surfaces, inline_part_groups = _split_inline_part_surfaces(con_surfaces, _allowed_inline_parts)
                else:
                    inline_part_groups = []

                # 1) Thematische con:*Surface-Boundaries schreiben (falls vorhanden)
                if con_surfaces:
                    # Bridge mit thematischen Surfaces: Boundary + Solid Struktur
                    for surf_type, polys in con_surfaces.items():
                        for gp in polys:
                            # Prüfen, ob dies eine echte MultiSurface ist
                            if gp.get("is_multisurface", False):
                                # MultiSurface mit mehreren surfaceMembers
                                surf_el, ms_parent = _begin_bs(
                                    feature,
                                    surf_type,
                                    gp.get("surf_id"),
                                    gp.get("ms_id"),
                                    gp.get("mat"),
                                )
                                
                                # Jedes Polygon als surfaceMember schreiben
                                for poly in gp.get("polygons", []):
                                    write_polygon_with_ring_ids(
                                        parent_ms=ms_parent,
                                        poly_gid=poly["poly_gid"],
                                        exterior_ring=poly["ext_xyz"],
                                        interior_rings=poly["int_xyz"] or None,
                                        srs_name=srs_name,
                                        exterior_ring_id=poly["ext_id"],
                                        interior_ring_ids=poly["int_ids"] or None,
                                    )
                                _write_inline_surface_opening(surf_el, gp.get("polygons", []), lod_level)
                            else:
                                # Einzelnes Polygon
                                surf_el, ms_parent = _begin_bs(
                                    feature,
                                    surf_type,
                                    gp.get("surf_id"),
                                    gp.get("ms_id"),
                                    gp.get("mat"),
                                    lod_level,
                                )
                                write_polygon_with_ring_ids(
                                    parent_ms=ms_parent,
                                    poly_gid=gp["poly_gid"],
                                    exterior_ring=gp["ext_xyz"],
                                    interior_rings=gp["int_xyz"] or None,
                                    srs_name=srs_name,
                                    exterior_ring_id=gp["ext_id"],
                                    interior_ring_ids=gp["int_ids"] or None,
                                )
                                _write_inline_surface_opening(surf_el, [gp], lod_level)

                    # LOD-flexible Solid für alle thematischen Surfaces
                    lod_solid_tag = f"lod{lod_level}Solid"
                    lod_solid = SubElement(feature, _C(lod_solid_tag))
                    solid = SubElement(lod_solid, Q("gml", "Solid"), {GML_ID: _new_gml_id("UUID", used_gml_ids)})
                    ext = SubElement(solid, Q("gml", "exterior"))
                    shell = SubElement(ext, Q("gml", "Shell"), {GML_ID: _new_gml_id("UUID", used_gml_ids)})
                    for polys in con_surfaces.values():
                        for gp in polys:
                            if gp.get("is_multisurface", False):
                                # MultiSurface: alle enthaltenen Polygone referenzieren
                                for poly in gp.get("polygons", []):
                                    SubElement(
                                        shell,
                                        Q("gml", "surfaceMember"),
                                        {Q("xlink", "href"): f"#{poly['poly_gid']}"}
                                    )
                            else:
                                # Einzelnes Polygon
                                SubElement(
                                    shell,
                                    Q("gml", "surfaceMember"),
                                    {Q("xlink", "href"): f"#{gp['poly_gid']}"}
                                )
                
                _write_inline_part_features(feature, inline_part_groups, {}, lod_level)

                # 2) Direkte LOD-flexible MultiSurface für Bridge (falls vorhanden)
                if bridge_multisurface:
                    # Bridge-MultiSurface: direkt unter dem Feature
                    lod_ms_tag = f"lod{lod_level}MultiSurface"
                    lod_ms = SubElement(feature, _C(lod_ms_tag))
                    
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
                
                # 3) BridgeConstructiveElement
                for gp in geom_buf["BridgeConstructiveElement"]:
                    wrapper = SubElement(feature, Q("brid", "bridgeConstructiveElement"))
                    elem = SubElement(
                        wrapper,
                        Q("brid", "BridgeConstructiveElement"),
                        {GML_ID: ensure_xs_id(gp.get("surf_id"), prefix="ID")}
                    )
                    SubElement(
                        elem,
                        _C("creationDate")
                    ).text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

                    lod_ms_tag = f"lod{lod_level}MultiSurface"
                    # lodX*-Geometrieeigenschaften kommen aus core:AbstractSpaceType (also core:-Namespace).
                    lod_ms_elem = SubElement(elem, _C(lod_ms_tag))
                    ms = SubElement(
                        lod_ms_elem,
                        Q("gml", "MultiSurface"),
                        {GML_ID: ensure_xs_id(gp.get("ms_id"), prefix="ID")}
                    )
                    
                    # Prüfen, ob MultiSurface-Gruppe oder einzelnes Polygon
                    if gp.get("is_multisurface", False):
                        # MultiSurface: mehrere Polygone schreiben
                        for poly in gp.get("polygons", []):
                            write_polygon_with_ring_ids(
                                parent_ms=ms,
                                poly_gid=poly["poly_gid"],
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
                    wrapper = SubElement(feature, Q("brid", "bridgeInstallation"))
                    elem = SubElement(
                        wrapper,
                        Q("brid", "BridgeInstallation"),
                        {GML_ID: ensure_xs_id(gp.get("surf_id"), prefix="ID")}
                    )
                    SubElement(
                        elem,
                        _C("creationDate")
                    ).text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

                    lod_ms_tag = f"lod{lod_level}MultiSurface"
                    lod_ms_elem = SubElement(elem, _C(lod_ms_tag))
                    ms = SubElement(
                        lod_ms_elem,
                        Q("gml", "MultiSurface"),
                        {GML_ID: ensure_xs_id(gp.get("ms_id"), prefix="ID")}
                    )
                    
                    # Prüfen, ob MultiSurface-Gruppe oder einzelnes Polygon
                    if gp.get("is_multisurface", False):
                        # MultiSurface: mehrere Polygone schreiben
                        for poly in gp.get("polygons", []):
                            write_polygon_with_ring_ids(
                                parent_ms=ms,
                                poly_gid=poly["poly_gid"],
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

            else:
                # Nicht-Bridge: CityFurniture/Vegetation/WaterBody bekommen direkt lod2MultiSurface,
                # andere Features (Buildings etc.) bekommen boundary+Solid
                
                # Feature-Typen, die direkt lod2MultiSurface ohne boundary schreiben
                simple_multisurface_features = [
                    ("frn", "CityFurniture"),
                    ("veg", "SolitaryVegetationObject"),
                    ("veg", "PlantCover"),
                    ("veg", "VegetationObject"),
                    ("wtr", "WaterBody"),
                    ("wtr", "WaterSurface"),
                    ("wtr", "WaterGroundSurface"),
                    ("con", "OtherConstruction"),
                    ("luse", "LandUse"),
                    ("gen", "GenericLogicalSpace"),
                    ("gen", "GenericOccupiedSpace"),
                    ("gen", "GenericUnoccupiedSpace"),
                    ("gen", "GenericThematicSurface"),
                    ("brid", "BridgeFurniture"),
                    ("tun", "HollowSpace"),
                    ("tun", "TunnelFurniture"),
                    ("tran", "Road"),
                    ("tran", "Railway"),
                    ("tran", "Track"),
                    ("tran", "Square"),
                    ("tran", "TrafficArea"),
                    ("tran", "AuxiliaryTrafficArea"),
                    ("tran", "Marking"),
                    ("tran", "Hole"),
                    ("tran", "ClearanceSpace"),
                    ("tran", "Waterway"),
                    ("tran", "Section"),
                    ("tran", "Intersection"),
                ]
                
                is_simple_ms = (ns_key, local_name) in simple_multisurface_features
                if (ns_key, local_name) == ("con", "OtherConstruction") and set(geom_buf).intersection({
                    "RoofSurface",
                    "WallSurface",
                    "GroundSurface",
                    "OuterCeilingSurface",
                    "OuterFloorSurface",
                    "ClosureSurface",
                    "CeilingSurface",
                    "InteriorWallSurface",
                    "FloorSurface",
                }):
                    is_simple_ms = False
                if (ns_key, local_name) == ("wtr", "WaterBody") and set(geom_buf).intersection({
                    "WaterSurface",
                    "WaterGroundSurface",
                }):
                    is_simple_ms = False
                if (ns_key, local_name) in {
                    ("tran", "TrafficSpace"),
                    ("tran", "AuxiliaryTrafficSpace"),
                } and not set(geom_buf).intersection({
                    "TrafficArea",
                    "AuxiliaryTrafficArea",
                    "Marking",
                }):
                    is_simple_ms = True
                # BuildingPart ohne echte BoundarySurfaces: als lod2MultiSurface exportieren
                # (nicht jede Fläche als eigenes Sub-BuildingPart)
                if (ns_key, local_name) == ("bldg", "BuildingPart"):
                    _BOUNDARY_TYPES = {
                        "RoofSurface", "WallSurface", "GroundSurface",
                        "OuterCeilingSurface", "OuterFloorSurface", "ClosureSurface",
                        "CeilingSurface", "InteriorWallSurface", "FloorSurface",
                    }
                    if not set(geom_buf).intersection(_BOUNDARY_TYPES):
                        is_simple_ms = True
                # Tunnel/TunnelPart und Installations ohne BoundarySurfaces:
                # als lod2MultiSurface exportieren; mit BoundarySurfaces: boundary+Solid
                if (ns_key, local_name) in {
                    ("tun", "Tunnel"), ("tun", "TunnelPart"),
                    ("brid", "BridgeInstallation"),
                    ("tun", "TunnelInstallation"),
                }:
                    _BOUNDARY_TYPES = {
                        "RoofSurface", "WallSurface", "GroundSurface",
                        "OuterCeilingSurface", "OuterFloorSurface", "ClosureSurface",
                        "CeilingSurface", "InteriorWallSurface", "FloorSurface",
                    }
                    if not set(geom_buf).intersection(_BOUNDARY_TYPES):
                        is_simple_ms = True
                is_tin_relief = (ns_key, local_name) == ("dem", "TINRelief")
                
                if is_tin_relief:
                    # TINRelief: dem:lod MUSS vor dem:tin kommen (XSD-Reihenfolge)
                    _write_specific_attributes(feature, obj)
                    
                    # TINRelief: spezielle Struktur mit dem:tin und gml:TriangulatedSurface
                    tin_el = SubElement(feature, Q("dem", "tin"))
                    
                    # Extrahiere TriangulatedSurface-ID falls vorhanden
                    tin_id = None
                    for polys in geom_buf.values():
                        for gp in polys:
                            if gp.get("ms_id"):
                                tin_id = gp["ms_id"]
                                break
                        if tin_id:
                            break
                    
                    if not tin_id:
                        tin_id = _new_gml_id("UUID", used_gml_ids)
                    
                    tri_surf = SubElement(tin_el, Q("gml", "TriangulatedSurface"), {GML_ID: tin_id})
                    patches_el = SubElement(tri_surf, Q("gml", "patches"))
                    
                    # Jedes Polygon wird als gml:Triangle geschrieben
                    for polys in geom_buf.values():
                        for gp in polys:
                            if gp.get("is_multisurface", False):
                                # MultiSurface-Gruppe: jedes Polygon als Triangle
                                for poly in gp.get("polygons", []):
                                    triangle = SubElement(patches_el, Q("gml", "Triangle"))
                                    exterior = SubElement(triangle, Q("gml", "exterior"))
                                    lr = SubElement(exterior, Q("gml", "LinearRing"))
                                    poslist = SubElement(lr, Q("gml", "posList"))
                                    poslist.set("srsDimension", "3")
                                    poslist.text = format_poslist(poly["ext_xyz"])
                            else:
                                # Einzelnes Polygon als Triangle
                                triangle = SubElement(patches_el, Q("gml", "Triangle"))
                                exterior = SubElement(triangle, Q("gml", "exterior"))
                                lr = SubElement(exterior, Q("gml", "LinearRing"))
                                poslist = SubElement(lr, Q("gml", "posList"))
                                poslist.set("srsDimension", "3")
                                poslist.text = format_poslist(gp["ext_xyz"])
                
                elif is_simple_ms:
                    # CityFurniture/Vegetation/WaterBody/BuildingPart: direkt LOD-flexible MultiSurface schreiben
                    lod_ms_tag = f"lod{lod_level}MultiSurface"
                    lod_ms = SubElement(feature, _C(lod_ms_tag))
                    ms_id = None
                    
                    # MultiSurface-ID aus den Polygonen extrahieren (falls vorhanden)
                    for polys in geom_buf.values():
                        for gp in polys:
                            if gp.get("ms_id"):
                                ms_id = gp["ms_id"]
                                break
                        if ms_id:
                            break
                    
                    if not ms_id:
                        ms_id = f"ID_{uuid4()}"
                    
                    ms = SubElement(
                        lod_ms,
                        Q("gml", "MultiSurface"),
                        {GML_ID: ms_id}
                    )
                    
                    # Gruppiere Polygone nach CompositeSurface-ID
                    composite_groups = defaultdict(list)
                    standalone_polygons = []
                    
                    for polys in geom_buf.values():
                        for gp in polys:
                            # Prüfen, ob es eine MultiSurface-Gruppe ist oder ein einzelnes Polygon
                            if gp.get("is_multisurface", False):
                                # MultiSurface-Gruppe: jedes Polygon einzeln prüfen
                                for poly in gp.get("polygons", []):
                                    cs_id = poly.get("cs_id", "")
                                    if cs_id:
                                        composite_groups[cs_id].append(poly)
                                    else:
                                        standalone_polygons.append(poly)
                            else:
                                # Einzelnes Polygon
                                cs_id = gp.get("cs_id", "")
                                if cs_id:
                                    composite_groups[cs_id].append(gp)
                                else:
                                    standalone_polygons.append(gp)
                    
                    # 1) CompositeSurfaces schreiben
                    for cs_id, cs_polys in composite_groups.items():
                        write_compositesurface_with_polygons(
                            parent_ms=ms,
                            cs_id=cs_id,
                            polygons=cs_polys,
                            srs_name=srs_name,
                        )
                    
                    # 2) Standalone Polygone schreiben
                    for gp in standalone_polygons:
                        if gp.get("is_multisurface", False):
                            # MultiSurface-Gruppe: alle enthaltenen Polygone
                            for poly in gp.get("polygons", []):
                                write_polygon_with_ring_ids(
                                    parent_ms=ms,
                                    poly_gid=poly["poly_gid"],
                                    exterior_ring=poly["ext_xyz"],
                                    interior_rings=poly["int_xyz"] if poly["int_xyz"] else None,
                                    srs_name=srs_name,
                                    exterior_ring_id=poly["ext_id"],
                                    interior_ring_ids=poly["int_ids"] if poly["int_ids"] else None,
                                )
                        else:
                            # Einzelnes Polygon
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
                    # BuildingInstallation und ähnliche Installation-Typen erhalten
                    # bldg:buildingInstallation mit lod2Solid statt boundary
                    INSTALLATION_TYPES = {
                        "BuildingInstallation", "IntBuildingInstallation",
                        "BridgeInstallation", "IntBridgeInstallation",
                        "TunnelInstallation", "IntTunnelInstallation",
                    }
                    _self_installation_feature = (ns_key, local_name) in {
                        ("bldg", "BuildingInstallation"),
                        ("brid", "BridgeInstallation"),
                        ("tun", "TunnelInstallation"),
                    }
                    self_installation_geometry_types_written = set()

                    # ClosureSurfaces from Join Object Parts carry the same
                    # subfeature id as their Part/Installation and are written
                    # inside that inline feature below.
                    
                    # Gruppiere Installation-Polygone nach Typ (alle Polygone eines Typs in eine Installation)
                    # WICHTIG: Polygone ohne gml:id bekommen beim Import keine gemeinsame surf_id,
                    # deshalb gruppieren wir nach inst_type statt surf_id
                    installation_groups = {}
                    if not _self_installation_feature:
                        for inst_type in INSTALLATION_TYPES:
                            if inst_type in geom_buf:
                                for gp in geom_buf[inst_type]:
                                    mat = gp.get("mat")
                                    # Nur Polygone mit in_shell=True als Installation exportieren
                                    if mat and mat.get("in_shell"):
                                        # Alle BuildingInstallation-Polygone in EINE Gruppe
                                        # (entspricht der Struktur im Import: ein BuildingInstallation mit mehreren Polygonen)
                                        if inst_type not in installation_groups:
                                            # Verwende surf_id wenn vorhanden, sonst generiere neue
                                            surf_id = gp.get("surf_id")
                                            if not surf_id or surf_id.startswith("ID_") or surf_id.startswith("UUID_"):
                                                surf_id = f"ID_{uuid4().hex}"
                                            installation_groups[inst_type] = {
                                                "type": inst_type,
                                                "polygons": [],
                                                "mat": mat,
                                                "surf_id": surf_id,
                                            }
                                        installation_groups[inst_type]["polygons"].append(gp)
                    
                    # Schreibe Installation-Elemente (vor boundaries)
                    for inst_type, inst_data in installation_groups.items():
                        polys = inst_data["polygons"]
                        mat = inst_data["mat"]
                        surf_id = inst_data["surf_id"]
                        
                        # Bestimme korrekten Namespace / Property-Name (CityGML 3.0 schema)
                        if inst_type in ("BuildingInstallation", "IntBuildingInstallation"):
                            inst_ns = "bldg"
                            inst_elem_name = "buildingInstallation"
                        elif inst_type in ("BridgeInstallation", "IntBridgeInstallation"):
                            inst_ns = "brid"
                            inst_elem_name = "bridgeInstallation"
                        elif inst_type in ("TunnelInstallation", "IntTunnelInstallation"):
                            inst_ns = "tun"
                            inst_elem_name = "tunnelInstallation"
                        else:
                            continue
                        
                        # CityGML 3.0: installations are feature members on the parent feature.
                        # They must NOT be written as <boundary> children (which expects con:*Surface).
                        # Schema expects: <bldg:buildingInstallation><bldg:BuildingInstallation>...</...></...>
                        inst_container = SubElement(feature, Q(inst_ns, inst_elem_name))
                        inst_elem = SubElement(inst_container, Q(inst_ns, inst_type))
                        # XSD-Konformität: surf_id aus Material-Properties validieren
                        validated_inst_id = make_gml_id(str(surf_id), used_gml_ids) if surf_id else f"ID_{uuid4().hex}"
                        inst_elem.set(GML_ID, validated_inst_id)
                        
                        # creationDate
                        SubElement(
                            inst_elem,
                            _C("creationDate")
                        ).text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        
                        # lod2Solid mit Solid/Shell Struktur
                        lod_solid_tag = f"lod{lod_level}Solid"
                        lod_solid = SubElement(inst_elem, _C(lod_solid_tag))
                        solid = SubElement(lod_solid, Q("gml", "Solid"))
                        solid.set("srsName", effective_srs_name)
                        solid.set("srsDimension", "3")
                        ext = SubElement(solid, Q("gml", "exterior"))
                        shell = SubElement(ext, Q("gml", "Shell"))
                        
                        # Schreibe alle Polygone als surfaceMembers
                        for gp in polys:
                            # MultiSurface-Gruppe: enthält "polygons" statt "ext_xyz"
                            if gp.get("is_multisurface", False):
                                for poly in gp.get("polygons", []):
                                    sm = SubElement(shell, Q("gml", "surfaceMember"))
                                    gpoly = SubElement(sm, Q("gml", "Polygon"))
                                    exterior = SubElement(gpoly, Q("gml", "exterior"))
                                    lre = SubElement(exterior, Q("gml", "LinearRing"))
                                    posliste = SubElement(lre, Q("gml", "posList"))
                                    posliste.text = format_poslist(poly["ext_xyz"])
                                    if poly.get("int_xyz"):
                                        for ring in poly["int_xyz"]:
                                            interior = SubElement(gpoly, Q("gml", "interior"))
                                            lri = SubElement(interior, Q("gml", "LinearRing"))
                                            poslisti = SubElement(lri, Q("gml", "posList"))
                                            poslisti.text = format_poslist(ring)
                                continue

                            # Einzelnes Polygon: ext_xyz muss vorhanden sein
                            if not gp.get("ext_xyz"):
                                continue

                            # surfaceMember mit inline Polygon (ohne gml:id)
                            sm = SubElement(shell, Q("gml", "surfaceMember"))
                            gpoly = SubElement(sm, Q("gml", "Polygon"))
                            
                            # exterior
                            exterior = SubElement(gpoly, Q("gml", "exterior"))
                            lre = SubElement(exterior, Q("gml", "LinearRing"))
                            posliste = SubElement(lre, Q("gml", "posList"))
                            posliste.text = format_poslist(gp["ext_xyz"])
                            
                            # interior rings (falls vorhanden)
                            if gp.get("int_xyz"):
                                for ring in gp["int_xyz"]:
                                    interior = SubElement(gpoly, Q("gml", "interior"))
                                    lri = SubElement(interior, Q("gml", "LinearRing"))
                                    poslisti = SubElement(lri, Q("gml", "posList"))
                                    poslisti.text = format_poslist(ring)

                    # Direct geometry of an installation feature belongs to that feature,
                    # not to a nested installation relation.
                    if _self_installation_feature:
                        def _iter_direct_feature_polygons(gp):
                            if gp.get("is_multisurface", False):
                                for poly in gp.get("polygons", []):
                                    yield poly
                            else:
                                yield gp

                        for inst_type in INSTALLATION_TYPES:
                            direct_polys = list(geom_buf.get(inst_type, []))
                            if not direct_polys:
                                continue

                            self_installation_geometry_types_written.add(inst_type)
                            from_solid = any(
                                bool(gp.get("mat") and gp.get("mat").get("in_shell"))
                                for gp in direct_polys
                            )

                            if from_solid:
                                lod_solid_tag = f"lod{lod_level}Solid"
                                lod_solid = SubElement(feature, _C(lod_solid_tag))
                                solid = SubElement(lod_solid, Q("gml", "Solid"))
                                solid.set("srsName", effective_srs_name)
                                solid.set("srsDimension", "3")
                                ext = SubElement(solid, Q("gml", "exterior"))
                                shell = SubElement(ext, Q("gml", "Shell"))

                                for gp in direct_polys:
                                    for poly in _iter_direct_feature_polygons(gp):
                                        if not poly.get("ext_xyz"):
                                            continue
                                        sm = SubElement(shell, Q("gml", "surfaceMember"))
                                        gpoly = SubElement(sm, Q("gml", "Polygon"))
                                        exterior = SubElement(gpoly, Q("gml", "exterior"))
                                        lre = SubElement(exterior, Q("gml", "LinearRing"))
                                        posliste = SubElement(lre, Q("gml", "posList"))
                                        posliste.text = format_poslist(poly["ext_xyz"])
                                        if poly.get("int_xyz"):
                                            for ring in poly["int_xyz"]:
                                                interior = SubElement(gpoly, Q("gml", "interior"))
                                                lri = SubElement(interior, Q("gml", "LinearRing"))
                                                poslisti = SubElement(lri, Q("gml", "posList"))
                                                poslisti.text = format_poslist(ring)
                            else:
                                first_gp = direct_polys[0]
                                lod_ms_tag = f"lod{lod_level}MultiSurface"
                                lod_ms = SubElement(feature, _C(lod_ms_tag))
                                ms_id = first_gp.get("ms_id")
                                ms_attrs = {
                                    GML_ID: (
                                        make_gml_id(str(ms_id), used_gml_ids)
                                        if ms_id
                                        else _new_gml_id("UUID", used_gml_ids)
                                    )
                                }
                                ms = SubElement(lod_ms, Q("gml", "MultiSurface"), ms_attrs)

                                for gp in direct_polys:
                                    for poly in _iter_direct_feature_polygons(gp):
                                        if not poly.get("ext_xyz"):
                                            continue
                                        write_polygon_with_ring_ids(
                                            parent_ms=ms,
                                            poly_gid=poly["poly_gid"],
                                            exterior_ring=poly["ext_xyz"],
                                            interior_rings=poly["int_xyz"] if poly["int_xyz"] else None,
                                            srs_name=srs_name,
                                            exterior_ring_id=poly["ext_id"],
                                            interior_ring_ids=poly["int_ids"] if poly["int_ids"] else None,
                                        )

                    # ── Filling-Kinder vorab sammeln ──
                    # Door/Window-Surfaces mit filling_parent_sid werden nicht als
                    # eigenständige <boundary> exportiert, sondern als
                    # <con:fillingSurface> innerhalb ihrer Eltern-Surface geschrieben.
                    _allowed_inline_parts = _allowed_inline_part_types_for_parent(ns_key, local_name)
                    if _allowed_inline_parts:
                        surface_groups_for_boundaries, inline_part_groups = _split_inline_part_surfaces(
                            geom_buf,
                            _allowed_inline_parts,
                        )
                    else:
                        surface_groups_for_boundaries = geom_buf
                        inline_part_groups = []

                    _FILLING_SURF_TYPES = {"DoorSurface", "WindowSurface", "Door", "Window"}
                    fillings_by_parent: Dict[str, list] = {}  # parent surf_id -> [(surf_type, gp)]
                    openings_by_parent: Dict[str, list] = {}  # parent surf_id -> [gp]
                    for surf_type, polys in surface_groups_for_boundaries.items():
                        for gp in polys:
                            fp = gp.get("filling_parent_sid") or ""
                            if fp and gp.get("opening_info"):
                                openings_by_parent.setdefault(fp, []).append(gp)
                                continue
                            if not fp:
                                # Prüfe auch Unter-Polygone (MultiSurface)
                                if gp.get("is_multisurface", False):
                                    fp = gp.get("filling_parent_sid") or ""
                            if fp and surf_type in _FILLING_SURF_TYPES:
                                fillings_by_parent.setdefault(fp, []).append((surf_type, gp))

                    # Buildings etc.: mit core:boundary und con:*Surface (ohne Installationen)
                    for surf_type, polys in surface_groups_for_boundaries.items():
                        if surf_type in self_installation_geometry_types_written:
                            continue
                        # Überspringe Installation-Typen, die bereits oben behandelt wurden
                        if surf_type in INSTALLATION_TYPES and not _self_installation_feature:
                            # Filtere nur Polygone OHNE in_shell
                            polys = [gp for gp in polys if not (gp.get("mat") and gp.get("mat").get("in_shell"))]
                            if not polys:
                                continue
                        # WindowSurface/DoorSurface WITH filling_parent_sid are handled
                        # inline under their parent wall (see per-polygon checks below).
                        # Those WITHOUT filling_parent_sid are written as standalone boundaries.
                        for gp in polys:
                            # Filling-Entries überspringen – werden inline unter Parent geschrieben
                            fp = gp.get("filling_parent_sid") or ""
                            if fp and gp.get("opening_info"):
                                continue
                            if fp and surf_type in _FILLING_SURF_TYPES:
                                continue

                            # Prüfen, ob dies eine echte MultiSurface ist (mehrere Polygone)
                            if gp.get("is_multisurface", False):
                                # MultiSurface mit mehreren surfaceMembers
                                surf_el, ms_parent = _begin_bs(
                                    feature,
                                    surf_type,
                                    gp.get("surf_id"),
                                    gp.get("ms_id"),
                                    gp.get("mat"),
                                    lod_level,
                                )
                                
                                # Jedes Polygon als surfaceMember schreiben
                                for poly in gp.get("polygons", []):
                                    write_polygon_with_ring_ids(
                                        parent_ms=ms_parent,
                                        poly_gid=poly["poly_gid"],
                                        exterior_ring=poly["ext_xyz"],
                                        interior_rings=poly["int_xyz"] if poly["int_xyz"] else None,
                                        srs_name=srs_name,
                                        exterior_ring_id=poly["ext_id"],
                                        interior_ring_ids=poly["int_ids"] if poly["int_ids"] else None,
                                    )
                                _write_inline_surface_opening(surf_el, gp.get("polygons", []), lod_level)
                            else:
                                # Einzelnes Polygon
                                surf_el, ms_parent = _begin_bs(
                                    feature,
                                    surf_type,
                                    gp.get("surf_id"),
                                    gp.get("ms_id"),
                                    gp.get("mat"),
                                    lod_level,
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
                                _write_inline_surface_opening(surf_el, [gp], lod_level)

                            # ── Filling-Kinder inline schreiben ──
                            # pop() statt get() → jedes Filling-Kind wird nur einmal geschrieben,
                            # auch wenn mehrere Einzel-Polygon-Entries dieselbe surf_id teilen
                            # (z.B. im use_inner_outer_script-Pfad).
                            parent_sid = gp.get("surf_id") or ""
                            for opening_gp in openings_by_parent.pop(parent_sid, []):
                                if opening_gp.get("is_multisurface", False):
                                    _write_inline_surface_opening(
                                        surf_el,
                                        opening_gp.get("polygons", []),
                                        lod_level,
                                    )
                                else:
                                    _write_inline_surface_opening(
                                        surf_el,
                                        [opening_gp],
                                        lod_level,
                                    )
                            for child_surf_type, child_gp in fillings_by_parent.pop(parent_sid, []):
                                # Bestimme den CityGML-Typ (DoorSurface/WindowSurface)
                                filling_wrapper = SubElement(surf_el, Q("con", "fillingSurface"))
                                # con_surface_id als gml:id der Filling-Surface
                                child_sid = child_gp.get("surf_id") or ""
                                child_ms_id = child_gp.get("ms_id") or ""
                                child_mat = child_gp.get("mat")
                                filling_surf = SubElement(filling_wrapper, Q("con", child_surf_type))
                                if child_sid:
                                    validated_child_sid = make_gml_id(str(child_sid), used_gml_ids)
                                    filling_surf.set(GML_ID, validated_child_sid)
                                else:
                                    filling_surf.set(GML_ID, _new_gml_id("UUID", used_gml_ids))

                                _write_surface_generics_on_surface(filling_surf, child_mat)

                                # lodNMultiSurface
                                lod_tag = f"lod{lod_level}MultiSurface"
                                child_lod = SubElement(filling_surf, _C(lod_tag))
                                child_ms_attrs = {
                                    GML_ID: (
                                        make_gml_id(str(child_ms_id), used_gml_ids)
                                        if child_ms_id
                                        else _new_gml_id("UUID", used_gml_ids)
                                    )
                                }
                                child_ms = SubElement(child_lod, Q("gml", "MultiSurface"), child_ms_attrs)

                                def _canonicalize_child_poly(poly_data: dict) -> tuple[str, str, list[str]]:
                                    poly_gid = str(poly_data.get("poly_gid") or "").strip()
                                    ext_id = str(poly_data.get("ext_id") or "").strip()
                                    int_ids = list(poly_data.get("int_ids") or [])
                                    poly_match = re.match(r"^(.*)_2$", poly_gid)
                                    ext_match = re.match(r"^(.*_\d+_)_2$", ext_id)
                                    if poly_match and ext_match:
                                        candidate_poly_gid = poly_match.group(1)
                                        candidate_ext_id = ext_match.group(1)
                                        if candidate_ext_id.startswith(candidate_poly_gid + "_"):
                                            poly_gid = candidate_poly_gid
                                            ext_id = candidate_ext_id
                                            int_ids = [
                                                re.match(r"^(.*_\d+_)_2$", str(rid or "").strip()).group(1)
                                                if re.match(r"^(.*_\d+_)_2$", str(rid or "").strip())
                                                else str(rid or "").strip()
                                                for rid in int_ids
                                            ]
                                    return poly_gid, ext_id, int_ids

                                if child_gp.get("is_multisurface", False):
                                    for poly in child_gp.get("polygons", []):
                                        child_poly_gid, child_ext_id, child_int_ids = _canonicalize_child_poly(poly)
                                        write_polygon_with_ring_ids(
                                            parent_ms=child_ms,
                                            poly_gid=child_poly_gid,
                                            exterior_ring=poly["ext_xyz"],
                                            interior_rings=poly["int_xyz"] if poly["int_xyz"] else None,
                                            srs_name=srs_name,
                                            exterior_ring_id=child_ext_id,
                                            interior_ring_ids=child_int_ids if child_int_ids else None,
                                        )
                                else:
                                    child_poly_gid, child_ext_id, child_int_ids = _canonicalize_child_poly(child_gp)
                                    write_polygon_with_ring_ids(
                                        parent_ms=child_ms,
                                        poly_gid=child_poly_gid,
                                        exterior_ring=child_gp["ext_xyz"],
                                        interior_rings=child_gp["int_xyz"] if child_gp["int_xyz"] else None,
                                        srs_name=srs_name,
                                        exterior_ring_id=child_ext_id,
                                        interior_ring_ids=child_int_ids if child_int_ids else None,
                                    )

                    _write_inline_part_features(feature, inline_part_groups, openings_by_parent, lod_level)

                    # ── Fallback: orphaned fillings/openings that didn't match any wall ──
                    # Write them as standalone boundary surfaces so they are not silently lost.
                    for _fp_key, _orphan_openings in openings_by_parent.items():
                        for _o_gp in _orphan_openings:
                            _o_info = _o_gp.get("opening_info") or {}
                            _o_surf_type = _o_info.get("filling_surface_type") or "WindowSurface"
                            surf_el_o, ms_parent_o = _begin_bs(
                                feature, _o_surf_type,
                                _o_gp.get("surf_id"), _o_gp.get("ms_id"),
                                _o_gp.get("mat"), lod_level,
                            )
                            if _o_gp.get("is_multisurface", False):
                                for _o_poly in _o_gp.get("polygons", []):
                                    write_polygon_with_ring_ids(
                                        parent_ms=ms_parent_o,
                                        poly_gid=_o_poly["poly_gid"],
                                        exterior_ring=_o_poly["ext_xyz"],
                                        interior_rings=_o_poly["int_xyz"] if _o_poly["int_xyz"] else None,
                                        srs_name=srs_name,
                                        exterior_ring_id=_o_poly["ext_id"],
                                        interior_ring_ids=_o_poly["int_ids"] if _o_poly["int_ids"] else None,
                                    )
                            else:
                                write_polygon_with_ring_ids(
                                    parent_ms=ms_parent_o,
                                    poly_gid=_o_gp["poly_gid"],
                                    exterior_ring=_o_gp["ext_xyz"],
                                    interior_rings=_o_gp["int_xyz"] if _o_gp["int_xyz"] else None,
                                    srs_name=srs_name,
                                    exterior_ring_id=_o_gp["ext_id"],
                                    interior_ring_ids=_o_gp["int_ids"] if _o_gp["int_ids"] else None,
                                )
                    for _fp_key, _orphan_fillings in fillings_by_parent.items():
                        for _f_surf_type, _f_gp in _orphan_fillings:
                            surf_el_f, ms_parent_f = _begin_bs(
                                feature, _f_surf_type,
                                _f_gp.get("surf_id"), _f_gp.get("ms_id"),
                                _f_gp.get("mat"), lod_level,
                            )
                            if _f_gp.get("is_multisurface", False):
                                for _f_poly in _f_gp.get("polygons", []):
                                    write_polygon_with_ring_ids(
                                        parent_ms=ms_parent_f,
                                        poly_gid=_f_poly["poly_gid"],
                                        exterior_ring=_f_poly["ext_xyz"],
                                        interior_rings=_f_poly["int_xyz"] if _f_poly["int_xyz"] else None,
                                        srs_name=srs_name,
                                        exterior_ring_id=_f_poly["ext_id"],
                                        interior_ring_ids=_f_poly["int_ids"] if _f_poly["int_ids"] else None,
                                    )
                            else:
                                write_polygon_with_ring_ids(
                                    parent_ms=ms_parent_f,
                                    poly_gid=_f_gp["poly_gid"],
                                    exterior_ring=_f_gp["ext_xyz"],
                                    interior_rings=_f_gp["int_xyz"] if _f_gp["int_xyz"] else None,
                                    srs_name=srs_name,
                                    exterior_ring_id=_f_gp["ext_id"],
                                    interior_ring_ids=_f_gp["int_ids"] if _f_gp["int_ids"] else None,
                                )

                    # Optional: write reference-only lod{N}Solid that points to the already exported polygons.
                    if write_lod_solid_refs:
                        lod_solid_tag = f"lod{lod_level}Solid"
                        lod_solid = SubElement(feature, _C(lod_solid_tag))
                        solid = SubElement(lod_solid, Q("gml", "Solid"), {GML_ID: _new_gml_id("UUID", used_gml_ids)})
                        ext = SubElement(solid, Q("gml", "exterior"))
                        shell = SubElement(ext, Q("gml", "Shell"), {GML_ID: _new_gml_id("UUID", used_gml_ids)})

                        for surf_type, polys in surface_groups_for_boundaries.items():
                            # For the parent feature's Solid, never reference installation polygons.
                            # Installations (Building/Bridge/Tunnel + interior variants) are exported separately
                            # and must not be referenced by the main Building/Bridge/Tunnel lod{n}Solid.
                            if surf_type in INSTALLATION_TYPES and not _self_installation_feature:
                                continue
                            
                            for gp in polys:
                                # Filling-Entries überspringen – sind inline unter Parent
                                fp = gp.get("filling_parent_sid") or ""
                                if fp and surf_type in _FILLING_SURF_TYPES:
                                    continue
                                if gp.get("is_multisurface", False):
                                    # MultiSurface: alle enthaltenen Polygone referenzieren
                                    for poly in gp.get("polygons", []):
                                        SubElement(
                                            shell,
                                            Q("gml", "surfaceMember"),
                                            {Q("xlink", "href"): f"#{poly['poly_gid']}"}
                                        )
                                else:
                                    # Einzelnes Polygon
                                    SubElement(
                                        shell,
                                        Q("gml", "surfaceMember"),
                                        {Q("xlink", "href"): f"#{gp['poly_gid']}"}
                                    )

        # CityGML-spezifische Attribute NACH der Geometrie exportieren
        # (außer für TINRelief - dort wurden sie bereits VOR dem:tin geschrieben)
        # No-op for FME compatibility: feature-local appearance is disabled.
        _flush_pending_feature_appearance(feature)
        if not ((ns_key, local_name) == ("dem", "TINRelief")):
            _write_specific_attributes(feature, _hier_parent or obj)

        # Storey-Kinder sammeln für spätere Inline-Einbettung (CityGML 3.0)
        if (ns_key, local_name) in (("bldg", "Building"), ("bldg", "BuildingPart")):
            _storey_source = _hier_parent or obj
            for child in _storey_source.children:
                if not object_is_viewport_visible(child, context):
                    continue
                c_feat = child.get("cgml3_feature")
                if c_feat == "Storey":
                    _storey_obj_to_building_el[id(child)] = feature
                elif c_feat == "BuildingPart":
                    _part_obj_to_building_el[id(child)] = feature
                elif c_feat in ("BuildingInstallation", "IntBuildingInstallation"):
                    _inst_obj_to_building_el[id(child)] = feature

        _register_inline_relation_children(_hier_parent or obj, feature, ns_key, local_name)

        # Inline buildingRoom export for hierarchical buildings with rooms (CityGML 3.0)
        if _hier_parent and (ns_key, local_name) in (("bldg", "Building"), ("bldg", "BuildingPart")):
            for child in _hier_parent.children:
                if not object_is_viewport_visible(child, context):
                    continue
                if child.get("structure_part") == "room":
                    _export_hier_room_v3(
                        child, feature, context, trf, fmt,
                        Q, NS, GML_ID, _feature_id,
                        ox, oy, oz, all_bbox_coords,
                        lod_level,
                    )

        # Inline buildingRoom export for Storey with Room children (CityGML 3.0)
        # Storey sits one level above Rooms in the hierarchy
        if (ns_key, local_name) == ("bldg", "Storey"):
            # Storey → Building Zuordnung registrieren (Mesh-Pfad)
            _bld_el = _storey_obj_to_building_el.get(id(obj))
            if _bld_el is not None:
                _storey_to_building_el[feat_id] = _bld_el

            for child in obj.children:
                if not object_is_viewport_visible(child, context):
                    continue
                if child.get("structure_part") == "room":
                    _export_hier_room_v3(
                        child, feature, context, trf, fmt,
                        Q, NS, GML_ID, _feature_id,
                        ox, oy, oz, all_bbox_coords,
                        lod_level,
                    )

        # BuildingPart → Parent Building Zuordnung registrieren (Mesh-Pfad)
        if (ns_key, local_name) == ("bldg", "BuildingPart"):
            _bld_el = _part_obj_to_building_el.get(id(obj))
            if _bld_el is not None:
                _part_to_building_el[feat_id] = _bld_el

        # BuildingInstallation → Parent Building Zuordnung registrieren (Mesh-Pfad)
        if (ns_key, local_name) == ("bldg", "BuildingInstallation"):
            _bld_el = _inst_obj_to_building_el.get(id(obj))
            if _bld_el is not None:
                _inst_to_building_el[feat_id] = _bld_el

        _inline_rel = _inline_relation_obj_to_parent_el.get(id(obj))
        if _inline_rel is not None:
            _parent_el, _rel_ns, _rel_name, _child_ns, _child_local = _inline_rel
            if (ns_key, local_name) == (_child_ns, _child_local):
                _inline_relation_to_parent_el[(feat_id, ns_key, local_name)] = (
                    _parent_el, _rel_ns, _rel_name
                )

        # Mark hierarchical parent EMPTY as inline-exported to prevent duplicate export
        if _hier_parent:
            try:
                _hier_parent["_inline_exported"] = True
            except Exception:
                pass

        obj_eval.to_mesh_clear()
        all_bbox_coords.extend(bbox_coords_abs)

    # ── Post-Processing: Storey-Features inline unter ihr parent Building verschieben ──
    if _storey_to_building_el:
        _bldg_storey_tag = Q("bldg", "Storey")
        # Alle cityObjectMember-Kindelemente durchsuchen
        for com_el in list(root):
            com_tag = getattr(com_el, "tag", "") or ""
            if not com_tag.endswith("cityObjectMember") and com_tag != "cityObjectMember":
                continue
            # Schaue, ob das erste Kind ein bldg:Storey ist
            for child_el in list(com_el):
                child_tag = getattr(child_el, "tag", "") or ""
                if child_tag != _bldg_storey_tag:
                    continue
                # gml:id auslesen
                storey_gml_id = child_el.get(GML_ID) or child_el.get(f"{{{NS['gml']}}}id") or ""
                if storey_gml_id not in _storey_to_building_el:
                    continue
                building_el = _storey_to_building_el[storey_gml_id]
                # bldg:Storey aus cityObjectMember entfernen und in Building einbetten
                com_el.remove(child_el)
                sub_el = SubElement(building_el, Q("bldg", "buildingSubdivision"))
                sub_el.append(child_el)
                # Leeren cityObjectMember entfernen
                if len(com_el) == 0:
                    root.remove(com_el)
                break

    # ── Post-Processing: BuildingPart-Features inline unter ihr parent Building verschieben ──
    if _part_to_building_el:
        _bldg_part_tag = Q("bldg", "BuildingPart")
        for com_el in list(root):
            com_tag = getattr(com_el, "tag", "") or ""
            if not com_tag.endswith("cityObjectMember") and com_tag != "cityObjectMember":
                continue
            for child_el in list(com_el):
                child_tag = getattr(child_el, "tag", "") or ""
                if child_tag != _bldg_part_tag:
                    continue
                part_gml_id = child_el.get(GML_ID) or child_el.get(f"{{{NS['gml']}}}id") or ""
                if part_gml_id not in _part_to_building_el:
                    continue
                building_el = _part_to_building_el[part_gml_id]
                # bldg:BuildingPart aus cityObjectMember entfernen und in Building einbetten
                com_el.remove(child_el)
                rel_el = SubElement(building_el, Q("bldg", "buildingPart"))
                rel_el.append(child_el)
                # Leeren cityObjectMember entfernen
                if len(com_el) == 0:
                    root.remove(com_el)
                break

    # ── Post-Processing: BuildingInstallation-Features inline unter ihr parent Building verschieben ──
    if _inst_to_building_el:
        _bldg_inst_tag = Q("bldg", "BuildingInstallation")
        for com_el in list(root):
            com_tag = getattr(com_el, "tag", "") or ""
            if not com_tag.endswith("cityObjectMember") and com_tag != "cityObjectMember":
                continue
            for child_el in list(com_el):
                child_tag = getattr(child_el, "tag", "") or ""
                if child_tag != _bldg_inst_tag:
                    continue
                inst_gml_id = child_el.get(GML_ID) or child_el.get(f"{{{NS['gml']}}}id") or ""
                if inst_gml_id not in _inst_to_building_el:
                    continue
                building_el = _inst_to_building_el[inst_gml_id]
                # bldg:BuildingInstallation aus cityObjectMember entfernen und in Building einbetten
                com_el.remove(child_el)
                rel_el = SubElement(building_el, Q("bldg", "buildingInstallation"))
                rel_el.append(child_el)
                # Leeren cityObjectMember entfernen
                if len(com_el) == 0:
                    root.remove(com_el)
                break

    # ── Post-Processing: XSD-konforme Kindelement-Reihenfolge ──
    if _inline_relation_to_parent_el:
        def _tag_ns_local(el):
            tag = getattr(el, "tag", "") or ""
            if "}" not in tag:
                return "", tag
            uri, local = tag[1:].split("}", 1)
            for key, ns_uri in NS.items():
                if ns_uri == uri:
                    return key, local
            return "", local

        for com_el in list(root):
            com_tag = getattr(com_el, "tag", "") or ""
            if not com_tag.endswith("cityObjectMember") and com_tag != "cityObjectMember":
                continue
            for child_el in list(com_el):
                child_ns, child_local = _tag_ns_local(child_el)
                child_gml_id = child_el.get(GML_ID) or child_el.get(f"{{{NS['gml']}}}id") or ""
                rel_target = _inline_relation_to_parent_el.get((child_gml_id, child_ns, child_local))
                if rel_target is None:
                    continue
                parent_el, rel_ns, rel_name = rel_target
                com_el.remove(child_el)
                rel_el = SubElement(parent_el, Q(rel_ns, rel_name))
                rel_el.append(child_el)
                if len(com_el) == 0:
                    root.remove(com_el)
                break

    _FEATURE_TAGS_BUILDING = {
        Q("bldg", "Building"), Q("bldg", "BuildingPart"),
        Q("bldg", "Storey"), Q("bldg", "BuildingInstallation"),
    }
    _FEATURE_TAGS_BRIDGE_TUNNEL = {
        Q("brid", "Bridge"), Q("brid", "BridgePart"),
        Q("brid", "BridgeInstallation"), Q("brid", "BridgeFurniture"),
        Q("tun", "Tunnel"), Q("tun", "TunnelPart"),
        Q("tun", "TunnelInstallation"), Q("tun", "TunnelFurniture"),
        Q("tun", "HollowSpace"),
    }
    _FEATURE_TAGS_ROOM = {Q("bldg", "BuildingRoom")}
    _ALL_FEATURE_TAGS = _FEATURE_TAGS_BUILDING | _FEATURE_TAGS_BRIDGE_TUNNEL | _FEATURE_TAGS_ROOM
    for el in root.iter():
        el_tag = getattr(el, "tag", "") or ""
        if el_tag in _FEATURE_TAGS_BUILDING or el_tag in _FEATURE_TAGS_BRIDGE_TUNNEL:
            _reorder_feature_children(el, is_room=False)
        elif el_tag in _FEATURE_TAGS_ROOM:
            _reorder_feature_children(el, is_room=True)

    # appearanceMember am CityModel-Ende zusammenfassen
    # CityGML 3: core namespace is default, so the element is usually unprefixed.
    app_members = [child for child in list(root) if getattr(child, "tag", None) == "appearanceMember"]
    if app_members:
        for am in app_members:
            root.remove(am)
        for am in app_members:
            root.append(am)
    
    if all_bbox_coords:
        add_bounded_by(root, all_bbox_coords, _epsg_num)


    out_dir = os.path.dirname(os.path.abspath(filepath))
    os.makedirs(os.path.join(out_dir, "appearance"), exist_ok=True)
    
    # Semantische Validierung (optional, bei aktiviertem Debug-Modus)
    validation_warnings = []
    try:
        scene = getattr(context, "scene", None)
        if scene and hasattr(scene, "cgml3") and getattr(scene.cgml3, "xsd_validate", False):
            from .helpers import validate_semantic_rules
            validate_semantic_rules(root, validation_warnings)
            if validation_warnings:
                print("[CityGML3] Semantische Validierungs-Warnungen:")
                for warn in validation_warnings:
                    print(f"  - {warn}")
    except Exception as e:
        print(f"[CityGML3] Semantische Validierung fehlgeschlagen: {e}")
    
    from .helpers import pretty_xml
    # Write pretty-printed XML with explicit declaration matching import__v3.gml
    # (ElementTree's xml_declaration does not support standalone="yes").
    xml = pretty_xml(root)
    if not xml.lstrip().startswith("<?xml"):
        xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + xml
    else:
        # Normalize declaration line
        xml = re.sub(
            r'^<\\?xml[^>]*\\?>',
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            xml,
            count=1,
            flags=re.IGNORECASE,
        )
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(xml)

    # Debug: count how many features made it into the final document with implicit reps
    try:
        if export_debug:
            count_feats = 0
            count_impl = 0
            for el in root.iter():
                tag = getattr(el, "tag", None)
                if not isinstance(tag, str):
                    continue
                if _ln(tag) in ("CityFurniture", "SolitaryVegetationObject", "PlantCover"):
                    count_feats += 1
                # CityGML 3 implicit representation tags are in the core namespace.
                # Depending on how ElementTree serializes them, they can show up as
                #   - prefixed:    {core-ns}lod2ImplicitRepresentation
                #   - unprefixed:  lod2ImplicitRepresentation (default-ns)
                # This check is localname-based, so just match the local part.
                if _ln(tag).startswith("lod") and _ln(tag).endswith("ImplicitRepresentation"):
                    count_impl += 1
            _dbg(f"final_doc feature_count={count_feats} implicit_rep_count={count_impl}")
    except Exception:
        pass
