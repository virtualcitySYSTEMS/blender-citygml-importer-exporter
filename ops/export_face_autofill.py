# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
from __future__ import annotations

from typing import Dict, Optional

import numpy as np
from mathutils import Vector

META_KEYS = (
    "gml_polygon_id",
    "gml_ring_id",
    "con_surface_id",
    "SurfaceTyp",
    "surface_type",
    "app_target_or_uri",
    "opening_type",
    "OpeningType",
    "con_opening_id",
    "opening_id",
    "opening_gml_id",
    "filling_parent_surface_id",
    "opening_surface_id",
    "opening_surface_type",
    "Interior",
    "ExteriorPolyId",
    "ExteriorRingId",
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
)

EPS = 1e-9
ROOF_NORMAL_Z_MIN = 0.85
INTERIOR_MATCH_MAX_PLANE_DIST = 0.05
INTERIOR_MATCH_MIN_OPPOSITE_DOT = -0.85
INTERIOR_RELAXED_MIN_OPPOSITE_DOT = -0.25
INTERIOR_MATCH_AREA_EPS = 1e-6
INTERIOR_MATCH_MIN_VERTEX_RATIO = 0.6
SOURCE_MATCH_MAX_PLANE_DIST = 0.05
SOURCE_MATCH_MIN_NORMAL_DOT = 0.85
CONTACT_MATCH_MIN_PARALLEL_DOT = 0.85


class ExportMaterialProxy:
    """Read-only material proxy used only during export."""

    def __init__(
        self,
        source_material=None,
        *,
        overrides: Optional[dict] = None,
        inherit_all_props: bool = False,
        name: str = "",
    ):
        self._source_material = source_material
        self._overrides = dict(overrides or {})
        self._inherit_all_props = bool(inherit_all_props)
        self.name = name or getattr(source_material, "name", "ExportMaterialProxy")

    def get(self, key, default=None):
        if key in self._overrides:
            return self._overrides[key]
        if self._inherit_all_props and self._source_material is not None:
            try:
                return self._source_material.get(key, default)
            except Exception:
                return default
        return default

    def items(self):
        merged = {}
        if self._inherit_all_props and self._source_material is not None:
            try:
                merged.update(dict(self._source_material.items()))
            except Exception:
                pass
        merged.update(self._overrides)
        return merged.items()

    def keys(self):
        return [k for (k, _v) in self.items()]

    def __contains__(self, key):
        if key in self._overrides:
            return True
        if self._inherit_all_props and self._source_material is not None:
            try:
                return key in self._source_material.keys()
            except Exception:
                return False
        return False

    def __getattr__(self, name):
        src = object.__getattribute__(self, "_source_material")
        if src is None:
            raise AttributeError(name)
        return getattr(src, name)


def _ensure_closed(seq):
    if not seq:
        return seq
    return seq if seq[0] == seq[-1] else (list(seq) + [seq[0]])


def _open_ring(seq):
    data = list(seq or [])
    if len(data) >= 2 and data[0] == data[-1]:
        return data[:-1]
    return data


def _material_for_face(obj, poly):
    try:
        mi = int(poly.material_index)
    except Exception:
        mi = -1
    if mi < 0 or mi >= len(obj.material_slots):
        return None
    try:
        return obj.material_slots[mi].material
    except Exception:
        return None


def _poly_prop(obj, mesh, face_index: int, key: str, default=None):
    for poly_seq in (
        getattr(getattr(obj, "data", None), "polygons", None),
        getattr(mesh, "polygons", None),
    ):
        if poly_seq is None:
            continue
        try:
            poly = poly_seq[face_index]
        except Exception:
            continue
        try:
            val = poly.get(key, default)
        except Exception:
            val = default
        if val not in (None, ""):
            return val
    return default


def _material_has_metadata(mat) -> bool:
    if mat is None:
        return False
    for key in META_KEYS:
        try:
            val = mat.get(key, None)
        except Exception:
            val = None
        if val not in (None, ""):
            return True
    return False


