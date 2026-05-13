# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Material Management for CityGML ModelTyper
Handles material creation and face assignment
"""

import bpy
import bmesh
import uuid
from datetime import datetime
from ..citygml import get_surface_color

_OPENING_TYPES = {"Window", "Door"}


def _set_opening_properties_on_material(mat, category, version):
    """Set required CityGML opening custom properties on a material."""
    if version == "2.0":
        surface_type = category  # "Window" or "Door"
    else:
        surface_type = "WindowSurface" if category == "Window" else "DoorSurface"

    short = uuid.uuid4().hex[:12]
    opening_id = f"UUID_{category}_{short}"
    con_surface_id = f"ID_{uuid.uuid4().hex}"
    gml_polygon_id = f"ID_{uuid.uuid4().hex}"
    gml_ring_id = f"{gml_polygon_id}_0_"

    mat["SurfaceTyp"] = surface_type
    mat["surface_type"] = surface_type
    mat["OpeningType"] = category
    mat["opening_type"] = category
    mat["is_opening"] = True
    mat["con_opening_id"] = opening_id
    mat["opening_id"] = opening_id
    mat["opening_gml_id"] = opening_id
    mat["con_surface_id"] = con_surface_id
    mat["gml_polygon_id"] = gml_polygon_id
    mat["gml_ring_id"] = gml_ring_id


def clear_object_materials(obj):
    """Remove all material slots from mesh object"""
    if not obj or obj.type != 'MESH':
        return
    # Face-Indices neutralisieren (optional, robust)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    for f in bm.faces:
        f.material_index = 0
    bm.to_mesh(obj.data)
    bm.free()
    # Slots löschen
    obj.data.materials.clear()


def create_material(category, color, complex_type=None):
    """Create a new material with SurfaceTyp metadata and optional CityGML ComplexType
    
    Args:
        category: Surface type (e.g., "WallSurface", "RoofSurface")
        color: RGBA color tuple
        complex_type: Optional CityGML complex type (e.g., "BridgeInstallation", "BridgeConstructionElement")
    """
    uid = str(uuid.uuid4())
    mat_name = f"{category}_{uid}"
    mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs['Base Color'].default_value = color
        try:
            bsdf.inputs['Roughness'].default_value = 0.7
            bsdf.inputs['Specular'].default_value = 0.1
        except Exception:
            pass
    mat["uuid"] = uid
    mat["SurfaceTyp"] = category
    mat["created_at"] = datetime.now().isoformat()
    
    # Add CityGML ComplexType if provided
    if complex_type:
        mat["CityGMLComplexType"] = complex_type
    
    return mat


def set_model_type(obj, model_type_str):
    """Set ModelType custom property on object"""
    try: 
        obj["ModelType"] = model_type_str
    except Exception: 
        pass


def find_or_create_material(obj, surface_type, color, complex_type=None):
    """
    Find existing material with SurfaceTyp or create new one
    Preserves existing material properties if found
    
    Args:
        obj: Blender mesh object
        surface_type: Surface type name
        color: Material color
        complex_type: Optional CityGML complex type (e.g., "BridgeInstallation")
    
    Returns:
        Material object and its index in obj.data.materials
    """
    # Search for existing material with this SurfaceTyp
    for idx, mat in enumerate(obj.data.materials):
        if mat and mat.get("SurfaceTyp") == surface_type:
            # Found existing material - update color and properties
            mat["SurfaceTyp"] = surface_type
            mat["updated_at"] = datetime.now().isoformat()
            
            # Update ComplexType if provided
            if complex_type:
                mat["CityGMLComplexType"] = complex_type
            
            # Update color
            if mat.use_nodes:
                bsdf = mat.node_tree.nodes.get("Principled BSDF")
                if bsdf:
                    bsdf.inputs['Base Color'].default_value = color
            
            return mat, idx
    
    # No existing material found - create new one
    mat = create_material(surface_type, color, complex_type)
    
    # Copy custom properties from existing materials (preserve user data)
    reserved_props = {"SurfaceTyp", "uuid", "created_at", "updated_at", "CityGMLComplexType"}
    if len(obj.data.materials) > 0:
        # Find first material with custom properties
        for existing_mat in obj.data.materials:
            if existing_mat:
                # Copy all custom properties except reserved ones
                for key in existing_mat.keys():
                    if key not in reserved_props:
                        try:
                            mat[key] = existing_mat[key]
                        except Exception:
                            pass
                break
    
    obj.data.materials.append(mat)
    return mat, len(obj.data.materials) - 1


def _material_has_image_texture(mat):
    """Check if material has an Image Texture node connected."""
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return False
    for node in mat.node_tree.nodes:
        if node.type == 'TEX_IMAGE' and node.image is not None:
            return True
    return False


def assign_materials_per_face(obj, labels, model_type="BUILDING", version="2.0", complex_type=None, preserve_existing_colors=False):
    """
    Assign materials to faces based on classification labels
    Reuses existing materials with matching SurfaceTyp to preserve custom properties
    
    Args:
        obj: Blender mesh object
        labels: List of surface type labels (one per face)
        model_type: Feature type (e.g., "BUILDING", "BRIDGE")
        version: CityGML version
        complex_type: Optional CityGML complex type (e.g., "BridgeInstallation")
        preserve_existing_colors: If True, keep existing texture/color for faces that already have materials with images or non-default colors
    """
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    
    # When preserving colors, record existing face->material mapping
    existing_face_materials = {}
    if preserve_existing_colors:
        for f in bm.faces:
            mat_idx = f.material_index
            if 0 <= mat_idx < len(obj.data.materials):
                mat = obj.data.materials[mat_idx]
                if mat is not None and _material_has_image_texture(mat):
                    existing_face_materials[f.index] = mat
    
    # Create dictionary to cache material indices per surface type
    material_cache = {}
    
    for f in bm.faces:
        surface_type = labels[f.index]
        
        # If preserving colors and face already had a textured material,
        # create a copy that keeps the visual appearance but gets the new SurfaceTyp
        if preserve_existing_colors and f.index in existing_face_materials:
            old_mat = existing_face_materials[f.index]
            cache_key = (surface_type, old_mat.name)
            if cache_key not in material_cache:
                new_mat = old_mat.copy()
                new_mat.name = f"{surface_type}_{uuid.uuid4().hex[:8]}"
                new_mat["SurfaceTyp"] = surface_type
                if complex_type:
                    new_mat["CityGMLComplexType"] = complex_type
                if surface_type in _OPENING_TYPES:
                    _set_opening_properties_on_material(new_mat, surface_type, version)
                obj.data.materials.append(new_mat)
                material_cache[cache_key] = len(obj.data.materials) - 1
            f.material_index = material_cache[cache_key]
            continue
        
        # Check cache first
        if surface_type not in material_cache:
            col = get_surface_color(surface_type, model_type, version)
            mat, mat_idx = find_or_create_material(obj, surface_type, col, complex_type)
            # Set opening properties if this is a Window/Door type
            if surface_type in _OPENING_TYPES:
                _set_opening_properties_on_material(mat, surface_type, version)
            material_cache[surface_type] = mat_idx
        
        f.material_index = material_cache[surface_type]
    
    bm.to_mesh(obj.data)
    bm.free()
    
    # Cleanup unused materials
    cleanup_unused_materials(obj)


def cleanup_unused_materials(obj):
    """Remove unused material slots from object"""
    if not obj or obj.type != 'MESH':
        return
    
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    
    # Find used material indices
    used = set(f.material_index for f in bm.faces)
    
    # Build mapping
    old = list(obj.data.materials)
    idx_map = {}
    new = []
    for i, m in enumerate(old):
        if i in used:
            idx_map[i] = len(new)
            new.append(m)
    
    # Remap face indices
    for f in bm.faces: 
        f.material_index = idx_map.get(f.material_index, 0)
    
    # Apply new materials
    obj.data.materials.clear()
    for m in new: 
        obj.data.materials.append(m)
    
    bm.to_mesh(obj.data)
    bm.free()


def assign_uv_metadata(obj):
    """
    Assign per-face materials with unique gml_polygon_id/gml_ring_id for correct
    texture coordinate export (ParameterizedTexture / TexCoordList targeting).

    For OBJ imports (non-CityGML sources), we intentionally do NOT store
    cgml3_uv_start_by_ring / cgml3_uv_sign_by_ring because:
    - The exporter writes UV rings in the same loop_indices order as geometry,
      so positional correspondence is already correct without rotation.
    - Storing uv_start would trigger _rotate_to_start in the appearance writer,
      which can produce incorrect UV-geometry correspondence if
      _order_uvs_for_loop_vertices reorders the ring (non-identity world matrix).

    Call this after assign_materials_per_face when the mesh has UV data.
    """
    if not obj or obj.type != 'MESH':
        return
    mesh = obj.data
    if not mesh.uv_layers.active:
        return

    # Remove any stale UV metadata from previous runs (prevents incorrect rotation)
    for key in ("cgml3_uv_start_by_ring", "cgml3_uv_sign_by_ring"):
        try:
            del mesh[key]
        except KeyError:
            pass

    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()

    new_materials = []
    face_mat_indices = []

    for f in bm.faces:
        old_mat_idx = f.material_index
        base_mat = mesh.materials[old_mat_idx] if 0 <= old_mat_idx < len(mesh.materials) else None

        # Generate unique IDs with correct format for writer grouping
        poly_id = f"UUID_{uuid.uuid4().hex}"
        ring_id = f"{poly_id}_0_"
        con_surface_id = f"ID_{uuid.uuid4().hex}"
        gml_multisurface_id = f"ID_{uuid.uuid4().hex}"

        # Copy material and attach IDs
        if base_mat is not None:
            mat = base_mat.copy()
            mat.name = f"{base_mat.get('SurfaceTyp', 'Surface')}_{uuid.uuid4().hex[:8]}"
        else:
            mat = bpy.data.materials.new(name=f"Surface_{uuid.uuid4().hex[:8]}")
        mat["gml_polygon_id"] = poly_id
        mat["gml_ring_id"] = ring_id
        mat["con_surface_id"] = con_surface_id
        mat["gml_multisurface_id"] = gml_multisurface_id
        mat["lod"] = 2

        new_materials.append(mat)
        face_mat_indices.append(len(new_materials) - 1)

    # Reassign materials on mesh
    mesh.materials.clear()
    for mat in new_materials:
        mesh.materials.append(mat)

    # Reassign face material indices
    for f, mat_idx in zip(bm.faces, face_mat_indices):
        f.material_index = mat_idx

    bm.to_mesh(mesh)
    bm.free()

    _mark_detected_interior_ring_materials(obj)


def _next_interior_polygon_index(mesh, exterior_poly_id: str) -> int:
    next_index = 1
    if not exterior_poly_id:
        return next_index

    for mat in mesh.materials:
        if mat is None:
            continue
        try:
            mat_poly_id = str(mat.get("gml_polygon_id", "") or "")
            mat_ext_id = str(mat.get("ExteriorPolyId", "") or "")
        except Exception:
            continue

        suffix = ""
        if mat_ext_id == exterior_poly_id and mat_poly_id.startswith(f"{exterior_poly_id}_"):
            suffix = mat_poly_id[len(exterior_poly_id) + 1:]
        elif mat_poly_id.startswith(f"{exterior_poly_id}_"):
            suffix = mat_poly_id[len(exterior_poly_id) + 1:]
        if not suffix:
            continue

        try:
            value = int(suffix)
        except (TypeError, ValueError):
            continue
        next_index = max(next_index, value + 1)

    return next_index


def _mark_detected_interior_ring_materials(obj):
    """Mark detected inner rings on auto-assigned meshes.

    OBJ and other non-CityGML meshes can already contain paired faces that
    represent a GML interior ring. Auto assignment gives every face CityGML IDs;
    this pass adds the missing Interior/Exterior* relationship where a smaller
    opposite face is contained in a larger exterior face.
    """
    if not obj or obj.type != 'MESH' or not obj.data:
        return

    try:
        from ...ops.get_inner_and_outer_rings import detect_face_groups
    except Exception:
        try:
            from ops.get_inner_and_outer_rings import detect_face_groups
        except Exception:
            return

    try:
        pairs = detect_face_groups(obj, obj.data) or []
    except Exception:
        return
    if not pairs:
        return

    mesh = obj.data
    for outer, inner in pairs:
        try:
            outer_index = int(outer["index"])
            inner_index = int(inner["index"])
            outer_poly = mesh.polygons[outer_index]
            inner_poly = mesh.polygons[inner_index]
        except Exception:
            continue

        try:
            outer_mat = mesh.materials[int(outer_poly.material_index)]
            inner_mat = mesh.materials[int(inner_poly.material_index)]
        except Exception:
            continue
        if outer_mat is None or inner_mat is None:
            continue

        try:
            if bool(inner_mat.get("Interior", False)) and inner_mat.get("ExteriorPolyId"):
                continue
        except Exception:
            pass

        exterior_poly_id = str(outer_mat.get("gml_polygon_id", "") or "").strip()
        exterior_ring_id = str(outer_mat.get("gml_ring_id", "") or "").strip()
        if not exterior_poly_id:
            exterior_poly_id = f"UUID_{uuid.uuid4().hex}"
            outer_mat["gml_polygon_id"] = exterior_poly_id
        if not exterior_ring_id:
            exterior_ring_id = f"{exterior_poly_id}_0_"
            outer_mat["gml_ring_id"] = exterior_ring_id

        interior_index = _next_interior_polygon_index(mesh, exterior_poly_id)
        surface_type = str(
            outer_mat.get("SurfaceTyp")
            or outer_mat.get("surface_type")
            or inner_mat.get("SurfaceTyp")
            or inner_mat.get("surface_type")
            or "WallSurface"
        )

        inner_mat["Interior"] = True
        inner_mat["ExteriorPolyId"] = exterior_poly_id
        inner_mat["ExteriorRingId"] = exterior_ring_id
        inner_mat["gml_polygon_id"] = f"{exterior_poly_id}_{interior_index}"
        inner_mat["gml_ring_id"] = f"UUID_{uuid.uuid4().hex}"
        inner_mat["SurfaceTyp"] = surface_type
        inner_mat["surface_type"] = surface_type
        inner_mat["Typ"] = surface_type
        try:
            inner_mat["lod"] = int(inner_mat.get("lod", outer_mat.get("lod", 2)) or 2)
        except (TypeError, ValueError):
            inner_mat["lod"] = 2


def register():
    pass

def unregister():
    pass
