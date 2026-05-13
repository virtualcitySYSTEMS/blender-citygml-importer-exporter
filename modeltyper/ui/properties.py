# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Blender properties for the integrated CityGML ModelTyper tools.
"""

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty

from ..citygml import get_enum_items


def get_feature_type_items(self, context):
    """Get all available CityGML feature types."""
    return [
        ("BUILDING", "Building", "Building features", 'HOME', 0),
        ("BRIDGE", "Bridge", "Bridge features", 'MESH_PLANE', 1),
        ("TUNNEL", "Tunnel", "Tunnel features", 'MESH_CUBE', 2),
        ("WATERBODY", "WaterBody", "Water body features", 'MOD_FLUIDSIM', 3),
        ("TRANSPORTATION", "Transportation", "Transportation features (roads, railways)", 'EXPORT', 4),
        ("VEGETATION", "Vegetation", "Vegetation features", 'OUTLINER_OB_FORCE_FIELD', 5),
        ("LANDUSE", "LandUse", "Land use features", 'WORLD', 6),
        ("CITYFURNITURE", "CityFurniture", "City furniture features", 'LIGHT', 7),
    ]


def get_collection_items(self, context):
    """Get the collections currently available in the scene."""
    items = [(c.name, c.name, "", i) for i, c in enumerate(bpy.data.collections)]
    if not items:
        return [("NONE", "No Collections", "Create a collection first", 0)]
    return items


def get_category_items(self, context):
    """Get surface type categories based on feature type and LOD."""
    props = getattr(context.scene, "cgml3_modeltyper_props", None)
    if not props:
        return [("NONE", "None", "No categories available", 0)]

    feature_type = props.feature_type
    lod = "4" if props.show_lod4_surfaces else "1-3"
    version = props.citygml_version
    return get_enum_items(feature_type, version, lod)


def update_feature_type(self, context):
    """Reset the manual category when feature type options change."""
    props = getattr(context.scene, "cgml3_modeltyper_props", None)
    if props is None:
        return

    items = get_category_items(self, context)
    if items and items[0][0] != "NONE":
        props.category_enum = items[0][0]

    for row in getattr(props, "property_mappings", []):
        row_items = get_mapping_surface_type_items(row, context)
        identifiers = {item[0] for item in row_items}
        if row_items and row.target_surface_type not in identifiers and row_items[0][0] != "NONE":
            row.target_surface_type = row_items[0][0]


def get_mapping_surface_type_items(self, context):
    """Get target surface types for a custom-property mapping row."""
    props = getattr(context.scene, "cgml3_modeltyper_props", None) if context else None
    version = props.citygml_version if props else "2.0"
    lod = "4" if props and props.show_lod4_surfaces else "1-3"
    feature_type = getattr(self, "target_feature_type", "BUILDING") or "BUILDING"
    return get_enum_items(feature_type, version, lod)


def update_mapping_feature_type(self, context):
    """Reset a mapping row's surface type when the target feature type changes."""
    items = get_mapping_surface_type_items(self, context)
    if items and items[0][0] != "NONE":
        self.target_surface_type = items[0][0]


class CGML3_ModelTyperMappingItem(bpy.types.PropertyGroup):
    """One custom-property mapping rule for ModelTyper."""

    enabled: BoolProperty(
        name="Enabled",
        description="Use this mapping row",
        default=True,
    )

    name: StringProperty(
        name="Name",
        description="Optional label for this mapping row",
        default="Mapping",
    )

    object_property: StringProperty(
        name="Object Property",
        description="Object custom property to match",
        default="",
    )

    object_value: StringProperty(
        name="Object Value",
        description="Expected object custom property value; leave empty to match any value",
        default="",
    )

    material_property: StringProperty(
        name="Material Property",
        description="Material custom property to match for each face",
        default="",
    )

    material_value: StringProperty(
        name="Material Value",
        description="Expected material custom property value; leave empty to match any value",
        default="",
    )

    match_mode: EnumProperty(
        name="Match Mode",
        description="How custom property values are compared",
        items=[
            ("EXACT", "Exact", "Value must match exactly"),
            ("CONTAINS", "Contains", "Actual value must contain the configured value"),
            ("REGEX", "Regex", "Configured value is interpreted as a regular expression"),
        ],
        default="EXACT",
    )

    case_sensitive: BoolProperty(
        name="Case Sensitive",
        description="Use case-sensitive property name and value matching",
        default=False,
    )

    target_feature_type: EnumProperty(
        name="Feature Type",
        description="CityGML feature type to assign to matched objects",
        items=get_feature_type_items,
        default=0,
        update=update_mapping_feature_type,
    )

    target_surface_type: EnumProperty(
        name="Surface Type",
        description="CityGML surface type to assign to matched faces",
        items=get_mapping_surface_type_items,
    )

    copy_source_material: BoolProperty(
        name="Copy Source Material",
        description="Copy the source material appearance and custom properties before adding CityGML semantics",
        default=True,
    )


