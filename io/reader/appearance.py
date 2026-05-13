# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
from .namespaces import NS
from .xml_utils import _norm_id
from pathlib import Path

def parse_x3d_materials(root):
    """
    Liest app:X3DMaterial aus dem Appearance-Modul und liefert ein Mapping
    von Polygon-gml:id -> diffuseColor (R,G,B in [0,1]).

    CityGML 3 erlaubt als target sowohl einzelne Polygone als auch
    aggregierte Geometrien (CompositeSurface/MultiSurface/Solid).
    In vielen FME-Exports zeigt der target auf die gml:id einer
    CompositeSurface (z.B. 'FME_GeometryIntance8'); die tatsächlichen
    Polygone haben andere IDs. Damit das Material trotzdem ankommt,
    wird hier jeder target auf alle darunterliegenden Polygone
    „entfaltet“.
    """
    def ln(tag):
        return tag.split("}")[-1] if isinstance(tag, str) and "}" in tag else tag

    out = {}

    # einmalige gml:id -> Element Lookup-Tabelle
    gml_ns = NS.get("gml")
    gml_id_attr = f"{{{gml_ns}}}id" if gml_ns else None
    id_lookup = {}
    if gml_id_attr:
        for el in root.iter():
            gid = el.get(gml_id_attr)
            if gid:
                id_lookup[gid] = el

    for x3d in root.iter():
        if not isinstance(x3d.tag, str) or ln(x3d.tag) != "X3DMaterial":
            continue

        # 1) diffuseColor lesen
        diff = None
        for c in x3d.iter():
            if isinstance(c.tag, str) and ln(c.tag) == "diffuseColor" and c.text and c.text.strip():
                diff = c.text.strip()
                break
        if not diff:
            continue

        try:
            parts = [float(x) for x in diff.split()]
            rgb = tuple(max(0.0, min(1.0, v)) for v in parts[:3]) if len(parts) >= 3 else (0.8, 0.8, 0.8)
        except Exception:
            rgb = (0.8, 0.8, 0.8)

        # 2) alle Targets einsammeln (#id oder xlink:href)
        targets = []
        for t in x3d.iter():
            if not isinstance(t.tag, str):
                continue
            l = ln(t.tag)
            if l not in ("target", "uri"):
                continue
            href = t.get("{http://www.w3.org/1999/xlink}href")
            s = (href or (t.text or "")).strip()
            if not s:
                continue
            s = _norm_id(s)   # entfernt führendes '#'
            if s:
                targets.append(s)

        # 3) Targets auf Polygone „entfalten“
        for tid in targets:
            # Fallback: wie bisher -> direkt unter dieser ID eintragen
            out[tid] = rgb

            el = id_lookup.get(tid)
            if el is None or not gml_id_attr:
                continue

            # alle untergeordneten Polygone dieses Targets mit einfärben
            for poly in el.iter():
                if not isinstance(poly.tag, str):
                    continue
                if ln(poly.tag) not in ("Polygon", "Triangle", "Rectangle"):
                    continue
                pgid = poly.get(gml_id_attr)
                if not pgid:
                    continue
                out[pgid] = rgb

    return out

def _resolve_image_path(gml_path: str, uri: str) -> str:
    if not uri:
        return ""
    p = Path(uri)
    if p.is_absolute() and p.exists():
        return str(p)

    base = Path(gml_path).parent
    candidates = [
        base / uri,                                   # relativ zur GML
        base / "Appearance" / Path(uri).name,        # nur Dateiname im Appearance-Ordner
        base / "Appearance" / uri,                   # relativer Unterpfad im Appearance-Ordner
    ]
    for c in candidates:
        try:
            if c.exists():
                return str(c.resolve())
        except Exception:
            continue
    # Fallback
    return str((base / uri).resolve())


