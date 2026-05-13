# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
CityGML Import Scanner UI Panel
================================

Displays scan results before import to help users make informed decisions.

Features:
- File metadata display
- Feature type distribution
- LOD level statistics
- Bounding box visualization info
- Memory usage estimate
- Quick import button with scan results
"""

import bpy
from bpy.types import Panel, Operator
from bpy.props import StringProperty
import json


class CGML3_PT_ImportScanner(Panel):
    """Panel for CityGML import scanning and preview"""
    bl_label = "Import Scanner"
    bl_idname = "CGML3_PT_import_scanner"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "CityGML"
    bl_options = {'DEFAULT_CLOSED'}
    
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        
        # Info box
        box = layout.box()
        box.label(text="Scan CityGML files before import", icon='INFO')
        box.label(text="to preview metadata and statistics")
        
        # Scan button
        col = layout.column(align=True)
        col.operator("cgml3.scan_file_browser", text="Select File to Scan", icon='FILE_FOLDER')
        
        # Display scan results if available
        if "cgml3_scan_filepath" in scene:
            layout.separator()
            self._draw_scan_results(layout, scene)
    
    def _draw_scan_results(self, layout, scene):
        """Draw scan results section"""
        
        # File info
        box = layout.box()
        box.label(text="File Information", icon='FILE')
        
        filepath = scene.get("cgml3_scan_filepath", "")
        from pathlib import Path
        filename = Path(filepath).name if filepath else "Unknown"
        
        col = box.column(align=True)
        col.label(text=f"File: {filename}")
        col.label(text=f"Size: {scene.get('cgml3_scan_filesize', 0):.2f} MB")
        col.label(text=f"Version: CityGML {scene.get('cgml3_scan_version', 'Unknown')}")
        col.label(text=f"CRS: {scene.get('cgml3_scan_crs', 'Unknown')}")
        col.label(text=f"Scan Time: {scene.get('cgml3_scan_time', 0):.2f}s")
        
        # Feature statistics
        box = layout.box()
        box.label(text="Feature Statistics", icon='MESH_DATA')
        
        total_features = scene.get("cgml3_scan_features", 0)
        col = box.column(align=True)
        col.label(text=f"Total Features: {total_features}")
        
        # Feature type breakdown
        feature_types_json = scene.get("cgml3_scan_feature_types", "{}")
        try:
            feature_types = json.loads(feature_types_json)
            if feature_types:
                col.separator()
                col.label(text="Feature Types:")
                for ftype, count in sorted(feature_types.items(), key=lambda x: -x[1])[:5]:
                    col.label(text=f"  • {ftype}: {count}")
                
                if len(feature_types) > 5:
                    col.label(text=f"  ... and {len(feature_types) - 5} more")
        except Exception:
            pass
        
        # LOD distribution
        box = layout.box()
        box.label(text="LOD Distribution", icon='LINENUMBERS_ON')
        
        col = box.column(align=True)
        lod0 = scene.get("cgml3_scan_lod0", 0)
        lod1 = scene.get("cgml3_scan_lod1", 0)
        lod2 = scene.get("cgml3_scan_lod2", 0)
        lod3 = scene.get("cgml3_scan_lod3", 0)
        lod4 = scene.get("cgml3_scan_lod4", 0)
        
        if lod0 > 0:
            col.label(text=f"LOD0: {lod0} geometries")
        if lod1 > 0:
            col.label(text=f"LOD1: {lod1} geometries")
        if lod2 > 0:
            col.label(text=f"LOD2: {lod2} geometries")
        if lod3 > 0:
            col.label(text=f"LOD3: {lod3} geometries")
        if lod4 > 0:
            col.label(text=f"LOD4: {lod4} geometries")
        
        if lod0 == 0 and lod1 == 0 and lod2 == 0 and lod3 == 0 and lod4 == 0:
            col.label(text="No LOD geometries detected")
        
        # Bounding box
        if "cgml3_scan_bbox_min_x" in scene:
            box = layout.box()
            box.label(text="Spatial Extent", icon='EMPTY_AXIS')
            
            min_x = scene.get("cgml3_scan_bbox_min_x", 0)
            min_y = scene.get("cgml3_scan_bbox_min_y", 0)
            min_z = scene.get("cgml3_scan_bbox_min_z", 0)
            max_x = scene.get("cgml3_scan_bbox_max_x", 0)
            max_y = scene.get("cgml3_scan_bbox_max_y", 0)
            max_z = scene.get("cgml3_scan_bbox_max_z", 0)
            
            width = max_x - min_x
            height = max_y - min_y
            depth = max_z - min_z
            
            col = box.column(align=True)
            col.label(text=f"Width (X): {width:.2f} m")
            col.label(text=f"Height (Y): {height:.2f} m")
            col.label(text=f"Depth (Z): {depth:.2f} m")
            col.separator()
            col.label(text=f"Min: ({min_x:.2f}, {min_y:.2f}, {min_z:.2f})")
            col.label(text=f"Max: ({max_x:.2f}, {max_y:.2f}, {max_z:.2f})")
        
        # Memory estimate
        box = layout.box()
        box.label(text="Import Estimate", icon='MEMORY')
        
        memory_est = scene.get("cgml3_scan_memory_est", 0)
        col = box.column(align=True)
        col.label(text=f"Estimated RAM: ~{memory_est:.1f} MB")
        
        # Warning for large files
        if memory_est > 1000:
            col.separator()
            col.label(text="⚠ Large import! Consider:", icon='ERROR')
            col.label(text="  • Filtering by bounding box")
            col.label(text="  • Selecting specific LOD levels")
            col.label(text="  • Using streaming import")
        
        # Import button
        layout.separator()
        col = layout.column(align=True)
        col.scale_y = 1.5
        col.operator("cgml3.import_scanned_file", text="Import This File", icon='IMPORT')
        col.operator("cgml3.clear_scan_results", text="Clear Scan Results", icon='X')


class CGML3_OT_ScanFileBrowser(Operator):
    """Open file browser to select and scan CityGML file"""
    bl_idname = "cgml3.scan_file_browser"
    bl_label = "Select CityGML File"
    bl_options = {'REGISTER'}
    
    filepath: StringProperty(
        name="CityGML File",
        subtype='FILE_PATH'
    )
    filter_glob: StringProperty(
        default="*.gml;*.xml",
        options={'HIDDEN'}
    )
    
    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}
    
    def execute(self, context):
        if not self.filepath:
            self.report({'ERROR'}, "No file selected")
            return {'CANCELLED'}
        
        # Trigger scan
        bpy.ops.cgml3.scan_import_file(filepath=self.filepath)
        
        return {'FINISHED'}


class CGML3_OT_ImportScannedFile(Operator):
    """Import the currently scanned CityGML file"""
    bl_idname = "cgml3.import_scanned_file"
    bl_label = "Import Scanned File"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        scene = context.scene
        
        filepath = scene.get("cgml3_scan_filepath")
        if not filepath:
            self.report({'ERROR'}, "No file scanned. Scan a file first.")
            return {'CANCELLED'}
        
        # Use existing import operator
        bpy.ops.cgml3.import_gml_file(filepath=filepath)
        
        return {'FINISHED'}


class CGML3_OT_ClearScanResults(Operator):
    """Clear scan results from scene properties"""
    bl_idname = "cgml3.clear_scan_results"
    bl_label = "Clear Scan Results"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        scene = context.scene
        
        # Remove all scan properties
        scan_keys = [k for k in scene.keys() if k.startswith("cgml3_scan_")]
        for key in scan_keys:
            try:
                del scene[key]
            except Exception:
                pass
        
        self.report({'INFO'}, "Scan results cleared")
        return {'FINISHED'}


def register():
    bpy.utils.register_class(CGML3_PT_ImportScanner)
    bpy.utils.register_class(CGML3_OT_ScanFileBrowser)
    bpy.utils.register_class(CGML3_OT_ImportScannedFile)
    bpy.utils.register_class(CGML3_OT_ClearScanResults)


def unregister():
    bpy.utils.unregister_class(CGML3_OT_ClearScanResults)
    bpy.utils.unregister_class(CGML3_OT_ImportScannedFile)
    bpy.utils.unregister_class(CGML3_OT_ScanFileBrowser)
    bpy.utils.unregister_class(CGML3_PT_ImportScanner)
