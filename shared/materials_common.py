# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# shared/materials_common.py
"""
Common Material Utilities - Shared between CityGML 2.0 and 3.0

This module contains material and texture application functions that are shared
across CityGML versions.
"""

from typing import List, Tuple, Optional, Dict, Any
import bpy
import re


# Mapping-Tabelle für bekannte ADV-URNs und andere spezielle CRS-Bezeichnungen
# Basierend auf AdV-Profil und gängigen deutschen CRS
ADV_CRS_MAPPING = {
    # ETRS89 / UTM mit DHHN (Deutsche Haupthöhennetz) - Häufig in Deutschland
    "urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH": "EPSG:25832+5783",  # UTM32N + DHHN92
    "urn:adv:crs:ETRS89_UTM33*DE_DHHN92_NH": "EPSG:25833+5783",  # UTM33N + DHHN92
    "urn:adv:crs:ETRS89_UTM31*DE_DHHN92_NH": "EPSG:25831+5783",  # UTM31N + DHHN92
    "urn:adv:crs:ETRS89_UTM32*DE_DHHN2016_NH": "EPSG:25832",      # UTM32N + DHHN2016 -> horizontal EPSG only
    "urn:adv:crs:ETRS89_UTM33*DE_DHHN2016_NH": "EPSG:25833",      # UTM33N + DHHN2016 -> horizontal EPSG only
    
    # ETRS89 / UTM ohne Höhenbezug
    "urn:adv:crs:ETRS89_UTM32": "EPSG:25832",
    "urn:adv:crs:ETRS89_UTM33": "EPSG:25833",
    "urn:adv:crs:ETRS89_UTM31": "EPSG:25831",
    
    # Gauss-Krüger Systeme (historisch, noch in Verwendung)
    "urn:adv:crs:DE_DHDN_3GK2*DE_DHN92_NH": "EPSG:31466+5783",
    "urn:adv:crs:DE_DHDN_3GK3*DE_DHN92_NH": "EPSG:31467+5783",
    "urn:adv:crs:DE_DHDN_3GK4*DE_DHN92_NH": "EPSG:31468+5783",
    "urn:adv:crs:DE_DHDN_3GK5*DE_DHN92_NH": "EPSG:31469+5783",
}

ADV_VERTICAL_EPSG_MAPPING = {
    "urn:adv:crs:ETRS89_UTM32*DE_DHHN2016_NH": "7837",
    "urn:adv:crs:ETRS89_UTM33*DE_DHHN2016_NH": "7837",
}


