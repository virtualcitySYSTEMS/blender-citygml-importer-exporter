# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Join material-less object parts into touching mesh objects.
"""

from __future__ import annotations

import math
from uuid import uuid4

import bmesh
import bpy
from bpy.props import BoolProperty, EnumProperty
from bpy.types import Menu, Operator
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from ..modeltyper.citygml import get_surface_color
from .export_face_autofill import prepare_new_face_export_overrides
from .get_inner_and_outer_rings import detect_face_groups

ROOF_NORMAL_Z_MIN = 0.85
TOUCH_EPSILON_MIN = 0.005
TOUCH_EPSILON_MAX = 0.1
TOUCH_EPSILON_REL = 0.001
VERTEX_SAMPLE_LIMIT = 64

# Mapping: generic join_mode + feature_type → concrete CityGML surface type
_PART_TYPE_BY_FEATURE = {
    "Building": "BuildingPart",
    "BuildingPart": "BuildingPart",
    "Bridge": "BridgePart",
    "BridgePart": "BridgePart",
    "Tunnel": "TunnelPart",
    "TunnelPart": "TunnelPart",
}
_INSTALLATION_TYPE_BY_FEATURE = {
    "Building": "BuildingInstallation",
    "BuildingPart": "BuildingInstallation",
    "Bridge": "BridgeInstallation",
    "BridgePart": "BridgeInstallation",
    "Tunnel": "TunnelInstallation",
    "TunnelPart": "TunnelInstallation",
}
_FURNITURE_TYPE_BY_FEATURE = {
    "Building": "BuildingFurniture",
    "BuildingPart": "BuildingFurniture",
    "Bridge": "BridgeFurniture",
    "BridgePart": "BridgeFurniture",
    "Tunnel": "TunnelFurniture",
    "TunnelPart": "TunnelFurniture",
}
_CONSTRUCTION_ELEMENT_TYPE_BY_FEATURE = {
    "Bridge": "BridgeConstructionElement",
    "BridgePart": "BridgeConstructionElement",
}
_CONSTRUCTION_ELEMENT_ALLOWED_FEATURES = set(_CONSTRUCTION_ELEMENT_TYPE_BY_FEATURE.keys())


def _detect_feature_type(obj) -> str:
    """Return the CityGML feature type of *obj* (e.g. 'Building', 'Bridge', 'Tunnel')."""
    for key in ("cgml3_feature", "feature_type", "ModelType"):
        try:
            val = str(obj.get(key, "") or "").strip()
        except Exception:
            continue
        if val:
            return val
    return "Building"


def _supports_construction_element_join(obj) -> bool:
    return _detect_feature_type(obj) in _CONSTRUCTION_ELEMENT_ALLOWED_FEATURES


def _construction_element_block_message(target) -> str:
    feature_type = _detect_feature_type(target)
    target_name = getattr(target, "name", "target")
    return (
        "BridgeConstructionElement can only be used on Bridge or BridgePart targets; "
        f"found {feature_type} on {target_name}"
    )


def _object_has_real_materials(obj) -> bool:
    if obj is None or obj.type != 'MESH':
        return False
    try:
        return any(mat is not None for mat in obj.data.materials)
    except Exception:
        return False


def _object_has_citygml_metadata(obj) -> bool:
    if obj is None:
        return False

    for key in ("ModelType", "cgml3_feature", "feature_type", "gml_id"):
        try:
            value = obj.get(key)
        except Exception:
            value = None
        if value not in (None, ""):
            return True

    if obj.type == 'MESH':
        try:
            for mat in obj.data.materials:
                if mat is None:
                    continue
                if mat.get("SurfaceTyp") or mat.get("surface_type") or mat.get("FeatureType"):
                    return True
        except Exception:
            pass

    return False


def _is_citygml_mesh(obj) -> bool:
    return _object_has_citygml_metadata(obj) or _object_has_real_materials(obj)


def _selected_source_meshes(context):
    return [
        obj for obj in context.selected_objects
        if obj.type == 'MESH' and not _is_citygml_mesh(obj)
    ]


def _world_bbox(obj):
    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    mins = Vector((
        min(co.x for co in corners),
        min(co.y for co in corners),
        min(co.z for co in corners),
    ))
    maxs = Vector((
        max(co.x for co in corners),
        max(co.y for co in corners),
        max(co.z for co in corners),
    ))
    return mins, maxs


def _bbox_gap(min_a, max_a, min_b, max_b) -> float:
    dx = max(0.0, min_b.x - max_a.x, min_a.x - max_b.x)
    dy = max(0.0, min_b.y - max_a.y, min_a.y - max_b.y)
    dz = max(0.0, min_b.z - max_a.z, min_a.z - max_b.z)
    return math.sqrt(dx * dx + dy * dy + dz * dz)


class _MeshProbe:
    def __init__(self, obj, depsgraph):
        self.obj = obj
        self.eval_obj = obj.evaluated_get(depsgraph)
        self.mesh = self.eval_obj.to_mesh()
        self.verts_world = []
        self.bvh = None

        try:
            if self.mesh is not None and len(self.mesh.vertices) > 0 and len(self.mesh.polygons) > 0:
                self.verts_world = [self.eval_obj.matrix_world @ vert.co for vert in self.mesh.vertices]
                polygons = [tuple(poly.vertices) for poly in self.mesh.polygons if len(poly.vertices) >= 3]
                if polygons:
                    self.bvh = BVHTree.FromPolygons(self.verts_world, polygons, all_triangles=False)
        except Exception:
            self.verts_world = []
            self.bvh = None

        if self.verts_world:
            self.bbox_min = Vector((
                min(co.x for co in self.verts_world),
                min(co.y for co in self.verts_world),
                min(co.z for co in self.verts_world),
            ))
            self.bbox_max = Vector((
                max(co.x for co in self.verts_world),
                max(co.y for co in self.verts_world),
                max(co.z for co in self.verts_world),
            ))
        else:
            self.bbox_min, self.bbox_max = _world_bbox(obj)

        self.diag = (self.bbox_max - self.bbox_min).length

    def clear(self):
        try:
            self.eval_obj.to_mesh_clear()
        except Exception:
            pass


def _touch_tolerance(source_probe: _MeshProbe, target_probe: _MeshProbe) -> float:
    diag = max(source_probe.diag, target_probe.diag, 1.0)
    return max(TOUCH_EPSILON_MIN, min(TOUCH_EPSILON_MAX, diag * TOUCH_EPSILON_REL))


def _sample_min_distance(coords, bvh) -> float | None:
    if not coords or bvh is None:
        return None

    step = max(1, len(coords) // VERTEX_SAMPLE_LIMIT)
    min_distance = None
    for co in coords[::step]:
        hit = bvh.find_nearest(co)
        if not hit or hit[0] is None:
            continue
        distance = (hit[0] - co).length
        if min_distance is None or distance < min_distance:
            min_distance = distance
    return min_distance


def _mesh_objects_for_search(context, source):
    objects = []
    for obj in context.scene.objects:
        if obj == source or obj.type != 'MESH':
            continue
        if not _is_citygml_mesh(obj):
            continue
        try:
            visible = obj.visible_get(view_layer=context.view_layer)
        except TypeError:
            visible = obj.visible_get()
        except Exception:
            visible = True
        if visible:
            objects.append(obj)
    return objects


def _find_touching_target(context, source):
    depsgraph = context.evaluated_depsgraph_get()
    probes = {}

    def get_probe(obj):
        probe = probes.get(obj.name)
        if probe is None:
            probe = _MeshProbe(obj, depsgraph)
            probes[obj.name] = probe
        return probe

    best_target = None
    best_score = None

    try:
        source_probe = get_probe(source)
        if source_probe.bvh is None:
            return None

        for candidate in _mesh_objects_for_search(context, source):
            target_probe = get_probe(candidate)
            if target_probe.bvh is None:
                continue

            tolerance = _touch_tolerance(source_probe, target_probe)
            gap = _bbox_gap(
                source_probe.bbox_min,
                source_probe.bbox_max,
                target_probe.bbox_min,
                target_probe.bbox_max,
            )
            if gap > tolerance:
                continue

            overlap_count = 0
            try:
                overlap = source_probe.bvh.overlap(target_probe.bvh)
                overlap_count = len(overlap or [])
            except Exception:
                overlap_count = 0

            min_distance = 0.0 if overlap_count else None
            if not overlap_count:
                distances = [
                    _sample_min_distance(source_probe.verts_world, target_probe.bvh),
                    _sample_min_distance(target_probe.verts_world, source_probe.bvh),
                ]
                distances = [dist for dist in distances if dist is not None]
                if not distances:
                    continue
                min_distance = min(distances)
                if min_distance > tolerance:
                    continue

            has_metadata = _object_has_real_materials(candidate) or _object_has_citygml_metadata(candidate)
            metadata_rank = 0 if has_metadata else 1
            selected_rank = 0 if not candidate.select_get() else 1
            score = (
                metadata_rank,
                selected_rank,
                min_distance,
                gap,
                -overlap_count,
                candidate.name.lower(),
            )
            if best_score is None or score < best_score:
                best_score = score
                best_target = candidate
    finally:
        for probe in probes.values():
            probe.clear()

    return best_target


def _face_surface_labels(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()

    normal_matrix = obj.matrix_world.to_3x3()
    labels = []

    for face in bm.faces:
        world_normal = normal_matrix @ face.normal
        if world_normal.length_squared <= 1e-12:
            labels.append("WallSurface")
            continue
        world_normal.normalize()
        if float(world_normal.z) >= ROOF_NORMAL_Z_MIN:
            labels.append("RoofSurface")
        elif float(world_normal.z) <= -ROOF_NORMAL_Z_MIN:
            labels.append("GroundSurface")
        else:
            labels.append("WallSurface")

    bm.free()
    return labels


def _capture_source_face_data(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()

    normal_matrix = obj.matrix_world.to_3x3()
    face_data = []
    for face in bm.faces:
        verts_world = [obj.matrix_world @ vert.co for vert in face.verts]
        if len(verts_world) < 3:
            continue

        world_normal = normal_matrix @ face.normal
        if world_normal.length_squared <= 1e-12:
            continue
        world_normal.normalize()

        center_world = sum(verts_world, Vector((0.0, 0.0, 0.0))) / len(verts_world)
        face_data.append({
            "verts_world": verts_world,
            "center_world": center_world,
            "normal_world": world_normal,
        })

    bm.free()
    return face_data


def _remove_property_if_present(id_data, key):
    try:
        if key in id_data.keys():
            del id_data[key]
    except Exception:
        pass


def _new_join_token() -> str:
    return uuid4().hex


def _new_polygon_id() -> str:
    return f"UUID_{uuid4().hex}"


def _placeholder_material_name(join_token: str) -> str:
    return f"CGML3_JOIN_TMP_{join_token[:8]}"


def _get_poly_material(obj, poly_index):
    try:
        poly = obj.data.polygons[poly_index]
        material_index = int(poly.material_index)
    except Exception:
        return None
    if material_index < 0 or material_index >= len(obj.data.materials):
        return None
    try:
        return obj.data.materials[material_index]
    except Exception:
        return None


def _is_placeholder_material(mat, join_token: str) -> bool:
    if mat is None:
        return False
    try:
        return str(mat.get("cgml3_join_token", "")) == join_token
    except Exception:
        return False


def _assign_placeholder_material(obj, join_token: str):
    placeholder = bpy.data.materials.new(name=_placeholder_material_name(join_token))
    placeholder["cgml3_join_token"] = join_token
    placeholder["cgml3_join_placeholder"] = True

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()

    obj.data.materials.clear()
    obj.data.materials.append(placeholder)
    for face in bm.faces:
        face.material_index = 0

    bm.to_mesh(obj.data)
    bm.free()


def _collect_placeholder_faces(obj, join_token: str):
    face_indices = []
    for poly in obj.data.polygons:
        if _is_placeholder_material(_get_poly_material(obj, poly.index), join_token):
            face_indices.append(poly.index)
    return face_indices


def _collect_existing_ring_indices(obj, join_token: str):
    ring_indices = {}
    for mat in obj.data.materials:
        if mat is None or _is_placeholder_material(mat, join_token):
            continue

        try:
            poly_id = str(mat.get("gml_polygon_id", "") or "")
            ring_id = str(mat.get("gml_ring_id", "") or "")
        except Exception:
            continue

        if not poly_id or not ring_id:
            continue
        if not ring_id.startswith(f"{poly_id}_") or not ring_id.endswith("_"):
            continue

        suffix = ring_id[len(poly_id) + 1:-1]
        try:
            index = int(suffix)
        except Exception:
            continue
        ring_indices.setdefault(poly_id, set()).add(index)
    return ring_indices


def _allocate_ring_id(poly_id: str, ring_indices) -> str:
    used = ring_indices.setdefault(poly_id, set())
    next_index = 0
    while next_index in used:
        next_index += 1
    used.add(next_index)
    return f"{poly_id}_{next_index}_"


def _normalize_face_uvs(uvs, loop_count: int):
    values = list(uvs or [])
    if not values:
        return []
    if len(values) >= 2 and values[0] == values[-1]:
        values = values[:-1]
    if len(values) == loop_count:
        return values
    if len(values) + 1 == loop_count and values:
        return values + [values[0]]
    return values[:loop_count]


def _order_uvs_for_face(face, ring_xyz, ring_uvs, tol=1e-6):
    def _same_xyz(a, b):
        return (
            abs(a[0] - b[0]) <= tol
            and abs(a[1] - b[1]) <= tol
            and abs(a[2] - b[2]) <= tol
        )

    coords = list(ring_xyz or [])
    uvs = list(ring_uvs or [])
    if len(coords) >= 2 and _same_xyz(coords[0], coords[-1]):
        coords = coords[:-1]
    if len(uvs) >= 2 and uvs[0] == uvs[-1]:
        uvs = uvs[:-1]
    if not coords or not uvs:
        return uvs

    coord_map = {}
    for index, (x, y, z) in enumerate(coords):
        coord_map[(round(x, 6), round(y, 6), round(z, 6))] = index

    loop_order = []
    for loop in face.loops:
        vx = float(loop.vert.co.x)
        vy = float(loop.vert.co.y)
        vz = float(loop.vert.co.z)
        key = (round(vx, 6), round(vy, 6), round(vz, 6))
        best_index = coord_map.get(key, -1)
        if best_index == -1:
            best_dist = 1e30
            for index, (x, y, z) in enumerate(coords):
                dist = (vx - x) * (vx - x) + (vy - y) * (vy - y) + (vz - z) * (vz - z)
                if dist < best_dist:
                    best_dist = dist
                    best_index = index
        loop_order.append(best_index if 0 <= best_index < len(uvs) else 0)

    ordered = [uvs[index] for index in loop_order]

    def _signed_area(points):
        area = 0.0
        for index in range(len(points)):
            x1, y1 = points[index]
            x2, y2 = points[(index + 1) % len(points)]
            area += x1 * y2 - x2 * y1
        return 0.5 * area

    if len(ordered) >= 3 and _signed_area(ordered) < 0:
        ordered.reverse()
    return ordered


def _mesh_face_local_xyz(obj, face_index):
    try:
        poly = obj.data.polygons[face_index]
    except Exception:
        return []

    coords = []
    for loop_index in poly.loop_indices:
        try:
            vertex_index = obj.data.loops[loop_index].vertex_index
            co = obj.data.vertices[vertex_index].co
        except Exception:
            continue
        coords.append((float(co.x), float(co.y), float(co.z)))
    return coords


def _assign_face_uvs(face, uv_layer, uvs, ring_xyz=None):
    if uv_layer is None:
        return
    ordered = _normalize_face_uvs(uvs, len(face.loops))
    if len(ordered) != len(face.loops):
        return
    if ring_xyz:
        ordered = _order_uvs_for_face(face, ring_xyz, ordered)
    for loop, (u, v) in zip(face.loops, ordered):
        loop[uv_layer].uv = (float(u), float(v))


def _store_uv_start_by_ring(obj, mat, uvs):
    try:
        ring_id = str(mat.get("gml_ring_id", "") or "")
    except Exception:
        ring_id = ""
    ordered = _normalize_face_uvs(uvs, len(uvs or []))
    if not ring_id or not ordered:
        return

    start_uv = (float(ordered[0][0]), float(ordered[0][1]))
    try:
        mat["cgml3_uv_start"] = start_uv
    except Exception:
        pass
    try:
        existing = dict(obj.data.get("cgml3_uv_start_by_ring", {}) or {})
        existing[ring_id] = start_uv
        obj.data["cgml3_uv_start_by_ring"] = existing
    except Exception:
        pass


def _source_material_from_override(mat_like):
    if isinstance(mat_like, bpy.types.Material):
        return mat_like
    try:
        return object.__getattribute__(mat_like, "_source_material")
    except Exception:
        return None


def _create_join_material(surface_type: str, *, reference_material=None, feature_type: str = "Building"):
    if reference_material is not None:
        mat = reference_material.copy()
        mat.name = f"{surface_type}_join_{uuid4().hex[:8]}"
    else:
        mat = bpy.data.materials.new(name=f"{surface_type}_join_{uuid4().hex[:8]}")
        mat.use_nodes = True
        color = get_surface_color(surface_type, feature_type.upper(), "2.0")
        try:
            bsdf = mat.node_tree.nodes.get("Principled BSDF")
        except Exception:
            bsdf = None
        if bsdf:
            try:
                bsdf.inputs["Base Color"].default_value = color
                bsdf.inputs["Roughness"].default_value = 0.7
                bsdf.inputs["Specular"].default_value = 0.1
            except Exception:
                pass

    mat["SurfaceTyp"] = surface_type
    mat["surface_type"] = surface_type
    _remove_property_if_present(mat, "FeatureType")
    _remove_property_if_present(mat, "feature_type")
    _remove_property_if_present(mat, "cgml3_join_token")
    _remove_property_if_present(mat, "cgml3_join_placeholder")
    _remove_property_if_present(mat, "Interior")
    _remove_property_if_present(mat, "ExteriorPolyId")
    _remove_property_if_present(mat, "ExteriorRingId")
    return mat


def _cleanup_unused_materials(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()

    used_indices = {int(face.material_index) for face in bm.faces}
    old_materials = list(obj.data.materials)
    index_map = {}
    new_materials = []

    for idx, mat in enumerate(old_materials):
        if idx in used_indices:
            index_map[idx] = len(new_materials)
            new_materials.append(mat)

    for face in bm.faces:
        face.material_index = index_map.get(int(face.material_index), 0)

    obj.data.materials.clear()
    for mat in new_materials:
        obj.data.materials.append(mat)

    bm.to_mesh(obj.data)
    bm.free()


def _build_inner_to_outer_map(detected_pairs):
    mapping = {}
    for outer, inner in detected_pairs or []:
        try:
            outer_index = int(outer["index"])
            inner_index = int(inner["index"])
        except Exception:
            continue
        mapping[inner_index] = outer_index
    return mapping


def _extend_inner_to_outer_map_from_overrides(mapping, face_overrides):
    merged = dict(mapping or {})
    for face_index, override in (face_overrides or {}).items():
        if not isinstance(override, dict) or not override.get("is_interior"):
            continue
        try:
            source_face_index = int(override.get("source_face_index"))
        except Exception:
            continue
        merged[int(face_index)] = source_face_index
    return merged


def _resolve_existing_outer_metadata(target, outer_index, face_overrides):
    outer_mat = None
    outer_override = face_overrides.get(outer_index)
    if outer_override:
        outer_mat = outer_override.get("mat")
    if outer_mat is None:
        outer_mat = _get_poly_material(target, outer_index)

    poly_id = ""
    ring_id = ""
    surface_type = ""
    for mat_like in (outer_mat, _source_material_from_override(outer_mat)):
        if mat_like is None:
            continue
        try:
            poly_id = str(mat_like.get("gml_polygon_id", "") or mat_like.get("ExteriorPolyId", "") or poly_id or "")
        except Exception:
            pass
        try:
            ring_id = str(mat_like.get("gml_ring_id", "") or mat_like.get("ExteriorRingId", "") or ring_id or "")
        except Exception:
            pass
        try:
            surface_type = str(mat_like.get("SurfaceTyp", "") or mat_like.get("surface_type", "") or surface_type or "")
        except Exception:
            pass
        if poly_id and ring_id and surface_type:
            break
    return poly_id, ring_id, surface_type


def _detect_target_lod(target, join_token: str):
    """Ermittelt das LoD-Level aus den vorhandenen Materialien des Target-Objekts."""
    for mat in target.data.materials:
        if mat is None or _is_placeholder_material(mat, join_token):
            continue
        lod_val = mat.get("lod")
        if lod_val is not None:
            try:
                return int(lod_val)
            except (ValueError, TypeError):
                pass
    return None


def _set_boolean_float_solver(modifier):
    """Use Blender's float/legacy boolean solver across Blender versions."""
    for solver in ("FLOAT", "FAST"):
        try:
            modifier.solver = solver
            return
        except TypeError:
            continue

    try:
        available = [
            item.identifier
            for item in modifier.bl_rna.properties["solver"].enum_items
        ]
    except Exception:
        available = []
    raise TypeError(
        "Boolean Float solver not available"
        + (f"; available solvers: {', '.join(available)}" if available else "")
    )


