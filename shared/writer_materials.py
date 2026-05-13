# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io/writer/materials.py

from __future__ import annotations
from typing import Tuple, Optional
import bpy

def extract_base_color_rgba(mat) -> Tuple[float,float,float,float]:
    if not mat:
        return (0.8, 0.8, 0.8, 1.0)
    if getattr(mat, "use_nodes", False) and mat.node_tree:
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            col = bsdf.inputs.get("Base Color")
            if col and hasattr(col, "default_value"):
                dv = col.default_value
                if dv and len(dv) >= 3:
                    return (
                        float(dv[0]),
                        float(dv[1]),
                        float(dv[2]),
                        float(dv[3]) if len(dv) > 3 else 1.0,
                    )
    vc = getattr(mat, "diffuse_color", (0.8, 0.8, 0.8, 1.0))
    return (
        float(vc[0]),
        float(vc[1]),
        float(vc[2]),
        float(vc[3]) if len(vc) > 3 else 1.0,
    )

def extract_x3d_params_from_material(mat) -> dict:
    """
    Liefert ein Dict mit Schlüsseln für X3DMaterial:
      ambientIntensity, emissiveColor(3), specularColor(3),
      shininess, transparency, isSmooth
    """
    params = {}
    try:
        # Ambient gibt es in Blender nicht explizit: sinnvollen Default 0.2 (Schema-Default)
        params["ambientIntensity"] = float(getattr(mat, "cgml3_ambient", 0.2))

        # Basisfarbe
        base = getattr(mat, "diffuse_color", (0.8,0.8,0.8,1.0))
        base_rgb = (float(base[0]), float(base[1]), float(base[2]))

        # Principled BSDF versuchen
        bsdf = None
        if mat and hasattr(mat, "node_tree") and mat.node_tree:
            for n in mat.node_tree.nodes:
                if n.type == 'BSDF_PRINCIPLED':
                    bsdf = n
                    break

        # Emission
        emissive = (0.0, 0.0, 0.0)
        if bsdf:
            try:
                e_col = tuple(bsdf.inputs["Emission"].default_value)[:3]
                e_str = float(bsdf.inputs["Emission Strength"].default_value)
                emissive = tuple(max(0.0, min(1.0, c * e_str)) for c in e_col)
            except Exception:
                pass
        params["emissiveColor"] = emissive

        # Specular-Farbe: einfache Heuristik = base * specular (Skalar)
        spec_val = 0.5
        if bsdf:
            try:
                spec_val = float(bsdf.inputs["Specular"].default_value)
            except Exception:
                pass
        params["specularColor"] = tuple(max(0.0, min(1.0, c * spec_val)) for c in base_rgb)

        # Shininess in X3D ~ "Glanz" -> invertierte Roughness
        rough = 0.5
        if bsdf:
            try:
                rough = float(bsdf.inputs["Roughness"].default_value)
            except Exception:
                pass
        params["shininess"] = max(0.0, min(1.0, 1.0 - rough))

        # Transparency: 1 - Alpha
        alpha = float(base[3]) if len(base) >= 4 else 1.0
        params["transparency"] = max(0.0, min(1.0, 1.0 - alpha))

        # isSmooth: Material-weiter Schalter (Fallback True)
        val = getattr(mat, "use_screen_refraction", None)
        params["isSmooth"] = True if val is None else bool(val)
    except Exception:
        pass
    return params

def first_image_path_from_material(obj: bpy.types.Object, mat_index: int) -> Optional[str]:
    if mat_index < 0 or mat_index >= len(obj.material_slots):
        return None
    mat = obj.material_slots[mat_index].material
    if not mat or not getattr(mat, "use_nodes", False) or not mat.node_tree:
        return None
    for node in mat.node_tree.nodes:
        if node.type == "TEX_IMAGE" and getattr(node, "image", None):
            img = node.image
            if getattr(img, "filepath", ""):
                return bpy.path.abspath(img.filepath)
    # Hinweis: KEIN Fallback auf "app_target_or_uri".
    # Dieser Key wird beim Import auch für app:target / "#<poly_id>" oder andere
    # nicht-Dateiwerte verwendet. Das würde beim Export fälschlicherweise
    # ParameterizedTexture/imageURI erzeugen, obwohl keine Textur existiert.
    return None