def _surface_type_from_material(mat) -> str:
    if mat is None:
        return ""
    for key in ("SurfaceTyp", "surface_type"):
        try:
            val = mat.get(key, None)
        except Exception:
            val = None
        if val:
            return str(val).strip()
    return ""


def _safe_face_normal_world(verts_world) -> Vector:
    pts = list(verts_world or [])
    if len(pts) < 3:
        return Vector((0.0, 0.0, 1.0))
    v0 = pts[0]
    for idx in range(1, len(pts) - 1):
        cross = (pts[idx] - v0).cross(pts[idx + 1] - v0)
        if cross.length > EPS:
            return cross.normalized()
    return Vector((0.0, 0.0, 1.0))


def _classify_surface_type(normal_world: Vector) -> str:
    try:
        n = normal_world.normalized()
    except Exception:
        return "WallSurface"
    return "RoofSurface" if abs(float(n.z)) >= ROOF_NORMAL_Z_MIN else "WallSurface"


def _plane_axes(normal_world: Vector) -> tuple[Vector, Vector]:
    n = normal_world.normalized()
    x = Vector((1.0, 0.0, 0.0)) if abs(float(n.x)) < 0.9 else Vector((0.0, 1.0, 0.0))
    y = n.cross(x)
    if y.length <= EPS:
        y = Vector((0.0, 1.0, 0.0))
    y.normalize()
    x = y.cross(n)
    if x.length <= EPS:
        x = Vector((1.0, 0.0, 0.0))
    x.normalize()
    return x, y


def _project_points_to_plane_2d(points, origin: Vector, axis_x: Vector, axis_y: Vector):
    out = []
    for pt in points or []:
        dv = pt - origin
        out.append((float(dv.dot(axis_x)), float(dv.dot(axis_y))))
    return out


def _signed_area_2d(points) -> float:
    area = 0.0
    for idx, (x1, y1) in enumerate(points):
        x2, y2 = points[(idx + 1) % len(points)]
        area += x1 * y2 - x2 * y1
    return 0.5 * area


def _point_on_segment_2d(point, start, end, tol=1e-6) -> bool:
    px, py = point
    x1, y1 = start
    x2, y2 = end
    cross = (px - x1) * (y2 - y1) - (py - y1) * (x2 - x1)
    if abs(cross) > tol:
        return False
    dot = (px - x1) * (px - x2) + (py - y1) * (py - y2)
    return dot <= tol


def _point_in_polygon_2d(point, polygon, tol=1e-6) -> bool:
    if not polygon:
        return False
    for idx, start in enumerate(polygon):
        end = polygon[(idx + 1) % len(polygon)]
        if _point_on_segment_2d(point, start, end, tol=tol):
            return True

    x, y = point
    inside = False
    count = len(polygon)
    for idx in range(count):
        x1, y1 = polygon[idx]
        x2, y2 = polygon[(idx + 1) % count]
        if (y1 > y) != (y2 > y):
            x_int = (x2 - x1) * (y - y1) / ((y2 - y1) + 1e-18) + x1
            if x < x_int:
                inside = not inside
    return inside


def _count_points_inside_polygon_2d(points, polygon, tol=1e-6) -> int:
    return sum(1 for point in points or [] if _point_in_polygon_2d(point, polygon, tol=tol))


def _distance_point_plane(point: Vector, origin: Vector, normal: Vector) -> float:
    return abs(float((point - origin).dot(normal)))


