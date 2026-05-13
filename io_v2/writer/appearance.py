# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
from __future__ import annotations
from typing import Dict, List, Tuple, Mapping
from uuid import uuid4
import re
from xml.etree.ElementTree import SubElement

from .namespaces import Q, GML_ID
from .helpers import fmt
from .texio import _rel_image_uri


def _mime_type_for_image(image_uri: str) -> str:
    """Return a mimeType string matching export_v2.gml conventions.

    Note: export_v2.gml uses 'image/jpg' (not 'image/jpeg') for *.jpg.
    """
    u = (image_uri or "").lower()
    if u.endswith(".jpg"):
        return "image/jpg"
    if u.endswith(".jpeg"):
        return "image/jpeg"
    if u.endswith(".png"):
        return "image/png"
    if u.endswith(".tif") or u.endswith(".tiff"):
        return "image/tiff"
    return "application/octet-stream"


def add_x3d_materials(app_appear_el, x3d_by_key: Dict[Any, dict]) -> None:
    """
    Write app:X3DMaterial elements.

    x3d_by_key mapping expected:
      key: (rgba_tuple, params_tuple)
      bundle: {"rgba": (r,g,b,a), "params": dict, "poly_ids": [poly_gid, ...]}
    """
    if not x3d_by_key:
        return

    for _k, bundle in x3d_by_key.items():
        rgba = bundle.get("rgba", (0.8, 0.8, 0.8, 1.0))
        params = bundle.get("params", {}) or {}
        poly_ids = bundle.get("poly_ids", []) or []

        # CityGML 2.0 AppearanceType expects surfaceDataMember*, not surfaceData.
        sdm = SubElement(app_appear_el, Q("app", "surfaceDataMember"))
        mat = SubElement(sdm, Q("app", "X3DMaterial"))

        # Reihenfolge laut XSD:
        # ambientIntensity?, diffuseColor?, emissiveColor?, specularColor?, shininess?, transparency?, isSmooth?, target*

        SubElement(mat, Q("app", "diffuseColor")).text = f"{fmt(rgba[0])} {fmt(rgba[1])} {fmt(rgba[2])}"

        if "emissiveColor" in params and params["emissiveColor"]:
            ec = params["emissiveColor"]
            SubElement(mat, Q("app", "emissiveColor")).text = f"{fmt(ec[0])} {fmt(ec[1])} {fmt(ec[2])}"

        if "specularColor" in params and params["specularColor"]:
            sc = params["specularColor"]
            SubElement(mat, Q("app", "specularColor")).text = f"{fmt(sc[0])} {fmt(sc[1])} {fmt(sc[2])}"

        if "shininess" in params and params["shininess"] is not None:
            SubElement(mat, Q("app", "shininess")).text = fmt(float(params["shininess"]))

        # transparency = 1 - alpha
        a = float(rgba[3]) if rgba[3] is not None else 1.0
        transparency = max(0.0, min(1.0, 1.0 - a))
        SubElement(mat, Q("app", "transparency")).text = fmt(transparency)

        for pid in poly_ids:
            SubElement(mat, Q("app", "target")).text = f"#{pid}"

