# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io/writer/streaming_appearance.py
"""
Streaming Appearance Helper
============================

Collects and manages appearance data (materials and textures) 
during streaming export for consolidated writing at end of file.
"""

from __future__ import annotations
from typing import Dict, List, Tuple, Set, Optional
from xml.etree.ElementTree import Element, SubElement
from collections import defaultdict
import os
import re
from uuid import uuid4

from .namespaces import Q, GML_ID
from .helpers import fmt
from .materials import extract_base_color_rgba, extract_x3d_params_from_material, first_image_path_from_material
from .texio import _ensure_export_texture, _rel_image_uri
from .appearance import _mime_type_for_image

try:
    from .parallel_texture import ParallelTextureExporter
    PARALLEL_TEXTURE_AVAILABLE = True
except ImportError:
    PARALLEL_TEXTURE_AVAILABLE = False


class StreamingAppearanceCollector:
    """
    Collects appearance data during streaming export.
    
    Accumulates X3DMaterials and ParameterizedTextures from exported objects,
    then generates consolidated Appearance elements at end of export.
    """
    
    def __init__(self, output_dir: str, double_sided: bool = False,
                 compress_textures: bool = False, texture_quality: int = 85,
                 texture_max_size: Optional[int] = None, texture_workers: int = 4,
                 export_debug: bool = False):
        """
        Args:
            output_dir: Directory where GML file is written (for texture export)
            double_sided: Whether to create front+back materials
            compress_textures: Enable parallel texture compression (PNG → JPEG)
            texture_quality: JPEG compression quality (1-100)
            texture_max_size: Maximum texture dimension in pixels (None = no resize)
            texture_workers: Number of parallel worker threads for texture processing
        """
        self.output_dir = output_dir
        self.double_sided = double_sided
        self.export_debug = bool(export_debug)
        
        # Texture compression settings
        self.compress_textures = compress_textures and PARALLEL_TEXTURE_AVAILABLE
        self.texture_quality = texture_quality
        self.texture_max_size = texture_max_size
        self.texture_workers = texture_workers
        
        # Initialize parallel texture exporter if enabled
        if self.compress_textures:
            self.parallel_exporter = ParallelTextureExporter(
                max_workers=texture_workers,
                used_names=set()
            )
            print(f"[Texture Compression] Enabled with {texture_workers} workers (quality={texture_quality})")
        else:
            self.parallel_exporter = None
        
        # X3DMaterial collection: rgba -> {poly_ids, params}
        self.materials_by_rgba: Dict[Tuple[float, float, float, float], Dict] = {}
        
        # ParameterizedTexture collection: image_path -> [entries]
        self.textures_by_image: Dict[str, List[Dict]] = defaultdict(list)
        self.processed_interior_ring_uvs: Dict[str, Dict[str, object]] = {}
        
        # Track used texture filenames for uniqueness
        self.used_texture_names: Set[str] = set()
        
        # Track exported texture files: src_path -> (dest_path, rel_uri)
        self.texture_paths: Dict[str, Tuple[str, str]] = {}

    def _dbg_heuristic(self, action: str, poly_id: str, ring_id: str, uvs_in, uvs_out, exterior_sign: int, expected_sign, uv_start, forced_interior: bool = False, previous_action: str | None = None) -> None:
        if not self.export_debug:
            return
        def _head(seq, n=4):
            try:
                return list(seq[:n]) if seq else []
            except Exception:
                return []
        def _uv_sign_local(uvs):
            try:
                core = list(uvs or [])
                if len(core) >= 2 and core[0] == core[-1]:
                    core = core[:-1]
                if len(core) < 3:
                    return 0
                area = 0.0
                for i, (u1, v1) in enumerate(core):
                    u2, v2 = core[(i + 1) % len(core)]
                    area += u1 * v2 - u2 * v1
                if area > 1e-12:
                    return 1
                if area < -1e-12:
                    return -1
                return 0
            except Exception:
                return None
        print(
            "[CityGML3 Export][DEBUG][PTX-HEURISTIC]",
            f"action={action}",
            f"poly_id={poly_id}",
            f"ring_id={ring_id}",
            f"exterior_sign={exterior_sign}",
            f"ring_sign_in={_uv_sign_local(uvs_in)}",
            f"ring_sign_out={_uv_sign_local(uvs_out)}",
            f"expected_sign={expected_sign}",
            f"uv_start={uv_start}",
            f"forced_interior={forced_interior}",
            f"previous_action={previous_action}",
            f"in_head={_head(uvs_in)}",
            f"out_head={_head(uvs_out)}",
        )

    def _dbg_duplicate(self, poly_id: str, ring_id: str, chosen: str, prev_entry: dict, new_entry: dict) -> None:
        if not self.export_debug:
            return
        def _head(seq, n=4):
            try:
                return list(seq[:n]) if seq else []
            except Exception:
                return []
        print(
            "[CityGML3 Export][DEBUG][PTX-DUP]",
            f"poly_id={poly_id}",
            f"ring_id={ring_id}",
            f"chosen={chosen}",
            f"prev_score={self._ring_entry_score_debug(prev_entry)}",
            f"new_score={self._ring_entry_score_debug(new_entry)}",
            f"prev_head={_head(prev_entry.get('uvs'))}",
            f"new_head={_head(new_entry.get('uvs'))}",
        )

    @staticmethod
    def _ring_entry_score_debug(entry: dict):
        uvs = list(entry.get('uvs') or [])
        return (
            1 if entry.get('is_interior', None) is True else 0,
            1 if entry.get('is_interior', None) is not None else 0,
            1 if entry.get('uv_start', None) is not None else 0,
            1 if entry.get('expected_sign', None) in (-1, 1) else 0,
            len(uvs),
        )
    
    def add_material(self, obj, mat, poly_ids: List[str]):
        """
        Add X3DMaterial data for an object.
        
        Args:
            obj: Blender object
            mat: Blender material
            poly_ids: List of polygon gml:ids using this material
        """
        if not mat or not poly_ids:
            return
        
        rgba = extract_base_color_rgba(mat)
        
        if rgba not in self.materials_by_rgba:
            self.materials_by_rgba[rgba] = {
                'poly_ids': [],
                'params': extract_x3d_params_from_material(mat)
            }
        
        self.materials_by_rgba[rgba]['poly_ids'].extend(poly_ids)
    
    def add_texture(self, obj, mat, mat_index: int, poly_id: str, ring_id: str, uvs: List[Tuple[float, float]]):
        """
        Add ParameterizedTexture data for a polygon.
        
        Args:
            obj: Blender object
            mat: Blender material
            mat_index: Material slot index
            poly_id: Polygon gml:id
            ring_id: Ring gml:id (for exterior/interior rings)
            uvs: List of UV coordinates [(u, v), ...]
        """
        # Edge case: Empty UV data or missing material
        if not mat:
            return
        
        if not uvs or len(uvs) == 0:
            print(f"  Warning: Skipping texture for {poly_id} - no UV coordinates")
            return

        def _norm_id(v) -> str:
            s = str(v or "").strip()
            return s[1:] if s.startswith("#") else s

        uv_start = None
        expected_sign = None
        try:
            rid_this = _norm_id(ring_id)
            d = None
            if obj is not None:
                try:
                    d = obj.get("cgml3_uv_start_by_ring", None)
                except Exception:
                    d = None
                if d is None and getattr(obj, "data", None) is not None:
                    try:
                        d = obj.data.get("cgml3_uv_start_by_ring", None)
                    except Exception:
                        d = None
            if d is not None and rid_this:
                uv_start = d.get(rid_this, None)
        except Exception:
            uv_start = None

        if uv_start is None:
            try:
                uv_start = mat.get("cgml3_uv_start", None) if mat else None
                rid_mat = _norm_id(mat.get("gml_ring_id", "")) if mat else ""
                if not (uv_start is not None and rid_mat and rid_this and rid_mat == rid_this):
                    uv_start = None
            except Exception:
                uv_start = None

        if uv_start is not None:
            try:
                uv_start = (float(uv_start[0]), float(uv_start[1]))
            except Exception:
                uv_start = None

        try:
            rid_this = _norm_id(ring_id)
            d = None
            if obj is not None:
                try:
                    d = obj.get("cgml3_uv_sign_by_ring", None)
                except Exception:
                    d = None
                if d is None and getattr(obj, "data", None) is not None:
                    try:
                        d = obj.data.get("cgml3_uv_sign_by_ring", None)
                    except Exception:
                        d = None
            if d is not None and rid_this:
                expected_sign = d.get(rid_this, None)
        except Exception:
            expected_sign = None

        try:
            expected_sign = int(expected_sign) if expected_sign is not None else None
        except Exception:
            expected_sign = None
        if expected_sign not in (-1, 1):
            expected_sign = None
        
        # Get image from material
        image_path = first_image_path_from_material(obj, mat_index)
        if not image_path:
            return
        
        # Add texture job to parallel exporter or export immediately
        if image_path not in self.texture_paths:
            if self.parallel_exporter:
                # Queue texture for parallel processing
                # Determine dest filename
                base_name = os.path.basename(image_path)
                name_part, ext_part = os.path.splitext(base_name)
                
                # Use JPEG extension if compressing
                if self.compress_textures and ext_part.lower() in ['.png', '.bmp', '.tif', '.tiff']:
                    ext_part = '.jpg'
                
                dest_name = base_name if not self.compress_textures else f"{name_part}{ext_part}"
                
                # Add to parallel exporter queue
                self.parallel_exporter.add_texture(
                    src_path=image_path,
                    dest_name=dest_name,
                    compress=self.compress_textures,
                    quality=self.texture_quality,
                    max_size=self.texture_max_size
                )
                
                # Store relative URI for later use
                rel_uri = f"appearance/{dest_name}"
                self.texture_paths[image_path] = (os.path.join(self.output_dir, rel_uri), rel_uri)
            else:
                # Traditional sequential export
                dest_path, rel_uri = _ensure_export_texture(
                    image_path, 
                    self.output_dir, 
                    self.used_texture_names
                )
                self.texture_paths[image_path] = (dest_path, rel_uri)
        else:
            dest_path, rel_uri = self.texture_paths[image_path]
        
        # Add texture entry
        self.textures_by_image[rel_uri].append({
            'poly_id': poly_id,
            'ring_id': ring_id,
            'uvs': uvs,
            'ori': 0,  # Orientation flag
            'uv_start': uv_start,
            'expected_sign': expected_sign,
        })
    
    def generate_appearance_elements(self) -> List[Element]:
        """
        Generate Appearance elements from collected data.
        
        Executes parallel texture export if enabled before generating XML.
        
        Returns:
            List of app:Appearance XML elements
        """
        # Execute parallel texture export if enabled
        if self.parallel_exporter:
            import time
            start_time = time.time()
            texture_count = len(self.parallel_exporter.jobs)
            
            print(f"[Texture Compression] Exporting {texture_count} textures with {self.texture_workers} workers...")
            
            # Execute all texture jobs in parallel
            results = self.parallel_exporter.export_all(self.output_dir)
            
            elapsed = time.time() - start_time
            success_count = sum(1 for r in results if r.success)
            error_count = len(results) - success_count
            
            print(f"[Texture Compression] Completed in {elapsed:.2f}s: {success_count} successful, {error_count} errors")
            
            # Report errors
            for result in results:
                if not result.success:
                    print(f"  ERROR: {result.src_path} - {result.error}")
        
        appearances = []
        
        # Generate X3DMaterial Appearance
        if self.materials_by_rgba:
            app_mat = Element(Q('app', 'Appearance'))
            self._add_x3d_materials(app_mat)
            if len(app_mat) > 0:  # Has surface data
                appearances.append(app_mat)
        
        # Generate ParameterizedTexture Appearance
        if self.textures_by_image:
            app_tex = Element(Q('app', 'Appearance'))
            self._add_parameterized_textures(app_tex)
            if len(app_tex) > 0:  # Has surface data
                appearances.append(app_tex)
        
        return appearances
    
    def _add_x3d_materials(self, app_elem: Element):
        """Add X3DMaterial surfaceData elements."""
        for rgba, bundle in self.materials_by_rgba.items():
            poly_ids = bundle['poly_ids']
            params = bundle.get('params', {})
            
            sides = [(True, False)] if self.double_sided else [(True,)]
            
            for is_front in sides[0] if self.double_sided else [sides[0][0]]:
                # CityGML 2.0 AppearanceType expects surfaceDataMember*, not surfaceData.
                sdm = SubElement(app_elem, Q('app', 'surfaceDataMember'))
                x3d = SubElement(sdm, Q('app', 'X3DMaterial'))
                
                # isFront must be first child
                SubElement(x3d, Q('app', 'isFront')).text = 'true' if is_front else 'false'
                
                # ambientIntensity
                if 'ambientIntensity' in params:
                    SubElement(x3d, Q('app', 'ambientIntensity')).text = fmt(params['ambientIntensity'])
                
                # diffuseColor
                r, g, b, a = rgba
                diff = SubElement(x3d, Q('app', 'diffuseColor'))
                diff.text = f"{fmt(r)} {fmt(g)} {fmt(b)}"
                
                # emissiveColor
                if 'emissiveColor' in params:
                    ec = params['emissiveColor']
                    if ec and len(ec) >= 3:
                        SubElement(x3d, Q('app', 'emissiveColor')).text = \
                            f"{fmt(ec[0])} {fmt(ec[1])} {fmt(ec[2])}"
                
                # specularColor
                if 'specularColor' in params:
                    sc = params['specularColor']
                    if sc and len(sc) >= 3:
                        SubElement(x3d, Q('app', 'specularColor')).text = \
                            f"{fmt(sc[0])} {fmt(sc[1])} {fmt(sc[2])}"
                
                # shininess
                if 'shininess' in params:
                    SubElement(x3d, Q('app', 'shininess')).text = fmt(params['shininess'])
                
                # transparency
                if 'transparency' in params:
                    SubElement(x3d, Q('app', 'transparency')).text = fmt(params['transparency'])
                
                # isSmooth
                if 'isSmooth' in params:
                    SubElement(x3d, Q('app', 'isSmooth')).text = \
                        'true' if params['isSmooth'] else 'false'
                
                # Target URIs
                for pid in poly_ids:
                    tgt = SubElement(x3d, Q('app', 'target'))
                    tgt.text = f"#{pid}"
    
    def _add_parameterized_textures(self, app_elem: Element):
        """Add ParameterizedTexture surfaceData elements."""
        def _ring_idx(rid: str) -> int:
            m = re.search(r'_(\d+)_$', str(rid))
            return int(m.group(1)) if m else 0

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
            rid = str(entry.get('ring_id', '') or '')
            is_interior = _infer_is_interior(poly_id, rid, entry.get('is_interior', None))
            nums = _ring_suffix_numbers(poly_id, rid)
            if not nums:
                nums = (_ring_idx(rid),)
            return (1 if is_interior else 0, nums, rid)

        def _ring_entry_score(entry: dict):
            uvs = list(entry.get('uvs') or [])
            return (
                1 if entry.get('is_interior', None) is True else 0,
                1 if entry.get('is_interior', None) is not None else 0,
                1 if entry.get('uv_start', None) is not None else 0,
                1 if entry.get('expected_sign', None) in (-1, 1) else 0,
                len(uvs),
            )
        
        def _is_closed(uvs):
            return len(uvs) >= 2 and uvs[0] == uvs[-1]
        
        def _open(seq):
            return seq[:-1] if _is_closed(seq) else list(seq)
        
        def _close(seq):
            return seq + [seq[0]] if seq and not _is_closed(seq) else seq

        def _match_interior_winding(uvs, is_interior: bool, exterior_sign: int, expected_sign: int | None = None):
            if not uvs or not is_interior:
                return list(uvs or [])
            ring_sign = _uv_sign(uvs)
            if expected_sign in (-1, 1) and ring_sign != 0 and ring_sign != expected_sign:
                was_closed = _is_closed(uvs)
                core = list(reversed(_open(uvs)))
                return _close(core) if was_closed else core
            if ring_sign != 0 and exterior_sign != 0 and ring_sign == exterior_sign:
                was_closed = _is_closed(uvs)
                core = list(reversed(_open(uvs)))
                return _close(core) if was_closed else core
            return list(uvs or [])

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
        
        for img_uri, entries in self.textures_by_image.items():
            # Group by poly_id
            by_poly: Dict[str, Dict[str, dict]] = {}
            for e in entries:
                poly_entries = by_poly.setdefault(e['poly_id'], {})
                ring_entry = {
                    'ring_id': e['ring_id'],
                    'uvs': list(e['uvs']),
                    'ori': int(e.get('ori', 0)),
                    'uv_start': e.get('uv_start', None),
                    'expected_sign': e.get('expected_sign', None),
                    'is_interior': e.get('is_interior', None),
                }
                rid = str(ring_entry['ring_id'] or '')
                prev = poly_entries.get(rid)
                if prev is None:
                    poly_entries[rid] = ring_entry
                elif _ring_entry_score(ring_entry) > _ring_entry_score(prev):
                    self._dbg_duplicate(e['poly_id'], rid, 'new', prev, ring_entry)
                    poly_entries[rid] = ring_entry
                else:
                    self._dbg_duplicate(e['poly_id'], rid, 'prev', prev, ring_entry)
            
            sides = (True, False) if self.double_sided else (None,)
            
            for is_front in sides:
                # CityGML 2.0 AppearanceType expects surfaceDataMember*, not surfaceData.
                sdm = SubElement(app_elem, Q('app', 'surfaceDataMember'))
                ptx = SubElement(sdm, Q('app', 'ParameterizedTexture'))
                ptx.set(GML_ID, f"ID_{uuid4().hex}")
                
                # isFront (if used) must come before imageURI
                if is_front is not None:
                    SubElement(ptx, Q('app', 'isFront')).text = 'true' if is_front else 'false'
                
                SubElement(ptx, Q('app', 'imageURI')).text = img_uri
                # mimeType ist optional, wird aber von vielen Tools erwartet (siehe import.gml)
                SubElement(ptx, Q('app', 'mimeType')).text = _mime_type_for_image(img_uri)
                # textureType/wrapMode/borderColor lassen wir standardmäßig weg, um näher an
                # import.gml zu bleiben und doppelte/abweichende Interpretation zu vermeiden.
                
                # TextureParameterization for each polygon
                for poly_id, ring_map in by_poly.items():
                    ring_entries = sorted(ring_map.values(), key=lambda r: _ring_sort_key(poly_id, r))

                    def _uv_sign(uvs):
                        core = _open(list(uvs))
                        if len(core) < 3:
                            return 0
                        area = 0.0
                        for i, (u1, v1) in enumerate(core):
                            u2, v2 = core[(i + 1) % len(core)]
                            area += u1 * v2 - u2 * v1
                        if area > 1e-12:
                            return 1
                        if area < -1e-12:
                            return -1
                        return 0

                    exterior_entry = next(
                        (
                            r for r in ring_entries
                            if not _infer_is_interior(
                                poly_id,
                                str(r.get('ring_id', '') or ''),
                                r.get('is_interior', None),
                            )
                        ),
                        None,
                    )
                    exterior_sign = _uv_sign(exterior_entry['uvs']) if exterior_entry else 0
                    tp_outer = SubElement(ptx, Q('app', 'textureParameterization'))
                    assoc = SubElement(tp_outer, Q('app', 'TextureAssociation'))
                    SubElement(assoc, Q('app', 'target')).text = f"#{poly_id}"
                    tp_inner = SubElement(assoc, Q('app', 'textureParameterization'))
                    tcl = SubElement(tp_inner, Q('app', 'TexCoordList'))
                    processed = []
                    for r in ring_entries:
                        hint_interior = r.get('is_interior', None) if isinstance(r, dict) else None
                        is_interior = _infer_is_interior(poly_id, str(r['ring_id']), hint_interior)
                        expected_sign = r.get('expected_sign', None) if isinstance(r, dict) else None
                        uv_start = r.get('uv_start') if isinstance(r, dict) else None
                        forced_interior = bool(hint_interior is False and is_interior)
                        if not is_interior:
                            self._dbg_heuristic('skip-non-interior', poly_id, str(r['ring_id']), list(r['uvs']), list(r['uvs']), exterior_sign, expected_sign, uv_start, forced_interior=False)
                        cache_key = str(r['ring_id'] or '')
                        cached_uvs = None
                        cached_action = None
                        if is_interior and cache_key:
                            cached_entry = self.processed_interior_ring_uvs.get(cache_key)
                            if isinstance(cached_entry, dict):
                                cached_uvs = list(cached_entry.get('uvs') or [])
                                cached_action = str(cached_entry.get('action') or '') or None
                            elif cached_entry is not None:
                                cached_uvs = list(cached_entry)
                        if cached_uvs is not None:
                            uvs = list(cached_uvs)
                            self._dbg_heuristic('cache-reuse', poly_id, str(r['ring_id']), list(r['uvs']), uvs, exterior_sign, expected_sign, uv_start, forced_interior=forced_interior, previous_action=cached_action)
                        else:
                            uvs = _match_interior_winding(
                                list(r['uvs']),
                                is_interior,
                                exterior_sign,
                                expected_sign,
                            )
                            winding_corrected = (uvs != list(r['uvs']))
                            if uv_start is not None:
                                uvs = _rotate_to_start(uvs, uv_start)
                            action_taken = 'reordered' if winding_corrected else 'kept'
                            if is_interior and cache_key:
                                self.processed_interior_ring_uvs[cache_key] = {'uvs': list(uvs), 'action': action_taken}
                            if is_interior:
                                self._dbg_heuristic(
                                    action_taken,
                                    poly_id,
                                    str(r['ring_id']),
                                    list(r['uvs']),
                                    uvs,
                                    exterior_sign,
                                    expected_sign,
                                    uv_start,
                                    forced_interior=forced_interior,
                                    previous_action=None,
                                )

                        processed.append({
                            'ring_id': r['ring_id'],
                            'uvs': uvs,
                        })

                    for p in processed:
                        tc = SubElement(tcl, Q('app', 'textureCoordinates'))
                        tc.text = ' '.join(f"{fmt(u)} {fmt(v)}" for (u, v) in p['uvs'])

                    for p in processed:
                        SubElement(tcl, Q('app', 'ring')).text = f"#{p['ring_id']}"
    
    def get_stats(self) -> Dict[str, int]:
        """Get statistics about collected appearance data."""
        return {
            'materials': len(self.materials_by_rgba),
            'textures': len(self.textures_by_image),
            'texture_files_exported': len(self.texture_paths),
            'material_targets': sum(len(b['poly_ids']) for b in self.materials_by_rgba.values()),
            'texture_entries': sum(len(entries) for entries in self.textures_by_image.values())
        }