def _find_interior_reference_face(target, face_data, desired_surface_type: str):
    target_normal = target["normal_world"]
    target_verts = _open_ring(target.get("verts_world") or [])
    if len(target_verts) < 3:
        return None

    candidates = []
    for data in face_data.values():
        if not data.get("is_reference"):
            continue
        if desired_surface_type and data.get("surface_type") != desired_surface_type:
            continue
        candidates.append(data)

    if not candidates and desired_surface_type:
        for data in face_data.values():
            if data.get("is_reference"):
                candidates.append(data)

    best = None
    best_score = None
    for cand in candidates:
        cand_normal = cand.get("normal_world")
        cand_verts = _open_ring(cand.get("verts_world") or [])
        if cand_normal is None or len(cand_verts) < 3:
            continue

        try:
            dot = float(cand_normal.normalized().dot(target_normal.normalized()))
        except Exception:
            dot = 1.0
        if dot > INTERIOR_MATCH_MIN_OPPOSITE_DOT:
            continue

        origin = cand_verts[0]
        axis_x, axis_y = _plane_axes(cand_normal)
        if any(_distance_point_plane(pt, origin, cand_normal) > INTERIOR_MATCH_MAX_PLANE_DIST for pt in target_verts):
            continue

        cand_2d = _project_points_to_plane_2d(cand_verts, origin, axis_x, axis_y)
        target_2d = _project_points_to_plane_2d(target_verts, origin, axis_x, axis_y)
        if len(cand_2d) < 3 or len(target_2d) < 3:
            continue

        inside_count = _count_points_inside_polygon_2d(target_2d, cand_2d)
        center_2d = _project_points_to_plane_2d([target["center_world"]], origin, axis_x, axis_y)
        center_inside = bool(center_2d) and _point_in_polygon_2d(center_2d[0], cand_2d)
        full_containment = inside_count == len(target_2d)
        enough_vertices_inside = inside_count >= max(1, int(round(len(target_2d) * INTERIOR_MATCH_MIN_VERTEX_RATIO)))
        if not (full_containment or (center_inside and enough_vertices_inside)):
            continue

        cand_area = abs(_signed_area_2d(cand_2d))
        target_area = abs(_signed_area_2d(target_2d))
        if cand_area + INTERIOR_MATCH_AREA_EPS < target_area:
            continue

        try:
            center_distance = float((cand["center_world"] - target["center_world"]).length)
        except Exception:
            center_distance = 0.0
        containment_penalty = 0.0 if full_containment else 1000.0
        score = containment_penalty + abs(cand_area - target_area) * 100.0 + center_distance
        if best_score is None or score < best_score:
            best_score = score
            best = cand
    return best


def _find_relaxed_interior_reference_face(target, face_data, desired_surface_type: str):
    target_normal = target["normal_world"]
    target_verts = _open_ring(target.get("verts_world") or [])
    if len(target_verts) < 3:
        return None

    candidates = []
    for data in face_data.values():
        if not data.get("is_reference"):
            continue
        if desired_surface_type and data.get("surface_type") != desired_surface_type:
            continue
        candidates.append(data)

    if not candidates and desired_surface_type:
        for data in face_data.values():
            if data.get("is_reference"):
                candidates.append(data)

    best = None
    best_score = None
    for cand in candidates:
        cand_normal = cand.get("normal_world")
        cand_verts = _open_ring(cand.get("verts_world") or [])
        if cand_normal is None or len(cand_verts) < 3:
            continue

        try:
            dot = float(cand_normal.normalized().dot(target_normal.normalized()))
        except Exception:
            dot = 1.0
        if dot > INTERIOR_RELAXED_MIN_OPPOSITE_DOT:
            continue

        origin = cand_verts[0]
        axis_x, axis_y = _plane_axes(cand_normal)
        plane_distances = [_distance_point_plane(pt, origin, cand_normal) for pt in target_verts]
        max_plane_distance = max(plane_distances or [0.0])
        if max_plane_distance > SOURCE_MATCH_MAX_PLANE_DIST * 2.0:
            continue

        cand_2d = _project_points_to_plane_2d(cand_verts, origin, axis_x, axis_y)
        target_2d = _project_points_to_plane_2d(target_verts, origin, axis_x, axis_y)
        if len(cand_2d) < 3 or len(target_2d) < 3:
            continue

        cand_area = abs(_signed_area_2d(cand_2d))
        target_area = abs(_signed_area_2d(target_2d))
        if cand_area + INTERIOR_MATCH_AREA_EPS < (target_area * 0.5):
            continue

        try:
            center_distance = float((cand["center_world"] - target["center_world"]).length)
        except Exception:
            center_distance = 0.0

        score = ((dot + 1.0) * 500.0) + (max_plane_distance * 1000.0) + (abs(cand_area - target_area) * 50.0) + center_distance
        if best_score is None or score < best_score:
            best_score = score
            best = cand
    return best


