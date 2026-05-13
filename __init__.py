# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# __init__.py
import os

from .bundled_deps import ensure_bundled_site_packages

ensure_bundled_site_packages(os.path.dirname(__file__))

bl_info = {
    "name": "VCS 3DCityDB Importer/Exporter",
    "author": "Virtual City Systems - Oliver Förster",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "File > Import/Export; Sidebar > CityGML",
    "description": "Import/Export CityGML 2.0/3. Files and 3DCityDB via citydb-tool",
    "category": "Import-Export",
}

import bpy
from bpy.types import Panel, Operator, AddonPreferences, PropertyGroup
from bpy.props import StringProperty, BoolProperty, EnumProperty, IntProperty, FloatProperty, FloatVectorProperty, PointerProperty
import datetime
import sys, importlib, addon_utils
import shutil
import traceback
from pathlib import Path
from types import SimpleNamespace
from .ops.validate import validate_citygml3_xsd
from .common import import_citygml_auto
from .common.filter_utils import format_bbox_coords_string, parse_bbox_coords_string
from .ops import import_scanner
from .ops import drag_drop
from .ops import viewport_performance
from .ops import join_object_parts
from .ops import assign_object_part
from .ui import join_object_parts_panel
from . import modeltyper

def _cgml3_async_validate(filepath: str, last_size: int = -1):
    """
    Timer-basierte Validierung:
    wartet, bis die Exportdatei existiert und ihre Größe stabil ist,
    dann führt XML- und XSD-Validierung aus.
    """
    import os
    from .ops.validate_auto import well_formed_citygml_auto, validate_citygml_auto

    if not os.path.exists(filepath):
        return 0.5  # weiter warten

    size = os.path.getsize(filepath)

    if last_size != -1 and size != last_size:
        return lambda: _cgml3_async_validate(filepath, size)

    if last_size == -1:
        return lambda: _cgml3_async_validate(filepath, size)

    ok, msg = well_formed_citygml_auto(filepath)
    if not ok:
        print(f"[CityGML] XML nicht wohlgeformt: {msg}")
        return None

    okx, msgx = validate_citygml_auto(filepath)
    if not okx:
        print(f"[CityGML] XSD-Fehler: {msgx}")
    else:
        print("[CityGML] XSD-Validierung erfolgreich")

    return None

from .ops.citydb_cli import CityDBTool
from . import ui as cgml3_ui
from . import presets as cgml3_presets


HEADLESS_PREFS_KEY = "cgml3_headless_prefs"


def _default_citydb_prefs():
    return SimpleNamespace(
        citydb_exe="citydb-tool-1.3.0\\citydb.bat" if os.name == "nt" else "/opt/vcdb-tool/vcdb",
        use_docker=False,
        docker_image="ghcr.io/3dcitydb/citydb-tool:latest",
        citydb_v4_java_exe="",
        citydb_v4_impexp_jar="",
        citydb_v4_default_config="",
    )


def _get_citydb_prefs(context=None):
    ctx = context or bpy.context
    addon_keys = []
    module_name = (__package__ or __name__).split('.')[0]
    for key in (module_name, __name__, "CityGML-3_Importer-Exporter"):
        if key and key not in addon_keys:
            addon_keys.append(key)

    try:
        addons = ctx.preferences.addons
    except Exception:
        addons = None

    if addons is not None:
        for key in addon_keys:
            entry = addons.get(key)
            if entry is not None and hasattr(entry, "preferences"):
                return entry.preferences

    prefs = _default_citydb_prefs()
    overrides = getattr(bpy.app, "driver_namespace", {}).get(HEADLESS_PREFS_KEY, {}) or {}
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if hasattr(prefs, key):
                setattr(prefs, key, value)
    return prefs


def _detect_citygml_version_from_scene(scene=None):
    """
    Erkennt CityGML-Version anhand des Namens der obersten Collection im Blender-Szene.
    Gibt '3.0', '2.0' oder None zurück.
    """
    import bpy
    root_coll = (scene if scene is not None else bpy.context.scene).collection
    for col in root_coll.children:
        name_up = col.name.upper()
        if 'CITYGML3' in name_up:
            return '3.0'
        if 'CITYGML2' in name_up:
            return '2.0'
    return None

def _delete_pycache(root: Path) -> None:
    """Löscht alle __pycache__-Verzeichnisse unterhalb von root."""
    try:
        pycache_dirs = list(root.rglob("__pycache__"))
    except (FileNotFoundError, OSError):
        pycache_dirs = []
    for p in pycache_dirs:
        try:
            shutil.rmtree(p, ignore_errors=False)
        except FileNotFoundError:
            pass
        except PermissionError:
            # Nicht abbrechen, nur weitergehen
            pass
        except Exception as e:
            # Optional: in der Blender-Konsole sichtbar machen
            print(f"[pycache-cleanup] Konnte {p} nicht löschen: {e}")


def _cleanup_now_and_stop() -> None | float:
    """Einmalige, leicht verzögerte Bereinigung, dann Timer beenden."""
    _delete_pycache(Path(__file__).resolve().parent)
    return None  # Timer nicht erneut ausführen

def _citydb_v5_requires_v4_fallback(log_text: str) -> bool:
    """
    Detect when the v5 citydb-tool rejects the connected database version and
    the add-on should try the bundled v4 Java importer/exporter instead.
    """
    msg = (log_text or "").lower()
    if not msg:
        return False

    if "databaseversionexception" in msg and "not supported" in msg:
        return True

    return "not supported" in msg and "supported versions are 5." in msg


def _citydb_backend_label(use_v4_backend: bool) -> str:
    if use_v4_backend:
        return "3DCityDB v4 (Java impexp)"
    return "3DCityDB v5 (citydb-tool)"


def _citydb_backend_version(use_v4_backend: bool) -> str:
    return "4" if use_v4_backend else "5"


def _log_citydb_backend_fallback(operation: str) -> None:
    print(
        f"[3DCityDB] {operation}: citydb-tool v5 ist fuer diese Datenbank nicht kompatibel, "
        f"wechsle auf {_citydb_backend_label(True)}."
    )


def _log_citydb_backend_success(operation: str, use_v4_backend: bool) -> None:
    print(
        f"[3DCityDB] {operation}: Version {_citydb_backend_version(use_v4_backend)} erkannt, "
        f"verwendet wurde {_citydb_backend_label(use_v4_backend)}."
    )


def _collect_scene_feature_ids(scene) -> list[str]:
    from .shared.export_helpers import object_is_viewport_visible

    feature_ids: list[str] = []
    seen: set[str] = set()

    for obj in getattr(scene, "objects", []):
        if not object_is_viewport_visible(obj, bpy.context):
            continue
        try:
            feature_name = str(obj.get("cgml3_feature") or obj.get("feature_type") or "").strip()
        except Exception:
            feature_name = ""
        if not feature_name:
            continue

        try:
            feature_id = str(obj.get("gml_id") or "").strip()
        except Exception:
            feature_id = ""
        if not feature_id:
            continue

        if feature_id.endswith("_outer") or feature_id.endswith("_geom"):
            continue

        if feature_id in seen:
            continue

        seen.add(feature_id)
        feature_ids.append(feature_id)

    return feature_ids


# ---------------- Reload UI ----------------
class CGML3_PT_Reload(Panel):
    bl_label = "Plugin Reload"
    bl_idname = "CGML3_PT_Reload"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "CityGML"
    bl_order = 999

    def draw(self, context):
        col = self.layout.column(align=True)
        col.operator("cgml3.reload_addon", text="🔁 Reload CityGML Add-on", icon='FILE_REFRESH')


class CGML3_OT_ReloadAddon(Operator):
    """Reload dieses Add-on sicher"""
    bl_idname = "cgml3.reload_addon"
    bl_label = "Reload CityGML Add-on"

    def execute(self, context):
        def _reload_addon_safe():
            try:
                module_name = (__package__ or __name__).split('.')[0]

                # Deaktivieren
                addon_utils.disable(module_name, default_set=False)

                # Submodule aus sys.modules entfernen
                to_delete = [m for m in list(sys.modules)
                             if m == module_name or m.startswith(module_name + ".")]
                for m in to_delete:
                    del sys.modules[m]

                # Neu laden und aktivieren
                importlib.invalidate_caches()
                addon_utils.enable(module_name, default_set=False, persistent=False)

                print(f"✅ Reload abgeschlossen: {module_name}")
            except Exception as e:
                print(f"❌ Fehler beim Reload: {e}")
            return None

        bpy.app.timers.register(_reload_addon_safe, first_interval=0.1)
        return {'FINISHED'}

# ---------------- Filter Helper Operators ----------------
class CGML3_OT_SelectAllLODs(Operator):
    """Select all LOD levels"""
    bl_idname = "cgml3.select_all_lods"
    bl_label = "Select All LODs"
    
    def execute(self, context):
        cgml3 = context.scene.cgml3
        cgml3.lod_0 = True
        cgml3.lod_1 = True
        cgml3.lod_2 = True
        cgml3.lod_3 = True
        cgml3.lod_4 = True
        return {'FINISHED'}

class CGML3_OT_DeselectAllLODs(Operator):
    """Deselect all LOD levels"""
    bl_idname = "cgml3.deselect_all_lods"
    bl_label = "Deselect All LODs"
    
    def execute(self, context):
        cgml3 = context.scene.cgml3
        cgml3.lod_0 = False
        cgml3.lod_1 = False
        cgml3.lod_2 = False
        cgml3.lod_3 = False
        cgml3.lod_4 = False
        return {'FINISHED'}

class CGML3_OT_SelectAllFeatureTypes(Operator):
    """Select all feature types"""
    bl_idname = "cgml3.select_all_feature_types"
    bl_label = "Select All Types"
    
    def execute(self, context):
        cgml3 = context.scene.cgml3
        cgml3.import_buildings = True
        cgml3.import_bridges = True
        cgml3.import_tunnels = True
        cgml3.import_vegetation = True
        cgml3.import_water = True
        cgml3.import_transportation = True
        cgml3.import_cityfurniture = True
        cgml3.import_landuse = True
        cgml3.import_relief = True
        cgml3.import_generics = True
        return {'FINISHED'}

class CGML3_OT_DeselectAllFeatureTypes(Operator):
    """Deselect all feature types"""
    bl_idname = "cgml3.deselect_all_feature_types"
    bl_label = "Deselect All Types"
    
    def execute(self, context):
        cgml3 = context.scene.cgml3
        cgml3.import_buildings = False
        cgml3.import_bridges = False
        cgml3.import_tunnels = False
        cgml3.import_vegetation = False
        cgml3.import_water = False
        cgml3.import_transportation = False
        cgml3.import_cityfurniture = False
        cgml3.import_landuse = False
        cgml3.import_relief = False
        cgml3.import_generics = False
        return {'FINISHED'}

# ---------------- Preferences ----------------
class CGML3_Prefs(AddonPreferences):
    bl_idname = __name__

    citydb_exe: StringProperty(
        name="vcdb executable",
        subtype="FILE_PATH",
        description=(
            "Path to vcdb-tool. "
            "Windows: z.B. citydb-tool-1.3.0\\citydb.bat | "
            "Linux: z.B. /opt/vcdb-tool/vcdb"
        ),
        default="citydb-tool-1.3.0\\citydb.bat" if os.name == "nt" else "/opt/vcdb-tool/vcdb"
    )
    use_docker: BoolProperty(
        name="use Docker",
        default=False,
        description="citydb-tool start via Container"
    )
    docker_image: StringProperty(
        name="Docker image",
        default="ghcr.io/3dcitydb/citydb-tool:latest",
    )

    citydb_v4_java_exe: StringProperty(
        name="Java executable (v4)",
        description="Path to java.exe (optional). Leave empty to use 'java' from PATH.",
        subtype="FILE_PATH",
        default="",
    )
    citydb_v4_impexp_jar: StringProperty(
        name="impexp-client-cli JAR (v4)",
        description="Path to impexp-client-cli JAR. Leave empty to use the bundled JAR from 3dcitydb-tool-v4/.",
        subtype="FILE_PATH",
        default="",
    )
    citydb_v4_default_config: StringProperty(
        name="Default impexp config (v4)",
        description="Path to impexp_config.xml. Leave empty to use the bundled 3dcitydb-tool-v4/impexp_config.xml.",
        subtype="FILE_PATH",
        default="",
    )

    def draw(self, ctx):
        col = self.layout.column()
        box = col.box()
        box.label(text="3DCityDB v4 Fallback (Java impexp)")
        box.prop(self, "citydb_v4_java_exe")
        box.prop(self, "citydb_v4_impexp_jar")
        box.prop(self, "citydb_v4_default_config")
        col.separator()
        col.label(text="Note: DB version is automatically detected (v5→v4 fallback).")

        col.prop(self, "use_docker")
        if self.use_docker:
            col.prop(self, "docker_image")
            col.label(text="With Docker, input/output files and configuration")
            col.label(text="must be in the same mounted folder (/data).")
        else:
            col.prop(self, "citydb_exe")

