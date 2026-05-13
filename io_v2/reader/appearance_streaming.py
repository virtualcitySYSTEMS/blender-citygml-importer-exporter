# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Streaming Appearance Parser for CityGML 2.0

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

try:
    from .namespaces import NS
    from ..reader.xml_utils import localname, _norm_id
    from .appearance import _resolve_image_path
except ImportError:  # allows standalone execution for debugging
    from io_v2.reader.namespaces import NS  # type: ignore
    from io_v2.reader.xml_utils import localname, _norm_id  # type: ignore
    from io_v2.reader.appearance import _resolve_image_path  # type: ignore


def extract_href(el) -> str:
    """
    Extract a reference URI from common CityGML link patterns.

    Supports:
    - xlink:href="#id"
    - CityGML 2.0 app:target uri="#id" (attribute "uri")
    """
    try:
        href = (el.get("{http://www.w3.org/1999/xlink}href") or "").strip()
        if href:
            return href
    except Exception:
        pass
    return ""


def extract_uri_attr(el) -> str:
    """
    Extract CityGML 2.0 `<app:target uri="#...">` attribute.

    Note: The attribute is NOT namespaced.
    """
    try:
        return (el.get("uri") or "").strip()
    except Exception:
        return ""


def parse_appearance_streaming(path: str, gml_path: str) -> Tuple[Dict, Dict, Dict, Dict, Dict, Dict]:
    """
    Parse appearance data from a CityGML 2.0 file using streaming (iterparse).

    Returns:
        Tuple of (x3d_by_poly, ptex_by_ring, poly_to_image, ptex_by_poly, poly_to_app, theme_by_app)
        - x3d_by_poly: polygon_id -> (R, G, B) diffuse color
        - ptex_by_ring: ring_id -> {"image": path, "uv": [(u,v), ...], "poly": optional}
        - poly_to_image: polygon_id -> image path
        - ptex_by_poly: polygon_id -> {"image": path, "uv": [(u,v), ...], "ring": optional}
        - poly_to_app: polygon_id -> Appearance gml:id (used to re-export grouped appearances)
        - theme_by_app: Appearance gml:id -> theme string
    """
    if not HAS_LXML:
        return {}, {}, {}, {}, {}, {}

    x3d_by_poly: Dict = {}
    ptex_by_ring: Dict = {}
    poly_to_image: Dict = {}
    ptex_by_poly: Dict = {}
    poly_to_app: Dict = {}
    theme_by_app: Dict = {}

    seen_parameterized_texture = 0

    # Build gml:id lookup for polygon expansion (X3D materials may target MultiSurface/CompositeSurface/...)
    id_lookup: Dict[str, object] = {}
    gml_id_attr = f"{{{NS['gml']}}}id"

    # NOTE: We avoid a full first pass building an element lookup because it keeps
    # references to the entire parsed tree and interacts poorly with elem.clear().
    # For streaming texture import we mainly need ParameterizedTexture mappings.

    try:
        current_app_id: Optional[str] = None
        current_theme: Optional[str] = None

        # IMPORTANT: We can NOT rely on `elem.iter()` at parent end-events because
        # Blender's run clears elements during streaming elsewhere, leaving parents
        # without children at their end-event (even though counters still see child ends).
        # Therefore we collect appearance data incrementally at child end-events.
        #
        # State for current ParameterizedTexture scope
        current_pt_image_uri: str = ""
        current_pt_targets: List[str] = []

        # State for current TexCoordList scope (inside current PT)
        current_tcl_rids: List[str] = []
        current_tcl_uvs: List[tuple[float, float]] = []

        for _event, elem in ET.iterparse(path, events=("end",)):
            tag_ln = localname(elem.tag)

            if tag_ln == "Appearance":
                # Start of a new Appearance scope
                current_app_id = elem.get(gml_id_attr)
                current_theme = None

            if tag_ln == "theme" and current_app_id:
                txt = (elem.text or "").strip()
                if txt:
                    current_theme = txt

            elif tag_ln == "X3DMaterial":
                # Best-effort only: X3D may require full element lookup for expansion.
                # We still parse direct targets; expanded targets require id_lookup.
                _parse_x3d_material(elem, id_lookup, gml_id_attr, x3d_by_poly, poly_to_app, current_app_id)

            elif tag_ln == "imageURI":
                # Capture imageURI when we're inside a ParameterizedTexture
                # (CityGML2 exports use <app:imageURI> as direct child of ParameterizedTexture).
                txt = (elem.text or "").strip()
                if txt:
                    current_pt_image_uri = txt

            elif tag_ln == "target":
                # CityGML2: <app:target uri="#...">
                uri_attr = extract_uri_attr(elem)
                href = extract_href(elem)
                s = (href or uri_attr or (elem.text or "")).strip()
                s = _norm_id(s)
                if s:
                    current_pt_targets.append(s)
                    if current_app_id:
                        poly_to_app.setdefault(s, current_app_id)

            elif tag_ln == "TexCoordList":
                # Finalize any current TexCoordList-based mapping (UVs) if we have them.
                # NOTE: CityGML2 places <app:textureCoordinates ring="#..."> directly in TexCoordList.
                # We parse those at textureCoordinates end events below.
                # Nothing to do here besides marking the scope end.
                current_tcl_rids = []
                current_tcl_uvs = []

            elif tag_ln == "textureCoordinates":
                # Parse UV list and ring reference (CityGML2)
                tc_text = (elem.text or "").strip()
                rid_txt = (elem.get("ring") or "").strip()
                rid = _norm_id(rid_txt)
                if tc_text:
                    try:
                        vals = [float(x) for x in " ".join(tc_text.split()).split()]
                        uvs = [(vals[k], vals[k + 1]) for k in range(0, len(vals) - 1, 2)]
                    except Exception:
                        uvs = []
                    if rid and uvs:
                        # ensure we have an image resolved
                        if current_pt_image_uri:
                            img_resolved = _resolve_image_path(gml_path, current_pt_image_uri)
                            if rid not in ptex_by_ring:
                                entry = {"image": img_resolved, "uv": uvs}
                                if current_pt_targets:
                                    entry["poly"] = current_pt_targets[0]
                                ptex_by_ring[rid] = entry
                            # also populate per-poly mappings (needed by materials)
                            for pid in current_pt_targets:
                                if pid:
                                    poly_to_image.setdefault(pid, img_resolved)
                                    if pid not in ptex_by_poly:
                                        ptex_by_poly[pid] = {"image": img_resolved, "uv": uvs, "ring": rid}

            elif tag_ln == "ParameterizedTexture":
                # Finalize a ParameterizedTexture: even if we didn't see TexCoordList,
                # at least map targets -> image so preloading can work.
                seen_parameterized_texture += 1
                if current_pt_image_uri and current_pt_targets:
                    img_resolved = _resolve_image_path(gml_path, current_pt_image_uri)
                    for pid in current_pt_targets:
                        poly_to_image.setdefault(pid, img_resolved)
                # reset PT state
                current_pt_image_uri = ""
                current_pt_targets = []

            elif tag_ln == "GeoreferencedTexture":
                _parse_georeferenced_texture(elem, gml_path, poly_to_image)

            elif tag_ln == "Appearance":
                if current_app_id:
                    theme_by_app[current_app_id] = current_theme or ""
                current_app_id = None
                current_theme = None

            elem.clear()

    except Exception:
        # Robustness: any appearance parsing issues should not abort import.
        return {}, {}, {}, {}, {}, {}

    return x3d_by_poly, ptex_by_ring, poly_to_image, ptex_by_poly, poly_to_app, theme_by_app


