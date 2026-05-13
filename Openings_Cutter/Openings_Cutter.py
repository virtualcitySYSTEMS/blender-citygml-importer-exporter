# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""Integrated CityGML openings cutter helpers and operators."""

import bpy
import bmesh
import gpu
import uuid
import copy
import json

from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import BoolProperty, EnumProperty, FloatVectorProperty, IntProperty, PointerProperty, StringProperty
from gpu_extras.batch import batch_for_shader
from mathutils import Vector
from mathutils.geometry import intersect_line_plane
from bpy_extras import view3d_utils

from ..shared.lod_helpers import opening_lod_for_version, promote_opening_host_lod


addon_keymaps = []
INTERIOR_OFFSET = 0.0
OPENING_CUTTER_SNAP_PIXEL_THRESHOLD = 14.0
OPENING_CUTTER_SNAP_OBJECT_TYPES = {"MESH", "CURVE", "SURFACE", "FONT"}
OPENING_CUTTER_MAX_SNAP_OBJECTS = 0  # 0 = no limit; snapping must inspect every visible snap object.
OPENING_CUTTER_VERTEX_SNAP_ELEMENTS = {"VERTEX"}
OPENING_CUTTER_EDGE_SNAP_ELEMENTS = {"EDGE", "EDGE_MIDPOINT", "EDGE_PERPENDICULAR"}


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def get_active_mesh_object(context):
    obj = context.active_object
    if obj is None or obj.type != "MESH":
        return None
    return obj


def point_to_plane_basis(point, origin, axis_u, axis_v):
    rel = point - origin
    return Vector((rel.dot(axis_u), rel.dot(axis_v)))


def barycentric_2d(p, a, b, c):
    v0 = b - a
    v1 = c - a
    v2 = p - a

    den = v0.x * v1.y - v1.x * v0.y
    if abs(den) < 1e-12:
        return None

    inv_den = 1.0 / den
    u = (v2.x * v1.y - v1.x * v2.y) * inv_den
    v = (v0.x * v2.y - v2.x * v0.y) * inv_den
    w = 1.0 - u - v
    return (w, u, v)


def point_in_barycentric(bary, eps=1e-6):
    if bary is None:
        return False
    w, u, v = bary
    return (w >= -eps and u >= -eps and v >= -eps)


def fit_uv_affine_from_source(mapping, world_points):
    """
    Plugin-specific logic from the CityGML Exporter:
    UVs are not interpolated using fan triangulation,
    but rather through an affine fit of the reference surface to the target points.
    This is more robust for rectangular/planar new surfaces.
    """
    origin = Vector(mapping["origin"])
    axis_u = Vector(mapping["axis_u"])
    axis_v = Vector(mapping["axis_v"])

    source_pts_2d = [Vector(p) for p in mapping["source_points_2d"]]
    source_uvs = [Vector(uv) for uv in mapping["source_uvs"]]

    if len(source_pts_2d) < 3 or len(source_pts_2d) != len(source_uvs) or not world_points:
        return [Vector((0.0, 0.0)) for _ in world_points]

    rows = []
    bu = []
    bv = []
    for p2, uv in zip(source_pts_2d, source_uvs):
        rows.append((float(p2.x), float(p2.y), 1.0))
        bu.append(float(uv.x))
        bv.append(float(uv.y))

    def solve_normal_equations(A_rows, b_vals):
        s_xx = s_xy = s_x1 = 0.0
        s_yy = s_y1 = s_11 = 0.0
        t_x = t_y = t_1 = 0.0

        for (x, y, one), b in zip(A_rows, b_vals):
            s_xx += x * x
            s_xy += x * y
            s_x1 += x * one
            s_yy += y * y
            s_y1 += y * one
            s_11 += one * one
            t_x += x * b
            t_y += y * b
            t_1 += one * b

        M = [
            [s_xx, s_xy, s_x1],
            [s_xy, s_yy, s_y1],
            [s_x1, s_y1, s_11],
        ]
        rhs = [t_x, t_y, t_1]

        for col in range(3):
            pivot = col
            for row in range(col + 1, 3):
                if abs(M[row][col]) > abs(M[pivot][col]):
                    pivot = row
            if abs(M[pivot][col]) < 1e-12:
                return None
            if pivot != col:
                M[col], M[pivot] = M[pivot], M[col]
                rhs[col], rhs[pivot] = rhs[pivot], rhs[col]

            div = M[col][col]
            for k in range(col, 3):
                M[col][k] /= div
            rhs[col] /= div

            for row in range(3):
                if row == col:
                    continue
                factor = M[row][col]
                if abs(factor) < 1e-12:
                    continue
                for k in range(col, 3):
                    M[row][k] -= factor * M[col][k]
                rhs[row] -= factor * rhs[col]

        return rhs

    coeff_u = solve_normal_equations(rows, bu)
    coeff_v = solve_normal_equations(rows, bv)
    if coeff_u is None or coeff_v is None:
        return [Vector((0.0, 0.0)) for _ in world_points]

    out = []
    for world_point in world_points:
        p2 = point_to_plane_basis(world_point, origin, axis_u, axis_v)
        x = float(p2.x)
        y = float(p2.y)
        u = coeff_u[0] * x + coeff_u[1] * y + coeff_u[2]
        v = coeff_v[0] * x + coeff_v[1] * y + coeff_v[2]
        out.append(Vector((u, v)))
    return out


def get_selected_face_info(context, obj):
    """
    Returns:
      plane_point, plane_normal, source_material, uv_mapping_data_json, source_uv_map_name, source_face_index
    Requires exactly one selected face in Edit Mode.
    """
    if obj is None or obj.type != "MESH":
        return None, None, None, "", "", -1

    if obj.mode != "EDIT":
        return None, None, None, "", "", -1

    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()

    selected_faces = [f for f in bm.faces if f.select]
    if len(selected_faces) != 1:
        return None, None, None, "", "", -1

    face = selected_faces[0]

    local_center = face.calc_center_median().copy()
    local_normal = face.normal.copy()

    world_center = obj.matrix_world @ local_center
    world_normal = (obj.matrix_world.to_3x3() @ local_normal).normalized()

    source_material = None
    mat_index = face.material_index
    if 0 <= mat_index < len(obj.data.materials):
        source_material = obj.data.materials[mat_index]

    uv_layer = bm.loops.layers.uv.active
    source_uv_map_name = uv_layer.name if uv_layer is not None else ""

    loops = list(face.loops)
    world_points = [obj.matrix_world @ loop.vert.co.copy() for loop in loops]

    # Stable basis on source face plane
    origin = world_points[0]
    axis_u = (world_points[1] - world_points[0]).normalized()
    axis_v = world_normal.cross(axis_u).normalized()

    source_points_2d = []
    for wp in world_points:
        p2 = point_to_plane_basis(wp, origin, axis_u, axis_v)
        source_points_2d.append([p2.x, p2.y])

    if uv_layer is not None:
        source_uvs = []
        for loop in loops:
            uv = loop[uv_layer].uv.copy()
            source_uvs.append([uv.x, uv.y])
    else:
        # fallback simple normalized local mapping from source face bounds
        xs = [p[0] for p in source_points_2d]
        ys = [p[1] for p in source_points_2d]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        dx = max(max_x - min_x, 1e-9)
        dy = max(max_y - min_y, 1e-9)

        source_uvs = []
        for p in source_points_2d:
            source_uvs.append([(p[0] - min_x) / dx, (p[1] - min_y) / dy])

    uv_mapping = {
        "origin": [origin.x, origin.y, origin.z],
        "axis_u": [axis_u.x, axis_u.y, axis_u.z],
        "axis_v": [axis_v.x, axis_v.y, axis_v.z],
        "source_points_2d": source_points_2d,
        "source_uvs": source_uvs,
    }

    return world_center, world_normal, source_material, json.dumps(uv_mapping), source_uv_map_name, face.index