def add_parameterized_textures(
    app_appear_el,
    ptx_by_image,
    *,
    double_sided: bool = False,
    processed_interior_ring_uvs=None,
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
            1 if entry.get("preserve_uv_order", False) else 0,
            len(uvs),
        )
    def _dbg_duplicate(poly_id: str, rid: str, chosen: str, prev_entry: dict, new_entry: dict):
        if not export_debug:
            return
        try:
            print(
                "[CityGML2][PTX-DUP]",
                f"poly_id={poly_id}",
                f"ring_id={rid}",
                f"chosen={chosen}",
                f"prev_score={_ring_entry_score(prev_entry)}",
                f"new_score={_ring_entry_score(new_entry)}",
            )
        except Exception:
            pass
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

    # NOTE: We intentionally do not use 3D-normal heuristics for interior rings here.
    # Instead we normalize the UV winding against the polygon's exterior ring and
    # only keep the preserved `uv_start` when it still matches that corrected order.

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
                "preserve_uv_order": bool(e.get("preserve_uv_order", False)),
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
            # CityGML 2.0 AppearanceType expects surfaceDataMember*, not surfaceData.
            sdm = SubElement(app_appear_el, Q("app","surfaceDataMember"))
            ptx = SubElement(sdm, Q("app","ParameterizedTexture"))
            ptx.set(GML_ID, f"ID_{uuid4().hex}")

            # isFront (falls genutzt) MUSS vor imageURI/textureType/... kommen (XSD!)
            if is_front is not None:
                SubElement(ptx, Q("app","isFront")).text = "true" if is_front else "false"

            SubElement(ptx, Q("app","imageURI")).text = _rel_image_uri(img)
            SubElement(ptx, Q("app","mimeType")).text = _mime_type_for_image(img)
            if False:
                SubElement(ptx, Q("app","textureType")).text = "specific"
                SubElement(ptx, Q("app","wrapMode")).text = "border"
                SubElement(ptx, Q("app","borderColor")).text = "0.0 0.0 0.0 1.0"

            for poly_id, ring_map in by_poly.items():
                ring_entries = sorted(ring_map.values(), key=lambda r: _ring_sort_key(poly_id, r))
                target = SubElement(ptx, Q("app","target"), {"uri": f"#{poly_id}"})
                tcl = SubElement(target, Q("app","TexCoordList"))
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
                for r in ring_entries:
                    # CityGML 2 / appearance:TexCoordList content model does not allow <app:ring>.
                    # The ring reference is encoded via the `ring` attribute on textureCoordinates.
                    tc = SubElement(tcl, Q("app","textureCoordinates"), {"ring": f"#{r['ring_id']}"})
                    ring_id_dbg = str(r.get("ring_id", ""))
                    uvs_in = list(r.get("uvs") or [])
                    uv_start = r.get("uv_start", None)
                    expected_sign = r.get("expected_sign", None)
                    is_interior = _infer_is_interior(poly_id, ring_id_dbg, r.get("is_interior", None))
                    preserve_uv_order = bool(r.get("preserve_uv_order", False))

                    cache_key = ring_id_dbg
                    cached_uvs = None
                    if is_interior and not preserve_uv_order and processed_interior_ring_uvs is not None and cache_key:
                        cached_entry = processed_interior_ring_uvs.get(cache_key)
                        if isinstance(cached_entry, dict):
                            cached_uvs = list(cached_entry.get("uvs") or [])
                        elif cached_entry is not None:
                            cached_uvs = list(cached_entry)

                    if cached_uvs is not None:
                        uvs = list(cached_uvs)
                    elif preserve_uv_order:
                        uvs = list(uvs_in)
                    else:
                        # Keep exterior UVs exactly in mesh loop order. Only interior
                        # rings may need winding normalization to match GML hole order.
                        uvs, winding_corrected = _match_interior_winding(
                            uvs_in,
                            is_interior,
                            exterior_sign,
                            expected_sign,
                        )
                    if uv_start is not None and not preserve_uv_order:
                        uvs = _rotate_to_start(uvs, uv_start)
                    else:
                        # No preserved start: do NOT normalize/rotate, because the exporter
                        # already aligned UVs to the mesh loop order, and changing the start
                        # point here can break visual mapping for some surfaces.
                        pass

                    if is_interior and not preserve_uv_order and processed_interior_ring_uvs is not None and cache_key:
                        processed_interior_ring_uvs[cache_key] = {"uvs": list(uvs)}

                    # Opt-in debug for one ring: set env var `CGML3_DEBUG_TEXCOORD_RING` to a substring.
                    try:
                        watch = os.environ.get("CGML3_DEBUG_TEXCOORD_RING", "").strip()
                        if watch and watch in ring_id_dbg:
                            try:
                                keys = list(r.keys()) if isinstance(r, dict) else None
                            except Exception:
                                keys = None
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
                            print("[CityGML2][TC-DEBUG] ring_id=", ring_id_dbg,
                                  " idx=", _ring_idx(ring_id_dbg),
                                  " uv_start=", uv_start,
                                  " reversed=", rev,
                                  " entry_type=", type(r).__name__,
                                  " entry_keys=", keys,
                                  "\n  in_head=", _h(uvs_in),
                                  "\n  out_head=", _h(uvs))
                    except Exception:
                        pass

                    tc.text = " ".join(f"{fmt(u)} {fmt(v)}" for (u, v) in uvs)
                    # (no <app:ring> for CityGML2)


def add_georeferenced_textures(app_appear_el, gtx_by_image):
    for img, entries in gtx_by_image.items():
        # CityGML 2.0 AppearanceType expects surfaceDataMember*, not surfaceData.
        sdm = SubElement(app_appear_el, Q("app","surfaceDataMember"))
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