def _find_contact_reference_face(target, face_data, desired_surface_type: str):
    """Find the exterior face touched by a joined object-part face.

    Boolean caps produced by imported OBJ parts do not always get the
    opposite normal direction that the regular interior-ring detector expects.
    For Join Object Parts we can still identify the semantic interior by
    coplanar containment in an existing exterior reference face.
    """

    target_normal = target["normal_world"]
    target_verts = _open_ring(target.get("verts_world") or [])
    if len(target_verts) < 3:
        return None

    preferred = []
    fallback = []
    for data in face_data.values():
        if not data.get("is_reference"):
            continue
        mat = data.get("material")
        try:
            if mat is not None and bool(mat.get("Interior")):
                continue
        except Exception:
            pass
        if int(data.get("index", -1)) == int(target.get("index", -2)):
            continue
        if desired_surface_type and data.get("surface_type") == desired_surface_type:
            preferred.append(data)
        else:
            fallback.append(data)

    best = None
    best_score = None
    for cand in preferred + fallback:
        cand_normal = cand.get("normal_world")
        cand_verts = _open_ring(cand.get("verts_world") or [])
        if cand_normal is None or len(cand_verts) < 3:
            continue

        try:
            dot = float(cand_normal.normalized().dot(target_normal.normalized()))
        except Exception:
            dot = 0.0
        parallel_dot = abs(dot)
        if parallel_dot < CONTACT_MATCH_MIN_PARALLEL_DOT:
            continue

        origin = cand_verts[0]
        axis_x, axis_y = _plane_axes(cand_normal)
        plane_distances = [_distance_point_plane(pt, origin, cand_normal) for pt in target_verts]
        max_plane_distance = max(plane_distances or [0.0])
        if max_plane_distance > SOURCE_MATCH_MAX_PLANE_DIST * 2.0:
            continue

        cand_2d = _project_points_to_plane_2d(cand_verts, origin, axis_x, axis_y)
        target_2d = _project_points_to_plane_2d(target_verts, origin, axis_x, axis_y)
        if len(cand_2d) < 3 or len(target_2d) < 3:
            continue

        inside_count = _count_points_inside_polygon_2d(target_2d, cand_2d)
        center_2d = _project_points_to_plane_2d([target["center_world"]], origin, axis_x, axis_y)
        center_inside = bool(center_2d) and _point_in_polygon_2d(center_2d[0], cand_2d)
        full_containment = inside_count == len(target_2d)
        enough_vertices_inside = inside_count >= max(1, int(round(len(target_2d) * INTERIOR_MATCH_MIN_VERTEX_RATIO)))
        if not (full_containment or (center_inside and enough_vertices_inside)):
            continue

        cand_area = abs(_signed_area_2d(cand_2d))
        target_area = abs(_signed_area_2d(target_2d))
        if cand_area + INTERIOR_MATCH_AREA_EPS < target_area:
            continue

        try:
            center_distance = float((cand["center_world"] - target["center_world"]).length)
        except Exception:
            center_distance = 0.0
        surface_penalty = 0.0 if cand in preferred else 10000.0
        containment_penalty = 0.0 if full_containment else 1000.0
        score = (
            surface_penalty
            + containment_penalty
            + ((1.0 - parallel_dot) * 1000.0)
            + (max_plane_distance * 1000.0)
            + (abs(cand_area - target_area) * 50.0)
            + center_distance
        )
        if best_score is None or score < best_score:
            best_score = score
            best = cand
    return best


