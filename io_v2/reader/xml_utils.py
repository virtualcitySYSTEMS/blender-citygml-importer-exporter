# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io_v2/reader/xml_utils.py
"""
XML Parsing Utilities for CityGML 2.0

DEPRECATED: This module wraps shared/xml_parsing.py.
New code should import directly from shared.xml_parsing.
"""

from .namespaces import NS

# Import and re-export shared functions
from ...shared.xml_parsing import (
    localname,
    inherit_srs,
    is_axis_order_latlon,
    parse_poslist,
    _norm_id
)

# Wrapper for get_attr with NS injection
def get_attr(el, ns_key: str, name: str):
    from ...shared.xml_parsing import get_attr as shared_get_attr
    return shared_get_attr(el, ns_key, name, NS)
