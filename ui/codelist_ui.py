# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
UI Operators for CityGML Codelist Selection

Provides UI dropdown operators for selecting CityGML codelist values
with search and validation.
"""

import bpy
from bpy.types import Operator
from bpy.props import StringProperty, EnumProperty

# Import codelist utilities
import sys
from pathlib import Path
addon_dir = Path(__file__).resolve().parent.parent
if str(addon_dir) not in sys.path:
    sys.path.insert(0, str(addon_dir))

from shared.codelists import (
    get_codelist,
    get_common_codes,
    search_codes,
    validate_code,
    get_code_description,
    CODELISTS
)


def get_enum_items_for_codelist(self, context):
    """
    Dynamic enum callback for codelist selection.
    Returns list of (identifier, name, description) tuples for UI.
    """
    attribute_name = self.attribute_name
    codelist = get_codelist(attribute_name)
    
    if not codelist:
        return [('NONE', 'No codelist defined', '')]
    
    items = []
    for code, desc in codelist.items():
        items.append((code, f"{code}: {desc}", desc))
    
    return items if items else [('NONE', 'Empty codelist', '')]


class CGML3_OT_SelectCodelistValue(Operator):
    """Select CityGML codelist value with dropdown"""
    bl_idname = "cgml3.select_codelist_value"
    bl_label = "Select Codelist Value"
    bl_options = {'REGISTER', 'UNDO'}
    
    # Property name to set (e.g., 'bldg:class')
    attribute_name: StringProperty(name="Attribute Name")
    
    # Selected code value
    code_value: EnumProperty(
        name="Code",
        description="Select code from codelist",
        items=get_enum_items_for_codelist
    )
    
    # Target object type ('OBJECT' or 'MATERIAL')
    target_type: StringProperty(default='OBJECT')
    
    def execute(self, context):
        if self.code_value == 'NONE':
            self.report({'WARNING'}, "No valid code selected")
            return {'CANCELLED'}
        
        # Get target (active object or active material)
        target = None
        if self.target_type == 'OBJECT':
            target = context.active_object
        elif self.target_type == 'MATERIAL' and context.active_object:
            target = context.active_object.active_material
        
        if not target:
            self.report({'ERROR'}, f"No active {self.target_type.lower()}")
            return {'CANCELLED'}
        
        # Set custom property
        target[self.attribute_name] = self.code_value
        
        # Get description for feedback
        desc = get_code_description(self.attribute_name, self.code_value)
        if desc:
            self.report({'INFO'}, f"Set {self.attribute_name} = {self.code_value} ({desc})")
        else:
            self.report({'INFO'}, f"Set {self.attribute_name} = {self.code_value}")
        
        return {'FINISHED'}
    
    def invoke(self, context, event):
        # Pre-select current value if exists
        target = None
        if self.target_type == 'OBJECT':
            target = context.active_object
        elif self.target_type == 'MATERIAL' and context.active_object:
            target = context.active_object.active_material
        
        if target:
            current = target.get(self.attribute_name)
            if current and validate_code(self.attribute_name, str(current)):
                self.code_value = str(current)
        
        return context.window_manager.invoke_props_dialog(self, width=500)
    
    def draw(self, context):
        layout = self.layout
        layout.label(text=f"Select {self.attribute_name}")
        layout.prop(self, "code_value", text="")


class CGML3_OT_SearchCodelistValue(Operator):
    """Search CityGML codelist values"""
    bl_idname = "cgml3.search_codelist_value"
    bl_label = "Search Codelist"
    bl_options = {'REGISTER', 'UNDO'}
    
    # Property name (e.g., 'bldg:class')
    attribute_name: StringProperty(name="Attribute Name")
    
    # Search query
    search_query: StringProperty(
        name="Search",
        description="Search code descriptions",
        default=""
    )
    
    # Target object type
    target_type: StringProperty(default='OBJECT')
    
    def execute(self, context):
        if not self.search_query:
            self.report({'WARNING'}, "Enter search query")
            return {'CANCELLED'}
        
        # Search codes
        results = search_codes(self.attribute_name, self.search_query)
        
        if not results:
            self.report({'INFO'}, f"No results for '{self.search_query}'")
            return {'FINISHED'}
        
        # Show results in info area
        self.report({'INFO'}, f"Found {len(results)} results:")
        for code, desc in results[:5]:  # Show first 5
            print(f"  {code}: {desc}")
        
        return {'FINISHED'}
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "search_query")


class CGML3_OT_ValidateCodelistValue(Operator):
    """Validate CityGML codelist value"""
    bl_idname = "cgml3.validate_codelist_value"
    bl_label = "Validate Codelist Value"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        obj = context.active_object
        if not obj:
            self.report({'ERROR'}, "No active object")
            return {'CANCELLED'}
        
        # Check all codelist attributes on object
        invalid_count = 0
        for attr_name in CODELISTS.keys():
            value = obj.get(attr_name)
            if value:
                if not validate_code(attr_name, str(value)):
                    invalid_count += 1
                    desc = get_code_description(attr_name, str(value))
                    if desc:
                        self.report({'WARNING'}, f"{attr_name}={value} is valid but unusual")
                    else:
                        self.report({'ERROR'}, f"{attr_name}={value} is NOT in standard codelist")
        
        if invalid_count == 0:
            self.report({'INFO'}, "All codelist values are valid")
        else:
            self.report({'WARNING'}, f"Found {invalid_count} non-standard code(s)")
        
        return {'FINISHED'}


# Panel for codelist selection
class CGML3_PT_CodelistPanel(bpy.types.Panel):
    """CityGML Codelist Selection Panel"""
    bl_label = "CityGML Codelists"
    bl_idname = "CGML3_PT_codelists"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'CityGML'
    bl_context = 'objectmode'
    
    @classmethod
    def poll(cls, context):
        return context.active_object is not None
    
    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        
        if not obj:
            return
        
        # Get feature type
        feature_type = obj.get('cgml3_feature', '')
        
        # Building codelists
        if feature_type in ['Building', 'BuildingPart', 'BuildingUnit']:
            box = layout.box()
            box.label(text="Building Attributes", icon='HOME')
            
            # Class
            row = box.row(align=True)
            current_class = obj.get('bldg:class', '')
            if current_class:
                desc = get_code_description('bldg:class', current_class)
                label = f"{current_class}: {desc}" if desc else current_class
                row.label(text=f"Class: {label}")
            else:
                row.label(text="Class: (not set)")
            
            op = row.operator("cgml3.select_codelist_value", text="", icon='DOWNARROW_HLT')
            op.attribute_name = 'bldg:class'
            op.target_type = 'OBJECT'
            
            # Function
            row = box.row(align=True)
            current_func = obj.get('bldg:function', '')
            if current_func:
                desc = get_code_description('bldg:function', current_func)
                label = f"{current_func}: {desc}" if desc else current_func
                row.label(text=f"Function: {label}")
            else:
                row.label(text="Function: (not set)")
            
            op = row.operator("cgml3.select_codelist_value", text="", icon='DOWNARROW_HLT')
            op.attribute_name = 'bldg:function'
            op.target_type = 'OBJECT'
            
            # Usage
            row = box.row(align=True)
            current_usage = obj.get('bldg:usage', '')
            if current_usage:
                desc = get_code_description('bldg:usage', current_usage)
                label = f"{current_usage}: {desc}" if desc else current_usage
                row.label(text=f"Usage: {label}")
            else:
                row.label(text="Usage: (not set)")
            
            op = row.operator("cgml3.select_codelist_value", text="", icon='DOWNARROW_HLT')
            op.attribute_name = 'bldg:usage'
            op.target_type = 'OBJECT'
            
            # Roof Type (if applicable)
            if feature_type == 'Building':
                row = box.row(align=True)
                current_roof = obj.get('bldg:roofType', '')
                if current_roof:
                    desc = get_code_description('bldg:roofType', current_roof)
                    label = f"{current_roof}: {desc}" if desc else current_roof
                    row.label(text=f"Roof: {label}")
                else:
                    row.label(text="Roof: (not set)")
                
                op = row.operator("cgml3.select_codelist_value", text="", icon='DOWNARROW_HLT')
                op.attribute_name = 'bldg:roofType'
                op.target_type = 'OBJECT'
        
        # Transportation codelists
        elif feature_type in ['Road', 'Railway', 'Track', 'Square', 'Waterway', 'Section', 'Intersection']:
            box = layout.box()
            box.label(text="Transportation Attributes", icon='AUTO')
            
            for attr in ['tran:class', 'tran:function', 'tran:usage']:
                row = box.row(align=True)
                current = obj.get(attr, '')
                attr_short = attr.split(':')[1].title()
                
                if current:
                    desc = get_code_description(attr, current)
                    label = f"{current}: {desc}" if desc else current
                    row.label(text=f"{attr_short}: {label}")
                else:
                    row.label(text=f"{attr_short}: (not set)")
                
                op = row.operator("cgml3.select_codelist_value", text="", icon='DOWNARROW_HLT')
                op.attribute_name = attr
                op.target_type = 'OBJECT'
        
        # Validation
        layout.separator()
        layout.operator("cgml3.validate_codelist_value", text="Validate Codes", icon='CHECKMARK')


# Registration
classes = (
    CGML3_OT_SelectCodelistValue,
    CGML3_OT_SearchCodelistValue,
    CGML3_OT_ValidateCodelistValue,
    CGML3_PT_CodelistPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
