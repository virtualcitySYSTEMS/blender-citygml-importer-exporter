# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
PointCloud Reader Module for CityGML 3.0

Handles parsing of pcl:PointCloud features with support for:
- Inline gml:MultiPoint geometries (pcl:points)
- External point cloud files (pcl:pointFile) - Binary/ASCII formats
- Point attributes (RGB, Intensity, Classification)
- Blender Point Cloud objects (Blender 3.0+)

Supported external formats:
- LAS/LAZ (binary)
- PLY (ASCII/binary)
- XYZ/PTS (ASCII)
- E57 (if laspy/plyfile libraries available)
"""

import bpy
from mathutils import Vector
from pathlib import Path
import struct
import os

try:
    from lxml import etree as ET
except Exception:
    import xml.etree.ElementTree as ET

from .namespaces import NS


def parse_pointcloud_inline_points(pointcloud_element):
    """
    Parse inline gml:MultiPoint from pcl:points element.
    Returns list of (x, y, z) tuples.
    """
    points = []
    
    # Find pcl:points/gml:MultiPoint
    multipoint_el = pointcloud_element.find(".//pcl:points/gml:MultiPoint", namespaces=NS)
    if multipoint_el is None:
        # NOTE: avoid noisy debug prints in normal operation
        return points
    
    # Parse gml:pointMembers (note: singular container, multiple Points inside)
    pointmembers_el = multipoint_el.find("gml:pointMembers", namespaces=NS)
    if pointmembers_el is not None:
        for point_el in pointmembers_el.findall("gml:Point", namespaces=NS):
            pos_text = point_el.findtext("gml:pos", namespaces=NS)
            if pos_text:
                coords = [float(x) for x in pos_text.strip().split()]
                if len(coords) >= 3:
                    points.append((coords[0], coords[1], coords[2]))
                elif len(coords) == 2:
                    points.append((coords[0], coords[1], 0.0))
    
    # Also try gml:pointMember (plural usage)
    for point_member in multipoint_el.findall("gml:pointMember", namespaces=NS):
        pos_text = point_member.findtext(".//gml:pos", namespaces=NS)
        if pos_text:
            coords = [float(x) for x in pos_text.strip().split()]
            if len(coords) >= 3:
                points.append((coords[0], coords[1], coords[2]))
            elif len(coords) == 2:
                points.append((coords[0], coords[1], 0.0))
    
    # Alternatively: gml:posList (more efficient for large point clouds)
    poslist = multipoint_el.findtext(".//gml:posList", namespaces=NS)
    if poslist:
        coords = [float(x) for x in poslist.strip().split()]
        # Group by 3 (x, y, z)
        for i in range(0, len(coords), 3):
            if i + 2 < len(coords):
                points.append((coords[i], coords[i+1], coords[i+2]))
    
    # NOTE: avoid noisy debug prints in normal operation
    return points


def parse_pointcloud_metadata(pointcloud_element):
    """
    Extract PointCloud metadata from pcl:PointCloud element.
    
    Returns dict with:
    - mimeType: MIME type of external file
    - pointFile: URI to external file
    - pointFileSrsName: CRS of external file
    - multipoint_id: gml:id of inline MultiPoint
    """
    metadata = {}
    
    # MIME type
    mime = pointcloud_element.findtext(".//pcl:mimeType", namespaces=NS)
    if mime:
        metadata["mimeType"] = mime.strip()
    
    # External file URI
    point_file = pointcloud_element.findtext(".//pcl:pointFile", namespaces=NS)
    if point_file:
        metadata["pointFile"] = point_file.strip()
    
    # CRS of external file
    srs = pointcloud_element.findtext(".//pcl:pointFileSrsName", namespaces=NS)
    if srs:
        metadata["pointFileSrsName"] = srs.strip()
    
    # Inline MultiPoint gml:id
    multipoint_el = pointcloud_element.find(".//pcl:points/gml:MultiPoint", namespaces=NS)
    if multipoint_el is not None:
        mp_id = multipoint_el.get("{http://www.opengis.net/gml/3.2}id")
        if mp_id:
            metadata["multipoint_id"] = mp_id
    
    return metadata


def load_external_pointcloud(file_path, mime_type=None):
    """
    Load points from external file (LAS, PLY, XYZ, PTS, etc.).
    
    Returns:
    - points: list of (x, y, z) tuples
    - attributes: dict with 'colors' (RGB), 'intensity', 'classification' lists
    """
    points = []
    attributes = {
        "colors": [],      # RGB tuples (0-255 or 0.0-1.0)
        "intensity": [],   # Float values
        "classification": []  # Integer codes
    }
    
    if not file_path or not os.path.exists(file_path):
        print(f"PointCloud file not found: {file_path}")
        return points, attributes
    
    file_ext = Path(file_path).suffix.lower()
    
    # LAS/LAZ format (binary)
    if file_ext in ['.las', '.laz']:
        try:
            import laspy
            las = laspy.read(file_path)
            
            # Coordinates
            points = list(zip(las.x, las.y, las.z))
            
            # RGB colors (if available)
            if hasattr(las, 'red') and hasattr(las, 'green') and hasattr(las, 'blue'):
                # LAS RGB is typically 0-65535, normalize to 0-255
                attributes["colors"] = [
                    (int(r/256), int(g/256), int(b/256))
                    for r, g, b in zip(las.red, las.green, las.blue)
                ]
            
            # Intensity
            if hasattr(las, 'intensity'):
                attributes["intensity"] = las.intensity.tolist()
            
            # Classification
            if hasattr(las, 'classification'):
                attributes["classification"] = las.classification.tolist()
            
            print(f"Loaded {len(points)} points from LAS file: {file_path}")
            
        except ImportError:
            print("laspy library not installed. Install with: pip install laspy")
        except Exception as e:
            print(f"Error loading LAS file: {e}")
    
    # PLY format (ASCII or binary)
    elif file_ext == '.ply':
        try:
            import plyfile
            ply_data = plyfile.PlyData.read(file_path)
            vertex = ply_data['vertex']
            
            # Coordinates
            points = list(zip(vertex['x'], vertex['y'], vertex['z']))
            
            # RGB colors (if available)
            if 'red' in vertex and 'green' in vertex and 'blue' in vertex:
                attributes["colors"] = [
                    (int(r), int(g), int(b))
                    for r, g, b in zip(vertex['red'], vertex['green'], vertex['blue'])
                ]
            
            # Intensity
            if 'intensity' in vertex:
                attributes["intensity"] = vertex['intensity'].tolist()
            
            # Classification
            if 'classification' in vertex:
                attributes["classification"] = vertex['classification'].tolist()
            
            print(f"Loaded {len(points)} points from PLY file: {file_path}")
            
        except ImportError:
            print("plyfile library not installed. Install with: pip install plyfile")
        except Exception as e:
            print(f"Error loading PLY file: {e}")
    
    # XYZ/PTS format (ASCII: x y z [r g b] [intensity] [classification])
    elif file_ext in ['.xyz', '.pts', '.txt']:
        try:
            with open(file_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    
                    parts = line.split()
                    if len(parts) >= 3:
                        x, y, z = float(parts[0]), float(parts[1]), float(parts[2])
                        points.append((x, y, z))
                        
                        # Optional RGB (columns 4-6)
                        if len(parts) >= 6:
                            r, g, b = int(float(parts[3])), int(float(parts[4])), int(float(parts[5]))
                            attributes["colors"].append((r, g, b))
                        
                        # Optional intensity (column 7)
                        if len(parts) >= 7:
                            attributes["intensity"].append(float(parts[6]))
                        
                        # Optional classification (column 8)
                        if len(parts) >= 8:
                            attributes["classification"].append(int(float(parts[7])))
            
            print(f"Loaded {len(points)} points from ASCII file: {file_path}")
            
        except Exception as e:
            print(f"Error loading ASCII file: {e}")
    
    else:
        print(f"Unsupported point cloud format: {file_ext}")
    
    return points, attributes


def create_blender_pointcloud(name, points, attributes=None, ref_origin=(0, 0, 0)):
    """
    Create Blender Point Cloud object from point list.
    
    Blender 3.0+ supports native point clouds with per-point attributes.
    For older versions, falls back to Mesh with vertices only.
    
    Args:
        name: Object name
        points: List of (x, y, z) tuples
        attributes: Dict with 'colors', 'intensity', 'classification'
        ref_origin: Reference origin for coordinate offset
    
    Returns:
        Blender Object (PointCloud or Mesh)
    """
    if not points:
        print(f"No points to create point cloud: {name}")
        return None
    
    # Check Blender version
    blender_version = bpy.app.version
    supports_pointcloud = blender_version >= (3, 0, 0)
    
    # Blender 4.4+ changed PointCloud API completely - use Mesh fallback for reliability
    # The new API requires geometry nodes or different approach that's not stable yet
    supports_pointcloud = False
    
    if supports_pointcloud:
        # Disabled for now due to API changes in Blender 4.4+
        pass
    
    # Use Mesh with vertices only (no faces) - works in all Blender versions
    if not supports_pointcloud:
        mesh = bpy.data.meshes.new(name)
        
        # Apply coordinates with offset
        vertices = [
            (x - ref_origin[0], y - ref_origin[1], z - ref_origin[2])
            for x, y, z in points
        ]
        
        mesh.from_pydata(vertices, [], [])
        mesh.update()
        
        obj = bpy.data.objects.new(name, mesh)
        
        # Set display mode to show points
        obj.display_type = 'WIRE'  # Show as wireframe/points
        obj.show_wire = True
        obj.show_all_edges = True
        
        # Store attributes as custom properties (limited support)
        if attributes:
            if attributes.get("colors"):
                obj["has_colors"] = True
                obj["color_count"] = len(attributes["colors"])
            if attributes.get("intensity"):
                obj["has_intensity"] = True
            if attributes.get("classification"):
                obj["has_classification"] = True
    
    print(f"Created Mesh-based PointCloud: {name} with {len(points)} points")
    return obj


def resolve_external_file_path(point_file_uri, citygml_file_path):
    """
    Resolve external point cloud file path from URI.
    
    Handles:
    - Relative paths (relative to CityGML file)
    - Absolute paths
    - File:// URIs
    - HTTP/HTTPS URLs (not downloaded, just logged)
    """
    if not point_file_uri:
        return None
    
    uri = point_file_uri.strip()
    
    # Remove file:// prefix
    if uri.startswith("file://"):
        uri = uri[7:]
    
    # Handle HTTP/HTTPS (not supported for download yet)
    if uri.startswith("http://") or uri.startswith("https://"):
        print(f"External HTTP PointCloud files not supported yet: {uri}")
        return None
    
    # Absolute path
    if os.path.isabs(uri):
        return uri if os.path.exists(uri) else None
    
    # Relative path (relative to CityGML file)
    if citygml_file_path:
        citygml_dir = os.path.dirname(citygml_file_path)
        full_path = os.path.normpath(os.path.join(citygml_dir, uri))
        return full_path if os.path.exists(full_path) else None
    
    return None
