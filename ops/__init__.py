# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# ops/__init__.py
"""Operations package for CityGML 3 Blender add-on.

Enthält:
- CityDBTool: Wrapper für citydb-tool (3DCityDB v5)
- Validatoren: Wohlgeformt- und XSD-Validierung (CityGML 2.0 + 3.0)
- Auto-Validierung: Automatische Version-Erkennung und Validierung
- Semantische Validierung: Boundary surfaces, Relief components, IDs, XLinks
- Geometrie-Validierung: Degenerate polygons, surface normals, UV coordinates
- Umfassende Validierung: Alle Validierungstypen kombiniert
- Import Scanner: Metadaten-Extraktion vor dem Import
- Instance Manager: Instanced Collections für große Städte
"""

from .citydb_cli import CityDBTool
from . import import_scanner
from .validate import (
    well_formed_citygml3,
    validate_citygml3_xsd,
    validate_if_enabled,
)
from .validate_citygml2 import (
    well_formed_citygml2,
    validate_citygml2_xsd,
)
from .validate_auto import (
    detect_version_from_file,
    validate_citygml_auto,
    well_formed_citygml_auto,
)
from .semantic_validator import (
    validate_semantic,
    ValidationError,
)
from .geometry_validator import (
    validate_geometry,
)
from .validate_comprehensive import (
    validate_citygml_comprehensive,
    validate_citygml_quick,
    validate_citygml_wellformed,
    detect_citygml_version,
    ValidationReport,
)

__all__ = [
    "CityDBTool",
    # CityGML 3.0
    "well_formed_citygml3",
    "validate_citygml3_xsd",
    "validate_if_enabled",
    # CityGML 2.0
    "well_formed_citygml2",
    "validate_citygml2_xsd",
    # Auto (Version-Detection)
    "detect_version_from_file",
    "validate_citygml_auto",
    "well_formed_citygml_auto",
    # Semantic Validation
    "validate_semantic",
    "ValidationError",
    # Geometry Validation
    "validate_geometry",
    # Comprehensive Validation
    "validate_citygml_comprehensive",
    "validate_citygml_quick",
    "validate_citygml_wellformed",
    "detect_citygml_version",
    "ValidationReport",
]
