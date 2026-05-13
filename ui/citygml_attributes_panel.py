# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
CityGML Attributes Panel for Blender N-Panel (3D View)

Provides a user-friendly interface for viewing and editing CityGML-specific attributes:
- Feature Type & GML ID
- LOD Level (lod0-lod4)
- Feature-specific attributes (class, function, usage, etc.)
- Appearance & Boundary Semantics
- Generic Attributes (via existing UI)

This tool is currently disabled in the GUI on purpose.
The panel code is kept here so it can be re-enabled later without rebuilding it.
"""

import bpy
from bpy.types import Panel, Operator, PropertyGroup
from bpy.props import StringProperty, IntProperty, EnumProperty, BoolProperty


# ============================================================================
# Helper Functions
# ============================================================================

def get_feature_type(obj):
    """Get CityGML feature type from object."""
    if obj is None:
        return None
    # Try cgml3_feature first (CityGML 3.0)
    feat = obj.get("cgml3_feature")
    if feat:
        return feat
    # Try CityGML 2.0 pattern (feature_type custom property)
    feat = obj.get("feature_type")
    if feat:
        return feat
    return None


def get_gml_id(obj):
    """Get GML ID from object."""
    if obj is None:
        return None
    return obj.get("gml_id")


def get_lod_level(obj):
    """Detect LOD level from object name or custom properties."""
    if obj is None:
        return None
    
    # Check _lod custom property (used by exporter)
    lod = obj.get("_lod")
    if lod is not None:
        return lod
    
    # Fallback: parse from name (e.g., "Building_LOD2_123")
    name = obj.name.lower()
    for i in range(5):
        if f"lod{i}" in name:
            return i
    
    return None


def get_feature_attributes(obj):
    """Get feature-specific attributes (class, function, usage, etc.)."""
    if obj is None:
        return {}
    
    attrs = {}
    feature_type = get_feature_type(obj)
    
    # Common attributes across all feature types
    common = ["class", "function", "usage", "gml_name", "gml_description", 
              "creationDate", "terminationDate", "validFrom", "validTo"]
    
    # Feature-specific attributes
    specific = {
        "Building": ["yearOfConstruction", "yearOfDemolition", "roofType", 
                     "measuredHeight", "storeysAboveGround", "storeysBelowGround"],
        "BuildingPart": ["yearOfConstruction", "yearOfDemolition", "roofType", 
                         "measuredHeight", "storeysAboveGround", "storeysBelowGround"],
        "BuildingUnit": ["type", "ownerName", "numberOfRooms", 
                         "numberOfBedRooms", "numberOfBathRooms"],
        "Storey": ["type", "storeysAboveGround", "storeysBelowGround"],
        "Bridge": ["isMovable", "yearOfConstruction", "yearOfDemolition"],
        "BridgePart": ["isMovable", "yearOfConstruction", "yearOfDemolition"],
        "BridgeRoom": [],  # uses common class/function/usage
        "BridgeFurniture": [],
        "Tunnel": ["yearOfConstruction", "yearOfDemolition"],
        "TunnelPart": ["yearOfConstruction", "yearOfDemolition"],
        "HollowSpace": [],
        "TunnelFurniture": [],
        "Road": ["surfaceMaterial"],
        "Railway": ["surfaceMaterial"],
        "Section": [],
        "Intersection": [],
        "Waterway": [],
        "WaterBody": ["waterLevel"],
        "SolitaryVegetationObject": ["species", "trunkDiameter", "crownDiameter", "height"],
        "PlantCover": ["averageHeight"],
        "CityFurniture": [],
        "LandUse": [],
        "ReliefFeature": [],
        "TINRelief": [],
    }
    
    # Collect common attributes
    for attr in common:
        val = obj.get(attr)
        if val is not None:
            attrs[attr] = val
    
    # Collect feature-specific attributes
    if feature_type and feature_type in specific:
        for attr in specific[feature_type]:
            val = obj.get(attr)
            if val is not None:
                attrs[attr] = val
    
    return attrs


def get_boundary_surfaces(obj):
    """Get boundary surface info from materials."""
    if obj is None or obj.type != 'MESH':
        return []
    
    boundaries = []
    for mat_slot in obj.material_slots:
        mat = mat_slot.material
        if mat is None:
            continue
        
        surf_type = mat.get("surface_type") or mat.get("SurfaceTyp")
        if surf_type:
            boundaries.append({
                "name": mat.name,
                "type": surf_type,
                "gml_id": mat.get("gml_surface_id") or mat.get("gml_polygon_id"),
            })
    
    return boundaries


# ============================================================================
# Operators
# ============================================================================

class CGML3_OT_SetLOD(Operator):
    """Set LOD level for selected object"""
    bl_idname = "cgml3.set_lod"
    bl_label = "Set LOD"
    bl_options = {'REGISTER', 'UNDO'}
    
    lod_level: IntProperty(
        name="LOD Level",
        default=2,
        min=0,
        max=4,
    )
    
    def execute(self, context):
        obj = context.active_object
        if obj is None:
            self.report({'WARNING'}, "No active object")
            return {'CANCELLED'}
        
        obj["_lod"] = self.lod_level
        self.report({'INFO'}, f"Set LOD{self.lod_level} for {obj.name}")
        return {'FINISHED'}


class CGML3_OT_SetFeatureType(Operator):
    """Set CityGML feature type for selected object"""
    bl_idname = "cgml3.set_feature_type"
    bl_label = "Set Feature Type"
    bl_options = {'REGISTER', 'UNDO'}
    
    feature_type: StringProperty(
        name="Feature Type",
        default="Building",
    )
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def execute(self, context):
        obj = context.active_object
        if obj is None:
            self.report({'WARNING'}, "No active object")
            return {'CANCELLED'}
        
        obj["cgml3_feature"] = self.feature_type
        self.report({'INFO'}, f"Set feature type to {self.feature_type}")
        return {'FINISHED'}


class CGML3_OT_SetGMLID(Operator):
    """Set GML ID for selected object"""
    bl_idname = "cgml3.set_gml_id"
    bl_label = "Set GML ID"
    bl_options = {'REGISTER', 'UNDO'}
    
    gml_id: StringProperty(
        name="GML ID",
        default="",
    )
    
    def invoke(self, context, event):
        obj = context.active_object
        if obj:
            current = get_gml_id(obj)
            if current:
                self.gml_id = current
        return context.window_manager.invoke_props_dialog(self)
    
    def execute(self, context):
        obj = context.active_object
        if obj is None:
            self.report({'WARNING'}, "No active object")
            return {'CANCELLED'}
        
        if not self.gml_id.strip():
            self.report({'WARNING'}, "GML ID cannot be empty")
            return {'CANCELLED'}
        
        obj["gml_id"] = self.gml_id.strip()
        self.report({'INFO'}, f"Set GML ID to {self.gml_id}")
        return {'FINISHED'}


# ============================================================================
# Panels (N-Panel in 3D View)
# ============================================================================

class CGML3_PT_MainPanel(Panel):
    """Main CityGML Attributes Panel in 3D View"""
    bl_label = "CityGML Attributes"
    bl_idname = "CGML3_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'CityGML'
    
    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        
        if obj is None:
            layout.label(text="No active object", icon='INFO')
            return
        
        # Header with object name
        box = layout.box()
        box.label(text=f"Object: {obj.name}", icon='OBJECT_DATA')


class CGML3_PT_CoreAttributes(Panel):
    """Core CityGML attributes (Feature Type, GML ID, LOD)"""
    bl_label = "Core Attributes"
    bl_idname = "CGML3_PT_core_attributes"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'CityGML'
    bl_parent_id = "CGML3_PT_main_panel"
    bl_options = {'DEFAULT_CLOSED'}
    
    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        
        if obj is None:
            return
        
        # Feature Type
        feature_type = get_feature_type(obj)
        row = layout.row()
        row.label(text="Feature Type:")
        if feature_type:
            row.label(text=feature_type, icon='FILE_3D')
        else:
            row.label(text="(none)", icon='QUESTION')
        
        row = layout.row()
        op = row.operator("cgml3.set_feature_type", text="Set Feature Type", icon='GREASEPENCIL')
        if feature_type:
            op.feature_type = feature_type
        
        layout.separator()
        
        # GML ID
        gml_id = get_gml_id(obj)
        row = layout.row()
        row.label(text="GML ID:")
        if gml_id:
            row.label(text=gml_id, icon='LINKED')
        else:
            row.label(text="(none)", icon='UNLINKED')
        
        row = layout.row()
        row.operator("cgml3.set_gml_id", text="Set GML ID", icon='GREASEPENCIL')
        
        layout.separator()
        
        # LOD Level
        lod = get_lod_level(obj)
        row = layout.row()
        row.label(text="LOD Level:")
        if lod is not None:
            row.label(text=f"LOD{lod}", icon='OUTLINER_DATA_MESH')
        else:
            row.label(text="(auto)", icon='QUESTION')
        
        # LOD buttons
        row = layout.row(align=True)
        for i in range(5):
            op = row.operator("cgml3.set_lod", text=f"LOD{i}")
            op.lod_level = i


class CGML3_PT_FeatureAttributes(Panel):
    """Feature-specific attributes (class, function, usage, etc.)"""
    bl_label = "Feature Attributes"
    bl_idname = "CGML3_PT_feature_attributes"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'CityGML'
    bl_parent_id = "CGML3_PT_main_panel"
    bl_options = {'DEFAULT_CLOSED'}
    
    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        
        if obj is None:
            return
        
        attrs = get_feature_attributes(obj)
        
        if not attrs:
            layout.label(text="No feature attributes", icon='INFO')
            return
        
        # Draw attributes as two-column layout
        for key, value in sorted(attrs.items()):
            row = layout.row()
            row.label(text=key + ":")
            row.label(text=str(value))


class CGML3_PT_BoundarySemantics(Panel):
    """Boundary Surface Semantics (from materials)"""
    bl_label = "Boundary Surfaces"
    bl_idname = "CGML3_PT_boundary_semantics"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'CityGML'
    bl_parent_id = "CGML3_PT_main_panel"
    bl_options = {'DEFAULT_CLOSED'}
    
    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        
        if obj is None or obj.type != 'MESH':
            layout.label(text="Not a mesh object", icon='INFO')
            return
        
        boundaries = get_boundary_surfaces(obj)
        
        if not boundaries:
            layout.label(text="No boundary surfaces", icon='INFO')
            return
        
        for boundary in boundaries:
            box = layout.box()
            box.label(text=boundary["type"], icon='MATERIAL')
            row = box.row()
            row.label(text="Material:")
            row.label(text=boundary["name"])
            if boundary.get("gml_id"):
                row = box.row()
                row.label(text="GML ID:")
                row.label(text=boundary["gml_id"])


# ============================================================================
# Registration
# ============================================================================

# Note: This panel module is intentionally not registered from ui/__init__.py.
# That hides the CityGML Attributes tab from the Blender UI without deleting the tool.

CLASSES = (
    CGML3_OT_SetLOD,
    CGML3_OT_SetFeatureType,
    CGML3_OT_SetGMLID,
    CGML3_PT_MainPanel,
    CGML3_PT_CoreAttributes,
    CGML3_PT_FeatureAttributes,
    CGML3_PT_BoundarySemantics,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
