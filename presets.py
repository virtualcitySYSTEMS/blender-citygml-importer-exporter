# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Presets: Saving and Loading Settings Presets
# Author: Virtual City Systems
# Year: 2026

import os
import json
from pathlib import Path

import bpy
from bpy.types import Operator, Panel
from bpy.props import StringProperty, EnumProperty

# Directory for preset files
PRESETS_DIR = Path(__file__).resolve().parent / "presets"

# Properties from CGML3_Runtime to be saved
# (excluding SKIP_SAVE and read-only statistics fields)
SAVEABLE_PROPS = [
    "xsd_validate",
    "xsd_lenient_validation",
    "use_bbox_filter",
    "bbox_min_x",
    "bbox_min_y",
    "bbox_max_x",
    "bbox_max_y",
    "bbox_coords_string",
    "use_gmlid_filter",
    "gmlid_filter",
    "use_lod_filter",
    "lod_0",
    "lod_1",
    "lod_2",
    "lod_3",
    "lod_4",
    "use_feature_type_filter",
    "import_buildings",
    "import_bridges",
    "import_tunnels",
    "import_vegetation",
    "import_water",
    "import_transportation",
    "import_cityfurniture",
    "import_landuse",
    "import_relief",
    "import_generics",
    "import_local",
    "mesh_validate",
    "export_debug",
    "import_streaming_threshold_mb",
    "export_schema_location_mode",
    "db_host",
    "db_port",
    "db_name",
    "db_user",
    "db_pass",
    "db_schema",
]


def _ensure_presets_dir():
    """Creates the presets directory if it doesn't exist."""
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)


def _list_preset_files() -> list[Path]:
    """Returns a sorted list of all .json preset files."""
    _ensure_presets_dir()
    return sorted(PRESETS_DIR.glob("*.json"))


def _preset_name_to_filename(name: str) -> str:
    """Converts a preset name into a safe filename."""
    # Only alphanumeric characters, underscores, hyphens, and spaces
    safe = "".join(c for c in name if c.isalnum() or c in " _-")
    return safe.strip() or "preset"


def _get_preset_items(self, context):
    """Callback for EnumProperty: returns available presets."""
    items = []
    for f in _list_preset_files():
        name = f.stem
        items.append((str(f), name, f"Load preset: {name}"))
    if not items:
        items.append(("NONE", "(no presets)", "No presets saved yet"))
    return items


def save_preset(context, name: str) -> tuple[bool, str]:
    """Saves the current CGML3_Runtime settings as a preset."""
    _ensure_presets_dir()

    cgml3 = context.scene.cgml3
    data = {}
    for prop in SAVEABLE_PROPS:
        if hasattr(cgml3, prop):
            val = getattr(cgml3, prop)
            # Blender EnumProperty values are strings
            data[prop] = val

    filename = _preset_name_to_filename(name) + ".json"
    filepath = PRESETS_DIR / filename

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError as e:
        return False, f"Error saving preset: {e}"

    return True, str(filepath)


def load_preset(context, filepath: str) -> tuple[bool, str]:
    """Loads a preset from a JSON file and applies the CGML3_Runtime settings."""
    if not os.path.isfile(filepath):
        return False, f"File not found: {filepath}"

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return False, f"Error loading preset: {e}"

    if not isinstance(data, dict):
        return False, "Invalid preset format"

    cgml3 = context.scene.cgml3
    applied = 0
    for prop, value in data.items():
        if prop in SAVEABLE_PROPS and hasattr(cgml3, prop):
            try:
                setattr(cgml3, prop, value)
                applied += 1
            except (TypeError, ValueError):
                pass

    return True, f"{applied} settings loaded"


# ---------------- Operators ----------------

class CGML3_OT_SavePreset(Operator):
    """Saves the current settings as a preset"""
    bl_idname = "cgml3.save_preset"
    bl_label = "Save Preset"
    bl_options = {'REGISTER'}

    def execute(self, context):
        name = context.scene.cgml3_preset_name.strip()
        if not name:
            self.report({'ERROR'}, "Please enter a preset name")
            return {'CANCELLED'}

        ok, msg = save_preset(context, name)
        if ok:
            self.report({'INFO'}, f"Preset saved: {name}")
        else:
            self.report({'ERROR'}, msg)
            return {'CANCELLED'}

        return {'FINISHED'}


class CGML3_OT_LoadPreset(Operator):
    """Loads the selected preset"""
    bl_idname = "cgml3.load_preset"
    bl_label = "Load Preset"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        filepath = context.scene.cgml3_preset_select
        if not filepath or filepath == "NONE":
            self.report({'WARNING'}, "No preset selected")
            return {'CANCELLED'}

        ok, msg = load_preset(context, filepath)
        if ok:
            preset_name = Path(filepath).stem
            self.report({'INFO'}, f"Preset loaded: {preset_name} ({msg})")
        else:
            self.report({'ERROR'}, msg)
            return {'CANCELLED'}

        return {'FINISHED'}


class CGML3_OT_DeletePreset(Operator):
    """Deletes the selected preset"""
    bl_idname = "cgml3.delete_preset"
    bl_label = "Delete Preset"
    bl_options = {'REGISTER'}

    def execute(self, context):
        filepath = context.scene.cgml3_preset_select
        if not filepath or filepath == "NONE":
            self.report({'WARNING'}, "No preset selected")
            return {'CANCELLED'}

        try:
            os.remove(filepath)
            preset_name = Path(filepath).stem
            self.report({'INFO'}, f"Preset deleted: {preset_name}")
        except OSError as e:
            self.report({'ERROR'}, f"Error deleting preset: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}


# ---------------- Panel ----------------

class CGML3_PT_Presets(Panel):
    bl_label = "Presets"
    bl_idname = "CGML3_PT_Presets"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "CityGML"
    bl_parent_id = "CGML3_PT_Main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # Load preset
        box = layout.box()
        box.label(text="Load Preset", icon='IMPORT')
        box.prop(scene, "cgml3_preset_select", text="")
        row = box.row(align=True)
        row.operator("cgml3.load_preset", text="Load", icon='CHECKMARK')
        row.operator("cgml3.delete_preset", text="", icon='TRASH')

        # Save preset
        box = layout.box()
        box.label(text="Save Preset", icon='EXPORT')
        box.prop(scene, "cgml3_preset_name", text="Name")
        box.operator("cgml3.save_preset", text="Save", icon='FILE_TICK')


# ---------------- Registration ----------------

_classes = (
    CGML3_OT_SavePreset,
    CGML3_OT_LoadPreset,
    CGML3_OT_DeletePreset,
    CGML3_PT_Presets,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.cgml3_preset_name = StringProperty(
        name="Preset Name",
        description="Name for the preset to be saved",
        default="",
    )
    bpy.types.Scene.cgml3_preset_select = EnumProperty(
        name="Preset",
        description="Available presets",
        items=_get_preset_items,
    )


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

    if hasattr(bpy.types.Scene, "cgml3_preset_name"):
        del bpy.types.Scene.cgml3_preset_name
    if hasattr(bpy.types.Scene, "cgml3_preset_select"):
        del bpy.types.Scene.cgml3_preset_select
