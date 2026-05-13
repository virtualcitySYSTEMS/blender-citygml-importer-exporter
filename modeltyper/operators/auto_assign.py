# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Automatic assignment operator.
"""

import bpy
from datetime import date
from uuid import uuid4

from ..core import classification, geometry, materials


class CGML3_MODELTYPER_OT_AutoAssign(bpy.types.Operator):
    """Automatically assign surface types based on geometry analysis."""

    bl_idname = "cgml3_modeltyper.auto_assign"
    bl_label = "Assign Surface Types (Auto)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.cgml3_modeltyper_props

        if props.assignment_mode == "SELECTION":
            meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']
            if not meshes:
                self.report({'WARNING'}, "No mesh objects selected")
                return {'CANCELLED'}
        else:
            collection_name = props.collection_enum
            if collection_name not in bpy.data.collections:
                self.report({'WARNING'}, "Collection not found")
                return {'CANCELLED'}

            collection = bpy.data.collections[collection_name]
            meshes = [obj for obj in collection.all_objects if obj.type == 'MESH']
            if not meshes:
                self.report({'WARNING'}, "No mesh objects in collection")
                return {'CANCELLED'}

        feature_type = props.feature_type
        version = props.citygml_version

        for obj in meshes:
            bm = geometry.bm_from_object(obj)

            complex_type = None
            if feature_type == "BRIDGE":
                complex_type = classification.classify_bridge_complex_type(obj, bm)
                if complex_type:
                    print(f"ModelTyper: Detected {complex_type} for {obj.name}")

            if feature_type == "BUILDING":
                labels = classification.ensemble_labels_building(bm)
            elif feature_type == "BRIDGE":
                labels = classification.ensemble_labels_bridge(bm)
            elif feature_type == "TUNNEL":
                labels = classification.ensemble_labels_tunnel(bm)
            elif feature_type == "WATERBODY":
                labels = classification.classify_waterbody(bm)
            elif feature_type == "TRANSPORTATION":
                labels = classification.classify_transportation(bm)
            elif feature_type == "CITYFURNITURE":
                labels = ["CityFurniture"] * len(bm.faces)
            elif feature_type == "LANDUSE":
                labels = ["LandUse"] * len(bm.faces)
            elif feature_type == "VEGETATION":
                labels = self._classify_vegetation(bm)
            else:
                labels = ["GenericSurface"] * len(bm.faces)

            is_valid, errors = classification.validate_surface_types(feature_type, labels)
            if not is_valid:
                print(f"ModelTyper Warning: Invalid surface types detected for {obj.name}")
                for error in errors[:5]:
                    print(f"  {error}")

            bm.free()

            materials.assign_materials_per_face(
                obj, labels, feature_type, version, complex_type,
                preserve_existing_colors=props.preserve_existing_colors,
            )
            materials.set_model_type(obj, feature_type)
            materials.assign_uv_metadata(obj)

            # Set CityGML custom properties
            obj["cgml3_feature"] = obj.get("ModelType", feature_type)
            world = bpy.data.worlds.get("World")
            if world and "CRS" in world:
                obj["CRS"] = world["CRS"]
            obj["core:creationDate"] = date.today().isoformat()
            if "gml_id" not in obj:
                obj["gml_id"] = f"UUID_{uuid4()}"

        self.report({'INFO'}, f"Processed {len(meshes)} object(s) as {feature_type}")
        return {'FINISHED'}

    def _classify_vegetation(self, bm):
        """Classify vegetation surfaces based on geometry characteristics."""
        import mathutils

        labels = []
        up_vector = mathutils.Vector((0, 0, 1))

        for face in bm.faces:
            alignment = abs(face.normal.dot(up_vector))
            if alignment > 0.9:
                labels.append("PlantCover")
            else:
                labels.append("VegetationObject")

        return labels


def register():
    bpy.utils.register_class(CGML3_MODELTYPER_OT_AutoAssign)


def unregister():
    try:
        bpy.utils.unregister_class(CGML3_MODELTYPER_OT_AutoAssign)
    except RuntimeError:
        pass