def parse_parameterized_textures(root, gml_path: str):
    """
    Liest ParameterizedTexture in CityGML 3 und 2 und baut drei Mappings:
      - ptex_by_ring: ring_id -> {"image": Pfad, "uv": [(u,v), ...], "poly": optional poly_id}
      - poly_to_image: poly_id -> Bildpfad
      - ptex_by_poly: poly_id -> {"image": Pfad, "uv": [(u,v), ...], "ring": optional ring_id}
    """
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
        """Sammelt alle Referenzen aus <target> oder <uri> (Text oder xlink:href)."""
        targets = []
        for t in scope.iter():
            if not isinstance(t.tag, str):
                continue
            l = ln(t.tag)
            if l not in ("target", "uri"):
                continue
            href = t.get("{http://www.w3.org/1999/xlink}href")
            s = (href or (t.text or "")).strip()
            s = _norm_id(s)
            if s:
                targets.append(s)
        return targets

    def parse_texcoordlist(tcl, targets, img_resolved, ptex_by_ring, ptex_by_poly, poly_to_image):
        """
        Verarbeitet ein <TexCoordList> und ordnet die Koordinaten Rings (ring_id)
        und den betroffenen Polygonen (targets) zu.
        Unterstützt Schreibweisen:
          A) textureCoordinates, ring, textureCoordinates, ring
          B) textureCoordinates, textureCoordinates, ring, ring
          C) ring als Kind von <textureCoordinates>
        """
        kids = list(tcl)
        claimed_rings = set()

        i = 0
        while i < len(kids):
            node = kids[i]
            i += 1
            if not isinstance(node.tag, str) or ln(node.tag) != "textureCoordinates":
                continue

            tc = node
            tc_text = (tc.text or "").strip()
            if not tc_text:
                continue

            # 1) ring-Attribut
            rid_txt = (tc.get("ring") or "").strip()

            # 2) CityGML 3: <ring> als Kind von <textureCoordinates>
            if not rid_txt:
                for rnode in tc.iter():
                    if not isinstance(rnode.tag, str):
                        continue
                    if ln(rnode.tag) == "ring":
                        cand = (rnode.text or "").strip()
                        if cand:
                            rid_txt = cand
                            break

            # 3) CityGML 3: <ring> als separates Geschwister-Element
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

            # UV-Paare extrahieren
            try:
                vals = [float(x) for x in " ".join(tc_text.split()).split()]
            except ValueError:
                continue
            if len(vals) < 2:
                continue
            uvs = [(vals[k], vals[k + 1]) for k in range(0, len(vals) - 1, 2)]
            if not uvs:
                continue

            # Ring-basiertes Mapping
            if rid:
                if rid not in ptex_by_ring:
                    entry = {"image": img_resolved, "uv": uvs}
                    if targets:
                        entry["poly"] = targets[0]
                    ptex_by_ring[rid] = entry

            # Polygon-basiertes Mapping
            for pid in targets or []:
                if not pid:
                    continue
                poly_to_image.setdefault(pid, img_resolved)
                if pid not in ptex_by_poly:
                    ptex_by_poly[pid] = {
                        "image": img_resolved,
                        "uv": uvs,
                        "ring": rid or "",
                        "ring_uv_start": (uvs[0] if uvs else None),
                    }

    ptex_by_ring = {}
    poly_to_image = {}
    ptex_by_poly = {}

    img_cache = {}

    def resolve_image(img):
        if not img:
            return ""
        if img in img_cache:
            return img_cache[img]
        r = _resolve_image_path(gml_path, img)
        img_cache[img] = r
        return r

    # Durch alle ParameterizedTexture-Elemente laufen
    for ptx in root.iter():
        if not isinstance(ptx.tag, str) or ln(ptx.tag) != "ParameterizedTexture":
            continue

        img = first_text(ptx, "imageURI")
        if not img:
            continue
        img_resolved = resolve_image(img)

        # Basis-Ziele (CityGML 2: <target> direkt unter ParameterizedTexture)
        base_targets = gather_targets(ptx)

        had_assoc = False

        # 1) CityGML 3: TextureAssociation-Blöcke
        for assoc in ptx.iter():
            if not isinstance(assoc.tag, str) or ln(assoc.tag) != "TextureAssociation":
                continue
            had_assoc = True

            assoc_targets = gather_targets(assoc) or base_targets
            for pid in assoc_targets:
                if pid:
                    poly_to_image.setdefault(pid, img_resolved)

            for tcl in assoc.iter():
                if not isinstance(tcl.tag, str) or ln(tcl.tag) != "TexCoordList":
                    continue
                parse_texcoordlist(tcl, assoc_targets, img_resolved,
                                   ptex_by_ring, ptex_by_poly, poly_to_image)

        # 2) CityGML 2: TexCoordList direkt unter ParameterizedTexture ohne TextureAssociation
        if not had_assoc:
            for pid in base_targets:
                if pid:
                    poly_to_image.setdefault(pid, img_resolved)
            for tcl in ptx.iter():
                if not isinstance(tcl.tag, str) or ln(tcl.tag) != "TexCoordList":
                    continue
                parse_texcoordlist(tcl, base_targets, img_resolved,
                                   ptex_by_ring, ptex_by_poly, poly_to_image)

    return ptex_by_ring, poly_to_image, ptex_by_poly

