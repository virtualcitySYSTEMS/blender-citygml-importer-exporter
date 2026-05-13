# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
from __future__ import annotations

import json
from typing import Any, Dict

import bpy
from bpy.types import Operator, Panel
from bpy.props import StringProperty, EnumProperty


def _load_dict_from_idprop(owner, dict_key: str, json_key: str) -> Dict[str, Any]:
    try:
        d = owner.get(dict_key)
        if isinstance(d, dict):
            return dict(d)
    except Exception:
        pass
    try:
        s = owner.get(json_key)
        if isinstance(s, str) and s.strip():
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        pass
    return {}

def _derive_from_custom_properties(owner, *, target: str) -> Dict[str, Any]:
    """
    Fallback: derive a "best effort" generic-attribute dict from existing IDProperties.
    This is useful when the importer hasn't populated cgml3_*_generic_attributes yet.
    """
    data: Dict[str, Any] = {}
    if owner is None:
        return data

    # keep in sync with exporter ignore lists
    internal = {
        "cgml3_feature",
        "gml_id",
        "cgml3_implicit_template_id",
        "cgml3_generic_attributes",
        "cgml3_generic_attributes_json",
        "cgml3_surface_generic_attributes",
        "cgml3_surface_generic_attributes_json",
    }
    material_internal = {
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
        "face_index",
        "gml_surface_id",
        "multisurface_id",
        "source_material",
        "surface_id",
        "cgml3_uv_start_by_ring",
    }

    for k, v in getattr(owner, "items", lambda: [])():
        if not isinstance(k, str):
            continue
        if not k or k in internal:
            continue
        if k.startswith("_"):
            # still show (e.g. _lod)
            pass
        if target == "MATERIAL" and k in material_internal:
            continue
        # Hide blender UI helper keys we introduced
        if k.endswith("_attr_filter") or k.endswith("_generic_attr_filter"):
            continue
        # Skip scene/runtime keys (including generic attributes themselves to avoid circular copy)
        if k.startswith("cgml3_"):
            continue
        try:
            data[k] = v
        except Exception:
            data[k] = str(v)
    return data


def _save_dict_to_idprop(owner, dict_key: str, json_key: str, data: Dict[str, Any]) -> None:
    # Store both: dict (for exporter) + json (for UI visibility)
    try:
        owner[dict_key] = dict(data)
    except Exception:
        # If dict assignment fails, fall back to json only
        pass
    try:
        owner[json_key] = json.dumps(data, ensure_ascii=False, sort_keys=True)
    except Exception:
        owner[json_key] = str(data)


class CGML3_OT_GenericAttrAdd(Operator):
    bl_idname = "cgml3.generic_attr_add"
    bl_label = "Add Generic Attribute"
    bl_options = {"UNDO"}

    target: EnumProperty(
        name="Target",
        items=(
            ("OBJECT", "Object", ""),
            ("MATERIAL", "Material", ""),
        ),
        default="OBJECT",
    )

    key: StringProperty(name="Name", default="")
    value: StringProperty(name="Value", default="")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        owner = context.object if self.target == "OBJECT" else getattr(context, "material", None)
        if owner is None:
            self.report({"WARNING"}, "No target selected")
            return {"CANCELLED"}

        dict_key = "cgml3_generic_attributes" if self.target == "OBJECT" else "cgml3_surface_generic_attributes"
        json_key = dict_key + "_json"

        k = (self.key or "").strip()
        if not k:
            self.report({"WARNING"}, "Name is empty")
            return {"CANCELLED"}

        data = _load_dict_from_idprop(owner, dict_key, json_key)
        data[k] = self.value
        _save_dict_to_idprop(owner, dict_key, json_key, data)
        return {"FINISHED"}


