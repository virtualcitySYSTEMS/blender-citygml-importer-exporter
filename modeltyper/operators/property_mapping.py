# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Operators for ModelTyper custom-property mapping.
"""

import bpy

from ..citygml import get_enum_items
from ..core import property_mapping as mapping_core


def _target_meshes(context, props):
    if props.assignment_mode == "SELECTION":
        return [obj for obj in context.selected_objects if obj.type == "MESH"], ""

    collection_name = props.collection_enum
    if collection_name not in bpy.data.collections:
        return [], "Collection not found"

    collection = bpy.data.collections[collection_name]
    return [obj for obj in collection.all_objects if obj.type == "MESH"], ""


class CGML3_MODELTYPER_OT_AddPropertyMapping(bpy.types.Operator):
    """Add a custom-property mapping row."""

    bl_idname = "cgml3_modeltyper.mapping_add"
    bl_label = "Add Mapping Row"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.cgml3_modeltyper_props
        row = props.property_mappings.add()
        row.name = f"Mapping {len(props.property_mappings)}"
        row.target_feature_type = props.feature_type

        lod = "4" if props.show_lod4_surfaces else "1-3"
        items = get_enum_items(row.target_feature_type, props.citygml_version, lod)
        if items:
            row.target_surface_type = items[0][0]

        props.property_mapping_index = len(props.property_mappings) - 1
        return {'FINISHED'}


class CGML3_MODELTYPER_OT_RemovePropertyMapping(bpy.types.Operator):
    """Remove the active custom-property mapping row."""

    bl_idname = "cgml3_modeltyper.mapping_remove"
    bl_label = "Remove Mapping Row"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.cgml3_modeltyper_props
        if not props.property_mappings:
            return {'CANCELLED'}

        idx = max(0, min(props.property_mapping_index, len(props.property_mappings) - 1))
        props.property_mappings.remove(idx)
        props.property_mapping_index = min(idx, max(0, len(props.property_mappings) - 1))
        return {'FINISHED'}


class CGML3_MODELTYPER_OT_MovePropertyMapping(bpy.types.Operator):
    """Move the active custom-property mapping row."""

    bl_idname = "cgml3_modeltyper.mapping_move"
    bl_label = "Move Mapping Row"
    bl_options = {'REGISTER', 'UNDO'}

    direction: bpy.props.EnumProperty(
        items=[
            ("UP", "Up", "Move the mapping row up"),
            ("DOWN", "Down", "Move the mapping row down"),
        ],
        default="UP",
    )

    def execute(self, context):
        props = context.scene.cgml3_modeltyper_props
        count = len(props.property_mappings)
        if count < 2:
            return {'CANCELLED'}

        idx = max(0, min(props.property_mapping_index, count - 1))
        new_idx = idx - 1 if self.direction == "UP" else idx + 1
        if new_idx < 0 or new_idx >= count:
            return {'CANCELLED'}

        props.property_mappings.move(idx, new_idx)
        props.property_mapping_index = new_idx
        return {'FINISHED'}


class CGML3_MODELTYPER_OT_ApplyPropertyMapping(bpy.types.Operator):
    """Apply custom-property mappings to selected objects or a collection."""

    bl_idname = "cgml3_modeltyper.apply_property_mapping"
    bl_label = "Apply Property Mapping"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != "OBJECT":
            self.report({'ERROR'}, "Please switch to Object Mode")
            return {'CANCELLED'}

        props = context.scene.cgml3_modeltyper_props
        rows = [row for row in props.property_mappings if row.enabled]
        rows = [row for row in rows if row.object_property.strip() or row.material_property.strip()]
        if not rows:
            self.report({'WARNING'}, "No enabled mapping rows with source properties")
            return {'CANCELLED'}

        meshes, error = _target_meshes(context, props)
        if error:
            self.report({'WARNING'}, error)
            return {'CANCELLED'}
        if not meshes:
            self.report({'WARNING'}, "No mesh objects to process")
            return {'CANCELLED'}

        totals = {
            "matched": 0,
            "unmatched": 0,
            "skipped_existing": 0,
            "invalid_targets": 0,
            "warnings": [],
        }

        for obj in meshes:
            result = mapping_core.apply_mappings_to_object(
                obj,
                rows,
                props.citygml_version,
                overwrite_existing_semantics=props.mapping_overwrite_existing_semantics,
                cleanup_unused_materials=props.mapping_cleanup_unused_materials,
            )
            for key in ("matched", "unmatched", "skipped_existing", "invalid_targets"):
                totals[key] += result.get(key, 0)
            totals["warnings"].extend(result.get("warnings", []))

        for warning in totals["warnings"][:10]:
            print(f"ModelTyper Mapping Warning: {warning}")
        if len(totals["warnings"]) > 10:
            print(f"ModelTyper Mapping Warning: ... and {len(totals['warnings']) - 10} more")

        if totals["matched"] == 0:
            self.report({'WARNING'}, "No faces matched the property mappings")
            return {'CANCELLED'}

        message = (
            f"Mapped {totals['matched']} face(s) on {len(meshes)} object(s); "
            f"{totals['unmatched']} unmatched"
        )
        if totals["skipped_existing"]:
            message += f", {totals['skipped_existing']} skipped"
        if totals["invalid_targets"]:
            message += f", {totals['invalid_targets']} invalid targets"
        self.report({'INFO'}, message)
        return {'FINISHED'}


classes = (
    CGML3_MODELTYPER_OT_AddPropertyMapping,
    CGML3_MODELTYPER_OT_RemovePropertyMapping,
    CGML3_MODELTYPER_OT_MovePropertyMapping,
    CGML3_MODELTYPER_OT_ApplyPropertyMapping,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