def _find_matching_source_face(target, source_face_data, *, allow_opposite: bool = False) -> Optional[dict]:
    if not source_face_data:
        return None

    target_normal = target["normal_world"]
    target_verts = _open_ring(target.get("verts_world") or [])
    if len(target_verts) < 3:
        return None

    best = None
    best_score = None
    for cand in source_face_data:
        cand_normal = cand.get("normal_world")
        cand_verts = _open_ring(cand.get("verts_world") or [])
        if cand_normal is None or len(cand_verts) < 3:
            continue

        try:
            dot = float(cand_normal.normalized().dot(target_normal.normalized()))
        except Exception:
            dot = -1.0
        match_dot = abs(dot) if allow_opposite else dot
        if match_dot < SOURCE_MATCH_MIN_NORMAL_DOT:
            continue

        origin = cand_verts[0]
        axis_x, axis_y = _plane_axes(cand_normal)
        if any(_distance_point_plane(pt, origin, cand_normal) > SOURCE_MATCH_MAX_PLANE_DIST for pt in target_verts):
            continue

        cand_2d = _project_points_to_plane_2d(cand_verts, origin, axis_x, axis_y)
        target_2d = _project_points_to_plane_2d(target_verts, origin, axis_x, axis_y)
        if len(cand_2d) < 3 or len(target_2d) < 3:
            continue

        inside_count = _count_points_inside_polygon_2d(target_2d, cand_2d)
        center_2d = _project_points_to_plane_2d([target["center_world"]], origin, axis_x, axis_y)
        center_inside = bool(center_2d) and _point_in_polygon_2d(center_2d[0], cand_2d)
        full_containment = inside_count == len(target_2d)
        enough_vertices_inside = inside_count >= max(1, int(round(len(target_2d) * INTERIOR_MATCH_MIN_VERTEX_RATIO)))
        if not (full_containment or (center_inside and enough_vertices_inside)):
            continue

        cand_area = abs(_signed_area_2d(cand_2d))
        target_area = abs(_signed_area_2d(target_2d))
        if cand_area + INTERIOR_MATCH_AREA_EPS < target_area:
            continue

        try:
            center_distance = float((cand["center_world"] - target["center_world"]).length)
        except Exception:
            center_distance = 0.0
        score = abs(cand_area - target_area) * 100.0 + center_distance
        if best_score is None or score < best_score:
            best_score = score
            best = cand
    return best


def _fit_uvs_from_reference(source_xyz, source_uvs, target_xyz):
    src_xyz = _open_ring(source_xyz)
    src_uvs = _open_ring(source_uvs)
    tgt_xyz = _open_ring(target_xyz)

    if len(src_xyz) < 3 or len(src_xyz) != len(src_uvs) or not tgt_xyz:
        return None

    normal = _safe_face_normal_world(src_xyz)
    if normal.length <= EPS:
        return None

    origin = src_xyz[0]
    axis_x, axis_y = _plane_axes(normal)

    def _to_2d(points):
        out = []
        for pt in points:
            dv = pt - origin
            out.append((float(dv.dot(axis_x)), float(dv.dot(axis_y))))
        return out

    src_2d = _to_2d(src_xyz)
    tgt_2d = _to_2d(tgt_xyz)

    if len(src_2d) < 3:
        return None

    A = np.asarray([[x, y, 1.0] for (x, y) in src_2d], dtype=np.float64)
    bu = np.asarray([float(u) for (u, _v) in src_uvs], dtype=np.float64)
    bv = np.asarray([float(v) for (_u, v) in src_uvs], dtype=np.float64)

    try:
        coeff_u, *_rest_u = np.linalg.lstsq(A, bu, rcond=None)
        coeff_v, *_rest_v = np.linalg.lstsq(A, bv, rcond=None)
    except Exception:
        return None

    out = []
    for x, y in tgt_2d:
        u = float(coeff_u[0] * x + coeff_u[1] * y + coeff_u[2])
        v = float(coeff_v[0] * x + coeff_v[1] * y + coeff_v[2])
        out.append((u, v))
    return _ensure_closed(out)


