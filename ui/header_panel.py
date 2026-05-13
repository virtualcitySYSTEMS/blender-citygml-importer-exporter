import os
import bpy
import bpy.utils.previews

# Global icon preview collection
_preview_collections = {}


def _get_vcs_icon():
    """Get the VCS logo icon id, registering on first call."""
    pcoll = _preview_collections.get("vcs_icons")
    if pcoll is None:
        pcoll = bpy.utils.previews.new()
        _preview_collections["vcs_icons"] = pcoll
    if "vcs_logo" not in pcoll:
        icon_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icons", "vcs_20jahre.png")
        if os.path.isfile(icon_path):
            pcoll.load("vcs_logo", icon_path, 'IMAGE')
        else:
            return 0
    return pcoll["vcs_logo"].icon_id


class VCS3DCityDB_PT_HeaderPanel(bpy.types.Panel):
    bl_label = "VCS"
    bl_idname = "VCS3DCITYDB_PT_header"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'CityGML'
    bl_order = 0
    bl_options = {'HIDE_HEADER'}

    def draw(self, context):
        layout = self.layout
        icon_id = _get_vcs_icon()

        col = layout.column(align=True)
        if icon_id:
            col.template_icon(icon_value=icon_id, scale=15.0)
            col.operator('wm.url_open', text="vc.systems", icon='URL').url = "https://vc.systems/en/"
        else:
            col.operator('wm.url_open', text="by Virtual City Systems", icon='URL').url = "https://vc.systems/en/"


def register():
    bpy.utils.register_class(VCS3DCityDB_PT_HeaderPanel)


def unregister():
    bpy.utils.unregister_class(VCS3DCityDB_PT_HeaderPanel)
    for pcoll in _preview_collections.values():
        bpy.utils.previews.remove(pcoll)
    _preview_collections.clear()
