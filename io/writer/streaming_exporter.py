# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io/writer/streaming_exporter.py
"""
Streaming-based CityGML Export Integration

Provides drop-in replacement for traditional DOM-based export with
streaming XML writing for memory efficiency.

Usage:
    # Instead of:
    from io.writer.exporter import export_blender_to_citygml3
    
    # Use:
    from io.writer.streaming_exporter import export_blender_to_citygml3_streaming
    
    export_blender_to_citygml3_streaming(
        filepath="output.gml",
        context=bpy.context,
        srs_name="EPSG:25832",
        feature_type_prop="ModelType",
        use_streaming=True  # Auto-decides based on object count
    )

Features:
- Automatic streaming threshold (>1000 objects)
- Manual override with use_streaming parameter
- Progress reporting every 100 objects
- Memory monitoring (optional)
- Compatible with existing exporter parameters
"""

from __future__ import annotations
from typing import Optional, Callable
import bpy
import os
from uuid import uuid4
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement

from .streaming_writer import StreamingXMLWriter, should_use_streaming
from .streaming_geometry import StreamingGeometryBuffer, create_polygon_element
from .streaming_appearance import StreamingAppearanceCollector
from .namespaces import NS, Q, GML_ID
from .helpers import fmt, make_gml_id, _envelope_elem, resolve_feature_tag, _read_world_crs, _extract_epsg
from .geometry import write_polygon_with_ring_ids, write_compositesurface_with_polygons
from .appearance import add_x3d_materials, add_parameterized_textures, add_georeferenced_textures
from .materials import extract_base_color_rgba, first_image_path_from_material
from .crs_transform import GeoTransformer
from ...shared.export_helpers import object_is_viewport_visible
import re
import json

NON_EXPORT_GENERIC_ATTR_KEYS = {
    "image_path",
}

NON_EXPORT_GENERIC_ATTR_PREFIXES = (
    "apt_",
)


def _should_skip_generic_attr_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    if key in NON_EXPORT_GENERIC_ATTR_KEYS:
        return True
    return any(key.startswith(prefix) for prefix in NON_EXPORT_GENERIC_ATTR_PREFIXES)

def _write_generic_attributes_citygml3(parent_el: Element, obj_or_mat) -> None:
    """
    Write CityGML 3.0 genericAttribute blocks.

    Prefers lossless roundtrip dicts from importer:
      - obj['cgml3_generic_attributes']
      - mat['cgml3_surface_generic_attributes']

    Falls back to iterating direct idprops (excluding known internal keys).
    """
    def _load_json_chunks(owner, base_key: str) -> dict | None:
        try:
            direct = owner.get(base_key, None)
        except Exception:
            direct = None

        txt = None
        if isinstance(direct, str) and direct.strip():
            txt = direct
        else:
            chunks: list[str] = []
            try:
                keys = list(getattr(owner, "keys", lambda: [])())
            except Exception:
                keys = []
            for k in sorted([k for k in keys if isinstance(k, str) and k.startswith(base_key + "_")]):
                try:
                    part = owner.get(k, "")
                except Exception:
                    part = ""
                if isinstance(part, str):
                    chunks.append(part)
            if chunks:
                txt = "".join(chunks)

        if not txt:
            return None
        try:
            data = json.loads(txt)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    raw = None
    try:
        raw = obj_or_mat.get("cgml3_generic_attributes", None)
        if raw is None:
            raw = obj_or_mat.get("cgml3_surface_generic_attributes", None)
    except Exception:
        raw = None

    if not (isinstance(raw, dict) and raw):
        raw = (
            _load_json_chunks(obj_or_mat, "cgml3_generic_attributes_json")
            or _load_json_chunks(obj_or_mat, "cgml3_surface_generic_attributes_json")
            or raw
        )

    if isinstance(raw, dict) and raw:
        items = list(raw.items())
        allow_underscore = True
    else:
        items = list(getattr(obj_or_mat, "items", lambda: [])())
        allow_underscore = False

    INTERNAL_ATTR_KEYS = {"cgml3_feature", "gml_id", "cgml3_implicit_template_id"}
    MATERIAL_INTERNAL_ATTR_KEYS = {
        "EPSG",
        "gml_ring_id",
        "gml_polygon_id",
        "gml_multisurface_id",
        "gml_compositesurface_id",
        "con_surface_id",
        "SurfaceTyp",
        "surface_type",
        "app_target_or_uri",
        "relationToConstruction",
        "con:relationToConstruction",
        "is_multisurface_member",
        "is_compositesurface_member",
        "Interior",
        "ExteriorPolyId",
        "ExteriorRingId",
        "face_index",
        "gml_surface_id",
        "multisurface_id",
        "source_material",
        "surface_id",
        "cgml3_uv_start_by_ring",
    }

    for k, v in items:
        if not isinstance(k, str):
            continue
        if k in INTERNAL_ATTR_KEYS or k in MATERIAL_INTERNAL_ATTR_KEYS:
            continue
        if _should_skip_generic_attr_key(k):
            continue
        if k.startswith("_") and not allow_underscore:
            continue
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue

        ga = SubElement(parent_el, Q("core", "genericAttribute"))
        name_el = None
        val_el = None

        if isinstance(v, bool):
            a = SubElement(ga, Q("gen", "IntAttribute"))
            SubElement(a, Q("gen", "name")).text = k
            SubElement(a, Q("gen", "value")).text = "1" if v else "0"
            continue

        if isinstance(v, int):
            a = SubElement(ga, Q("gen", "IntAttribute"))
            SubElement(a, Q("gen", "name")).text = k
            SubElement(a, Q("gen", "value")).text = str(v)
            continue

        if isinstance(v, float):
            a = SubElement(ga, Q("gen", "DoubleAttribute"))
            SubElement(a, Q("gen", "name")).text = k
            SubElement(a, Q("gen", "value")).text = fmt(v)
            continue

        s = str(v).strip()
        if re.match(r"^\\d{4}-\\d{2}-\\d{2}$", s):
            a = SubElement(ga, Q("gen", "DateAttribute"))
            SubElement(a, Q("gen", "name")).text = k
            SubElement(a, Q("gen", "value")).text = s
            continue

        if re.match(r"^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}(:\\d{2}(?:\\.\\d+)?)?(Z|[+-]\\d{2}:\\d{2})?$", s):
            a = SubElement(ga, Q("gen", "DateTimeAttribute"))
            SubElement(a, Q("gen", "name")).text = k
            SubElement(a, Q("gen", "value")).text = s
            continue

        a = SubElement(ga, Q("gen", "StringAttribute"))
        SubElement(a, Q("gen", "name")).text = k
        SubElement(a, Q("gen", "value")).text = s