def mouse_to_plane(context, mouse_xy, plane_point, plane_normal):
    region = context.region
    rv3d = context.space_data.region_3d

    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, mouse_xy)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, mouse_xy)
    far_point = origin + direction * 100000.0

    return intersect_line_plane(origin, far_point, plane_point, plane_normal, False)


def get_opening_cutter_snap_elements(context):
    tool_settings = getattr(context.scene, "tool_settings", None)
    if tool_settings is None or not getattr(tool_settings, "use_snap", False):
        return set()

    snap_elements = getattr(tool_settings, "snap_elements", None)
    if snap_elements:
        return {str(element).upper() for element in snap_elements}

    snap_element = getattr(tool_settings, "snap_element", None)
    if snap_element:
        return {str(snap_element).upper()}

    return set()


def opening_cutter_snap_enabled(context):
    snap_elements = get_opening_cutter_snap_elements(context)
    return bool(
        snap_elements.intersection(OPENING_CUTTER_VERTEX_SNAP_ELEMENTS)
        or snap_elements.intersection(OPENING_CUTTER_EDGE_SNAP_ELEMENTS)
    )


def closest_point_on_segment_2d(point, start, end):
    segment = end - start
    length_squared = segment.length_squared
    if length_squared <= 1e-12:
        return start.copy(), 0.0

    factor = max(0.0, min(1.0, (point - start).dot(segment) / length_squared))
    return start + segment * factor, factor


def screen_rect_distance_squared(point, min_x, max_x, min_y, max_y):
    if min_x <= point.x <= max_x:
        dx = 0.0
    else:
        dx = min(abs(point.x - min_x), abs(point.x - max_x))

    if min_y <= point.y <= max_y:
        dy = 0.0
    else:
        dy = min(abs(point.y - min_y), abs(point.y - max_y))

    return dx * dx + dy * dy


def _opening_cutter_bbox_from_vertices(vertices_world):
    if not vertices_world:
        return []

    min_x = min(vertex.x for vertex in vertices_world)
    max_x = max(vertex.x for vertex in vertices_world)
    min_y = min(vertex.y for vertex in vertices_world)
    max_y = max(vertex.y for vertex in vertices_world)
    min_z = min(vertex.z for vertex in vertices_world)
    max_z = max(vertex.z for vertex in vertices_world)

    return [
        Vector((x, y, z))
        for x in (min_x, max_x)
        for y in (min_y, max_y)
        for z in (min_z, max_z)
    ]


def _opening_cutter_project_snap_source(context, source):
    region = context.region
    rv3d = context.space_data.region_3d

    vertices_screen = [
        view3d_utils.location_3d_to_region_2d(region, rv3d, world_point)
        for world_point in source["vertices_world"]
    ]
    source["vertices_screen"] = vertices_screen

    bbox_screen = [
        view3d_utils.location_3d_to_region_2d(region, rv3d, world_point)
        for world_point in source["bbox_world"]
    ]
    bbox_screen = [point for point in bbox_screen if point is not None]
    if bbox_screen:
        source["bbox_screen_rect"] = (
            min(point.x for point in bbox_screen),
            max(point.x for point in bbox_screen),
            min(point.y for point in bbox_screen),
            max(point.y for point in bbox_screen),
        )

    return source


def _opening_cutter_snap_source_from_mesh(obj, mesh, matrix_world):
    if mesh is None or not mesh.vertices:
        return None

    vertices_world = [matrix_world @ vertex.co for vertex in mesh.vertices]
    edge_indices = [
        (edge.vertices[0], edge.vertices[1])
        for edge in mesh.edges
        if len(edge.vertices) == 2
    ]

    bound_box = getattr(obj, "bound_box", None)
    if bound_box:
        bbox_world = [matrix_world @ Vector(corner) for corner in bound_box]
    else:
        bbox_world = _opening_cutter_bbox_from_vertices(vertices_world)

    return {
        "vertices_world": vertices_world,
        "edge_indices": edge_indices,
        "bbox_world": bbox_world,
    }


def _opening_cutter_snap_source_from_edit_mesh(obj):
    if obj.type != "MESH" or obj.mode != "EDIT":
        return None

    try:
        bm = bmesh.from_edit_mesh(obj.data)
        matrix_world = obj.matrix_world.copy()
        vertex_index_by_bm_vert = {}
        vertices_world = []

        for vert in bm.verts:
            if getattr(vert, "hide", False):
                continue
            vertex_index_by_bm_vert[id(vert)] = len(vertices_world)
            vertices_world.append(matrix_world @ vert.co)

        edge_indices = []
        for edge in bm.edges:
            if getattr(edge, "hide", False):
                continue
            start_idx = vertex_index_by_bm_vert.get(id(edge.verts[0]))
            end_idx = vertex_index_by_bm_vert.get(id(edge.verts[1]))
            if start_idx is None or end_idx is None:
                continue
            edge_indices.append((start_idx, end_idx))

        if not vertices_world:
            return None

        bbox_world = _opening_cutter_bbox_from_vertices(vertices_world)
        return {
            "vertices_world": vertices_world,
            "edge_indices": edge_indices,
            "bbox_world": bbox_world,
        }
    except Exception:
        return None


def build_opening_cutter_snap_cache(context):
    if not opening_cutter_snap_enabled(context):
        return None

    depsgraph = context.evaluated_depsgraph_get()
    snap_sources = []
    processed_objects = 0

    for obj in context.visible_objects:
        if obj.type not in OPENING_CUTTER_SNAP_OBJECT_TYPES:
            continue
        if OPENING_CUTTER_MAX_SNAP_OBJECTS and processed_objects >= OPENING_CUTTER_MAX_SNAP_OBJECTS:
            break

        edit_source = _opening_cutter_snap_source_from_edit_mesh(obj)
        if edit_source is not None:
            snap_sources.append(_opening_cutter_project_snap_source(context, edit_source))
            processed_objects += 1
            continue

        eval_obj = obj.evaluated_get(depsgraph)
        mesh = None
        try:
            mesh = eval_obj.to_mesh()
            snap_source = _opening_cutter_snap_source_from_mesh(
                eval_obj,
                mesh,
                eval_obj.matrix_world.copy(),
            )
            if snap_source is None:
                continue
            snap_sources.append(_opening_cutter_project_snap_source(context, snap_source))
            processed_objects += 1
        except Exception:
            continue
        finally:
            if mesh is not None:
                eval_obj.to_mesh_clear()

    return snap_sources


