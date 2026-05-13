# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io_v2/reader/xlink_resolver.py
"""
XLink Resolution for CityGML 2.0.

Delegates to shared/xlink_resolver.py.
"""

from ...shared.xlink_resolver import (  # noqa: F401
    XLinkResolver,
    extract_href,
    resolve_href_text,
    get_xlink_stats_summary,
)