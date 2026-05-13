# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Custom-property mapping for ModelTyper.

Maps object and material custom properties from non-CityGML imports to
CityGML feature and surface semantics stored on newly created materials.
"""

import re
import uuid
from datetime import datetime

import bmesh

from ..citygml import get_surface_color, get_surface_types
from . import materials


_OPENING_ALIASES = {
    "Window": "Window",
    "WindowSurface": "Window",
    "Door": "Door",
    "DoorSurface": "Door",
}

_OPENING_PROPS = {
    "OpeningType",
    "opening_type",
    "is_opening",
    "con_opening_id",
    "opening_id",
    "opening_gml_id",
    "CityGMLTarget",
    "filling_parent_surface_id",
    "opening_surface_id",
}

_SEMANTIC_PROPS = {
    "SurfaceTyp",
    "surface_type",
    "Typ",
    "FeatureType",
    "CityGMLFeatureType",
    "ModelType",
    "CityGMLComplexType",
    "gml_polygon_id",
    "gml_ring_id",
    "con_surface_id",
    "gml_multisurface_id",
    "lod",
    *_OPENING_PROPS,
}


def _stringify(value):
    """Convert Blender custom property values into stable comparison text."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(_stringify(v) for v in value)
    try:
        # IDPropertyArray behaves like a sequence but is not always a list.
        if hasattr(value, "__iter__") and not isinstance(value, (str, bytes, dict)):
            return ",".join(_stringify(v) for v in value)
    except Exception:
        pass
    return str(value)


def _get_custom_prop(owner, prop_name, case_sensitive=False):
    """Return a custom property value by name, optionally case-insensitive."""
    prop_name = str(prop_name or "").strip()
    if not owner or not prop_name:
        return None, False

    try:
        if prop_name in owner:
            return owner[prop_name], True
    except Exception:
        return None, False

    if case_sensitive:
        return None, False

    lookup = prop_name.lower()
    try:
        for key in owner.keys():
            if str(key).lower() == lookup:
                return owner[key], True
    except Exception:
        pass
    return None, False


def _value_matches(actual, expected, mode, case_sensitive):
    expected = str(expected or "").strip()
    if not expected or expected == "*":
        return True

    actual_text = _stringify(actual).strip()
    if not case_sensitive:
        actual_cmp = actual_text.lower()
        expected_cmp = expected.lower()
    else:
        actual_cmp = actual_text
        expected_cmp = expected

    if mode == "CONTAINS":
        return expected_cmp in actual_cmp

    if mode == "REGEX":
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            return re.search(expected, actual_text, flags) is not None
        except re.error:
            return False

    return actual_cmp == expected_cmp


def _criterion_matches(owner, prop_name, expected_value, mode, case_sensitive):
    prop_name = str(prop_name or "").strip()
    if not prop_name:
        return True

    actual, found = _get_custom_prop(owner, prop_name, case_sensitive)
    if not found:
        return False
    return _value_matches(actual, expected_value, mode, case_sensitive)


def _row_has_source_criterion(row):
    return bool(
        str(getattr(row, "object_property", "") or "").strip()
        or str(getattr(row, "material_property", "") or "").strip()
    )


def _row_matches(row, obj, mat):
    if not getattr(row, "enabled", True):
        return False
    if not _row_has_source_criterion(row):
        return False

    mode = getattr(row, "match_mode", "EXACT")
    case_sensitive = bool(getattr(row, "case_sensitive", False))
    return (
        _criterion_matches(
            obj,
            getattr(row, "object_property", ""),
            getattr(row, "object_value", ""),
            mode,
            case_sensitive,
        )
        and _criterion_matches(
            mat,
            getattr(row, "material_property", ""),
            getattr(row, "material_value", ""),
            mode,
            case_sensitive,
        )
    )


def find_matching_row(rows, obj, mat):
    """Return the first enabled mapping row that matches an object/material."""
    for row in rows:
        if _row_matches(row, obj, mat):
            return row
    return None


def _has_citygml_semantics(mat):
    if mat is None:
        return False
    for key in _SEMANTIC_PROPS:
        try:
            value = mat.get(key)
        except Exception:
            continue
        if _stringify(value).strip():
            return True
    return False


def _remove_stale_semantics(mat):
    for key in _SEMANTIC_PROPS:
        try:
            if key in mat:
                del mat[key]
        except Exception:
            pass


def _set_base_color(mat, color):
    if mat is None:
        return
    mat.diffuse_color = color
    if not mat.use_nodes:
        return
    bsdf = mat.node_tree.nodes.get("Principled BSDF") if mat.node_tree else None
    if bsdf:
        try:
            bsdf.inputs["Base Color"].default_value = color
        except Exception:
            pass