def _apply_join_materials_to_target(target, join_mode: str, join_token: str, source_face_data=None, create_closure_surface=False):
    placeholder_faces = _collect_placeholder_faces(target, join_token)
    if not placeholder_faces:
        return

    # Feature-Typ vom Target-Objekt ermitteln (Building / Bridge / Tunnel)
    feature_type = _detect_feature_type(target)
    if join_mode == 'CONSTRUCTION_ELEMENT' and feature_type not in _CONSTRUCTION_ELEMENT_TYPE_BY_FEATURE:
        raise ValueError(_construction_element_block_message(target))

    # LoD-Level vom Target-Objekt ermitteln (aus vorhandenen Materialien)
    target_lod = _detect_target_lod(target, join_token)

    detected_pairs = detect_face_groups(target, target.data) or []
    face_overrides = prepare_new_face_export_overrides(
        target,
        target,
        target.data,
        detected_pairs,
        source_face_data=source_face_data,
    )
    inner_to_outer = _extend_inner_to_outer_map_from_overrides(
        _build_inner_to_outer_map(detected_pairs),
        face_overrides,
    )
    ring_indices = _collect_existing_ring_indices(target, join_token)
    fallback_labels = _face_surface_labels(target)
    feature_group_id = f"ID_{uuid4().hex}"
    feature_multisurface_id = f"ID_{uuid4().hex}"
    subfeature_type = ""
    if join_mode == 'PART':
        subfeature_type = _PART_TYPE_BY_FEATURE.get(feature_type, "BuildingPart")
    elif join_mode == 'INSTALLATION':
        subfeature_type = _INSTALLATION_TYPE_BY_FEATURE.get(feature_type, "BuildingInstallation")
    boundary_group_ids = {}

    def _boundary_ids_for_surface(surface_type: str):
        key = str(surface_type or "WallSurface")
        ids = boundary_group_ids.get(key)
        if ids is None:
            ids = (f"ID_{uuid4().hex}", f"ID_{uuid4().hex}")
            boundary_group_ids[key] = ids
        return ids

    generated_outer_meta = {}

    # Pre-populate interior counter from existing materials on the target.
    # Existing interiors have gml_polygon_id = "{exterior_poly_id}_{N}".
    # We need to start numbering AFTER the highest existing N.
    interior_counter_by_exterior = {}  # exterior_poly_id -> next interior number
    for mat in target.data.materials:
        if mat is None or _is_placeholder_material(mat, join_token):
            continue
        if not mat.get("Interior"):
            continue
        ext_pid = str(mat.get("ExteriorPolyId", "") or "")
        int_pid = str(mat.get("gml_polygon_id", "") or "")
        if not ext_pid or not int_pid:
            continue
        # Extract suffix: int_pid should be "{ext_pid}_{N}"
        if int_pid.startswith(f"{ext_pid}_"):
            suffix = int_pid[len(ext_pid) + 1:]
            try:
                num = int(suffix)
                existing_next = interior_counter_by_exterior.get(ext_pid, 1)
                if num >= existing_next:
                    interior_counter_by_exterior[ext_pid] = num + 1
            except (ValueError, TypeError):
                pass
    for face_index in placeholder_faces:
        override = face_overrides.get(face_index) or {}
        if face_index in inner_to_outer or bool(override.get("is_interior")):
            continue
        if join_mode in {'PART', 'INSTALLATION'}:
            surface_type = str(override.get("surface_type") or fallback_labels[face_index] or "WallSurface")
        elif join_mode == 'FURNITURE':
            surface_type = _FURNITURE_TYPE_BY_FEATURE.get(feature_type, "BuildingFurniture")
        elif join_mode == 'CONSTRUCTION_ELEMENT':
            surface_type = _CONSTRUCTION_ELEMENT_TYPE_BY_FEATURE[feature_type]
        else:
            surface_type = str(override.get("surface_type") or fallback_labels[face_index] or "WallSurface")
        poly_id = _new_polygon_id()
        ring_id = _allocate_ring_id(poly_id, ring_indices)
        generated_outer_meta[face_index] = {
            "surface_type": surface_type,
            "poly_id": poly_id,
            "ring_id": ring_id,
        }

    # Track interior faces for optional ClosureSurface creation
    interior_bmfaces_for_closure = []

    bm = bmesh.new()
    bm.from_mesh(target.data)
    bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv.verify()

    for face_index in placeholder_faces:
        face = bm.faces[face_index]
        override = face_overrides.get(face_index) or {}
        override_mat = override.get("mat")
        reference_material = _source_material_from_override(override_mat)
        is_interior = face_index in inner_to_outer or bool(override.get("is_interior"))

        poly_id = ""
        ring_id = ""
        exterior_poly_id = ""
        exterior_ring_id = ""

        if is_interior:
            outer_index = inner_to_outer.get(face_index)
            if outer_index in generated_outer_meta:
                outer_meta = generated_outer_meta[outer_index]
                surface_type = outer_meta["surface_type"]
                exterior_poly_id = outer_meta["poly_id"]
                exterior_ring_id = outer_meta["ring_id"]
            else:
                if outer_index is not None:
                    exterior_poly_id, exterior_ring_id, exterior_surface_type = _resolve_existing_outer_metadata(
                        target,
                        outer_index,
                        face_overrides,
                    )
                else:
                    exterior_poly_id, exterior_ring_id, exterior_surface_type = "", "", ""
                surface_type = exterior_surface_type or str(
                    override.get("surface_type")
                    or (fallback_labels[outer_index] if outer_index is not None else "")
                    or fallback_labels[face_index]
                    or "WallSurface"
                )

            if not exterior_poly_id:
                exterior_poly_id = _new_polygon_id()
            if not exterior_ring_id:
                exterior_ring_id = _allocate_ring_id(exterior_poly_id, ring_indices)

            # Sicherstellen, dass Index 0 (Exterior) als belegt registriert ist,
            # damit die Interior immer Index >= 1 bekommt (CityGML3-Konvention).
            ring_indices.setdefault(exterior_poly_id, set()).add(0)

            # Interior bekommt eigene Polygon-ID: exterior_poly_id + "_N" (aufnummeriert)
            interior_num = interior_counter_by_exterior.get(exterior_poly_id, 1)
            interior_counter_by_exterior[exterior_poly_id] = interior_num + 1
            poly_id = f"{exterior_poly_id}_{interior_num}"
            # Ring-ID ist eine neue UUID (unabhängig von der Polygon-ID)
            ring_id = f"UUID_{uuid4().hex}"
        else:
            outer_meta = generated_outer_meta.get(face_index)
            surface_type = outer_meta["surface_type"] if outer_meta else "WallSurface"
            poly_id = outer_meta["poly_id"] if outer_meta else _new_polygon_id()
            ring_id = outer_meta["ring_id"] if outer_meta else _allocate_ring_id(poly_id, ring_indices)

        if join_mode == 'SURFACES' and not is_interior:
            surface_type = str(override.get("surface_type") or surface_type or fallback_labels[face_index] or "WallSurface")

        if join_mode == 'SURFACES' and is_interior and not surface_type:
            surface_type = str(override.get("surface_type") or fallback_labels[face_index] or "WallSurface")

        if bool(override.get("skip_texture_export")):
            reference_material = None

        if join_mode in {'PART', 'INSTALLATION', 'FURNITURE', 'CONSTRUCTION_ELEMENT'} and not is_interior:
            reference_material = None

        mat = _create_join_material(surface_type, reference_material=reference_material, feature_type=feature_type)
        mat["gml_polygon_id"] = poly_id
        mat["gml_ring_id"] = ring_id

        if target_lod is not None:
            mat["lod"] = target_lod

        if join_mode in {'PART', 'INSTALLATION', 'FURNITURE', 'CONSTRUCTION_ELEMENT'}:
            if join_mode in {'PART', 'INSTALLATION'}:
                surf_group_id, surf_ms_id = _boundary_ids_for_surface(surface_type)
                mat["con_surface_id"] = surf_group_id
                mat["gml_multisurface_id"] = surf_ms_id
                mat["cgml3_subfeature_type"] = subfeature_type
                mat["cgml3_subfeature_id"] = feature_group_id
                mat["cgml3_subfeature_parent_id"] = str(target.get("gml_id", "") or "")
            else:
                mat["con_surface_id"] = feature_group_id
                mat["gml_multisurface_id"] = feature_multisurface_id
            mat["is_multisurface_member"] = True
            if join_mode == 'INSTALLATION':
                mat["in_shell"] = True

        if is_interior:
            mat["Interior"] = True
            mat["ExteriorPolyId"] = exterior_poly_id
            mat["ExteriorRingId"] = exterior_ring_id

        target.data.materials.append(mat)
        face.material_index = len(target.data.materials) - 1

        # Track interior face for ClosureSurface creation
        if is_interior and create_closure_surface and join_mode in {'PART', 'INSTALLATION'}:
            interior_bmfaces_for_closure.append(face)

        if override.get("uvs"):
            _assign_face_uvs(face, uv_layer, override.get("uvs"), _mesh_face_local_xyz(target, face_index))
            _store_uv_start_by_ring(target, mat, override.get("uvs"))

    # ── ClosureSurface-Erstellung ──
    # Für jede Interior-Fläche (Loch im Hauptobjekt) wird eine ClosureSurface
    # als Duplikat erstellt, die das Part/Installation-Volumen an der
    # Kontaktfläche schließt. Die Normale wird umgekehrt, damit sie vom
    # Part/Installation-Volumen nach außen zeigt.
    if interior_bmfaces_for_closure:
        for int_face in interior_bmfaces_for_closure:
            if not int_face.is_valid:
                continue

            # Fläche duplizieren (inkl. Vertices und Edges)
            dup_geom = list(int_face.verts) + list(int_face.edges) + [int_face]
            result = bmesh.ops.duplicate(bm, geom=dup_geom)
            new_faces = [g for g in result['geom'] if isinstance(g, bmesh.types.BMFace)]
            if not new_faces:
                continue
            new_face = new_faces[0]

            # Normale umkehren: ClosureSurface zeigt vom Part/Installation nach außen
            # bmesh.ops.reverse_faces(bm, faces=[new_face])

            # ClosureSurface-Material erstellen
            closure_mat = _create_join_material("ClosureSurface", feature_type=feature_type)
            closure_poly_id = _new_polygon_id()
            closure_ring_id = _allocate_ring_id(closure_poly_id, ring_indices)
            closure_mat["gml_polygon_id"] = closure_poly_id
            closure_mat["gml_ring_id"] = closure_ring_id
            closure_mat["con_surface_id"] = f"ID_{uuid4().hex}"
            closure_mat["gml_multisurface_id"] = f"ID_{uuid4().hex}"
            closure_mat["is_multisurface_member"] = True
            closure_mat["closure_parent_id"] = feature_group_id
            closure_mat["cgml3_subfeature_type"] = subfeature_type
            closure_mat["cgml3_subfeature_id"] = feature_group_id
            closure_mat["cgml3_subfeature_parent_id"] = str(target.get("gml_id", "") or "")
            if join_mode == 'INSTALLATION':
                closure_mat["in_shell"] = True
            if target_lod is not None:
                closure_mat["lod"] = target_lod

            target.data.materials.append(closure_mat)
            new_face.material_index = len(target.data.materials) - 1

    bm.to_mesh(target.data)
    bm.free()
    _cleanup_unused_materials(target)


