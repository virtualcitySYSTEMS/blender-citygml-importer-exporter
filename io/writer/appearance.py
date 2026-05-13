# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
from __future__ import annotations
from typing import Dict, List, Tuple, Mapping
from uuid import uuid4
import re
import mimetypes
from xml.etree.ElementTree import SubElement

from .namespaces import Q, GML_ID
from .helpers import fmt
from .texio import _rel_image_uri


def _mime_type_for_image(p: str) -> str:
    mt, _ = mimetypes.guess_type(str(p) if p is not None else "")
    if mt:
        return mt
    lower = str(p or "").lower()
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith((".tif", ".tiff")):
        return "image/tiff"
    return "image/jpeg"


def add_x3d_materials(
    app_appear_el,
    x3d_by_rgba: Mapping[
        Tuple[float, float, float, float],
        Dict[str, object]  # {"poly_ids": List[str], "params": Dict[str, object]}
    ],
    *,
    double_sided: bool = False,
):
    for rgba, bundle in x3d_by_rgba.items():
        poly_ids: List[str] = bundle["poly_ids"]
        params: Dict[str, object] = dict(bundle.get("params", {}))

        def _vec3_text(v):
            try:
                return f"{v[0]} {v[1]} {v[2]}"
            except Exception:
                return None

        # je nach double_sided Vorder- UND Rückseite erzeugen
        for is_front in (True, False) if double_sided else (True,):
            sdm = SubElement(app_appear_el, Q("app", "surfaceData"))
            x3d = SubElement(sdm, Q("app", "X3DMaterial"))

            # isFront muss als erstes Kindelement von AbstractSurfaceData kommen
            SubElement(x3d, Q("app", "isFront")).text = "true" if is_front else "false"

            if "ambientIntensity" in params:
                SubElement(x3d, Q("app", "ambientIntensity")).text = fmt(
                    params["ambientIntensity"]
                )

            r, g, b, _a = rgba
            diff = SubElement(x3d, Q("app", "diffuseColor"))
            diff.text = f"{fmt(r)} {fmt(g)} {fmt(b)}"

            if "emissiveColor" in params:
                t = _vec3_text(params["emissiveColor"])
                if t:
                    SubElement(x3d, Q("app", "emissiveColor")).text = t

            if "specularColor" in params:
                t = _vec3_text(params["specularColor"])
                if t:
                    SubElement(x3d, Q("app", "specularColor")).text = t

            if "shininess" in params:
                SubElement(x3d, Q("app", "shininess")).text = fmt(params["shininess"])

            if "transparency" in params:
                SubElement(x3d, Q("app", "transparency")).text = fmt(
                    params["transparency"]
                )

            if "isSmooth" in params:
                SubElement(x3d, Q("app", "isSmooth")).text = (
                    "true" if params["isSmooth"] else "false"
                )

            for pid in poly_ids:
                tgt = SubElement(x3d, Q("app", "target"))
                tgt.text = f"#{pid}"