def _parse_x3d_material(elem, id_lookup: Dict, gml_id_attr: str, out: Dict, poly_to_app: Dict, app_id: Optional[str]):

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
        href = extract_href(t)
        uri_attr = extract_uri_attr(t)
        s = (href or uri_attr or (t.text or "")).strip()
        if not s:
            continue
        s = _norm_id(s)
        if s:
            targets.append(s)
    
    # Expand targets to polygons
    for tid in targets:
        out[tid] = rgb
        if app_id and tid:
            poly_to_app.setdefault(tid, app_id)
        
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
                if app_id and pgid:
                    poly_to_app.setdefault(pgid, app_id)


def _parse_parameterized_texture(elem, gml_path: str, ptex_by_ring: Dict, poly_to_image: Dict, ptex_by_poly: Dict, poly_to_app: Dict, app_id: Optional[str]):

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
            if ln(t.tag) not in ("target", "uri"):
                continue
            href = extract_href(t)
            # CityGML 2.0 uses <app:target uri="#..."> (attribute), CityGML 3.0 often uses xlink:href or text.
            uri_attr = extract_uri_attr(t)
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

    # If base_targets are empty, we can't associate a texture to a polygon.
    
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
                if app_id:
                    poly_to_app.setdefault(pid, app_id)
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