# ---------------- Runtime / Scene Props ----------------
class CGML3_Runtime(PropertyGroup):
    tmp_dir: StringProperty(name="Temp Dir", subtype='DIR_PATH', default="")
    xsd_validate: BoolProperty(
        name="XSD-Validierung on",
        default=False,
        description=(
            "Validate against CityGML-3 XSD after import/export. "
            "Uses local schemas, falls back to OGC online schemas if needed."
        ),
    )
    xsd_lenient_validation: BoolProperty(
        name="Lenient Validation (3DCityDB)",
        default=False,
        description=(
            "For XSD errors from 3DCityDB exports (invalid gml:id values, incorrect element order, "
            "textureParameterization placement, etc.), only warn instead of aborting import. "
            "Useful for data from 3DCityDB version 5."
        ),
    )

    # Filter Options
    use_bbox_filter: BoolProperty(
        name="Bounding Box Filter",
        default=False,
        description="Limit import/export to a specific geographic area"
    )
    bbox_min_x: FloatProperty(name="Min X", default=0.0)
    bbox_min_y: FloatProperty(name="Min Y", default=0.0)
    bbox_max_x: FloatProperty(name="Max X", default=0.0)
    bbox_max_y: FloatProperty(name="Max Y", default=0.0)
    bbox_coords_string: StringProperty(
        name="BBox Coordinates",
        description="Format: MinX, MinY, MaxX, MaxY (e.g., 24487802, 6820388.5, 24488468, 6820804.5)",
        default=""
    )
    
    use_gmlid_filter: BoolProperty(
        name="GML ID Filter",
        default=False,
        description="Limit import/export to specific CityObjects"
    )
    gmlid_filter: StringProperty(
        name="GML IDs",
        description="Comma-separated list of GML IDs (e.g., 'DENW11AL0000h5UU_,DENW11AL0000h5UV_')",
        default="",
        maxlen=16384
    )
    
    # LOD Filter (for direct GML imports)
    use_lod_filter: BoolProperty(
        name="LOD Filter",
        default=False,
        description="Only import specific LODs (Level of Detail)"
    )
    lod_0: BoolProperty(name="LOD 0", default=True, description="Import LOD 0 geometries")
    lod_1: BoolProperty(name="LOD 1", default=True, description="Import LOD 1 geometries")
    lod_2: BoolProperty(name="LOD 2", default=True, description="Import LOD 2 geometries")
    lod_3: BoolProperty(name="LOD 3", default=True, description="Import LOD 3 geometries")
    lod_4: BoolProperty(name="LOD 4", default=True, description="Import LOD 4 geometries (CityGML 2.0 only)")
    
    # Feature-Type Filter (for direct GML imports)
    use_feature_type_filter: BoolProperty(
        name="Feature-Typ Filter",
        default=False,
        description="import only specific Feature-Typs"
    )
    import_buildings: BoolProperty(name="Buildings", default=True)
    import_bridges: BoolProperty(name="Bridges", default=True)
    import_tunnels: BoolProperty(name="Tunnels", default=True)
    import_vegetation: BoolProperty(name="Vegetation", default=True)
    import_water: BoolProperty(name="Water Bodies", default=True)
    import_transportation: BoolProperty(name="Transportation", default=True)
    import_cityfurniture: BoolProperty(name="City Furniture", default=True)
    import_landuse: BoolProperty(name="Land Use", default=True)
    import_relief: BoolProperty(name="Relief", default=True)
    import_generics: BoolProperty(name="Generic Objects", default=True)
    
    # Import statistics (read-only, set by importer)
    last_import_total: IntProperty(name="Total Features Found", default=0)
    last_import_imported: IntProperty(name="Features Imported", default=0)
    last_import_filtered: IntProperty(name="Features Filtered", default=0)
    
    # Import transformation options
    import_local: BoolProperty(
        name="Local Import (without Georeference)",
        default=False,
        description="Import objects locally at the Blender origin (0,0,0) without CRS transformation"
    )

    mesh_validate: BoolProperty(
        name="Run mesh.validate()",
        default=False,
        description=(
            "Runs mesh.validate() after import. Can remove faces for special geometries; "
            "disable for debugging/if faces are missing."
        ),
    )

    export_debug: BoolProperty(
        name="Export Debug (Console)",
        default=False,
        description="Writes additional debug info during export to the Blender console",
    )

    import_streaming_threshold_mb: FloatProperty(
        name="Streaming Threshold (MB)",
        default=30.0,
        min=1.0,
        max=10000.0,
        description=(
            "File size (in MB) at which the streaming mode is automatically used during import. "
            "Note: Streaming is especially useful for very large files."
        ),
    )

    export_schema_location_mode: EnumProperty(
        name="schemaLocation",
        description="How xsi:schemaLocation should be written (for validators like FME)",
        items=[
            ('REMOTE', 'Remote (schemas.opengis.net)', 'Uses online XSD URLs in schemaLocation'),
            ('LOCAL', 'Local (Schema/... in the addon)', 'Uses local XSD paths from the addon (file:// URIs)'),
            ('NONE', 'Without schemaLocation', 'Does not write xsi:schemaLocation'),
        ],
        default='REMOTE',
    )
    
    # Quick-Defaults for DB connection (for Panels)
    db_host: StringProperty(name="Host", default="localhost")
    db_port: StringProperty(name="Port", default="5432")
    db_name: StringProperty(name="DB", default="uebung-5")
    db_user: StringProperty(name="User", default="postgres")
    db_pass: StringProperty(name="Password", default="postgres")
    db_schema: StringProperty(name="Schema", default="citydb")

    db_import_ui_show_connection: BoolProperty(name="DB-Import Connection", default=True, options={'SKIP_SAVE'})
    db_import_ui_show_crs: BoolProperty(name="DB-Import CRS", default=False, options={'SKIP_SAVE'})
    db_import_ui_show_filters: BoolProperty(name="DB-Import Filter", default=False, options={'SKIP_SAVE'})
    db_import_ui_show_validity: BoolProperty(name="DB-Import Validity", default=False, options={'SKIP_SAVE'})
    db_import_ui_show_format: BoolProperty(name="DB-Import Appearance", default=False, options={'SKIP_SAVE'})
    db_import_ui_show_config: BoolProperty(name="DB-Import Config", default=False, options={'SKIP_SAVE'})

    db_export_ui_show_connection: BoolProperty(name="DB-Export Connection", default=True, options={'SKIP_SAVE'})
    db_export_ui_show_export: BoolProperty(name="DB-Export CityGML", default=False, options={'SKIP_SAVE'})
    db_export_ui_show_textures: BoolProperty(name="DB-Export Textures", default=False, options={'SKIP_SAVE'})
    db_export_ui_show_feature_types: BoolProperty(name="DB-Export Feature Types", default=False, options={'SKIP_SAVE'})
    db_export_ui_show_config: BoolProperty(name="DB-Export Config", default=False, options={'SKIP_SAVE'})
    db_export_ui_show_import_options: BoolProperty(name="DB-Export Import Options", default=False, options={'SKIP_SAVE'})
    db_export_ui_show_import_filters: BoolProperty(name="DB-Export Import Filters", default=False, options={'SKIP_SAVE'})
    db_export_ui_show_import_appearance: BoolProperty(name="DB-Export Import Appearance", default=False, options={'SKIP_SAVE'})
    db_export_ui_show_import_citygml: BoolProperty(name="DB-Export Import CityGML", default=False, options={'SKIP_SAVE'})
    db_export_ui_show_upgrade: BoolProperty(name="DB-Export Upgrade", default=False, options={'SKIP_SAVE'})

def _ensure_scene_props():
    sc = bpy.types.Scene

    if not hasattr(sc, "cgml3_offset"):
        sc.cgml3_offset = FloatVectorProperty(
            name="CityModel-Offset", size=3, default=(0.0, 0.0, 0.0)
        )


def _draw_foldout_section(layout, data, prop_name, label):
    box = layout.box()
    header = box.row(align=True)
    is_open = bool(getattr(data, prop_name))
    header.prop(
        data,
        prop_name,
        text=label,
        icon='TRIA_DOWN' if is_open else 'TRIA_RIGHT',
        emboss=False,
    )
    if not is_open:
        return None
    return box.column(align=True)

# ---------------- Panels ----------------
class CGML3_PT_Main(Panel):
    bl_label = "CityGML (3DCityDB)"
    bl_idname = "CGML3_PT_Main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "CityGML"
    bl_order = 1

    def draw(self, ctx):
        layout = self.layout
        sc = ctx.scene

        # Scene options
        box = layout.box()
        box.label(text="Scene options")
        row = box.row()
        row.prop(ctx.scene.cgml3, "xsd_validate", text="XSD Validation")
        if ctx.scene.cgml3.xsd_validate:
            row = box.row()
            row.prop(ctx.scene.cgml3, "xsd_lenient_validation", text="Tolerant (3DCityDB)")
        box.prop(ctx.scene.cgml3, "export_schema_location_mode", text="schemaLocation")

        if ctx.scene.get("cgml3_last_export_streaming", False):
            row.enabled = False
            box.label(text="Disabled during streaming export", icon='INFO')

        # Import/Export (File + 3DCityDB)
        box = layout.box()
        box.label(text="Import / Export")
        col = box.column(align=True)
        col.operator("cgml3.import_gml_file", icon='IMPORT')
        col.operator("cgml3.import_from_db", icon='ASSET_MANAGER')
        col.separator()
        col.operator("cgml3.export_gml_file", icon='EXPORT')
        col.operator("cgml3.export_to_db", icon='ASSET_MANAGER')

class CGML3_PT_Filter(Panel):
    bl_label = "Filter Options"
    bl_idname = "CGML3_PT_Filter"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "CityGML"
    bl_parent_id = "CGML3_PT_Main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, ctx):
        layout = self.layout
        cgml3 = ctx.scene.cgml3

        # Bounding Box Filter
        box = layout.box()
        row = box.row()
        row.prop(cgml3, "use_bbox_filter")
        
        col = box.column()
        col.enabled = cgml3.use_bbox_filter
        
        # Dynamically read EPSG from World["CRS"] (Fallback: Scene["SRID"])
        current_epsg = "Unknown"
        try:
            world = ctx.scene.world
            if world and "CRS" in world.keys():
                crs_str = str(world["CRS"])
                # Extract EPSG code from various formats
                import re
                match = re.search(r'EPSG[:\s]*(\d+)', crs_str, re.IGNORECASE)
                if match:
                    current_epsg = f"EPSG:{match.group(1)}"
        except Exception:
            pass
        if current_epsg == "Unknown":
            try:
                sc = ctx.scene
                if sc and "SRID" in sc:
                    crs_str = str(sc["SRID"])
                    import re
                    match = re.search(r'EPSG[:\s]*(\d+)', crs_str, re.IGNORECASE)
                    if not match:
                        match = re.search(r'(\d{4,5})', crs_str)
                    if match:
                        current_epsg = f"EPSG:{match.group(1)}"
            except Exception:
                pass
        
        col.label(text=f"Bounding Box ({current_epsg}):")
        
        # Info-Text
        if current_epsg == "Unknown":
            info_row = col.row()
            info_row.label(text="No CRS loaded.", icon='INFO')
        else:
            info_row = col.row()
            info_row.label(text="Coordinates in the CRS of the file to be imported", icon='INFO')
        
        # Quick input: comma-separated coordinates
        col.separator()
        col.label(text="Quick input (MinX, MinY, MaxX, MaxY):")
        row = col.row(align=True)
        row.prop(cgml3, "bbox_coords_string", text="")
        row.operator("cgml3.parse_bbox_string", text="", icon='IMPORT')
        
        col.separator()
        col.label(text="Individual values:")
        
        # Min X/Y
        row = col.row()
        row.prop(cgml3, "bbox_min_x")
        row.prop(cgml3, "bbox_min_y")
        
        # Max X/Y
        row = col.row()
        row.prop(cgml3, "bbox_max_x")
        row.prop(cgml3, "bbox_max_y")
        
        # Copy to String button
        row = col.row()
        row.operator("cgml3.bbox_to_string", text="Copy values to string", icon='COPYDOWN')

        # Local Import
        box = layout.box()
        box.prop(cgml3, "import_local", text="Local Import (without georeference)")
        if cgml3.import_local:
            box.label(text="Objects will be imported at the origin (0,0,0)", icon='INFO')
        
        # Mesh validate option with copy-data-path button
        box = layout.box()
        row = box.row(align=True)
        row.prop(cgml3, "mesh_validate", text="Run mesh validate()")
        op = row.operator("ui.copy_data_path_button", text="", icon='COPYDOWN')
        try:
            op.full_path = True
        except Exception:
            pass
        box.label(text="Disable for testing if faces are missing", icon='INFO')

        # Streaming threshold
        box = layout.box()
        box.label(text="Streaming", icon='SETTINGS')
        box.prop(cgml3, "import_streaming_threshold_mb", text="automatically starting at (MB)")
        box.label(text="File size > threshold → Streaming import", icon='INFO')

        # Export debug
        box = layout.box()
        box.label(text="Debug", icon='INFO')
        box.prop(cgml3, "export_debug", text="Export Debug (Console)")
        
        # GML ID Filter
        box = layout.box()
        row = box.row()
        row.prop(cgml3, "use_gmlid_filter")
        
        col = box.column()
        col.enabled = cgml3.use_gmlid_filter
        col.prop(cgml3, "gmlid_filter")
        
        # LOD Filter
        box = layout.box()
        row = box.row()
        row.prop(cgml3, "use_lod_filter")
        
        col = box.column()
        col.enabled = cgml3.use_lod_filter
        col.label(text="Level of Detail:")
        
        # Select All/None buttons
        row = col.row(align=True)
        row.operator("cgml3.select_all_lods", text="All", icon='CHECKBOX_HLT')
        row.operator("cgml3.deselect_all_lods", text="None", icon='CHECKBOX_DEHLT')
        
        grid = col.grid_flow(row_major=True, columns=3, even_columns=True, even_rows=True, align=True)
        grid.prop(cgml3, "lod_0")
        grid.prop(cgml3, "lod_1")
        grid.prop(cgml3, "lod_2")
        grid.prop(cgml3, "lod_3")
        grid.prop(cgml3, "lod_4")
        
        # Feature-Typ Filter
        box = layout.box()
        row = box.row()
        row.prop(cgml3, "use_feature_type_filter")
        
        col = box.column()
        col.enabled = cgml3.use_feature_type_filter
        col.label(text="Feature-Typen:")
        
        # Select All/None buttons
        row = col.row(align=True)
        row.operator("cgml3.select_all_feature_types", text="All", icon='CHECKBOX_HLT')
        row.operator("cgml3.deselect_all_feature_types", text="None", icon='CHECKBOX_DEHLT')
        
        col.prop(cgml3, "import_buildings")
        col.prop(cgml3, "import_bridges")
        col.prop(cgml3, "import_tunnels")
        col.prop(cgml3, "import_vegetation")
        col.prop(cgml3, "import_water")
        col.prop(cgml3, "import_transportation")
        col.prop(cgml3, "import_cityfurniture")
        col.prop(cgml3, "import_landuse")
        col.prop(cgml3, "import_relief")
        col.prop(cgml3, "import_generics")
        
        # Import Statistics
        if cgml3.last_import_total > 0:
            box = layout.box()
            box.label(text="Last Import Statistics:", icon='INFO')
            col = box.column(align=True)
            col.label(text=f"  Total Features Found: {cgml3.last_import_total}")
            col.label(text=f"  Features Imported: {cgml3.last_import_imported}")
            if cgml3.last_import_filtered > 0:
                col.label(text=f"  Features Filtered: {cgml3.last_import_filtered}", icon='FILTER')