def normalize_epsg_code(srs: str) -> Optional[int]:
    """
    Extract EPSG code from SRS string.
    
    Args:
        srs: SRS string (e.g., "urn:ogc:def:crs:EPSG::25832")
    
    Returns:
        EPSG code as integer, or None if not found
    """
    if not srs:
        return None
    
    # Pattern: EPSG::12345 or EPSG:12345
    match = re.search(r'EPSG[:]+(\d+)', srs, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    
    return None


def _extract_epsg_hits(s: str) -> List[str]:
    """
    Extract EPSG numbers from common CRS encodings and return them in order.
    """
    hits: List[str] = []

    for match in re.finditer(r"epsg[^,\s]*", s, flags=re.IGNORECASE):
        segment = match.group(0)
        nums = re.findall(r"(?<!\d)(\d{3,6})(?!\d)", segment)
        if nums:
            hits.append(nums[-1])

    if hits:
        return hits

    return re.findall(r"epsg[^0-9]{0,12}([0-9]{3,6})", s, flags=re.IGNORECASE)


def normalize_crs_to_epsg(srs_name: str) -> str:
    """
    Extrahiert und normalisiert CRS-Informationen in EPSG-Format.
    
    Diese Funktion erkennt verschiedene CRS-Schreibweisen:
    - Standard EPSG: "EPSG:25832", "epsg:4326"
    - OGC URNs: "urn:ogc:def:crs:EPSG::25832"
    - Compound CRS: "urn:ogc:def:crs,crs:EPSG::25832,crs:EPSG::5783"
    - HTTP URLs: "http://www.opengis.net/def/crs/EPSG/0/25832"
    - ADV-URNs: "urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH"
    
    Args:
        srs_name: CRS-String aus der GML-Datei (z.B. aus srsName-Attribut)
    
    Returns:
        Normalisierter EPSG-Code als String (z.B. "EPSG:25832" oder "EPSG:25832+5783")
        oder "Unknown CRS" wenn nicht erkannt
    
    Examples:
        >>> normalize_crs_to_epsg("EPSG:25832")
        'EPSG:25832'
        >>> normalize_crs_to_epsg("urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH")
        'EPSG:25832+5783'
        >>> normalize_crs_to_epsg("urn:ogc:def:crs,crs:EPSG::25832,crs:EPSG::5783")
        'EPSG:25832+5783'
    """
    if not srs_name:
        return "Unknown CRS"
    
    s = str(srs_name).strip()
    
    # 1. Prüfe ADV-URN Mapping (höchste Priorität, da spezifisch)
    if s in ADV_CRS_MAPPING:
        return ADV_CRS_MAPPING[s]
    
    # 2. Prüfe auf Varianten von ADV-URNs (case-insensitive match für Teilstrings)
    s_lower = s.lower()
    for adv_urn, epsg_code in ADV_CRS_MAPPING.items():
        if adv_urn.lower() in s_lower:
            return epsg_code
    
    # 3. Standard EPSG-Code Extraktion (für alle anderen Fälle)
    # Findet EPSG-Codes in verschiedenen Formaten:
    # - "EPSG:25832"
    # - "urn:ogc:def:crs:EPSG::25832"
    # - "urn:x-ogc:def:crs:EPSG:6.18.3:4326"
    # - "http://www.opengis.net/gml/srs/epsg.xml#3857"
    # - "http://www.opengis.net/def/crs/EPSG/0/4258"
    # - Compound: "urn:ogc:def:crs,crs:EPSG:6.12:31466,crs:EPSG:6.12:5783"
    
    hits = _extract_epsg_hits(s)
    
    if not hits:
        return "Unknown CRS"
    
    # Wenn mehrere EPSG-Codes gefunden (Compound CRS)
    if len(hits) >= 2:
        # Erste ist horizontal, zweite ist vertikal
        return f"EPSG:{hits[0]}+{hits[1]}"
    else:
        return f"EPSG:{hits[0]}"


def extract_vertical_epsg(srs_name: str) -> str:
    """
    Extrahiert die vertikale EPSG aus einem Compound-CRS.
    
    Args:
        srs_name: CRS-String (z.B. "urn:ogc:def:crs,crs:EPSG::25832,crs:EPSG::5783")
    
    Returns:
        Vertikale EPSG als String (z.B. "5783") oder "" wenn nicht vorhanden
    """
    if not srs_name:
        return ""
    
    s = str(srs_name).strip()

    if s in ADV_VERTICAL_EPSG_MAPPING:
        return ADV_VERTICAL_EPSG_MAPPING[s]

    s_lower = s.lower()
    for adv_urn, z_epsg in ADV_VERTICAL_EPSG_MAPPING.items():
        if adv_urn.lower() in s_lower:
            return z_epsg
    
    # 1. Prüfe ADV-URN Mapping
    if s in ADV_CRS_MAPPING:
        epsg_str = ADV_CRS_MAPPING[s]
        # Extrahiere vertikalen Teil nach '+'
        if '+' in epsg_str:
            return epsg_str.split('+')[1]
        return ""
    
    # 2. Prüfe auf Varianten von ADV-URNs
    s_lower = s.lower()
    for adv_urn, epsg_code in ADV_CRS_MAPPING.items():
        if adv_urn.lower() in s_lower:
            if '+' in epsg_code:
                return epsg_code.split('+')[1]
            return ""
    
    # 3. Standard-Extraktion aus Compound CRS
    hits = _extract_epsg_hits(s)
    
    if len(hits) >= 2:
        return hits[1]
    
    # 3D-geographisch (ellipsoidisch) hat keinen separaten Z-EPSG
    first = hits[0] if hits else ""
    if first in ("4979", "4937"):
        return ""
    
    return ""


def get_or_create_material_by_name(name: str, 
                                   diffuse_color: Tuple[float, float, float, float] = (0.8, 0.8, 0.8, 1.0),
                                   use_nodes: bool = True) -> bpy.types.Material:
    """
    Get existing material by name or create a new one.
    
    Args:
        name: Material name
        diffuse_color: RGBA color tuple (default: light gray)
        use_nodes: Whether to use node-based material
    
    Returns:
        Blender material
    """
    # Try to get existing material
    mat = bpy.data.materials.get(name)
    if mat:
        return mat
    
    # Create new material
    mat = bpy.data.materials.new(name=name)
    mat.diffuse_color = diffuse_color
    mat.use_nodes = use_nodes
    
    # Set up basic node tree for Principled BSDF
    if use_nodes and mat.node_tree:
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        
        # Clear default nodes
        nodes.clear()
        
        # Add Principled BSDF
        bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
        bsdf.location = (0, 0)
        bsdf.inputs['Base Color'].default_value = diffuse_color
        
        # Add Material Output
        output = nodes.new(type='ShaderNodeOutputMaterial')
        output.location = (300, 0)
        
        # Link BSDF to output
        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    
    return mat


def ensure_material_slot(obj: bpy.types.Object, material: bpy.types.Material) -> int:
    """
    Ensure material is in object's material slots, return slot index.
    
    Args:
        obj: Blender object
        material: Material to add
    
    Returns:
        Material slot index (0-based)
    """
    # Check if material already exists in slots
    for i, slot in enumerate(obj.material_slots):
        if slot.material == material:
            return i
    
    # Add new slot
    obj.data.materials.append(material)
    return len(obj.material_slots) - 1


def create_image_texture_node(node_tree, image_path: str, location: Tuple[float, float] = (-300, 0)):
    """
    Create an Image Texture node in a material node tree.
    
    Args:
        node_tree: Material node tree
        image_path: Path to image file
        location: Node location in editor
    
    Returns:
        Image Texture node
    """
    nodes = node_tree.nodes
    
    # Create Image Texture node
    img_node = nodes.new(type='ShaderNodeTexImage')
    img_node.location = location
    
    # Load image
    try:
        img = bpy.data.images.load(image_path, check_existing=True)
        img_node.image = img
    except Exception:
        print(f"Warning: Could not load image: {image_path}")
    
    return img_node


def setup_texture_material(mat: bpy.types.Material, 
                          image_path: str,
                          diffuse_color: Optional[Tuple[float, float, float, float]] = None):
    """
    Set up a material with image texture.
    
    Args:
        mat: Material to configure
        image_path: Path to texture image
        diffuse_color: Optional base color (RGBA)
    """
    if not mat.use_nodes:
        mat.use_nodes = True
    
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    
    # Find or create Principled BSDF
    bsdf = None
    for node in nodes:
        if node.type == 'BSDF_PRINCIPLED':
            bsdf = node
            break
    
    if not bsdf:
        bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
        bsdf.location = (0, 0)
        
        # Link to output
        output = None
        for node in nodes:
            if node.type == 'OUTPUT_MATERIAL':
                output = node
                break
        if not output:
            output = nodes.new(type='ShaderNodeOutputMaterial')
            output.location = (300, 0)
        
        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    
    # Create image texture node
    img_node = create_image_texture_node(mat.node_tree, image_path, location=(-300, 0))
    
    # Link texture to BSDF
    links.new(img_node.outputs['Color'], bsdf.inputs['Base Color'])
    links.new(img_node.outputs['Alpha'], bsdf.inputs['Alpha'])
    
    # Set base color if provided
    if diffuse_color:
        bsdf.inputs['Base Color'].default_value = diffuse_color


def order_uvs_for_face(face_verts: List[Any], uv_coords: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """
    Order UV coordinates to match face vertex order.
    
    This function handles the common case where UV coordinates need to be
    reordered to match the winding order of face vertices.
    
    Args:
        face_verts: List of face vertices (BMesh or mesh vertices)
        uv_coords: List of UV coordinates
    
    Returns:
        Ordered UV coordinates matching face vertex order
    """
    if len(face_verts) != len(uv_coords):
        # Cannot reorder if counts don't match
        return uv_coords
    
    # For most cases, UV coords are already in correct order
    # This is a placeholder for more complex reordering logic if needed
    return uv_coords


def apply_uv_coordinates(mesh_obj: bpy.types.Object, 
                        face_idx: int,
                        uv_coords: List[Tuple[float, float]],
                        uv_layer_name: str = "UVMap"):
    """
    Apply UV coordinates to a specific face.
    
    Args:
        mesh_obj: Mesh object
        face_idx: Face index
        uv_coords: List of (u, v) coordinates for each vertex
        uv_layer_name: Name of UV layer to use
    """
    mesh = mesh_obj.data
    
    # Ensure UV layer exists
    if not mesh.uv_layers:
        mesh.uv_layers.new(name=uv_layer_name)
    
    uv_layer = mesh.uv_layers.get(uv_layer_name)
    if not uv_layer:
        uv_layer = mesh.uv_layers.new(name=uv_layer_name)
    
    # Get face
    if face_idx >= len(mesh.polygons):
        return
    
    face = mesh.polygons[face_idx]
    
    # Apply UV coords to face loops
    for i, loop_idx in enumerate(face.loop_indices):
        if i < len(uv_coords):
            uv_layer.data[loop_idx].uv = uv_coords[i]


def parse_x3d_material(appearance_el, ns_dict: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """
    Parse X3D material properties from appearance element.
    
    Args:
        appearance_el: XML appearance element
        ns_dict: Namespace dictionary
    
    Returns:
        Dictionary with material properties (diffuseColor, specularColor, etc.)
    """
    # Look for X3D Material
    for mat_el in appearance_el.findall(".//app:X3DMaterial", ns_dict):
        material = {}
        
        # Parse color attributes
        for attr in ['diffuseColor', 'specularColor', 'emissiveColor']:
            val = mat_el.get(attr)
            if val:
                try:
                    parts = val.split()
                    if len(parts) >= 3:
                        material[attr] = (float(parts[0]), float(parts[1]), float(parts[2]))
                except (ValueError, IndexError):
                    continue
        
        # Parse scalar attributes
        for attr in ['ambientIntensity', 'shininess', 'transparency']:
            val = mat_el.get(attr)
            if val:
                try:
                    material[attr] = float(val)
                except ValueError:
                    continue
        
        if material:
            return material
    
    return None


def apply_x3d_material_properties(mat: bpy.types.Material, x3d_props: Dict[str, Any]):
    """
    Apply X3D material properties to Blender material.
    
    Args:
        mat: Blender material
        x3d_props: Dictionary with X3D material properties
    """
    if not mat.use_nodes:
        mat.use_nodes = True
    
    nodes = mat.node_tree.nodes
    
    # Find Principled BSDF
    bsdf = None
    for node in nodes:
        if node.type == 'BSDF_PRINCIPLED':
            bsdf = node
            break
    
    if not bsdf:
        return
    
    # Apply diffuse color
    if 'diffuseColor' in x3d_props:
        r, g, b = x3d_props['diffuseColor']
        alpha = 1.0 - x3d_props.get('transparency', 0.0)
        bsdf.inputs['Base Color'].default_value = (r, g, b, alpha)
    
    # Apply specular color (use as specular tint)
    if 'specularColor' in x3d_props:
        # Principled BSDF doesn't have direct specular color
        # Can approximate with metallic/roughness
        pass
    
    # Apply shininess (inverse of roughness)
    if 'shininess' in x3d_props:
        shininess = x3d_props['shininess']
        # Shininess typically ranges 0-1, convert to roughness
        roughness = 1.0 - min(1.0, max(0.0, shininess))
        bsdf.inputs['Roughness'].default_value = roughness
    
    # Apply emissive color
    if 'emissiveColor' in x3d_props:
        r, g, b = x3d_props['emissiveColor']
        bsdf.inputs['Emission'].default_value = (r, g, b, 1.0)
        bsdf.inputs['Emission Strength'].default_value = 1.0
    
    # Apply transparency
    if 'transparency' in x3d_props:
        transparency = x3d_props['transparency']
        if transparency > 0.0:
            mat.blend_method = 'BLEND'
            bsdf.inputs['Alpha'].default_value = 1.0 - transparency


def sanitize_material_name(name: str) -> str:
    """
    Sanitize material name to be valid for Blender.
    
    Args:
        name: Raw material name
    
    Returns:
        Sanitized material name
    """
    # Remove invalid characters
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    
    # Limit length
    if len(name) > 63:
        name = name[:63]
    
    # Ensure non-empty
    if not name:
        name = "Material"
    
    return name