class CGML3_OT_GenericAttrDelete(Operator):
    bl_idname = "cgml3.generic_attr_delete"
    bl_label = "Delete Generic Attribute"
    bl_options = {"UNDO"}

    target: EnumProperty(
        name="Target",
        items=(
            ("OBJECT", "Object", ""),
            ("MATERIAL", "Material", ""),
        ),
        default="OBJECT",
    )

    key: StringProperty(name="Name", default="")

    def execute(self, context):
        owner = context.object if self.target == "OBJECT" else getattr(context, "material", None)
        if owner is None:
            self.report({"WARNING"}, "No target selected")
            return {"CANCELLED"}

        dict_key = "cgml3_generic_attributes" if self.target == "OBJECT" else "cgml3_surface_generic_attributes"
        json_key = dict_key + "_json"

        data = _load_dict_from_idprop(owner, dict_key, json_key)
        if self.key in data:
            data.pop(self.key, None)
            _save_dict_to_idprop(owner, dict_key, json_key, data)
            return {"FINISHED"}
        self.report({"INFO"}, "Key not found")
        return {"CANCELLED"}


class CGML3_OT_GenericAttrEdit(Operator):
    bl_idname = "cgml3.generic_attr_edit"
    bl_label = "Edit Generic Attribute"
    bl_options = {"UNDO"}

    target: EnumProperty(
        name="Target",
        items=(
            ("OBJECT", "Object", ""),
            ("MATERIAL", "Material", ""),
        ),
        default="OBJECT",
    )

    key: StringProperty(name="Name", default="")
    value: StringProperty(name="Value", default="")

    def invoke(self, context, event):
        owner = context.object if self.target == "OBJECT" else getattr(context, "material", None)
        if owner is None:
            return {"CANCELLED"}
        dict_key = "cgml3_generic_attributes" if self.target == "OBJECT" else "cgml3_surface_generic_attributes"
        json_key = dict_key + "_json"
        data = _load_dict_from_idprop(owner, dict_key, json_key)
        if self.key in data and not self.value:
            self.value = str(data.get(self.key, ""))
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        owner = context.object if self.target == "OBJECT" else getattr(context, "material", None)
        if owner is None:
            self.report({"WARNING"}, "No target selected")
            return {"CANCELLED"}
        dict_key = "cgml3_generic_attributes" if self.target == "OBJECT" else "cgml3_surface_generic_attributes"
        json_key = dict_key + "_json"
        data = _load_dict_from_idprop(owner, dict_key, json_key)
        if self.key not in data:
            self.report({"INFO"}, "Key not found")
            return {"CANCELLED"}
        data[self.key] = self.value
        _save_dict_to_idprop(owner, dict_key, json_key, data)
        return {"FINISHED"}


class CGML3_OT_GenericAttrClearFilter(Operator):
    bl_idname = "cgml3.generic_attr_clear_filter"
    bl_label = "Clear Generic Attribute Filter"
    bl_options = {"UNDO"}

    target: EnumProperty(
        name="Target",
        items=(
            ("OBJECT", "Object", ""),
            ("MATERIAL", "Material", ""),
        ),
        default="OBJECT",
    )

    def execute(self, context):
        wm = context.window_manager
        if self.target == "OBJECT":
            wm.cgml3_generic_attr_filter = ""
        else:
            wm.cgml3_surface_generic_attr_filter = ""
        return {"FINISHED"}


class CGML3_OT_GenericAttrMigrateFromProps(Operator):
    bl_idname = "cgml3.generic_attr_migrate_from_props"
    bl_label = "Import Existing Custom Properties"
    bl_options = {"UNDO"}

    target: EnumProperty(
        name="Target",
        items=(
            ("OBJECT", "Object", ""),
            ("MATERIAL", "Material", ""),
        ),
        default="OBJECT",
    )

    def execute(self, context):
        owner = context.object if self.target == "OBJECT" else getattr(context, "material", None)
        if owner is None:
            return {"CANCELLED"}
        dict_key = "cgml3_generic_attributes" if self.target == "OBJECT" else "cgml3_surface_generic_attributes"
        json_key = dict_key + "_json"

        data = _derive_from_custom_properties(owner, target=self.target)
        if not data:
            self.report({"INFO"}, "No custom properties to import")
            return {"CANCELLED"}
        _save_dict_to_idprop(owner, dict_key, json_key, data)
        return {"FINISHED"}