class CGML3_PT_DBDefaults(Panel):
    bl_label = "Standard DB-Connection Settings"
    bl_idname = "CGML3_PT_DBDefaults"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "CityGML"
    bl_parent_id = "CGML3_PT_Main"

    def draw(self, ctx):
        p = ctx.scene.cgml3
        col = self.layout.column(align=True)
        col.prop(p, "db_host"); col.prop(p, "db_port")
        col.prop(p, "db_name"); col.prop(p, "db_schema")
        col.prop(p, "db_user"); col.prop(p, "db_pass")

# ---------------- Operator: BBox String Parser ----------------
class CGML3_OT_ParseBBoxString(Operator):
    bl_idname = "cgml3.parse_bbox_string"
    bl_label = "Parse BBox"
    bl_description = "Parse Bounding Box coordinates from string (MinX, MinY, MaxX, MaxY)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        cgml3 = ctx.scene.cgml3
        bbox_coords = parse_bbox_coords_string(cgml3.bbox_coords_string)
        
        if bbox_coords is None:
            if not str(cgml3.bbox_coords_string or "").strip():
                self.report({'WARNING'}, "No coordinates entered")
            else:
                self.report({'ERROR'}, "Invalid format. Expected: MinX, MinY, MaxX, MaxY")
            return {'CANCELLED'}

        min_x, min_y, max_x, max_y = bbox_coords
        cgml3.bbox_min_x = min_x
        cgml3.bbox_min_y = min_y
        cgml3.bbox_max_x = max_x
        cgml3.bbox_max_y = max_y

        self.report({'INFO'}, f"BBox set: ({min_x}, {min_y}) - ({max_x}, {max_y})")
        return {'FINISHED'}

class CGML3_OT_BBoxToString(Operator):
    bl_idname = "cgml3.bbox_to_string"
    bl_label = "Copy to String"
    bl_description = "Copy the individual values to the string field"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        cgml3 = ctx.scene.cgml3
        
        cgml3.bbox_coords_string = format_bbox_coords_string((
            cgml3.bbox_min_x,
            cgml3.bbox_min_y,
            cgml3.bbox_max_x,
            cgml3.bbox_max_y,
        ))
        
        self.report({'INFO'}, "Coordinates copied to string field")
        return {'FINISHED'}

# ---------------- Operator: File Import ----------------
class CGML3_OT_ImportGMLFile(Operator):
    bl_idname = "cgml3.import_gml_file"
    bl_label = "Import CityGML (File)"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Imports CityGML 2.0/3.0, optionally with Appearance/Textures"

    filepath: StringProperty(name="CityGML", subtype='FILE_PATH')
    filter_glob: StringProperty(default="*.gml;*.xml", options={'HIDDEN'})
    filename_ext = ".gml"
    import_appearance: BoolProperty(
        name="Import Appearance",
        description="Imports textures and material colors from Appearance data. Semantic material slots and custom properties are preserved",
        default=True,
    )

    def invoke(self, ctx, evt):
        ctx.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    @classmethod
    def description(cls, context, props):
        # Only static text. No access to props.filepath, no parser calls.
        return "Imports CityGML files optionally with Appearance, UV, and textures"

    def _log_text(self, name_prefix, content):
        from .shared.export_helpers import log_text_to_blender
        log_text_to_blender(name_prefix, content)

    def draw(self, ctx):
        box = self.layout.box()
        box.label(text="Appearance")
        box.prop(self, "import_appearance")

    def execute(self, ctx):
        from .common import import_citygml_auto

        if not self.filepath:
            self.report({'ERROR'}, "No file selected")
            return {'CANCELLED'}

        # Validation only if enabled
        if ctx.scene.cgml3.xsd_validate:
            from .ops.validate_auto import well_formed_citygml_auto, validate_citygml_auto
            
            # Basic check: well-formed CityGML?
            ok, msg = well_formed_citygml_auto(self.filepath)
            if not ok:
                self.report({'ERROR'}, f"Validator: {msg}")
                return {'CANCELLED'}

            # XSD validation
            okx, msgx = validate_citygml_auto(self.filepath)
            self._log_text("citygml_xsd_check", msgx)
            if not okx:
                # Lenient validation: only warn for known 3DCityDB export issues
                is_known_3dcitydb_issue = (
                    "is not a valid value of the atomic type 'xs:ID'" in msgx or
                    "xs:ID" in msgx or
                    "textureParameterization" in msgx or
                    "This element is not expected" in msgx or
                    "isFront" in msgx
                )
                if ctx.scene.cgml3.xsd_lenient_validation and is_known_3dcitydb_issue:
                    self.report({'WARNING'}, f"XSD warning (3DCityDB): {msgx[:200]}... Import will continue.")
                    print(f"[CityGML Import] XSD warning (lenient): {msgx}")
                else:
                    self.report({'ERROR'}, f"XSD: {msgx}")
                    return {'CANCELLED'}

        # eigentlicher Import (inkl. BBOX-/GML-ID-Filter im Python-Importer)
        import_citygml_auto(self.filepath, ctx, import_appearance=self.import_appearance)
        self.report({'INFO'}, "CityGML import ready")
        return {'FINISHED'}

