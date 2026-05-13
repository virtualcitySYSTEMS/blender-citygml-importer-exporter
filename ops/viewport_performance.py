# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Viewport Performance Optimization for CityGML scenes.

When handling large CityGML imports with hundreds of objects, switching to
Material Preview or Rendered viewport shading can cause long freezes due to
shader compilation and texture loading.

This module provides operators to optimize viewport performance by:
- Setting non-selected objects to simplified display modes
- Optionally hiding non-selected objects completely
- Restoring original display settings
"""

import bpy
from bpy.types import Menu, Operator, Panel
from bpy.props import EnumProperty, BoolProperty


class CGML3_OT_OptimizeViewport(Operator):
    """Optimize viewport for material preview by simplifying non-selected objects"""
    bl_idname = "cgml3.optimize_viewport"
    bl_label = "Optimize Viewport for Selection"
    bl_description = "Set non-selected objects to bounds/wire display for faster material preview"
    bl_options = {'REGISTER', 'UNDO'}
    
    mode: EnumProperty(
        name="Display Mode",
        description="How to display non-selected objects",
        items=[
            ('BOUNDS', "Bounds", "Show only bounding boxes (fastest)", 'SHADING_BBOX', 0),
            ('WIRE', "Wire", "Show wireframe", 'SHADING_WIRE', 1),
            ('HIDE', "Hide", "Hide completely (use Local View)", 'HIDE_ON', 2),
        ],
        default='BOUNDS'
    )
    
    store_state: BoolProperty(
        name="Store Original State",
        description="Remember original display settings for restoration",
        default=True
    )
    
    def execute(self, context):
        scene = context.scene
        selected = [obj for obj in scene.objects if obj.select_get()]
        
        if not selected:
            self.report({'WARNING'}, "No objects selected")
            return {'CANCELLED'}
        
        # Store original display settings if requested (per-object to avoid 63-char IDProperty key limit)
        if self.store_state and not scene.get('cgml3_viewport_optimized', False):
            for obj in scene.objects:
                obj['cgml3_orig_display_type'] = obj.display_type
                obj['cgml3_orig_hide_viewport'] = obj.hide_viewport
            scene['cgml3_viewport_optimized'] = True
        
        count = 0
        if self.mode == 'HIDE':
            # Hide non-selected objects
            for obj in scene.objects:
                if obj not in selected:
                    obj.hide_viewport = True
                    count += 1
        else:
            # Set display mode for non-selected
            display_type = self.mode
            for obj in scene.objects:
                if obj not in selected:
                    obj.display_type = display_type
                    count += 1
        
        # Force viewport update
        context.view_layer.update()
        
        mode_name = "hidden" if self.mode == 'HIDE' else f"{self.mode.lower()} display"
        self.report({'INFO'}, f"{count} objects set to {mode_name}")
        
        return {'FINISHED'}


class CGML3_OT_RestoreViewport(Operator):
    """Restore original viewport display settings"""
    bl_idname = "cgml3.restore_viewport"
    bl_label = "Restore Viewport"
    bl_description = "Restore original display settings for all objects and switch to Solid view"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        scene = context.scene
        
        # Restore viewport state from per-object custom properties
        if scene.get('cgml3_viewport_optimized', False):
            count = 0
            
            for obj in scene.objects:
                if 'cgml3_orig_display_type' in obj:
                    obj.display_type = obj['cgml3_orig_display_type']
                    obj.hide_viewport = obj.get('cgml3_orig_hide_viewport', False)
                    del obj['cgml3_orig_display_type']
                    if 'cgml3_orig_hide_viewport' in obj:
                        del obj['cgml3_orig_hide_viewport']
                    count += 1
                else:
                    # Reset objects without stored state to defaults
                    obj.display_type = 'TEXTURED'
                    obj.hide_viewport = False
            
            # Clear scene-level flags
            del scene['cgml3_viewport_optimized']
            if 'cgml3_hidden_state' in scene:
                del scene['cgml3_hidden_state']
            
            self.report({'INFO'}, f"Restored display for {count} objects")
        
        # Exit local view if active
        in_local_view = False
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        if space.local_view:
                            in_local_view = True
                            with context.temp_override(area=area):
                                bpy.ops.view3d.localview()
                            break
        
        if in_local_view:
            self.report({'INFO'}, "Exited Local View")
        
        # Switch to Solid view
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.shading.type = 'SOLID'
                        break
        
        # Force viewport update
        context.view_layer.update()
        
        return {'FINISHED'}


class CGML3_OT_ToggleHideUnselected(Operator):
    """Toggle visibility of non-selected objects"""
    bl_idname = "cgml3.toggle_hide_unselected"
    bl_label = "Toggle Hide Unselected"
    bl_description = "Show/hide non-selected objects"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        scene = context.scene
        selected = [obj for obj in scene.objects if obj.select_get()]
        
        if not selected:
            self.report({'WARNING'}, "No objects selected")
            return {'CANCELLED'}
        
        # Check if we're currently in hidden state
        is_hidden = 'cgml3_hidden_state' in scene and scene['cgml3_hidden_state']
        
        if is_hidden:
            # Restore visibility from per-object properties
            if scene.get('cgml3_viewport_optimized', False):
                count = 0
                for obj in scene.objects:
                    if 'cgml3_orig_hide_viewport' in obj:
                        obj.hide_viewport = obj['cgml3_orig_hide_viewport']
                        del obj['cgml3_orig_display_type']
                        del obj['cgml3_orig_hide_viewport']
                        count += 1
                    else:
                        obj.hide_viewport = False
                
                # Clear state
                del scene['cgml3_viewport_optimized']
                del scene['cgml3_hidden_state']
                
                self.report({'INFO'}, f"{count} objects shown")
            else:
                self.report({'WARNING'}, "No hidden state found")
        else:
            # Hide non-selected
            # Store state first (only if not already stored)
            if not scene.get('cgml3_viewport_optimized', False):
                for obj in scene.objects:
                    obj['cgml3_orig_display_type'] = obj.display_type
                    obj['cgml3_orig_hide_viewport'] = obj.hide_viewport
                scene['cgml3_viewport_optimized'] = True
            
            count = 0
            for obj in scene.objects:
                if obj not in selected:
                    obj.hide_viewport = True
                    count += 1
            
            # Mark as hidden
            scene['cgml3_hidden_state'] = True
            
            self.report({'INFO'}, f"{count} objects hidden")
        
        # Force viewport update
        context.view_layer.update()
        
        return {'FINISHED'}


class CGML3_OT_MaterialPreviewSelection(Operator):
    """Switch to Material Preview with optimized display"""
    bl_idname = "cgml3.material_preview_selection"
    bl_label = "Material Preview (Selection Only)"
    bl_description = "Switch to material preview showing only selected objects with full materials"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # First optimize viewport
        bpy.ops.cgml3.optimize_viewport(mode='BOUNDS', store_state=True)
        
        # Then switch to material preview
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.shading.type = 'MATERIAL'
                        break
        
        self.report({'INFO'}, "Switched to Material Preview (optimized)")
        
        return {'FINISHED'}


class CGML3_OT_QuickLocalView(Operator):
    """Toggle Local View (Numpad /) with automatic material preview"""
    bl_idname = "cgml3.quick_local_view"
    bl_label = "Local View + Material Preview"
    bl_description = "Toggle local view for selection and switch to material preview"
    bl_options = {'REGISTER', 'UNDO'}
    
    switch_shading: BoolProperty(
        name="Switch to Material Preview",
        description="Automatically switch to material preview in local view",
        default=True
    )
    
    def execute(self, context):
        # Check if we have a 3D view
        area = None
        for a in context.screen.areas:
            if a.type == 'VIEW_3D':
                area = a
                break
        
        if not area:
            self.report({'WARNING'}, "No 3D View found")
            return {'CANCELLED'}
        
        # Toggle local view using temp_override (Blender 3.2+)
        with context.temp_override(area=area):
            bpy.ops.view3d.localview()
        
        # Switch to material preview if requested
        if self.switch_shading:
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    space.shading.type = 'MATERIAL'
                    break
        
        self.report({'INFO'}, "Local View toggled")
        
        return {'FINISHED'}


class CGML3_MT_MaterialPreviewContext(Menu):
    """Object context menu entry for material preview helpers"""
    bl_label = "Material Preview"
    bl_idname = "CGML3_MT_material_preview_context"

    def draw(self, context):
        layout = self.layout
        layout.operator(
            "cgml3.material_preview_selection",
            text="Material Preview (Selection Only)",
            icon='SHADING_RENDERED',
        )
        layout.operator(
            "cgml3.quick_local_view",
            text="Local View + Material Preview",
            icon='LOCKVIEW_ON',
        )


def draw_material_preview_context_menu(self, context):
    """Append material preview actions to the object right-click menu."""
    if context.mode != 'OBJECT':
        return
    if not context.selected_objects:
        return

    layout = self.layout
    layout.separator()
    layout.menu(CGML3_MT_MaterialPreviewContext.bl_idname, icon='SHADING_RENDERED')


class CGML3_PT_ViewportPerformance(Panel):
    """Panel for viewport performance optimization"""
    bl_label = "Viewport Performance"
    bl_idname = "CGML3_PT_ViewportPerformance"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "CityGML"
    bl_order = 21
    bl_options = {'DEFAULT_CLOSED'}
    
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        
        box = layout.box()
        box.label(text="Speed up material preview:", icon='SHADING_RENDERED')
        
        # Quick action buttons
        col = box.column(align=True)
        col.operator("cgml3.material_preview_selection", 
                     text="Material Preview (Selection Only)", 
                     icon='SHADING_RENDERED')
        col.operator("cgml3.quick_local_view", 
                     text="Local View + Material Preview", 
                     icon='LOCKVIEW_ON')
        
        box.separator()
        
        # Advanced options
        col = box.column(align=True)
        col.label(text="Manual Optimization:", icon='SETTINGS')
        
        row = col.row(align=True)
        op = row.operator("cgml3.optimize_viewport", text="Bounds", icon='SHADING_BBOX')
        op.mode = 'BOUNDS'
        op = row.operator("cgml3.optimize_viewport", text="Wire", icon='SHADING_WIRE')
        op.mode = 'WIRE'
        
        # Toggle Hide button with dynamic text
        is_hidden = 'cgml3_hidden_state' in scene and scene['cgml3_hidden_state']
        hide_text = "Show again" if is_hidden else "Hide"
        hide_icon = 'HIDE_OFF' if is_hidden else 'HIDE_ON'
        col.operator("cgml3.toggle_hide_unselected", text=hide_text, icon=hide_icon)
        
        # Check if in local view
        in_local_view = False
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        if space.local_view:
                            in_local_view = True
                            break
        
        # Restore button (show if state exists OR in local view)
        if scene.get('cgml3_viewport_optimized', False) or in_local_view:
            col.separator()
            col.operator("cgml3.restore_viewport", 
                        text="Restore Original", 
                        icon='RECOVER_LAST')
        
        # Tips
        box.separator()
        col = box.column(align=True)
        col.label(text="💡 Tips:", icon='INFO')
        col.label(text="• Select objects → hide the others")
        col.label(text="• 'Bounds' = fastest display")
        col.label(text="• 'Local View' Completely hide unselected objects")


# Registration
classes = (
    CGML3_OT_OptimizeViewport,
    CGML3_OT_RestoreViewport,
    CGML3_OT_ToggleHideUnselected,
    CGML3_OT_MaterialPreviewSelection,
    CGML3_OT_QuickLocalView,
    CGML3_MT_MaterialPreviewContext,
    CGML3_PT_ViewportPerformance,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
