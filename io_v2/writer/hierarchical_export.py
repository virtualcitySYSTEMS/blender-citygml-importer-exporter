# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Hierarchischer Export für CityGML 2.0 Buildings mit Rooms, Furniture, Installations und Openings.

Rekonstruiert die CityGML-Struktur aus hierarchischen Blender-Objekten:
- Building-Empty → <bldg:Building>
- Room-Empty → <bldg:interiorRoom><bldg:Room>
- Room-Geometry → <bldg:boundedBy> Surfaces
- Opening-Objekte → <bldg:opening><bldg:Door|Window>
- Furniture-Objekte → <bldg:interiorFurniture><bldg:BuildingFurniture>
- Installation-Objekte → <bldg:roomInstallation><bldg:IntBuildingInstallation>
"""

from xml.etree.ElementTree import SubElement
from typing import List
import numpy as np
import bpy
from uuid import uuid4
from ...shared.export_helpers import object_is_viewport_visible
from .helpers import _envelope_elem, _extract_epsg, _read_world_crs


class _BBoxCollector:
    """Forward bbox coordinates to the current feature and the global export bbox."""

    def __init__(self, *targets):
        self._targets = [target for target in targets if target is not None]

    def append(self, value):
        for target in self._targets:
            target.append(value)

    def extend(self, values):
        values = list(values)
        for target in self._targets:
            target.extend(values)


def _localname(tag) -> str:
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{") and "}" in tag:
        return tag.split("}", 1)[1]
    return tag.split(":", 1)[-1]


def _namespace(tag) -> str:
    if isinstance(tag, str) and tag.startswith("{") and "}" in tag:
        return tag[1:tag.index("}")]
    return ""


def _srs_for_envelope(trf) -> str:
    try:
        auth = trf.tgt.to_authority()
        if auth and auth[0] == "EPSG" and auth[1]:
            return f"EPSG:{auth[1]}"
    except Exception:
        pass
    try:
        srs = trf.tgt.to_string()
        if srs:
            return str(srs)
    except Exception:
        pass
    return f"EPSG:{_extract_epsg(_read_world_crs()) or '25832'}"


def _normalize_lod_value(value, fallback: str = "") -> str:
    try:
        text = str(value if value is not None else "").strip()
    except Exception:
        text = ""
    if text.lower().startswith("lod"):
        text = text[3:].strip()
    if text in {"0", "1", "2", "3", "4"}:
        return text
    return fallback


def _idblock_lod_value(idblock, fallback: str = "") -> str:
    if idblock is None:
        return fallback
    for key in ("lod", "cgml3_lod"):
        try:
            lod = _normalize_lod_value(idblock.get(key, None), "")
        except Exception:
            lod = ""
        if lod:
            return lod
    return fallback


def _material_lod_value(mat, fallback: str = "") -> str:
    try:
        return _normalize_lod_value(mat.get("lod", None), fallback) if mat else fallback
    except Exception:
        return fallback


def _mesh_lod_value(mesh_obj, fallback: str = "4", mesh=None, face_indices=None) -> str:
    lod = _idblock_lod_value(mesh_obj, "")
    if lod:
        return lod

    lod_counts = {}
    if mesh is not None and face_indices is not None:
        for fidx in face_indices:
            try:
                poly = mesh.polygons[fidx]
                mi = poly.material_index
                mat = mesh_obj.material_slots[mi].material if 0 <= mi < len(mesh_obj.material_slots) else None
            except Exception:
                mat = None
            mat_lod = _material_lod_value(mat, "")
            if mat_lod:
                lod_counts[mat_lod] = lod_counts.get(mat_lod, 0) + 1
    else:
        try:
            slots = list(getattr(mesh_obj, "material_slots", []) or [])
        except Exception:
            slots = []
        for slot in slots:
            mat_lod = _material_lod_value(getattr(slot, "material", None), "")
            if mat_lod:
                lod_counts[mat_lod] = lod_counts.get(mat_lod, 0) + 1

    if lod_counts:
        return max(lod_counts.items(), key=lambda item: (item[1], int(item[0])))[0]
    return _normalize_lod_value(fallback, "4")


def _opening_lod_value(lod_value) -> str:
    lod = _normalize_lod_value(lod_value, "2")
    return "4" if lod == "4" else "3"


def _idblock_text_value(idblock, *keys) -> str:
    if idblock is None:
        return ""
    for key in keys:
        try:
            value = idblock.get(key, None)
        except Exception:
            continue
        if value is None:
            continue
        try:
            text = str(value).strip()
        except Exception:
            continue
        if text:
            return text
    return ""


def _material_surface_identity(mat) -> str:
    return _idblock_text_value(mat, "con_surface_id", "gml_surface_id", "surface_id")


def _material_opening_parent_surface_identity(mat) -> str:
    return _idblock_text_value(mat, "filling_parent_surface_id", "opening_surface_id")


def _insert_feature_envelope(feature_el, coords_3d, trf, NS):
    if not coords_3d:
        return

    gml_ns = NS.get("gml", "http://www.opengis.net/gml")
    for child in feature_el:
        if _namespace(child.tag) == gml_ns and _localname(child.tag) == "boundedBy":
            return

    before = len(feature_el)
    _envelope_elem(feature_el, coords_3d, _srs_for_envelope(trf))
    if len(feature_el) <= before:
        return

    envelope_el = feature_el[-1]
    if not (_namespace(envelope_el.tag) == gml_ns and _localname(envelope_el.tag) == "boundedBy"):
        return

    feature_el.remove(envelope_el)
    insert_idx = 0
    for idx, child in enumerate(list(feature_el)):
        if _namespace(child.tag) == gml_ns and _localname(child.tag) in {"description", "name"}:
            insert_idx = idx + 1
    feature_el.insert(insert_idx, envelope_el)


_FEATURE_METADATA_EXPORT_SKIP_KEYS = {
    "cgml3_feature",
    "gml_id",
    "structure_type",
    "structure_part",
    "surface_id_map",
    "surfaces_info",
    "cgml3_surface_generic_attributes",
    "cgml3_surface_generic_attributes_json",
    "cgml3_uv_start_by_ring",
    "cgml3_flip_v",
    "gml_ring_id",
    "gml_polygon_id",
    "gml_multisurface_id",
    "gml_compositesurface_id",
    "gml_surface_id",
    "surface_id",
    "surface_type",
    "SurfaceTyp",
    "image_path",
}

_FEATURE_METADATA_EXPORT_SKIP_PREFIXES = (
    "_",
    "apt_",
    "cgml3_subfeature_",
)


def _copy_missing_feature_metadata_from_outer_shell(obj):
    try:
        children = list(obj.children)
    except Exception:
        return

    outer_shell = None
    for child in children:
        try:
            if child.type == "MESH" and child.get("structure_part") == "outer_shell":
                outer_shell = child
                break
        except Exception:
            continue

    if outer_shell is None:
        return

    for key, value in getattr(outer_shell, "items", lambda: [])():
        if not isinstance(key, str):
            continue
        if key in _FEATURE_METADATA_EXPORT_SKIP_KEYS:
            continue
        if any(key.startswith(prefix) for prefix in _FEATURE_METADATA_EXPORT_SKIP_PREFIXES):
            continue
        try:
            if key in obj:
                continue
            obj[key] = value
        except (TypeError, ValueError):
            try:
                obj[key] = str(value)
            except Exception:
                continue
        except Exception:
            continue


def export_hierarchical_building(
    obj,  # Building-Empty object
    root,  # CityModel root element
    context,
    trf,  # GeoTransformer
    fmt,  # formatting function
    add_cityobject_member,
    Q,  # namespace helper
    NS,  # namespaces dict
    GML_ID,
    _feature_id,
    _resolve_export_offset,
    _write_custom_attributes,
    _write_specific_attributes,
    all_bbox_coords: List,
    unclassified_surface_type: str = "WallSurface",
    _get_app_group=None,
    export_ptx=True,
    export_gtx=False,
    export_x3d=True,
    _remap_image_uri=None
) -> bool:
    """
    Exportiert ein hierarchisches Building mit Rooms, Furniture, Installations und Openings.

    Returns:
        True wenn erfolgreich exportiert, False wenn nicht hierarchisch
    """
    # Prüfe, ob dies ein hierarchisches Building ist
    if not object_is_viewport_visible(obj, context):
        return False

    structure_type = obj.get("structure_type")
    if structure_type != "hierarchical":
        return False

    cgml_feature = obj.get("cgml3_feature")
    feature_ns_by_type = {
        "Building": "bldg",
        "BuildingPart": "bldg",
        "Bridge": "brid",
        "BridgePart": "brid",
        "Tunnel": "tun",
        "TunnelPart": "tun",
    }
    ns_prefix = feature_ns_by_type.get(cgml_feature)
    if ns_prefix is None:
        return False
    
    # Building-Empty hat keine Geometrie, aber Children
    if not obj.children:
        return False
    
    print(f"[DEBUG hierarchical export] Exporting hierarchical building: {obj.name}")
    feature_lod = _idblock_lod_value(obj, "")
    
    # Set zur Vermeidung doppelter gml:id-Werte innerhalb dieses Buildings
    _used_ids_local = set()
    feature_bbox_coords = []
    bbox_collector = _BBoxCollector(feature_bbox_coords, all_bbox_coords)
    
    # Erstelle Building-Element.
    # Wenn root ein CityModel ist, wird ein cityObjectMember erzeugt.
    # Wenn root bereits ein Container (z.B. bldg:consistsOfBuildingPart) ist,
    # hängen wir das Feature direkt darunter.
    if add_cityobject_member is not None:
        com = add_cityobject_member(root)
        feature_parent = com
    else:
        feature_parent = root

    feat_id = _feature_id(obj)
    feature = SubElement(feature_parent, Q(ns_prefix, cgml_feature))
    feature.set(GML_ID, feat_id)

    _copy_missing_feature_metadata_from_outer_shell(obj)
    
    # gml:description (wenn vorhanden)
    description = obj.get("gml_description")
    if description:
        desc_el = SubElement(feature, Q("gml", "description"))
        desc_el.text = str(description)
    
    # gml:name
    if obj.name and obj.name != feat_id:
        name_el = SubElement(feature, Q("gml", "name"))
        name_el.text = obj.name
    
    # Custom Attributes (yearOfConstruction, address etc.)
    _write_custom_attributes(feature, obj, ns_prefix)
    _write_specific_attributes(feature, obj)
    
    # Offset
    ox, oy, oz = _resolve_export_offset(context, obj)
    
    # ============================================================================
    # CityGML 2.0 Schema-Reihenfolge für AbstractBuilding:
    #   ...outerBuildingInstallation*, interiorBuildingInstallation*,
    #   boundedBy*, lod3..., lod4..., interiorRoom*, consistsOfBuildingPart*...
    #
    # Wir müssen Installations VOR boundedBy und Parts NACH interiorRoom schreiben.
    # ============================================================================
    
    # ============================================================================
    # Phase 1: outerBuildingInstallation / interiorBuildingInstallation
    # (Mesh children mit cgml3_feature == "BuildingInstallation" / "IntBuildingInstallation")
    # ============================================================================
    from .helpers import make_gml_id

    _installation_maps = {
        "bldg": {
            "BuildingInstallation": ("bldg", "outerBuildingInstallation", "BuildingInstallation"),
            "IntBuildingInstallation": ("bldg", "interiorBuildingInstallation", "IntBuildingInstallation"),
        },
        "brid": {
            "BridgeInstallation": ("brid", "outerBridgeInstallation", "BridgeInstallation"),
            "IntBridgeInstallation": ("brid", "interiorBridgeInstallation", "IntBridgeInstallation"),
        },
        "tun": {
            "TunnelInstallation": ("tun", "outerTunnelInstallation", "TunnelInstallation"),
            "IntTunnelInstallation": ("tun", "interiorTunnelInstallation", "IntTunnelInstallation"),
        },
    }
    _installation_map = _installation_maps.get(ns_prefix, {})
    print(f"[DEBUG hierarchical export] Children of {obj.name}: {[(c.name, c.type, c.get('cgml3_feature'), c.get('structure_part')) for c in obj.children]}")
    for child in obj.children:
        if not object_is_viewport_visible(child, context):
            continue
        if child.type != "MESH":
            continue
        if child.get("structure_part") == "outer_shell":
            continue
        child_feat = child.get("cgml3_feature")
        if child_feat not in _installation_map:
            continue
        mp_ns, mp_container, mp_local = _installation_map[child_feat]

        wrapper_el = SubElement(feature, Q(mp_ns, mp_container))
        child_id = child.get("gml_id")
        if child_id:
            child_gml_id = make_gml_id(str(child_id), _used_ids_local)
        else:
            child_gml_id = f"ID_{uuid4().hex}"
        part_el = SubElement(wrapper_el, Q(mp_ns, mp_local))
        part_el.set(GML_ID, child_gml_id)

        _write_custom_attributes(part_el, child, mp_ns)
        _write_specific_attributes(part_el, child)

        _export_mesh_as_boundedbys(
            child, part_el, context, trf, fmt, Q, ox, oy, oz, bbox_collector,
            room_empty=None,
            unclassified_surface_type=unclassified_surface_type,
            lod_level=_idblock_lod_value(child, feature_lod or ""),
            module_ns=mp_ns,
            _get_app_group=_get_app_group,
            export_ptx=export_ptx,
            export_gtx=export_gtx,
            export_x3d=export_x3d,
            _remap_image_uri=_remap_image_uri,
            _used_ids=_used_ids_local,
        )

    # ============================================================================
    # Phase 2: OUTER SHELL als boundedBy (lod{N}MultiSurface)
    # ============================================================================
    
    outer_shell_obj = None
    for child in obj.children:
        if not object_is_viewport_visible(child, context):
            continue
        if child.get("structure_part") == "outer_shell":
            outer_shell_obj = child
            break
    
    if outer_shell_obj and outer_shell_obj.type == 'MESH':
        print(f"[DEBUG hierarchical export] Exporting outer shell: {outer_shell_obj.name}")
        _export_mesh_as_boundedbys(
            outer_shell_obj, feature, context, trf, fmt, Q, ox, oy, oz, bbox_collector,
            room_empty=obj,  # Building-EMPTY für Outer-Shell-Openings
            unclassified_surface_type=unclassified_surface_type,
            lod_level=feature_lod or _idblock_lod_value(outer_shell_obj, ""),
            module_ns=ns_prefix,
            _get_app_group=_get_app_group,
            export_ptx=export_ptx,
            export_gtx=export_gtx,
            export_x3d=export_x3d,
            _remap_image_uri=_remap_image_uri,
            _used_ids=_used_ids_local
        )
    
    # ============================================================================
    # Phase 3: ROOMS als <bldg:interiorRoom>
    # ============================================================================
    
    for child in obj.children:
        if not object_is_viewport_visible(child, context):
            continue
        if child.get("structure_part") == "room":
            _export_room(
                child, feature, context, trf, fmt, Q, NS, GML_ID,
                _feature_id, _write_custom_attributes, _write_specific_attributes,
                ox, oy, oz, bbox_collector,
                unclassified_surface_type=unclassified_surface_type,
                _get_app_group=_get_app_group,
                export_ptx=export_ptx,
                export_gtx=export_gtx,
                export_x3d=export_x3d,
                _remap_image_uri=_remap_image_uri,
                _used_ids=_used_ids_local
            )

    # ============================================================================
    # Phase 4: consistsOfBuildingPart (EMPTY children UND Mesh children)
    # Schema: consistsOfBuildingPart* kommt NACH interiorRoom*
    # ============================================================================
    
    # 4a: EMPTY-basierte Parts (rekursiv)
    part_specs = {
        "Building": ("bldg", "consistsOfBuildingPart", "BuildingPart"),
        "BuildingPart": ("bldg", "consistsOfBuildingPart", "BuildingPart"),
        "Bridge": ("brid", "consistsOfBridgePart", "BridgePart"),
        "BridgePart": ("brid", "consistsOfBridgePart", "BridgePart"),
        "Tunnel": ("tun", "consistsOfTunnelPart", "TunnelPart"),
        "TunnelPart": ("tun", "consistsOfTunnelPart", "TunnelPart"),
    }
    spec = part_specs.get(cgml_feature)
    if spec:
        ns_prefix, container_name, child_feature = spec
        for child in obj.children:
            if not object_is_viewport_visible(child, context):
                continue
            if child.type != "EMPTY":
                continue
            if child.get("structure_type") != "hierarchical":
                continue
            if child.get("cgml3_feature") != child_feature:
                continue

            part_container = SubElement(feature, Q(ns_prefix, container_name))

            # Re-use exporter to create the Part feature element and its children
            export_hierarchical_building(
                child,
                part_container,
                context,
                trf,
                fmt,
                add_cityobject_member=None,  # root ist bereits der Container
                Q=Q,
                NS=NS,
                GML_ID=GML_ID,
                _feature_id=_feature_id,
                _resolve_export_offset=_resolve_export_offset,
                _write_custom_attributes=_write_custom_attributes,
                _write_specific_attributes=_write_specific_attributes,
                all_bbox_coords=bbox_collector,
                unclassified_surface_type=unclassified_surface_type,
                _get_app_group=_get_app_group,
                export_ptx=export_ptx,
                export_gtx=export_gtx,
                export_x3d=export_x3d,
                _remap_image_uri=_remap_image_uri,
            )

    # 4b: Mesh-based parts (from "Assign Object Part" operator)
    for child in obj.children:
        if not object_is_viewport_visible(child, context):
            continue
        if child.type != "MESH":
            continue
        if child.get("structure_part") == "outer_shell":
            continue
        child_feat = child.get("cgml3_feature")
        if not spec or child_feat != child_feature:
            continue

        wrapper_el = SubElement(feature, Q(ns_prefix, container_name))
        child_id = child.get("gml_id")
        if child_id:
            child_gml_id = make_gml_id(str(child_id), _used_ids_local)
        else:
            child_gml_id = f"ID_{uuid4().hex}"
        part_el = SubElement(wrapper_el, Q(ns_prefix, child_feature))
        part_el.set(GML_ID, child_gml_id)

        _write_custom_attributes(part_el, child, ns_prefix)
        _write_specific_attributes(part_el, child)

        part_bbox_coords = []
        part_bbox_collector = _BBoxCollector(part_bbox_coords, bbox_collector)
        _export_mesh_as_boundedbys(
            child, part_el, context, trf, fmt, Q, ox, oy, oz, part_bbox_collector,
            room_empty=None,
            unclassified_surface_type=unclassified_surface_type,
            lod_level=_idblock_lod_value(child, feature_lod or ""),
            module_ns=ns_prefix,
            _get_app_group=_get_app_group,
            export_ptx=export_ptx,
            export_gtx=export_gtx,
            export_x3d=export_x3d,
            _remap_image_uri=_remap_image_uri,
            _used_ids=_used_ids_local,
        )
        _insert_feature_envelope(part_el, part_bbox_coords, trf, NS)
    
    # ============================================================================
    # Phase 5: XSD-Reihenfolge sicherstellen
    # CityGML 2.0 erfordert:
    #   ...outerBuildingInstallation* → interiorBuildingInstallation* → boundedBy*
    #   → lod3...lod4... → interiorRoom* → consistsOfBuildingPart* → address*
    # ============================================================================
    _insert_feature_envelope(feature, feature_bbox_coords, trf, NS)
    _reorder_building_children_for_xsd(feature, ns_prefix, NS)

    return True


def _reorder_building_children_for_xsd(feature_el, ns_prefix, NS):
    """Reorder children of a Building/BuildingPart to satisfy CityGML 2.0 XSD."""
    try:
        kids = list(feature_el)
        if not kids:
            return

        feature_ns = NS.get(ns_prefix, "")

        def _lname(tag):
            if not isinstance(tag, str):
                return ""
            if tag.startswith("{") and "}" in tag:
                return tag.split("}", 1)[1]
            return tag.split(":", 1)[-1]

        def _ns(tag):
            if not isinstance(tag, str):
                return ""
            if tag.startswith("{") and "}" in tag:
                return tag[1:tag.index("}")]
            return ""

        early_set = {
            "outerBuildingInstallation",
            "interiorBuildingInstallation",
        }
        bounded_name = "boundedBy"
        room_name = "interiorRoom"
        part_name = "consistsOfBuildingPart"
        address_name = "address"
        furniture_name = "interiorFurniture"

        early = [k for k in kids if _ns(k.tag) == feature_ns and _lname(k.tag) in early_set]
        bounded = [k for k in kids if _ns(k.tag) == feature_ns and _lname(k.tag) == bounded_name]
        rooms = [k for k in kids if _ns(k.tag) == feature_ns and _lname(k.tag) == room_name]
        parts = [k for k in kids if _ns(k.tag) == feature_ns and _lname(k.tag) == part_name]
        addresses = [k for k in kids if _ns(k.tag) == feature_ns and _lname(k.tag) == address_name]
        furniture = [k for k in kids if _ns(k.tag) == feature_ns and _lname(k.tag) == furniture_name]
        classified = set(id(k) for k in early + bounded + rooms + parts + addresses + furniture)
        rest = [k for k in kids if id(k) not in classified]

        # XSD order: rest (name, generics, lod*Solid) → early (installations) → boundedBy → rooms → parts → furniture → addresses
        ordered = rest + early + bounded + rooms + parts + furniture + addresses

        if ordered != kids:
            for k in kids:
                feature_el.remove(k)
            for k in ordered:
                feature_el.append(k)
    except Exception:
        return


def _export_room(
    room_empty,
    building_el,
    context,
    trf,
    fmt,
    Q,
    NS,
    GML_ID,
    _feature_id,
    _write_custom_attributes,
    _write_specific_attributes,
    ox, oy, oz,
    all_bbox_coords,
    unclassified_surface_type: str = "WallSurface",
    _get_app_group=None,
    export_ptx=True,
    export_gtx=False,
    export_x3d=True,
    _remap_image_uri=None,
    _used_ids=None
):
    """Exportiert einen Room als <bldg:interiorRoom>."""
    if not object_is_viewport_visible(room_empty, context):
        return

    room_id = room_empty.get("gml_id", room_empty.name)
    room_name = room_empty.get("gml_name", room_empty.name)
    
    print(f"[DEBUG hierarchical export] Exporting room: {room_name} ({room_id})")
    
    # <bldg:interiorRoom>
    interior_room_el = SubElement(building_el, Q("bldg", "interiorRoom"))
    room_el = SubElement(interior_room_el, Q("bldg", "Room"))
    room_el.set(GML_ID, room_id)
    
    # gml:name
    if room_name and room_name != room_id:
        name_el = SubElement(room_el, Q("gml", "name"))
        name_el.text = room_name
    
    # gml:description (wenn vorhanden)
    room_desc = room_empty.get("gml_description")
    if room_desc:
        desc_el = SubElement(room_el, Q("gml", "description"))
        desc_el.text = str(room_desc)
    
    # KEIN lod4Solid mehr! Nur boundedBy-Surfaces
    
    # Room-Geometrie (Wände, Boden, Decke)
    room_geom_obj = None
    for child in room_empty.children:
        if not object_is_viewport_visible(child, context):
            continue
        if child.get("structure_part") == "room_geometry":
            room_geom_obj = child
            break
    
    if room_geom_obj and room_geom_obj.type == 'MESH':
        print(f"[DEBUG hierarchical export] Exporting room geometry: {len(room_geom_obj.data.polygons)} faces")
        _export_mesh_as_boundedbys(
            room_geom_obj, room_el, context, trf, fmt, Q, ox, oy, oz, all_bbox_coords,
            room_empty=room_empty,  # Übergebe Room-Empty für Opening-Zuordnung
            unclassified_surface_type=unclassified_surface_type,
            _get_app_group=_get_app_group,
            export_ptx=export_ptx,
            export_gtx=export_gtx,
            export_x3d=export_x3d,
            _remap_image_uri=_remap_image_uri,
            _used_ids=_used_ids
        )
    
    # Openings wurden bereits in _export_mesh_as_boundedbys exportiert
    
    # Furniture
    for child in room_empty.children:
        if not object_is_viewport_visible(child, context):
            continue
        if child.get("structure_part") == "furniture":
            _export_furniture(
                child, room_el, context, trf, fmt, Q, GML_ID,
                _feature_id, ox, oy, oz, all_bbox_coords,
                _get_app_group=_get_app_group,
                export_ptx=export_ptx,
                export_gtx=export_gtx,
                export_x3d=export_x3d,
                _remap_image_uri=_remap_image_uri
            )
    
    # Installations
    for child in room_empty.children:
        if not object_is_viewport_visible(child, context):
            continue
        if child.get("structure_part") == "installation":
            _export_installation(
                child, room_el, context, trf, fmt, Q, GML_ID,
                _feature_id, ox, oy, oz, all_bbox_coords,
                _get_app_group=_get_app_group,
                export_ptx=export_ptx,
                export_gtx=export_gtx,
                export_x3d=export_x3d,
                _remap_image_uri=_remap_image_uri
            )


def _export_mesh_as_boundedbys(
    mesh_obj,
    parent_el,  # Building or Room element
    context,
    trf,
    fmt,
    Q,
    ox, oy, oz,
    all_bbox_coords,
    room_empty=None,  # Optional: Room-Empty für Opening-Suche
    unclassified_surface_type: str = "WallSurface",
    lod_level: str | None = None,
    _get_app_group=None,
    export_ptx=True,
    export_gtx=False,
    export_x3d=True,
    _remap_image_uri=None,
    module_ns: str = "bldg",
    _used_ids=None
):
    """
    Exportiert Mesh-Faces als <bldg:boundedBy> Surfaces.
    Gruppiert Faces nach individueller Surface-Identität (surface_id) wenn verfügbar,
    ansonsten Fallback auf Surface-Type (aus Material).
    Fügt Openings den korrekten Surfaces hinzu (wenn room_empty gegeben).
    """
    if not object_is_viewport_visible(mesh_obj, context):
        return

    if _used_ids is None:
        _used_ids = set()
    
    deps = context.evaluated_depsgraph_get()
    obj_eval = mesh_obj.evaluated_get(deps)
    mesh = obj_eval.to_mesh(preserve_all_data_layers=True, depsgraph=deps)
    
    if not mesh or not mesh.polygons:
        if obj_eval:
            obj_eval.to_mesh_clear()
        return
    
    from collections import defaultdict, OrderedDict
    import json as _json
    
    # Lade per-Face surface_id Mapping und surfaces_info
    surface_id_map = {}
    surfaces_info = {}
    sid_json = mesh_obj.get("surface_id_map", "")
    if sid_json:
        try:
            surface_id_map = {int(k): v for k, v in _json.loads(sid_json).items()}
        except (ValueError, TypeError):
            pass
    sinfo_json = mesh_obj.get("surfaces_info", "")
    if sinfo_json:
        try:
            surfaces_info = _json.loads(sinfo_json)
        except (ValueError, TypeError):
            pass
    
    # Gruppiere Faces nach Surface-Identität
    # Key: surface_id (aus Mapping) oder "type_<SurfaceType>" als Fallback
    faces_by_surface = OrderedDict()  # key -> list of face indices
    surface_type_by_key = {}  # key -> surface type (WallSurface, InteriorWallSurface, ...)
    surface_name_by_key = {}  # key -> surface name (gml:name)
    
    default_unclassified_surface_type = str(unclassified_surface_type or "WallSurface").strip() or "WallSurface"
    mesh_lod = _normalize_lod_value(lod_level, "") or _mesh_lod_value(mesh_obj, fallback="4")
    
    # Two-pass grouping: interior faces (with ExteriorPolyId) must be placed in the
    # same surface group as their exterior polygon to ensure correct polygon merging.
    # Pass 1: assign non-interior faces, build polygon_id → group_key map
    _poly_id_to_group_key = {}  # gml_polygon_id → group_key
    _deferred_interior_faces = []  # (fidx, mat) tuples for faces with ExteriorPolyId

    for fidx, poly in enumerate(mesh.polygons):
        surf_type = default_unclassified_surface_type

        # Hole Surface-Type aus Material
        try:
            mi = poly.material_index
            mat = mesh_obj.material_slots[mi].material if 0 <= mi < len(mesh_obj.material_slots) else None
            if mat:
                v = mat.get("surface_type") or mat.get("SurfaceTyp")
                if v:
                    surf_type = str(v)
        except Exception:
            mat = None

        # Check if this is an interior face that should be deferred
        is_interior_face = False
        if mat:
            try:
                is_interior_face = bool(mat.get("Interior", False)) and bool(str(mat.get("ExteriorPolyId", "") or "").strip())
            except Exception:
                pass

        if is_interior_face:
            _deferred_interior_faces.append((fidx, mat))
            continue

        # Bestimme Gruppen-Key: surface_id wenn verfügbar, sonst Fallback auf Typ
        sid = surface_id_map.get(fidx)
        if sid is not None:
            sid = str(sid).strip()
        if not sid:
            sid = _material_surface_identity(mat)
        if sid:
            group_key = sid
            # Typ und Name aus surfaces_info
            sinfo = surfaces_info.get(sid, {})
            if group_key not in surface_type_by_key:
                surface_type_by_key[group_key] = sinfo.get("type", surf_type)
                surface_name_by_key[group_key] = sinfo.get("name", "")
        else:
            group_key = f"type_{surf_type}"
            if group_key not in surface_type_by_key:
                surface_type_by_key[group_key] = surf_type
                surface_name_by_key[group_key] = ""

        if group_key not in faces_by_surface:
            faces_by_surface[group_key] = []
        faces_by_surface[group_key].append(fidx)
        
        # Track polygon_id → group_key for interior face resolution
        if mat:
            try:
                poly_id = str(mat.get("gml_polygon_id", "") or "").strip()
                if poly_id:
                    _poly_id_to_group_key[poly_id] = group_key
            except Exception:
                pass

    # Pass 2: assign interior faces to the same group as their exterior polygon
    for fidx, mat in _deferred_interior_faces:
        ext_poly_id = str(mat.get("ExteriorPolyId", "") or "").strip()
        target_group = _poly_id_to_group_key.get(ext_poly_id)
        if target_group:
            group_key = target_group
        else:
            # Fallback: use own material identity/type if exterior polygon not found
            surf_type = default_unclassified_surface_type
            try:
                v = mat.get("surface_type") or mat.get("SurfaceTyp")
                if v:
                    surf_type = str(v)
            except Exception:
                pass
            sid = _material_surface_identity(mat)
            if sid:
                group_key = sid
                sinfo = surfaces_info.get(sid, {})
                if group_key not in surface_type_by_key:
                    surface_type_by_key[group_key] = sinfo.get("type", surf_type)
                    surface_name_by_key[group_key] = sinfo.get("name", "")
            else:
                group_key = f"type_{surf_type}"
                if group_key not in surface_type_by_key:
                    surface_type_by_key[group_key] = surf_type
                    surface_name_by_key[group_key] = ""

        if group_key not in faces_by_surface:
            faces_by_surface[group_key] = []
        faces_by_surface[group_key].append(fidx)
    
    print(f"[DEBUG export] Surface groups found: {len(faces_by_surface)} "
          f"(types: {set(surface_type_by_key.values())})")
    
    # Transformiere Vertices (matrix_world anwenden für korrekte Weltkoordinaten)
    mw = obj_eval.matrix_world
    coords_local = np.asarray(
        [(float((mw @ v.co).x), float((mw @ v.co).y), float((mw @ v.co).z)) for v in mesh.vertices],
        dtype=np.float64
    )
    coords_tgt, origin_tgt = trf.transform_with_origin(coords_local, (ox, oy, oz))
    
    # Sammle Openings: nach surface_id UND surface_type (Doppelstrategie)
    openings_by_surface_id = defaultdict(list)
    openings_by_surface_type = defaultdict(list)
    if room_empty:
        for child in room_empty.children:
            if not object_is_viewport_visible(child, context):
                continue
            if child.get("structure_part") == "opening":
                opening_surf_id = child.get("opening_surface_id")
                opening_surf_type = child.get("opening_surface_type")
                if opening_surf_id:
                    openings_by_surface_id[opening_surf_id].append(child)
                elif opening_surf_type:
                    openings_by_surface_type[opening_surf_type].append(child)
    
    # Sammle alle Polygon-IDs über alle Surfaces für Appearance-Export
    all_poly_ids_by_material = defaultdict(list)
    # Fallback: Polygon-IDs ohne Material (für Default-Appearance)
    all_poly_ids_no_material = []
    
    # Hilfsfunktion: Ring-Koordinaten für ein Face extrahieren (inkl. Close)
    def _ring_coords_for_face(fidx):
        """Extrahiert die transformierten Ring-Koordinaten für ein Face."""
        poly = mesh.polygons[fidx]
        ring_xyz = []
        for v_idx in poly.vertices:
            coord = coords_tgt[v_idx]
            x = float(coord[0]) + origin_tgt[0]
            y = float(coord[1]) + origin_tgt[1]
            z = float(coord[2]) + origin_tgt[2]
            ring_xyz.append((x, y, z))
            all_bbox_coords.append((x, y, z))
        # Close ring (repeat first vertex)
        if ring_xyz:
            ring_xyz.append(ring_xyz[0])
        return ring_xyz

    def _get_face_material(fidx):
        """Material für ein Face holen."""
        poly = mesh.polygons[fidx]
        try:
            mi = poly.material_index
            return mesh_obj.material_slots[mi].material if 0 <= mi < len(mesh_obj.material_slots) else None
        except Exception:
            return None

    def _lod_for_faces(face_indices, fallback=None):
        return _mesh_lod_value(
            mesh_obj,
            fallback=fallback or mesh_lod,
            mesh=mesh,
            face_indices=face_indices,
        )

    def _get_image_path_for_face(fidx):
        """Bildpfad für ein Face aus dem Material extrahieren."""
        poly = mesh.polygons[fidx]
        try:
            mi = poly.material_index
            if mi < 0 or mi >= len(mesh_obj.material_slots):
                return None
            mat = mesh_obj.material_slots[mi].material
            if not mat or not getattr(mat, "use_nodes", False) or not mat.node_tree:
                return None
            for node in mat.node_tree.nodes:
                if node.type == "TEX_IMAGE" and getattr(node, "image", None):
                    img = node.image
                    if getattr(img, "filepath", ""):
                        return bpy.path.abspath(img.filepath)
        except Exception:
            pass
        return None

    def _get_uvs_for_face(fidx):
        """UV-Koordinaten für ein Face extrahieren (closed ring)."""
        if not mesh.uv_layers.active:
            return None
        poly = mesh.polygons[fidx]
        if not poly.loop_indices:
            return None
        uvs = []
        for li in poly.loop_indices:
            uv = mesh.uv_layers.active.data[li].uv
            uvs.append((float(uv.x), float(uv.y)))
        # Close ring
        if uvs and uvs[0] != uvs[-1]:
            uvs.append(uvs[0])
        return uvs

    # Opening-Typen dürfen nicht als standalone BoundarySurface exportiert werden
    _OPENING_TYPES = {"Door", "Window", "DoorSurface", "WindowSurface"}

    # Separiere Opening-Faces von echten BoundarySurface-Faces
    opening_face_groups = {}  # group_key -> face_indices (für inline-Opening-Export)
    boundary_surface_keys = []
    for group_key, face_indices in faces_by_surface.items():
        surf_type = surface_type_by_key.get(group_key, default_unclassified_surface_type)
        if surf_type in _OPENING_TYPES:
            opening_face_groups[group_key] = face_indices
        else:
            boundary_surface_keys.append(group_key)

    # Exportiere jede Surface-Gruppe als eigenes boundedBy-Element
    last_wall_surface_el = None  # Für Zuordnung von Face-basierten Openings
    surface_el_by_key = {}
    for group_key in boundary_surface_keys:
        face_indices = faces_by_surface[group_key]
        surf_type = surface_type_by_key.get(group_key, default_unclassified_surface_type)
        surf_name = surface_name_by_key.get(group_key, "")
        surface_lod = _lod_for_faces(face_indices, mesh_lod)
        
        print(f"[DEBUG export] Exporting {surf_type} ({group_key}): {len(face_indices)} faces")
        
        # CityGML 2.0: BuildingPart und BuildingInstallation sind KEINE
        # boundedBy-Surfaces, sondern brauchen eigene Wrapper-Elemente.
        if surf_type == "BuildingPart":
            wrapper_el = SubElement(parent_el, Q(module_ns, "consistsOfBuildingPart"))
            surface_el = SubElement(wrapper_el, Q(module_ns, "BuildingPart"))
        elif surf_type in ("BuildingInstallation", "IntBuildingInstallation"):
            prop_local = (
                "outerBuildingInstallation"
                if surf_type == "BuildingInstallation"
                else "interiorBuildingInstallation"
            )
            wrapper_el = SubElement(parent_el, Q(module_ns, prop_local))
            surface_el = SubElement(wrapper_el, Q(module_ns, surf_type))
        elif surf_type == "BuildingFurniture":
            wrapper_el = SubElement(parent_el, Q(module_ns, "interiorFurniture"))
            surface_el = SubElement(wrapper_el, Q(module_ns, "BuildingFurniture"))
        else:
            bounded_by_el = SubElement(parent_el, Q(module_ns, "boundedBy"))
            surface_el = SubElement(bounded_by_el, Q(module_ns, surf_type))
        
        # Track last wall-like surface for attaching face-based openings
        if surf_type in ("WallSurface", "InteriorWallSurface"):
            last_wall_surface_el = surface_el

        surface_el_by_key[group_key] = surface_el
        
        # gml:id der Surface (wenn aus surface_id_map, verwende original gml:id)
        if not group_key.startswith("type_"):
            surface_el.set(Q("gml", "id"), group_key)
        
        # gml:name (falls vorhanden)
        if surf_name:
            name_el = SubElement(surface_el, Q("gml", "name"))
            name_el.text = surf_name
        
        # CityGML 2.0: BuildingInstallation/Furniture nutzt lod{N}Geometry (nicht lod{N}MultiSurface)
        if surf_type in ("BuildingInstallation", "IntBuildingInstallation", "BuildingFurniture"):
            lod4ms_el = SubElement(surface_el, Q(module_ns, f"lod{surface_lod}Geometry"))
        else:
            lod4ms_el = SubElement(surface_el, Q(module_ns, f"lod{surface_lod}MultiSurface"))
        multisurface_el = SubElement(lod4ms_el, Q("gml", "MultiSurface"))
        
        # ================================================================
        # Phase 1: Faces in Polygon-Gruppen ordnen (Exterior + Interior Rings)
        # und CompositeSurface-Zugehörigkeit erkennen.
        # ================================================================
        # poly_groups: exterior_poly_id -> {ext_fidx, ext_ring_id, int_faces: [(fidx, ring_id)]}
        poly_groups = OrderedDict()
        
        for fidx in face_indices:
            mat = _get_face_material(fidx)
            poly_id = ""
            ext_poly_id = ""
            ring_id = ""
            if mat:
                try:
                    poly_id = str(mat.get("gml_polygon_id", "") or "").strip()
                    ext_poly_id = str(mat.get("ExteriorPolyId", "") or "").strip()
                    ring_id = str(mat.get("gml_ring_id", "") or "").strip()
                except Exception:
                    pass
            if not poly_id:
                poly_id = f"poly_{mesh_obj.name}_{fidx}"
            
            if ext_poly_id:
                # Dies ist ein Interior-Ring — gehört zum Polygon ext_poly_id
                grp = poly_groups.get(ext_poly_id)
                if grp is None:
                    grp = {"ext_fidx": None, "ext_poly_id": ext_poly_id,
                            "ext_ring_id": "", "int_faces": [], "cs_id": "", "mat": None}
                    poly_groups[ext_poly_id] = grp
                grp["int_faces"].append((fidx, poly_id, ring_id))
            else:
                # Dies ist ein Exterior-Ring (oder einfaches Polygon ohne Interior)
                grp = poly_groups.get(poly_id)
                if grp is None:
                    grp = {"ext_fidx": fidx, "ext_poly_id": poly_id,
                            "ext_ring_id": ring_id, "int_faces": [], "cs_id": "", "mat": mat}
                    poly_groups[poly_id] = grp
                else:
                    grp["ext_fidx"] = fidx
                    grp["ext_ring_id"] = ring_id
                    grp["mat"] = mat
                # CompositeSurface-ID aus dem Material des Exterior-Rings
                if mat:
                    try:
                        cs = str(mat.get("gml_compositesurface_id", "") or "").strip()
                        if cs:
                            grp["cs_id"] = cs
                    except Exception:
                        pass

            # Material für Appearance sammeln
            if mat:
                all_poly_ids_by_material[mat.name].append(poly_id)
            else:
                all_poly_ids_no_material.append(poly_id)
        
        # ================================================================
        # Phase 2: Polygon-Gruppen nach CompositeSurface gruppieren
        # ================================================================
        cs_groups = defaultdict(list)   # cs_id -> [poly_group]
        no_cs_groups = []                # Polygon-Gruppen ohne CompositeSurface
        
        for ext_pid, grp in poly_groups.items():
            if grp["cs_id"]:
                cs_groups[grp["cs_id"]].append(grp)
            else:
                no_cs_groups.append(grp)
        
        # ================================================================
        # Phase 3: XML-Elemente schreiben
        # ================================================================
        
        def _dedup_id(raw_id):
            """Dedupliziert eine ID innerhalb des Buildings."""
            if _used_ids is None:
                return raw_id
            base = raw_id
            k = 2
            result = raw_id
            while result in _used_ids:
                result = f"{base}_{k}"
                k += 1
            _used_ids.add(result)
            return result
        
        def _write_polygon_el(parent, grp):
            """Schreibt ein <gml:Polygon> mit Exterior + Interior Rings.
            Sammelt gleichzeitig PTX-Daten (UV + Bild) für ParameterizedTexture."""
            ext_pid = _dedup_id(grp["ext_poly_id"])
            polygon_el = SubElement(parent, Q("gml", "Polygon"))
            polygon_el.set(Q("gml", "id"), ext_pid)
            
            # Ring-ID-Zähler für dieses Polygon
            ring_counter = 0
            
            # Exterior Ring
            if grp["ext_fidx"] is not None:
                ext_el = SubElement(polygon_el, Q("gml", "exterior"))
                ring_el = SubElement(ext_el, Q("gml", "LinearRing"))
                ext_ring_id = _dedup_id(grp["ext_ring_id"] or f"{ext_pid}_{ring_counter}_")
                ring_el.set(Q("gml", "id"), ext_ring_id)
                ring_xyz = _ring_coords_for_face(grp["ext_fidx"])
                for (x, y, z) in ring_xyz:
                    pos_el = SubElement(ring_el, Q("gml", "pos"))
                    pos_el.text = f"{fmt(x)} {fmt(y)} {fmt(z)}"
                ring_counter += 1
                
                # PTX-Daten sammeln (Exterior Ring)
                if export_ptx and _get_app_group and _remap_image_uri:
                    img_path = _get_image_path_for_face(grp["ext_fidx"])
                    uvs = _get_uvs_for_face(grp["ext_fidx"])
                    if img_path and uvs:
                        mat = grp.get("mat")
                        app_id = ""
                        theme = ""
                        if mat:
                            app_id = str(mat.get("app_id", "") or "").strip()
                            theme = str(mat.get("app_theme", "") or "").strip()
                        if not app_id:
                            app_id = ext_pid
                        rel_uri = _remap_image_uri(img_path)
                        if rel_uri:
                            app_grp = _get_app_group(app_id, theme)
                            entry = {"poly_id": ext_pid, "ring_id": ext_ring_id, "uvs": uvs}
                            # UV-Startpunkt für Roundtrip bewahren
                            try:
                                d = mesh_obj.data.get("cgml3_uv_start_by_ring", None) if mesh_obj.data else None
                                if d:
                                    start_uv = None
                                    try:
                                        start_uv = d.get(ext_ring_id, None)
                                    except Exception:
                                        pass
                                    if start_uv is not None:
                                        entry["uv_start"] = (float(start_uv[0]), float(start_uv[1]))
                            except Exception:
                                pass
                            app_grp["ptx_by_image"].setdefault(rel_uri, []).append(entry)
            
            # Interior Rings
            for (int_fidx, int_poly_id, int_ring_id) in grp["int_faces"]:
                int_el = SubElement(polygon_el, Q("gml", "interior"))
                ring_el = SubElement(int_el, Q("gml", "LinearRing"))
                actual_ring_id = _dedup_id(int_ring_id or f"{ext_pid}_{ring_counter}_")
                ring_el.set(Q("gml", "id"), actual_ring_id)
                ring_xyz = _ring_coords_for_face(int_fidx)
                for (x, y, z) in ring_xyz:
                    pos_el = SubElement(ring_el, Q("gml", "pos"))
                    pos_el.text = f"{fmt(x)} {fmt(y)} {fmt(z)}"
                ring_counter += 1
                
                # PTX-Daten sammeln (Interior Ring)
                if export_ptx and _get_app_group and _remap_image_uri:
                    img_path = _get_image_path_for_face(int_fidx)
                    uvs = _get_uvs_for_face(int_fidx)
                    if img_path and uvs:
                        int_mat = _get_face_material(int_fidx)
                        app_id = ""
                        theme = ""
                        # Interior rings belong to the exterior polygon's
                        # TextureAssociation, so they must use the exterior
                        # appearance group even if their helper material has
                        # its own app_id.
                        mat = grp.get("mat")
                        if mat:
                            app_id = str(mat.get("app_id", "") or "").strip()
                            theme = str(mat.get("app_theme", "") or "").strip()
                        if not app_id and int_mat:
                            app_id = str(int_mat.get("app_id", "") or "").strip()
                            theme = str(int_mat.get("app_theme", "") or "").strip()
                        if not app_id:
                            app_id = ext_pid
                        rel_uri = _remap_image_uri(img_path)
                        if rel_uri:
                            app_grp = _get_app_group(app_id, theme)
                            entry = {"poly_id": ext_pid, "ring_id": actual_ring_id, "uvs": uvs, "is_interior": True}
                            try:
                                d = mesh_obj.data.get("cgml3_uv_start_by_ring", None) if mesh_obj.data else None
                                if d:
                                    start_uv = None
                                    try:
                                        start_uv = d.get(actual_ring_id, None)
                                    except Exception:
                                        pass
                                    if start_uv is not None:
                                        entry["uv_start"] = (float(start_uv[0]), float(start_uv[1]))
                            except Exception:
                                pass
                            app_grp["ptx_by_image"].setdefault(rel_uri, []).append(entry)
        
        # CompositeSurface-Gruppen schreiben
        for cs_id, grps in cs_groups.items():
            sm = SubElement(multisurface_el, Q("gml", "surfaceMember"))
            cs_el = SubElement(sm, Q("gml", "CompositeSurface"))
            cs_el.set(Q("gml", "id"), _dedup_id(cs_id))
            for grp in grps:
                poly_sm = SubElement(cs_el, Q("gml", "surfaceMember"))
                _write_polygon_el(poly_sm, grp)
        
        # Standalone-Polygone (ohne CompositeSurface) schreiben
        for grp in no_cs_groups:
            sm = SubElement(multisurface_el, Q("gml", "surfaceMember"))
            _write_polygon_el(sm, grp)
        
        # Füge Openings dieser Surface hinzu
        # Primär: nach surface_id matchen, Fallback: nach surface_type
        openings_for_this_surface = openings_by_surface_id.get(group_key, [])
        if not openings_for_this_surface and group_key.startswith("type_"):
            openings_for_this_surface = openings_by_surface_type.get(surf_type, [])
        for opening_obj in openings_for_this_surface:
            _export_opening_inline(
                opening_obj, surface_el, context, trf, fmt, Q,
                ox, oy, oz, all_bbox_coords, _used_ids=_used_ids,
                lod_level=_opening_lod_value(surface_lod),
                module_ns=module_ns
            )
            print(f"[DEBUG export] Added opening {opening_obj.get('gml_name')} to {surf_type}")
    
    # Exportiere Face-basierte Openings (Door/Window-Faces im Mesh, die keine
    # separaten Objekte sind) als <bldg:opening> der letzten WallSurface.
    # NUR wenn keine separaten Opening-Objekte vorhanden sind — sonst wurden
    # diese Faces bereits durch die Opening-Objekte abgedeckt.
    has_opening_objects = bool(openings_by_surface_id or openings_by_surface_type)
    if opening_face_groups and not has_opening_objects:
        # Bestimme Ziel-Surface für die Openings
        fallback_opening_surface_el = last_wall_surface_el
        for group_key, face_indices in opening_face_groups.items():
            target_surface_el = None
            for fidx in face_indices:
                parent_sid = _material_opening_parent_surface_identity(_get_face_material(fidx))
                if parent_sid:
                    target_surface_el = surface_el_by_key.get(parent_sid)
                    if target_surface_el is not None:
                        break

            if target_surface_el is None:
                target_surface_el = fallback_opening_surface_el
                if target_surface_el is None:
                    # Kein WallSurface vorhanden: erzeuge eine leere WallSurface als Container
                    bounded_by_el = SubElement(parent_el, Q(module_ns, "boundedBy"))
                    target_surface_el = SubElement(bounded_by_el, Q(module_ns, "InteriorWallSurface"))
                    fallback_opening_surface_el = target_surface_el

            opening_type = surface_type_by_key.get(group_key, "Door")
            # Normalisiere: "DoorSurface" → "Door", "WindowSurface" → "Window"
            if opening_type == "DoorSurface":
                opening_type = "Door"
            elif opening_type == "WindowSurface":
                opening_type = "Window"
            
            opening_name = surface_name_by_key.get(group_key, "")
            opening_id = group_key if not group_key.startswith("type_") else f"opening_{uuid4().hex[:12]}"
            
            print(f"[DEBUG export] Exporting face-based opening {opening_name or opening_id} ({opening_type})")
            
            # <bldg:opening>
            opening_container = SubElement(target_surface_el, Q(module_ns, "opening"))
            opening_el = SubElement(opening_container, Q(module_ns, opening_type))
            opening_el.set(Q("gml", "id"), opening_id)
            
            if opening_name and opening_name != opening_id:
                name_el = SubElement(opening_el, Q("gml", "name"))
                name_el.text = opening_name
            
            opening_lod = _opening_lod_value(_lod_for_faces(face_indices, mesh_lod))
            lod4ms_el = SubElement(opening_el, Q(module_ns, f"lod{opening_lod}MultiSurface"))
            multisurface_el = SubElement(lod4ms_el, Q("gml", "MultiSurface"))
            
            for fidx in face_indices:
                mat = _get_face_material(fidx)
                poly_id = ""
                ring_id = ""
                if mat:
                    try:
                        poly_id = str(mat.get("gml_polygon_id", "") or "").strip()
                        ring_id = str(mat.get("gml_ring_id", "") or "").strip()
                    except Exception:
                        pass
                if not poly_id:
                    poly_id = f"poly_{mesh_obj.name}_{fidx}"
                
                sm = SubElement(multisurface_el, Q("gml", "surfaceMember"))
                polygon_el = SubElement(sm, Q("gml", "Polygon"))
                polygon_el.set(Q("gml", "id"), poly_id)
                ext_el = SubElement(polygon_el, Q("gml", "exterior"))
                ring_el = SubElement(ext_el, Q("gml", "LinearRing"))
                actual_ring_id = ring_id or f"{poly_id}_0_"
                ring_el.set(Q("gml", "id"), actual_ring_id)
                ring_xyz = _ring_coords_for_face(fidx)
                for (x, y, z) in ring_xyz:
                    pos_el = SubElement(ring_el, Q("gml", "pos"))
                    pos_el.text = f"{fmt(x)} {fmt(y)} {fmt(z)}"
                
                if mat:
                    all_poly_ids_by_material[mat.name].append(poly_id)
                else:
                    all_poly_ids_no_material.append(poly_id)
                
                # PTX-Daten für face-based openings sammeln
                if export_ptx and _get_app_group and _remap_image_uri:
                    img_path = _get_image_path_for_face(fidx)
                    uvs = _get_uvs_for_face(fidx)
                    if img_path and uvs:
                        app_id = ""
                        theme = ""
                        if mat:
                            app_id = str(mat.get("app_id", "") or "").strip()
                            theme = str(mat.get("app_theme", "") or "").strip()
                        if not app_id:
                            app_id = poly_id
                        rel_uri = _remap_image_uri(img_path)
                        if rel_uri:
                            app_grp = _get_app_group(app_id, theme)
                            entry = {"poly_id": poly_id, "ring_id": actual_ring_id, "uvs": uvs}
                            app_grp["ptx_by_image"].setdefault(rel_uri, []).append(entry)

    # Sammle X3DMaterial-Informationen wenn _get_app_group verfügbar
    if export_x3d and _get_app_group and all_poly_ids_by_material:
        _collect_x3d_materials(mesh_obj, all_poly_ids_by_material, _get_app_group)

    # Default-Appearance für Faces ohne Material (graues X3DMaterial)
    if export_x3d and _get_app_group and all_poly_ids_no_material:
        _collect_default_x3d_material(mesh_obj, all_poly_ids_no_material, _get_app_group)
    
    if obj_eval:
        obj_eval.to_mesh_clear()


def _collect_x3d_materials(mesh_obj, poly_ids_by_material, _get_app_group):
    """
    Sammelt X3DMaterial-Informationen (RGBA-Farben) aus Blender-Materialien
    und fügt sie zur Appearance-Gruppe hinzu.
    """
    try:
        from ..writer.materials import extract_base_color_rgba, extract_x3d_params_from_material
    except ImportError:
        try:
            from .materials import extract_base_color_rgba, extract_x3d_params_from_material
        except ImportError:
            print("[WARN] Could not import material extraction functions")
            return
    
    for mat_name, poly_ids in poly_ids_by_material.items():
        if not poly_ids:
            continue
        
        # Finde Material
        mat = bpy.data.materials.get(mat_name)
        if not mat:
            continue
        
        # Extrahiere RGBA-Farbe
        try:
            rgba = extract_base_color_rgba(mat)
        except Exception:
            rgba = (0.8, 0.8, 0.8, 1.0)  # Default grau
        
        # Extrahiere X3D-Parameter (emissive, specular, shininess)
        try:
            params = extract_x3d_params_from_material(mat)
        except Exception:
            params = {}
        
        # Appearance-ID aus Material oder generiere neue
        try:
            app_id = mat.get("app_id") or mat.name or f"ID_{uuid4().hex}"
        except Exception:
            app_id = mat.name or f"ID_{uuid4().hex}"
        
        # Theme (optional)
        try:
            theme = mat.get("app_theme", "")
        except Exception:
            theme = ""
        
        # Hole Appearance-Gruppe
        app_grp = _get_app_group(app_id, theme)
        
        # Erstelle Key für X3D-Material (RGBA + params)
        params_tuple = tuple(sorted(params.items())) if params else ()
        x3d_key = (rgba, params_tuple)
        
        # Füge Material zur Gruppe hinzu
        if x3d_key not in app_grp["x3d_by_key"]:
            app_grp["x3d_by_key"][x3d_key] = {
                "rgba": rgba,
                "params": params,
                "poly_ids": []
            }
        
        # Füge Polygon-IDs hinzu
        app_grp["x3d_by_key"][x3d_key]["poly_ids"].extend(poly_ids)
        
        # (debug removed) appearance polygon collection


def _collect_default_x3d_material(mesh_obj, poly_ids, _get_app_group):
    """
    Erstellt ein Default-X3DMaterial (grau) für Meshes ohne zugewiesene Materialien.
    Wird aufgerufen wenn Faces keine Blender-Materialien haben.
    """
    # Default-Werte analog zum Standard-Export
    rgba = (0.8, 0.8, 0.8, 1.0)
    params = {
        "emissiveColor": (0.0, 0.0, 0.0),
        "specularColor": (0.4, 0.4, 0.4),
        "shininess": 0.5,
        "transparency": 0.0,
    }

    # app_id: verwende Polygon-ID des ersten Faces als Basis
    app_id = poly_ids[0] if poly_ids else f"ID_{uuid4().hex}"

    app_grp = _get_app_group(app_id, "")

    params_tuple = tuple(sorted(params.items()))
    x3d_key = (rgba, params_tuple)

    if x3d_key not in app_grp["x3d_by_key"]:
        app_grp["x3d_by_key"][x3d_key] = {
            "rgba": rgba,
            "params": params,
            "poly_ids": []
        }

    app_grp["x3d_by_key"][x3d_key]["poly_ids"].extend(poly_ids)


def _collect_ptx_for_multisurface(mesh_obj, mesh, _get_app_group, _remap_image_uri):
    """
    Sammelt ParameterizedTexture-Daten (UV + Bildpfad) für alle Faces eines Meshes.
    Wird für Furniture/Installation-Meshes verwendet (_export_mesh_as_multisurface).
    """
    uv_layer = mesh.uv_layers.active
    if not uv_layer:
        return

    for fidx, poly in enumerate(mesh.polygons):
        # Material mit Textur?
        try:
            mi = poly.material_index
            if mi < 0 or mi >= len(mesh_obj.material_slots):
                continue
            mat = mesh_obj.material_slots[mi].material
            if not mat or not getattr(mat, "use_nodes", False) or not mat.node_tree:
                continue
        except Exception:
            continue

        # Bildpfad finden
        img_path = None
        for node in mat.node_tree.nodes:
            if node.type == "TEX_IMAGE" and getattr(node, "image", None):
                img = node.image
                if getattr(img, "filepath", ""):
                    img_path = bpy.path.abspath(img.filepath)
                    break
        if not img_path:
            continue

        # UV-Koordinaten extrahieren
        uvs = []
        for li in poly.loop_indices:
            uv = uv_layer.data[li].uv
            uvs.append((float(uv.x), float(uv.y)))
        if not uvs:
            continue
        # Close ring
        if uvs[0] != uvs[-1]:
            uvs.append(uvs[0])

        # Polygon-ID und Ring-ID aus Material
        poly_id = str(mat.get("gml_polygon_id", "") or "").strip()
        ring_id = str(mat.get("gml_ring_id", "") or "").strip()
        if not poly_id:
            poly_id = f"poly_{mesh_obj.name}_{fidx}"
        if not ring_id:
            ring_id = f"{poly_id}_0_"

        # Appearance-Gruppe
        app_id = str(mat.get("app_id", "") or "").strip() or poly_id
        theme = str(mat.get("app_theme", "") or "").strip()

        rel_uri = _remap_image_uri(img_path)
        if not rel_uri:
            continue

        app_grp = _get_app_group(app_id, theme)
        entry = {"poly_id": poly_id, "ring_id": ring_id, "uvs": uvs}
        app_grp["ptx_by_image"].setdefault(rel_uri, []).append(entry)


def _export_faces_to_multisurface(
    mesh_obj, mesh, multisurface_el, coords_tgt, origin_tgt,
    fmt, Q, all_bbox_coords, poly_ids_by_material=None, _used_ids=None
):
    """
    Gemeinsame Logik: Exportiert Mesh-Faces in ein MultiSurface-Element.
    Rekonstruiert gml:interior-Ringe und gml:CompositeSurface-Wrapper
    basierend auf Material-Custom-Properties (ExteriorPolyId, gml_compositesurface_id).
    """
    from collections import OrderedDict, defaultdict

    def _dedup_id(raw_id):
        """Dedupliziert eine ID wenn _used_ids vorhanden."""
        if _used_ids is None:
            return raw_id
        base = raw_id
        k = 2
        result = raw_id
        while result in _used_ids:
            result = f"{base}_{k}"
            k += 1
        _used_ids.add(result)
        return result

    def _ring_coords(fidx):
        poly = mesh.polygons[fidx]
        ring_xyz = []
        for v_idx in poly.vertices:
            coord = coords_tgt[v_idx]
            x = float(coord[0]) + origin_tgt[0]
            y = float(coord[1]) + origin_tgt[1]
            z = float(coord[2]) + origin_tgt[2]
            ring_xyz.append((x, y, z))
            all_bbox_coords.append((x, y, z))
        if ring_xyz:
            ring_xyz.append(ring_xyz[0])
        return ring_xyz

    def _get_mat(fidx):
        poly = mesh.polygons[fidx]
        try:
            mi = poly.material_index
            return mesh_obj.material_slots[mi].material if 0 <= mi < len(mesh_obj.material_slots) else None
        except Exception:
            return None

    # Phase 1: Faces in Polygon-Gruppen ordnen
    poly_groups = OrderedDict()

    for fidx, poly in enumerate(mesh.polygons):
        mat = _get_mat(fidx)
        poly_id = ""
        ext_poly_id = ""
        ring_id = ""
        if mat:
            try:
                poly_id = str(mat.get("gml_polygon_id", "") or "").strip()
                ext_poly_id = str(mat.get("ExteriorPolyId", "") or "").strip()
                ring_id = str(mat.get("gml_ring_id", "") or "").strip()
            except Exception:
                pass
        if not poly_id:
            poly_id = f"poly_{mesh_obj.name}_{fidx}"

        if ext_poly_id:
            grp = poly_groups.get(ext_poly_id)
            if grp is None:
                grp = {"ext_fidx": None, "ext_poly_id": ext_poly_id,
                        "ext_ring_id": "", "int_faces": [], "cs_id": "", "mat": None}
                poly_groups[ext_poly_id] = grp
            grp["int_faces"].append((fidx, poly_id, ring_id))
        else:
            grp = poly_groups.get(poly_id)
            if grp is None:
                grp = {"ext_fidx": fidx, "ext_poly_id": poly_id,
                        "ext_ring_id": ring_id, "int_faces": [], "cs_id": "", "mat": mat}
                poly_groups[poly_id] = grp
            else:
                grp["ext_fidx"] = fidx
                grp["ext_ring_id"] = ring_id
                grp["mat"] = mat
            if mat:
                try:
                    cs = str(mat.get("gml_compositesurface_id", "") or "").strip()
                    if cs:
                        grp["cs_id"] = cs
                except Exception:
                    pass

        if poly_ids_by_material is not None and mat:
            poly_ids_by_material[mat.name].append(poly_id)

    # Phase 2: CompositeSurface-Gruppierung
    cs_groups = defaultdict(list)
    no_cs_groups = []
    for ext_pid, grp in poly_groups.items():
        if grp["cs_id"]:
            cs_groups[grp["cs_id"]].append(grp)
        else:
            no_cs_groups.append(grp)

    # Phase 3: XML schreiben
    def _write_polygon_el(parent, grp):
        ext_pid = _dedup_id(grp["ext_poly_id"])
        polygon_el = SubElement(parent, Q("gml", "Polygon"))
        polygon_el.set(Q("gml", "id"), ext_pid)
        ring_counter = 0
        if grp["ext_fidx"] is not None:
            ext_el = SubElement(polygon_el, Q("gml", "exterior"))
            ring_el = SubElement(ext_el, Q("gml", "LinearRing"))
            ext_ring_id = _dedup_id(grp["ext_ring_id"] or f"{ext_pid}_{ring_counter}_")
            ring_el.set(Q("gml", "id"), ext_ring_id)
            for (x, y, z) in _ring_coords(grp["ext_fidx"]):
                pos_el = SubElement(ring_el, Q("gml", "pos"))
                pos_el.text = f"{fmt(x)} {fmt(y)} {fmt(z)}"
            ring_counter += 1
        for (int_fidx, int_poly_id, int_ring_id) in grp["int_faces"]:
            int_el = SubElement(polygon_el, Q("gml", "interior"))
            ring_el = SubElement(int_el, Q("gml", "LinearRing"))
            actual_ring_id = _dedup_id(int_ring_id or f"{ext_pid}_{ring_counter}_")
            ring_el.set(Q("gml", "id"), actual_ring_id)
            for (x, y, z) in _ring_coords(int_fidx):
                pos_el = SubElement(ring_el, Q("gml", "pos"))
                pos_el.text = f"{fmt(x)} {fmt(y)} {fmt(z)}"
            ring_counter += 1

    for cs_id, grps in cs_groups.items():
        sm = SubElement(multisurface_el, Q("gml", "surfaceMember"))
        cs_el = SubElement(sm, Q("gml", "CompositeSurface"))
        cs_el.set(Q("gml", "id"), _dedup_id(cs_id))
        for grp in grps:
            poly_sm = SubElement(cs_el, Q("gml", "surfaceMember"))
            _write_polygon_el(poly_sm, grp)

    for grp in no_cs_groups:
        sm = SubElement(multisurface_el, Q("gml", "surfaceMember"))
        _write_polygon_el(sm, grp)


def _export_opening_inline(
    opening_obj,
    surface_el,  # InteriorWallSurface, WallSurface, etc.
    context,
    trf,
    fmt,
    Q,
    ox, oy, oz,
    all_bbox_coords,
    _used_ids=None,
    lod_level: str | None = None,
    module_ns: str = "bldg"
):
    """
    Exportiert Opening als <bldg:opening> innerhalb einer Surface.
    Unterstützt sowohl explizite Geometrie als auch OrientableSurface/xlink.
    """
    if _used_ids is None:
        _used_ids = set()
    
    if not object_is_viewport_visible(opening_obj, context):
        return

    opening_type = opening_obj.get("opening_type", "Window")
    opening_id = opening_obj.get("gml_id", opening_obj.name)
    opening_name = opening_obj.get("gml_name", opening_obj.name)
    
    # Dedupliziere Opening-ID
    base_id = opening_id
    k = 2
    while opening_id in _used_ids:
        opening_id = f"{base_id}_{k}"
        k += 1
    _used_ids.add(opening_id)
    
    print(f"[DEBUG export] Exporting opening: {opening_name} ({opening_type})")
    
    # <bldg:opening>
    opening_container = SubElement(surface_el, Q(module_ns, "opening"))
    opening_el = SubElement(opening_container, Q(module_ns, opening_type))
    opening_el.set(Q("gml", "id"), opening_id)
    
    # gml:name
    if opening_name and opening_name != opening_id:
        name_el = SubElement(opening_el, Q("gml", "name"))
        name_el.text = opening_name
    
    # Prüfe ob xlink-basiertes Opening (OrientableSurface-Referenz)
    explicit_lod = _normalize_lod_value(lod_level, "")
    opening_lod = _opening_lod_value(explicit_lod or _idblock_lod_value(opening_obj, "3"))
    xlink_refs_json = opening_obj.get("xlink_refs", "")
    if xlink_refs_json:
        import json
        try:
            xlink_refs = json.loads(xlink_refs_json)
        except (json.JSONDecodeError, TypeError):
            xlink_refs = []
        
        if xlink_refs:
            lod4ms_el = SubElement(opening_el, Q(module_ns, f"lod{opening_lod}MultiSurface"))
            ms_el = SubElement(lod4ms_el, Q("gml", "MultiSurface"))
            for ref_id in xlink_refs:
                sm_el = SubElement(ms_el, Q("gml", "surfaceMember"))
                os_el = SubElement(sm_el, Q("gml", "OrientableSurface"))
                os_el.set("orientation", "-")
                base_el = SubElement(os_el, Q("gml", "baseSurface"))
                base_el.set(Q("xlink", "href"), f"#{ref_id}")
            return
    
    # Explizite Geometrie exportieren
    _export_opening_multisurface(
        opening_obj, opening_el, context, trf, fmt, Q,
        ox, oy, oz, all_bbox_coords, _used_ids=_used_ids,
        lod_level=opening_lod,
        module_ns=module_ns
    )


def _export_opening_multisurface(
    mesh_obj, parent_el, context, trf, fmt, Q,
    ox, oy, oz, all_bbox_coords, _used_ids=None,
    lod_level: str | None = None,
    module_ns: str = "bldg"
):
    """
    Exportiert Opening-Geometrie als <bldg:lod{N}MultiSurface>.
    """
    if not object_is_viewport_visible(mesh_obj, context):
        return

    if mesh_obj.type != 'MESH':
        return
    
    deps = context.evaluated_depsgraph_get()
    obj_eval = mesh_obj.evaluated_get(deps)
    mesh = obj_eval.to_mesh(preserve_all_data_layers=True, depsgraph=deps)
    
    if not mesh or not mesh.polygons:
        if obj_eval:
            obj_eval.to_mesh_clear()
        return
    
    # Transformiere Vertices (matrix_world anwenden für korrekte Weltkoordinaten)
    mw = obj_eval.matrix_world
    coords_local = np.asarray(
        [(float((mw @ v.co).x), float((mw @ v.co).y), float((mw @ v.co).z)) for v in mesh.vertices],
        dtype=np.float64
    )
    coords_tgt, origin_tgt = trf.transform_with_origin(coords_local, (ox, oy, oz))
    
    explicit_lod = _normalize_lod_value(lod_level, "")
    opening_lod = _opening_lod_value(explicit_lod or _mesh_lod_value(mesh_obj, fallback="3"))
    lod4ms_el = SubElement(parent_el, Q(module_ns, f"lod{opening_lod}MultiSurface"))
    multisurface_el = SubElement(lod4ms_el, Q("gml", "MultiSurface"))
    
    _export_faces_to_multisurface(
        mesh_obj, mesh, multisurface_el, coords_tgt, origin_tgt,
        fmt, Q, all_bbox_coords, _used_ids=_used_ids
    )
    
    if obj_eval:
        obj_eval.to_mesh_clear()


def _export_furniture(
    furn_obj,
    room_el,
    context,
    trf,
    fmt,
    Q,
    GML_ID,
    _feature_id,
    ox, oy, oz,
    all_bbox_coords,
    _get_app_group=None,
    export_ptx=True,
    export_gtx=False,
    export_x3d=True,
    _remap_image_uri=None
):
    """Exportiert Furniture als <bldg:interiorFurniture>."""
    # Verwende _feature_id direkt für XSD-konforme ID-Validierung
    if not object_is_viewport_visible(furn_obj, context):
        return

    furn_id = _feature_id(furn_obj)
    furn_name = furn_obj.get("gml_name", furn_obj.name)
    
    print(f"[DEBUG hierarchical export] Exporting furniture: {furn_name}")
    
    # <bldg:interiorFurniture>
    interior_furn_el = SubElement(room_el, Q("bldg", "interiorFurniture"))
    furn_el = SubElement(interior_furn_el, Q("bldg", "BuildingFurniture"))
    furn_el.set(GML_ID, furn_id)
    
    # gml:name
    if furn_name and furn_name != furn_id:
        name_el = SubElement(furn_el, Q("gml", "name"))
        name_el.text = furn_name
    
    # gml:description
    furn_desc = furn_obj.get("gml_description")
    if furn_desc:
        desc_el = SubElement(furn_el, Q("gml", "description"))
        desc_el.text = str(furn_desc)
    
    # <bldg:lod4Geometry> als MultiSurface
    _export_mesh_as_multisurface(
        furn_obj, furn_el, "lod4Geometry", context, trf, fmt, Q, GML_ID,
        ox, oy, oz, all_bbox_coords,
        _get_app_group=_get_app_group,
        export_ptx=export_ptx,
        export_gtx=export_gtx,
        export_x3d=export_x3d,
        _remap_image_uri=_remap_image_uri
    )


def _export_installation(
    inst_obj,
    room_el,
    context,
    trf,
    fmt,
    Q,
    GML_ID,
    _feature_id,
    ox, oy, oz,
    all_bbox_coords,
    _get_app_group=None,
    export_ptx=True,
    export_gtx=False,
    export_x3d=True,
    _remap_image_uri=None
):
    """Exportiert Installation als <bldg:roomInstallation>."""
    # Verwende _feature_id direkt für XSD-konforme ID-Validierung
    if not object_is_viewport_visible(inst_obj, context):
        return

    inst_id = _feature_id(inst_obj)
    inst_name = inst_obj.get("gml_name", inst_obj.name)
    
    print(f"[DEBUG hierarchical export] Exporting installation: {inst_name}")
    
    # <bldg:roomInstallation>
    room_inst_el = SubElement(room_el, Q("bldg", "roomInstallation"))
    inst_el = SubElement(room_inst_el, Q("bldg", "IntBuildingInstallation"))
    inst_el.set(GML_ID, inst_id)
    
    # gml:name
    if inst_name and inst_name != inst_id:
        name_el = SubElement(inst_el, Q("gml", "name"))
        name_el.text = inst_name
    
    # gml:description
    inst_desc = inst_obj.get("gml_description")
    if inst_desc:
        desc_el = SubElement(inst_el, Q("gml", "description"))
        desc_el.text = str(inst_desc)
    
    # <bldg:lod4Geometry> als MultiSurface
    _export_mesh_as_multisurface(
        inst_obj, inst_el, "lod4Geometry", context, trf, fmt, Q, GML_ID,
        ox, oy, oz, all_bbox_coords,
        _get_app_group=_get_app_group,
        export_ptx=export_ptx,
        export_gtx=export_gtx,
        export_x3d=export_x3d,
        _remap_image_uri=_remap_image_uri
    )


def _export_mesh_as_multisurface(
    mesh_obj,
    parent_el,
    geom_tag,  # "lod4Geometry"
    context,
    trf,
    fmt,
    Q,
    GML_ID,
    ox, oy, oz,
    all_bbox_coords,
    _get_app_group=None,
    export_ptx=True,
    export_gtx=False,
    export_x3d=True,
    _remap_image_uri=None
):
    """Exportiert Mesh als <bldg:lod4Geometry><gml:MultiSurface>."""
    if not object_is_viewport_visible(mesh_obj, context):
        return

    deps = context.evaluated_depsgraph_get()
    obj_eval = mesh_obj.evaluated_get(deps)
    mesh = obj_eval.to_mesh(preserve_all_data_layers=True, depsgraph=deps)
    
    if not mesh or not mesh.polygons:
        if obj_eval:
            obj_eval.to_mesh_clear()
        return
    
    # <bldg:lod4Geometry>
    geom_el = SubElement(parent_el, Q("bldg", geom_tag))
    multisurface_el = SubElement(geom_el, Q("gml", "MultiSurface"))
    
    # Transformiere Vertices (matrix_world anwenden für korrekte Weltkoordinaten)
    mw = obj_eval.matrix_world
    coords_local = np.asarray(
        [(float((mw @ v.co).x), float((mw @ v.co).y), float((mw @ v.co).z)) for v in mesh.vertices],
        dtype=np.float64
    )
    coords_tgt, origin_tgt = trf.transform_with_origin(coords_local, (ox, oy, oz))
    
    # Sammle Material-Informationen für Appearance
    from collections import defaultdict, OrderedDict
    poly_ids_by_material = defaultdict(list)
    
    _export_faces_to_multisurface(
        mesh_obj, mesh, multisurface_el, coords_tgt, origin_tgt,
        fmt, Q, all_bbox_coords, poly_ids_by_material
    )
    
    # Sammle X3DMaterial wenn verfügbar
    if export_x3d and _get_app_group and poly_ids_by_material:
        _collect_x3d_materials(mesh_obj, poly_ids_by_material, _get_app_group)
    
    # Sammle ParameterizedTexture wenn verfügbar
    if export_ptx and _get_app_group and _remap_image_uri and mesh.uv_layers.active:
        _collect_ptx_for_multisurface(mesh_obj, mesh, _get_app_group, _remap_image_uri)
    
    if obj_eval:
        obj_eval.to_mesh_clear()