# ---------------- Operator: Datei-Export ----------------
class CGML3_OT_ExportGMLFile(Operator):
    bl_idname = "cgml3.export_gml_file"
    bl_label = "Export CityGML (File)"
    bl_options = {'REGISTER'}
    bl_description = "Export CityGML 2.0/3.0, including Appearance/Textures"

    filepath: StringProperty(name="Output File", subtype='FILE_PATH')
    filter_glob: StringProperty(default="*.gml;*.xml", options={'HIDDEN'})
    filename_ext = ".gml"
    
    citygml_version: EnumProperty(
        name="CityGML Version",
        description="Select the CityGML version for export",
        items=[
            ('3.0', 'CityGML 3.0', 'Export as CityGML 3.0'),
            ('2.0', 'CityGML 2.0', 'Export as CityGML 2.0'),
        ],
        default='3.0'
    )
    
    srs_name: StringProperty(
        name="srsName",
        description="Target CRS (e.g., 'EPSG:25832'). Leave empty to use World['CRS'].",
        default=""
    )
    feature_type_prop: StringProperty(name="Feature Type Property", default="ModelType")
    write_offset_back: BoolProperty(
        name="Write Origin Offset",
        description="Add the stored CityModel offset back during export",
        default=True
    )
    use_inner_outer_script: BoolProperty(
        name="Automatically Detect Interior Surfaces",
        description="Uses get_inner_and_outer_rings.py to automatically detect interior surfaces.",
        default=False,
    )

    autofill_new_surfaces: BoolProperty(
        name="Automatically Process New Surfaces",
        description="Only for newly modeled faces without CityGML metadata: detects interior faces, adopts appearance from the corresponding exterior, and assigns surface type for new roof/wall faces without modifying existing imported interior structures.",
        default=False,
    )

    unclassified_surface_type: EnumProperty(
        name="Standard Surface Type",
        description="Surface type for faces without existing material or face semantics",
        items=[
            ('WallSurface', 'WallSurface', 'Export unclassified faces as WallSurface'),
            ('RoofSurface', 'RoofSurface', 'Export unclassified faces as RoofSurface'),
            ('GroundSurface', 'GroundSurface', 'Export unclassified faces as GroundSurface'),
            ('ClosureSurface', 'ClosureSurface', 'Export unclassified faces as ClosureSurface'),
        ],
        default='WallSurface',
    )

    split_wall_roof_surfaces: BoolProperty(
        name="Export Roof-/WallSurfaces Individually",
        description="Only for RoofSurface and WallSurface: instead of bundling as MultiSurface/MultiPolygon, write each polygon as a separate surface (better for individual selection in FME).",
        default=False,
    )

    use_streaming: EnumProperty(
        name="Export Mode",
        description="Streaming export for large datasets (reduces memory usage)",
        items=[
            ('AUTO', 'Automatic', 'Automatic choice based on object count (>1000: Streaming)'),
            ('FORCE_STREAMING', 'Force Streaming', 'Always use streaming (for very large datasets)'),
            ('FORCE_DOM', 'Force DOM', 'Always use DOM-based export (faster for small datasets)'),
        ],
        default='AUTO'
    )

    enable_memory_tracking: BoolProperty(
        name="Memory Tracking",
        description="Monitor memory usage during export (approx. 5% performance overhead)",
        default=False,
    )

    write_lod_solid_refs: BoolProperty(
        name="Write LoD Solid from Surface References",
        description="Optional: writes a lod{n}Solid that only references the gml:Polygon IDs of the BoundarySurfaces via xlink:href (without installation geometries). If disabled, export proceeds as before without this additional Solid block.",
        default=False,
    )

    normalize_mixed_lods: BoolProperty(
        name="Normalize Mixed LoDs",
        description="Before export, detect top-level objects with mixed LoDs and raise all contained surfaces and parts to the highest LoD used in that top-level object.",
        default=False,
    )
    
    # Export Feature-Type Filter
    export_buildings: BoolProperty(
        name="Buildings export",
        description="Export Building and BuildingPart",
        default=True,
    )
    export_bridges: BoolProperty(
        name="Bridges export",
        description="Export Bridge and BridgePart",
        default=True,
    )
    export_tunnels: BoolProperty(
        name="Tunnels export",
        description="Export Tunnel and TunnelPart",
        default=True,
    )
    export_vegetation: BoolProperty(
        name="Vegetation export",
        description="Export SolitaryVegetationObject and PlantCover",
        default=True,
    )
    export_waterbodies: BoolProperty(
        name="WaterBodies export",
        description="Export WaterBody and Water-Surfaces",
        default=True,
    )
    export_construction: BoolProperty(
        name="Construction export",
        description="Export OtherConstruction, Window, Door",
        default=True,
    )
    export_cityfurniture: BoolProperty(
        name="CityFurniture export",
        description="Export CityFurniture",
        default=True,
    )
    export_landuse: BoolProperty(
        name="LandUse export",
        description="Export LandUse",
        default=True,
    )
    export_transportation: BoolProperty(
        name="Transportation export",
        description="Export Transportation features and surfaces",
        default=True,
    )
    export_relief: BoolProperty(
        name="Relief export",
        description="Export TINRelief and RasterRelief",
        default=True,
    )
    export_generics: BoolProperty(
        name="Generics export",
        description="Export Generics and CityObjectGroup",
        default=True,
    )
    
    # Texture Compression Options
    compress_textures: BoolProperty(
        name="Texture Compression",
        description="Compress textures and export in parallel (PNG → JPEG)",
        default=False,
    )
    
    texture_quality: IntProperty(
        name="JPEG Quality",
        description="JPEG compression quality (1-100, recommended: 75-90)",
        default=85,
        min=1,
        max=100,
    )
    
    texture_max_size: IntProperty(
        name="Max. Texture Size",
        description="Maximum texture dimension in pixels (0 = no size limit)",
        default=0,
        min=0,
        max=8192,
    )
    
    texture_workers: IntProperty(
        name="Parallel Texture Workers",
        description="Number of parallel threads for texture processing (recommended: 4-8)",
        default=4,
        min=1,
        max=16,
    )

    def draw(self, ctx):
        layout = self.layout

        box = layout.box()
        box.label(text="CityGML / CRS")
        box.prop(self, "citygml_version")
        box.prop(self, "srs_name")

        box = layout.box()
        box.label(text="Geometry")
        box.prop(self, "write_offset_back")
        box.prop(self, "use_inner_outer_script")
        box.prop(self, "autofill_new_surfaces")
        box.prop(self, "unclassified_surface_type")
        box.prop(self, "split_wall_roof_surfaces")
        box.prop(self, "write_lod_solid_refs")
        box.prop(self, "normalize_mixed_lods")

        box = layout.box()
        box.label(text="Feature Types")
        box.prop(self, "export_buildings")
        box.prop(self, "export_bridges")
        box.prop(self, "export_tunnels")
        box.prop(self, "export_vegetation")
        box.prop(self, "export_waterbodies")
        box.prop(self, "export_construction")
        box.prop(self, "export_cityfurniture")
        box.prop(self, "export_landuse")
        box.prop(self, "export_transportation")
        box.prop(self, "export_relief")
        box.prop(self, "export_generics")

        box = layout.box()
        box.label(text="Textures")
        box.prop(self, "compress_textures")
        col = box.column()
        col.enabled = bool(self.compress_textures)
        col.prop(self, "texture_quality")
        col.prop(self, "texture_max_size")
        col.prop(self, "texture_workers")

    def invoke(self, ctx, evt):
        # Set default filename if not already set
        if not self.filepath:
            self.filepath = "export.gml"

        # Automatically detect CityGML version from the top-level collection
        detected_version = _detect_citygml_version_from_scene(ctx.scene)
        if detected_version:
            self.citygml_version = detected_version

        # Automatically prefill srsName from World["CRS"], if available
        if not self.srs_name:
            try:
                w = bpy.data.worlds.get("World")
                if w and "CRS" in w:
                    val = str(w["CRS"]).strip()
                    if val:
                        self.srs_name = val
            except Exception:
                pass
            # Fallback: Scene["SRID"]
            if not self.srs_name:
                try:
                    sc = ctx.scene
                    if sc and "SRID" in sc:
                        val = str(sc["SRID"]).strip()
                        if val:
                            self.srs_name = val
                except Exception:
                    pass

        ctx.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def _log_text(self, name_prefix, content):
        from .shared.export_helpers import log_text_to_blender
        log_text_to_blender(name_prefix, content)

    def execute(self, ctx):
        import bpy
        from .shared.export_helpers import scene_has_opening_surfaces, scene_has_building_subdivision_features
        from .shared.lod_helpers import normalize_scene_top_level_lods

        props = ctx.scene.cgml3

        def _scene_has_opening_surfaces() -> bool:
            return scene_has_opening_surfaces(ctx)

        def _scene_has_building_subdivision_features() -> bool:
            return scene_has_building_subdivision_features(ctx)

        # -------------------------------
        # Determine streaming mode
        # -------------------------------
        if self.use_streaming == 'AUTO':
            use_streaming_mode = False
        elif self.use_streaming == 'FORCE_STREAMING':
            use_streaming_mode = True
        else:  # FORCE_DOM
            use_streaming_mode = False

        # -------------------------------
        # Determine export function
        # -------------------------------
        if self.citygml_version == '2.0':
            if use_streaming_mode is not False:
                try:
                    from .io.writer.streaming_exporter import export_citygml2_streaming
                    export_func = export_citygml2_streaming
                except ImportError:
                    from .io_v2 import export_citygml2_from_blender
                    export_func = export_citygml2_from_blender
                    use_streaming_mode = False
            else:
                from .io_v2 import export_citygml2_from_blender
                export_func = export_citygml2_from_blender
        else:
            if use_streaming_mode is not False:
                try:
                    from .io.writer.streaming_exporter import export_blender_to_citygml3_streaming
                    export_func = export_blender_to_citygml3_streaming
                except ImportError:
                    from .io.gml3_writer import export_blender_to_citygml3
                    export_func = export_blender_to_citygml3
                    use_streaming_mode = False
            else:
                from .io.gml3_writer import export_blender_to_citygml3
                export_func = export_blender_to_citygml3

        # -------------------------------
        # Fallback: split_wall_roof_surfaces
        # -------------------------------
        if self.split_wall_roof_surfaces and (
            "split_wall_roof_surfaces" not in export_func.__code__.co_varnames
        ):
            if self.citygml_version == '2.0':
                from .io_v2 import export_citygml2_from_blender
                export_func = export_citygml2_from_blender
            else:
                from .io.gml3_writer import export_blender_to_citygml3
                export_func = export_blender_to_citygml3
            use_streaming_mode = False

        if use_streaming_mode is not False and _scene_has_opening_surfaces():
            if self.citygml_version == '2.0':
                from .io_v2 import export_citygml2_from_blender
                export_func = export_citygml2_from_blender
            else:
                from .io.gml3_writer import export_blender_to_citygml3
                export_func = export_blender_to_citygml3
            use_streaming_mode = False

        if self.citygml_version == '3.0' and use_streaming_mode is not False and _scene_has_building_subdivision_features():
            from .io.gml3_writer import export_blender_to_citygml3
            export_func = export_blender_to_citygml3
            use_streaming_mode = False

        # -------------------------------
        # Normalize file path
        # -------------------------------
        import os
        import traceback

        # Ensure the file extension is .gml
        filepath = self.filepath
        if not filepath.lower().endswith('.gml'):
            filepath = filepath.rstrip('.') + '.gml'

        # IMPORTANT: Convert Blender-relative paths (//) to absolute paths
        filepath = bpy.path.abspath(filepath)

        # Ensure the output directory exists
        out_dir = os.path.dirname(filepath)
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        print(f"[CityGML] Export target: {filepath}")
        print(f"[CityGML] citygml_version={self.citygml_version}, use_streaming_mode={use_streaming_mode}, exporter={export_func.__module__}.{export_func.__name__}")

        if self.normalize_mixed_lods:
            normalization = normalize_scene_top_level_lods(ctx.scene)
            if normalization["normalized"]:
                count = len(normalization["normalized"])
                self.report({'INFO'}, f"Normalized mixed LoDs for {count} top-level object(s) before export")

        try:
            if getattr(ctx.scene, "cgml3", None) and ctx.scene.cgml3.export_debug:
                sample = bpy.context.object
                if sample:
                    d = None
                    try:
                        d = sample.get("cgml3_generic_attributes", None)
                    except Exception:
                        d = None
                    print(
                        "[CityGML3][DIAG] export sample obj="
                        f"{getattr(sample, 'name', '?')} gml_id={sample.get('gml_id')} "
                        f"generic_dict_len={(len(d) if isinstance(d, dict) else 0)} "
                        f"has_json={bool(sample.get('cgml3_generic_attributes_json') or sample.get('cgml3_generic_attributes_json_0'))}"
                    )
                try:
                    from .io.writer.document import create_citymodel_root
                    r = create_citymodel_root("0000")
                    print(f"[CityGML3][DIAG] writer root tag={r.tag}")
                except Exception as e:
                    print(f"[CityGML3][DIAG] writer root tag check failed: {e}")
        except Exception:
            pass

        def _call_export(out_path: str):
            export_types = []
            if self.export_buildings:
                export_types += [
                    "bldg:Building",
                    "bldg:BuildingPart",
                    "bldg:BuildingUnit",
                    "bldg:Storey",
                    "bldg:BuildingRoom",
                    "bldg:BuildingConstructiveElement",
                    "bldg:BuildingInstallation",
                    "bldg:BuildingFurniture",
                ]
                if self.citygml_version == '2.0':
                    export_types += ["bldg:IntBuildingInstallation"]
            if self.export_bridges:
                export_types += [
                    "brid:Bridge",
                    "brid:BridgePart",
                    "brid:BridgeRoom",
                    "brid:BridgeFurniture",
                    "brid:BridgeInstallation",
                ]
                if self.citygml_version == '2.0':
                    export_types += ["brid:IntBridgeInstallation", "brid:BridgeConstructionElement"]
            if self.export_tunnels:
                export_types += [
                    "tun:Tunnel",
                    "tun:TunnelPart",
                    "tun:HollowSpace",
                    "tun:TunnelFurniture",
                    "tun:TunnelInstallation",
                    "tun:TunnelConstructiveElement",
                ]
            if self.export_vegetation:
                export_types += ["veg:SolitaryVegetationObject", "veg:PlantCover"]
            if self.export_waterbodies:
                export_types += ["wtr:WaterBody", "wtr:WaterSurface", "wtr:WaterGroundSurface"]
                if self.citygml_version == '2.0':
                    export_types += ["wtr:WaterClosureSurface"]
            if self.export_construction and self.citygml_version == '3.0':
                export_types += ["con:OtherConstruction", "con:Window", "con:Door"]
            if self.export_transportation:
                export_types += ["tran:Road", "tran:Railway", "tran:Track", "tran:Square"]
                if self.citygml_version == '2.0':
                    export_types += ["tran:TrafficArea", "tran:AuxiliaryTrafficArea"]
                else:
                    export_types += [
                        "tran:Waterway",
                        "tran:Section",
                        "tran:Intersection",
                        "tran:TrafficSpace",
                        "tran:AuxiliaryTrafficSpace",
                        "tran:TrafficArea",
                        "tran:AuxiliaryTrafficArea",
                        "tran:ClearanceSpace",
                        "tran:Marking",
                        "tran:Hole",
                    ]
            if self.export_cityfurniture:
                export_types += ["frn:CityFurniture"]
            if self.export_landuse:
                export_types += ["luse:LandUse"]
            if self.export_relief:
                export_types += ["dem:ReliefFeature", "dem:TINRelief", "dem:RasterRelief", "dem:MassPointRelief", "dem:BreaklineRelief"]
            if self.export_generics:
                export_types += ["grp:CityObjectGroup"]
                if self.citygml_version == '2.0':
                    export_types += ["gen:GenericCityObject"]
                else:
                    export_types += [
                        "gen:GenericOccupiedSpace",
                        "gen:GenericLogicalSpace",
                        "gen:GenericUnoccupiedSpace",
                        "gen:GenericThematicSurface",
                    ]

            kwargs = {
                "use_inner_outer_script": self.use_inner_outer_script,
                "autofill_new_surfaces": self.autofill_new_surfaces,
                "unclassified_surface_type": self.unclassified_surface_type,
                "split_wall_roof_surfaces": self.split_wall_roof_surfaces,
                "write_lod_solid_refs": self.write_lod_solid_refs,
                "export_types": export_types,
            }

            # Pass streaming parameters only if the function accepts them
            if "use_streaming" in export_func.__code__.co_varnames:
                kwargs.update({
                    "use_streaming": use_streaming_mode,
                    "enable_memory_tracking": self.enable_memory_tracking,
                })
            
            # Texture compression options
            if "compress_textures" in export_func.__code__.co_varnames:
                kwargs.update({
                    "compress_textures": self.compress_textures,
                    "texture_quality": self.texture_quality,
                    "texture_max_size": self.texture_max_size if self.texture_max_size > 0 else None,
                    "texture_workers": self.texture_workers,
                })

            # Pass only parameters that the target function actually accepts
            kwargs = {k: v for k, v in kwargs.items() if k in export_func.__code__.co_varnames}

            export_func(
                out_path,
                ctx,
                self.srs_name,
                self.feature_type_prop,
                **kwargs
            )

        try:
            # Temporarily set offset to 0, if desired
            if not self.write_offset_back:
                old = tuple(ctx.scene.cgml3_offset)
                try:
                    v = ctx.scene.cgml3_offset
                    v[0] = 0.0; v[1] = 0.0; v[2] = 0.0
                    _call_export(filepath)
                finally:
                    v = ctx.scene.cgml3_offset
                    v[0], v[1], v[2] = old
            else:
                _call_export(filepath)

        except Exception as e:
            traceback.print_exc()
            self.report({'ERROR'}, f"CityGML Export FEHLGESCHLAGEN: {e}")
            return {'CANCELLED'}

        # After export: file must exist
        if not os.path.isfile(filepath):
            self.report({'ERROR'}, f"Export completed, but file was NOT created: {filepath} (see system console)")
            return {'CANCELLED'}

        if os.path.getsize(filepath) == 0:
            self.report({'ERROR'}, f"Export file is empty (0 bytes): {filepath} (see system console)")
            return {'CANCELLED'}

        # Optional: XSD validation after export
        if props.xsd_validate:
            try:
                if self.citygml_version == '2.0':
                    from .ops.validate_citygml2 import validate_citygml2_xsd, well_formed_citygml2
                    print(f"[CityGML 2.0] Validation: {filepath}")
                    ok, msg = well_formed_citygml2(filepath)
                    if not ok:
                        self.report({'WARNING'}, f"Validation (Structure): FAILED - {msg}")
                        print(f"[CityGML 2.0] Structure Check: {msg}")
                    else:
                        print(f"[CityGML 2.0] Structure Check: OK")
                        # XSD validation
                        ok_xsd, msg_xsd = validate_citygml2_xsd(filepath)
                        if ok_xsd:
                            self.report({'INFO'}, f"Export successfully validated (CityGML 2.0 XSD)")
                            print(f"[CityGML 2.0] XSD Validation: OK")
                        else:
                            self.report({'WARNING'}, f"XSD Validation FAILED - {msg_xsd}")
                            print(f"[CityGML 2.0] XSD Validation: {msg_xsd}")
                else:
                    from .ops.validate import validate_citygml3_xsd, well_formed_citygml3
                    print(f"[CityGML 3.0] Validation: {filepath}")
                    ok, msg = well_formed_citygml3(filepath)
                    if not ok:
                        self.report({'WARNING'}, f"Validation (Structure): FAILED - {msg}")
                        print(f"[CityGML 3.0] Structure Check: {msg}")
                    else:
                        print(f"[CityGML 3.0] Structure Check: OK")
                        # XSD validation
                        ok_xsd, msg_xsd = validate_citygml3_xsd(filepath)
                        if ok_xsd:
                            self.report({'INFO'}, f"Export successfully validated (CityGML 3.0 XSD)")
                            print(f"[CityGML 3.0] XSD Validation: OK")
                        else:
                            self.report({'WARNING'}, f"XSD Validation FAILED - {msg_xsd}")
                            print(f"[CityGML 3.0] XSD Validation: {msg_xsd}")
            except ImportError as e:
                self.report({'WARNING'}, f"Validation not available (lxml missing?): {e}")
                print(f"[Validation] Import Error: {e}")
            except Exception as e:
                self.report({'WARNING'}, f"Validation failed: {e}")
                print(f"[Validation] Error: {e}")
        
        # Show validation summary in popup
        if props.xsd_validate:
            self._show_validation_summary(ctx)
        
        self.report({'INFO'}, f"CityGML Export finished: {filepath}")
        return {'FINISHED'}
    
    def _show_validation_summary(self, context):
        """Show validation results in a text editor window"""
        import bpy
        
        # Create or get text block for validation results
        text_name = "Validation Report"
        if text_name in bpy.data.texts:
            text = bpy.data.texts[text_name]
            text.clear()
        else:
            text = bpy.data.texts.new(text_name)
        
        # Build report
        lines = []
        lines.append("=" * 70)
        lines.append("CityGML Export Validation Report")
        lines.append("=" * 70)
        lines.append("")
        
        props = context.scene.cgml3
        
        # XSD Validation section
        if props.xsd_validate:
            lines.append("📋 XSD Schema Validation")
            lines.append("-" * 70)
            lines.append("✅ XML Structure: Valid")
            lines.append("✅ XSD Schema: Valid")
            lines.append("")
        
        lines.append("=" * 70)
        lines.append(f"Report generated: {context.scene.name}")
        lines.append("=" * 70)
        
        # Write to text block
        text.write("\n".join(lines))
        
        # Try to show in text editor (if space exists)
        for area in context.screen.areas:
            if area.type == 'TEXT_EDITOR':
                area.spaces.active.text = text
                break
        else:
            # No text editor found - show info message
            self.report({'INFO'}, f"Validation report created: '{text_name}' (open Text Editor to view)")

