# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Comprehensive CityGML Validation Orchestrator

Combines all validation types:
1. XSD Schema Validation
2. Semantic Validation
3. Geometry Validation
4. Topology Validation

Provides unified interface for complete CityGML validation.
"""

from typing import List, Tuple, Dict
from pathlib import Path

from .validate import validate_citygml3_xsd, well_formed_citygml3
from .validate_citygml2 import validate_citygml2_xsd, well_formed_citygml2
from .semantic_validator import validate_semantic, ValidationError
from .geometry_validator import validate_geometry


class ValidationReport:
    """Complete validation report with all findings"""
    
    def __init__(self):
        self.xsd_valid = False
        self.xsd_message = ""
        
        self.semantic_valid = False
        self.semantic_errors: List[ValidationError] = []
        
        self.geometry_valid = False
        self.geometry_errors: List[ValidationError] = []
        
        self.version = "unknown"
    
    def is_valid(self) -> bool:
        """Check if file passed all validations (no errors, warnings allowed)"""
        return (
            self.xsd_valid and
            self.semantic_valid and
            self.geometry_valid
        )
    
    def has_errors(self) -> bool:
        """Check if any ERROR level issues exist"""
        if not self.xsd_valid:
            return True
        
        for err in self.semantic_errors + self.geometry_errors:
            if err.severity == 'ERROR':
                return True
        
        return False
    
    def get_error_count(self) -> Dict[str, int]:
        """Get count of errors by severity"""
        counts = {'ERROR': 0, 'WARNING': 0, 'INFO': 0}
        
        if not self.xsd_valid:
            counts['ERROR'] += 1
        
        for err in self.semantic_errors + self.geometry_errors:
            counts[err.severity] = counts.get(err.severity, 0) + 1
        
        return counts
    
    def get_summary(self) -> str:
        """Get human-readable summary"""
        lines = []
        lines.append(f"=== CityGML Validation Report ({self.version}) ===\n")
        
        # XSD Validation
        lines.append(f"1. XSD Schema Validation: {'✅ PASS' if self.xsd_valid else '❌ FAIL'}")
        if not self.xsd_valid:
            lines.append(f"   {self.xsd_message}")
        lines.append("")
        
        # Semantic Validation
        semantic_errors = [e for e in self.semantic_errors if e.severity == 'ERROR']
        semantic_warnings = [e for e in self.semantic_errors if e.severity == 'WARNING']
        
        lines.append(f"2. Semantic Validation: {'✅ PASS' if self.semantic_valid else '❌ FAIL'}")
        if semantic_errors:
            lines.append(f"   {len(semantic_errors)} error(s)")
            for err in semantic_errors[:5]:  # Show first 5
                lines.append(f"   - {err}")
            if len(semantic_errors) > 5:
                lines.append(f"   ... and {len(semantic_errors) - 5} more")
        if semantic_warnings:
            lines.append(f"   {len(semantic_warnings)} warning(s)")
            for warn in semantic_warnings[:3]:
                lines.append(f"   - {warn}")
            if len(semantic_warnings) > 3:
                lines.append(f"   ... and {len(semantic_warnings) - 3} more")
        lines.append("")
        
        # Geometry Validation
        geometry_errors = [e for e in self.geometry_errors if e.severity == 'ERROR']
        geometry_warnings = [e for e in self.geometry_errors if e.severity == 'WARNING']
        
        lines.append(f"3. Geometry Validation: {'✅ PASS' if self.geometry_valid else '❌ FAIL'}")
        if geometry_errors:
            lines.append(f"   {len(geometry_errors)} error(s)")
            for err in geometry_errors[:5]:
                lines.append(f"   - {err}")
            if len(geometry_errors) > 5:
                lines.append(f"   ... and {len(geometry_errors) - 5} more")
        if geometry_warnings:
            lines.append(f"   {len(geometry_warnings)} warning(s)")
            for warn in geometry_warnings[:3]:
                lines.append(f"   - {warn}")
            if len(geometry_warnings) > 3:
                lines.append(f"   ... and {len(geometry_warnings) - 3} more")
        lines.append("")
        
        # Overall result
        counts = self.get_error_count()
        lines.append(f"Overall: {counts['ERROR']} error(s), {counts['WARNING']} warning(s), {counts['INFO']} info")
        
        if self.is_valid():
            lines.append("✅ File is valid CityGML")
        elif not self.has_errors():
            lines.append("⚠️  File is valid but has warnings")
        else:
            lines.append("❌ File has validation errors")
        
        return "\n".join(lines)


def detect_citygml_version(file_path: str) -> str:
    """
    Detect CityGML version from file.
    
    Returns:
        "2.0", "3.0", or "unknown"
    """
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(file_path)
        root = tree.getroot()
        
        # Check namespace
        if 'citygml/3.0' in root.tag or 'citygml/3.0' in str(root.attrib):
            return "3.0"
        elif 'citygml/2.0' in root.tag or 'citygml/2.0' in str(root.attrib):
            return "2.0"
        
        # Check for CityGML 3.0 specific elements
        for elem in root.iter():
            if 'citygml/3.0' in str(elem.tag):
                return "3.0"
            elif 'citygml/2.0' in str(elem.tag):
                return "2.0"
        
        return "unknown"
    except Exception:
        return "unknown"


def validate_citygml_comprehensive(
    file_path: str,
    skip_xsd: bool = False,
    skip_semantic: bool = False,
    skip_geometry: bool = False,
    version: str | None = None
) -> ValidationReport:
    """
    Run comprehensive validation on CityGML file.
    
    Args:
        file_path: Path to CityGML file
        skip_xsd: Skip XSD schema validation
        skip_semantic: Skip semantic validation
        skip_geometry: Skip geometry validation
        version: Force specific version ("2.0" or "3.0"), or auto-detect if None
        
    Returns:
        ValidationReport with all findings
    """
    report = ValidationReport()
    
    # Detect version if not provided
    if version is None:
        version = detect_citygml_version(file_path)
    
    report.version = version
    
    # 1. XSD Validation
    if not skip_xsd:
        if version == "3.0":
            report.xsd_valid, report.xsd_message = validate_citygml3_xsd(file_path)
        elif version == "2.0":
            report.xsd_valid, report.xsd_message = validate_citygml2_xsd(file_path)
        else:
            report.xsd_valid = False
            report.xsd_message = f"Unknown CityGML version: {version}"
            return report  # Can't proceed without knowing version
    else:
        report.xsd_valid = True
        report.xsd_message = "XSD validation skipped"
    
    # 2. Semantic Validation
    if not skip_semantic:
        try:
            report.semantic_valid, report.semantic_errors = validate_semantic(file_path, version)
        except Exception as e:
            report.semantic_valid = False
            report.semantic_errors = [
                ValidationError('ERROR', 'SEMANTIC', f"Semantic validation failed: {e}")
            ]
    else:
        report.semantic_valid = True
    
    # 3. Geometry Validation
    if not skip_geometry:
        try:
            report.geometry_valid, report.geometry_errors = validate_geometry(file_path, version)
        except Exception as e:
            report.geometry_valid = False
            report.geometry_errors = [
                ValidationError('ERROR', 'GEOMETRY', f"Geometry validation failed: {e}")
            ]
    else:
        report.geometry_valid = True
    
    return report


def validate_citygml_quick(file_path: str) -> Tuple[bool, str]:
    """
    Quick validation (XSD only).
    
    Args:
        file_path: Path to CityGML file
        
    Returns:
        (is_valid, message)
    """
    version = detect_citygml_version(file_path)
    
    if version == "3.0":
        return validate_citygml3_xsd(file_path)
    elif version == "2.0":
        return validate_citygml2_xsd(file_path)
    else:
        return False, f"Unknown CityGML version: {version}"


def validate_citygml_wellformed(file_path: str) -> Tuple[bool, str]:
    """
    Well-formedness check only (no XSD).
    
    Args:
        file_path: Path to CityGML file
        
    Returns:
        (is_valid, message)
    """
    version = detect_citygml_version(file_path)
    
    if version == "3.0":
        return well_formed_citygml3(file_path)
    elif version == "2.0":
        return well_formed_citygml2(file_path)
    else:
        return False, f"Unknown CityGML version: {version}"


# Command-line interface
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python validate_comprehensive.py <citygml_file> [--quick] [--no-xsd] [--no-semantic] [--no-geometry]")
        sys.exit(1)
    
    file_path = sys.argv[1]
    
    if not Path(file_path).exists():
        print(f"Error: File not found: {file_path}")
        sys.exit(1)
    
    # Parse options
    skip_xsd = '--no-xsd' in sys.argv
    skip_semantic = '--no-semantic' in sys.argv
    skip_geometry = '--no-geometry' in sys.argv
    quick = '--quick' in sys.argv
    
    if quick:
        print("Running quick validation (XSD only)...")
        valid, message = validate_citygml_quick(file_path)
        print(message)
        sys.exit(0 if valid else 1)
    else:
        print("Running comprehensive validation...")
        report = validate_citygml_comprehensive(
            file_path,
            skip_xsd=skip_xsd,
            skip_semantic=skip_semantic,
            skip_geometry=skip_geometry
        )
        
        print(report.get_summary())
        sys.exit(0 if report.is_valid() else 1)
