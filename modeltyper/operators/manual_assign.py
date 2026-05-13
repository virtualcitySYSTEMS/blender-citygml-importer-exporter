# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Manual assignment operator for CityGML surface types.
"""

import uuid as _uuid

import bmesh
import bpy

from ..citygml import get_surface_color
from ..core import materials
from ...shared.lod_helpers import (
    material_is_opening,
    material_surface_type,
    opening_lod_for_version,
    promote_opening_host_lod,
    read_lod_value,
)

_OPENING_TYPES = {"Window", "Door"}
_OPENING_PARENT_SURFACE_TYPES = {
    "WallSurface",
    "RoofSurface",
    "GroundSurface",
    "ClosureSurface",
    "InteriorWallSurface",
    "CeilingSurface",
    "FloorSurface",
    "OuterCeilingSurface",
    "OuterFloorSurface",
}
_OPENING_PARENT_RESET_KEYS = (
    "OpeningType",
    "opening_type",
    "is_opening",
    "con_opening_id",
    "opening_id",
    "opening_gml_id",
    "filling_parent_surface_id",
    "opening_surface_id",
    "Interior",
    "ExteriorPolyId",
    "con_surface_id",
    "gml_polygon_id",
    "gml_ring_id",
    "gml_multisurface_id",
    "gml_compositesurface_id",
)


def _is_opening_category(category):
    """Check if the category is an opening type."""
    return category in _OPENING_TYPES


def _is_valid_opening_parent_surface(surface_type):
    return surface_type in _OPENING_PARENT_SURFACE_TYPES


def _delete_material_keys(mat, keys):
    if mat is None:
        return
    for key in keys:
        try:
            if key in mat:
                del mat[key]
        except Exception:
            pass


def _set_parent_surface_type(mat, surface_type):
    mat["SurfaceTyp"] = surface_type
    mat["surface_type"] = surface_type
    mat["Typ"] = surface_type


def _ensure_parent_surface_identity(mat):
    surface_id = str(mat.get("con_surface_id", "") or "").strip()
    if not surface_id:
        surface_id = f"ID_{_uuid.uuid4().hex}"
        mat["con_surface_id"] = surface_id

    polygon_id = str(mat.get("gml_polygon_id", "") or "").strip()
    if not polygon_id:
        polygon_id = f"ID_{_uuid.uuid4().hex}"
        mat["gml_polygon_id"] = polygon_id

    ring_id = str(mat.get("gml_ring_id", "") or "").strip()
    if not ring_id:
        mat["gml_ring_id"] = f"{polygon_id}_0_"

    return surface_id


def _derive_parent_surface_type(face, obj):
    mat_idx = face.material_index
    if 0 <= mat_idx < len(obj.data.materials):
        mat = obj.data.materials[mat_idx]
        surface_type = material_surface_type(mat)
        if _is_valid_opening_parent_surface(surface_type) and not material_is_opening(mat):
            return surface_type
    return "WallSurface"


def _ensure_parent_surface_lod(mat, lod_value):
    if lod_value is None:
        return
    current_lod = read_lod_value(mat.get("lod", None))
    if current_lod is None or current_lod < int(lod_value):
        mat["lod"] = int(lod_value)


def _ensure_opening_parent_material(obj, face, source_material, parent_surface_type, feature_type, version, lod_value):
    source_type = material_surface_type(source_material)
    use_source_as_parent = (
        source_material is not None
        and not material_is_opening(source_material)
        and _is_valid_opening_parent_surface(source_type)
    )

    if use_source_as_parent:
        parent_mat = source_material
    else:
        color = get_surface_color(parent_surface_type, feature_type, version)
        if source_material is not None and materials._material_has_image_texture(source_material):
            parent_mat = source_material.copy()
            parent_mat.name = f"{parent_surface_type}_{_uuid.uuid4().hex[:8]}"
            _delete_material_keys(parent_mat, _OPENING_PARENT_RESET_KEYS)
        else:
            parent_mat = materials.create_material(parent_surface_type, color)

        obj.data.materials.append(parent_mat)
        face.material_index = len(obj.data.materials) - 1

    _set_parent_surface_type(parent_mat, parent_surface_type)
    _ensure_parent_surface_lod(parent_mat, lod_value)
    return _ensure_parent_surface_identity(parent_mat)


def _set_unique_surface_properties(mat, category, version, parent_surface_id="", lod_value=None):
    """Set unique CityGML IDs on a material. For openings, sets additional properties."""
    con_surface_id = f"ID_{_uuid.uuid4().hex}"
    gml_polygon_id = f"ID_{_uuid.uuid4().hex}"
    gml_ring_id = f"{gml_polygon_id}_0_"

    mat["con_surface_id"] = con_surface_id
    mat["gml_polygon_id"] = gml_polygon_id
    mat["gml_ring_id"] = gml_ring_id

    if _is_opening_category(category):
        # Surface type depends on CityGML version
        if version == "2.0":
            surface_type = category  # "Window" or "Door"
        else:
            surface_type = "WindowSurface" if category == "Window" else "DoorSurface"

        short = _uuid.uuid4().hex[:12]
        opening_id = f"UUID_{category}_{short}"

        mat["SurfaceTyp"] = surface_type
        mat["surface_type"] = surface_type
        mat["OpeningType"] = category
        mat["opening_type"] = category
        mat["is_opening"] = True
        mat["con_opening_id"] = opening_id
        mat["opening_id"] = opening_id
        mat["opening_gml_id"] = opening_id

        # Link to the parent boundary surface so the writer nests this
        # opening inside the correct WallSurface/RoofSurface element.
        if parent_surface_id:
            mat["filling_parent_surface_id"] = parent_surface_id
            mat["opening_surface_id"] = parent_surface_id

        mat["lod"] = int(lod_value if lod_value is not None else 3)


def _derive_interior_surface_type(face, obj):
    """Determine what interior surface type to use based on the face's current material."""
    mat_idx = face.material_index
    if 0 <= mat_idx < len(obj.data.materials):
        mat = obj.data.materials[mat_idx]
        if mat is not None:
            source_type = str(
                mat.get("SurfaceTyp")
                or mat.get("surface_type")
                or "WallSurface"
            )
            if source_type in {"WallSurface", "InteriorWallSurface", "CeilingSurface", "FloorSurface", "ClosureSurface"}:
                return source_type
            if source_type in {"RoofSurface", "OuterCeilingSurface"}:
                return "CeilingSurface"
            if source_type in {"GroundSurface", "OuterFloorSurface"}:
                return "FloorSurface"
    return "WallSurface"


