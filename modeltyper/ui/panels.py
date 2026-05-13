# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
UI panels for the integrated CityGML ModelTyper tools.
"""

import bpy


class CGML3_MODELTYPER_UL_PropertyMappings(bpy.types.UIList):
    """List UI for ModelTyper custom-property mappings."""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "enabled", text="")

            source_parts = []
            if item.object_property:
                source = f"Obj {item.object_property}"
                if item.object_value:
                    source += f"={item.object_value}"
                source_parts.append(source)
            if item.material_property:
                source = f"Mat {item.material_property}"
                if item.material_value:
                    source += f"={item.material_value}"
                source_parts.append(source)

            source_label = " + ".join(source_parts) if source_parts else (item.name or "Mapping")
            target_label = f"{item.target_feature_type}/{item.target_surface_type}"
            row.label(text=f"{source_label} -> {target_label}", icon='MATERIAL')
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='MATERIAL')


class CGML3_MODELTYPER_PT_MainPanel(bpy.types.Panel):
    """Main panel for ModelTyper tools."""

    bl_label = "ModelTyper"
    bl_idname = "CGML3_MODELTYPER_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "CityGML"
    bl_order = 23
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        props = getattr(context.scene, "cgml3_modeltyper_props", None)
        if props is None:
            layout.label(text="ModelTyper properties are not available.", icon='ERROR')
            return

        box = layout.box()
        box.label(text="CityGML Surface Type Assignment", icon='MOD_BUILD')

        col = layout.column(align=True)
        col.label(text="Configuration:", icon='SETTINGS')
        col.prop(props, "feature_type", text="")
        col.prop(props, "citygml_version", text="")
        col.prop(props, "show_lod4_surfaces")

        layout.separator()

        box = layout.box()
        box.label(text="Automatic Assignment", icon='AUTO')
        col = box.column(align=True)
        col.prop(props, "assignment_mode", text="")
        if props.assignment_mode == "COLLECTION":
            col.prop(props, "collection_enum", text="Collection")
        row = col.row(align=True)
        row.operator("cgml3_modeltyper.auto_assign", icon='PLAY')
        row.prop(props, "preserve_existing_colors", text="", icon='MATERIAL')

        info = box.column(align=True)
        info.scale_y = 0.8
        info.label(text="Analyzes geometry and assigns", icon='INFO')
        if props.assignment_mode == "SELECTION":
            info.label(text="surface types to selected objects")
        else:
            info.label(text="surface types to the collection")

        layout.separator()

        box = layout.box()
        box.label(text="Custom Property Mapping", icon='MATERIAL')

        row = box.row()
        row.template_list(
            "CGML3_MODELTYPER_UL_PropertyMappings",
            "",
            props,
            "property_mappings",
            props,
            "property_mapping_index",
            rows=3,
        )
        buttons = row.column(align=True)
        buttons.operator("cgml3_modeltyper.mapping_add", text="", icon='ADD')
        buttons.operator("cgml3_modeltyper.mapping_remove", text="", icon='REMOVE')
        move_up = buttons.operator("cgml3_modeltyper.mapping_move", text="", icon='TRIA_UP')
        move_up.direction = "UP"
        move_down = buttons.operator("cgml3_modeltyper.mapping_move", text="", icon='TRIA_DOWN')
        move_down.direction = "DOWN"

        if props.property_mappings:
            idx = max(0, min(props.property_mapping_index, len(props.property_mappings) - 1))
            item = props.property_mappings[idx]
            detail = box.column(align=True)
            detail.prop(item, "name", text="Name")
            detail.prop(item, "enabled")

            detail.label(text="Source Custom Properties:", icon='SETTINGS')
            split = detail.split(factor=0.5)
            obj_col = split.column(align=True)
            obj_col.prop(item, "object_property", text="Object")
            obj_col.prop(item, "object_value", text="Value")
            mat_col = split.column(align=True)
            mat_col.prop(item, "material_property", text="Material")
            mat_col.prop(item, "material_value", text="Value")

            row = detail.row(align=True)
            row.prop(item, "match_mode", text="")
            row.prop(item, "case_sensitive", text="Case")

            detail.label(text="Target CityGML Semantics:", icon='MOD_BUILD')
            detail.prop(item, "target_feature_type", text="Feature Type")
            detail.prop(item, "target_surface_type", text="Surface Type")
            detail.prop(item, "copy_source_material")

        opts = box.column(align=True)
        opts.prop(props, "mapping_overwrite_existing_semantics")
        opts.prop(props, "mapping_cleanup_unused_materials")
        opts.operator("cgml3_modeltyper.apply_property_mapping", icon='FILE_REFRESH')

        info = box.column(align=True)
        info.scale_y = 0.8
        info.label(text="First enabled matching row wins", icon='INFO')
        info.label(text="Creates CityGML material semantics per matched face")

        layout.separator()

        box = layout.box()
        box.label(text="Manual Assignment (Edit Mode)", icon='EDITMODE_HLT')
        col = box.column(align=True)
        col.prop(props, "category_enum", text="Surface Type")
        row = col.row(align=True)
        row.operator("cgml3_modeltyper.manual_assign", icon='FACESEL')
        row.prop(props, "preserve_existing_colors_manual", text="", icon='MATERIAL')
        # Show interior surface toggle only for opening types
        if props.category_enum in ("Window", "Door"):
            col.prop(props, "generate_interior_surface", icon='MOD_SOLIDIFY')

        info = box.column(align=True)
        info.scale_y = 0.8
        info.label(text="Select faces in Edit Mode", icon='INFO')
        info.label(text="and assign a surface type manually")

        layout.separator()

        box = layout.box()
        box.label(text="Tools", icon='TOOL_SETTINGS')
        col = box.column(align=True)
        col.operator("cgml3_modeltyper.analyze_model", icon='VIEWZOOM')


class CGML3_MODELTYPER_PT_HelpPanel(bpy.types.Panel):
    """Help panel with surface type information."""

    bl_label = "Surface Types Info"
    bl_idname = "CGML3_MODELTYPER_PT_help_panel"
    bl_parent_id = "CGML3_MODELTYPER_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "CityGML"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        props = getattr(context.scene, "cgml3_modeltyper_props", None)
        if props is None:
            layout.label(text="ModelTyper properties are not available.", icon='ERROR')
            return

        from ..citygml import get_surface_types

        surfaces = get_surface_types(
            props.feature_type,
            props.citygml_version,
            "4" if props.show_lod4_surfaces else "1-3",
        )

        col = layout.column(align=True)
        col.label(text=f"{props.feature_type} Surface Types:")

        for name, info in sorted(surfaces.items()):
            box = col.box()
            row = box.row()
            row.label(text="", icon='COLORSET_01_VEC')
            row.label(text=name)
            box.label(text=info['description'], icon='INFO')
            box.label(text=f"LOD: {info['lod']} | {info['geometry']}")


def register():
    bpy.utils.register_class(CGML3_MODELTYPER_UL_PropertyMappings)
    bpy.utils.register_class(CGML3_MODELTYPER_PT_MainPanel)
    bpy.utils.register_class(CGML3_MODELTYPER_PT_HelpPanel)


def unregister():
    try:
        bpy.utils.unregister_class(CGML3_MODELTYPER_PT_HelpPanel)
        bpy.utils.unregister_class(CGML3_MODELTYPER_PT_MainPanel)
        bpy.utils.unregister_class(CGML3_MODELTYPER_UL_PropertyMappings)
    except RuntimeError:
        pass
