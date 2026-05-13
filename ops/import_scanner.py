# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
CityGML Import Scanner
======================

Scans CityGML files before import to extract metadata and statistics.
Provides preview information without loading full geometry.

Features:
- File size and format detection
- CityGML version detection (2.0 / 3.0)
- Feature count and type distribution
- Bounding box extraction
- LOD level distribution
- CRS/EPSG detection
- Memory estimate for import
"""

import bpy
from bpy.types import Operator
from bpy.props import StringProperty
from pathlib import Path
import re
import time

try:
    from lxml import etree as ET
except ImportError:
    import xml.etree.ElementTree as ET


class CityGMLFileInfo:
    """Data class for CityGML file metadata"""
    
    def __init__(self):
        self.filepath = ""
        self.file_size_mb = 0.0
        self.citygml_version = "Unknown"
        
        # Feature statistics
        self.total_features = 0
        self.feature_types = {}  # {feature_type: count}
        
        # LOD statistics
        self.lod_distribution = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
        
        # Spatial extent
        self.bounding_box = None  # (min_x, min_y, min_z, max_x, max_y, max_z)
        self.crs = "Unknown"
        
        # Memory estimate
        self.estimated_import_memory_mb = 0.0
        
        # Scan time
        self.scan_time_seconds = 0.0
        
        # Error flag
        self.error = None


def _localname(tag: str) -> str:
    """Extract local name from namespaced tag"""
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        return tag.split("}")[-1]
    return tag


def _detect_citygml_version(root) -> str:
    """
    Detect CityGML version from XML root element.
    
    Strategies:
    1. Check CityModel namespace URI
    2. Check schemaLocation attribute
    3. Check feature namespaces
    """
    try:
        # Strategy 1: CityModel namespace
        for elem in root.iter():
            tag = getattr(elem, "tag", None)
            if not isinstance(tag, str):
                continue
            
            if _localname(tag) == "CityModel":
                if "citygml/3.0" in tag or "citygml/core/3.0" in tag:
                    return "3.0"
                elif "citygml/2.0" in tag or "citygml/1.0" in tag:
                    return "2.0"
        
        # Strategy 2: schemaLocation
        schema_loc = root.get("{http://www.w3.org/2001/XMLSchema-instance}schemaLocation", "")
        if "citygml/3.0" in schema_loc or "citygml/core/3.0" in schema_loc:
            return "3.0"
        elif "citygml/2.0" in schema_loc or "citygml/1.0" in schema_loc:
            return "2.0"
        
        # Strategy 3: Feature namespaces
        for elem in root.iter():
            tag = getattr(elem, "tag", None)
            if not isinstance(tag, str):
                continue
            
            ln = _localname(tag)
            if ln in ("Building", "Bridge", "Tunnel", "Road", "CityFurniture"):
                if "citygml/3.0" in tag:
                    return "3.0"
                elif "citygml/2.0" in tag or "citygml/building" in tag:
                    return "2.0"
        
    except Exception as e:
        print(f"[Import Scanner] Version detection error: {e}")
    
    return "Unknown"


def _extract_bounding_box(root) -> tuple:
    """
    Extract bounding box from CityGML Envelope or by scanning coordinates.
    
    Returns: (min_x, min_y, min_z, max_x, max_y, max_z) or None
    """
    try:
        # Strategy 1: Use gml:Envelope if present
        for elem in root.iter():
            tag = getattr(elem, "tag", None)
            if not isinstance(tag, str):
                continue
            
            if _localname(tag) == "Envelope":
                lower_text = None
                upper_text = None
                
                for child in elem:
                    child_tag = getattr(child, "tag", None)
                    if not isinstance(child_tag, str):
                        continue
                    
                    ln = _localname(child_tag)
                    if ln == "lowerCorner":
                        lower_text = (child.text or "").strip()
                    elif ln == "upperCorner":
                        upper_text = (child.text or "").strip()
                
                if lower_text and upper_text:
                    try:
                        lower = [float(x) for x in lower_text.split()]
                        upper = [float(x) for x in upper_text.split()]
                        
                        if len(lower) >= 2 and len(upper) >= 2:
                            min_x, min_y = lower[0], lower[1]
                            max_x, max_y = upper[0], upper[1]
                            min_z = lower[2] if len(lower) >= 3 else 0.0
                            max_z = upper[2] if len(upper) >= 3 else 0.0
                            
                            return (min_x, min_y, min_z, max_x, max_y, max_z)
                    except (ValueError, IndexError):
                        pass
        
        # Strategy 2: Scan all coordinates (slower, but more reliable)
        min_x = min_y = min_z = float('inf')
        max_x = max_y = max_z = float('-inf')
        found = False
        
        count = 0
        max_scan = 10000  # Limit scan to avoid long processing times
        
        for elem in root.iter():
            if count > max_scan:
                break
            
            tag = getattr(elem, "tag", None)
            if not isinstance(tag, str):
                continue
            
            ln = _localname(tag)
            if ln not in ("posList", "pos"):
                continue
            
            text = (elem.text or "").strip()
            if not text:
                continue
            
            try:
                vals = [float(x) for x in text.split()]
                
                # Determine dimension (2D or 3D)
                dim_attr = elem.get("srsDimension")
                if dim_attr and dim_attr.isdigit():
                    dim = int(dim_attr)
                else:
                    dim = 3 if len(vals) % 3 == 0 else 2
                
                # Process coordinates
                for i in range(0, len(vals), dim):
                    if i + 1 >= len(vals):
                        break
                    
                    x, y = vals[i], vals[i+1]
                    z = vals[i+2] if dim == 3 and i+2 < len(vals) else 0.0
                    
                    min_x = min(min_x, x)
                    min_y = min(min_y, y)
                    min_z = min(min_z, z)
                    max_x = max(max_x, x)
                    max_y = max(max_y, y)
                    max_z = max(max_z, z)
                    found = True
                    
                    count += 1
                    if count > max_scan:
                        break
            except (ValueError, IndexError):
                pass
        
        if found and min_x != float('inf'):
            return (min_x, min_y, min_z, max_x, max_y, max_z)
    
    except Exception as e:
        print(f"[Import Scanner] Bounding box extraction error: {e}")
    
    return None


def _extract_crs(root) -> str:
    """
    Extract CRS/EPSG code from CityGML file.
    
    Checks:
    1. Envelope@srsName
    2. Geometry@srsName
    """
    try:
        # Strategy 1: Envelope srsName
        for elem in root.iter():
            tag = getattr(elem, "tag", None)
            if not isinstance(tag, str):
                continue
            
            if _localname(tag) == "Envelope":
                srs_name = elem.get("srsName", "")
                if srs_name:
                    return _normalize_epsg(srs_name)
        
        # Strategy 2: First geometry element with srsName
        geom_tags = ("Solid", "MultiSurface", "CompositeSurface", "Polygon", "Point")
        
        for elem in root.iter():
            tag = getattr(elem, "tag", None)
            if not isinstance(tag, str):
                continue
            
            if _localname(tag) in geom_tags:
                srs_name = elem.get("srsName", "")
                if srs_name:
                    return _normalize_epsg(srs_name)
    
    except Exception as e:
        print(f"[Import Scanner] CRS extraction error: {e}")
    
    return "Unknown"

# Import shared CRS normalization (supports ADV-URNs)
from ..shared.materials_common import normalize_crs_to_epsg

# Alias for backward compatibility
_normalize_epsg = normalize_crs_to_epsg


def _count_features(root) -> tuple:
    """
    Count CityGML features by type.
    
    Returns: (total_count, {feature_type: count})
    """
    feature_types = {}
    
    # CityGML feature types (CityGML 2.0 and 3.0)
    known_features = {
        "Building", "BuildingPart",
        "Bridge", "BridgePart",
        "Tunnel", "TunnelPart",
        "Road", "Railway", "Track", "Square",
        "CityFurniture",
        "WaterBody", "WaterBoundarySurface",
        "PlantCover", "SolitaryVegetationObject",
        "LandUse",
        "GenericCityObject",
        "ReliefFeature", "TINRelief", "MassPointRelief", "BreaklineRelief",
        "CityObjectGroup",
    }
    
    try:
        for elem in root.iter():
            tag = getattr(elem, "tag", None)
            if not isinstance(tag, str):
                continue
            
            ln = _localname(tag)
            
            # Check if this is a known feature type
            if ln in known_features:
                feature_types[ln] = feature_types.get(ln, 0) + 1
    
    except Exception as e:
        print(f"[Import Scanner] Feature counting error: {e}")
    
    total = sum(feature_types.values())
    return total, feature_types


def _detect_lod_levels(root) -> dict:
    """
    Detect LOD level distribution by scanning lodXGeometry elements.
    
    Returns: {0: count, 1: count, 2: count, 3: count, 4: count}
    """
    lod_dist = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    
    lod_tags = {
        "lod0Geometry": 0, "lod0TerrainIntersection": 0, "lod0FootPrint": 0, "lod0RoofEdge": 0,
        "lod1Geometry": 1, "lod1Solid": 1, "lod1MultiSurface": 1, "lod1TerrainIntersection": 1,
        "lod2Geometry": 2, "lod2Solid": 2, "lod2MultiSurface": 2, "lod2MultiCurve": 2,
        "lod3Geometry": 3, "lod3Solid": 3, "lod3MultiSurface": 3, "lod3MultiCurve": 3,
        "lod4Geometry": 4, "lod4Solid": 4, "lod4MultiSurface": 4, "lod4MultiCurve": 4,
        
        # CityGML 3.0 style (lod0ImplicitRepresentation, etc.)
        "lod0ImplicitRepresentation": 0,
        "lod1ImplicitRepresentation": 1,
        "lod2ImplicitRepresentation": 2,
        "lod3ImplicitRepresentation": 3,
        "lod4ImplicitRepresentation": 4,
    }
    
    try:
        for elem in root.iter():
            tag = getattr(elem, "tag", None)
            if not isinstance(tag, str):
                continue
            
            ln = _localname(tag)
            
            if ln in lod_tags:
                lod_level = lod_tags[ln]
                lod_dist[lod_level] += 1
    
    except Exception as e:
        print(f"[Import Scanner] LOD detection error: {e}")
    
    return lod_dist


def _estimate_import_memory(file_size_mb: float, feature_count: int) -> float:
    """
    Estimate memory usage for import.
    
    Heuristic:
    - Base: file_size * 10 (XML parsing + DOM)
    - Features: feature_count * 0.5 MB (geometry + materials)
    - Minimum: 50 MB
    """
    base_memory = file_size_mb * 10
    feature_memory = feature_count * 0.5
    total = max(50.0, base_memory + feature_memory)
    return round(total, 1)


def scan_citygml_file(filepath: str) -> CityGMLFileInfo:
    """
    Scan CityGML file and extract metadata without full import.
    
    Args:
        filepath: Path to CityGML file
        
    Returns:
        CityGMLFileInfo object with scan results
    """
    info = CityGMLFileInfo()
    info.filepath = filepath
    
    start_time = time.time()
    
    try:
        path = Path(filepath)
        
        # File size
        if not path.exists():
            info.error = "File not found"
            return info
        
        info.file_size_mb = round(path.stat().st_size / (1024 * 1024), 2)
        
        # Parse XML (streaming for large files)
        try:
            # Try lxml first (faster)
            tree = ET.parse(str(path))
            root = tree.getroot()
        except Exception:
            # Fallback to standard library
            import xml.etree.ElementTree as StdET
            tree = StdET.parse(str(path))
            root = tree.getroot()
        
        # Extract metadata
        info.citygml_version = _detect_citygml_version(root)
        info.crs = _extract_crs(root)
        info.bounding_box = _extract_bounding_box(root)
        
        total_features, feature_types = _count_features(root)
        info.total_features = total_features
        info.feature_types = feature_types
        
        info.lod_distribution = _detect_lod_levels(root)
        
        # Memory estimate
        info.estimated_import_memory_mb = _estimate_import_memory(
            info.file_size_mb,
            info.total_features
        )
        
        info.scan_time_seconds = round(time.time() - start_time, 2)
        
    except Exception as e:
        info.error = str(e)
        print(f"[Import Scanner] Error scanning {filepath}: {e}")
    
    return info


class CGML3_OT_ScanImportFile(Operator):
    """Scan CityGML file and show preview information"""
    bl_idname = "cgml3.scan_import_file"
    bl_label = "Scan CityGML File"
    bl_options = {'REGISTER'}
    bl_description = "Scan CityGML file to preview metadata before import"
    
    filepath: StringProperty(
        name="CityGML File",
        subtype='FILE_PATH'
    )
    
    def execute(self, context):
        if not self.filepath:
            self.report({'ERROR'}, "No file selected")
            return {'CANCELLED'}
        
        # Run scan
        info = scan_citygml_file(self.filepath)
        
        if info.error:
            self.report({'ERROR'}, f"Scan failed: {info.error}")
            return {'CANCELLED'}
        
        # Store results in scene properties
        scene = context.scene
        
        # Create or update scan results
        if not hasattr(scene, "cgml3_scan_results"):
            # Store as custom properties
            scene["cgml3_scan_filepath"] = info.filepath
            scene["cgml3_scan_filesize"] = info.file_size_mb
            scene["cgml3_scan_version"] = info.citygml_version
            scene["cgml3_scan_crs"] = info.crs
            scene["cgml3_scan_features"] = info.total_features
            scene["cgml3_scan_memory_est"] = info.estimated_import_memory_mb
            scene["cgml3_scan_time"] = info.scan_time_seconds
            
            # Feature types (JSON string)
            import json
            scene["cgml3_scan_feature_types"] = json.dumps(info.feature_types)
            
            # LOD distribution
            scene["cgml3_scan_lod0"] = info.lod_distribution[0]
            scene["cgml3_scan_lod1"] = info.lod_distribution[1]
            scene["cgml3_scan_lod2"] = info.lod_distribution[2]
            scene["cgml3_scan_lod3"] = info.lod_distribution[3]
            scene["cgml3_scan_lod4"] = info.lod_distribution[4]
            
            # Bounding box
            if info.bounding_box:
                scene["cgml3_scan_bbox_min_x"] = info.bounding_box[0]
                scene["cgml3_scan_bbox_min_y"] = info.bounding_box[1]
                scene["cgml3_scan_bbox_min_z"] = info.bounding_box[2]
                scene["cgml3_scan_bbox_max_x"] = info.bounding_box[3]
                scene["cgml3_scan_bbox_max_y"] = info.bounding_box[4]
                scene["cgml3_scan_bbox_max_z"] = info.bounding_box[5]
        
        # Report summary
        self.report({'INFO'}, 
            f"Scanned: {info.total_features} features, "
            f"CityGML {info.citygml_version}, "
            f"{info.crs}, "
            f"~{info.estimated_import_memory_mb} MB RAM"
        )
        
        return {'FINISHED'}


def register():
    bpy.utils.register_class(CGML3_OT_ScanImportFile)


def unregister():
    bpy.utils.unregister_class(CGML3_OT_ScanImportFile)
