# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""Shared LoD helpers for semantic editing tools."""

from __future__ import annotations


OPENING_SURFACE_TYPES = {"Window", "Door", "WindowSurface", "DoorSurface"}


def read_lod_value(value):
    """Return a normalized integer LoD value, or None if it is not valid."""
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    if text.lower().startswith("lod"):
        text = text[3:].strip()
    try:
        lod = int(text)
    except Exception:
        return None
    return lod if lod in {0, 1, 2, 3, 4} else None


def _safe_get(id_block, key, default=None):
    try:
        return id_block.get(key, default)
    except Exception:
        return default


def _safe_set(id_block, key, value) -> bool:
    try:
        id_block[key] = value
        return True
    except Exception:
        return False


def material_surface_type(mat) -> str:
    if mat is None:
        return ""
    value = (
        _safe_get(mat, "SurfaceTyp")
        or _safe_get(mat, "surface_type")
        or _safe_get(mat, "Typ")
        or ""
    )
    try:
        return str(value).strip()
    except Exception:
        return ""


def material_is_opening(mat) -> bool:
    surface_type = material_surface_type(mat)
    if surface_type in OPENING_SURFACE_TYPES:
        return True
    opening_type = _safe_get(mat, "OpeningType") or _safe_get(mat, "opening_type")
    return str(opening_type or "").strip() in {"Window", "Door"}


def material_lod(mat):
    if mat is None:
        return None
    return read_lod_value(_safe_get(mat, "lod"))


def iter_mesh_materials(obj):
    try:
        materials = getattr(getattr(obj, "data", None), "materials", None)
    except Exception:
        materials = None
    if not materials:
        return []
    return [mat for mat in materials if mat is not None]


def _has_citygml_identity(obj) -> bool:
    if obj is None:
        return False
    if str(_safe_get(obj, "structure_type", "") or "") == "hierarchical":
        return True
    for key in ("cgml3_feature", "feature_type", "ModelType", "gml_id"):
        value = _safe_get(obj, key)
        if value is not None and str(value).strip():
            return True
    return False


def _lod_target_objects(obj):
    targets = []
    current = obj
    while current is not None:
        if current is obj or _has_citygml_identity(current):
            targets.append(current)
        try:
            current = current.parent
        except Exception:
            current = None

    unique = []
    seen = set()
    for target in targets:
        key = id(target)
        if key in seen:
            continue
        seen.add(key)
        unique.append(target)
    return unique


def _mesh_receivers(obj):
    if obj is None:
        return []
    try:
        obj_type = getattr(obj, "type", None)
    except Exception:
        obj_type = None
    if obj_type == "MESH":
        return [obj]

    receivers = []
    stack = list(getattr(obj, "children", []) or [])
    while stack:
        child = stack.pop(0)
        if getattr(child, "type", None) == "MESH":
            receivers.append(child)
        stack.extend(list(getattr(child, "children", []) or []))
    return receivers


def _descendants(obj):
    if obj is None:
        return []
    result = []
    stack = list(getattr(obj, "children", []) or [])
    while stack:
        child = stack.pop(0)
        result.append(child)
        stack.extend(list(getattr(child, "children", []) or []))
    return result


def _object_lod(obj):
    for key in ("lod", "cgml3_lod"):
        lod = read_lod_value(_safe_get(obj, key))
        if lod is not None:
            return lod
    return None


def _infer_lod_from_materials(mesh_objects):
    lod_counts = {}
    for mesh_obj in mesh_objects or []:
        for mat in iter_mesh_materials(mesh_obj):
            lod = material_lod(mat)
            if lod is None:
                continue
            lod_counts[lod] = lod_counts.get(lod, 0) + 1
    if not lod_counts:
        return None
    return max(lod_counts.items(), key=lambda item: (item[1], item[0]))[0]


def _effective_lod(target_objects, mesh_objects):
    for target in target_objects or []:
        lod = _object_lod(target)
        if lod is not None:
            return lod
    return _infer_lod_from_materials(mesh_objects)


def opening_lod_for_version(obj=None, citygml_version="2.0", citygml2_lod=3, citygml3_min_lod=2) -> int:
    """
    Return the LoD that newly created opening surfaces should receive.

    CityGML 2 openings require LoD3 geometry. CityGML 3 openings should keep the
    current top-level feature LoD and only raise LoD0/1 data to LoD2.
    """
    version = str(citygml_version or "").strip()
    if version.startswith("3"):
        root = _top_level_root(obj)
        values = _all_lod_values_for_top_level(root) if root is not None else []
        current_lod = max(values) if values else None
        minimum_lod = int(citygml3_min_lod)
        return max(current_lod if current_lod is not None else minimum_lod, minimum_lod)

    return int(citygml2_lod)


