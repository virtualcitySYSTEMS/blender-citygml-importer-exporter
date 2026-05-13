# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Assign Object Part – semantic parent-child assignment via Outliner hierarchy.

Creates an EMPTY as CityGML feature container when the target is a Mesh.
CityGML metadata (cgml3_feature, gml_id) migrates from the Mesh to the EMPTY.
The Mesh becomes a child of the EMPTY (= Outer Shell).
New Parts/Installations/Furniture also become children of the EMPTY.

Resulting Outliner structure:
    📦 Gebäude_Building  (EMPTY, cgml3_feature="Building", structure_type="hierarchical")
      ├── 🔶 Gebäude     (MESH, structure_part="outer_shell")
      └── 🔶 Anbau       (MESH, cgml3_feature="BuildingPart")

Both the hierarchical exporter and the importer understand this structure.
"""

from __future__ import annotations

from uuid import uuid4

import bpy
from bpy.props import EnumProperty
from bpy.types import Menu, Operator

from ..shared.lod_helpers import iter_mesh_materials, material_surface_type, read_lod_value


# Backwards compatibility for old operator enum values.
_LEGACY_ASSIGN_FEATURE_MAP = {
    'BUILDING_PART': "BuildingPart",
    'BUILDING_INSTALLATION': "BuildingInstallation",
}

_UNSUPPORTED_ASSIGN_MODE = "__UNSUPPORTED__"


# item: (cgml3_feature, menu_label, description, blender_icon)
_ASSIGN_OPTIONS_V3 = {
    "Building": (
        ("BuildingPart", "BuildingPart", "Assign as BuildingPart", 'HOME'),
        ("BuildingInstallation", "BuildingInstallation", "Assign as BuildingInstallation", 'MOD_ARRAY'),
        ("BuildingFurniture", "BuildingFurniture", "Assign as BuildingFurniture", 'OUTLINER_OB_MESH'),
    ),
    "BuildingPart": (
        ("BuildingInstallation", "BuildingInstallation", "Assign as BuildingInstallation", 'MOD_ARRAY'),
        ("BuildingFurniture", "BuildingFurniture", "Assign as BuildingFurniture", 'OUTLINER_OB_MESH'),
    ),
    "Bridge": (
        ("BridgePart", "BridgePart", "Assign as BridgePart", 'MESH_CUBE'),
        ("BridgeInstallation", "BridgeInstallation", "Assign as BridgeInstallation", 'MOD_ARRAY'),
        ("BridgeConstructionElement", "BridgeConstructionElement", "Assign as BridgeConstructionElement (structural)", 'CONSTRAINT_BONE'),
        ("BridgeFurniture", "BridgeFurniture", "Assign as BridgeFurniture", 'OUTLINER_OB_MESH'),
    ),
    "BridgePart": (
        ("BridgeInstallation", "BridgeInstallation", "Assign as BridgeInstallation", 'MOD_ARRAY'),
        ("BridgeConstructionElement", "BridgeConstructionElement", "Assign as BridgeConstructionElement (structural)", 'CONSTRAINT_BONE'),
        ("BridgeFurniture", "BridgeFurniture", "Assign as BridgeFurniture", 'OUTLINER_OB_MESH'),
    ),
    "Tunnel": (
        ("TunnelPart", "TunnelPart", "Assign as TunnelPart", 'MESH_CUBE'),
        ("TunnelInstallation", "TunnelInstallation", "Assign as TunnelInstallation", 'MOD_ARRAY'),
        ("TunnelFurniture", "TunnelFurniture", "Assign as TunnelFurniture", 'OUTLINER_OB_MESH'),
    ),
    "TunnelPart": (
        ("TunnelInstallation", "TunnelInstallation", "Assign as TunnelInstallation", 'MOD_ARRAY'),
        ("TunnelFurniture", "TunnelFurniture", "Assign as TunnelFurniture", 'OUTLINER_OB_MESH'),
    ),
}


_ASSIGN_OPTIONS_V2 = {
    "Building": (
        ("BuildingPart", "BuildingPart", "Assign as BuildingPart", 'HOME'),
        ("BuildingInstallation", "BuildingInstallation", "Assign as outer BuildingInstallation", 'MOD_ARRAY'),
        ("IntBuildingInstallation", "IntBuildingInstallation", "Assign as interior BuildingInstallation", 'MOD_ARRAY'),
        ("BuildingFurniture", "BuildingFurniture", "Assign as BuildingFurniture (LOD4)", 'OUTLINER_OB_MESH'),
    ),
    "BuildingPart": (
        ("BuildingPart", "BuildingPart", "Assign as nested BuildingPart", 'HOME'),
        ("BuildingInstallation", "BuildingInstallation", "Assign as outer BuildingInstallation", 'MOD_ARRAY'),
        ("IntBuildingInstallation", "IntBuildingInstallation", "Assign as interior BuildingInstallation", 'MOD_ARRAY'),
        ("BuildingFurniture", "BuildingFurniture", "Assign as BuildingFurniture (LOD4)", 'OUTLINER_OB_MESH'),
    ),
    "Bridge": (
        ("BridgePart", "BridgePart", "Assign as BridgePart", 'MESH_CUBE'),
        ("BridgeInstallation", "BridgeInstallation", "Assign as outer BridgeInstallation", 'MOD_ARRAY'),
        ("IntBridgeInstallation", "IntBridgeInstallation", "Assign as interior BridgeInstallation", 'MOD_ARRAY'),
        ("BridgeConstructionElement", "BridgeConstructionElement", "Assign as BridgeConstructionElement (structural)", 'CONSTRAINT_BONE'),
        ("BridgeFurniture", "BridgeFurniture", "Assign as BridgeFurniture (LOD4)", 'OUTLINER_OB_MESH'),
    ),
    "BridgePart": (
        ("BridgePart", "BridgePart", "Assign as nested BridgePart", 'MESH_CUBE'),
        ("BridgeInstallation", "BridgeInstallation", "Assign as outer BridgeInstallation", 'MOD_ARRAY'),
        ("IntBridgeInstallation", "IntBridgeInstallation", "Assign as interior BridgeInstallation", 'MOD_ARRAY'),
        ("BridgeConstructionElement", "BridgeConstructionElement", "Assign as BridgeConstructionElement (structural)", 'CONSTRAINT_BONE'),
        ("BridgeFurniture", "BridgeFurniture", "Assign as BridgeFurniture (LOD4)", 'OUTLINER_OB_MESH'),
    ),
    "Tunnel": (
        ("TunnelPart", "TunnelPart", "Assign as TunnelPart", 'MESH_CUBE'),
        ("TunnelInstallation", "TunnelInstallation", "Assign as outer TunnelInstallation", 'MOD_ARRAY'),
        ("IntTunnelInstallation", "IntTunnelInstallation", "Assign as interior TunnelInstallation", 'MOD_ARRAY'),
        ("TunnelFurniture", "TunnelFurniture", "Assign as TunnelFurniture (LOD4)", 'OUTLINER_OB_MESH'),
    ),
    "TunnelPart": (
        ("TunnelPart", "TunnelPart", "Assign as nested TunnelPart", 'MESH_CUBE'),
        ("TunnelInstallation", "TunnelInstallation", "Assign as outer TunnelInstallation", 'MOD_ARRAY'),
        ("IntTunnelInstallation", "IntTunnelInstallation", "Assign as interior TunnelInstallation", 'MOD_ARRAY'),
        ("TunnelFurniture", "TunnelFurniture", "Assign as TunnelFurniture (LOD4)", 'OUTLINER_OB_MESH'),
    ),
}

_PART_PROMOTION_MAP = {
    "BuildingPart": "Building",
    "BridgePart": "Bridge",
    "TunnelPart": "Tunnel",
}

_PROTECTED_SURFACE_TYPES = {
    "Window",
    "Door",
    "WindowSurface",
    "DoorSurface",
    "InteriorWallSurface",
    "CeilingSurface",
    "FloorSurface",
    "ClosureSurface",
    "OuterCeilingSurface",
    "OuterFloorSurface",
}
_AUTO_SURFACE_MARKER = "cgml3_auto_object_part_surface"
_AUTO_SURFACE_SOURCE_KEY = "cgml3_auto_object_part_source"
_ROOF_GROUND_NORMAL_Z_MIN = 0.5
_FACE_ID_KEYS = {
    "app_target_or_uri",
    "cgml3_feature",
    "cgml3_subfeature_id",
    "cgml3_subfeature_name",
    "cgml3_subfeature_namespace",
    "cgml3_subfeature_parent_id",
    "cgml3_subfeature_relation",
    "cgml3_subfeature_type",
    "cgml_part_id",
    "cgml_part_name",
    "cgml_part_relation",
    "cgml_part_type",
    "CityGMLComplexType",
    "con_opening_id",
    "con_surface_id",
    "ExteriorPolyId",
    "ExteriorRingId",
    "filling_parent_surface_id",
    "gml_compositesurface_id",
    "gml_multisurface_id",
    "gml_polygon_id",
    "gml_ring_id",
    "gml_surface_id",
    "opening_gml_id",
    "opening_id",
    "opening_surface_id",
    "part_id",
    "part_name",
    "part_relation",
    "part_type",
    "surface_id",
}


def _feature_type_lookup() -> dict[str, str]:
    lookup = {}
    for option_map in (_ASSIGN_OPTIONS_V2, _ASSIGN_OPTIONS_V3):
        for parent_type, options in option_map.items():
            lookup[parent_type.casefold()] = parent_type
            for feature_type, _label, _desc, _icon in options:
                lookup[str(feature_type).casefold()] = str(feature_type)
    for legacy, feature_type in _LEGACY_ASSIGN_FEATURE_MAP.items():
        lookup[str(legacy).casefold()] = str(feature_type)
    for feature_type, parent_type in _PART_PROMOTION_MAP.items():
        lookup[str(feature_type).casefold()] = str(feature_type)
        lookup[str(parent_type).casefold()] = str(parent_type)
    return lookup


_CANONICAL_FEATURE_TYPES = _feature_type_lookup()


def _get_gml_id(obj) -> str:
    """Return gml_id of object, or empty string."""
    try:
        val = obj.get("gml_id")
    except Exception:
        val = None
    if val and str(val).strip():
        return str(val).strip()
    return ""


def _get_feature_type(obj) -> str | None:
    """Return the CityGML feature type of an object, or None."""
    if obj is None:
        return None
    for key in ("cgml3_feature", "ModelType", "feature_type"):
        try:
            val = obj.get(key)
        except Exception:
            val = None
        if val and str(val).strip():
            return str(val).strip()
    return None


def _local_feature_type(feature_type: str | None) -> str:
    """Return local feature name without namespace prefix."""
    if not feature_type:
        return ""
    text = str(feature_type).strip()
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    return text


def _canonical_feature_type(feature_type: str | None) -> str:
    local = _local_feature_type(feature_type)
    if not local:
        return ""
    return _CANONICAL_FEATURE_TYPES.get(local.casefold(), local)


def _get_hierarchy_container(obj):
    """Return the hierarchical EMPTY for obj if it already belongs to one."""
    if obj is None:
        return None
    if obj.type == 'EMPTY' and obj.get("structure_type") == "hierarchical":
        return obj
    if obj.parent and obj.parent.type == 'EMPTY':
        if obj.parent.get("structure_type") == "hierarchical":
            return obj.parent
    return None


def _get_target_feature_type(obj) -> str:
    """Return the semantic feature type that controls assignment options."""
    container = _get_hierarchy_container(obj)
    feature = _get_feature_type(container) if container else None
    if not feature:
        feature = _get_feature_type(obj)
    return _canonical_feature_type(feature) or "Building"


def _get_lod_from_idblock(obj) -> int | None:
    if obj is None:
        return None
    for key in ("lod", "cgml3_lod"):
        try:
            lod = read_lod_value(obj.get(key, None))
        except Exception:
            lod = None
        if lod is not None:
            return lod
    return None


def _iter_mesh_descendants(obj):
    if obj is None:
        return
    try:
        if obj.type == 'MESH':
            yield obj
    except Exception:
        return

    try:
        children = list(getattr(obj, "children_recursive", []) or [])
    except Exception:
        children = []
        stack = list(getattr(obj, "children", []) or [])
        while stack:
            child = stack.pop(0)
            children.append(child)
            stack.extend(list(getattr(child, "children", []) or []))

    for child in children:
        try:
            if child.type == 'MESH':
                yield child
        except Exception:
            continue


def _dominant_material_lod(mesh_objects) -> int | None:
    lod_counts = {}
    for mesh_obj in mesh_objects or []:
        for mat in iter_mesh_materials(mesh_obj):
            lod = read_lod_value(mat.get("lod", None))
            if lod is None:
                continue
            lod_counts[lod] = lod_counts.get(lod, 0) + 1
    if not lod_counts:
        return None
    return max(lod_counts.items(), key=lambda item: (item[1], item[0]))[0]


def _max_material_lod(mesh_objects) -> int | None:
    max_lod = None
    for mesh_obj in mesh_objects or []:
        for mat in iter_mesh_materials(mesh_obj):
            lod = read_lod_value(mat.get("lod", None))
            if lod is None:
                continue
            if max_lod is None or lod > max_lod:
                max_lod = lod
    return max_lod


def _top_level_lod(hierarchy_empty) -> int | None:
    outer_shell_meshes = []
    other_meshes = []
    candidate_lods = []

    lod = _get_lod_from_idblock(hierarchy_empty)
    if lod is not None:
        candidate_lods.append(lod)

    for mesh_obj in _iter_mesh_descendants(hierarchy_empty):
        mesh_lod = _get_lod_from_idblock(mesh_obj)
        if mesh_lod is not None:
            candidate_lods.append(mesh_lod)
        try:
            if mesh_obj.get("structure_part") == "outer_shell":
                outer_shell_meshes.append(mesh_obj)
            else:
                other_meshes.append(mesh_obj)
        except Exception:
            other_meshes.append(mesh_obj)

    outer_shell_max = _max_material_lod(outer_shell_meshes)
    if outer_shell_max is not None:
        candidate_lods.append(outer_shell_max)

    other_max = _max_material_lod(other_meshes)
    if other_max is not None:
        candidate_lods.append(other_max)

    if candidate_lods:
        return max(candidate_lods)

    return (
        _dominant_material_lod(outer_shell_meshes)
        or _dominant_material_lod(other_meshes)
    )


def _set_idblock_lod(obj, lod: int) -> bool:
    changed = False
    current = _get_lod_from_idblock(obj)
    if current != lod:
        try:
            obj["lod"] = int(lod)
            changed = True
        except Exception:
            pass

    try:
        cgml_lod = read_lod_value(obj.get("cgml3_lod", None))
    except Exception:
        cgml_lod = None
    if cgml_lod is not None and cgml_lod != lod:
        try:
            obj["cgml3_lod"] = int(lod)
            changed = True
        except Exception:
            pass
    return changed


def _set_mesh_material_lod(mesh_obj, lod: int) -> bool:
    changed = False
    try:
        materials = list(getattr(mesh_obj.data, "materials", []) or [])
    except Exception:
        materials = []

    copied = {}
    for idx, mat in enumerate(materials):
        if mat is None:
            continue
        target_mat = mat
        try:
            if getattr(mat, "users", 0) > 1:
                target_mat = copied.get(mat.name)
                if target_mat is None:
                    target_mat = mat.copy()
                    target_mat.name = f"{mat.name}_lod{lod}"
                    copied[mat.name] = target_mat
                mesh_obj.data.materials[idx] = target_mat
        except Exception:
            target_mat = mat

        current = read_lod_value(target_mat.get("lod", None))
        if current == lod:
            continue
        try:
            target_mat["lod"] = int(lod)
            changed = True
        except Exception:
            pass
    return changed


def _apply_lod_to_assigned_object(obj, lod: int | None) -> bool:
    if obj is None or lod is None:
        return False

    changed = _set_idblock_lod(obj, lod)
    for mesh_obj in _iter_mesh_descendants(obj):
        changed = _set_idblock_lod(mesh_obj, lod) or changed
        changed = _set_mesh_material_lod(mesh_obj, lod) or changed
    return changed


def _world_normal_for_poly(mesh_obj, poly):
    try:
        normal_matrix = mesh_obj.matrix_world.to_3x3().inverted().transposed()
        normal = normal_matrix @ poly.normal
        if normal.length_squared > 1e-12:
            normal.normalize()
            return normal
    except Exception:
        pass
    try:
        normal = poly.normal.copy()
        if normal.length_squared > 1e-12:
            normal.normalize()
            return normal
    except Exception:
        pass
    return None


def _world_z_bounds(mesh_obj):
    z_values = []
    try:
        matrix_world = mesh_obj.matrix_world
        for vert in mesh_obj.data.vertices:
            z_values.append(float((matrix_world @ vert.co).z))
    except Exception:
        return None, None
    if not z_values:
        return None, None
    return min(z_values), max(z_values)


def _poly_center_z_world(mesh_obj, poly) -> float | None:
    try:
        matrix_world = mesh_obj.matrix_world
        verts = mesh_obj.data.vertices
        values = [float((matrix_world @ verts[idx].co).z) for idx in poly.vertices]
    except Exception:
        values = []
    if not values:
        return None
    return sum(values) / len(values)


def _classify_boundary_surface(mesh_obj, poly, z_min: float, z_max: float) -> str:
    normal = _world_normal_for_poly(mesh_obj, poly)
    if normal is None:
        return "WallSurface"

    center_z = _poly_center_z_world(mesh_obj, poly)
    if center_z is None:
        return "WallSurface"

    z_span = max(float(z_max) - float(z_min), 1e-9)
    upper_zone_min = float(z_min) + 0.35 * z_span
    lower_zone_max = float(z_max) - 0.35 * z_span

    if float(normal.z) >= _ROOF_GROUND_NORMAL_Z_MIN and center_z >= upper_zone_min:
        return "RoofSurface"
    if float(normal.z) <= -_ROOF_GROUND_NORMAL_Z_MIN and center_z <= lower_zone_max:
        return "GroundSurface"
    return "WallSurface"


def _source_material_for_poly(mesh_obj, poly):
    try:
        idx = int(poly.material_index)
    except Exception:
        idx = -1
    try:
        materials = mesh_obj.data.materials
    except Exception:
        return None
    if 0 <= idx < len(materials):
        return materials[idx]
    return None


def _face_semantics_are_protected(mat) -> bool:
    if mat is None:
        return False
    try:
        if bool(mat.get("is_opening", False)) or bool(mat.get("Interior", False)):
            return True
    except Exception:
        pass
    for key in ("OpeningType", "opening_type", "openingType", "cgml_opening_type"):
        try:
            if str(mat.get(key, "") or "").strip():
                return True
        except Exception:
            continue
    return material_surface_type(mat) in _PROTECTED_SURFACE_TYPES


def _delete_material_keys(mat, keys):
    for key in keys:
        try:
            if key in mat:
                del mat[key]
        except Exception:
            continue


def _set_boundary_surface_material_metadata(mat, surface_type: str, lod: int | None):
    try:
        mat["SurfaceTyp"] = surface_type
        mat["surface_type"] = surface_type
        mat["Typ"] = surface_type
        mat[_AUTO_SURFACE_MARKER] = True
    except Exception:
        pass
    if lod is not None:
        try:
            mat["lod"] = int(lod)
        except Exception:
            pass


def _make_boundary_surface_material(source_mat, surface_type: str, lod: int | None):
    try:
        if source_mat is not None:
            mat = source_mat.copy()
            mat.name = f"{surface_type}_{uuid4().hex[:8]}"
        else:
            mat = bpy.data.materials.new(name=f"{surface_type}_{uuid4().hex[:8]}")
    except Exception:
        mat = bpy.data.materials.new(name=f"{surface_type}_{uuid4().hex[:8]}")

    _delete_material_keys(mat, _FACE_ID_KEYS)
    _set_boundary_surface_material_metadata(mat, surface_type, lod)
    try:
        mat[_AUTO_SURFACE_SOURCE_KEY] = getattr(source_mat, "name", "") if source_mat is not None else ""
    except Exception:
        pass
    return mat


def _auto_surface_material_index(mesh_obj, source_mat, surface_type: str, lod: int | None, cache) -> int:
    try:
        source_name = getattr(source_mat, "name", "") if source_mat is not None else ""
        cache_key = (source_name, surface_type)
        if cache_key in cache:
            return cache[cache_key]

        for idx, mat in enumerate(mesh_obj.data.materials):
            if mat is None:
                continue
            try:
                if not bool(mat.get(_AUTO_SURFACE_MARKER, False)):
                    continue
                if str(mat.get(_AUTO_SURFACE_SOURCE_KEY, "") or "") != source_name:
                    continue
            except Exception:
                continue
            if material_surface_type(mat) == surface_type:
                _set_boundary_surface_material_metadata(mat, surface_type, lod)
                cache[cache_key] = idx
                return idx

        mat = _make_boundary_surface_material(source_mat, surface_type, lod)
        mesh_obj.data.materials.append(mat)
        idx = len(mesh_obj.data.materials) - 1
        cache[cache_key] = idx
        return idx
    except Exception:
        return 0


def _assign_auto_boundary_surfaces(obj, lod: int | None) -> bool:
    """
    Give newly assigned parts/installations useful boundary semantics.

    The assigned object itself carries the feature type (BuildingPart,
    BuildingInstallation, ...). Its mesh faces should therefore be boundary
    surfaces, not feature-type surfaces. Existing opening/interior semantics are
    left untouched.
    """
    changed = False
    for mesh_obj in _iter_mesh_descendants(obj):
        try:
            mesh = mesh_obj.data
            polygons = list(mesh.polygons)
        except Exception:
            continue
        if not polygons:
            continue

        z_min, z_max = _world_z_bounds(mesh_obj)
        if z_min is None or z_max is None:
            continue

        material_cache = {}
        mesh_changed = False
        for poly in polygons:
            source_mat = _source_material_for_poly(mesh_obj, poly)
            if _face_semantics_are_protected(source_mat):
                continue

            surface_type = _classify_boundary_surface(mesh_obj, poly, z_min, z_max)
            mat_index = _auto_surface_material_index(mesh_obj, source_mat, surface_type, lod, material_cache)
            if int(poly.material_index) != int(mat_index):
                try:
                    poly.material_index = mat_index
                    mesh_changed = True
                except Exception:
                    continue

        if mesh_changed:
            changed = True
            try:
                mesh.update()
            except Exception:
                pass
    return changed


def _detect_scene_citygml_version(context) -> str:
    """Best-effort CityGML version detection for dynamic assignment options."""
    try:
        version = str(context.scene.get("cgml3_scan_version", "")).strip()
        if version.startswith("2"):
            return "2.0"
        if version.startswith("3"):
            return "3.0"
    except Exception:
        pass
    try:
        root_coll = context.scene.collection
        for col in root_coll.children:
            name_up = col.name.upper()
            if "CITYGML2" in name_up:
                return "2.0"
            if "CITYGML3" in name_up:
                return "3.0"
    except Exception:
        pass
    return "3.0"


def _get_assign_options_for_feature(feature_type: str, version: str):
    """Return assignable child feature specs for a parent feature."""
    local = _canonical_feature_type(feature_type)
    option_map = _ASSIGN_OPTIONS_V2 if version == "2.0" else _ASSIGN_OPTIONS_V3
    options = option_map.get(local)
    if options is None and version == "2.0":
        options = _ASSIGN_OPTIONS_V3.get(local)
    return options or ()


def _get_assign_options(context, target=None):
    """Return assignable child feature specs for the current active target."""
    if target is None:
        target = getattr(context, "active_object", None)
    if target is None:
        return ()
    version = _detect_scene_citygml_version(context)
    return _get_assign_options_for_feature(_get_target_feature_type(target), version)


def _assign_mode_items(self, context):
    """Dynamic EnumProperty items based on the active CityGML feature."""
    options = _get_assign_options(context)
    if not options:
        return [(
            _UNSUPPORTED_ASSIGN_MODE,
            "No compatible type",
            "The active object is not a supported Building, Bridge, or Tunnel feature",
        )]
    return [(feature, label, desc) for feature, label, desc, _icon in options]


def _ensure_gml_id(obj):
    """Ensure the object has a gml_id custom property."""
    if not _get_gml_id(obj):
        obj["gml_id"] = f"UUID_{uuid4().hex}"


_FEATURE_METADATA_TRANSFER_SKIP_KEYS = {
    "cgml3_feature",
    "gml_id",
    "structure_type",
    "structure_part",
    "surface_id_map",
    "surfaces_info",
    "cgml3_surface_generic_attributes",
    "cgml3_surface_generic_attributes_json",
    "cgml3_uv_start_by_ring",
    "cgml3_flip_v",
    "gml_ring_id",
    "gml_polygon_id",
    "gml_multisurface_id",
    "gml_compositesurface_id",
    "gml_surface_id",
    "surface_id",
    "surface_type",
    "SurfaceTyp",
    "image_path",
}

_FEATURE_METADATA_TRANSFER_SKIP_PREFIXES = (
    "_",
    "apt_",
    "cgml3_subfeature_",
)


def _copy_feature_metadata_to_empty(source, empty):
    """Copy feature-level custom properties from the original mesh to the hierarchy EMPTY."""
    for key, value in getattr(source, "items", lambda: [])():
        if not isinstance(key, str):
            continue
        if key in _FEATURE_METADATA_TRANSFER_SKIP_KEYS:
            continue
        if any(key.startswith(prefix) for prefix in _FEATURE_METADATA_TRANSFER_SKIP_PREFIXES):
            continue
        if key in empty:
            continue
        try:
            empty[key] = value
        except (TypeError, ValueError):
            try:
                empty[key] = str(value)
            except Exception:
                continue


def _get_or_create_hierarchy_empty(target, context) -> bpy.types.Object:
    """
    Get or create an EMPTY that serves as the hierarchical CityGML feature container.

    If target is already an EMPTY with structure_type="hierarchical", return it directly.
    If target is a MESH, create a new EMPTY, transfer feature metadata, and parent the mesh.
    """
    # Case 1: target is already a hierarchical EMPTY
    if target.type == 'EMPTY' and target.get("structure_type") == "hierarchical":
        return target

    # Case 2: target mesh is already parented to a hierarchical EMPTY
    if target.parent and target.parent.type == 'EMPTY':
        if target.parent.get("structure_type") == "hierarchical":
            return target.parent

    # Case 3: target is a MESH -> create feature EMPTY
    # Collect metadata from mesh before transfer
    gml_id = _get_gml_id(target)
    feat_type = _get_feature_type(target) or "Building"
    feat_type_local = _canonical_feature_type(feat_type) or "Building"
    if not gml_id:
        gml_id = f"UUID_{uuid4().hex}"

    # Create EMPTY with the same world transform as the target so imported
    # FBX objects keep their placement when moved into the CityGML hierarchy.
    empty_name = f"{target.name}_{feat_type_local}"
    empty = bpy.data.objects.new(empty_name, None)
    empty.empty_display_type = 'PLAIN_AXES'
    empty.empty_display_size = 0.5
    target_world = target.matrix_world.copy()

    # Link to same collection(s) as the target
    for col in target.users_collection:
        col.objects.link(empty)

    empty.matrix_world = target_world

    # Set hierarchical metadata on EMPTY
    empty["structure_type"] = "hierarchical"
    empty["cgml3_feature"] = feat_type_local
    empty["gml_id"] = gml_id

    # Copy additional CityGML attributes from mesh to empty
    _TRANSFER_KEYS = (
        "yearOfConstruction", "yearOfDemolition",
        "measuredHeight", "storeysAboveGround", "storeysBelowGround",
        "roofType", "function", "usage", "class", "isMovable",
        "bldg:yearOfConstruction", "bldg:yearOfDemolition",
        "bldg:measuredHeight", "bldg:storeysAboveGround",
        "bldg:storeysBelowGround", "bldg:roofType",
        "bldg:function", "bldg:usage", "bldg:class",
        "brid:yearOfConstruction", "brid:yearOfDemolition",
        "brid:isMovable", "brid:function", "brid:usage", "brid:class",
        "tun:yearOfConstruction", "tun:yearOfDemolition",
        "tun:function", "tun:usage", "tun:class",
        "gml_name", "gml_description",
    )
    for key in _TRANSFER_KEYS:
        val = target.get(key)
        if val is not None:
            empty[key] = val
            del target[key]

    _copy_feature_metadata_to_empty(target, empty)

    # Remove feature metadata from mesh (it now lives on the EMPTY)
    if "cgml3_feature" in target:
        del target["cgml3_feature"]
    if "gml_id" in target:
        del target["gml_id"]

    # Mark mesh as outer shell
    target["structure_part"] = "outer_shell"

    # Parent mesh under EMPTY (keep world transform)
    target.parent = empty
    target.matrix_world = target_world

    return empty


def _get_assign_sources(context, target):
    """Return selected objects that are NOT the target (or its container EMPTY)."""
    # If target is already under a building EMPTY, also exclude that EMPTY
    exclude = {target}
    if target.parent and target.parent.type == 'EMPTY':
        if target.parent.get("structure_type") == "hierarchical":
            exclude.add(target.parent)

    sources = []
    for obj in context.selected_objects:
        if obj in exclude:
            continue
        if obj.type not in ('MESH', 'EMPTY'):
            continue
        sources.append(obj)
    return sources


class CGML3_OT_AssignObjectPart(Operator):
    """Assign selected objects as semantic parts of the active CityGML feature (Outliner hierarchy)"""

    bl_idname = "cgml3.assign_object_part"
    bl_label = "Assign Object Part"
    bl_options = {'REGISTER', 'UNDO'}

    assign_mode: EnumProperty(
        name="Assign Mode",
        items=_assign_mode_items,
    )

    def execute(self, context):
        if context.mode != 'OBJECT':
            self.report({'WARNING'}, "Please switch to Object Mode")
            return {'CANCELLED'}

        active = context.active_object
        if active is None:
            self.report({'WARNING'}, "No active object. Select target last (active).")
            return {'CANCELLED'}

        target = active
        if target.type not in ('MESH', 'EMPTY'):
            self.report({'WARNING'},
                        f"Active object '{active.name}' must be a Mesh or Empty.")
            return {'CANCELLED'}

        sources = _get_assign_sources(context, target)
        if not sources:
            self.report({'WARNING'},
                        "No source objects to assign. Select source objects + target (active).")
            return {'CANCELLED'}

        feature_type = _canonical_feature_type(
            _LEGACY_ASSIGN_FEATURE_MAP.get(self.assign_mode, self.assign_mode)
        )
        if feature_type == _UNSUPPORTED_ASSIGN_MODE:
            self.report({'WARNING'}, "Active object is not a supported Building, Bridge, or Tunnel feature")
            return {'CANCELLED'}

        allowed = {_canonical_feature_type(item[0]) for item in _get_assign_options(context, target)}
        if feature_type not in allowed:
            parent_feature = _get_target_feature_type(target)
            self.report({'WARNING'},
                        f"{feature_type} cannot be assigned under {parent_feature}")
            return {'CANCELLED'}

        # Get or create the hierarchy EMPTY container
        hierarchy_empty = _get_or_create_hierarchy_empty(target, context)
        inherited_lod = _top_level_lod(hierarchy_empty)

        assigned_count = 0
        for source in sources:
            # Set CityGML feature type on source
            source["cgml3_feature"] = feature_type

            # Ensure source has gml_id
            _ensure_gml_id(source)

            # New parts inherit the current LoD of the top-level CityGML feature.
            _apply_lod_to_assigned_object(source, inherited_lod)
            _assign_auto_boundary_surfaces(source, inherited_lod)

            # Parent source under the hierarchy EMPTY (keep world transform)
            source_world = source.matrix_world.copy()
            source.parent = hierarchy_empty
            source.matrix_world = source_world

            assigned_count += 1

        if assigned_count > 0:
            self.report({'INFO'},
                        f"Assigned {assigned_count} object(s) as {feature_type} "
                        f"under '{hierarchy_empty.name}'")
        return {'FINISHED'}


class CGML3_OT_DetachObjectPart(Operator):
    """Remove object from Building hierarchy and make it an independent feature"""

    bl_idname = "cgml3.detach_object_part"
    bl_label = "Detach Object Part"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT':
            self.report({'WARNING'}, "Please switch to Object Mode")
            return {'CANCELLED'}

        detached = 0
        for obj in list(context.selected_objects):
            # Only detach mesh children from hierarchy EMPTYs
            if not obj.parent:
                continue
            if obj.parent.type != 'EMPTY':
                continue
            if obj.parent.get("structure_type") != "hierarchical":
                continue

            # Don't detach the outer shell (it IS the building geometry)
            if obj.get("structure_part") == "outer_shell":
                continue

            # Unparent (keep world transform)
            mat = obj.matrix_world.copy()
            obj.parent = None
            obj.matrix_world = mat

            # Promote semantic parts to standalone root features where applicable.
            feat = _get_feature_type(obj)
            promoted = _PART_PROMOTION_MAP.get(_canonical_feature_type(feat))
            if promoted:
                obj["cgml3_feature"] = promoted

            detached += 1

        if detached == 0:
            self.report({'WARNING'}, "No detachable parts selected (outer shells cannot be detached)")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Detached {detached} object(s) as independent features")
        return {'FINISHED'}


class CGML3_MT_AssignObjectPartContext(Menu):
    """Submenu for assigning object parts semantically"""

    bl_label = "Assign Object Part"
    bl_idname = "CGML3_MT_assign_object_part_context"

    def draw(self, context):
        layout = self.layout

        options = _get_assign_options(context)
        if options:
            for feature, label, _desc, icon in options:
                op = layout.operator(
                    "cgml3.assign_object_part",
                    text=label,
                    icon=icon,
                )
                op.assign_mode = feature
        else:
            active_feature = _get_target_feature_type(context.active_object)
            row = layout.row()
            row.enabled = False
            row.label(text=f"No compatible types for {active_feature}", icon='INFO')

        layout.separator()

        layout.operator(
            "cgml3.detach_object_part",
            text="Detach (make independent)",
            icon='UNLINKED',
        )


def draw_assign_object_part_context_menu(self, context):
    """Append Assign Object Part to the object right-click menu."""
    if context.mode != 'OBJECT':
        return
    # Show when at least 2 objects are selected (source + target)
    if len(context.selected_objects) < 2:
        return
    if context.active_object is None:
        return

    layout = self.layout
    layout.separator()
    layout.menu(CGML3_MT_AssignObjectPartContext.bl_idname, icon='LINKED')