def resolve_opening_cutter_snap_mouse(context, mouse_xy, snap_cache=None, pixel_threshold=OPENING_CUTTER_SNAP_PIXEL_THRESHOLD):
    if not opening_cutter_snap_enabled(context):
        return mouse_xy, snap_cache, False

    if snap_cache is None:
        snap_cache = build_opening_cutter_snap_cache(context)
    if not snap_cache:
        return mouse_xy, snap_cache, False

    region = context.region
    rv3d = context.space_data.region_3d
    mouse_vec = Vector((float(mouse_xy[0]), float(mouse_xy[1])))
    best_distance_squared = pixel_threshold * pixel_threshold
    best_screen = None

    snap_elements = get_opening_cutter_snap_elements(context)
    use_vertex_snap = bool(snap_elements.intersection(OPENING_CUTTER_VERTEX_SNAP_ELEMENTS))
    use_edge_snap = bool(snap_elements.intersection(OPENING_CUTTER_EDGE_SNAP_ELEMENTS))

    for source in snap_cache:
        bbox_screen_rect = source.get("bbox_screen_rect")
        if bbox_screen_rect:
            raw_min_x, raw_max_x, raw_min_y, raw_max_y = bbox_screen_rect
            min_x = raw_min_x - pixel_threshold
            max_x = raw_max_x + pixel_threshold
            min_y = raw_min_y - pixel_threshold
            max_y = raw_max_y + pixel_threshold
            if screen_rect_distance_squared(mouse_vec, min_x, max_x, min_y, max_y) > best_distance_squared:
                continue

        projected_vertices = source.get("vertices_screen")
        if projected_vertices is None:
            projected_vertices = [
                view3d_utils.location_3d_to_region_2d(region, rv3d, world_point)
                for world_point in source["vertices_world"]
            ]

        if use_vertex_snap:
            for projected in projected_vertices:
                if projected is None:
                    continue
                distance_squared = (projected - mouse_vec).length_squared
                if distance_squared < best_distance_squared:
                    best_distance_squared = distance_squared
                    best_screen = (projected.x, projected.y)

        if use_edge_snap:
            vertex_count = len(projected_vertices)
            for start_index, end_index in source["edge_indices"]:
                if start_index >= vertex_count or end_index >= vertex_count:
                    continue
                start_screen = projected_vertices[start_index]
                end_screen = projected_vertices[end_index]
                if start_screen is None or end_screen is None:
                    continue

                closest_screen, _ = closest_point_on_segment_2d(mouse_vec, start_screen, end_screen)
                distance_squared = (closest_screen - mouse_vec).length_squared
                if distance_squared < best_distance_squared:
                    best_distance_squared = distance_squared
                    best_screen = (closest_screen.x, closest_screen.y)

    if best_screen is None:
        return mouse_xy, snap_cache, False

    return best_screen, snap_cache, True


def project_opening_cutter_mouse_to_plane(context, mouse_xy, plane_point, plane_normal, snap_cache=None):
    resolved_mouse, snap_cache, snapped = resolve_opening_cutter_snap_mouse(context, mouse_xy, snap_cache=snap_cache)
    world_point = mouse_to_plane(context, resolved_mouse, plane_point, plane_normal)

    if world_point is None and resolved_mouse != mouse_xy:
        world_point = mouse_to_plane(context, mouse_xy, plane_point, plane_normal)
        if world_point is not None:
            return world_point, mouse_xy, snap_cache, False

    return world_point, resolved_mouse, snap_cache, snapped


def draw_opening_cutter_snap_marker(screen_point):
    if screen_point is None:
        return

    x, y = screen_point
    verts = [
        (x - 8.0, y),
        (x + 8.0, y),
        (x, y - 8.0),
        (x, y + 8.0),
    ]
    indices = [(0, 1), (2, 3)]
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "LINES", {"pos": verts}, indices=indices)

    gpu.state.blend_set("ALPHA")
    gpu.state.line_width_set(2.0)
    shader.bind()
    shader.uniform_float("color", (1.0, 0.45, 0.1, 0.95))
    batch.draw(shader)
    gpu.state.line_width_set(1.0)
    gpu.state.blend_set("NONE")


def ensure_material_slot(obj, mat):
    mats = obj.data.materials
    for i, existing in enumerate(mats):
        if existing == mat:
            return i
    mats.append(mat)
    return len(mats) - 1


def unique_suffix():
    return uuid.uuid4().hex[:12]


def compact_material_id(raw_id, fallback=""):
    text = str(raw_id or "").strip()
    if text.startswith("ID_"):
        text = text[3:]
    elif text.startswith("UUID_"):
        text = text[5:]
    text = "".join(ch for ch in text if ch.isalnum())
    if text:
        return text[-12:]
    return str(fallback or unique_suffix())


def opening_material_name(opening_surface_type, surface_id):
    label = str(opening_surface_type or "Opening").strip() or "Opening"
    return f"{label}_Opening_Exterior_{compact_material_id(surface_id)}"


def interior_material_name(surface_type, ring_or_poly_id):
    surface_type = str(surface_type or "").strip()
    if surface_type in {"CeilingSurface", "OuterCeilingSurface", "RoofSurface"}:
        prefix = "Ceiling"
    elif surface_type in {"FloorSurface", "OuterFloorSurface", "GroundSurface"}:
        prefix = "Floor"
    elif surface_type == "ClosureSurface":
        prefix = "Closure"
    else:
        prefix = "InteriorWall"
    return f"{prefix}_{compact_material_id(ring_or_poly_id)}"


def collection_contains_object(collection, obj):
    if collection is None or obj is None:
        return False

    try:
        for member in collection.objects:
            if member == obj:
                return True
    except Exception:
        pass

    try:
        for child in collection.children:
            if collection_contains_object(child, obj):
                return True
    except Exception:
        pass

    return False


def detect_citygml_version_for_object(obj, scene=None):
    scene = scene or bpy.context.scene
    root_coll = getattr(scene, "collection", None)
    if root_coll is None:
        return None

    for col in root_coll.children:
        if not collection_contains_object(col, obj):
            continue
        name_up = str(col.name).upper()
        if "CITYGML3" in name_up:
            return "3.0"
        if "CITYGML2" in name_up:
            return "2.0"

    for col in root_coll.children:
        name_up = str(col.name).upper()
        if "CITYGML3" in name_up:
            return "3.0"
        if "CITYGML2" in name_up:
            return "2.0"

    return None


def opening_surface_type_for_version(opening_type, citygml_version):
    if citygml_version == "2.0":
        return "Window" if opening_type == "Window" else "Door"
    return "WindowSurface" if opening_type == "Window" else "DoorSurface"


def build_unique_ids(opening_type):
    short = uuid.uuid4().hex[:12]

    if opening_type == "Window":
        opening_id = f"UUID_Window_{short}"
    else:
        opening_id = f"UUID_Door_{short}"

    gml_multisurface_id = f"ID_{uuid.uuid4().hex}"
    con_surface_id = f"ID_{uuid.uuid4().hex}"
    gml_polygon_id = f"ID_{uuid.uuid4().hex}"
    gml_ring_id = f"{gml_polygon_id}_0_"

    return {
        "opening_id": opening_id,
        "con_surface_id": con_surface_id,
        "gml_multisurface_id": gml_multisurface_id,
        "gml_polygon_id": gml_polygon_id,
        "gml_ring_id": gml_ring_id,
    }


def derive_interior_surface_type(source_material):
    source_type = str(
        source_material.get("SurfaceTyp")
        or source_material.get("surface_type")
        or source_material.get("Typ")
        or "WallSurface"
    )

    if source_type in {"InteriorWallSurface", "CeilingSurface", "FloorSurface", "ClosureSurface"}:
        return source_type
    if source_type in {"RoofSurface", "OuterCeilingSurface"}:
        return "CeilingSurface"
    if source_type in {"GroundSurface", "OuterFloorSurface"}:
        return "FloorSurface"
    return "InteriorWallSurface"


def allocate_next_interior_polygon_id(obj, exterior_poly_id):
    """Return the next interior polygon ID ``{exterior_poly_id}_{N}``.

    Scans existing materials on *obj* for interior surfaces whose
    ``gml_polygon_id`` matches the pattern ``{exterior_poly_id}_{N}`` and
    returns the next free number so that numbering continues correctly.
    """
    if not exterior_poly_id:
        return f"ID_{uuid.uuid4().hex}_1"

    max_num = 0
    prefix = f"{exterior_poly_id}_"

    materials = []
    try:
        materials = list(getattr(obj.data, "materials", []) or [])
    except Exception:
        materials = []

    for mat in materials:
        if not mat:
            continue
        if not mat.get("Interior"):
            continue
        pid = str(mat.get("gml_polygon_id", "") or "")
        if not pid.startswith(prefix):
            continue
        suffix = pid[len(prefix):]
        try:
            num = int(suffix)
            if num > max_num:
                max_num = num
        except (ValueError, TypeError):
            continue

    return f"{exterior_poly_id}_{max_num + 1}"


