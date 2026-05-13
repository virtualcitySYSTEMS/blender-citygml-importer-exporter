# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Analysis operator for CityGML surface type assignments.
"""

import bpy

from ..core import advanced_classification, classification, geometry


class CGML3_MODELTYPER_OT_AnalyzeModel(bpy.types.Operator):
    """Analyze current surface type assignments and provide suggestions."""

    bl_idname = "cgml3_modeltyper.analyze_model"
    bl_label = "Analyze Model"
    bl_description = "Analyze the model and provide improvement suggestions"
    bl_options = {'REGISTER'}

    def execute(self, context):
        obj = context.active_object
        props = context.scene.cgml3_modeltyper_props

        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "Active object is not a mesh")
            return {'CANCELLED'}

        bm = geometry.bm_from_object(obj)

        labels = []
        complex_types = set()
        for face in bm.faces:
            if face.material_index < len(obj.data.materials):
                mat = obj.data.materials[face.material_index]
                surface_type = mat.get("SurfaceTyp", "Unknown")
                labels.append(surface_type)

                complex_type = mat.get("CityGMLComplexType")
                if complex_type:
                    complex_types.add(complex_type)
            else:
                labels.append("Unknown")

        feature_type = obj.get("ModelType", None)
        if not feature_type:
            feature_type = classification.detect_feature_type_from_labels(labels)
        if not feature_type:
            feature_type = props.feature_type

        detected_complex_type = None
        if feature_type == "BRIDGE":
            detected_complex_type = classification.classify_bridge_complex_type(obj, bm)

        counts = classification.get_surface_type_counts(labels)
        is_valid, errors = classification.validate_surface_types(feature_type, labels)
        suggestions = advanced_classification.suggest_improvements(bm, labels)
        suggested_type = classification.suggest_feature_type(bm)

        bm.free()

        print("\n" + "=" * 60)
        print(f"ModelTyper Analysis: {obj.name}")
        print("=" * 60)
        print(f"Feature Type: {feature_type}")

        if not obj.get("ModelType"):
            print("  (detected from surface types)")

        if feature_type == "BRIDGE":
            if complex_types:
                print(f"CityGML ComplexType(s): {', '.join(sorted(complex_types))}")
            if detected_complex_type:
                if complex_types and detected_complex_type not in complex_types:
                    print(f"  Detected ComplexType: {detected_complex_type}")
                elif not complex_types:
                    print(f"  Detected ComplexType: {detected_complex_type}")
            elif not complex_types:
                print("  ComplexType: AbstractBridge (main structure)")

        print(f"Total Faces: {len(labels)}")
        print("\nSurface Type Distribution:")
        for surface_type, count in sorted(counts.items(), key=lambda item: -item[1]):
            percentage = (count / len(labels)) * 100 if labels else 0.0
            print(f"  {surface_type}: {count} ({percentage:.1f}%)")

        if suggested_type != feature_type:
            print(f"\nSuggested Feature Type: {suggested_type}")
            print("  (based on geometry analysis)")

        if not is_valid:
            print(f"\nValidation Errors: {len(errors)}")
            for error in errors[:5]:
                print(f"  {error}")
            if len(errors) > 5:
                print(f"  ... and {len(errors) - 5} more")
        else:
            print("\nAll surface types are valid")

        if suggestions:
            print(f"\nImprovement Suggestions: {len(suggestions)}")
            for suggestion in suggestions:
                print(f"  {suggestion}")
        else:
            print("\nNo improvement suggestions")

        print("=" * 60 + "\n")

        total_issues = len(errors) + len(suggestions)
        if total_issues > 0:
            self.report({'INFO'}, f"Analysis complete: {total_issues} issue(s) found (see console)")
        else:
            self.report({'INFO'}, "Analysis complete: No issues found")

        return {'FINISHED'}


def register():
    bpy.utils.register_class(CGML3_MODELTYPER_OT_AnalyzeModel)


def unregister():
    try:
        bpy.utils.unregister_class(CGML3_MODELTYPER_OT_AnalyzeModel)
    except RuntimeError:
        pass