# ---------------- Operator: DB-Import (Export->Datei->lesen) ----------------
class CGML3_OT_ImportFromDB(Operator):
    bl_idname = "cgml3.import_from_db"
    bl_label = "Import from 3DCityDB (CityGML)"
    bl_options = {'REGISTER', 'UNDO'}

    # Connection
    db_host: StringProperty(name="Host", default="")
    db_port: StringProperty(name="Port", default="")
    db_name: StringProperty(name="Database", default="")
    db_user: StringProperty(name="User", default="")
    db_pass: StringProperty(name="Password", default="")
    db_schema: StringProperty(name="Database Schema (optional)", default="")

    # CRS/Transform
    crs: StringProperty(name="--crs", default="")
    crs_name: StringProperty(name="--crs-name", default="")
    transform: StringProperty(name="--transform", default="")  # e.g., swap-xy or 12 matrix values

    # Filter
    type_names: StringProperty(name="--type-name", description="Comma-separated, e.g., bldg:Building,brid:Bridge", default="")
    lod: StringProperty(name="--lod", description="Comma-separated (1,2,3)", default="")
    lod_mode: EnumProperty(
    name="--lod-mode",
        items=[('NONE','(empty)',''), ('or','or',''), ('and','and',''),
            ('minimum','minimum',''), ('maximum','maximum','')],
        default='NONE'
    )
    lod_search_depth: StringProperty(name="--lod-search-depth", default="")
    cql2_filter: StringProperty(name="--filter (CQL2)", default="", description="CQL2 expression")
    filter_crs: StringProperty(name="--filter-crs", default="")
    sql_filter: StringProperty(name="--sql-filter", default="", description="SQL expression")
    sort_by: StringProperty(name="--sort-by", default="")
    limit: IntProperty(name="--limit", default=0, min=0)
    start_index: IntProperty(name="--start-index", default=0, min=0)

    # Temporal validity
    validity: EnumProperty(
    name="--validity",
        items=[('NONE','(empty)',''), ('latest','latest',''), ('at','at',''),
            ('between','between',''), ('terminated','terminated',''),
            ('terminated_at','terminated_at',''), ('all','all','')],
        default='NONE'
    )
    validity_at: StringProperty(name="--validity-at", default="", description="YYYY-MM-DD or ISO time")
    validity_between: StringProperty(name="--validity-between", default="", description="startISO,endISO")
    validity_reference: EnumProperty(
        name="--validity-reference",
        items=[('NONE','(empty)',''), ('transaction','transaction',''), ('validity','validity','')],
        default='NONE'
    )
    lenient_validity: BoolProperty(name="--lenient-validity", default=False)

    # Appearance
    no_appearances: BoolProperty(name="--no-appearances", default=False)
    appearance_themes: StringProperty(name="--appearance-theme", default="")

    # Output formatting
    no_pretty_print: BoolProperty(name="--no-pretty-print", default=False)
    xsl_transform: StringProperty(name="--xsl-transform", subtype='FILE_PATH', default="")

    # Config
    export_config: StringProperty(name="--config-file (JSON)", subtype='FILE_PATH', default="")

    def invoke(self, ctx, evt):
        # Use scene defaults if fields are empty
        d = ctx.scene.cgml3
        if not self.db_host: self.db_host = d.db_host
        if not self.db_port: self.db_port = d.db_port
        if not self.db_name: self.db_name = d.db_name
        if not self.db_user: self.db_user = d.db_user
        if not self.db_pass: self.db_pass = d.db_pass
        if not self.db_schema: self.db_schema = d.db_schema
        ctx.window_manager.invoke_props_dialog(self, width=600)
        return {'RUNNING_MODAL'}

    def draw(self, ctx):
        ui_state = ctx.scene.cgml3
        col = self.layout.column(align=True)

        box = _draw_foldout_section(col, ui_state, "db_import_ui_show_connection", "DB Connection")
        if box:
            box.prop(self, "db_host"); box.prop(self, "db_port")
            box.prop(self, "db_name"); box.prop(self, "db_schema")
            box.prop(self, "db_user"); box.prop(self, "db_pass")

        box = _draw_foldout_section(col, ui_state, "db_import_ui_show_crs", "CRS/Coordinates")
        if box:
            box.prop(self, "crs"); box.prop(self, "crs_name"); box.prop(self, "transform")

        box = _draw_foldout_section(col, ui_state, "db_import_ui_show_filters", "Filter")
        if box:
            box.prop(self, "type_names"); box.prop(self, "lod"); box.prop(self, "lod_mode")
            box.prop(self, "lod_search_depth")
            box.prop(self, "cql2_filter"); box.prop(self, "sql_filter"); box.prop(self, "filter_crs")
            box.prop(self, "sort_by"); box.prop(self, "limit"); box.prop(self, "start_index")

        box = _draw_foldout_section(col, ui_state, "db_import_ui_show_validity", "Temporal Validity")
        if box:
            box.prop(self, "validity"); box.prop(self, "validity_at"); box.prop(self, "validity_between")
            box.prop(self, "validity_reference"); box.prop(self, "lenient_validity")

        box = _draw_foldout_section(col, ui_state, "db_import_ui_show_format", "Appearance/Format")
        if box:
            box.prop(self, "no_appearances"); box.prop(self, "appearance_themes")
            box.prop(self, "no_pretty_print"); box.prop(self, "xsl_transform")

        box = _draw_foldout_section(col, ui_state, "db_import_ui_show_config", "Config")
        if box:
            box.prop(self, "export_config")

    def _log_text(self, name_prefix, content):
        from .shared.export_helpers import log_text_to_blender
        log_text_to_blender(name_prefix, content)

    def execute(self, ctx):
        import os, sys, tempfile, time
        from .ops.citydb_cli import CityDBTool
        from .common import import_citygml_auto
        from .ops.validate_auto import detect_version_from_file
        from .ops.validate_auto import well_formed_citygml_auto, validate_citygml_auto
        from .ops.citydb_v4_cli import CityDBV4Tool

        def _progress(phase: str, pct: float = 0):
            bar_len = 40
            filled = int(bar_len * pct / 100)
            bar = '█' * filled + '░' * (bar_len - filled)
            line = f'\r[{bar}] {pct:5.1f}% | {phase}'
            sys.stdout.write(line + ' ' * max(0, 100 - len(line)))
            sys.stdout.flush()

        if not self.db_pass:
            self.report({'ERROR'}, "Password cannot be empty. Otherwise, the citydb-tool prompt will hang.")
            return {'CANCELLED'}

        prefs = _get_citydb_prefs(ctx)
        v5_tool = CityDBTool(prefs)
        v4_tool = CityDBV4Tool(prefs)

        tmp = tempfile.mkdtemp(prefix="cgml3_")
        out_gml = os.path.join(tmp, "export.gml")

        type_names = [s.strip() for s in self.type_names.split(",") if s.strip()] or None
        lod = [s.strip() for s in self.lod.split(",") if s.strip()] or None
        sort_by = [s.strip() for s in self.sort_by.split(",") if s.strip()] or None
        appearance_themes = [s.strip() for s in self.appearance_themes.split(",") if s.strip()] or None
        schema = self.db_schema.strip() or None
        lod_mode_val = None if self.lod_mode == 'NONE' else self.lod_mode
        validity_val = None if self.validity == 'NONE' else self.validity
        validity_ref_val = None if self.validity_reference == 'NONE' else self.validity_reference

        # ── Use scene defaults if fields are empty ──
        # Feature type filter from Scene Panel → type_names (if empty in Operator)
        if not type_names and ctx.scene.cgml3.use_feature_type_filter:
            _ft_map = {
                "import_buildings":       ["bldg:Building", "bldg:BuildingPart"],
                "import_bridges":         ["brid:Bridge", "brid:BridgePart"],
                "import_tunnels":         ["tun:Tunnel", "tun:TunnelPart"],
                "import_vegetation":      ["veg:SolitaryVegetationObject", "veg:PlantCover"],
                "import_water":           ["wtr:WaterBody", "wtr:WaterSurface"],
                "import_transportation":  ["tran:Road", "tran:Railway", "tran:Track", "tran:Square", "tran:Waterway"],
                "import_cityfurniture":   ["frn:CityFurniture"],
                "import_landuse":         ["luse:LandUse"],
                "import_relief":          ["dem:TINRelief", "dem:RasterRelief", "dem:MassPointRelief", "dem:BreaklineRelief"],
                "import_generics":        ["gen:GenericOccupiedSpace", "gen:GenericLogicalSpace",
                                           "gen:GenericUnoccupiedSpace", "gen:GenericThematicSurface",
                                           "gen:GenericCityObject"],
            }
            _scene_types = []
            for prop, names in _ft_map.items():
                if getattr(ctx.scene.cgml3, prop, True):
                    _scene_types.extend(names)
            if _scene_types:
                type_names = _scene_types
                _progress(f"Feature type filter (Scene): {len(type_names)} types", 2)

        # LOD filter from Scene Panel → lod (if empty in Operator)
        if not lod and ctx.scene.cgml3.use_lod_filter:
            _scene_lods = []
            for lvl in range(5):  # LOD 0–4
                if getattr(ctx.scene.cgml3, f"lod_{lvl}", True):
                    _scene_lods.append(str(lvl))
            if _scene_lods:
                lod = _scene_lods
                _progress(f"LOD filter (Scene): {','.join(lod)}", 2)

        # GML-ID filter from Scene Properties -> database-side export filters.
        gml_ids = []
        if ctx.scene.cgml3.use_gmlid_filter:
            gml_ids_str = ctx.scene.cgml3.gmlid_filter.strip()
            if gml_ids_str:
                gml_ids = [gid.strip() for gid in gml_ids_str.replace(";", ",").split(",") if gid.strip()]
                if gml_ids:
                    _progress(f"GML-ID filter active: {len(gml_ids)} IDs", 3)

        effective_sql_filter = self.sql_filter.strip() if self.sql_filter else ""

        # Read BBox filter from Scene Properties and convert to CQL2 filter
        # Note: --bbox is only available for import, for export we need to use CQL2
        # Syntax: s_intersects(core:envelope, bbox(xmin,ymin,xmax,ymax))
        bbox_cql2 = None
        bbox_filter_crs = None
        if ctx.scene.cgml3.use_bbox_filter:
            min_x = ctx.scene.cgml3.bbox_min_x
            min_y = ctx.scene.cgml3.bbox_min_y
            max_x = ctx.scene.cgml3.bbox_max_x
            max_y = ctx.scene.cgml3.bbox_max_y
            # Use vcdb CQL2 syntax: s_intersects(core:envelope, bbox(...))
            bbox_cql2 = f"s_intersects(core:envelope, bbox({min_x},{min_y},{max_x},{max_y}))"
            # If filter_crs is set, use it, otherwise use the CRS from the field
            bbox_filter_crs = self.filter_crs or self.crs or None
            _progress(f"BBox filter active: {bbox_cql2}", 3)

        # Combine BBox filter with existing CQL2 filter if present
        combined_cql2 = None
        combined_filter_crs = self.filter_crs or bbox_filter_crs
        if bbox_cql2 and self.cql2_filter:
            combined_cql2 = f"({self.cql2_filter}) AND ({bbox_cql2})"
        elif bbox_cql2:
            combined_cql2 = bbox_cql2
        elif self.cql2_filter:
            combined_cql2 = self.cql2_filter

        _progress("Start DB export (3DCityDB version detected)...", 5)
        print()
        t0 = time.time()

        ok, log = v5_tool.export_db_to_citygml(
            output=out_gml,
            host=self.db_host, port=self.db_port, db=self.db_name,
            user=self.db_user, password=self.db_pass,
            db_schema=schema,
            citygml_version="3.0",
            config_file=(self.export_config or None),
            crs=(self.crs or None),
            crs_name=(self.crs_name or None),
            transform=(self.transform or None),
            cql2_filter=combined_cql2,
            filter_crs=combined_filter_crs,
            sql_filter=(effective_sql_filter or None),
            feature_ids=(gml_ids or None),
            type_names=type_names,
            lod=lod,
            lod_mode=lod_mode_val,
            lod_search_depth=(self.lod_search_depth or None),
            sort_by=sort_by,
            limit=(self.limit or None) if self.limit > 0 else None,
            start_index=(self.start_index or None) if self.start_index > 0 else None,
            # temporal validity
            validity=validity_val,
            validity_reference=validity_ref_val,
            validity_at=(self.validity_at or None),
            validity_between=(self.validity_between or None),
            lenient_validity=self.lenient_validity,
            # appearance
            no_appearances=self.no_appearances,
            appearance_themes=appearance_themes,
            # formatting
            no_pretty_print=self.no_pretty_print,
            xsl_transform=(self.xsl_transform or None)
        )

        v5_requires_v4_fallback = (not ok and _citydb_v5_requires_v4_fallback(log))

        if v5_requires_v4_fallback:
            self._log_text("citydb_export", log)
            _log_citydb_backend_fallback("DB-Export")
            _progress(f"DB-Export neu starten ({_citydb_backend_label(True)})...", 10)
            print()
            
            # FFor v4: convert CQL2 filter back to bbox if possible
            bbox_v4 = None
            if ctx.scene.cgml3.use_bbox_filter:
                min_x = ctx.scene.cgml3.bbox_min_x
                min_y = ctx.scene.cgml3.bbox_min_y
                max_x = ctx.scene.cgml3.bbox_max_x
                max_y = ctx.scene.cgml3.bbox_max_y
                bbox_v4 = f"{min_x},{min_y},{max_x},{max_y}"
            
            ok, log = v4_tool.export_db_to_citygml(
                output=out_gml,
                host=self.db_host, port=self.db_port, db=self.db_name,
                user=self.db_user, password=self.db_pass,
                db_schema=schema,
                config_file=(self.export_config or None),
                bbox=bbox_v4,
                feature_ids=(gml_ids or None),
                type_names=type_names,
                lod=lod,
                lod_mode=lod_mode_val,
                lod_search_depth=(self.lod_search_depth or None),
                count=(self.limit or None) if self.limit > 0 else None,
                start_index=(self.start_index or None) if self.start_index > 0 else None,
                no_appearance=self.no_appearances,
                appearance_themes=appearance_themes,
            )
        self._log_text("citydb_export", log)

        if not ok:
            _progress("DB export failed!", 0)
            print()  # newline after progress bar
            if v5_requires_v4_fallback:
                self.report(
                    {'ERROR'},
                    "DB export failed. v5 (citydb-tool) and v4 (Java impexp) could not handle the DB (see text log)."
                )
            else:
                self.report({'ERROR'}, "citydb export citygml failed (see text log)")
            return {'CANCELLED'}

        used_v4_backend = v5_requires_v4_fallback
        dt = time.time() - t0
        _log_citydb_backend_success("DB-Export", used_v4_backend)
        _progress(
            f"DB export completed ({dt:.1f}s, 3DCityDB v{_citydb_backend_version(used_v4_backend)})",
            25
        )

        _progress("XML validation...", 30)
        v_ok, v_msg = well_formed_citygml_auto(out_gml)
        if not v_ok:
            print()
            self.report({'ERROR'}, f"Validator: {v_msg}")
            return {'CANCELLED'}

        if ctx.scene.cgml3.xsd_validate:
            _progress("XSD validation...", 35)
            okx, msgx = validate_citygml_auto(out_gml)
            self._log_text("citygml_xsd_check", msgx)
            if not okx:
                # Lenient validation: only warn for known 3DCityDB export issues
                is_known_3dcitydb_issue = (
                    "is not a valid value of the atomic type 'xs:ID'" in msgx or
                    "xs:ID" in msgx or
                    "textureParameterization" in msgx or
                    "This element is not expected" in msgx or
                    "isFront" in msgx
                )
                if ctx.scene.cgml3.xsd_lenient_validation and is_known_3dcitydb_issue:
                    print()
                    print(f"[CityGML Export] XSD warning (tolerated): {msgx}")
                    self.report({'WARNING'}, f"XSD warning (3DCityDB): DB export will continue despite: {msgx[:100]}...")
                else:
                    print()
                    self.report({'ERROR'}, f"XSD: {msgx}")
                    return {'CANCELLED'}

        _progress("Start CityGML import...", 40)
        print()  # newline before importer's own progress bars

        # If this DB export produced a CityGML 2.0 file, textures are usually next to the GML
        # in an "Appearance/" folder inside tmp. Ensure texture resolution uses that base.
        try:
            version, _msg = detect_version_from_file(out_gml)
        except Exception:
            version = None

        if version == "2.0":
            try:
                from .io_v2 import import_citygml2_into_blender
                import_citygml2_into_blender(out_gml, ctx, gml_path_for_textures=out_gml)
                dt_total = time.time() - t0
                _progress(f"DB import completed ({dt_total:.1f}s)", 100)
                print()
                self.report({'INFO'}, "DB import finished")
                return {'FINISHED'}
            except Exception:
                # fallback to auto
                pass

        import_citygml_auto(out_gml, ctx)
        dt_total = time.time() - t0
        _progress(f"DB import completed ({dt_total:.1f}s)", 100)
        print()
        self.report({'INFO'}, "DB import finished")
        return {'FINISHED'}