def _create_interior_material(interior_type, exterior_polygon_id, obj, version,
                              source_material=None, lod_value=None):
    """Create an interior ring material that references the exterior wall's polygon.

    If *source_material* carries an image texture, the new material is a full
    copy (node tree + textures) so the interior face keeps the same appearance
    as the original wall — identical to the Openings Cutter behaviour.

    The interior SHARES the parent wall's gml_polygon_id and gets a new
    gml_ring_id (e.g. {polygon_id}_1_) so the writer exports it as an
    interior ring of that polygon — just like the Openings Cutter.
    """
    if source_material is not None and materials._material_has_image_texture(source_material):
        mat = source_material.copy()
        mat.name = f"{interior_type}_{_uuid.uuid4().hex[:8]}"
    else:
        color = get_surface_color(interior_type, "BUILDING", version)
        mat = materials.create_material(interior_type, color)

    # Share the parent wall's polygon ID; allocate the next ring index
    parent_poly_id = str(exterior_polygon_id or "").strip()
    if not parent_poly_id:
        parent_poly_id = f"ID_{_uuid.uuid4().hex}"

    # Find next available ring index for this polygon
    next_ring_idx = 1  # 0 is the exterior ring
    if obj and obj.data:
        for m in obj.data.materials:
            if m is None:
                continue
            m_poly = str(m.get("gml_polygon_id", "") or "")
            if m_poly == parent_poly_id:
                m_ring = str(m.get("gml_ring_id", "") or "")
                if m_ring.startswith(f"{parent_poly_id}_"):
                    try:
                        parts = m_ring.rsplit("_", 2)
                        idx = int(parts[-2])
                        next_ring_idx = max(next_ring_idx, idx + 1)
                    except (ValueError, IndexError):
                        pass

    mat["gml_polygon_id"] = parent_poly_id
    mat["gml_ring_id"] = f"{parent_poly_id}_{next_ring_idx}_"
    mat["con_surface_id"] = f"ID_{_uuid.uuid4().hex}"
    mat["lod"] = int(lod_value if lod_value is not None else 3)

    # Interior ring metadata
    mat["Interior"] = True
    mat["SurfaceTyp"] = interior_type
    mat["surface_type"] = interior_type
    mat["Typ"] = interior_type

    # Cross-reference the exterior polygon for semantic linkage
    mat["ExteriorPolyId"] = parent_poly_id

    # Clean opening-specific props that don't belong on interior materials
    for key in ("OpeningType", "opening_type", "is_opening", "con_opening_id",
                "opening_id", "opening_gml_id", "CityGMLTarget",
                "filling_parent_surface_id", "opening_surface_id"):
        if key in mat:
            del mat[key]

    return mat