def material_custom_props_to_dict(mat):
    if mat is None:
        return {}

    data = {}
    for key in mat.keys():
        if key == "_RNA_UI":
            continue
        try:
            data[key] = copy.deepcopy(mat[key])
        except Exception:
            pass
    return data


def apply_dict_to_material(mat, data):
    for key, value in data.items():
        if key == "_RNA_UI":
            continue
        try:
            mat[key] = value
        except Exception:
            pass


def clear_material_custom_properties(mat):
    keys = [k for k in mat.keys() if k != "_RNA_UI"]
    for key in keys:
        try:
            del mat[key]
        except Exception:
            pass


def make_exterior_opening_material(opening_type, source_material=None, obj=None, lod_value=None):
    """
    Creates the visible opening surface in a format compatible with export, but—
    similar to interior surfaces—inherits the material, node tree, image textures, and
    appearance-related properties from the selected source surface.
    """
    ids = build_unique_ids(opening_type)
    citygml_version = detect_citygml_version_for_object(obj)
    if lod_value is None:
        lod_value = opening_lod_for_version(obj, citygml_version)
    opening_surface_type = opening_surface_type_for_version(opening_type, citygml_version)
    mat_name = opening_material_name(opening_surface_type, ids["con_surface_id"])

    if source_material is not None:
        mat = duplicate_material_with_textures(source_material, mat_name)
        inherited = material_custom_props_to_dict(source_material)
        apply_dict_to_material(mat, inherited)
    else:
        mat = bpy.data.materials.new(name=mat_name)
        clear_material_custom_properties(mat)

    source_surface_type = "WallSurface"
    parent_surface_id = ""
    if source_material is not None:
        try:
            source_surface_type = str(
                source_material.get("SurfaceTyp")
                or source_material.get("surface_type")
                or source_material.get("Typ")
                or "WallSurface"
            )
        except Exception:
            source_surface_type = "WallSurface"
        try:
            parent_surface_id = str(source_material.get("con_surface_id", "") or "")
        except Exception:
            parent_surface_id = ""

    # Opening gets its own geometry IDs but retains texture/node setup.
    mat["SurfaceTyp"] = opening_surface_type
    mat["surface_type"] = opening_surface_type
    mat["Typ"] = opening_surface_type
    mat["OpeningType"] = opening_type
    mat["opening_type"] = opening_type
    mat["CityGMLTarget"] = opening_type
    mat["BoundarySurfaceType"] = source_surface_type
    mat["opening_surface_type"] = source_surface_type
    mat["opening_surface_id"] = parent_surface_id
    mat["filling_parent_surface_id"] = parent_surface_id
    mat["Interior"] = False

    mat["con_opening_id"] = ids["opening_id"]
    mat["opening_id"] = ids["opening_id"]
    mat["opening_gml_id"] = ids["opening_id"]
    mat["con_surface_id"] = ids["con_surface_id"]
    mat["gml_multisurface_id"] = ids["gml_multisurface_id"]
    mat["gml_polygon_id"] = ids["gml_polygon_id"]
    mat["gml_ring_id"] = ids["gml_ring_id"]

    mat["is_opening"] = True
    mat["is_multisurface_member"] = True
    mat["lod"] = int(lod_value)
    mat["hasWindows"] = 1 if opening_type == "Window" else 0

    # Retain existing appearance/texture assignments from the source surface.
    # Only initialize if nothing exists.
    mat["app_target_or_uri"] = ids["gml_polygon_id"]

    # Prevent incorrect inheritance of other semantic remnants.
    if "ExteriorPolyId" in mat:
        del mat["ExteriorPolyId"]
    if "ExteriorRingId" in mat:
        del mat["ExteriorRingId"]

    try:
        mat.name = mat_name
    except Exception:
        pass

    return mat


def duplicate_material_with_textures(source_material, new_name):
    mat = source_material.copy()
    mat.name = new_name
    return mat


def make_interior_material_from_source(source_material, obj=None, exterior_poly_id="", exterior_ring_id="", lod_value=None):
    """
    Inherits material incl. node tree and image textures 1:1 from source material.

    Important ID logic analogous to the "Join Object Parts" plugin:
    - The interior surface is exported as the inner ring of the selected exterior
      surface and therefore shares its gml_polygon_id.
    - gml_ring_id receives a new ring ID within this polygon.
    - ExteriorPolyId / ExteriorRingId reference the selected,
      coplanar source surface in edit mode.
    - Type / surface_type remain on the boundary surface type of the
      source surface (e.g., WallSurface, RoofSurface).
    """
    mat_name = f"InteriorWall_{unique_suffix()}"
    mat = duplicate_material_with_textures(source_material, mat_name)
    if lod_value is None:
        lod_value = opening_lod_for_version(obj, detect_citygml_version_for_object(obj))

    inherited = material_custom_props_to_dict(source_material)
    apply_dict_to_material(mat, inherited)

    inherited_surface_type = str(
        mat.get("SurfaceTyp")
        or mat.get("surface_type")
        or mat.get("Typ")
        or "WallSurface"
    )

    # Crucial: For interior materials, the Exterior*-IDs
    # must come from the selected coplanar reference surface.
    source_polygon_id = str(source_material.get("gml_polygon_id", "") or "")
    source_ring_id = str(source_material.get("gml_ring_id", "") or "")

    fallback_polygon_id = str(exterior_poly_id or "")
    fallback_ring_id = str(exterior_ring_id or "")

    exterior_polygon_id_final = source_polygon_id or fallback_polygon_id
    exterior_ring_id_final = source_ring_id or fallback_ring_id

    # Interior gets its own polygon ID: {exterior_poly_id}_{N} (numbered)
    interior_polygon_id = allocate_next_interior_polygon_id(obj, exterior_polygon_id_final)
    # Ring-ID = Interior-Polygon-ID + trailing underscore
    interior_ring_id = f"{interior_polygon_id}_"

    mat["Interior"] = True
    mat["opening_cutter_interior"] = True
    mat["created_by_openings_cutter"] = True
    mat["SurfaceTyp"] = inherited_surface_type
    mat["Typ"] = inherited_surface_type
    mat["surface_type"] = inherited_surface_type

    if interior_polygon_id:
        mat["gml_polygon_id"] = interior_polygon_id
    elif "gml_polygon_id" in mat:
        del mat["gml_polygon_id"]

    if interior_ring_id:
        mat["gml_ring_id"] = interior_ring_id

    # Reference to the selected exterior/boundary surface
    if exterior_polygon_id_final:
        mat["ExteriorPolyId"] = exterior_polygon_id_final
    elif "ExteriorPolyId" in mat:
        del mat["ExteriorPolyId"]

    if exterior_ring_id_final:
        mat["ExteriorRingId"] = exterior_ring_id_final
    elif "ExteriorRingId" in mat:
        del mat["ExteriorRingId"]

    if "OpeningType" in mat:
        del mat["OpeningType"]
    if "CityGMLTarget" in mat:
        del mat["CityGMLTarget"]
    if "con_opening_id" in mat:
        del mat["con_opening_id"]
    if "is_opening" in mat:
        del mat["is_opening"]

    mat["lod"] = int(lod_value)

    # Same export convention as in "Join Object Parts":
    # Interior rings must be inverted again in the Appearance-Writer
    # so that their TexCoordList winding matches the exported hole geometry.
    if "cgml3_skip_interior_uv_reverse" in mat:
        del mat["cgml3_skip_interior_uv_reverse"]

    try:
        mat.name = interior_material_name(
            inherited_surface_type,
            interior_ring_id or exterior_polygon_id_final or unique_suffix(),
        )
    except Exception:
        pass

    return mat