def export_blender_to_citygml3_streaming(
    filepath: str,
    context,
    srs_name: str,
    feature_type_prop: str,
    *,
    export_x3d: bool = True,
    export_ptx: bool = True,
    export_gtx: bool = True,
    use_inner_outer_script: bool = False,
    autofill_new_surfaces: bool = False,
    unclassified_surface_type: str = "WallSurface",
    use_streaming: Optional[bool] = None,
    enable_memory_tracking: bool = False,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    compress_textures: bool = False,
    texture_quality: int = 85,
    texture_max_size: Optional[int] = None,
    texture_workers: int = 4,
    export_types: Optional[list[str]] = None,
):
    """
    Export Blender scene to CityGML 3.0 using streaming XML writer.
    
    Args:
        filepath: Output GML file path
        context: Blender context
        srs_name: Target SRS (e.g., "EPSG:25832")
        feature_type_prop: Property name for feature type detection
        export_x3d: Export X3D materials
        export_ptx: Export parameterized textures
        export_gtx: Export georeferenced textures
        use_inner_outer_script: Use inner/outer ring script
        use_streaming: Force streaming on/off (None = auto-decide)
        enable_memory_tracking: Track memory usage (adds overhead)
        progress_callback: Optional callback(current, total) for progress
        compress_textures: Enable parallel texture compression (PNG → JPEG)
        texture_quality: JPEG compression quality (1-100)
        texture_max_size: Maximum texture dimension in pixels (None = no resize)
        texture_workers: Number of parallel worker threads for texture processing
    
    Returns:
        StreamingStats if streaming was used, else None
    """
    export_types_set = set(export_types or [])
    default_unclassified_surface_type = str(unclassified_surface_type or "WallSurface").strip() or "WallSurface"
    export_debug = False
    try:
        export_debug = bool(getattr(getattr(context, "scene", None), "cgml3", None) and context.scene.cgml3.export_debug)
    except Exception:
        export_debug = False

    def _scene_has_building_subdivision_features() -> bool:
        for obj in context.scene.objects:
            if not object_is_viewport_visible(obj, context):
                continue
            try:
                feat = str(obj.get("cgml3_feature") or "").strip()
            except Exception:
                feat = ""
            if feat in {"Storey", "BuildingRoom"}:
                return True
        return False

    def _should_export_obj(obj_) -> bool:
        if not export_types_set:
            return True
        val = None
        try:
            if feature_type_prop:
                v = obj_.get(feature_type_prop, None)
                if v is not None and str(v).strip():
                    val = str(v).strip()
        except Exception:
            pass

        if not val:
            try:
                v = obj_.get("cgml3_feature", None)
                if v is not None and str(v).strip():
                    val = str(v).strip()
            except Exception:
                pass

        if not val:
            val = "Building"

        try:
            ns_key, local_name = resolve_feature_tag(val)
        except Exception:
            return False
        return f"{ns_key}:{local_name}" in export_types_set

    _INLINE_SUBFEATURE_COLLECTION_NAMES = {
        "BuildingInstallation", "BuildingInstallations",
        "IntBuildingInstallation", "IntBuildingInstallations",
        "BuildingFurniture", "BuildingFurnitures",
        "BridgeInstallation", "BridgeInstallations",
        "IntBridgeInstallation", "IntBridgeInstallations",
        "BridgeConstructionElement", "BridgeConstructionElements",
        "BridgeFurniture", "BridgeFurnitures",
        "TunnelInstallation", "TunnelInstallations",
        "IntTunnelInstallation", "IntTunnelInstallations",
        "TunnelFurniture", "TunnelFurnitures",
    }

    def _object_in_auto_disabled_inline_collection(obj_):
        try:
            collections = list(obj_.users_collection)
        except Exception:
            collections = []
        for collection in collections:
            try:
                name = str(getattr(collection, "name", "") or "").strip()
            except Exception:
                name = ""
            if "." in name:
                base, suffix = name.rsplit(".", 1)
                if suffix.isdigit():
                    name = base
            if name not in _INLINE_SUBFEATURE_COLLECTION_NAMES:
                continue
            try:
                if bool(collection.get("cgml3_auto_disabled_subfeatures", False)):
                    return True
            except Exception:
                pass
        return False

    # Collect exportable objects
    export_objects = [
        obj for obj in context.scene.objects
        if obj.type == 'MESH'
        and object_is_viewport_visible(obj, context)
        and not obj.hide_render
        and not _object_in_auto_disabled_inline_collection(obj)
        and _should_export_obj(obj)
    ]
    
    object_count = len(export_objects)
    
    # Decide whether to use streaming
    if autofill_new_surfaces:
        use_streaming = False
    elif _scene_has_building_subdivision_features():
        use_streaming = False
    elif use_streaming is None:
        use_streaming = should_use_streaming(object_count)
    
    if not use_streaming:
        # Fall back to traditional export
        print(f"Using traditional DOM export ({object_count} objects)")
        from .exporter import export_blender_to_citygml3
        export_blender_to_citygml3(
            filepath, context, srs_name, feature_type_prop,
            export_x3d=export_x3d,
            export_ptx=export_ptx,
            export_gtx=export_gtx,
            use_inner_outer_script=use_inner_outer_script,
            autofill_new_surfaces=autofill_new_surfaces,
            unclassified_surface_type=default_unclassified_surface_type,
            export_types=list(export_types_set) if export_types_set else None,
        )
        return None
    
    print(f"Using streaming export ({object_count} objects)")
    
    # Prepare CRS transformation
    _epsg_num = _extract_epsg(srs_name) or _extract_epsg(_read_world_crs()) or "25832"
    src_crs = f"EPSG:{_epsg_num}"
    tgt_crs = srs_name or f"EPSG:{_epsg_num}"
    
    def _export_wants_3d() -> bool:
        try:
            w = bpy.data.worlds.get("World")
            return bool(w and "Z-Origin" in w)
        except Exception:
            return True
    
    trf = GeoTransformer(src_crs, tgt_crs, export_3d=_export_wants_3d())
    
    # Initialize appearance collector
    output_dir = os.path.dirname(os.path.abspath(filepath))
    # Doppelseitige Appearance nur, wenn explizit gesetzt (analog zum DOM-Exporter)
    double_sided = bool(
        context.scene.get("cgml3_double_sided", False)
        if hasattr(context, "scene") and hasattr(context.scene, "get")
        else False
    )
    appearance_collector = StreamingAppearanceCollector(
        output_dir, 
        double_sided=double_sided,
        compress_textures=compress_textures,
        texture_quality=texture_quality,
        texture_max_size=texture_max_size,
        texture_workers=texture_workers,
        export_debug=export_debug,
    )
    
    # Tracking
    used_feat_ids = set()
    
    # Helper functions (simplified from exporter.py)
    def _feature_id(obj_):
        try:
            raw = obj_.get("gml_id", None)
        except Exception:
            raw = None
        
        if raw is not None:
            gid = str(raw).strip()
            if gid:
                # XSD-Konformität sicherstellen: xs:ID muss mit Buchstabe/_ beginnen
                # UUIDs aus 3DCityDB können mit Zahl beginnen → make_gml_id validiert
                return make_gml_id(gid, used_feat_ids)
        
        return make_gml_id(obj_.name, used_feat_ids)
    
    def _feature_tag(obj_):
        val = None
        try:
            if feature_type_prop:
                v = obj_.get(feature_type_prop, None)
                if v is not None and str(v).strip():
                    val = str(v).strip()
        except Exception:
            pass
        
        if not val:
            try:
                v = obj_.get("cgml3_feature", None)
                if v is not None and str(v).strip():
                    val = str(v).strip()
            except Exception:
                pass
        
        if not val:
            val = "Building"
        
        # BridgeConstructiveElement ist in CityGML 3 ein Unterelement von brid:Bridge,
        # wird aber im Blender-Import oft als Feature-Typ/SurfaceTyp am Material/Objekt geführt.
        # Für Streaming-Export müssen wir es daher als gültigen Bridge-Untertyp auflösen.
        ns_key, local_name = resolve_feature_tag(val)
        return ns_key, local_name
    
    def _resolve_export_offset(ctx, obj_):
        try:
            w = bpy.data.worlds.get("World")
            if w and all(k in w for k in ("X-Origin", "Y-Origin", "Z-Origin")):
                return float(w["X-Origin"]), float(w["Y-Origin"]), float(w["Z-Origin"])
        except Exception:
            pass
        # Fallback: Scene properties
        try:
            sc = ctx.scene
            if sc and "crs x" in sc and "crs y" in sc:
                z = float(sc.get("crs z", 0.0))
                return float(sc["crs x"]), float(sc["crs y"]), z
        except Exception:
            pass
        so = getattr(ctx.scene, "cgml3_offset", (0.0, 0.0, 0.0))
        return float(so[0]), float(so[1]), float(so[2])
    
    # Create export directory
    out_dir = os.path.dirname(os.path.abspath(filepath))
    os.makedirs(os.path.join(out_dir, "appearance"), exist_ok=True)
    
    # Streaming export
    with StreamingXMLWriter(
        filepath,
        srs_name,
        version="3.0",
        enable_memory_tracking=enable_memory_tracking
    ) as writer:

        def _matrix_world_to_citygml_tm(mw, *, eps: float = 1e-12):
            """
            Convert Blender 4x4 matrix to CityGML transformationMatrix (row-major).
            Translation is expected to be handled via referencePoint.
            """
            try:
                rows = []
                for r in range(4):
                    for c in range(4):
                        v = float(mw[r][c])
                        if abs(v) < eps:
                            v = 0.0
                        rows.append(v)
                return rows
            except Exception:
                return [
                    1.0, 0.0, 0.0, 0.0,
                    0.0, 1.0, 0.0, 0.0,
                    0.0, 0.0, 1.0, 0.0,
                    0.0, 0.0, 0.0, 1.0,
                ]
        
        for idx, obj in enumerate(export_objects):
            # Progress reporting
            if progress_callback:
                progress_callback(idx + 1, object_count)
            elif (idx + 1) % 100 == 0:
                print(f"Exported {idx + 1}/{object_count} objects...")
            
            # Create feature element
            ns_key, local_name = _feature_tag(obj)
            feature_gid = _feature_id(obj)
            feature = Element(Q(ns_key, local_name), {GML_ID: feature_gid})
            # Feature genericAttributes (roundtrip)
            try:
                _write_generic_attributes_citygml3(feature, obj)
            except Exception:
                pass
            
            # Export geometry (simplified - full implementation would mirror exporter.py)
            try:
                obj_eval = obj.evaluated_get(context.view_layer.depsgraph)
                mesh = obj_eval.to_mesh()
                
                if mesh and len(mesh.polygons) > 0:
                    # Get transformation
                    ox, oy, oz = _resolve_export_offset(context, obj)

                    # CityGML3: For Vegetation and CityFurniture (incl. PlantCover),
                    # export LoD2 ImplicitGeometry with inline relativeGeometry (import__v3.gml style).
                    if (ns_key, local_name) in (
                        ("frn", "CityFurniture"),
                        ("veg", "SolitaryVegetationObject"),
                        ("veg", "PlantCover"),
                    ):
                        # referencePoint = object origin in target CRS
                        loc = obj.matrix_world.translation
                        x0, y0, z0 = float(loc.x) + ox, float(loc.y) + oy, float(loc.z) + oz
                        ref_x, ref_y, ref_z = trf.transform(x0, y0, z0)

                        # Use stored LoD level from import if available
                        implicit_lod = obj.get("cgml3_implicit_lod")
                        if implicit_lod is not None:
                            export_lod = int(implicit_lod)
                        else:
                            export_lod = 2  # Default to LoD2

                        lod_imp = SubElement(feature, f"lod{export_lod}ImplicitRepresentation")
                        imp = SubElement(lod_imp, "ImplicitGeometry")

                        # transformationMatrix: take rotation/scale from matrix_world, keep translation at 0
                        mw = obj.matrix_world.copy()
                        try:
                            mw[0][3] = 0.0
                            mw[1][3] = 0.0
                            mw[2][3] = 0.0
                        except Exception:
                            pass
                        tm = SubElement(imp, "transformationMatrix")
                        tm.text = " ".join(fmt(v) for v in _matrix_world_to_citygml_tm(mw))

                        ref = SubElement(imp, "referencePoint")
                        pt = SubElement(ref, Q("gml", "Point"))
                        pos = SubElement(pt, Q("gml", "pos"))
                        pos.set("srsDimension", "3")
                        pos.text = f"{fmt(ref_x)} {fmt(ref_y)} {fmt(ref_z)}"

                        rel = SubElement(imp, "relativeGeometry")
                        cs = SubElement(rel, Q("gml", "CompositeSurface"), {GML_ID: f"UUID_{uuid4()}"})

                        for poly in mesh.polygons:
                            verts_local = [mesh.vertices[vi].co for vi in poly.vertices]
                            verts_world = [(obj.matrix_world @ v) for v in verts_local]

                            verts_transformed = []
                            for v in verts_world:
                                x, y, z = v.x + ox, v.y + oy, v.z + oz
                                xt, yt, zt = trf.transform(x, y, z)
                                verts_transformed.append((xt - ref_x, yt - ref_y, zt - ref_z))

                            # Ensure closed ring
                            if verts_transformed and verts_transformed[0] != verts_transformed[-1]:
                                verts_transformed.append(verts_transformed[0])

                            # Accumulate bbox from absolute coordinates
                            writer.accumulate_bbox([(x + ref_x, y + ref_y, z + ref_z) for (x, y, z) in verts_transformed])

                            poly_gid = f"UUID_{uuid4()}"
                            poly_elem = create_polygon_element(
                                poly_gid=poly_gid,
                                exterior_ring=verts_transformed,
                                srs_name="",  # relativeGeometry is in local coords
                                exterior_ring_id=f"UUID_{uuid4()}",
                            )
                            sm = SubElement(cs, Q("gml", "surfaceMember"))
                            sm.append(poly_elem)

                        # Surface/material genericAttributes cannot be written here without knowing the
                        # thematic surfaces. In streaming mode we still export per-material generics by
                        # attaching them to construction surfaces in the boundary writer below (when available).

                        obj_eval.to_mesh_clear()
                        continue
                    
                    # Simple LOD2 Solid export (full implementation would handle all LODs)
                    geom_buffer = StreamingGeometryBuffer()
                    
                    for poly in mesh.polygons:
                        semantic_type = None
                        if ns_key == "bldg":
                            semantic_type = default_unclassified_surface_type
                            try:
                                mi = poly.material_index
                                mat = obj.material_slots[mi].material if 0 <= mi < len(obj.material_slots) else None
                                if mat:
                                    raw_surface_type = mat.get("SurfaceTyp") or mat.get("surface_type")
                                    raw_surface_type = str(raw_surface_type or "").strip()
                                    if raw_surface_type:
                                        semantic_type = raw_surface_type
                            except Exception:
                                pass

                        # Extract vertices
                        verts_local = [mesh.vertices[vi].co for vi in poly.vertices]
                        verts_world = [(obj.matrix_world @ v) for v in verts_local]
                        
                        # Apply offset and transformation
                        verts_transformed = []
                        for v in verts_world:
                            x, y, z = v.x + ox, v.y + oy, v.z + oz
                            xt, yt, zt = trf.transform(x, y, z)
                            verts_transformed.append((xt, yt, zt))
                        
                        # Accumulate bbox from transformed coordinates (CityGML 3.0)
                        writer.accumulate_bbox(verts_transformed)
                        
                        # Ensure closed ring
                        if verts_transformed and verts_transformed[0] != verts_transformed[-1]:
                            verts_transformed.append(verts_transformed[0])
                        
                        # Create polygon
                        poly_gid = f"UUID_{uuid4()}"
                        poly_elem = create_polygon_element(
                            poly_gid=poly_gid,
                            exterior_ring=verts_transformed,
                            srs_name=srs_name,
                            exterior_ring_id=f"UUID_{uuid4()}"
                        )
                        
                        geom_buffer.add_polygon(poly_elem, lod=2, semantic_type=semantic_type)
                    
                    # Attach geometry to feature
                    geom_buffer.attach_to_feature(feature, version="3.0", use_solid=True)
                    
                    # Cleanup
                    obj_eval.to_mesh_clear()
                    geom_buffer.clear()
                
            except Exception as e:
                print(f"Warning: Failed to export geometry for {obj.name}: {e}")
            
            # Add feature-specific attributes
            try:
                # Determine feature namespace from feature tag
                feat_ns, feat_ln = ns_key, local_name
                
                # Building attributes
                if feat_ns == "bldg":
                    for attr_key in ("class", "function", "usage", "roofType", 
                                     "storeysAboveGround", "storeysBelowGround"):
                        full_key = f"bldg:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("bldg", attr_key)).text = str(val)
                    
                    # Construction attributes
                    for attr_key in ("dateOfConstruction", "dateOfDemolition"):
                        full_key = f"con:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("con", attr_key)).text = str(val)
                
                # Bridge attributes
                elif feat_ns == "brid":
                    for attr_key in ("class", "function", "usage"):
                        full_key = f"brid:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("brid", attr_key)).text = str(val)
                    
                    # isMovable as boolean
                    is_movable = obj.get("brid:isMovable")
                    if is_movable is not None:
                        SubElement(feature, Q("brid", "isMovable")).text = "true" if is_movable else "false"
                
                # Tunnel attributes
                elif feat_ns == "tun":
                    for attr_key in ("class", "function", "usage"):
                        full_key = f"tun:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("tun", attr_key)).text = str(val)
                
                # WaterBody attributes
                elif feat_ns == "wtr":
                    for attr_key in ("class", "function", "usage"):
                        full_key = f"wtr:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("wtr", attr_key)).text = str(val)
                
                # Vegetation attributes
                elif feat_ns == "veg":
                    for attr_key in ("class", "function", "usage", "species"):
                        full_key = f"veg:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("veg", attr_key)).text = str(val)
                    
                    # Measurements with uom
                    for attr_key in ("height", "trunkDiameter", "crownDiameter"):
                        full_key = f"veg:{attr_key}"
                        val = obj.get(full_key)
                        if val is not None:
                            elem = SubElement(feature, Q("veg", attr_key))
                            elem.text = fmt(val)
                            uom_key = f"{full_key}:uom"
                            uom = obj.get(uom_key, "m")
                            elem.set("uom", uom)
                
                # CityFurniture attributes
                elif feat_ns == "frn":
                    for attr_key in ("class", "function", "usage"):
                        full_key = f"frn:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("frn", attr_key)).text = str(val)
                
                # LandUse attributes
                elif feat_ns == "luse":
                    for attr_key in ("class", "function", "usage"):
                        full_key = f"luse:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("luse", attr_key)).text = str(val)
                
                # Transportation attributes
                elif feat_ns == "tran":
                    for attr_key in ("class", "function", "usage"):
                        full_key = f"tran:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("tran", attr_key)).text = str(val)
                
            except Exception as e:
                print(f"Warning: Failed to add attributes for {obj.name}: {e}")

            
            # Accumulate bbox from object vertices (before transformation back)
            try:
                if obj.type == 'MESH' and obj.data:
                    coords = [(v.co.x, v.co.y, v.co.z) for v in obj.data.vertices]
                    writer.accumulate_bbox(coords)
            except Exception as e:
                print(f"Warning: Failed to accumulate bbox for {obj.name}: {e}")
            
            # Write feature immediately and free memory
            writer.write_city_object(feature)
            
            # Collect appearance data
            if obj.type == 'MESH' and obj.data.materials:
                try:
                    # Get polygon IDs from geometry buffer (if available)
                    poly_ids = []
                    if hasattr(geom_buffer, 'polygon_ids'):
                        poly_ids = list(geom_buffer.polygon_ids.values())
                    
                    # Collect X3DMaterial data
                    if export_x3d:
                        for mat_idx, mat in enumerate(obj.data.materials):
                            if mat:
                                # Get polygons using this material
                                mat_poly_ids = []
                                if hasattr(obj.data, 'polygons'):
                                    for poly in obj.data.polygons:
                                        # Edge case: material_index out of range
                                        if (poly.material_index == mat_idx and 
                                            poly_ids and 
                                            poly.index < len(poly_ids)):
                                            poly_gml_id = poly_ids[poly.index]
                                            if poly_gml_id:
                                                mat_poly_ids.append(poly_gml_id)
                                
                                # Edge case: Material exists but no polygons use it
                                if mat_poly_ids:
                                    appearance_collector.add_material(obj, mat, mat_poly_ids)
                    
                    # Collect ParameterizedTexture data
                    if export_ptx and obj.data.uv_layers:
                        uv_layer = obj.data.uv_layers.active
                        if uv_layer:
                            for mat_idx, mat in enumerate(obj.data.materials):
                                # Edge case: Material slot empty or no texture
                                if not mat:
                                    continue
                                if not first_image_path_from_material(obj, mat_idx):
                                    continue
                                    
                                # Collect UV data per polygon
                                for poly in obj.data.polygons:
                                    if (poly.material_index == mat_idx and 
                                        poly_ids and 
                                        poly.index < len(poly_ids)):
                                        poly_gml_id = poly_ids[poly.index]
                                        if poly_gml_id:
                                            # Get UV coordinates for this polygon
                                            uvs = []
                                            for loop_idx in poly.loop_indices:
                                                uv = uv_layer.data[loop_idx].uv
                                                uvs.append((uv.x, uv.y))
                                            
                                            # Edge case: Empty UV data handled in add_texture()
                                            ring_id = f"{poly_gml_id}_0_"
                                            appearance_collector.add_texture(
                                                obj, mat, mat_idx, poly_gml_id, ring_id, uvs
                                            )
                
                except Exception as e:
                    print(f"Warning: Failed to collect appearance for {obj.name}: {e}")
                    import traceback
                    traceback.print_exc()
        
        # Generate and write appearance elements at end
        print(f"\nGenerating appearance data...")
        appearance_elements = appearance_collector.generate_appearance_elements()
        
        for app_elem in appearance_elements:
            writer.buffer_appearance(app_elem)
        
        # Print appearance statistics
        app_stats = appearance_collector.get_stats()
        print(f"  Materials: {app_stats['materials']} ({app_stats['material_targets']} targets)")
        print(f"  Textures: {app_stats['textures']} ({app_stats['texture_entries']} entries)")
        print(f"  Texture files exported: {app_stats['texture_files_exported']}")
        
        print(f"\nStreaming export complete!")
        
    # Print statistics
    stats = writer.get_stats()
    print(stats.get_summary())
    
    return stats