class CGML3_MODELTYPER_OT_ManualAssign(bpy.types.Operator):
    """Manually assign a surface type to selected faces."""

    bl_idname = "cgml3_modeltyper.manual_assign"
    bl_label = "Assign Surface Type (Manual)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        props = context.scene.cgml3_modeltyper_props
        category = props.category_enum
        feature_type = props.feature_type
        version = props.citygml_version
        preserve_existing_colors = props.preserve_existing_colors_manual

        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "Active object is not a mesh")
            return {'CANCELLED'}

        if context.mode != 'EDIT_MESH':
            self.report({'ERROR'}, "Please switch to Edit Mode")
            return {'CANCELLED'}

        materials.set_model_type(obj, feature_type)

        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()

        selected_faces = [face for face in bm.faces if face.select]
        if not selected_faces:
            self.report({'WARNING'}, "No faces selected")
            return {'CANCELLED'}

        color = get_surface_color(category, feature_type, version)
        complex_types = {
            "BridgeConstructionElement",
            "BridgeInstallation",
            "IntBridgeInstallation",
            "BridgeRoom",
            "BridgeFurniture",
            "AbstractBridge",
        }
        complex_type = category if category in complex_types else None

        # Each face gets its own unique material with unique CityGML IDs
        generate_interior = (
            _is_opening_category(category)
            and props.generate_interior_surface
        )
        interior_faces_to_create = []
        # For openings: we need to create duplicate faces (same geometry) for
        # the Window/Door surface while KEEPING the original face as its parent
        # boundary surface (WallSurface/RoofSurface).
        opening_faces_to_create = []

        is_opening = _is_opening_category(category)
        opening_lod = opening_lod_for_version(obj, version) if is_opening else None

        for face in selected_faces:
            # Remember original material for interior derivation before reassignment
            original_interior_type = _derive_interior_surface_type(face, obj) if generate_interior else None
            parent_surface_type = _derive_parent_surface_type(face, obj) if is_opening else None
            original_material = None
            parent_surface_id = ""
            old_idx = face.material_index
            if 0 <= old_idx < len(obj.data.materials):
                old_mat_ref = obj.data.materials[old_idx]
                if old_mat_ref is not None:
                    original_material = old_mat_ref

            if is_opening:
                # For openings the original face STAYS as the exterior parent
                # (WallSurface/RoofSurface). We ensure it has a unique con_surface_id
                # and create a separate Window/Door duplicate face nested under it.
                parent_surface_id = _ensure_opening_parent_material(
                    obj,
                    face,
                    original_material,
                    parent_surface_type or "WallSurface",
                    feature_type,
                    version,
                    opening_lod,
                )

                # Queue a duplicate face for the Window/Door opening
                opening_faces_to_create.append(
                    (face, parent_surface_id, original_interior_type, original_material)
                )
            else:
                # Non-opening: replace the face material in-place (existing behaviour)
                if preserve_existing_colors:
                    old_mat_idx = face.material_index
                    if 0 <= old_mat_idx < len(obj.data.materials):
                        old_mat = obj.data.materials[old_mat_idx]
                        if old_mat is not None and materials._material_has_image_texture(old_mat):
                            new_mat = old_mat.copy()
                            new_mat.name = f"{category}_{_uuid.uuid4().hex[:8]}"
                            new_mat["SurfaceTyp"] = category
                            new_mat["lod"] = 2
                            if complex_type:
                                new_mat["CityGMLComplexType"] = complex_type
                            _set_unique_surface_properties(new_mat, category, version, "")
                            obj.data.materials.append(new_mat)
                            face.material_index = len(obj.data.materials) - 1
                            continue

                new_mat = materials.create_material(category, color, complex_type)
                new_mat["lod"] = 2
                _set_unique_surface_properties(new_mat, category, version, "")
                obj.data.materials.append(new_mat)
                face.material_index = len(obj.data.materials) - 1

        # Create opening faces (duplicate with same winding = Window/Door surface)
        for src_face, parent_sid, interior_type, src_mat in opening_faces_to_create:
            # Create Window/Door material for the opening face.
            # Always copy texture from source material so the opening inherits
            # the wall's appearance (image texture + UV mapping).
            if src_mat is not None and materials._material_has_image_texture(src_mat):
                opening_mat = src_mat.copy()
                opening_mat.name = f"{category}_{_uuid.uuid4().hex[:8]}"
                opening_mat["SurfaceTyp"] = category
                if complex_type:
                    opening_mat["CityGMLComplexType"] = complex_type
            else:
                opening_mat = materials.create_material(category, color, complex_type)
            opening_mat["lod"] = int(opening_lod if opening_lod is not None else 3)
            _set_unique_surface_properties(opening_mat, category, version, parent_sid, opening_lod)
            obj.data.materials.append(opening_mat)
            opening_mat_idx = len(obj.data.materials) - 1

            # Duplicate the face with SAME vertex order (opening has same orientation)
            new_verts = [bm.verts.new(v.co.copy()) for v in src_face.verts]
            try:
                opening_face = bm.faces.new(new_verts)
                opening_face.material_index = opening_mat_idx

                # Copy UV mapping from source face
                for uv_layer in bm.loops.layers.uv.values():
                    src_uvs = [loop[uv_layer].uv.copy() for loop in src_face.loops]
                    for loop, uv in zip(opening_face.loops, src_uvs):
                        loop[uv_layer].uv = uv
            except ValueError:
                pass

            # Queue interior face creation
            if generate_interior:
                # Interior belongs to the parent wall polygon (not the opening)
                parent_poly_id = ""
                src_face_mat_idx = src_face.material_index
                if 0 <= src_face_mat_idx < len(obj.data.materials):
                    parent_mat = obj.data.materials[src_face_mat_idx]
                    if parent_mat is not None:
                        parent_poly_id = str(parent_mat.get("gml_polygon_id", "") or "")
                interior_faces_to_create.append((src_face, interior_type, parent_poly_id, src_mat))

        # Create interior faces (duplicated with reversed winding + UV copy)
        for src_face, interior_type, ext_poly_id, src_mat in interior_faces_to_create:
            int_mat = _create_interior_material(interior_type, ext_poly_id, obj, version,
                                                source_material=src_mat, lod_value=opening_lod)
            obj.data.materials.append(int_mat)
            int_mat_idx = len(obj.data.materials) - 1

            # Duplicate the face with reversed vertex order
            new_verts = [bm.verts.new(v.co.copy()) for v in reversed(src_face.verts)]
            try:
                int_face = bm.faces.new(new_verts)
                int_face.material_index = int_mat_idx

                # Copy UV mapping from source face (reversed to match winding)
                for uv_layer in bm.loops.layers.uv.values():
                    src_uvs = [loop[uv_layer].uv.copy() for loop in src_face.loops]
                    src_uvs.reverse()
                    for loop, uv in zip(int_face.loops, src_uvs):
                        loop[uv_layer].uv = uv
            except ValueError:
                # Face already exists (e.g. duplicate geometry) — skip
                pass

        # Inline cleanup of unused materials (edit-mode safe using existing bm)
        used = set(f.material_index for f in bm.faces)
        old_mats = list(obj.data.materials)
        idx_map = {}
        new_mats = []
        for i, m in enumerate(old_mats):
            if i in used:
                idx_map[i] = len(new_mats)
                new_mats.append(m)
        if len(new_mats) < len(old_mats):
            for f in bm.faces:
                f.material_index = idx_map.get(f.material_index, 0)
            obj.data.materials.clear()
            for m in new_mats:
                obj.data.materials.append(m)

        bmesh.update_edit_mesh(obj.data, loop_triangles=True)
        if is_opening:
            promote_opening_host_lod(obj, opening_lod=opening_lod or 3)

        self.report({'INFO'}, f"Assigned {category} to {len(selected_faces)} face(s)")
        return {'FINISHED'}


def register():
    bpy.utils.register_class(CGML3_MODELTYPER_OT_ManualAssign)


def unregister():
    try:
        bpy.utils.unregister_class(CGML3_MODELTYPER_OT_ManualAssign)
    except RuntimeError:
        pass