def polygon_normal(points):
    """Return a robust normal for a planar polygon or triangle-like point list."""
    pts = list(points or [])
    if len(pts) < 3:
        return Vector((0, 0, 1))

    n = Vector((0.0, 0.0, 0.0))
    for index, p0 in enumerate(pts):
        p1 = pts[(index + 1) % len(pts)]
        n.x += (p0.y - p1.y) * (p0.z + p1.z)
        n.y += (p0.z - p1.z) * (p0.x + p1.x)
        n.z += (p0.x - p1.x) * (p0.y + p1.y)

    if n.length == 0:
        for i in range(len(pts) - 2):
            n = (pts[i + 1] - pts[i]).cross(pts[i + 2] - pts[i])
            if n.length != 0:
                break

    if n.length == 0:
        return Vector((0, 0, 1))
    return n.normalized()


def quad_normal(p0, p1, p2):
    return polygon_normal([p0, p1, p2])


def same_xyz(a, b, tol=1e-6):
    return (
        abs(a[0] - b[0]) <= tol
        and abs(a[1] - b[1]) <= tol
        and abs(a[2] - b[2]) <= tol
    )


def cleaned_polygon_points(points, tol=1e-6):
    cleaned = []
    for point in points or []:
        p = Vector(point)
        if cleaned and same_xyz(cleaned[-1], p, tol=tol):
            continue
        cleaned.append(p)

    if len(cleaned) >= 2 and same_xyz(cleaned[0], cleaned[-1], tol=tol):
        cleaned.pop()

    return cleaned


def create_face_with_coords(bm, coords):
    verts = [bm.verts.new(co) for co in coords]
    bm.verts.ensure_lookup_table()

    try:
        face = bm.faces.new(verts)
        return face, coords
    except ValueError:
        coords_rev = list(reversed(coords))
        verts = [bm.verts.new(co) for co in coords_rev]
        bm.verts.ensure_lookup_table()
        face = bm.faces.new(verts)
        return face, coords_rev


def order_uvs_for_face(face, ring_xyz, ring_uvs, tol=1e-6):
    coords = list(ring_xyz or [])
    uvs = list(ring_uvs or [])
    if len(coords) >= 2 and same_xyz(coords[0], coords[-1], tol=tol):
        coords = coords[:-1]
    if len(uvs) >= 2 and uvs[0] == uvs[-1]:
        uvs = uvs[:-1]
    if not coords or not uvs or len(coords) != len(uvs):
        return list(ring_uvs or [])

    used = set()
    loop_order = []
    for loop in face.loops:
        vx = float(loop.vert.co.x)
        vy = float(loop.vert.co.y)
        vz = float(loop.vert.co.z)

        best_index = None
        best_distance = None
        for index, (x, y, z) in enumerate(coords):
            if index in used:
                continue
            distance = (vx - x) * (vx - x) + (vy - y) * (vy - y) + (vz - z) * (vz - z)
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_index = index

        if best_index is None:
            best_index = 0
        used.add(best_index)
        loop_order.append(best_index)

    ordered = [uvs[index] for index in loop_order]
    if ordered and ordered[0] != ordered[-1]:
        ordered.append(ordered[0])
    return ordered


def store_uv_start_by_ring(obj, mat, fitted_uvs):
    if not obj or not mat or not fitted_uvs:
        return

    try:
        ring_id = str(mat.get("gml_ring_id", "") or "")
    except Exception:
        ring_id = ""

    if not ring_id:
        return

    start_uv = (float(fitted_uvs[0].x), float(fitted_uvs[0].y))

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


def uv_ring_sign(uvs):
    values = list(uvs or [])
    if len(values) >= 2 and values[0] == values[-1]:
        values = values[:-1]
    if len(values) < 3:
        return 0

    area = 0.0
    for index, uv in enumerate(values):
        nxt = values[(index + 1) % len(values)]
        area += float(uv[0]) * float(nxt[1]) - float(nxt[0]) * float(uv[1])

    if area > 1e-12:
        return 1
    if area < -1e-12:
        return -1
    return 0


def reverse_uv_ring(uvs):
    values = list(uvs or [])
    if not values:
        return values

    closed = len(values) >= 2 and values[0] == values[-1]
    if closed:
        values = values[:-1]

    values = list(reversed(values))
    if closed and values:
        values.append(values[0])
    return values


def write_uvs_to_face(face, uv_layer, uvs):
    if uv_layer is None:
        return

    values = list(uvs or [])
    if len(values) >= 2 and values[0] == values[-1]:
        values = values[:-1]

    for loop, uv in zip(face.loops, values):
        loop[uv_layer].uv = (float(uv[0]), float(uv[1]))


def ensure_uv_layer(bm, preferred_name=""):
    """
    Use the existing UV map from the source face if possible.
    Only create a new UV layer when there is really none.
    """
    if preferred_name:
        uv_layer = bm.loops.layers.uv.get(preferred_name)
        if uv_layer is not None:
            return uv_layer

    uv_layer = bm.loops.layers.uv.active
    if uv_layer is not None:
        return uv_layer

    all_uv_layers = list(bm.loops.layers.uv.keys())
    if all_uv_layers:
        return bm.loops.layers.uv.get(all_uv_layers[0])

    new_name = preferred_name if preferred_name else "UVMap"
    return bm.loops.layers.uv.new(new_name)


def assign_uvs_from_source_mapping(face, uv_layer, local_coords_used, world_coords_used, uv_mapping_json):
    if not uv_mapping_json:
        for loop in face.loops:
            loop[uv_layer].uv = (0.0, 0.0)
        return []

    mapping = json.loads(uv_mapping_json)
    fitted_uvs = fit_uv_affine_from_source(mapping, world_coords_used)
    ordered_uvs = order_uvs_for_face(face, local_coords_used, fitted_uvs)

    for loop, uv in zip(face.loops, ordered_uvs):
        loop[uv_layer].uv = (uv.x, uv.y)

    return ordered_uvs


def restore_selected_face(obj, source_face_index):
    if obj is None or obj.type != "MESH" or obj.mode != "EDIT":
        return

    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()

    if not (0 <= source_face_index < len(bm.faces)):
        return

    for face in bm.faces:
        face.select = False

    source_face = bm.faces[source_face_index]
    source_face.select = True
    bm.faces.active = source_face

    try:
        bm.select_flush_mode()
    except Exception:
        pass

    bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=False)