def _draw_generic_table(layout, data: Dict[str, Any], target: str):
    # Filter input
    # IMPORTANT: Do not write to ID datablocks inside draw(); Blender forbids this in many contexts.
    wm = bpy.context.window_manager
    if target == "OBJECT":
        flt = getattr(wm, "cgml3_generic_attr_filter", "") or ""
    else:
        flt = getattr(wm, "cgml3_surface_generic_attr_filter", "") or ""

    header = layout.row(align=True)
    op = header.operator("cgml3.generic_attr_add", text="Add", icon="ADD")
    op.target = target
    mig = header.operator("cgml3.generic_attr_migrate_from_props", text="", icon="IMPORT")
    mig.target = target
    if target == "OBJECT":
        header.prop(wm, "cgml3_generic_attr_filter", text="", icon="VIEWZOOM")
    else:
        header.prop(wm, "cgml3_surface_generic_attr_filter", text="", icon="VIEWZOOM")
    if flt:
        clear = header.operator("cgml3.generic_attr_clear_filter", text="", icon="X")
        clear.target = target

    if not data:
        layout.label(text="(none)")
        return

    flt_l = flt.strip().lower()
    def _match(k: str, v: Any) -> bool:
        if not flt_l:
            return True
        try:
            return flt_l in str(k).lower() or flt_l in str(v).lower()
        except Exception:
            return False

    keys = [k for k in sorted(data.keys(), key=lambda s: s.lower()) if _match(k, data.get(k))]
    if not keys:
        layout.label(text="No matches")
        return

    for k in keys:
        v = data.get(k)
        r = layout.row(align=True)
        r.label(text=str(k))
        r.label(text=str(v))
        op_e = r.operator("cgml3.generic_attr_edit", text="", icon="GREASEPENCIL")
        op_e.target = target
        op_e.key = str(k)
        op_d = r.operator("cgml3.generic_attr_delete", text="", icon="TRASH")
        op_d.target = target
        op_d.key = str(k)


class CGML3_PT_ObjectGenericAttributes(Panel):
    bl_label = "Generic Attributes (Object)"
    bl_idname = "CGML3_PT_ObjectGenericAttributes"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    def draw(self, context):
        obj = context.object
        if obj is None:
            self.layout.label(text="No active object")
            return
        data = _load_dict_from_idprop(obj, "cgml3_generic_attributes", "cgml3_generic_attributes_json")
        if not data:
            data = _derive_from_custom_properties(obj, target="OBJECT")
        _draw_generic_table(self.layout, data, target="OBJECT")


class CGML3_PT_MaterialGenericAttributes(Panel):
    bl_label = "Generic Attributes (Surface/Material)"
    bl_idname = "CGML3_PT_MaterialGenericAttributes"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "material"

    def draw(self, context):
        mat = getattr(context, "material", None)
        if mat is None:
            self.layout.label(text="No active material")
            return
        data = _load_dict_from_idprop(mat, "cgml3_surface_generic_attributes", "cgml3_surface_generic_attributes_json")
        if not data:
            data = _derive_from_custom_properties(mat, target="MATERIAL")
        _draw_generic_table(self.layout, data, target="MATERIAL")


CLASSES = (
    CGML3_OT_GenericAttrAdd,
    CGML3_OT_GenericAttrEdit,
    CGML3_OT_GenericAttrDelete,
    CGML3_OT_GenericAttrClearFilter,
    CGML3_OT_GenericAttrMigrateFromProps,
    CGML3_PT_ObjectGenericAttributes,
    CGML3_PT_MaterialGenericAttributes,
)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    # UI-only filter state (window-manager level)
    bpy.types.WindowManager.cgml3_generic_attr_filter = StringProperty(
        name="Generic Attribute Filter",
        default="",
        description="Filter generic attributes by name or value",
    )
    bpy.types.WindowManager.cgml3_surface_generic_attr_filter = StringProperty(
        name="Surface Generic Attribute Filter",
        default="",
        description="Filter surface/material generic attributes by name or value",
    )


def unregister():
    try:
        del bpy.types.WindowManager.cgml3_generic_attr_filter
    except Exception:
        pass
    try:
        del bpy.types.WindowManager.cgml3_surface_generic_attr_filter
    except Exception:
        pass
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