def _find_reference_face(target, face_data, desired_surface_type: str):
    target_center = target["center_world"]
    target_normal = target["normal_world"]

    candidates = []
    for data in face_data.values():
        if not data.get("is_reference"):
            continue
        if desired_surface_type and data.get("surface_type") != desired_surface_type:
            continue
        candidates.append(data)

    if not candidates and desired_surface_type:
        for data in face_data.values():
            if data.get("is_reference"):
                candidates.append(data)

    best = None
    best_score = None
    for cand in candidates:
        try:
            dot = abs(float(cand["normal_world"].normalized().dot(target_normal.normalized())))
        except Exception:
            dot = 0.0
        angle_penalty = 1.0 - max(0.0, min(1.0, dot))
        try:
            dist = float((cand["center_world"] - target_center).length)
        except Exception:
            dist = 0.0
        score = angle_penalty * 1000.0 + dist
        if best_score is None or score < best_score:
            best_score = score
            best = cand
    return best


def _collect_face_data(obj, obj_eval, mesh):
    data = {}
    uv_layer = getattr(mesh.uv_layers, "active", None)
    uv_data = getattr(uv_layer, "data", None) if uv_layer is not None else None

    for poly in mesh.polygons:
        verts_world = []
        for li in poly.loop_indices:
            vi = mesh.loops[li].vertex_index
            verts_world.append(obj_eval.matrix_world @ mesh.vertices[vi].co)

        center_world = Vector((0.0, 0.0, 0.0))
        if verts_world:
            center_world = sum(verts_world, Vector((0.0, 0.0, 0.0))) / len(verts_world)

        uvs = None
        if uv_data is not None and poly.loop_indices:
            uvs = []
            for li in poly.loop_indices:
                uv = uv_data[li].uv
                uvs.append((float(uv.x), float(uv.y)))
            uvs = _ensure_closed(uvs)

        mat = _material_for_face(obj, poly)
        poly_gid = _poly_prop(obj, mesh, poly.index, "gml_id", "")
        has_metadata = bool(poly_gid) or _material_has_metadata(mat)
        surface_type = _surface_type_from_material(mat)
        normal_world = _safe_face_normal_world(verts_world)

        data[poly.index] = {
            "index": poly.index,
            "material": mat,
            "verts_world": _ensure_closed(verts_world),
            "center_world": center_world,
            "normal_world": normal_world,
            "uvs": uvs,
            "has_metadata": has_metadata,
            "is_reference": bool(has_metadata and mat is not None),
            "surface_type": surface_type or _classify_surface_type(normal_world),
        }

    return data