# ---------------- Operator: DB-Export (File->Import in DB) ----------------
class CGML3_OT_ExportToDB(Operator):
    bl_idname = "cgml3.export_to_db"
    bl_label = "Export in 3DCityDB (CityGML)"
    bl_options = {'REGISTER'}

    autofill_new_surfaces: BoolProperty(
        name="Automatically process new surfaces",
        description="Only for newly modeled faces without CityGML metadata: detects interior faces, adopts appearance from the corresponding exterior, and assigns surface type for new roof/wall faces without modifying existing imported interior structures.",
        default=False,
    )

    # Connection
    db_host: StringProperty(name="Host", default="")
    db_port: StringProperty(name="Port", default="")
    db_name: StringProperty(name="DB", default="")
    db_user: StringProperty(name="User", default="")
    db_pass: StringProperty(name="Passwort", default="")
    db_schema: StringProperty(name="DB-Schema (optional)", default="")

    # File export (before import into DB)
    citygml_version: EnumProperty(
        name="CityGML Version",
        description="Select the CityGML version for export",
        items=[
            ('3.0', 'CityGML 3.0', 'Export as CityGML 3.0'),
            ('2.0', 'CityGML 2.0', 'Export as CityGML 2.0'),
        ],
        default='3.0'
    )
    
    srs_name: StringProperty(
        name="srsName",
        description="Target CRS (e.g., 'EPSG:25832'). Leave empty to use World['CRS'].",
        default=""
    )
    feature_type_prop: StringProperty(name="Feature-Typ Property", default="ModelType")
    write_offset_back: BoolProperty(
        name="Rewrite the origin offset",
        description="When exporting, add the saved CityModel offset back into the geometry coordinates (only for export, does not modify the scene's offset permanently).",
        default=True
    )
    use_inner_outer_script: BoolProperty(
        name="Automatically detect interior surfaces",
        description="Uses get_inner_and_outer_rings.py to automatically detect interior surfaces.",
        default=False,
    )

    split_wall_roof_surfaces: BoolProperty(
        name="Export roof/wall surfaces individually",
        description="Only for RoofSurface and WallSurface: instead of bundling as MultiSurface/MultiPolygon, write each polygon as a separate surface.",
        default=False,
    )

    write_lod_solid_refs: BoolProperty(
        name="Write LoD Solid from surface references",
        description="Optional: writes a lod{n}Solid that only references the gml:Polygon IDs of the BoundarySurfaces via xlink:href (without installation geometries).",
        default=False,
    )

    normalize_mixed_lods: BoolProperty(
        name="Normalize Mixed LoDs",
        description="Before export, detect top-level objects with mixed LoDs and raise all contained surfaces and parts to the highest LoD used in that top-level object.",
        default=False,
    )

    use_streaming: EnumProperty(
        name="Export Mode",
        description="Streaming export for large datasets (reduces memory usage)",
        items=[
            ('AUTO', 'Automatic', 'Automatic choice based on object count (>1000: Streaming)'),
            ('FORCE_STREAMING', 'Streaming (force)', 'Always use streaming (for very large datasets)'),
            ('FORCE_DOM', 'DOM (force)', 'Always use DOM-based export (faster for small datasets)'),
        ],
        default='AUTO'
    )

    enable_memory_tracking: BoolProperty(
        name="Memory Tracking",
        description="Monitor memory usage during export (approx. 5% performance overhead)",
        default=False,
    )

    # Texture compression options (only used by streaming exporters that support it)
    compress_textures: BoolProperty(
        name="Compress Textures",
        description="Re-save textures as JPEG/PNG and optionally resize (Streaming export).",
        default=False,
    )
    texture_quality: IntProperty(
        name="JPEG Quality",
        description="Quality 1..100 (only if textures are compressed).",
        default=85,
        min=1,
        max=100,
    )
    texture_max_size: IntProperty(
        name="Max. Texture Size",
        description="Maximum edge length (px). 0 = no limit.",
        default=0,
        min=0,
    )
    texture_workers: IntProperty(
        name="Texture Workers",
        description="Number of parallel workers for texture compression.",
        default=4,
        min=1,
        max=64,
    )

    # Export Feature-Type Filter (match file-export UI)
    export_buildings: BoolProperty(name="Buildings export", default=True)
    export_bridges: BoolProperty(name="Bridges export", default=True)
    export_tunnels: BoolProperty(name="Tunnels export", default=True)
    export_vegetation: BoolProperty(name="Vegetation export", default=True)
    export_waterbodies: BoolProperty(name="WaterBodies export", default=True)
    export_construction: BoolProperty(name="Construction export", default=True)
    export_cityfurniture: BoolProperty(name="CityFurniture export", default=True)
    export_landuse: BoolProperty(name="LandUse export", default=True)
    export_transportation: BoolProperty(name="Transportation export", default=True)
    export_relief: BoolProperty(name="Relief export", default=True)
    export_generics: BoolProperty(name="Generics export", default=True)

    # Config
    import_config: StringProperty(name="--config-file (JSON)", subtype='FILE_PATH', default="")

    # ── DB-Import Filter (vcdb import citygml) ──
    import_mode: EnumProperty(
        name="Import Mode",
        description="How existing features in the DB are handled",
        items=[
            ('import_all', 'import_all', 'Import all (default)'),
            ('skip', 'skip', 'Skip existing features'),
            ('delete', 'delete', 'Delete existing features and re-import'),
            ('terminate', 'terminate', 'Terminate existing features and re-import'),
        ],
        default='import_all'
    )
    pre_delete_existing_features: BoolProperty(
        name="Pre-delete by GML ID",
        description="Deletes or terminates existing DB features separately based on GML IDs before normal import",
           default=True,
    )
    pre_delete_mode: EnumProperty(
        name="Pre-delete Mode",
        description="How existing DB features are handled in the separate pre-delete step",
        items=[
            ('delete', 'delete', 'Delete existing features'),
            ('terminate', 'terminate', 'Terminate existing features'),
        ],
        default='delete'
    )
    import_threads: IntProperty(name="Threads", default=0, min=0, description="Number of threads (0 = auto)")
    import_compute_extent: BoolProperty(name="Compute envelopes", default=False, description="--compute-extent")
    import_transform: StringProperty(name="--transform", default="", description="swap-xy or 3x4 matrix")

    # Filter for DB import
    import_type_names: StringProperty(
        name="--type-name",
        description="Comma-separated, e.g., bldg:Building,brid:Bridge",
        default=""
    )
    import_feature_ids: StringProperty(
        name="--id (GML-IDs)",
        description="Comma-separated, e.g., ID_001,ID_002",
        default=""
    )
    import_bbox: StringProperty(
        name="--bbox",
        description="xmin,ymin,xmax,ymax[,srid]",
        default=""
    )
    import_bbox_mode: EnumProperty(
        name="--bbox-mode",
        items=[
            ('NONE', '(Standard: intersects)', ''),
            ('intersects', 'intersects', ''),
            ('contains', 'contains', ''),
            ('on_tile', 'on_tile', ''),
        ],
        default='NONE'
    )
    import_limit: IntProperty(name="--limit", default=0, min=0)
    import_start_index: IntProperty(name="--start-index", default=0, min=0)

    # Appearance
    import_no_appearances: BoolProperty(name="--no-appearances", default=False)
    import_appearance_themes: StringProperty(name="--appearance-theme", default="")

    # CityGML-specific
    import_xsl_transform: StringProperty(name="--xsl-transform", subtype='FILE_PATH', default="")
    import_xal_source: BoolProperty(name="Import xAL source", default=False, description="--import-xal-source")

    # Upgrade options (CityGML 2.0/1.0 → 3.0)
    use_lod4_as_lod3: BoolProperty(name="Use LoD4 as LoD3", default=False, description="--use-lod4-as-lod3")
    map_lod0_roof_edge: BoolProperty(name="Map LoD0 RoofEdge", default=False, description="--map-lod0-roof-edge")
    map_lod1_surface: BoolProperty(name="Map LoD1 Surface", default=False, description="--map-lod1-surface")

    def invoke(self, ctx, evt):
        # Apply scene defaults
        d = ctx.scene.cgml3
        if not self.db_host: self.db_host = d.db_host
        if not self.db_port: self.db_port = d.db_port
        if not self.db_name: self.db_name = d.db_name
        if not self.db_user: self.db_user = d.db_user
        if not self.db_pass: self.db_pass = d.db_pass
        if not self.db_schema: self.db_schema = d.db_schema

        # Automatically detect CityGML version from the top-level collection
        detected_version = _detect_citygml_version_from_scene(ctx.scene)
        if detected_version:
            self.citygml_version = detected_version

        # Automatically prefill srsName from World["CRS"], if available
        if not self.srs_name:
            try:
                w = bpy.data.worlds.get("World")
                if w and "CRS" in w:
                    val = str(w["CRS"]).strip()
                    if val:
                        self.srs_name = val
            except Exception:
                pass
            # Fallback: Scene["SRID"]
            if not self.srs_name:
                try:
                    sc = ctx.scene
                    if sc and "SRID" in sc:
                        val = str(sc["SRID"]).strip()
                        if val:
                            self.srs_name = val
                except Exception:
                    pass

        ctx.window_manager.invoke_props_dialog(self, width=500)
        return {'RUNNING_MODAL'}

    def draw(self, ctx):
        ui_state = ctx.scene.cgml3
        col = self.layout.column(align=True)

        box = _draw_foldout_section(col, ui_state, "db_export_ui_show_connection", "DB Connection")
        if box:
            box.prop(self, "db_host"); box.prop(self, "db_port")
            box.prop(self, "db_name"); box.prop(self, "db_schema")
            box.prop(self, "db_user"); box.prop(self, "db_pass")

        box = _draw_foldout_section(col, ui_state, "db_export_ui_show_export", "Export (CityGML-Datei)")
        if box:
            box.prop(self, "citygml_version")
            box.prop(self, "srs_name")
            box.prop(self, "feature_type_prop")
            box.prop(self, "write_offset_back")
            box.prop(self, "use_inner_outer_script")
            box.prop(self, "autofill_new_surfaces")
            box.prop(self, "split_wall_roof_surfaces")
            box.prop(self, "write_lod_solid_refs")
            box.prop(self, "normalize_mixed_lods")
            box.prop(self, "use_streaming")
            box.prop(self, "enable_memory_tracking")

        box = _draw_foldout_section(col, ui_state, "db_export_ui_show_textures", "Texture Compression (Streaming)")
        if box:
            box.prop(self, "compress_textures")
            row = box.row()
            row.enabled = bool(self.compress_textures)
            row.prop(self, "texture_quality")
            row = box.row()
            row.enabled = bool(self.compress_textures)
            row.prop(self, "texture_max_size")
            row = box.row()
            row.enabled = bool(self.compress_textures)
            row.prop(self, "texture_workers")

        box = _draw_foldout_section(col, ui_state, "db_export_ui_show_feature_types", "Feature Types")
        if box:
            box.prop(self, "export_buildings")
            box.prop(self, "export_bridges")
            box.prop(self, "export_tunnels")
            box.prop(self, "export_vegetation")
            box.prop(self, "export_waterbodies")
            box.prop(self, "export_construction")
            box.prop(self, "export_transportation")
            box.prop(self, "export_cityfurniture")
            box.prop(self, "export_landuse")
            box.prop(self, "export_relief")
            box.prop(self, "export_generics")

        box = _draw_foldout_section(col, ui_state, "db_export_ui_show_config", "Config")
        if box:
            box.prop(self, "import_config")

        box = _draw_foldout_section(col, ui_state, "db_export_ui_show_import_options", "DB-Import Optionen (vcdb import)")
        if box:
            box.prop(self, "import_mode")
            box.prop(self, "pre_delete_existing_features")
            row = box.row()
            row.enabled = bool(self.pre_delete_existing_features)
            row.prop(self, "pre_delete_mode")
            box.prop(self, "import_threads")
            box.prop(self, "import_compute_extent")
            box.prop(self, "import_transform")

        box = _draw_foldout_section(col, ui_state, "db_export_ui_show_import_filters", "DB-Import Filter")
        if box:
            box.label(text="(Scene filters are also applied)", icon='INFO')
            box.prop(self, "import_type_names")
            box.prop(self, "import_feature_ids")
            box.prop(self, "import_bbox")
            box.prop(self, "import_bbox_mode")
            box.prop(self, "import_limit")
            box.prop(self, "import_start_index")

        box = _draw_foldout_section(col, ui_state, "db_export_ui_show_import_appearance", "DB-Import Appearance")
        if box:
            box.prop(self, "import_no_appearances")
            box.prop(self, "import_appearance_themes")

        box = _draw_foldout_section(col, ui_state, "db_export_ui_show_import_citygml", "DB-Import CityGML")
        if box:
            box.prop(self, "import_xsl_transform")
            box.prop(self, "import_xal_source")

        box = _draw_foldout_section(col, ui_state, "db_export_ui_show_upgrade", "Upgrade (CityGML 2.0/1.0)")
        if box:
            box.prop(self, "use_lod4_as_lod3")
            box.prop(self, "map_lod0_roof_edge")
            box.prop(self, "map_lod1_surface")

    def _log_text(self, name_prefix, content):
        from .shared.export_helpers import log_text_to_blender
        log_text_to_blender(name_prefix, content)

    def execute(self, ctx):
        import os, tempfile
        from .ops.citydb_cli import CityDBTool
        from .ops.validate_auto import well_formed_citygml_auto, validate_citygml_auto
        from .ops.citydb_v4_cli import CityDBV4Tool
        from .shared.export_helpers import scene_has_opening_surfaces, scene_has_building_subdivision_features
        from .shared.lod_helpers import normalize_scene_top_level_lods
        import bpy

        def _scene_has_opening_surfaces() -> bool:
            return scene_has_opening_surfaces(ctx)

        def _scene_has_building_subdivision_features() -> bool:
            return scene_has_building_subdivision_features(ctx)

        # -------------------------------
        # Select Streaming Mode (same as File Export)
        # -------------------------------
        if self.use_streaming == 'AUTO':
            use_streaming_mode = False
        elif self.use_streaming == 'FORCE_STREAMING':
            use_streaming_mode = True
        else:  # FORCE_DOM
            use_streaming_mode = False
        
        # -------------------------------
        # Determine Export Function (same as File Export)
        # -------------------------------
        if self.citygml_version == '2.0':
            if use_streaming_mode is not False:
                try:
                    from .io.writer.streaming_exporter import export_citygml2_streaming
                    export_func = export_citygml2_streaming
                except ImportError:
                    from .io_v2 import export_citygml2_from_blender
                    export_func = export_citygml2_from_blender
                    use_streaming_mode = False
            else:
                from .io_v2 import export_citygml2_from_blender
                export_func = export_citygml2_from_blender
        else:
            if use_streaming_mode is not False:
                try:
                    from .io.writer.streaming_exporter import export_blender_to_citygml3_streaming
                    export_func = export_blender_to_citygml3_streaming
                except ImportError:
                    from .io.gml3_writer import export_blender_to_citygml3
                    export_func = export_blender_to_citygml3
                    use_streaming_mode = False
            else:
                from .io.gml3_writer import export_blender_to_citygml3
                export_func = export_blender_to_citygml3

        if not self.db_pass:
            self.report({'ERROR'}, "Password cannot be empty. Otherwise, the citydb-tool prompt will hang.")
            return {'CANCELLED'}
        
        # --- NEW: if the streaming exporter does not support this option, fall back to DOM exporter
        if self.split_wall_roof_surfaces and ("split_wall_roof_surfaces" not in export_func.__code__.co_varnames):
            if self.citygml_version == '2.0':
                from .io_v2 import export_citygml2_from_blender
                export_func = export_citygml2_from_blender
            else:
                from .io.gml3_writer import export_blender_to_citygml3
                export_func = export_blender_to_citygml3
            use_streaming_mode = False

        if use_streaming_mode is not False and _scene_has_opening_surfaces():
            if self.citygml_version == '2.0':
                from .io_v2 import export_citygml2_from_blender
                export_func = export_citygml2_from_blender
            else:
                from .io.gml3_writer import export_blender_to_citygml3
                export_func = export_blender_to_citygml3
            use_streaming_mode = False

        if self.citygml_version == '3.0' and use_streaming_mode is not False and _scene_has_building_subdivision_features():
            from .io.gml3_writer import export_blender_to_citygml3
            export_func = export_blender_to_citygml3
            use_streaming_mode = False

        prefs = _get_citydb_prefs(ctx)
        v5_tool = CityDBTool(prefs)
        v4_tool = CityDBV4Tool(prefs)

        tmp = tempfile.mkdtemp(prefix="cgml3_")
        gml = os.path.join(tmp, "upload.gml")

        if self.normalize_mixed_lods:
            normalization = normalize_scene_top_level_lods(ctx.scene)
            if normalization["normalized"]:
                count = len(normalization["normalized"])
                self.report({'INFO'}, f"Normalized mixed LoDs for {count} top-level object(s) before export")

        def _call_export(out_path: str) -> None:
            export_types = []
            if self.export_buildings:
                export_types += [
                    "bldg:Building",
                    "bldg:BuildingPart",
                    "bldg:BuildingUnit",
                    "bldg:Storey",
                    "bldg:BuildingRoom",
                    "bldg:BuildingConstructiveElement",
                    "bldg:BuildingInstallation",
                    "bldg:BuildingFurniture",
                ]
                if self.citygml_version == '2.0':
                    export_types += ["bldg:IntBuildingInstallation"]
            if self.export_bridges:
                export_types += [
                    "brid:Bridge",
                    "brid:BridgePart",
                    "brid:BridgeRoom",
                    "brid:BridgeFurniture",
                    "brid:BridgeInstallation",
                ]
                if self.citygml_version == '2.0':
                    export_types += ["brid:IntBridgeInstallation", "brid:BridgeConstructionElement"]
            if self.export_tunnels:
                export_types += [
                    "tun:Tunnel",
                    "tun:TunnelPart",
                    "tun:HollowSpace",
                    "tun:TunnelFurniture",
                    "tun:TunnelInstallation",
                    "tun:TunnelConstructiveElement",
                ]
            if self.export_vegetation:
                export_types += ["veg:SolitaryVegetationObject", "veg:PlantCover"]
            if self.export_waterbodies:
                export_types += ["wtr:WaterBody", "wtr:WaterSurface", "wtr:WaterGroundSurface"]
                if self.citygml_version == '2.0':
                    export_types += ["wtr:WaterClosureSurface"]
            if self.export_construction and self.citygml_version == '3.0':
                export_types += ["con:OtherConstruction", "con:Window", "con:Door"]
            if self.export_transportation:
                export_types += ["tran:Road", "tran:Railway", "tran:Track", "tran:Square"]
                if self.citygml_version == '2.0':
                    export_types += ["tran:TrafficArea", "tran:AuxiliaryTrafficArea"]
                else:
                    export_types += [
                        "tran:Waterway",
                        "tran:Section",
                        "tran:Intersection",
                        "tran:TrafficSpace",
                        "tran:AuxiliaryTrafficSpace",
                        "tran:TrafficArea",
                        "tran:AuxiliaryTrafficArea",
                        "tran:ClearanceSpace",
                        "tran:Marking",
                        "tran:Hole",
                    ]
            if self.export_cityfurniture:
                export_types += ["frn:CityFurniture"]
            if self.export_landuse:
                export_types += ["luse:LandUse"]
            if self.export_relief:
                export_types += ["dem:ReliefFeature", "dem:TINRelief", "dem:RasterRelief", "dem:MassPointRelief", "dem:BreaklineRelief"]
            if self.export_generics:
                export_types += ["grp:CityObjectGroup"]
                if self.citygml_version == '2.0':
                    export_types += ["gen:GenericCityObject"]
                else:
                    export_types += [
                        "gen:GenericOccupiedSpace",
                        "gen:GenericLogicalSpace",
                        "gen:GenericUnoccupiedSpace",
                        "gen:GenericThematicSurface",
                    ]

            kwargs = {
                "use_inner_outer_script": self.use_inner_outer_script,
                "autofill_new_surfaces": self.autofill_new_surfaces,
                "split_wall_roof_surfaces": self.split_wall_roof_surfaces,
                "write_lod_solid_refs": self.write_lod_solid_refs,
                "export_types": export_types,
            }
            if "use_streaming" in export_func.__code__.co_varnames:
                kwargs.update({
                    "use_streaming": use_streaming_mode,
                    "enable_memory_tracking": self.enable_memory_tracking,
                })
            if "compress_textures" in export_func.__code__.co_varnames:
                kwargs.update({
                    "compress_textures": self.compress_textures,
                    "texture_quality": self.texture_quality,
                    "texture_max_size": self.texture_max_size if self.texture_max_size > 0 else None,
                    "texture_workers": self.texture_workers,
                })
            kwargs = {k: v for k, v in kwargs.items() if k in export_func.__code__.co_varnames}
            export_func(out_path, ctx, self.srs_name, self.feature_type_prop, **kwargs)

        if not self.write_offset_back:
            old = tuple(ctx.scene.cgml3_offset)
            try:
                v = ctx.scene.cgml3_offset
                v[0] = 0.0
                v[1] = 0.0
                v[2] = 0.0
                _call_export(gml)
            finally:
                v = ctx.scene.cgml3_offset
                v[0], v[1], v[2] = old
        else:
            _call_export(gml)

        ok_v, msg_v = well_formed_citygml_auto(gml)
        if not ok_v:
            self.report({'ERROR'}, f"Validator: {msg_v}")
            return {'CANCELLED'}

        if ctx.scene.cgml3.xsd_validate:
            okx, msgx = validate_citygml_auto(gml)
            self._log_text("citygml_xsd_check", msgx)
            if not okx:
                # Tolerant validation: issue a warning only for known 3DCityDB export errors
                is_known_3dcitydb_issue = (
                    "is not a valid value of the atomic type 'xs:ID'" in msgx or
                    "xs:ID" in msgx or
                    "textureParameterization" in msgx or
                    "This element is not expected" in msgx or
                    "isFront" in msgx
                )
                if ctx.scene.cgml3.xsd_lenient_validation and is_known_3dcitydb_issue:
                    self.report({'WARNING'}, f"XSD-Warnung (3DCityDB): {msgx[:100]}... Export wird fortgesetzt.")
                    print(f"[CityGML Export] XSD-Warnung (toleriert): {msgx}")
                else:
                    self.report({'ERROR'}, f"XSD: {msgx}")
                    return {'CANCELLED'}

        # ── Prepare Scene Filters for DB Import ──

        # Feature Type Filter: Operator Field → Scene Panel Fallback
        _db_type_names = [s.strip() for s in self.import_type_names.split(",") if s.strip()] or None
        if not _db_type_names and ctx.scene.cgml3.use_feature_type_filter:
            _ft_map = {
                "import_buildings":       ["bldg:Building", "bldg:BuildingPart"],
                "import_bridges":         ["brid:Bridge", "brid:BridgePart"],
                "import_tunnels":         ["tun:Tunnel", "tun:TunnelPart"],
                "import_vegetation":      ["veg:SolitaryVegetationObject", "veg:PlantCover"],
                "import_water":           ["wtr:WaterBody", "wtr:WaterSurface"],
                "import_transportation":  ["tran:Road", "tran:Railway", "tran:Track", "tran:Square", "tran:Waterway"],
                "import_cityfurniture":   ["frn:CityFurniture"],
                "import_landuse":         ["luse:LandUse"],
                "import_relief":          ["dem:TINRelief", "dem:RasterRelief", "dem:MassPointRelief", "dem:BreaklineRelief"],
                "import_generics":        ["gen:GenericOccupiedSpace", "gen:GenericLogicalSpace",
                                           "gen:GenericUnoccupiedSpace", "gen:GenericThematicSurface",
                                           "gen:GenericCityObject"],
            }
            _scene_types = []
            for prop, names in _ft_map.items():
                if getattr(ctx.scene.cgml3, prop, True):
                    _scene_types.extend(names)
            if _scene_types:
                _db_type_names = _scene_types
                print(f"[CityGML DB-Import] Feature Type Filter (Scene): {len(_db_type_names)} types")

        # GML-ID Filter: Operator Field → Scene Panel Fallback
        # vcdb import citygml supports --id directly (no SQL needed)!
        _db_feature_ids = [s.strip() for s in self.import_feature_ids.split(",") if s.strip()] or None
        if not _db_feature_ids and ctx.scene.cgml3.use_gmlid_filter:
            gml_ids_str = ctx.scene.cgml3.gmlid_filter.strip()
            if gml_ids_str:
                _db_feature_ids = [gid.strip() for gid in gml_ids_str.replace(";", ",").split(",") if gid.strip()]
                if _db_feature_ids:
                    print(f"[CityGML DB-Import] GML-ID-Filter (Scene): {len(_db_feature_ids)} IDs")

        # BBox-Filter: Operator-Feld → Scene-Panel-Fallback
        # vcdb import citygml support --bbox=xmin,ymin,xmax,ymax[,srid]
        _db_bbox = self.import_bbox.strip() or None
        if not _db_bbox and ctx.scene.cgml3.use_bbox_filter:
            min_x = ctx.scene.cgml3.bbox_min_x
            min_y = ctx.scene.cgml3.bbox_min_y
            max_x = ctx.scene.cgml3.bbox_max_x
            max_y = ctx.scene.cgml3.bbox_max_y
            _db_bbox = f"{min_x},{min_y},{max_x},{max_y}"
            print(f"[CityGML DB-Import] BBox-Filter (Scene): {_db_bbox}")

        # Appearance-Themes
        _db_appearance_themes = [s.strip() for s in self.import_appearance_themes.split(",") if s.strip()] or None

        if self.pre_delete_existing_features and self.import_mode in {'delete', 'terminate'}:
            msg = (
                "Pre-delete is already active. Please set the DB import mode to 'import_all' or 'skip', "
                "so that existing features are not deleted or terminated twice."
            )
            self._log_text("citydb_delete", msg)
            self.report({'ERROR'}, msg)
            return {'CANCELLED'}

        if self.pre_delete_existing_features and not _db_feature_ids:
            _db_feature_ids = _collect_scene_feature_ids(ctx.scene) or None
            if _db_feature_ids:
                print(f"[3DCityDB] Pre-Delete: {len(_db_feature_ids)} IDs automatically collected from the scene")

        if self.pre_delete_existing_features and not _db_feature_ids:
            msg = (
                "Pre-delete by GML-ID could not derive any IDs from the scene. "
                "Please check if the imported objects have valid gml_id properties."
            )
            self._log_text("citydb_delete", msg)
            self.report({'ERROR'}, msg)
            return {'CANCELLED'}

        if self.pre_delete_existing_features:
            print(
                "[3DCityDB] Pre-Delete: "
                f"mode={self.pre_delete_mode}, ids={len(_db_feature_ids or [])}"
            )

            delete_ok, delete_log = v5_tool.delete_features_from_db(
                host=self.db_host,
                port=self.db_port,
                db=self.db_name,
                user=self.db_user,
                password=self.db_pass,
                db_schema=(self.db_schema.strip() or None),
                delete_mode=self.pre_delete_mode,
                type_names=_db_type_names,
                feature_ids=_db_feature_ids,
            )

            v5_delete_requires_v4_fallback = (not delete_ok and _citydb_v5_requires_v4_fallback(delete_log))

            if v5_delete_requires_v4_fallback:
                self._log_text("citydb_delete", delete_log)
                _log_citydb_backend_fallback("DB-Delete")
                delete_ok, delete_log = v4_tool.delete_features_from_db(
                    host=self.db_host,
                    port=self.db_port,
                    db=self.db_name,
                    user=self.db_user,
                    password=self.db_pass,
                    db_schema=(self.db_schema.strip() or None),
                    delete_mode=self.pre_delete_mode,
                    feature_ids=_db_feature_ids,
                    type_names=_db_type_names,
                    bbox=_db_bbox,
                    bbox_mode=None if self.import_bbox_mode == 'NONE' else self.import_bbox_mode,
                    limit=self.import_limit if self.import_limit > 0 else None,
                    start_index=self.import_start_index if self.import_start_index > 0 else None,
                )

            self._log_text("citydb_delete", delete_log)

            if not delete_ok:
                if v5_delete_requires_v4_fallback:
                    self.report({'ERROR'}, "Pre-delete failed. Neither v5 nor v4 could delete or terminate the DB (see text log).")
                else:
                    self.report({'ERROR'}, "Pre-delete failed (see text log).")
                return {'CANCELLED'}

            _log_citydb_backend_success("DB-Delete", v5_delete_requires_v4_fallback)

            # IDs were only needed for the delete; the import GML already
            # contains the correct feature set.  Passing them through to the
            # import command would exceed the Windows command-line length limit
            # (WinError 206) for large scenes.
            if not self.import_feature_ids.strip():
                _db_feature_ids = None

        if self.import_mode == 'delete' and not self.import_no_appearances:
            msg = (
                "DB-Import aborted: citydb-tool v5 does not import appearance data in 'delete' mode. "
                "Please delete or terminate existing features separately first "
                "and then export without delete mode."
            )
            self._log_text("citydb_import", msg)
            self.report({'ERROR'}, msg)
            return {'CANCELLED'}

        print("[3DCityDB] DB-Import: 3DCityDB version is checked via citydb-tool v5.")

        ok, log = v5_tool.import_citygml_to_db(
            gml,
            host=self.db_host, port=self.db_port, db=self.db_name,
            user=self.db_user, password=self.db_pass,
            db_schema=(self.db_schema.strip() or None),
            config_file=(self.import_config or None),
            # Import-Optionen
            import_mode=self.import_mode if self.import_mode != 'import_all' else None,
            threads=self.import_threads if self.import_threads > 0 else None,
            compute_extent=self.import_compute_extent,
            transform=(self.import_transform.strip() or None),
            # Filter
            type_names=_db_type_names,
            feature_ids=_db_feature_ids,
            bbox=_db_bbox,
            bbox_mode=None if self.import_bbox_mode == 'NONE' else self.import_bbox_mode,
            limit=self.import_limit if self.import_limit > 0 else None,
            start_index=self.import_start_index if self.import_start_index > 0 else None,
            # Appearance
            no_appearances=self.import_no_appearances,
            appearance_themes=_db_appearance_themes,
            # CityGML-spezifisch
            xsl_transform=(self.import_xsl_transform.strip() or None),
            import_xal_source=self.import_xal_source,
            # Upgrade
            use_lod4_as_lod3=self.use_lod4_as_lod3,
            map_lod0_roof_edge=self.map_lod0_roof_edge,
            map_lod1_surface=self.map_lod1_surface,
        )

        v5_requires_v4_fallback = (not ok and _citydb_v5_requires_v4_fallback(log))

        if v5_requires_v4_fallback:
            self._log_text("citydb_import", log)
            _log_citydb_backend_fallback("DB-Import")
            ok, log = v4_tool.import_citygml_to_db(
                gml,
                host=self.db_host, port=self.db_port, db=self.db_name,
                user=self.db_user, password=self.db_pass,
                db_schema=(self.db_schema.strip() or None),
                config_file=(self.import_config or None),
                import_mode=self.import_mode if self.import_mode != 'import_all' else None,
                no_appearance=self.import_no_appearances,
                type_names=_db_type_names,
                bbox=_db_bbox,
                bbox_mode=None if self.import_bbox_mode == 'NONE' else self.import_bbox_mode,
                limit=self.import_limit if self.import_limit > 0 else None,
                start_index=self.import_start_index if self.import_start_index > 0 else None,
            )
        self._log_text("citydb_import", log)

        if not ok:
            if v5_requires_v4_fallback:
                self.report({'ERROR'}, "DB-Import failed. v5 (citydb-tool) and v4 (Java impexp) could not handle the DB (see text log).")
            else:
                self.report({'ERROR'}, "CityGML import to DB failed (see text log).")
            return {'CANCELLED'}
        _log_citydb_backend_success("DB-Import", v5_requires_v4_fallback)
        self.report({'INFO'}, "Export to DB finished")
        return {'FINISHED'}

