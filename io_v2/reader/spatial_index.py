# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io_v2/reader/spatial_index.py
"""
Spatial Indexing for Efficient BBOX Filtering (CityGML 2.0).

Delegates to shared/spatial_index.py.
"""

from ...shared.spatial_index import (  # noqa: F401
    FeatureBBox,
    SpatialIndex,
    build_spatial_index_from_gml,
    estimate_spatial_index_benefit,
    RTREE_AVAILABLE,
)