def export_citygml2_streaming(
    filepath: str,
    context,
    srs_name: str,
    feature_type_prop: str,
    *,
    export_x3d: bool = True,
    export_ptx: bool = True,
    export_gtx: bool = True,
    use_inner_outer_script: bool = False,
    autofill_new_surfaces: bool = False,
    unclassified_surface_type: str = "WallSurface",
    use_streaming: Optional[bool] = None,
    enable_memory_tracking: bool = False,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    export_types: Optional[list[str]] = None,
):
    """
    Export Blender scene to CityGML 2.0 using streaming XML writer.
    
    Same parameters as export_blender_to_citygml3_streaming but for CityGML 2.0.
    """
    export_types_set = set(export_types or [])
    default_unclassified_surface_type = str(unclassified_surface_type or "WallSurface").strip() or "WallSurface"

    def _should_export_obj(obj_) -> bool:
        if not export_types_set:
            return True
        val = None
        try:
            if feature_type_prop:
                v = obj_.get(feature_type_prop, None)
                if v is not None and str(v).strip():
                    val = str(v).strip()
        except Exception:
            pass

        if not val:
            try:
                v = obj_.get("cgml3_feature", None)
                if v is not None and str(v).strip():
                    val = str(v).strip()
            except Exception:
                pass

        if not val:
            val = "Building"

        try:
            ns_key, local_name = resolve_feature_tag(val)
        except Exception:
            return False
        return f"{ns_key}:{local_name}" in export_types_set

    _INLINE_SUBFEATURE_COLLECTION_NAMES = {
        "BuildingInstallation", "BuildingInstallations",
        "IntBuildingInstallation", "IntBuildingInstallations",
        "BuildingFurniture", "BuildingFurnitures",
        "BridgeInstallation", "BridgeInstallations",
        "IntBridgeInstallation", "IntBridgeInstallations",
        "BridgeConstructionElement", "BridgeConstructionElements",
        "BridgeFurniture", "BridgeFurnitures",
        "TunnelInstallation", "TunnelInstallations",
        "IntTunnelInstallation", "IntTunnelInstallations",
        "TunnelFurniture", "TunnelFurnitures",
    }

    def _object_in_auto_disabled_inline_collection(obj_):
        try:
            collections = list(obj_.users_collection)
        except Exception:
            collections = []
        for collection in collections:
            try:
                name = str(getattr(collection, "name", "") or "").strip()
            except Exception:
                name = ""
            if "." in name:
                base, suffix = name.rsplit(".", 1)
                if suffix.isdigit():
                    name = base
            if name not in _INLINE_SUBFEATURE_COLLECTION_NAMES:
                continue
            try:
                if bool(collection.get("cgml3_auto_disabled_subfeatures", False)):
                    return True
            except Exception:
                pass
        return False

    # Collect objects
    export_objects = [
        obj for obj in context.scene.objects
        if obj.type == 'MESH'
        and object_is_viewport_visible(obj, context)
        and not obj.hide_render
        and not _object_in_auto_disabled_inline_collection(obj)
        and _should_export_obj(obj)
    ]
    
    object_count = len(export_objects)
    
    if autofill_new_surfaces:
        use_streaming = False
    elif default_unclassified_surface_type != "WallSurface":
        use_streaming = False
    elif use_streaming is None:
        use_streaming = should_use_streaming(object_count)
    
    if not use_streaming:
        print(f"Using traditional DOM export ({object_count} objects)")
        from ...io_v2 import export_citygml2_from_blender
        export_citygml2_from_blender(
            filepath, context, srs_name, feature_type_prop,
            export_x3d=export_x3d,
            export_ptx=export_ptx,
            export_gtx=export_gtx,
            use_inner_outer_script=use_inner_outer_script,
            autofill_new_surfaces=autofill_new_surfaces,
            unclassified_surface_type=default_unclassified_surface_type,
            export_types=list(export_types_set) if export_types_set else None,
        )
        return None
    
    print(f"Using streaming export for CityGML 2.0 ({object_count} objects)")
    
    # Prepare CRS transformation
    _epsg_num = _extract_epsg(srs_name) or _extract_epsg(_read_world_crs()) or "25832"
    src_crs = f"EPSG:{_epsg_num}"
    tgt_crs = srs_name or f"EPSG:{_epsg_num}"
    
    def _export_wants_3d() -> bool:
        try:
            w = bpy.data.worlds.get("World")
            return bool(w and "Z-Origin" in w)
        except Exception:
            return True
    
    trf = GeoTransformer(src_crs, tgt_crs, export_3d=_export_wants_3d())
    
    # Tracking
    used_feat_ids = set()
    all_bbox_coords = []
    
    # Helper functions (same as CityGML 3.0)
    def _feature_id(obj_):
        try:
            raw = obj_.get("gml_id", None)
        except Exception:
            raw = None
        
        if raw is not None:
            gid = str(raw).strip()
            if gid:
                # XSD-Konformität sicherstellen: xs:ID muss mit Buchstabe/_ beginnen
                # UUIDs aus 3DCityDB können mit Zahl beginnen → make_gml_id validiert
                return make_gml_id(gid, used_feat_ids)
        
        return make_gml_id(obj_.name, used_feat_ids)
    
    def _feature_tag(obj_):
        val = None
        try:
            if feature_type_prop:
                v = obj_.get(feature_type_prop, None)
                if v is not None and str(v).strip():
                    val = str(v).strip()
        except Exception:
            pass
        
        if not val:
            try:
                v = obj_.get("cgml3_feature", None)
                if v is not None and str(v).strip():
                    val = str(v).strip()
            except Exception:
                pass
        
        if not val:
            val = "Building"
        
        ns_key, local_name = resolve_feature_tag(val)
        return ns_key, local_name
    
    def _resolve_export_offset(ctx, obj_):
        try:
            w = bpy.data.worlds.get("World")
            if w and all(k in w for k in ("X-Origin", "Y-Origin", "Z-Origin")):
                return float(w["X-Origin"]), float(w["Y-Origin"]), float(w["Z-Origin"])
        except Exception:
            pass
        # Fallback: Scene properties
        try:
            sc = ctx.scene
            if sc and "crs x" in sc and "crs y" in sc:
                z = float(sc.get("crs z", 0.0))
                return float(sc["crs x"]), float(sc["crs y"]), z
        except Exception:
            pass
        so = getattr(ctx.scene, "cgml3_offset", (0.0, 0.0, 0.0))
        return float(so[0]), float(so[1]), float(so[2])
    
    # Create export directory
    out_dir = os.path.dirname(os.path.abspath(filepath))
    os.makedirs(os.path.join(out_dir, "appearance"), exist_ok=True)
    
    # Streaming export with CityGML 2.0 namespaces
    with StreamingXMLWriter(
        filepath,
        srs_name,
        version="2.0",
        enable_memory_tracking=enable_memory_tracking
    ) as writer:
        
        for idx, obj in enumerate(export_objects):
            # Progress reporting
            if progress_callback:
                progress_callback(idx + 1, object_count)
            elif (idx + 1) % 100 == 0:
                print(f"Exported {idx + 1}/{object_count} objects...")
            
            # Create feature element
            ns_key, local_name = _feature_tag(obj)
            feature_gid = _feature_id(obj)
            feature = Element(Q(ns_key, local_name), {GML_ID: feature_gid})
            
            # Export geometry (same as CityGML 3.0)
            try:
                obj_eval = obj.evaluated_get(context.view_layer.depsgraph)
                mesh = obj_eval.to_mesh()
                
                if mesh and len(mesh.polygons) > 0:
                    # Get transformation
                    ox, oy, oz = _resolve_export_offset(context, obj)
                    
                    # Simple LOD2 Solid export
                    geom_buffer = StreamingGeometryBuffer()
                    
                    for poly in mesh.polygons:
                        # Extract vertices
                        verts_local = [mesh.vertices[vi].co for vi in poly.vertices]
                        verts_world = [(obj.matrix_world @ v) for v in verts_local]
                        
                        # Apply offset and transformation
                        verts_transformed = []
                        for v in verts_world:
                            x, y, z = v.x + ox, v.y + oy, v.z + oz
                            xt, yt, zt = trf.transform(x, y, z)
                            verts_transformed.append((xt, yt, zt))
                        
                        # Accumulate bbox from transformed coordinates (CityGML 2.0)
                        writer.accumulate_bbox(verts_transformed)
                        
                        # Ensure closed ring
                        if verts_transformed and verts_transformed[0] != verts_transformed[-1]:
                            verts_transformed.append(verts_transformed[0])
                        
                        # Create polygon
                        poly_gid = f"UUID_{uuid4()}"
                        poly_elem = create_polygon_element(
                            poly_gid=poly_gid,
                            exterior_ring=verts_transformed,
                            srs_name=srs_name,
                            exterior_ring_id=f"UUID_{uuid4()}"
                        )
                        
                        geom_buffer.add_polygon(poly_elem, lod=2)
                    
                    # Attach geometry to feature (CityGML 2.0: use version="2.0")
                    geom_buffer.attach_to_feature(feature, version="2.0", use_solid=True)
                    
                    # Cleanup
                    obj_eval.to_mesh_clear()
                    geom_buffer.clear()
                
            except Exception as e:
                print(f"Warning: Failed to export geometry for {obj.name}: {e}")
            
            # Add feature-specific attributes (CityGML 2.0 namespaces)
            try:
                feat_ns, feat_ln = ns_key, local_name
                
                # Building attributes
                if feat_ns == "bldg":
                    for attr_key in ("class", "function", "usage", "roofType", 
                                     "storeysAboveGround", "storeysBelowGround"):
                        full_key = f"bldg:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("bldg", attr_key)).text = str(val)
                    
                    # CityGML 2.0: yearOfConstruction/yearOfDemolition
                    for attr_key in ("yearOfConstruction", "yearOfDemolition"):
                        full_key = f"bldg:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("bldg", attr_key)).text = str(val)
                
                # Bridge attributes
                elif feat_ns == "brid":
                    for attr_key in ("class", "function", "usage"):
                        full_key = f"brid:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("brid", attr_key)).text = str(val)
                    
                    is_movable = obj.get("brid:isMovable")
                    if is_movable is not None:
                        SubElement(feature, Q("brid", "isMovable")).text = "true" if is_movable else "false"
                
                # Tunnel attributes
                elif feat_ns == "tun":
                    for attr_key in ("class", "function", "usage"):
                        full_key = f"tun:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("tun", attr_key)).text = str(val)
                
                # WaterBody attributes
                elif feat_ns == "wtr":
                    for attr_key in ("class", "function", "usage"):
                        full_key = f"wtr:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("wtr", attr_key)).text = str(val)
                
                # Vegetation attributes
                elif feat_ns == "veg":
                    for attr_key in ("class", "function", "usage", "species"):
                        full_key = f"veg:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("veg", attr_key)).text = str(val)
                    
                    # Measurements with uom
                    for attr_key in ("height", "trunkDiameter", "crownDiameter"):
                        full_key = f"veg:{attr_key}"
                        val = obj.get(full_key)
                        if val is not None:
                            elem = SubElement(feature, Q("veg", attr_key))
                            elem.text = fmt(val)
                            uom_key = f"{full_key}:uom"
                            uom = obj.get(uom_key, "m")
                            elem.set("uom", uom)
                
                # CityFurniture attributes
                elif feat_ns == "frn":
                    for attr_key in ("class", "function", "usage"):
                        full_key = f"frn:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("frn", attr_key)).text = str(val)
                
                # LandUse attributes
                elif feat_ns == "luse":
                    for attr_key in ("class", "function", "usage"):
                        full_key = f"luse:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("luse", attr_key)).text = str(val)
                
                # Transportation attributes
                elif feat_ns == "tran":
                    for attr_key in ("class", "function", "usage"):
                        full_key = f"tran:{attr_key}"
                        val = obj.get(full_key)
                        if val:
                            SubElement(feature, Q("tran", attr_key)).text = str(val)
                
            except Exception as e:
                print(f"Warning: Failed to add attributes for {obj.name}: {e}")
            
            # Accumulate bbox from object vertices (before transformation back)
            try:
                if obj.type == 'MESH' and obj.data:
                    coords = [(v.co.x, v.co.y, v.co.z) for v in obj.data.vertices]
                    writer.accumulate_bbox(coords)
            except Exception as e:
                print(f"Warning: Failed to accumulate bbox for {obj.name}: {e}")
            
            # Write feature immediately
            writer.write_city_object(feature)
        
        print(f"\nCityGML 2.0 streaming export complete!")
    
    stats = writer.get_stats()
    print(stats.get_summary())
    return stats