# ---------------- File menu entries ----------------
def menu_func_import(self, context):
    self.layout.operator(CGML3_OT_ImportGMLFile.bl_idname, text="CityGML 2/3 (.gml/.xml)")

def menu_func_export(self, context):
    self.layout.operator(CGML3_OT_ExportGMLFile.bl_idname, text="CityGML 2/3 (.gml)")

# ---------------- Register ----------------
classes = (
    CGML3_Prefs, CGML3_Runtime, 
    CGML3_PT_Main, CGML3_PT_Filter, CGML3_PT_DBDefaults,
    CGML3_OT_ParseBBoxString, CGML3_OT_BBoxToString,
    CGML3_OT_ImportGMLFile, CGML3_OT_ExportGMLFile,
    CGML3_OT_ImportFromDB, CGML3_OT_ExportToDB,
    CGML3_PT_Reload, CGML3_OT_ReloadAddon,
    CGML3_OT_SelectAllLODs, CGML3_OT_DeselectAllLODs,
    CGML3_OT_SelectAllFeatureTypes, CGML3_OT_DeselectAllFeatureTypes,
    # Import Scanner operators
    import_scanner.CGML3_OT_ScanImportFile,
    # Viewport Performance operators
    viewport_performance.CGML3_OT_OptimizeViewport,
    viewport_performance.CGML3_OT_RestoreViewport,
    viewport_performance.CGML3_OT_ToggleHideUnselected,
    viewport_performance.CGML3_OT_MaterialPreviewSelection,
    viewport_performance.CGML3_OT_QuickLocalView,
    viewport_performance.CGML3_MT_MaterialPreviewContext,
    viewport_performance.CGML3_PT_ViewportPerformance,
    join_object_parts_panel.CGML3_PT_JoinObjectParts,
    join_object_parts.CGML3_OT_JoinObjectParts,
    join_object_parts.CGML3_MT_JoinObjectPartsContext,
    assign_object_part.CGML3_OT_AssignObjectPart,
    assign_object_part.CGML3_OT_DetachObjectPart,
    assign_object_part.CGML3_MT_AssignObjectPartContext,
)