def _create_semantic_material(row, source_mat, feature_type, surface_type, version):
    copy_source = bool(getattr(row, "copy_source_material", True))
    color = get_surface_color(surface_type, feature_type, version)

    if copy_source and source_mat is not None:
        mat = source_mat.copy()
        mat.name = f"{surface_type}_{uuid.uuid4().hex[:8]}"
        _remove_stale_semantics(mat)
    else:
        mat = materials.create_material(surface_type, color)

    mat["SurfaceTyp"] = surface_type
    mat["surface_type"] = surface_type
    mat["Typ"] = surface_type
    mat["FeatureType"] = feature_type
    mat["CityGMLFeatureType"] = feature_type
    mat["ModelTyperMapping"] = str(getattr(row, "name", "") or "").strip()
    mat["created_at"] = datetime.now().isoformat()
    mat["lod"] = 2

    poly_id = f"UUID_{uuid.uuid4().hex}"
    mat["gml_polygon_id"] = poly_id
    mat["gml_ring_id"] = f"{poly_id}_0_"
    mat["con_surface_id"] = f"ID_{uuid.uuid4().hex}"
    mat["gml_multisurface_id"] = f"ID_{uuid.uuid4().hex}"

    opening_category = _OPENING_ALIASES.get(surface_type)
    if opening_category:
        materials._set_opening_properties_on_material(mat, opening_category, version)
    else:
        for key in _OPENING_PROPS:
            try:
                if key in mat:
                    del mat[key]
            except Exception:
                pass

    if not copy_source:
        _set_base_color(mat, color)

    return mat


def _valid_surface_type(feature_type, surface_type, version):
    surfaces = get_surface_types(feature_type, version)
    return surface_type in surfaces


def _surface_types_for_object(obj):
    surface_types = set()
    if not obj or obj.type != "MESH":
        return surface_types
    for mat in obj.data.materials:
        if mat is None:
            continue
        surface_type = str(mat.get("SurfaceTyp", "") or mat.get("surface_type", "") or "").strip()
        if surface_type:
            surface_types.add(surface_type)
    return surface_types


def apply_mappings_to_object(
    obj,
    rows,
    version="2.0",
    *,
    overwrite_existing_semantics=True,
    cleanup_unused_materials=True,
):
    """Apply mapping rows to all faces of one mesh object."""
    result = {
        "object": obj.name if obj else "",
        "matched": 0,
        "unmatched": 0,
        "skipped_existing": 0,
        "invalid_targets": 0,
        "feature_counts": {},
        "warnings": [],
    }

    if not obj or obj.type != "MESH":
        return result

    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()

    feature_order = []

    for face in bm.faces:
        mat_idx = face.material_index
        source_mat = mesh.materials[mat_idx] if 0 <= mat_idx < len(mesh.materials) else None

        if not overwrite_existing_semantics and _has_citygml_semantics(source_mat):
            result["skipped_existing"] += 1
            continue

        row = find_matching_row(rows, obj, source_mat)
        if row is None:
            result["unmatched"] += 1
            continue

        feature_type = str(getattr(row, "target_feature_type", "") or "").strip() or "BUILDING"
        surface_type = str(getattr(row, "target_surface_type", "") or "").strip()
        if not _valid_surface_type(feature_type, surface_type, version):
            result["invalid_targets"] += 1
            result["warnings"].append(
                f"{obj.name}: invalid target {feature_type}/{surface_type}"
            )
            continue

        semantic_mat = _create_semantic_material(row, source_mat, feature_type, surface_type, version)
        mesh.materials.append(semantic_mat)
        face.material_index = len(mesh.materials) - 1

        result["matched"] += 1
        result["feature_counts"][feature_type] = result["feature_counts"].get(feature_type, 0) + 1
        if feature_type not in feature_order:
            feature_order.append(feature_type)

    bm.to_mesh(mesh)
    bm.free()

    if cleanup_unused_materials and result["matched"] > 0:
        materials.cleanup_unused_materials(obj)

    if feature_order:
        dominant_feature = max(feature_order, key=lambda item: result["feature_counts"].get(item, 0))
        materials.set_model_type(obj, dominant_feature)
        surface_types = _surface_types_for_object(obj)
        if surface_types:
            obj["surface_types"] = ", ".join(sorted(surface_types))
        if len(feature_order) > 1:
            result["warnings"].append(
                f"{obj.name}: multiple target feature types matched; ModelType set to {dominant_feature}"
            )

    return result


def register():
    pass


def unregister():
    pass
