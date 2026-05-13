# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Shared export helper functions used by both File-Export and DB-Export operators.
Eliminates duplication between CGML3_OT_ExportGMLFile and CGML3_OT_ExportToDB.
"""
import datetime
import bpy


def log_text_to_blender(name_prefix: str, content: str):
    """Write log content to a Blender text block."""
    name = f"{name_prefix}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    tb = bpy.data.texts.get(name) or bpy.data.texts.new(name)
    tb.clear()
    tb.write(content[:2_000_000])


def object_is_viewport_visible(obj, ctx=None) -> bool:
    """Return False for objects hidden in the Outliner/current viewport."""
    if obj is None:
        return False

    view_layer = None
    try:
        view_layer = getattr(ctx, "view_layer", None)
    except Exception:
        view_layer = None
    if view_layer is None:
        try:
            view_layer = bpy.context.view_layer
        except Exception:
            view_layer = None

    try:
        if bool(getattr(obj, "hide_viewport", False)):
            return False
    except Exception:
        pass

    try:
        if view_layer is not None:
            if obj.hide_get(view_layer=view_layer):
                return False
        elif obj.hide_get():
            return False
    except TypeError:
        try:
            if obj.hide_get():
                return False
        except Exception:
            pass
    except Exception:
        pass

    try:
        if view_layer is not None:
            return bool(obj.visible_get(view_layer=view_layer))
        return bool(obj.visible_get())
    except TypeError:
        try:
            return bool(obj.visible_get())
        except Exception:
            return True
    except Exception:
        return True


def scene_has_opening_surfaces(ctx) -> bool:
    """Check if the scene/selection contains materials with SurfaceTyp=='Opening'."""
    objs = ctx.selected_objects or ctx.scene.objects
    for obj in objs:
        if getattr(obj, "type", None) != "MESH":
            continue
        if not object_is_viewport_visible(obj, ctx):
            continue
        for slot in getattr(obj, "material_slots", []):
            mat = getattr(slot, "material", None)
            if not mat:
                continue
            try:
                surf_type = mat.get("SurfaceTyp") or mat.get("surface_type")
            except Exception:
                surf_type = None
            if str(surf_type or "").strip() == "Opening":
                return True
    return False


def scene_has_building_subdivision_features(ctx) -> bool:
    """Check if the scene contains Storey or BuildingRoom features."""
    for obj in ctx.scene.objects:
        if not object_is_viewport_visible(obj, ctx):
            continue
        try:
            feat = str(obj.get("cgml3_feature") or obj.get("feature_type") or "").strip()
        except Exception:
            feat = ""
        if feat in {"Storey", "BuildingRoom"}:
            return True
    return False