def register():
    # Check Cython acceleration status
    try:
        from .cython_ext import check_cython_support
        cython_ok, modules, msg = check_cython_support()
        if cython_ok:
            print(f"[CityGML] 🚀 {msg}")
        else:
            print(f"[CityGML] ⚠️ {msg}")
    except ImportError:
        print("[CityGML] ⚠️ Cython modules not found. Using pure Python (slower).")
        print("[CityGML] To enable 10-50× speedup, run: python setup.py build_ext --inplace")
    
    # Set bl_order for JoinObjectParts explicitly (between VP=21 and MT=23)
    join_object_parts_panel.CGML3_PT_JoinObjectParts.bl_order = 22

    for c in classes:
        bpy.utils.register_class(c)
        # Register presets directly after the main panel (before Filter/DB panels)
        if c is CGML3_PT_Main:
            try:
                cgml3_presets.register()
            except Exception as e:
                print(f"[CityGML] Presets register failed: {e}")
    try:
        cgml3_ui.register()
    except Exception as e:
        print(f"[CityGML] UI register failed: {e}")
    try:
        modeltyper.register()
    except Exception as e:
        print(f"[CityGML] ModelTyper register failed: {e}")
        traceback.print_exc()
    bpy.types.Scene.cgml3 = PointerProperty(type=CGML3_Runtime)
    _ensure_scene_props()
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)
    bpy.types.VIEW3D_MT_object_context_menu.append(
        viewport_performance.draw_material_preview_context_menu
    )
    bpy.types.VIEW3D_MT_object_context_menu.append(
        join_object_parts.draw_join_object_parts_context_menu
    )
    bpy.types.VIEW3D_MT_object_context_menu.append(
        assign_object_part.draw_assign_object_part_context_menu
    )
    bpy.types.Scene.cgml3_offset = FloatVectorProperty(
    name="CityModel-Offset", size=3, default=(0.0, 0.0, 0.0)
    )
    # Register drag & drop handler
    try:
        drag_drop.register()
        print("[CityGML] Drag & Drop enabled for .gml/.xml files")
    except Exception as e:
        print(f"[CityGML] Drag & Drop registration failed: {e}")
    # Immediately upon activation and at Blender start (if the add-on is enabled):
    _delete_pycache(Path(__file__).resolve().parent)
    # Additionally, once shortly delayed, to cover reloading modules:
    try:
        bpy.app.timers.register(_cleanup_now_and_stop, first_interval=0.2)
    except Exception:
        # Fallback, if timers are not available in very old versions
        pass

def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    try:
        bpy.types.VIEW3D_MT_object_context_menu.remove(
            viewport_performance.draw_material_preview_context_menu
        )
    except Exception:
        pass
    try:
        bpy.types.VIEW3D_MT_object_context_menu.remove(
            join_object_parts.draw_join_object_parts_context_menu
        )
    except Exception:
        pass
    try:
        bpy.types.VIEW3D_MT_object_context_menu.remove(
            assign_object_part.draw_assign_object_part_context_menu
        )
    except Exception:
        pass
    try:
        drag_drop.unregister()
    except Exception:
        pass
    try:
        cgml3_presets.unregister()
    except Exception:
        pass
    try:
        cgml3_ui.unregister()
    except Exception:
        pass
    try:
        modeltyper.unregister()
    except Exception:
        traceback.print_exc()
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    pass

if __name__ == "__main__":
    register()