def add_parameterized_textures(
    app_appear_el,
    ptx_by_image,
    *,
    double_sided: bool = False,
    processed_interior_ring_uvs: dict | None = None,
    export_debug: bool = False,
):
    import os
    def _ring_idx(rid: str) -> int:
        m = re.search(r'_(\d+)_$', str(rid)); return int(m.group(1)) if m else 0
    def _ring_suffix_numbers(poly_id: str, rid: str):
        poly_id = str(poly_id or "")
        rid = str(rid or "")
        suffix = rid[len(poly_id):] if poly_id and rid.startswith(poly_id) else rid
        return tuple(int(n) for n in re.findall(r'_(\d+)(?=_|$)', suffix))
    def _infer_is_interior(poly_id: str, rid: str, hint=None) -> bool:
        poly_id = str(poly_id or "")
        rid = str(rid or "")
        if poly_id and rid in (f"{poly_id}_0", f"{poly_id}_0_"):
            return False
        nums = _ring_suffix_numbers(poly_id, rid)
        derived = False
        if len(nums) >= 2:
            derived = True
        elif len(nums) == 1:
            derived = nums[0] != 0
        else:
            derived = _ring_idx(rid) > 0
        if hint is True:
            return True
        if hint is False and derived:
            return True
        if hint is not None:
            return bool(hint)
        return derived
    def _ring_sort_key(poly_id: str, entry: dict):
        rid = str(entry.get("ring_id", "") or "")
        is_interior = _infer_is_interior(poly_id, rid, entry.get("is_interior", None))
        nums = _ring_suffix_numbers(poly_id, rid)
        if not nums:
            nums = (_ring_idx(rid),)
        return (1 if is_interior else 0, nums, rid)
    def _ring_entry_score(entry: dict):
        uvs = list(entry.get("uvs") or [])
        return (
            1 if entry.get("is_interior", None) is True else 0,
            1 if entry.get("is_interior", None) is not None else 0,
            1 if entry.get("uv_start", None) is not None else 0,
            1 if entry.get("expected_sign", None) in (-1, 1) else 0,
            len(uvs),
        )
    def _dbg_heuristic(action: str, poly_id: str, rid: str, uvs_in, uvs_out, exterior_sign: int, expected_sign, uv_start, forced_interior: bool = False, previous_action: str | None = None):
        if not export_debug:
            return
        def _head(seq, n=4):
            try:
                return list(seq[:n]) if seq else []
            except Exception:
                return []
        try:
            ring_sign_in = _uv_sign(uvs_in)
        except Exception:
            ring_sign_in = None
        try:
            ring_sign_out = _uv_sign(uvs_out)
        except Exception:
            ring_sign_out = None
        print(
            "[CityGML3 Export][DEBUG][PTX-HEURISTIC]",
            f"action={action}",
            f"poly_id={poly_id}",
            f"ring_id={rid}",
            f"exterior_sign={exterior_sign}",
            f"ring_sign_in={ring_sign_in}",
            f"ring_sign_out={ring_sign_out}",
            f"expected_sign={expected_sign}",
            f"uv_start={uv_start}",
            f"forced_interior={forced_interior}",
            f"previous_action={previous_action}",
            f"in_head={_head(uvs_in)}",
            f"out_head={_head(uvs_out)}",
        )
    def _dbg_duplicate(poly_id: str, rid: str, chosen: str, prev_entry: dict, new_entry: dict):
        if not export_debug:
            return
        def _head(seq, n=4):
            try:
                return list(seq[:n]) if seq else []
            except Exception:
                return []
        print(
            "[CityGML3 Export][DEBUG][PTX-DUP]",
            f"poly_id={poly_id}",
            f"ring_id={rid}",
            f"chosen={chosen}",
            f"prev_score={_ring_entry_score(prev_entry)}",
            f"new_score={_ring_entry_score(new_entry)}",
            f"prev_head={_head(prev_entry.get('uvs'))}",
            f"new_head={_head(new_entry.get('uvs'))}",
        )
    def _is_closed(uvs): return len(uvs) >= 2 and uvs[0] == uvs[-1]
    def _open(seq):  return seq[:-1] if _is_closed(seq) else list(seq)
    def _close(seq): return seq + [seq[0]] if seq and not _is_closed(seq) else seq
    def _area2_uv(open_uvs):
        a = 0.0; n = len(open_uvs)
        for i in range(n):
            x1,y1 = open_uvs[i]; x2,y2 = open_uvs[(i+1) % n]
            a += x1*y2 - x2*y1
        return 1 if a > 0 else -1
    def _rotate_to_start(uvs, start_uv, tol=1e-6):
        if not uvs or start_uv is None:
            return uvs
        try:
            su, sv = float(start_uv[0]), float(start_uv[1])
        except Exception:
            return uvs
        open_uvs = _open(list(uvs))
        if not open_uvs:
            return uvs
        idx = None
        for i, (u, v) in enumerate(open_uvs):
            if abs(u - su) <= tol and abs(v - sv) <= tol:
                idx = i
                break
        if idx is None:
            best_i = 0
            best_d = None
            for i, (u, v) in enumerate(open_uvs):
                d = (u - su) * (u - su) + (v - sv) * (v - sv)
                if best_d is None or d < best_d:
                    best_d = d
                    best_i = i
            idx = best_i
        open_uvs = open_uvs[idx:] + open_uvs[:idx]
        return _close(open_uvs)

    def _orient_interior_uvs(uvs, ori: int, ring_id: str):
        # Legacy kept for compatibility; winding is normalized below against the exterior ring.
        return uvs

    def _uv_sign(uvs):
        try:
            open_uvs = _open(list(uvs))
        except Exception:
            return 0
        if len(open_uvs) < 3:
            return 0
        area = 0.0
        for i, (u1, v1) in enumerate(open_uvs):
            u2, v2 = open_uvs[(i + 1) % len(open_uvs)]
            area += u1 * v2 - u2 * v1
        if area > 1e-12:
            return 1
        if area < -1e-12:
            return -1
        return 0

    def _reverse_ring_uvs(uvs):
        try:
            was_closed = _is_closed(uvs)
            core = _open(list(uvs))
            core = list(reversed(core))
            return _close(core) if was_closed else core
        except Exception:
            return uvs

    def _match_interior_winding(uvs, is_interior: bool, exterior_sign: int, expected_sign: int | None = None):
        if not uvs or not is_interior:
            return list(uvs or []), False
        ring_sign = _uv_sign(uvs)
        if expected_sign in (-1, 1) and ring_sign != 0 and ring_sign != expected_sign:
            return _reverse_ring_uvs(list(uvs)), True
        if ring_sign != 0 and exterior_sign != 0 and ring_sign == exterior_sign:
            return _reverse_ring_uvs(list(uvs)), True
        return list(uvs or []), False

    for img, entries in ptx_by_image.items():
        # nach Poly-ID gruppieren
        by_poly: Dict[str, Dict[str, dict]] = {}
        for e in entries:
            poly_entries = by_poly.setdefault(e["poly_id"], {})
            ring_entry = {
                "ring_id": e["ring_id"],
                "uvs": list(e["uvs"]),
                "uv_start": e.get("uv_start", None),
                "expected_sign": e.get("expected_sign", None),
                "is_interior": e.get("is_interior", None),
            }
            rid = str(ring_entry["ring_id"] or "")
            prev = poly_entries.get(rid)
            if prev is None:
                poly_entries[rid] = ring_entry
            elif _ring_entry_score(ring_entry) > _ring_entry_score(prev):
                _dbg_duplicate(e["poly_id"], rid, "new", prev, ring_entry)
                poly_entries[rid] = ring_entry
            else:
                _dbg_duplicate(e["poly_id"], rid, "prev", prev, ring_entry)

        sides = (True, False) if double_sided else (None,)

        for is_front in sides:
            sdm = SubElement(app_appear_el, Q("app","surfaceData"))
            ptx = SubElement(sdm, Q("app","ParameterizedTexture"))
            ptx.set(GML_ID, f"ID_{uuid4().hex}")

            # isFront (falls genutzt) MUSS vor imageURI/textureType/... kommen (XSD!)
            if is_front is not None:
                SubElement(ptx, Q("app","isFront")).text = "true" if is_front else "false"

            SubElement(ptx, Q("app","imageURI")).text = _rel_image_uri(img)
            # mimeType ist optional, aber viele Tools erwarten ihn (und import.gml enthält ihn).
            SubElement(ptx, Q("app","mimeType")).text = _mime_type_for_image(img)
            # textureType/wrapMode/borderColor sind für viele Exporte nicht nötig und führen
            # in manchen Toolchains zu abweichender Interpretation. Wir schreiben sie daher
            # nur, wenn explizit gewünscht (Default: auslassen wie in import.gml).
            if False:
                SubElement(ptx, Q("app","textureType")).text = "specific"
                SubElement(ptx, Q("app","wrapMode")).text = "border"
                SubElement(ptx, Q("app","borderColor")).text = "0.0 0.0 0.0 1.0"

            for poly_id, ring_map in by_poly.items():
                ring_entries = sorted(ring_map.values(), key=lambda r: _ring_sort_key(poly_id, r))
                exterior_entry = next(
                    (
                        r for r in ring_entries
                        if not _infer_is_interior(
                            poly_id,
                            str(r.get("ring_id", "") or ""),
                            r.get("is_interior", None),
                        )
                    ),
                    None,
                )
                exterior_sign = _uv_sign(exterior_entry["uvs"]) if exterior_entry else 0
                tp_outer = SubElement(ptx, Q("app","textureParameterization"))
                assoc    = SubElement(tp_outer, Q("app","TextureAssociation"))
                SubElement(assoc, Q("app","target")).text = f"#{poly_id}"
                tp_inner = SubElement(assoc, Q("app","textureParameterization"))
                tcl      = SubElement(tp_inner, Q("app","TexCoordList"))

                processed = []
                for r in ring_entries:
                    rid = r["ring_id"]
                    uvs_in = list(r["uvs"])
                    expected_sign = r.get("expected_sign", None) if isinstance(r, dict) else None
                    hint_interior = r.get("is_interior", None) if isinstance(r, dict) else None
                    is_interior = _infer_is_interior(poly_id, rid, hint_interior)
                    uv_start = r.get("uv_start", None) if isinstance(r, dict) else None
                    winding_corrected = False
                    forced_interior = bool(hint_interior is False and is_interior)
                    if not is_interior:
                        _dbg_heuristic("skip-non-interior", poly_id, rid, uvs_in, uvs_in, exterior_sign, expected_sign, uv_start, forced_interior=False)
                    cache_key = str(rid or "")
                    cached_uvs = None
                    cached_action = None
                    if is_interior and processed_interior_ring_uvs is not None and cache_key:
                        cached_entry = processed_interior_ring_uvs.get(cache_key)
                        if isinstance(cached_entry, dict):
                            cached_uvs = list(cached_entry.get("uvs") or [])
                            cached_action = str(cached_entry.get("action") or "") or None
                        elif cached_entry is not None:
                            cached_uvs = list(cached_entry)
                    if cached_uvs is not None:
                        uvs = list(cached_uvs)
                        _dbg_heuristic("cache-reuse", poly_id, rid, uvs_in, uvs, exterior_sign, expected_sign, uv_start, forced_interior=forced_interior, previous_action=cached_action)
                    else:
                        uvs, winding_corrected = _match_interior_winding(uvs_in, is_interior, exterior_sign, expected_sign)
                        if uv_start is not None:
                            uvs = _rotate_to_start(uvs, uv_start)
                        action_taken = "reordered" if winding_corrected else "kept"
                        if is_interior and processed_interior_ring_uvs is not None and cache_key:
                            processed_interior_ring_uvs[cache_key] = {"uvs": list(uvs), "action": action_taken}
                        if is_interior:
                            _dbg_heuristic(
                                action_taken,
                                poly_id,
                                rid,
                                uvs_in,
                                uvs,
                                exterior_sign,
                                expected_sign,
                                uv_start,
                                forced_interior=forced_interior,
                                previous_action=None,
                            )
                    try:
                        watch = os.environ.get("CGML3_DEBUG_TEXCOORD_RING", "").strip()
                        if watch and watch in str(rid):
                            def _h(seq, n=4):
                                return list(seq[:n]) if seq else []
                            rev = None
                            try:
                                o_in = _open(list(uvs_in))
                                o_out = _open(list(uvs))
                                if len(o_in) == len(o_out) and len(o_in) >= 3:
                                    rev = (o_out == list(reversed(o_in)))
                            except Exception:
                                rev = None
                            print("[CityGML3][TC-DEBUG] ring_id=", rid, " interior=", is_interior,
                                  " uv_start=", uv_start, " reversed=", rev,
                                  "\n  in_head=", _h(uvs_in),
                                  "\n  out_head=", _h(uvs))
                    except Exception:
                        pass
                    processed.append({
                        "ring_id": rid,
                        "uvs": uvs,
                    })

                for p in processed:
                    tc = SubElement(tcl, Q("app", "textureCoordinates"))
                    tc.text = " ".join(f"{fmt(u)} {fmt(v)}" for (u, v) in p["uvs"])

                for p in processed:
                    SubElement(tcl, Q("app", "ring")).text = f"#{p['ring_id']}"