def _top_level_root(obj):
    targets = _lod_target_objects(obj)
    return targets[-1] if targets else obj


def _top_level_objects(obj):
    root = _top_level_root(obj)
    if root is None:
        return []
    return [root] + _descendants(root)


def _top_level_mesh_objects(obj):
    objects = _top_level_objects(obj)
    return [candidate for candidate in objects if getattr(candidate, "type", None) == "MESH"]


def _all_lod_values_for_top_level(obj):
    values = []
    for candidate in _top_level_objects(obj):
        lod = _object_lod(candidate)
        if lod is not None:
            values.append(lod)
    for mesh_obj in _top_level_mesh_objects(obj):
        for mat in iter_mesh_materials(mesh_obj):
            lod = material_lod(mat)
            if lod is not None:
                values.append(lod)
    return values


def top_level_has_mixed_lods(obj) -> bool:
    values = set(_all_lod_values_for_top_level(obj))
    return len(values) > 1


def normalize_top_level_lod(obj, minimum_lod=None) -> dict:
    """Normalize all objects and surfaces of a top-level feature to one LoD."""
    root = _top_level_root(obj)
    if root is None:
        return {"changed": False, "lod": None, "root": None, "mixed": False}

    all_values = _all_lod_values_for_top_level(root)
    target_lod = max(all_values) if all_values else None
    if minimum_lod is not None:
        minimum_lod = int(minimum_lod)
        target_lod = minimum_lod if target_lod is None else max(target_lod, minimum_lod)

    if target_lod is None:
        return {"changed": False, "lod": None, "root": root, "mixed": False}

    mixed = len(set(all_values)) > 1
    changed = False
    for candidate in _top_level_objects(root):
        changed = _set_object_lod_for_opening(candidate, target_lod) or changed
    for mesh_obj in _top_level_mesh_objects(root):
        for mat in iter_mesh_materials(mesh_obj):
            changed = set_material_lod_for_opening(mat, target_lod) or changed

    return {"changed": changed, "lod": target_lod, "root": root, "mixed": mixed}


def normalize_scene_top_level_lods(scene, minimum_lod=None) -> dict:
    """Normalize mixed LoDs across all detected top-level features in a scene."""
    try:
        objects = list(getattr(scene, "objects", []) or [])
    except Exception:
        objects = []

    seen_roots = set()
    normalized = []
    changed = 0

    for obj in objects:
        root = _top_level_root(obj)
        if root is None:
            continue
        key = id(root)
        if key in seen_roots:
            continue
        seen_roots.add(key)

        before_mixed = top_level_has_mixed_lods(root)
        result = normalize_top_level_lod(root, minimum_lod=minimum_lod)
        if result["changed"]:
            changed += 1
        if before_mixed or result["changed"]:
            normalized.append({
                "name": getattr(root, "name", ""),
                "lod": result["lod"],
                "changed": result["changed"],
                "mixed": before_mixed,
            })

    return {"changed_count": changed, "normalized": normalized}


def _set_object_lod_for_opening(obj, lod_value) -> bool:
    changed = False
    current_lod = read_lod_value(_safe_get(obj, "lod"))
    if current_lod is None or current_lod < lod_value:
        changed = _safe_set(obj, "lod", int(lod_value)) or changed

    current_cgml_lod = read_lod_value(_safe_get(obj, "cgml3_lod"))
    if current_cgml_lod is not None and current_cgml_lod < lod_value:
        changed = _safe_set(obj, "cgml3_lod", int(lod_value)) or changed
    return changed


def set_material_lod_for_opening(mat, lod_value=3) -> bool:
    if mat is None:
        return False
    lod = material_lod(mat)
    if lod is not None and lod >= lod_value:
        return False
    return _safe_set(mat, "lod", int(lod_value))


def promote_opening_host_lod(obj, opening_lod=3) -> bool:
    """
    Promote an object edited with openings to at least the requested LoD.

    CityGML 2 export reads the top-level object custom property ``lod`` for the
    feature geometry tag, while both writers also use per-surface material LoD.
    This helper updates both places for the edited mesh and its CityGML parents.
    Existing higher-LoD objects are left unchanged.
    """
    if obj is None:
        return False

    target_lod = int(opening_lod)
    root = _top_level_root(obj)
    mesh_objects = _top_level_mesh_objects(root)
    target_objects = _top_level_objects(root)
    effective_lod = _effective_lod(target_objects, mesh_objects)

    if effective_lod is not None and effective_lod > target_lod:
        return False

    result = normalize_top_level_lod(root, minimum_lod=target_lod)
    return bool(result["changed"])