def create_joined_surfaces(
    obj,
    world_corners,
    opening_type,
    plane_normal,
    source_material,
    uv_mapping_json,
    source_uv_map_name,
    source_face_index=-1,
    generate_interior=True,
):
    """
    Writes faces directly into the active mesh object:
    - exterior opening face -> CityGML2 opening semantics
    - interior face -> inherited from source boundary surface material, including texture image
      (only if generate_interior=True)
    - exterior/interior UVs are derived from the selected source face UV mapping
    - UVs are written into the existing source UV map
    """
    if source_material is None:
        raise RuntimeError("Please select a base surface with material.")

    world_corners = cleaned_polygon_points(world_corners)
    if len(world_corners) < 3:
        raise RuntimeError("At least 3 valid polygon points are required.")

    source_face_poly_id = ""
    source_face_app_id = ""
    try:
        if source_face_index is not None and source_face_index >= 0 and source_face_index < len(obj.data.polygons):
            source_poly = obj.data.polygons[source_face_index]
            source_face_poly_id = str(source_poly.get("gml_id", "") or "").strip()
            source_face_app_id = str(source_poly.get("app_id", "") or "").strip()
    except Exception:
        source_face_poly_id = ""
        source_face_app_id = ""

    # Ensure source material has required IDs for export grouping.
    # Auto-assign creates sparse materials without proper IDs.
    # Without these, the writer can't group the interior ring with the wall polygon
    # and can't link the opening to its parent surface.
    _src_poly_id = source_face_poly_id or str(source_material.get("gml_polygon_id", "") or "").strip()
    if not _src_poly_id:
        _src_poly_id = f"ID_{uuid.uuid4().hex}"
    source_material["gml_polygon_id"] = _src_poly_id
    # Ensure gml_ring_id follows the pattern "{polygon_id}_0_" for the exterior ring.
    # Auto-assign may set an independent UUID which breaks writer grouping logic.
    _src_ring_id = str(source_material.get("gml_ring_id", "") or "").strip()
    if not _src_ring_id or not _src_ring_id.startswith(_src_poly_id):
        source_material["gml_ring_id"] = f"{_src_poly_id}_0_"
    if not str(source_material.get("con_surface_id", "") or "").strip():
        source_material["con_surface_id"] = f"ID_{uuid.uuid4().hex}"
    if not str(source_material.get("gml_multisurface_id", "") or "").strip():
        source_material["gml_multisurface_id"] = f"ID_{uuid.uuid4().hex}"

    citygml_version = detect_citygml_version_for_object(obj)
    opening_lod = opening_lod_for_version(obj, citygml_version)

    original_mode = obj.mode
    mesh = obj.data
    bm = None

    try:
        if original_mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        bm = bmesh.new()
        bm.from_mesh(mesh)

        uv_layer = ensure_uv_layer(bm, source_uv_map_name)

        world_to_local = obj.matrix_world.inverted()
        local_plane_normal = (
            obj.matrix_world.to_3x3().transposed() @ plane_normal
        ).normalized()

        local_ext = [world_to_local @ p for p in world_corners]
        ext_normal = polygon_normal(local_ext)
        if ext_normal.dot(local_plane_normal) < 0:
            local_ext.reverse()
            world_corners = list(reversed(world_corners))

        inward_world = -plane_normal.normalized() * INTERIOR_OFFSET
        interior_world_corners = [p + inward_world for p in world_corners]
        local_interior = [world_to_local @ p for p in interior_world_corners]

        int_normal = polygon_normal(local_interior)
        if int_normal.dot(-local_plane_normal) < 0:
            local_interior.reverse()
            interior_world_corners = list(reversed(interior_world_corners))

        ext_mat = make_exterior_opening_material(
            opening_type,
            source_material=source_material,
            obj=obj,
            lod_value=opening_lod,
        )
        if source_face_app_id:
            ext_mat["app_id"] = source_face_app_id

        ext_mat_index = ensure_material_slot(obj, ext_mat)

        ext_face, ext_local_used = create_face_with_coords(bm, local_ext)
        ext_face.material_index = ext_mat_index

        ext_world_used = list(world_corners)
        if ext_local_used and local_ext and not same_xyz(ext_local_used[0], local_ext[0]):
            ext_world_used = list(reversed(world_corners))

        # UVs for exterior face
        ext_uvs = assign_uvs_from_source_mapping(
            ext_face,
            uv_layer,
            ext_local_used,
            ext_world_used,
            uv_mapping_json,
        )
        store_uv_start_by_ring(obj, ext_mat, ext_uvs)

        # Interior face (optional)
        if generate_interior:
            int_mat = make_interior_material_from_source(
                source_material,
                obj=obj,
                lod_value=opening_lod,
            )
            if source_face_app_id:
                int_mat["app_id"] = source_face_app_id
            int_mat_index = ensure_material_slot(obj, int_mat)

            int_face, int_local_used = create_face_with_coords(bm, local_interior)
            int_face.material_index = int_mat_index

            int_world_used = list(interior_world_corners)
            if int_local_used and local_interior and not same_xyz(int_local_used[0], local_interior[0]):
                int_world_used = list(reversed(interior_world_corners))

            int_uvs = assign_uvs_from_source_mapping(
                int_face,
                uv_layer,
                int_local_used,
                int_world_used,
                uv_mapping_json,
            )

            if uv_ring_sign(ext_uvs) != 0 and uv_ring_sign(int_uvs) == uv_ring_sign(ext_uvs):
                int_uvs = reverse_uv_ring(int_uvs)
                write_uvs_to_face(int_face, uv_layer, int_uvs)

            store_uv_start_by_ring(obj, int_mat, int_uvs)

        bm.normal_update()
        bm.to_mesh(mesh)
        mesh.update()
        promote_opening_host_lod(obj, opening_lod=opening_lod)
    finally:
        if bm is not None:
            bm.free()

        uv_layer_data = mesh.uv_layers.get(source_uv_map_name) if source_uv_map_name else None
        if uv_layer_data is not None:
            try:
                mesh.uv_layers.active = uv_layer_data
            except Exception:
                pass
            try:
                uv_layer_data.active_render = True
            except Exception:
                pass

        if original_mode == "EDIT":
            bpy.ops.object.mode_set(mode="EDIT")
            restore_selected_face(obj, source_face_index)


# ------------------------------------------------------------
# Properties
# ------------------------------------------------------------

class SURFACEBOX_PG_State(PropertyGroup):
    p0: FloatVectorProperty(size=3)
    p1: FloatVectorProperty(size=3)
    p2: FloatVectorProperty(size=3)
    p3: FloatVectorProperty(size=3)
    plane_normal: FloatVectorProperty(size=3)
    source_material_name: StringProperty(default="")
    source_uv_mapping_json: StringProperty(default="")
    source_uv_map_name: StringProperty(default="")
    source_face_index: IntProperty(default=-1)
    # Polygon mode: JSON-encoded list of [x,y,z] world corners
    polygon_corners_json: StringProperty(default="")


def store_opening_draw_state(
    state,
    world_corners,
    plane_normal,
    source_material,
    source_uv_mapping_json,
    source_uv_map_name,
    source_face_index,
    polygon_mode=False,
):
    corners = cleaned_polygon_points(world_corners)
    if len(corners) < 3:
        raise RuntimeError("At least 3 valid polygon points are required.")

    # Keep the legacy four-vector fields populated for the rectangle path and
    # use JSON for true polygons with arbitrary vertex counts.
    padded = corners + [corners[-1]] * max(0, 4 - len(corners))
    state.p0 = padded[0]
    state.p1 = padded[1]
    state.p2 = padded[2]
    state.p3 = padded[3]

    state.polygon_corners_json = (
        json.dumps([[p.x, p.y, p.z] for p in corners])
        if polygon_mode or len(corners) != 4
        else ""
    )
    state.plane_normal = plane_normal
    state.source_material_name = source_material.name if source_material else ""
    state.source_uv_mapping_json = source_uv_mapping_json
    state.source_uv_map_name = source_uv_map_name
    state.source_face_index = source_face_index
    return corners


# ------------------------------------------------------------
# Dialog
# ------------------------------------------------------------

class SURFACEBOX_OT_TypeDialog(Operator):
    bl_idname = "surfacebox.type_dialog"
    bl_label = "Opening Type"

    opening_type: EnumProperty(
        name="Typ",
        items=[
            ("Window", "Window", ""),
            ("Door", "Door", ""),
        ],
        default="Window",
    )

    generate_interior: BoolProperty(
        name="Generate Interior Surface",
        description="Additionally generate an interior surface (inner ring/hole in the wall surface)",
        default=True,
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "opening_type", text="Opening")
        self.layout.prop(self, "generate_interior")

    def execute(self, context):
        obj = get_active_mesh_object(context)
        if obj is None:
            self.report({"ERROR"}, "No active mesh object found.")
            return {"CANCELLED"}

        state = context.scene.surfacebox_state

        # Polygon mode: use JSON-encoded corners if available
        if state.polygon_corners_json:
            corners = [Vector(p) for p in json.loads(state.polygon_corners_json)]
        else:
            corners = [
                Vector(state.p0),
                Vector(state.p1),
                Vector(state.p2),
                Vector(state.p3),
            ]
        plane_normal = Vector(state.plane_normal)

        source_material = None
        if state.source_material_name:
            source_material = bpy.data.materials.get(state.source_material_name)

        try:
            create_joined_surfaces(
                obj=obj,
                world_corners=corners,
                opening_type=self.opening_type,
                plane_normal=plane_normal,
                source_material=source_material,
                uv_mapping_json=state.source_uv_mapping_json,
                source_uv_map_name=state.source_uv_map_name,
                source_face_index=state.source_face_index,
                generate_interior=self.generate_interior,
            )
        except Exception as e:
            self.report({"ERROR"}, f"Error during creation:: {e}")
            return {"CANCELLED"}

        self.report({"INFO"}, f"{self.opening_type} generated successfully.")
        return {"FINISHED"}