class CGML3_ModelTyperProperties(bpy.types.PropertyGroup):
    """Properties for ModelTyper tools."""

    feature_type: EnumProperty(
        name="Feature Type",
        description="CityGML feature type",
        items=get_feature_type_items,
        default=0,
        update=update_feature_type,
    )

    citygml_version: EnumProperty(
        name="CityGML Version",
        description="CityGML schema version",
        items=[
            ("2.0", "CityGML 2.0", "Use CityGML 2.0 surface types"),
            ("3.0", "CityGML 3.0", "Use CityGML 3.0 surface types (experimental)"),
        ],
        default="2.0",
        update=update_feature_type,
    )

    show_lod4_surfaces: BoolProperty(
        name="Include LOD4 Interior Surfaces",
        description="Show interior surface types (FloorSurface, CeilingSurface, InteriorWallSurface)",
        default=False,
        update=update_feature_type,
    )

    assignment_mode: EnumProperty(
        name="Assignment Mode",
        description="Choose which objects to process",
        items=[
            ("SELECTION", "Selected Objects", "Process only selected objects", 'RESTRICT_SELECT_OFF', 0),
            ("COLLECTION", "Entire Collection", "Process all objects in collection", 'OUTLINER_COLLECTION', 1),
        ],
        default="SELECTION",
    )

    collection_enum: EnumProperty(
        name="Collection",
        description="Select collection to process",
        items=get_collection_items,
    )

    category_enum: EnumProperty(
        name="Surface Type",
        description="Select surface type for manual assignment",
        items=get_category_items,
    )

    preserve_existing_colors: BoolProperty(
        name="Keep colors",
        description="Keep existing textures and material colors, no recoloring (Auto)",
        default=False,
    )

    preserve_existing_colors_manual: BoolProperty(
        name="Keep colors (Manual)",
        description="Keep existing textures and material colors, no recoloring (Manual)",
        default=False,
    )

    generate_interior_surface: BoolProperty(
        name="Generate Interior Surface",
        description="For Window/Door, also generate an Interior Surface (inner ring/hole)",
        default=True,
    )

    property_mappings: CollectionProperty(
        name="Property Mappings",
        description="Mapping rows from object/material custom properties to CityGML semantics",
        type=CGML3_ModelTyperMappingItem,
    )

    property_mapping_index: IntProperty(
        name="Active Mapping",
        description="Active custom-property mapping row",
        default=0,
    )

    mapping_overwrite_existing_semantics: BoolProperty(
        name="Overwrite Existing Semantics",
        description="Overwrite faces whose materials already contain CityGML semantic properties",
        default=True,
    )

    mapping_cleanup_unused_materials: BoolProperty(
        name="Remove Unused Materials",
        description="Remove material slots that are no longer referenced after applying mappings",
        default=True,
    )


def register():
    bpy.utils.register_class(CGML3_ModelTyperMappingItem)
    bpy.utils.register_class(CGML3_ModelTyperProperties)
    bpy.types.Scene.cgml3_modeltyper_props = bpy.props.PointerProperty(type=CGML3_ModelTyperProperties)


def unregister():
    if hasattr(bpy.types.Scene, "cgml3_modeltyper_props"):
        try:
            del bpy.types.Scene.cgml3_modeltyper_props
        except Exception:
            pass
    try:
        bpy.utils.unregister_class(CGML3_ModelTyperProperties)
    except RuntimeError:
        pass
    try:
        bpy.utils.unregister_class(CGML3_ModelTyperMappingItem)
    except RuntimeError:
        pass
