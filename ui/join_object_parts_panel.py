# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Join Object Parts UI Panel

Provides a dedicated GUI tab for the existing Join Object Parts tool.
"""

import bpy
from bpy.types import Panel


class CGML3_PT_JoinObjectParts(Panel):
    """GUI panel for joining material-less object parts into target meshes"""

    bl_label = "Join Object Parts"
    bl_idname = "CGML3_PT_JoinObjectParts"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'CityGML'
    bl_order = 22
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout

        # --- Join Object Parts (existing) ---
        box1 = layout.box()
        box1.label(text="Join Object Parts", icon='AUTOMERGE_ON')
        box1.label(text="Merges geometry into touching target.")
        box1.label(text="Requires: mesh parts without materials")
        box1.label(text="that physically touch a target mesh.")
        col1 = box1.column(align=True)
        col1.label(text="RMB > Join Object Parts", icon='MOUSE_RMB')

        layout.separator()

        # --- Assign Object Part (new) ---
        box2 = layout.box()
        box2.label(text="Assign Object Part", icon='LINKED')
        box2.label(text="Semantic assignment (no geometry merge).")
        box2.label(text="Creates parent-child hierarchy for export.")
        box2.label(text="No touching required.")
        col2 = box2.column(align=True)
        col2.label(text="1. Select source object(s).")
        col2.label(text="2. Shift+select target (active).")
        col2.label(text="3. RMB > Assign Object Part.", icon='MOUSE_RMB')

        layout.separator()

        # --- Detach ---
        box3 = layout.box()
        box3.label(text="Detach Part", icon='UNLINKED')
        box3.label(text="Removes parent-child relationship.")
        box3.label(text="Promotes BuildingPart to Building.")


def register():
    bpy.utils.register_class(CGML3_PT_JoinObjectParts)


def unregister():
    bpy.utils.unregister_class(CGML3_PT_JoinObjectParts)