# ------------------------------------------------------------
# Modal Draw
# ------------------------------------------------------------

class SURFACEBOX_OT_DrawBox(Operator):
    bl_idname = "surfacebox.draw_box"
    bl_label = "Draw Opening Box"
    bl_options = {"REGISTER", "UNDO"}

    _handle = None
    start_mouse = None
    current_mouse = None
    dragging = False
    plane_point = None
    plane_normal = None
    source_material = None
    source_uv_mapping_json = ""
    source_uv_map_name = ""
    source_face_index = -1
    snap_cache = None
    current_snap_active = False

    @staticmethod
    def draw_callback(self, context):
        if not self.dragging or self.start_mouse is None or self.current_mouse is None:
            return

        x1, y1 = self.start_mouse
        x2, y2 = self.current_mouse

        verts = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        indices = ((0, 1), (1, 2), (2, 3), (3, 0))

        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        batch = batch_for_shader(shader, "LINES", {"pos": verts}, indices=indices)

        gpu.state.blend_set("ALPHA")
        gpu.state.line_width_set(2.0)
        shader.bind()
        shader.uniform_float("color", (0.1, 0.7, 1.0, 1.0))
        batch.draw(shader)
        gpu.state.line_width_set(1.0)
        gpu.state.blend_set("NONE")

        if self.current_snap_active:
            draw_opening_cutter_snap_marker(self.current_mouse)

    def invoke(self, context, event):
        obj = get_active_mesh_object(context)
        if obj is None:
            self.report({"ERROR"}, "No active mesh object found.")
            return {"CANCELLED"}

        if context.area.type != "VIEW_3D":
            self.report({"ERROR"}, "Only available in the 3D viewport.")
            return {"CANCELLED"}

        if context.mode not in {"EDIT_MESH"}:
            self.report({"ERROR"}, "Please select exactly one face in Edit Mode.")
            return {"CANCELLED"}

        plane_point, plane_normal, source_material, source_uv_mapping_json, source_uv_map_name, source_face_index = get_selected_face_info(context, obj)

        if plane_point is None or plane_normal is None or source_material is None:
            self.report({"ERROR"}, "Please select exactly one face with a material in Edit Mode.")
            return {"CANCELLED"}

        self.plane_point = plane_point
        self.plane_normal = plane_normal.normalized()
        self.source_material = source_material
        self.source_uv_mapping_json = source_uv_mapping_json
        self.source_uv_map_name = source_uv_map_name
        self.source_face_index = source_face_index

        self.snap_cache = build_opening_cutter_snap_cache(context)
        self.start_mouse, self.snap_cache, self.current_snap_active = resolve_opening_cutter_snap_mouse(
            context,
            (event.mouse_region_x, event.mouse_region_y),
            snap_cache=self.snap_cache,
        )
        self.current_mouse = self.start_mouse
        self.dragging = True

        args = (self, context)
        self._handle = bpy.types.SpaceView3D.draw_handler_add(
            self.draw_callback, args, "WINDOW", "POST_PIXEL"
        )

        context.window_manager.modal_handler_add(self)
        context.area.tag_redraw()
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if context.area:
            context.area.tag_redraw()

        if event.type == "MOUSEMOVE":
            self.current_mouse, self.snap_cache, self.current_snap_active = resolve_opening_cutter_snap_mouse(
                context,
                (event.mouse_region_x, event.mouse_region_y),
                snap_cache=self.snap_cache,
            )
            return {"RUNNING_MODAL"}

        if event.type in {"ESC", "RIGHTMOUSE"}:
            self.finish(context)
            return {"CANCELLED"}

        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            self.current_mouse, self.snap_cache, self.current_snap_active = resolve_opening_cutter_snap_mouse(
                context,
                (event.mouse_region_x, event.mouse_region_y),
                snap_cache=self.snap_cache,
            )

            x1, y1 = self.start_mouse
            x2, y2 = self.current_mouse

            if abs(x2 - x1) < 4 or abs(y2 - y1) < 4:
                self.finish(context)
                self.report({"WARNING"}, "Box too small.")
                return {"CANCELLED"}

            screen_corners = [
                (x1, y1),
                (x2, y1),
                (x2, y2),
                (x1, y2),
            ]

            world_corners = []
            for mouse_xy in screen_corners:
                hit = mouse_to_plane(context, mouse_xy, self.plane_point, self.plane_normal)
                if hit is None:
                    self.finish(context)
                    self.report({"ERROR"}, "Projection onto plane failed.")
                    return {"CANCELLED"}
                world_corners.append(hit)

            state = context.scene.surfacebox_state
            try:
                store_opening_draw_state(
                    state,
                    world_corners,
                    self.plane_normal,
                    self.source_material,
                    self.source_uv_mapping_json,
                    self.source_uv_map_name,
                    self.source_face_index,
                    polygon_mode=False,
                )
            except Exception as e:
                self.finish(context)
                self.report({"ERROR"}, f"Polygon could not be prepared: {e}")
                return {"CANCELLED"}

            self.finish(context)
            bpy.ops.surfacebox.type_dialog("INVOKE_DEFAULT")
            return {"FINISHED"}

        return {"RUNNING_MODAL"}

    def finish(self, context):
        self.dragging = False
        self.snap_cache = None
        self.current_snap_active = False
        if self._handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, "WINDOW")
            self._handle = None


# ------------------------------------------------------------
# Modal Polygon Draw (F9)
# ------------------------------------------------------------

