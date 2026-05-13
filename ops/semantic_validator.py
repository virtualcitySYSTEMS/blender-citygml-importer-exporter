# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Semantic Validation Rules for CityGML 2.0 & 3.0

Validates semantic correctness beyond XSD schema validation:
- Boundary surface types in appropriate LODs
- Feature-specific constraints
- Attribute value ranges
- Cross-references integrity
"""

from typing import List, Tuple, Dict, Any
import xml.etree.ElementTree as ET


class ValidationError:
    """Single validation error or warning"""
    
    def __init__(
        self,
        severity: str,  # 'ERROR', 'WARNING', 'INFO'
        category: str,  # 'SEMANTIC', 'GEOMETRY', 'TOPOLOGY', 'ATTRIBUTE'
        message: str,
        element_id: str | None = None,
        location: str | None = None
    ):
        self.severity = severity
        self.category = category
        self.message = message
        self.element_id = element_id
        self.location = location
    
    def __str__(self) -> str:
        parts = [f"[{self.severity}] {self.category}: {self.message}"]
        if self.element_id:
            parts.append(f"(Element: {self.element_id})")
        if self.location:
            parts.append(f"at {self.location}")
        return " ".join(parts)


class SemanticValidator:
    """Validates semantic rules for CityGML documents"""
    
    def __init__(self, version: str = "3.0"):
        """
        Args:
            version: CityGML version ('2.0' or '3.0')
        """
        self.version = version
        self.errors: List[ValidationError] = []
        
        # Namespace mappings
        if version == "3.0":
            self.ns = {
                'core': 'http://www.opengis.net/citygml/3.0',
                'bldg': 'http://www.opengis.net/citygml/building/3.0',
                'brid': 'http://www.opengis.net/citygml/bridge/3.0',
                'tun': 'http://www.opengis.net/citygml/tunnel/3.0',
                'dem': 'http://www.opengis.net/citygml/relief/3.0',
                'gml': 'http://www.opengis.net/gml/3.2',
            }
        else:  # 2.0
            self.ns = {
                'core': 'http://www.opengis.net/citygml/2.0',
                'bldg': 'http://www.opengis.net/citygml/building/2.0',
                'brid': 'http://www.opengis.net/citygml/bridge/2.0',
                'tun': 'http://www.opengis.net/citygml/tunnel/2.0',
                'dem': 'http://www.opengis.net/citygml/relief/2.0',
                'gml': 'http://www.opengis.net/gml/3.2',
            }
    
    def validate(self, tree: ET.ElementTree) -> List[ValidationError]:
        """
        Run all validation checks.
        
        Args:
            tree: Parsed ElementTree
            
        Returns:
            List of validation errors/warnings
        """
        self.errors = []
        root = tree.getroot()
        
        # Run all validation rules
        self._check_boundary_surfaces_in_lod(root)
        self._check_relief_components(root)
        self._check_gml_id_uniqueness(root)
        self._check_xlink_references(root)
        self._check_lod_consistency(root)
        
        if self.version == "3.0":
            self._check_citygml3_specific(root)
        
        return self.errors
    
    def _check_boundary_surfaces_in_lod(self, root: ET.Element) -> None:
        """
        Check that boundary surfaces appear in appropriate LODs.
        
        Rules:
        - WallSurface, RoofSurface, GroundSurface: LOD2+
        - Door, Window: LOD3+
        - lod0FootPrint, lod0RoofEdge: No boundary surfaces
        """
        # LOD0 should not have boundary surfaces
        for lod0_prop in ['lod0FootPrint', 'lod0RoofEdge', 'lod0MultiSurface']:
            xpath = f".//*[local-name()='{lod0_prop}']"
            for lod0_el in root.findall(xpath):
                # Check if any boundary surfaces exist as siblings
                parent = lod0_el.getparent() if hasattr(lod0_el, 'getparent') else None
                if parent:
                    boundaries = parent.findall(".//core:boundary", self.ns)
                    if boundaries:
                        parent_id = parent.get(f"{{{self.ns['gml']}}}id", "unknown")
                        self.errors.append(ValidationError(
                            'ERROR',
                            'SEMANTIC',
                            f"Feature has {lod0_prop} but also boundary surfaces (invalid for LOD0)",
                            element_id=parent_id,
                            location=lod0_prop
                        ))
        
        # Check that boundary surfaces only appear in LOD2+
        surface_types = ['WallSurface', 'RoofSurface', 'GroundSurface', 'CeilingSurface', 
                        'FloorSurface', 'InteriorWallSurface', 'ClosureSurface']
        
        for surf_type in surface_types:
            xpath = f".//*[local-name()='{surf_type}']"
            for surf_el in root.findall(xpath):
                # Check parent feature's LOD
                feature = surf_el
                for _ in range(10):  # Walk up max 10 levels
                    if feature.tag.split('}')[-1] in ['Building', 'BuildingPart', 'Bridge', 'Tunnel']:
                        break
                    feature = feature.getparent() if hasattr(feature, 'getparent') else None
                    if feature is None:
                        break
                
                if feature is not None:
                    # Check if only LOD0/LOD1 geometries exist
                    has_lod01_only = (
                        feature.find(".//bldg:lod0FootPrint", self.ns) is not None or
                        feature.find(".//bldg:lod1Solid", self.ns) is not None or
                        feature.find(".//bldg:lod1MultiSurface", self.ns) is not None
                    )
                    has_lod2plus = (
                        feature.find(".//bldg:lod2Solid", self.ns) is not None or
                        feature.find(".//bldg:lod2MultiSurface", self.ns) is not None or
                        feature.find(".//bldg:lod3Solid", self.ns) is not None
                    )
                    
                    if has_lod01_only and not has_lod2plus:
                        feature_id = feature.get(f"{{{self.ns['gml']}}}id", "unknown")
                        surf_id = surf_el.get(f"{{{self.ns['gml']}}}id", "unknown")
                        self.errors.append(ValidationError(
                            'ERROR',
                            'SEMANTIC',
                            f"{surf_type} found in LOD0/LOD1 only feature (requires LOD2+)",
                            element_id=surf_id,
                            location=f"Feature {feature_id}"
                        ))
    
    def _check_relief_components(self, root: ET.Element) -> None:
        """
        Check ReliefFeature has at least one reliefComponent.
        """
        xpath = ".//*[local-name()='ReliefFeature']"
        for relief_el in root.findall(xpath):
            relief_id = relief_el.get(f"{{{self.ns['gml']}}}id", "unknown")
            
            # Check for reliefComponent children
            components = relief_el.findall(".//dem:reliefComponent", self.ns)
            if not components:
                self.errors.append(ValidationError(
                    'ERROR',
                    'SEMANTIC',
                    "ReliefFeature must have at least one reliefComponent",
                    element_id=relief_id
                ))
    
    def _check_gml_id_uniqueness(self, root: ET.Element) -> None:
        """
        Check that all gml:id values are unique within the document.
        """
        seen_ids: Dict[str, List[str]] = {}
        
        for el in root.iter():
            gml_id = el.get(f"{{{self.ns['gml']}}}id")
            if gml_id:
                tag_name = el.tag.split('}')[-1]
                if gml_id in seen_ids:
                    seen_ids[gml_id].append(tag_name)
                else:
                    seen_ids[gml_id] = [tag_name]
        
        # Report duplicates
        for gml_id, tags in seen_ids.items():
            if len(tags) > 1:
                self.errors.append(ValidationError(
                    'ERROR',
                    'SEMANTIC',
                    f"Duplicate gml:id '{gml_id}' found in {len(tags)} elements: {', '.join(tags)}",
                    element_id=gml_id
                ))
    
    def _check_xlink_references(self, root: ET.Element) -> None:
        """
        Check that xlink:href references point to existing elements.
        """
        # Collect all gml:ids
        all_ids = set()
        for el in root.iter():
            gml_id = el.get(f"{{{self.ns['gml']}}}id")
            if gml_id:
                all_ids.add(gml_id)
        
        # Check all xlink:href
        xlink_ns = 'http://www.w3.org/1999/xlink'
        for el in root.iter():
            href = el.get(f'{{{xlink_ns}}}href')
            if href and href.startswith('#'):
                target_id = href[1:]  # Remove '#'
                if target_id not in all_ids:
                    source_id = el.get(f"{{{self.ns['gml']}}}id", "unknown")
                    self.errors.append(ValidationError(
                        'ERROR',
                        'SEMANTIC',
                        f"xlink:href='#{target_id}' points to non-existent element",
                        element_id=source_id
                    ))
    
    def _check_lod_consistency(self, root: ET.Element) -> None:
        """
        Check LOD consistency within features.
        
        Rules:
        - If LOD2 geometry exists, LOD1 should also exist (recommendation)
        - LOD values should be consistent (e.g., lod="2" with lod2MultiSurface)
        """
        features = root.findall(".//*[local-name()='Building']", self.ns)
        features += root.findall(".//*[local-name()='Bridge']", self.ns)
        features += root.findall(".//*[local-name()='Tunnel']", self.ns)
        
        for feature in features:
            feature_id = feature.get(f"{{{self.ns['gml']}}}id", "unknown")
            
            # Check what LODs are present
            has_lod0 = feature.find(".//*[starts-with(local-name(), 'lod0')]") is not None
            has_lod1 = feature.find(".//*[starts-with(local-name(), 'lod1')]") is not None
            has_lod2 = feature.find(".//*[starts-with(local-name(), 'lod2')]") is not None
            has_lod3 = feature.find(".//*[starts-with(local-name(), 'lod3')]") is not None
            has_lod4 = feature.find(".//*[starts-with(local-name(), 'lod4')]") is not None
            
            # Recommendation: If LOD2+, should have LOD1
            if (has_lod2 or has_lod3 or has_lod4) and not has_lod1:
                self.errors.append(ValidationError(
                    'WARNING',
                    'SEMANTIC',
                    "Feature has LOD2+ but no LOD1 (recommended for completeness)",
                    element_id=feature_id
                ))
    
    def _check_citygml3_specific(self, root: ET.Element) -> None:
        """
        CityGML 3.0 specific validation rules.
        """
        # Check Dynamizer references
        dynamizers = root.findall(".//dyn:Dynamizer", self.ns)
        for dyn in dynamizers:
            dyn_id = dyn.get(f"{{{self.ns['gml']}}}id", "unknown")
            
            # Dynamizer should have dynamizedBy reference
            dynamized_by = dyn.find(".//dyn:dynamizedBy", self.ns)
            if dynamized_by is None:
                self.errors.append(ValidationError(
                    'WARNING',
                    'SEMANTIC',
                    "Dynamizer has no dynamizedBy reference",
                    element_id=dyn_id
                ))


def validate_semantic(file_path: str, version: str = "3.0") -> Tuple[bool, List[ValidationError]]:
    """
    Run semantic validation on CityGML file.
    
    Args:
        file_path: Path to CityGML file
        version: CityGML version ('2.0' or '3.0')
        
    Returns:
        (is_valid, errors) where is_valid is True if no ERROR severity issues found
    """
    try:
        tree = ET.parse(file_path)
    except Exception as e:
        return False, [ValidationError('ERROR', 'SEMANTIC', f"Failed to parse XML: {e}")]
    
    validator = SemanticValidator(version)
    errors = validator.validate(tree)
    
    # Check if any errors (not just warnings)
    has_errors = any(err.severity == 'ERROR' for err in errors)
    is_valid = not has_errors
    
    return is_valid, errors
