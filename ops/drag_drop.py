# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# ops/drag_drop.py
"""Drag & Drop handler for CityGML files"""

import bpy
import os
from pathlib import Path


class CityGMLDropHandler(bpy.types.FileHandler):
    """Handler for drag & drop of CityGML files into Blender"""
    bl_idname = "CGML3_FH_citygml"
    bl_label = "CityGML File Handler"
    bl_import_operator = "cgml3.import_gml_file"
    bl_file_extensions = ".gml;.xml"

    @classmethod
    def poll_drop(cls, context):
        """Check if drop is allowed in current context"""
        return context.area and context.area.type in {'VIEW_3D', 'OUTLINER'}


def register():
    """Register drag & drop handler"""
    bpy.utils.register_class(CityGMLDropHandler)


def unregister():
    """Unregister drag & drop handler"""
    bpy.utils.unregister_class(CityGMLDropHandler)


if __name__ == "__main__":
    register()