def add_georeferenced_textures(app_appear_el, gtx_by_image):
    for img, entries in gtx_by_image.items():
        sdm = SubElement(app_appear_el, Q("app","surfaceData"))
        gtx = SubElement(sdm, Q("app","GeoreferencedTexture"))
        ti  = SubElement(gtx, Q("app","texImage"))
        uri = SubElement(ti, Q("app","imageURI"))
        uri.text = _rel_image_uri(img)
        SubElement(ti, Q("app","mimeType")).text = _mime_type_for_image(img)

        seen_polys = set()
        for e in entries:
            pid = e["poly_id"]
            if pid in seen_polys:
                continue
            seen_polys.add(pid)
            tgt = SubElement(gtx, Q("app","target"))
            tgt.text = f"#{pid}"

        for e in entries:
            x0, y0 = e["refpt"]
            a, b, c, d = e["M"]
            rp = SubElement(gtx, Q("app","referencePoint"))
            gp = SubElement(rp, Q("gml","Point"))
            pos = SubElement(gp, Q("gml","pos"))
            pos.text = f"{fmt(x0)} {fmt(y0)}"
            orient = SubElement(gtx, Q("app","orientation"))
            orient.text = f"{fmt(a)} {fmt(b)} {fmt(c)} {fmt(d)}"
