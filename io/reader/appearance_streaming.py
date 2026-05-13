# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Streaming Appearance Parser for CityGML 3.0

Provides memory-efficient appearance parsing for large files using iterparse.
This module extracts X3DMaterial, ParameterizedTexture, and GeoreferencedTexture
data without loading the full XML tree into memory.
"""

try:
    from lxml import etree as ET
    HAS_LXML = True
except ImportError:
    import xml.etree.ElementTree as ET
    HAS_LXML = False

from typing import Dict, Tuple, List, Optional
from pathlib import Path

from .namespaces import NS
from .xml_utils import localname, _norm_id
from .appearance import _resolve_image_path
from .xlink_resolver import XLinkResolver, extract_href


def parse_appearance_streaming(path: str, gml_path: str) -> Tuple[Dict, Dict, Dict, Dict]:
    """
    Parse appearance data from CityGML file using streaming.
    
    Args:
        path: Path to CityGML file
        gml_path: Path for resolving relative image paths
    
    Returns:
        Tuple of (x3d_by_poly, ptex_by_ring, poly_to_image, ptex_by_poly)
        - x3d_by_poly: polygon_id -> (R, G, B) diffuse color
        - ptex_by_ring: ring_id -> {"image": path, "uv": [(u,v), ...], "poly": optional}
        - poly_to_image: polygon_id -> image path
        - ptex_by_poly: polygon_id -> {"image": path, "uv": [(u,v), ...], "ring": optional}
    """
    if not HAS_LXML:
        return {}, {}, {}, {}
    
    x3d_by_poly = {}
    ptex_by_ring = {}
    poly_to_image = {}
    ptex_by_poly = {}
    
    # XLink resolver for efficient reference resolution
    xlink_resolver = XLinkResolver()
    
    # Build gml:id lookup for polygon expansion (X3D materials)
    id_lookup = {}
    
    try:
        # Single pass: Build lookup AND parse appearance in one go
        gml_id_attr = f"{{{NS['gml']}}}id"
        print("[CityGML3] Parsing appearance elements (single pass)...")
        appearance_count = 0
        x3d_count = 0
        ptex_count = 0
        gtex_count = 0
        
        for event, elem in ET.iterparse(path, events=('end',)):
            # Build gml:id lookup for ALL elements
            gml_id = elem.get(gml_id_attr)
            if gml_id:
                id_lookup[gml_id] = elem
            
            tag_ln = localname(elem.tag)
            
            if tag_ln == 'Appearance':
                appearance_count += 1
            
            # X3D Materials - parse immediately while element is intact
            elif tag_ln == 'X3DMaterial':
                x3d_count += 1
                _parse_x3d_material(elem, id_lookup, gml_id_attr, x3d_by_poly)
            
            # Parameterized Textures
            elif tag_ln == 'ParameterizedTexture':
                ptex_count += 1
                _parse_parameterized_texture(elem, gml_path, ptex_by_ring, poly_to_image, ptex_by_poly)
            
            # Georeferenced Textures
            elif tag_ln == 'GeoreferencedTexture':
                gtex_count += 1
                _parse_georeferenced_texture(elem, gml_path, poly_to_image)
        
        print(f"[CityGML3] Built lookup with {len(id_lookup)} elements")
        print(f"[CityGML3] Found {appearance_count} Appearance elements, {x3d_count} X3DMaterial, {ptex_count} ParameterizedTexture, {gtex_count} GeoreferencedTexture")
        print(f"[CityGML3] Parsed {len(x3d_by_poly)} X3D materials, {len(ptex_by_ring)} texture rings, {len(poly_to_image)} poly images")
            
    except Exception as e:
        print(f"Warning: Appearance parsing failed: {e}")
        import traceback
        traceback.print_exc()
    
    return x3d_by_poly, ptex_by_ring, poly_to_image, ptex_by_poly


def _parse_x3d_material(elem, id_lookup: Dict, gml_id_attr: str, out: Dict):
    """Parse single X3DMaterial element."""
    def ln(tag):
        return tag.split("}")[-1] if isinstance(tag, str) and "}" in tag else tag
    
    # Read diffuseColor
    diff = None
    for c in elem.iter():
        if isinstance(c.tag, str) and ln(c.tag) == "diffuseColor" and c.text and c.text.strip():
            diff = c.text.strip()
            break
    
    if not diff:
        return
    
    try:
        parts = [float(x) for x in diff.split()]
        rgb = tuple(max(0.0, min(1.0, v)) for v in parts[:3]) if len(parts) >= 3 else (0.8, 0.8, 0.8)
    except Exception:
        rgb = (0.8, 0.8, 0.8)
    
    # Collect targets
    targets = []
    for t in elem.iter():
        if not isinstance(t.tag, str):
            continue
        l = ln(t.tag)
        if l not in ("target", "uri"):
            continue
        
        # Try xlink:href first, then text content
        href = extract_href(t)
        if href:
            s = href.strip()
        else:
            s = (t.text or "").strip()
        
        if not s:
            continue
        s = _norm_id(s)
        if s:
            targets.append(s)
    
    if not targets:
        return
    
    # Expand targets to polygons
    expanded_count = 0
    for tid in targets:
        out[tid] = rgb
        expanded_count += 1
        
        el = id_lookup.get(tid)
        if el is None or not gml_id_attr:
            continue
        
        # Expand to all child polygons
        for poly in el.iter():
            if not isinstance(poly.tag, str):
                continue
            if ln(poly.tag) not in ("Polygon", "Triangle", "Rectangle"):
                continue
            pgid = poly.get(gml_id_attr)
            if pgid:
                out[pgid] = rgb
                expanded_count += 1
    
    # Silent by default (was debug print)


def _parse_parameterized_texture(elem, gml_path: str, ptex_by_ring: Dict, poly_to_image: Dict, ptex_by_poly: Dict):
    """Parse single ParameterizedTexture element."""
    def ln(tag):
        return tag.split("}")[-1] if isinstance(tag, str) and "}" in tag else tag
    
    def first_text(el, lname):
        for c in el.iter():
            if isinstance(c.tag, str) and ln(c.tag) == lname:
                txt = (c.text or "").strip()
                if txt:
                    return txt
        return ""
    
    def gather_targets(scope):
        targets = []
        for t in scope.iter():
            if not isinstance(t.tag, str):
                continue
            l = ln(t.tag)
            if ln(t.tag) not in ("target", "uri"):
                continue
            href = extract_href(t)
            # CityGML 2.0 uses <app:target uri="#..."> (attribute), CityGML 3.0 often uses xlink:href or text.
            uri_attr = (t.get("uri") or "").strip()
            s = (href or uri_attr or (t.text or "")).strip()
            s = _norm_id(s)
            if s:
                targets.append(s)
        return targets
    
    # Get image URI
    img = first_text(elem, "imageURI")
    if not img:
        return
    
    img_resolved = _resolve_image_path(gml_path, img)
    base_targets = gather_targets(elem)
    
    # Process TextureAssociation elements (CityGML 3)
    had_assoc = False
    for assoc in elem.iter():
        if not isinstance(assoc.tag, str) or ln(assoc.tag) != "TextureAssociation":
            continue
        had_assoc = True
        
        assoc_targets = gather_targets(assoc) or base_targets
        for pid in assoc_targets:
            if pid:
                poly_to_image.setdefault(pid, img_resolved)
        
        # Parse TexCoordList
        for tcl in assoc.iter():
            if not isinstance(tcl.tag, str) or ln(tcl.tag) != "TexCoordList":
                continue
            _parse_texcoordlist(tcl, assoc_targets, img_resolved, ptex_by_ring, ptex_by_poly, poly_to_image)
    
    # CityGML 2: TexCoordList directly under ParameterizedTexture
    if not had_assoc:
        for pid in base_targets:
            if pid:
                poly_to_image.setdefault(pid, img_resolved)
        for tcl in elem.iter():
            if not isinstance(tcl.tag, str) or ln(tcl.tag) != "TexCoordList":
                continue
            _parse_texcoordlist(tcl, base_targets, img_resolved, ptex_by_ring, ptex_by_poly, poly_to_image)


def _parse_texcoordlist(tcl, targets, img_resolved, ptex_by_ring, ptex_by_poly, poly_to_image):
    """Parse TexCoordList element."""
    def ln(tag):
        return tag.split("}")[-1] if isinstance(tag, str) and "}" in tag else tag
    
    claimed_rings = set()
    kids = list(tcl)
    
    for i, tc in enumerate(kids):
        if not isinstance(tc.tag, str) or ln(tc.tag) != "textureCoordinates":
            continue
        
        tc_text = (tc.text or "").strip()
        if not tc_text:
            continue
        
        # Extract ring ID (multiple strategies for CityGML 2/3 compatibility)
        rid_txt = (tc.get("ring") or "").strip()
        
        # CityGML 3: <ring> as child of <textureCoordinates>
        if not rid_txt:
            for rnode in tc.iter():
                if not isinstance(rnode.tag, str):
                    continue
                if ln(rnode.tag) == "ring":
                    cand = (rnode.text or "").strip()
                    if cand:
                        rid_txt = cand
                        break
        
        # CityGML 3: <ring> as separate sibling element
        if not rid_txt:
            j = i
            while j < len(kids):
                sib = kids[j]
                j += 1
                if not isinstance(sib.tag, str) or ln(sib.tag) != "ring":
                    continue
                cand = (sib.text or "").strip()
                cid = _norm_id(cand)
                if cid and cid not in claimed_rings:
                    rid_txt = cand
                    break
        
        rid = _norm_id(rid_txt)
        if rid:
            claimed_rings.add(rid)
        
        # Extract UV pairs
        try:
            vals = [float(x) for x in " ".join(tc_text.split()).split()]
        except ValueError:
            continue
        
        if len(vals) < 2:
            continue
        
        uvs = [(vals[k], vals[k + 1]) for k in range(0, len(vals) - 1, 2)]
        if not uvs:
            continue
        
        # Ring-based mapping
        if rid:
            if rid not in ptex_by_ring:
                entry = {"image": img_resolved, "uv": uvs}
                if targets:
                    entry["poly"] = targets[0]
                ptex_by_ring[rid] = entry
        
        # Polygon-based mapping
        for pid in targets or []:
            if not pid:
                continue
            poly_to_image.setdefault(pid, img_resolved)
            if pid not in ptex_by_poly:
                ptex_by_poly[pid] = {
                    "image": img_resolved,
                    "uv": uvs,
                    "ring": rid or ""
                }


def _parse_georeferenced_texture(elem, gml_path: str, out: Dict):
    """Parse single GeoreferencedTexture element."""
    def ln(tag):
        return tag.split("}")[-1] if isinstance(tag, str) and "}" in tag else tag
    
    def first_text(el, lname: str) -> str:
        for c in el.iter():
            if isinstance(c.tag, str) and ln(c.tag) == lname:
                t = (c.text or "").strip()
                if t:
                    return t
        return ""
    
    # Get image URI
    img = first_text(elem, "imageURI")
    if not img:
        return
    
    img_resolved = _resolve_image_path(gml_path, img)
    
    # Parse reference point
    ref_txt = (first_text(elem, "pos") or first_text(elem, "posList") or first_text(elem, "referencePoint"))
    ref_vals = [float(x) for x in " ".join(ref_txt.split()).split()] if ref_txt else []
    
    if len(ref_vals) < 2:
        return
    
    refpt = (ref_vals[0], ref_vals[1], ref_vals[2] if len(ref_vals) >= 3 else 0.0)
    
    # Parse orientation matrix
    M_txt = first_text(elem, "orientation")
    m_vals = [float(x) for x in " ".join(M_txt.split()).split()] if M_txt else []
    M = (m_vals[0], m_vals[1], m_vals[2], m_vals[3]) if len(m_vals) >= 4 else ((m_vals[0], 0.0, 0.0, m_vals[1]) if len(m_vals) >= 2 else (1.0, 0.0, 0.0, 1.0))
    
    # Collect targets
    targets = []
    for node in elem.iter():
        if not isinstance(node.tag, str):
            continue
        if ln(node.tag) not in ("target", "uri"):
            continue
        href = node.get("{http://www.w3.org/1999/xlink}href")
        s = (href or (node.text or "")).strip()
        s = _norm_id(s)
        if s:
            targets.append(s)
    
    # Map to polygons
    for pid in targets:
        if pid and pid not in out:
            out[pid] = {"image": img_resolved, "refpt": refpt, "M": M}