def prepare_new_face_export_overrides(obj, obj_eval, mesh, detected_pairs, source_face_data=None) -> Dict[int, dict]:
    """Build export-time overrides for metadata-less faces."""

    face_data = _collect_face_data(obj, obj_eval, mesh)
    overrides: Dict[int, dict] = {}

    inner_to_outer = {}
    for outer, inner in detected_pairs or []:
        try:
            outer_idx = int(outer["index"])
            inner_idx = int(inner["index"])
        except Exception:
            continue
        inner_to_outer[inner_idx] = outer_idx

    new_face_indices = [idx for (idx, data) in face_data.items() if not data.get("has_metadata")]

    for idx in new_face_indices:
        if idx in inner_to_outer:
            continue
        target = face_data.get(idx)
        if target is None:
            continue
        desired_surface_type = _classify_surface_type(target["normal_world"])
        matched_outer = _find_interior_reference_face(target, face_data, desired_surface_type)
        if matched_outer is None:
            matched_outer = _find_relaxed_interior_reference_face(target, face_data, desired_surface_type)
        if matched_outer is None and source_face_data is not None:
            matched_outer = _find_contact_reference_face(target, face_data, desired_surface_type)
        if matched_outer is not None:
            inner_to_outer[idx] = int(matched_outer["index"])
            continue

        # Only skip original source faces after we tried to link them as
        # interior rings. For OBJ/source parts the contact face can be an
        # original source face and still be the new interior ring of the
        # target polygon.
        if _find_matching_source_face(target, source_face_data, allow_opposite=True) is not None:
            continue

    # Pass 1: standalone new faces and new outers get a base material/surface override.
    for idx in new_face_indices:
        if idx in inner_to_outer:
            continue
        target = face_data[idx]
        surface_type = _classify_surface_type(target["normal_world"])
        ref = _find_reference_face(target, face_data, surface_type)
        ref_mat = ref.get("material") if ref else None
        proxy = ExportMaterialProxy(
            ref_mat,
            overrides={
                "SurfaceTyp": surface_type,
                "surface_type": surface_type,
                # New exterior faces must export without inherited texture nodes/UV mappings.
                "cgml3_skip_texture_export": True,
                "face_index": idx,
                "source_material": getattr(ref_mat, "name", ""),
            },
            inherit_all_props=False,
            name=f"{surface_type}_{idx}_export_proxy",
        )
        overrides[idx] = {
            "mat": proxy,
            "surface_type": surface_type,
            "uvs": _fit_uvs_from_reference(ref["verts_world"], ref["uvs"], target["verts_world"]) if ref and ref.get("uvs") else None,
            "skip_texture_export": True,
            "source_face_index": ref["index"] if ref else None,
        }

    # Pass 2: new inner faces inherit from their matched outer face.
    for inner_idx, outer_idx in inner_to_outer.items():
        if inner_idx not in new_face_indices:
            continue

        inner_face = face_data.get(inner_idx)
        outer_face = face_data.get(outer_idx)
        if inner_face is None or outer_face is None:
            continue

        outer_override = overrides.get(outer_idx)
        source_mat = None
        source_type = outer_face.get("surface_type") or _classify_surface_type(outer_face["normal_world"])
        source_uvs = outer_face.get("uvs")

        if outer_override:
            source_mat = outer_override.get("mat")
            if outer_override.get("surface_type"):
                source_type = outer_override["surface_type"]
            if outer_override.get("uvs"):
                source_uvs = outer_override["uvs"]

        if source_mat is None:
            source_mat = outer_face.get("material")

        ext_poly_id = ""
        ext_ring_id = ""
        for mat_like in (source_mat, outer_face.get("material")):
            if mat_like is None:
                continue
            try:
                ext_poly_id = str(mat_like.get("gml_polygon_id", "") or mat_like.get("ExteriorPolyId", "") or "")
            except Exception:
                ext_poly_id = ""
            try:
                ext_ring_id = str(mat_like.get("gml_ring_id", "") or mat_like.get("ExteriorRingId", "") or "")
            except Exception:
                ext_ring_id = ""
            if ext_poly_id or ext_ring_id:
                break

        proxy = ExportMaterialProxy(
            source_mat,
            overrides={
                "Interior": True,
                "SurfaceTyp": source_type,
                "surface_type": source_type,
                "cgml3_skip_texture_export": False,
                "ExteriorPolyId": ext_poly_id,
                "ExteriorRingId": ext_ring_id,
                "face_index": inner_idx,
                "source_material": getattr(source_mat, "name", ""),
            },
            inherit_all_props=True,
            name=f"{source_type}_{inner_idx}_interior_export_proxy",
        )

        overrides[inner_idx] = {
            "mat": proxy,
            "surface_type": source_type,
            "uvs": _fit_uvs_from_reference(
                outer_face["verts_world"],
                source_uvs,
                inner_face["verts_world"],
            ) if source_uvs else None,
            "source_face_index": outer_idx,
            "is_interior": True,
            "skip_texture_export": False,
        }

    return overrides
