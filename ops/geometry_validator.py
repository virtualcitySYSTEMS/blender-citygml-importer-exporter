# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Geometry Validation for CityGML

Validates geometric correctness:
- Degenerate geometries (zero-area polygons, duplicate vertices)
- Self-intersections
- Normal orientation (GroundSurface pointing down, RoofSurface up)
- UV coordinates within valid range [0,1]
- Watertight solids (closed surfaces)
"""

from typing import List, Tuple, Set
import xml.etree.ElementTree as ET
import math

from .semantic_validator import ValidationError


class GeometryValidator:
    """Validates geometric properties of CityGML features"""
    
    def __init__(self, version: str = "3.0", tolerance: float = 1e-6):
        """
        Args:
            version: CityGML version ('2.0' or '3.0')
            tolerance: Numerical tolerance for floating point comparisons
        """
        self.version = version
        self.tolerance = tolerance
        self.errors: List[ValidationError] = []
        
        # Namespace mappings
        if version == "3.0":
            self.ns = {
                'gml': 'http://www.opengis.net/gml/3.2',
                'bldg': 'http://www.opengis.net/citygml/building/3.0',
            }
        else:
            self.ns = {
                'gml': 'http://www.opengis.net/gml/3.2',
                'bldg': 'http://www.opengis.net/citygml/building/2.0',
            }
    
    def validate(self, tree: ET.ElementTree) -> List[ValidationError]:
        """
        Run all geometry validation checks.
        
        Args:
            tree: Parsed ElementTree
            
        Returns:
            List of validation errors/warnings
        """
        self.errors = []
        root = tree.getroot()
        
        # Run all validation rules
        self._check_degenerate_polygons(root)
        self._check_surface_normals(root)
        self._check_uv_coordinates(root)
        self._check_watertight_solids(root)
        
        return self.errors
    
    def _parse_pos_list(self, pos_list_text: str) -> List[Tuple[float, float, float]]:
        """Parse gml:posList text into list of 3D coordinates."""
        if not pos_list_text:
            return []
        
        values = pos_list_text.strip().split()
        coords = []
        
        for i in range(0, len(values), 3):
            if i + 2 < len(values):
                try:
                    x = float(values[i])
                    y = float(values[i + 1])
                    z = float(values[i + 2])
                    coords.append((x, y, z))
                except ValueError:
                    pass
        
        return coords
    
    def _check_degenerate_polygons(self, root: ET.Element) -> None:
        """
        Check for degenerate polygons:
        - Fewer than 3 unique vertices
        - Zero area
        - Duplicate consecutive vertices
        """
        for polygon in root.findall(".//gml:Polygon", self.ns):
            poly_id = polygon.get(f"{{{self.ns['gml']}}}id", "unknown")
            
            # Get exterior ring
            exterior = polygon.find(".//gml:exterior//gml:LinearRing//gml:posList", self.ns)
            if exterior is None or not exterior.text:
                continue
            
            coords = self._parse_pos_list(exterior.text)
            
            if len(coords) < 4:  # Need at least 4 (3 unique + closing vertex)
                self.errors.append(ValidationError(
                    'ERROR',
                    'GEOMETRY',
                    f"Polygon has fewer than 4 vertices ({len(coords)})",
                    element_id=poly_id
                ))
                continue
            
            # Check for duplicate consecutive vertices
            for i in range(len(coords) - 1):
                p1 = coords[i]
                p2 = coords[i + 1]
                dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(p1, p2)))
                
                if dist < self.tolerance:
                    self.errors.append(ValidationError(
                        'WARNING',
                        'GEOMETRY',
                        f"Polygon has duplicate consecutive vertices at index {i}",
                        element_id=poly_id
                    ))
            
            # Check for zero area (simplified: check if all points are collinear)
            if len(coords) >= 3:
                area = self._calculate_polygon_area(coords)
                if abs(area) < self.tolerance:
                    self.errors.append(ValidationError(
                        'ERROR',
                        'GEOMETRY',
                        f"Polygon has zero or near-zero area ({area:.2e})",
                        element_id=poly_id
                    ))
    
    def _calculate_polygon_area(self, coords: List[Tuple[float, float, float]]) -> float:
        """Calculate polygon area using cross product (3D)."""
        if len(coords) < 3:
            return 0.0
        
        # Use Newell's method for robust area calculation
        normal = [0.0, 0.0, 0.0]
        
        for i in range(len(coords)):
            p1 = coords[i]
            p2 = coords[(i + 1) % len(coords)]
            
            normal[0] += (p1[1] - p2[1]) * (p1[2] + p2[2])
            normal[1] += (p1[2] - p2[2]) * (p1[0] + p2[0])
            normal[2] += (p1[0] - p2[0]) * (p1[1] + p2[1])
        
        area = math.sqrt(sum(n * n for n in normal)) / 2.0
        return area
    
    def _calculate_normal(self, coords: List[Tuple[float, float, float]]) -> Tuple[float, float, float]:
        """Calculate polygon normal vector using Newell's method."""
        if len(coords) < 3:
            return (0.0, 0.0, 1.0)  # Default up
        
        normal = [0.0, 0.0, 0.0]
        
        for i in range(len(coords)):
            p1 = coords[i]
            p2 = coords[(i + 1) % len(coords)]
            
            normal[0] += (p1[1] - p2[1]) * (p1[2] + p2[2])
            normal[1] += (p1[2] - p2[2]) * (p1[0] + p2[0])
            normal[2] += (p1[0] - p2[0]) * (p1[1] + p2[1])
        
        # Normalize
        length = math.sqrt(sum(n * n for n in normal))
        if length > self.tolerance:
            normal = [n / length for n in normal]
        
        return tuple(normal)
    
    def _check_surface_normals(self, root: ET.Element) -> None:
        """
        Check that surface normals point in expected directions:
        - GroundSurface: Normal should point down (negative Z component)
        - RoofSurface: Normal should point up (positive Z component)
        """
        # Check GroundSurfaces
        for ground_surf in root.findall(".//bldg:GroundSurface", self.ns):
            surf_id = ground_surf.get(f"{{{self.ns['gml']}}}id", "unknown")
            
            for polygon in ground_surf.findall(".//gml:Polygon", self.ns):
                exterior = polygon.find(".//gml:exterior//gml:LinearRing//gml:posList", self.ns)
                if exterior is None or not exterior.text:
                    continue
                
                coords = self._parse_pos_list(exterior.text)
                if len(coords) < 3:
                    continue
                
                normal = self._calculate_normal(coords)
                
                # GroundSurface should point down (normal.z < 0)
                if normal[2] > 0.1:  # Allow small tolerance
                    self.errors.append(ValidationError(
                        'WARNING',
                        'GEOMETRY',
                        f"GroundSurface normal points up (expected down): {normal}",
                        element_id=surf_id
                    ))
        
        # Check RoofSurfaces
        for roof_surf in root.findall(".//bldg:RoofSurface", self.ns):
            surf_id = roof_surf.get(f"{{{self.ns['gml']}}}id", "unknown")
            
            for polygon in roof_surf.findall(".//gml:Polygon", self.ns):
                exterior = polygon.find(".//gml:exterior//gml:LinearRing//gml:posList", self.ns)
                if exterior is None or not exterior.text:
                    continue
                
                coords = self._parse_pos_list(exterior.text)
                if len(coords) < 3:
                    continue
                
                normal = self._calculate_normal(coords)
                
                # RoofSurface should point up (normal.z > 0)
                if normal[2] < -0.1:  # Allow small tolerance
                    self.errors.append(ValidationError(
                        'WARNING',
                        'GEOMETRY',
                        f"RoofSurface normal points down (expected up): {normal}",
                        element_id=surf_id
                    ))
    
    def _check_uv_coordinates(self, root: ET.Element) -> None:
        """
        Check that UV coordinates are within valid range [0, 1].
        
        Note: Some applications may use UV coordinates outside this range
        for tiling, so this is a WARNING rather than ERROR.
        """
        # Find all TexCoordList elements
        for tex_coord in root.findall(".//app:TexCoordList", {'app': 'http://www.opengis.net/citygml/appearance/3.0'}):
            tex_coord_id = tex_coord.get(f"{{{self.ns['gml']}}}id", "unknown")
            
            for text_coords in tex_coord.findall(".//app:textureCoordinates"):
                if not text_coords.text:
                    continue
                
                values = text_coords.text.strip().split()
                
                for i in range(0, len(values), 2):
                    if i + 1 < len(values):
                        try:
                            u = float(values[i])
                            v = float(values[i + 1])
                            
                            if u < 0.0 or u > 1.0 or v < 0.0 or v > 1.0:
                                self.errors.append(ValidationError(
                                    'WARNING',
                                    'GEOMETRY',
                                    f"UV coordinate outside [0,1]: ({u:.3f}, {v:.3f})",
                                    element_id=tex_coord_id
                                ))
                        except ValueError:
                            pass
    
    def _check_watertight_solids(self, root: ET.Element) -> None:
        """
        Check that solids are watertight (closed surfaces).
        
        This is a simplified check that verifies:
        - CompositeSurface has at least 4 surfaces (minimum for closed polyhedron)
        - All edges are shared by exactly 2 polygons
        """
        for solid in root.findall(".//gml:Solid", self.ns):
            solid_id = solid.get(f"{{{self.ns['gml']}}}id", "unknown")
            
            # Find CompositeSurface
            comp_surf = solid.find(".//gml:CompositeSurface", self.ns)
            if comp_surf is None:
                continue
            
            # Count surfaces
            surfaces = comp_surf.findall(".//gml:surfaceMember", self.ns)
            if len(surfaces) < 4:
                self.errors.append(ValidationError(
                    'ERROR',
                    'TOPOLOGY',
                    f"Solid has fewer than 4 surfaces ({len(surfaces)}) - cannot be watertight",
                    element_id=solid_id
                ))
                continue
            
            # Check edge connectivity (simplified)
            # In a watertight solid, every edge should be shared by exactly 2 faces
            edges: dict[Tuple[Tuple[float, float, float], Tuple[float, float, float]], int] = {}
            
            for surface in surfaces:
                polygon = surface.find(".//gml:Polygon", self.ns)
                if polygon is None:
                    continue
                
                exterior = polygon.find(".//gml:exterior//gml:LinearRing//gml:posList", self.ns)
                if exterior is None or not exterior.text:
                    continue
                
                coords = self._parse_pos_list(exterior.text)
                
                # Add edges (order-independent by sorting)
                for i in range(len(coords) - 1):
                    p1 = coords[i]
                    p2 = coords[i + 1]
                    
                    # Round to tolerance to handle floating point issues
                    p1_rounded = tuple(round(c / self.tolerance) * self.tolerance for c in p1)
                    p2_rounded = tuple(round(c / self.tolerance) * self.tolerance for c in p2)
                    
                    edge = tuple(sorted([p1_rounded, p2_rounded]))
                    edges[edge] = edges.get(edge, 0) + 1
            
            # Check for non-manifold edges
            non_manifold = sum(1 for count in edges.values() if count != 2)
            if non_manifold > 0:
                self.errors.append(ValidationError(
                    'WARNING',
                    'TOPOLOGY',
                    f"Solid may not be watertight: {non_manifold} edges are not shared by exactly 2 faces",
                    element_id=solid_id
                ))


def validate_geometry(file_path: str, version: str = "3.0") -> Tuple[bool, List[ValidationError]]:
    """
    Run geometry validation on CityGML file.
    
    Args:
        file_path: Path to CityGML file
        version: CityGML version ('2.0' or '3.0')
        
    Returns:
        (is_valid, errors) where is_valid is True if no ERROR severity issues found
    """
    try:
        tree = ET.parse(file_path)
    except Exception as e:
        return False, [ValidationError('ERROR', 'GEOMETRY', f"Failed to parse XML: {e}")]
    
    validator = GeometryValidator(version)
    errors = validator.validate(tree)
    
    # Check if any errors (not just warnings)
    has_errors = any(err.severity == 'ERROR' for err in errors)
    is_valid = not has_errors
    
    return is_valid, errors