class SURFACEBOX_OT_DrawPolygon(Operator):
    """Draw an opening polygon by placing points on the selected face plane."""

    bl_idname = "surfacebox.draw_polygon"
    bl_label = "Draw Opening Polygon"
    bl_options = {"REGISTER", "UNDO"}

    _handle = None
    points_screen: list = []
    points_world: list = []
    current_mouse: tuple = (0, 0)
    plane_point = None
    plane_normal = None
    source_material = None
    source_uv_mapping_json = ""
    source_uv_map_name = ""
    source_face_index = -1
    snap_cache = None
    current_snap_active = False

    @staticmethod
    def draw_callback(self, context):
        if not self.points_screen and not self.current_snap_active:
            return

        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        gpu.state.blend_set("ALPHA")
        gpu.state.line_width_set(2.0)

        # Draw existing polygon edges
        if len(self.points_screen) >= 2:
            verts = self.points_screen[:]
            indices = [(i, i + 1) for i in range(len(verts) - 1)]
            batch = batch_for_shader(shader, "LINES", {"pos": verts}, indices=indices)
            shader.bind()
            shader.uniform_float("color", (0.1, 0.7, 1.0, 1.0))
            batch.draw(shader)

        # Draw line from last point to current mouse position
        if self.points_screen:
            last = self.points_screen[-1]
            cur = self.current_mouse
            verts = [last, cur]
            batch = batch_for_shader(shader, "LINES", {"pos": verts}, indices=[(0, 1)])
            shader.bind()
            shader.uniform_float("color", (0.1, 0.7, 1.0, 0.5))
            batch.draw(shader)

        # Draw closing line (last point to first) as dashed hint
        if len(self.points_screen) >= 3:
            verts = [self.points_screen[-1], self.points_screen[0]]
            batch = batch_for_shader(shader, "LINES", {"pos": verts}, indices=[(0, 1)])
            shader.bind()
            shader.uniform_float("color", (0.4, 1.0, 0.4, 0.4))
            batch.draw(shader)

        # Draw point markers
        if self.points_screen:
            gpu.state.point_size_set(8.0)
            batch = batch_for_shader(shader, "POINTS", {"pos": self.points_screen})
            shader.bind()
            shader.uniform_float("color", (1.0, 0.9, 0.1, 1.0))
            batch.draw(shader)
            gpu.state.point_size_set(1.0)

        gpu.state.line_width_set(1.0)
        gpu.state.blend_set("NONE")

        if self.current_snap_active:
            draw_opening_cutter_snap_marker(self.current_mouse)

    def invoke(self, context, event):
        obj = get_active_mesh_object(context)
        if obj is None:
            self.report({"ERROR"}, "No active mesh object found.")
            return {"CANCELLED"}

        if context.area.type != "VIEW_3D":
            self.report({"ERROR"}, "Only available in the 3D viewport.")
            return {"CANCELLED"}

        if context.mode not in {"EDIT_MESH"}:
            self.report({"ERROR"}, "Please select exactly one face in Edit Mode.")
            return {"CANCELLED"}

        plane_point, plane_normal, source_material, source_uv_mapping_json, source_uv_map_name, source_face_index = get_selected_face_info(context, obj)

        if plane_point is None or plane_normal is None or source_material is None:
            self.report({"ERROR"}, "Please select exactly one face with a material in Edit Mode.")
            return {"CANCELLED"}

        self.plane_point = plane_point
        self.plane_normal = plane_normal.normalized()
        self.source_material = source_material
        self.source_uv_mapping_json = source_uv_mapping_json
        self.source_uv_map_name = source_uv_map_name
        self.source_face_index = source_face_index

        self.snap_cache = build_opening_cutter_snap_cache(context)
        self.points_screen = []
        self.points_world = []
        self.current_mouse, self.snap_cache, self.current_snap_active = resolve_opening_cutter_snap_mouse(
            context,
            (event.mouse_region_x, event.mouse_region_y),
            snap_cache=self.snap_cache,
        )

        args = (self, context)
        self._handle = bpy.types.SpaceView3D.draw_handler_add(
            self.draw_callback, args, "WINDOW", "POST_PIXEL"
        )

        context.window_manager.modal_handler_add(self)
        context.area.tag_redraw()
        context.area.header_text_set("Polygon mode: Left mouse button = Set point | Right mouse button/Enter = Finish | ESC = Cancel")
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if context.area:
            context.area.tag_redraw()

        if event.type == "MOUSEMOVE":
            self.current_mouse, self.snap_cache, self.current_snap_active = resolve_opening_cutter_snap_mouse(
                context,
                (event.mouse_region_x, event.mouse_region_y),
                snap_cache=self.snap_cache,
            )
            return {"RUNNING_MODAL"}

        if event.type in {"ESC"}:
            self.finish(context)
            return {"CANCELLED"}

        if event.type == "RIGHTMOUSE" and event.value == "PRESS":
            # Finish polygon
            return self._complete_polygon(context)

        if event.type in {"RET", "NUMPAD_ENTER"} and event.value == "PRESS":
            # Finish polygon
            return self._complete_polygon(context)

        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            # Place a new point
            mouse_xy = (event.mouse_region_x, event.mouse_region_y)
            world_pt, resolved_mouse, self.snap_cache, self.current_snap_active = project_opening_cutter_mouse_to_plane(
                context,
                mouse_xy,
                self.plane_point,
                self.plane_normal,
                snap_cache=self.snap_cache,
            )
            if world_pt is None:
                self.report({"WARNING"}, "Point could not be projected onto plane.")
                return {"RUNNING_MODAL"}

            self.points_screen.append(resolved_mouse)
            self.points_world.append(world_pt)
            return {"RUNNING_MODAL"}

        if event.type == "Z" and event.value == "PRESS" and event.ctrl:
            # Undo last point
            if self.points_screen:
                self.points_screen.pop()
                self.points_world.pop()
            return {"RUNNING_MODAL"}

        return {"RUNNING_MODAL"}

    def _complete_polygon(self, context):
        if len(self.points_world) < 3:
            self.report({"WARNING"}, "At least 3 points are required.")
            return {"RUNNING_MODAL"}

        state = context.scene.surfacebox_state
        try:
            store_opening_draw_state(
                state,
                self.points_world,
                self.plane_normal,
                self.source_material,
                self.source_uv_mapping_json,
                self.source_uv_map_name,
                self.source_face_index,
                polygon_mode=True,
            )
        except Exception as e:
            self.report({"ERROR"}, f"Polygon could not be prepared: {e}")
            return {"RUNNING_MODAL"}

        self.finish(context)
        bpy.ops.surfacebox.type_dialog("INVOKE_DEFAULT")
        return {"FINISHED"}

    def finish(self, context):
        self.snap_cache = None
        self.current_snap_active = False
        if self._handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, "WINDOW")
            self._handle = None
        self.points_screen = []
        self.points_world = []
        if context.area:
            context.area.header_text_set(None)


# ------------------------------------------------------------
# UI
# ------------------------------------------------------------

class CGML3_PT_OpeningsInfo(Panel):
    bl_label = "Openings Cutter"
    bl_idname = "CGML3_PT_openings_info"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "CityGML"
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 30

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text="Start", icon="MESH_PLANE")
        box.label(text="Start in Edit Mode using F8 (Rectangle) or F9 (Polygon).")
        box.label(text="Alternative: F3 > Draw Opening Box / Draw Opening Polygon.")

        box = layout.box()
        box.label(text="What it does", icon="INFO")
        box.label(text="Generates export-compatible window or door surfaces")
        box.label(text="on the plane of a selected reference face.")
        box.label(text="It does not cut an opening using Boolean operations.")

        box = layout.box()
        box.label(text="How it works", icon="MOD_UVPROJECT")
        box.label(text="1. In Edit Mode, select exactly 1 face with a material.")
        box.label(text="2. F8 (Draw Rectangle) or F9 (Set Polygon Points).")
        box.label(text="3. For F9: LMB=Set Point, RMB/Enter=Finish, Ctrl+Z=Undo.")
        box.label(text="4. Choose type (Window/Door) → Exterior Opening + Interior Face.")

        box = layout.box()
        box.label(text="What is inherited", icon="MATERIAL")
        box.label(text="Material, textures, and relevant custom properties")
        box.label(text="are inherited from the selected source face.")
        box.label(text="The UVs remain in the existing UV map of the mesh.")

        box = layout.box()
        box.label(text="Note", icon="EVENT_S")
        box.label(text="The tool expects exactly 1 selected face")
        box.label(text="mit Material im Edit Mode.")


# ------------------------------------------------------------
# Register / Unregister
# ------------------------------------------------------------

classes = (
    SURFACEBOX_PG_State,
    SURFACEBOX_OT_TypeDialog,
    SURFACEBOX_OT_DrawBox,
    SURFACEBOX_OT_DrawPolygon,
    CGML3_PT_OpeningsInfo,
)


def register_keymaps():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return

    km = kc.keymaps.new(name="Mesh", space_type="EMPTY")
    kmi = km.keymap_items.new(
        "surfacebox.draw_box",
        type="F8",
        value="PRESS",
    )
    addon_keymaps.append((km, kmi))

    kmi2 = km.keymap_items.new(
        "surfacebox.draw_polygon",
        type="F9",
        value="PRESS",
    )
    addon_keymaps.append((km, kmi2))


def unregister_keymaps():
    for km, kmi in addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    addon_keymaps.clear()


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.surfacebox_state = PointerProperty(type=SURFACEBOX_PG_State)
    register_keymaps()


def unregister():
    unregister_keymaps()

    if hasattr(bpy.types.Scene, "surfacebox_state"):
        del bpy.types.Scene.surfacebox_state

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