def parse_georeferenced_textures(root, gml_path: str):
    def ln(tag): return tag.split("}")[-1] if isinstance(tag, str) and "}" in tag else tag
    def _first_text(el, lname: str) -> str:
        for c in el.iter():
            if isinstance(c.tag, str) and ln(c.tag) == lname:
                t = (c.text or "").strip()
                if t: return t
        return ""
    def _all_named(el, *lnames):
        want = set(lnames)
        for c in el.iter():
            if isinstance(c.tag, str) and ln(c.tag) in want:
                yield c
    out = {}
    for gtx in root.iter():
        if not isinstance(gtx.tag, str) or ln(gtx.tag) != "GeoreferencedTexture":
            continue
        n = gtx.find("./app:texImage/app:imageURI", NS)
        img = (n.text.strip() if n is not None and (n.text or "").strip() else _first_text(gtx, "imageURI"))
        if not img: continue
        img_resolved = _resolve_image_path(gml_path, img)
        ref_txt = (_first_text(gtx, "pos") or _first_text(gtx, "posList") or _first_text(gtx, "referencePoint"))
        ref_vals = [float(x) for x in " ".join(ref_txt.split()).split()] if ref_txt else []
        if len(ref_vals) >= 2:
            refpt = (ref_vals[0], ref_vals[1], ref_vals[2] if len(ref_vals) >= 3 else 0.0)
        else:
            continue
        M_txt = _first_text(gtx, "orientation")
        m_vals = [float(x) for x in " ".join(M_txt.split()).split()] if M_txt else []
        M = (m_vals[0], m_vals[1], m_vals[2], m_vals[3]) if len(m_vals) >= 4 else ((m_vals[0],0.0,0.0,m_vals[1]) if len(m_vals) >= 2 else (1.0,0.0,0.0,1.0))
        targets = []
        for node in _all_named(gtx, "target","uri"):
            href = node.get("{http://www.w3.org/1999/xlink}href")
            s = (href or (node.text or "")).strip()
            s = _norm_id(s);  targets.append(s) if s else None
        for assoc in gtx.iter():
            if not isinstance(assoc.tag, str) or ln(assoc.tag) != "TextureAssociation":
                continue
            for node in _all_named(assoc, "target","uri"):
                href = node.get("{http://www.w3.org/1999/xlink}href")
                s = (href or (node.text or "")).strip()
                s = _norm_id(s); targets.append(s) if s else None
        if not targets: continue
        for pid in targets:
            if pid and pid not in out:
                out[pid] = {"image": img_resolved, "refpt": refpt, "M": M}
    return out