def _apply_difference_modifier(context, source, target):
    bpy.ops.object.select_all(action='DESELECT')
    source.select_set(True)
    context.view_layer.objects.active = source

    modifier = source.modifiers.new(name="CGML3_JoinParts", type='BOOLEAN')
    modifier.operation = 'DIFFERENCE'
    _set_boolean_float_solver(modifier)
    modifier.object = target

    try:
        bpy.ops.object.modifier_apply(modifier=modifier.name)
        return True
    except Exception:
        try:
            source.modifiers.remove(modifier)
        except Exception:
            pass
        return False


def _join_into_target(context, source, target):
    bpy.ops.object.select_all(action='DESELECT')
    source.select_set(True)
    target.select_set(True)
    context.view_layer.objects.active = target
    bpy.ops.object.join()
    return target


class CGML3_OT_JoinObjectParts(Operator):
    """Join material-less selected object parts into touching mesh objects"""

    bl_idname = "cgml3.join_object_parts"
    bl_label = "Join Object Parts"
    bl_options = {'REGISTER', 'UNDO'}

    join_mode: EnumProperty(
        name="Join Mode",
        items=[
            ('PART', "Part", "Assign as Part (BuildingPart / BridgePart / TunnelPart)"),
            ('INSTALLATION', "Installation", "Assign as Installation (BuildingInstallation / BridgeInstallation / TunnelInstallation)"),
            ('FURNITURE', "Furniture", "Assign as Furniture (BuildingFurniture / BridgeFurniture / TunnelFurniture)"),
            ('CONSTRUCTION_ELEMENT', "BridgeConstructionElement", "Assign as BridgeConstructionElement (Bridge / BridgePart only)"),
            ('SURFACES', "Surfaces", "Assign SurfaceTyp WallSurface / RoofSurface"),
        ],
        default='PART',
    )

    create_closure_surface: BoolProperty(
        name="ClosureSurface erzeugen",
        description=(
            "Erzeugt eine ClosureSurface an der Kontaktfläche, "
            "die das Part/Installation-Volumen schließt"
        ),
        default=False,
    )

    def invoke(self, context, event):
        if self.join_mode in {'PART', 'INSTALLATION'}:
            return context.window_manager.invoke_props_dialog(self)
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "join_mode")
        if self.join_mode in {'PART', 'INSTALLATION'}:
            layout.prop(self, "create_closure_surface")

    def execute(self, context):
        if context.mode != 'OBJECT':
            self.report({'WARNING'}, "Please switch to Object Mode")
            return {'CANCELLED'}

        pending_names = [obj.name for obj in _selected_source_meshes(context)]
        processed = 0
        skipped = []
        boolean_failed = []
        blocked = []
        last_target = None

        while pending_names:
            source_name = pending_names.pop(0)
            source = bpy.data.objects.get(source_name)
            if source is None or source.type != 'MESH':
                continue
            if _object_has_real_materials(source):
                continue

            target = _find_touching_target(context, source)
            if target is None:
                skipped.append(source.name)
                continue

            if self.join_mode == 'CONSTRUCTION_ELEMENT' and not _supports_construction_element_join(target):
                blocked.append(f"{source.name} -> {target.name} ({_detect_feature_type(target)})")
                continue

            source_face_data = _capture_source_face_data(source)

            if not _apply_difference_modifier(context, source, target):
                boolean_failed.append(source.name)
                continue

            join_token = _new_join_token()
            _assign_placeholder_material(source, join_token)
            last_target = _join_into_target(context, source, target)
            _apply_join_materials_to_target(last_target, self.join_mode, join_token, source_face_data=source_face_data, create_closure_surface=self.create_closure_surface)
            processed += 1

        if last_target is not None and last_target.name in bpy.data.objects:
            bpy.ops.object.select_all(action='DESELECT')
            last_target.select_set(True)
            context.view_layer.objects.active = last_target

        if processed == 0:
            if blocked:
                self.report(
                    {'WARNING'},
                    "BridgeConstructionElement is only valid for Bridge/BridgePart targets: "
                    + ", ".join(blocked[:3]),
                )
            elif boolean_failed:
                self.report({'WARNING'}, f"Boolean cut produced no usable geometry for: {', '.join(boolean_failed[:3])}")
            elif skipped:
                self.report({'WARNING'}, f"No touching target found for: {', '.join(skipped[:3])}")
            else:
                self.report({'WARNING'}, "No selected mesh objects without materials found")
            return {'CANCELLED'}

        if skipped or boolean_failed or blocked:
            parts = [f"Joined {processed} object(s)"]
            if skipped:
                parts.append(f"skipped {len(skipped)} without target")
            if boolean_failed:
                parts.append(f"skipped {len(boolean_failed)} empty boolean result")
            if blocked:
                parts.append(f"blocked {len(blocked)} non-Bridge target(s)")
            self.report({'INFO'}, "; ".join(parts))
        else:
            self.report({'INFO'}, f"Joined {processed} object(s)")
        return {'FINISHED'}


