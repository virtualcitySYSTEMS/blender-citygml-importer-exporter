# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io/gml3_writer.py
# Kompatibilitäts-Wrapper nach Aufteilung. Keine Funktionsänderung.

from __future__ import annotations

# Re-Exports der bisherigen Symbole
from .writer.namespaces import NS, Q, QC, QG, GML_ID, XSI_SL, build_schema_location
from .writer.helpers import (
    fmt_pos, fmt, _height_srid, _envelope_elem, make_gml_id, resolve_feature_tag,
    is_closed, clamp01, pretty_xml, _ring_area_sign_xyz,
)
from .writer.geometry import format_poslist, write_polygon_with_ring_ids
from .writer.appearance import (
    add_x3d_materials, add_parameterized_textures, add_georeferenced_textures,
)
from .writer.materials import extract_base_color_rgba, first_image_path_from_material
from .writer.texio import _ensure_export_texture, _rel_image_uri
from .writer.document import create_citymodel_root, add_bounded_by, add_cityobject_member
from .writer.exporter import export_blender_to_citygml3

__all__ = [
    # API
    "export_blender_to_citygml3",
    # NS + QName helpers
    "NS", "Q", "QC", "QG", "GML_ID", "XSI_SL", "build_schema_location",
    # helpers
    "fmt_pos", "fmt", "_height_srid", "_envelope_elem", "make_gml_id", "resolve_feature_tag",
    "is_closed", "clamp01", "pretty_xml", "_ring_area_sign_xyz",
    # geometry
    "format_poslist", "write_polygon_with_ring_ids",
    # appearance
    "add_x3d_materials", "add_parameterized_textures", "add_georeferenced_textures",
    # mats + tex
    "extract_base_color_rgba", "first_image_path_from_material",
    "_ensure_export_texture", "_rel_image_uri",
    # doc
    "create_citymodel_root", "add_bounded_by", "add_cityobject_member",
]