class CGML3_MT_JoinObjectPartsContext(Menu):
    """Submenu for joining object parts"""

    bl_label = "Join Object Parts"
    bl_idname = "CGML3_MT_join_object_parts_context"

    def draw(self, context):
        layout = self.layout

        op = layout.operator(
            "cgml3.join_object_parts",
            text="Part",
            icon='OBJECT_DATAMODE',
        )
        op.join_mode = 'PART'

        op = layout.operator(
            "cgml3.join_object_parts",
            text="Installation",
            icon='MOD_ARRAY',
        )
        op.join_mode = 'INSTALLATION'

        op = layout.operator(
            "cgml3.join_object_parts",
            text="Furniture",
            icon='OUTLINER_OB_MESH',
        )
        op.join_mode = 'FURNITURE'

        op = layout.operator(
            "cgml3.join_object_parts",
            text="BridgeConstructionElement (Bridge only)",
            icon='CONSTRAINT_BONE',
        )
        op.join_mode = 'CONSTRUCTION_ELEMENT'

        op = layout.operator(
            "cgml3.join_object_parts",
            text="Surfaces",
            icon='MESH_CUBE',
        )
        op.join_mode = 'SURFACES'


def draw_join_object_parts_context_menu(self, context):
    """Append Join Object Parts to the object right-click menu."""
    if context.mode != 'OBJECT':
        return
    if not _selected_source_meshes(context):
        return

    layout = self.layout
    layout.separator()
    layout.menu(CGML3_MT_JoinObjectPartsContext.bl_idname, icon='AUTOMERGE_ON')
