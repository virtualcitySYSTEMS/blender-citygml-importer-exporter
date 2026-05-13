# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Classification Algorithms for CityGML Surface Types
Enthält die Logik für automatische Surface-Type-Klassifikation
"""

import math
from mathutils import Vector
from . import geometry as geom


# =========================
# Basisklassifikation
# =========================
def classify_face_building_basic(f, center_z, z_min):
    """Basic classification for building faces"""
    if f.normal.length == 0: return "WallSurface"
    n = f.normal.normalized()
    theta = math.degrees(n.angle(Vector((0,0,1))))

    if center_z < z_min + 0.3 and geom.face_is_horizontal(n) and n.z > 0.9:
        return "GroundSurface"

    if (n.z >= geom.UPWARD_MIN_NZ) and (geom.MIN_ROOF_SLOPE_DEG <= theta <= geom.ROOF_MAX_SLOPE_DEG):
        return "RoofSurface"

    if geom.face_is_vertical(n):
        return "WallSurface"

    return "RoofSurface" if n.z > 0 else "WallSurface"


def classify_face_bridge_basic(f, center_z, z_min):
    """Basic classification for bridge faces"""
    if f.normal.length == 0: return "WallSurface"
    n = f.normal.normalized()
    theta = math.degrees(n.angle(Vector((0,0,1))))

    # Ground: waagerecht, unten
    if center_z <= z_min + 0.05 and geom.face_is_horizontal(n) and n.z >= 0.0:
        return "GroundSurface"

    # Strict floors/ceilings: horizontal entscheidet
    if geom.face_is_horizontal(n) and n.z > 0.0:
        return "OuterFloorSurface"
    if geom.face_is_horizontal(n) and n.z < 0.0:
        return "OuterCeilingSurface"

    # Roof
    if (n.z >= geom.UPWARD_MIN_NZ) and (geom.MIN_ROOF_SLOPE_DEG <= theta <= geom.ROOF_MAX_SLOPE_DEG):
        return "RoofSurface"

    # Wall
    if geom.face_is_vertical(n):
        return "WallSurface"

    return "WallSurface"


# =========================
# Label-Constraints erzwingen
# =========================
def enforce_constraints_building(bm, labels):
    """Enforce geometric constraints for building labels"""
    # Roof niemals nach unten; Wall niemals waagerecht
    for f in bm.faces:
        i = f.index
        n = f.normal.normalized()
        if n.length_squared < 1e-12:
            continue
        if labels[i] == "RoofSurface" and n.z <= 0:
            labels[i] = "WallSurface"
        if labels[i] == "WallSurface" and geom.face_is_horizontal(n):
            # bevorzugt Ground wenn unten, sonst Roof wenn aufwärts
            c = f.calc_center_median()
            mins, maxs = geom.bm_bounds_local(bm)
            if c.z < mins.z + 0.05*(maxs.z-mins.z) and n.z > 0:
                labels[i] = "GroundSurface"
            elif n.z > 0 and math.degrees(n.angle(Vector((0,0,1)))) <= geom.ROOF_MAX_SLOPE_DEG:
                labels[i] = "RoofSurface"
    return labels


def enforce_constraints_bridge(bm, labels, top_hits, ofloor_lock=None):
    """Enforce geometric constraints for bridge labels"""
    if ofloor_lock is None:
        ofloor_lock = set()
    mins, maxs = geom.bm_bounds_local(bm)
    z_span = max(1e-6, (maxs.z - mins.z))
    top_cut = maxs.z - geom.OFLOOR_TOP_MARGIN * z_span

    for f in bm.faces:
        i = f.index
        n = f.normal.normalized()
        if n.length_squared < 1e-12:
            continue
        c = f.calc_center_median()

        # Floors/Ceilings mit Paar: nie überschreiben
        if i in ofloor_lock and labels[i] != "OuterFloorSurface":
            labels[i] = "OuterFloorSurface"
            continue

        # Roof nie nach unten
        if labels[i] == "RoofSurface" and n.z <= 0:
            labels[i] = "WallSurface"

        # Wall nie waagerecht
        if labels[i] == "WallSurface" and geom.face_is_horizontal(n):
            labels[i] = "OuterFloorSurface" if n.z > 0 else "OuterCeilingSurface"

        # OuterFloor nur dann am +Z-Ende verbieten, wenn NICHT gelockt
        if labels[i] == "OuterFloorSurface" and i not in ofloor_lock:
            if (i in top_hits) or (c.z >= top_cut):
                theta = math.degrees(n.angle(Vector((0,0,1))))
                if (n.z >= geom.UPWARD_MIN_NZ) and (geom.MIN_ROOF_SLOPE_DEG <= theta <= geom.ROOF_MAX_SLOPE_DEG):
                    labels[i] = "RoofSurface"

        # Ground immer waagerecht
        if labels[i] == "GroundSurface" and not geom.face_is_horizontal(n):
            labels[i] = "WallSurface"

        # Walls streng vertikal
        if labels[i] == "WallSurface" and not geom.face_is_vertical(n):
            if geom.face_is_horizontal(n):
                labels[i] = "OuterFloorSurface" if n.z > 0 else "OuterCeilingSurface"
            else:
                theta = math.degrees(n.angle(Vector((0,0,1))))
                if (n.z >= geom.UPWARD_MIN_NZ) and (geom.MIN_ROOF_SLOPE_DEG <= theta <= geom.ROOF_MAX_SLOPE_DEG):
                    labels[i] = "RoofSurface"

    return labels


# =========================
# Ensemble-Methode: Building
# =========================
def ensemble_labels_building(bm):
    """
    Ensemble classification for building surfaces
    Combines raycast, parallel pairs, region growing, and constraints
    """
    mins, maxs = geom.bm_bounds_local(bm)
    z_min, z_max = mins.z, maxs.z
    
    # M1: Raycast-Envelopes
    step = geom.estimate_grid_step(bm)
    top_hits, bot_hits = geom.raycast_envelopes(bm, step)
    
    # Ground Detection
    ground_lock = geom.detect_ground_baseplates_multi(bm)
    
    # Initial classification
    labels = [None]*len(bm.faces)
    for f in bm.faces:
        i = f.index
        c = f.calc_center_median()
        if i in ground_lock:
            labels[i] = "GroundSurface"
        else:
            labels[i] = classify_face_building_basic(f, c.z, z_min)
    
    # Refine ground
    labels = geom.refine_ground_faces(bm, labels, bot_hits)
    
    # M3: Region-growing for roofs
    seed_mask = [False]*len(bm.faces)
    for f in bm.faces:
        i = f.index
        if labels[i] == "GroundSurface":
            continue
        n = f.normal.normalized()
        if n.length_squared < 1e-12:
            continue
        th = math.degrees(n.angle(Vector((0,0,1))))
        if (i in top_hits) and (n.z >= geom.UPWARD_MIN_NZ) and (geom.MIN_ROOF_SLOPE_DEG <= th <= geom.ROOF_MAX_SLOPE_DEG):
            seed_mask[i] = True
    
    grown = geom.grow_roof_regions(bm, seed_mask)
    for i in grown:
        if labels[i] != "GroundSurface":
            labels[i] = "RoofSurface"
    
    # Coplanar unification & constraints
    labels = geom.unify_coplanar_groups(bm, labels)
    labels = enforce_constraints_building(bm, labels)
    
    # Lock ground faces
    for i in ground_lock:
        labels[i] = "GroundSurface"
    
    labels = geom.unify_coplanar_groups(bm, labels)
    
    return labels


# =========================
# Ensemble-Methode: Bridge
# =========================
def ensemble_labels_bridge(bm):
    """
    Ensemble classification for bridge surfaces
    Combines raycast, parallel pairs, region growing, and constraints
    """
    mins, maxs = geom.bm_bounds_local(bm)
    z_min, z_max = mins.z, maxs.z
    
    # M1: Raycast-Envelopes
    step = geom.estimate_grid_step(bm)
    top_hits, bot_hits = geom.raycast_envelopes(bm, step)
    
    # M2: Parallel Pairs
    ofloor_set, oceiling_set = geom.detect_parallel_pairs(bm)
    
    # Ground Detection
    ground_lock = geom.detect_ground_baseplates_multi(bm)
    
    # Lock floor/ceiling pairs
    ofloor_lock = ofloor_set.copy()
    oceiling_lock = oceiling_set.copy()
    
    # Initial classification
    labels = [None]*len(bm.faces)
    for f in bm.faces:
        i = f.index
        c = f.calc_center_median()
        
        if i in ground_lock:
            labels[i] = "GroundSurface"
        elif i in ofloor_lock:
            labels[i] = "OuterFloorSurface"
        elif i in oceiling_lock:
            labels[i] = "OuterCeilingSurface"
        else:
            labels[i] = classify_face_bridge_basic(f, c.z, z_min)
    
    # Refine ground
    labels = geom.refine_ground_faces(bm, labels, bot_hits)
    
    # M3: Region-growing for roofs
    seed_mask = [False]*len(bm.faces)
    for f in bm.faces:
        i = f.index
        if labels[i] in ("OuterFloorSurface","OuterCeilingSurface","GroundSurface"):
            continue
        n = f.normal.normalized()
        if n.length_squared < 1e-12:
            continue
        th = math.degrees(n.angle(Vector((0,0,1))))
        if (i in top_hits) and (n.z >= geom.UPWARD_MIN_NZ) and (geom.MIN_ROOF_SLOPE_DEG <= th <= geom.ROOF_MAX_SLOPE_DEG):
            seed_mask[i] = True
    
    grown = geom.grow_roof_regions(bm, seed_mask)
    for i in grown:
        if labels[i] not in ("OuterFloorSurface","OuterCeilingSurface","GroundSurface"):
            labels[i] = "RoofSurface"
    
    # Coplanar and Constraints
    labels = geom.unify_coplanar_groups(bm, labels)
    labels = enforce_constraints_bridge(bm, labels, top_hits, ofloor_lock)
    
    # Locks erneut fixieren
    for i in ground_lock:   labels[i] = "GroundSurface"
    for i in ofloor_lock:   labels[i] = "OuterFloorSurface"
    for i in oceiling_lock: labels[i] = "OuterCeilingSurface"
    
    labels = geom.unify_coplanar_groups(bm, labels)
    
    return labels


# =========================
# Tunnel Classification
# =========================
def ensemble_labels_tunnel(bm):
    """
    Ensemble classification for tunnels
    Similar to buildings but with closure surface detection at entrances
    """
    if len(bm.faces) == 0:
        return []
    
    mins, maxs = geom.bm_bounds_local(bm)
    z_min = mins.z
    
    # M1: Raycast-Envelopes
    step = geom.estimate_grid_step(bm)
    top_hits, bot_hits = geom.raycast_envelopes(bm, step)
    
    # Ground Detection
    ground_lock = geom.detect_ground_baseplates_multi(bm)
    
    # Initial classification
    labels = [None]*len(bm.faces)
    for f in bm.faces:
        i = f.index
        c = f.calc_center_median()
        
        if i in ground_lock:
            labels[i] = "GroundSurface"
        else:
            labels[i] = classify_face_building_basic(f, c.z, z_min)
    
    # Detect closure surfaces (tunnel entrances/exits)
    # Vertical faces at the boundary (min/max X or Y) are likely ClosureSurfaces
    x_min, x_max = mins.x, maxs.x
    y_min, y_max = mins.y, maxs.y
    x_range = x_max - x_min
    y_range = y_max - y_min
    threshold = 0.1  # 10% from edge
    
    for f in bm.faces:
        i = f.index
        n = f.normal.normalized()
        c = f.calc_center_median()
        
        # Check if vertical face at boundary
        if geom.face_is_vertical(n):
            near_x_min = (c.x - x_min) < (threshold * x_range)
            near_x_max = (x_max - c.x) < (threshold * x_range)
            near_y_min = (c.y - y_min) < (threshold * y_range)
            near_y_max = (y_max - c.y) < (threshold * y_range)
            
            if near_x_min or near_x_max or near_y_min or near_y_max:
                labels[i] = "ClosureSurface"
    
    # Refine ground
    labels = geom.refine_ground_faces(bm, labels, bot_hits)
    
    # M3: Region-growing for roofs
    seed_mask = [False]*len(bm.faces)
    for f in bm.faces:
        i = f.index
        if labels[i] in ("GroundSurface", "ClosureSurface"):
            continue
        n = f.normal.normalized()
        if n.length_squared < 1e-12:
            continue
        th = math.degrees(n.angle(Vector((0,0,1))))
        if (i in top_hits) and (n.z >= geom.UPWARD_MIN_NZ) and (geom.MIN_ROOF_SLOPE_DEG <= th <= geom.ROOF_MAX_SLOPE_DEG):
            seed_mask[i] = True
    
    grown = geom.grow_roof_regions(bm, seed_mask)
    for i in grown:
        if labels[i] not in ("GroundSurface", "ClosureSurface"):
            labels[i] = "RoofSurface"
    
    # Coplanar and Constraints
    labels = geom.unify_coplanar_groups(bm, labels)
    labels = enforce_constraints_building(bm, labels)
    
    # Restore locks
    for i in ground_lock:
        labels[i] = "GroundSurface"
    
    labels = geom.unify_coplanar_groups(bm, labels)
    
    return labels


# =========================
# WaterBody Classification
# =========================
def classify_waterbody(bm):
    """
    Enhanced classification for water bodies
    Detects flowing vs. standing water based on geometry
    """
    if len(bm.faces) == 0:
        return []
    
    mins, maxs = geom.bm_bounds_local(bm)
    z_range = maxs.z - mins.z
    
    labels = []
    
    # Analyze overall geometry to detect flow direction
    all_verts = [v.co for v in bm.verts]
    avg_z = sum(v.z for v in all_verts) / len(all_verts)
    
    # Check for significant elevation change (indicates flowing water)
    has_elevation_change = z_range > 0.5
    
    for f in bm.faces:
        normal = f.normal.normalized()
        center = f.calc_center_median()
        
        # Horizontal surfaces
        if abs(normal.z) > 0.9:
            if normal.z > 0:
                # Top surface
                labels.append("WaterSurface")
            else:
                # Bottom surface
                labels.append("WaterGroundSurface")
        else:
            # Non-horizontal surfaces
            if has_elevation_change and geom.face_is_vertical(normal):
                # Vertical surfaces in flowing water might be closure surfaces
                # at dams, locks, or boundaries between water bodies
                labels.append("WaterClosureSurface")
            else:
                # Sloped bottom (terrain following)
                if center.z < avg_z:
                    labels.append("WaterGroundSurface")
                else:
                    labels.append("WaterSurface")
    
    return labels


# =========================
# Transportation Classification
# =========================
def classify_transportation(bm):
    """
    Classification for transportation features (roads, railways)
    Mainly horizontal surfaces with traffic distinction
    """
    if len(bm.faces) == 0:
        return []
    
    labels = []
    
    # Calculate average area to distinguish main traffic from auxiliary
    areas = [f.calc_area() for f in bm.faces]
    avg_area = sum(areas) / len(areas) if areas else 0
    
    for f in bm.faces:
        normal = f.normal.normalized()
        area = f.calc_area()
        
        # Horizontal surfaces are traffic surfaces
        if abs(normal.z) > 0.9 and normal.z > 0:
            # Larger faces are main traffic, smaller are auxiliary (sidewalks)
            if area > avg_area * 0.8:
                labels.append("TrafficArea")
            else:
                labels.append("AuxiliaryTrafficArea")
        else:
            # Non-horizontal or downward facing
            labels.append("AuxiliaryTrafficArea")
    
    return labels


# =========================
# Validation Functions
# =========================
def validate_surface_types(feature_type, labels):
    """
    Validate that surface types are appropriate for the feature type
    
    Args:
        feature_type: Feature type (e.g., "BUILDING", "BRIDGE", etc.)
        labels: List of surface type labels
    
    Returns:
        Tuple of (is_valid, error_messages)
    """
    from ..citygml import get_surface_types
    
    valid_surfaces = set(get_surface_types(feature_type, "2.0").keys())
    errors = []
    
    for i, label in enumerate(labels):
        if label not in valid_surfaces:
            errors.append(f"Face {i}: '{label}' is not valid for {feature_type}")
    
    return (len(errors) == 0, errors)


def get_surface_type_counts(labels):
    """
    Count occurrences of each surface type
    
    Returns:
        Dictionary with counts
    """
    counts = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return counts


def suggest_feature_type(bm):
    """
    Analyze geometry and suggest appropriate feature type
    
    Returns:
        Suggested feature type identifier
    """
    if len(bm.faces) == 0:
        return "BUILDING"
    
    # Analyze normals
    horizontal_count = 0
    vertical_count = 0
    
    for f in bm.faces:
        n = f.normal.normalized()
        if abs(n.z) > 0.9:
            horizontal_count += 1
        elif geom.face_is_vertical(n):
            vertical_count += 1
    
    total = len(bm.faces)
    horizontal_ratio = horizontal_count / total
    
    # Mostly horizontal: likely transportation or waterbody
    if horizontal_ratio > 0.8:
        # Check if flat (transportation) or has depth (waterbody)
        mins, maxs = geom.bm_bounds_local(bm)
        z_range = maxs.z - mins.z
        xy_range = max(maxs.x - mins.x, maxs.y - mins.y)
        
        if z_range < xy_range * 0.1:
            return "TRANSPORTATION"
        else:
            return "WATERBODY"
    
    # Mixed geometry: likely building, bridge, or tunnel
    # Default to building
    return "BUILDING"


def detect_feature_type_from_labels(labels):
    """
    Detect feature type from assigned surface type labels
    
    Args:
        labels: List of surface type labels
    
    Returns:
        Detected feature type identifier or None
    """
    if not labels:
        return None
    
    # Count unique surface types
    unique_labels = set(labels)
    
    # Check for WaterBody specific types
    waterbody_types = {"WaterSurface", "WaterGroundSurface", "WaterClosureSurface"}
    if unique_labels & waterbody_types:
        return "WATERBODY"
    
    # Check for Transportation specific types
    transport_types = {"TrafficArea", "AuxiliaryTrafficArea", "Marking"}
    if unique_labels & transport_types:
        return "TRANSPORTATION"
    
    # Check for CityFurniture, Vegetation, LandUse specific types
    if "CityFurniture" in unique_labels:
        return "CITYFURNITURE"
    
    vegetation_types = {"VegetationObject", "PlantCover", "SolitaryVegetationObject"}
    if unique_labels & vegetation_types:
        return "VEGETATION"
    
    if "LandUse" in unique_labels:
        return "LANDUSE"
    
    # GenericSurface is a fallback - cannot determine specific type
    if unique_labels == {"GenericSurface"}:
        return None  # Cannot determine feature type from generic surface
    
    # Check for OuterFloorSurface/OuterCeilingSurface (Bridge specific)
    bridge_specific = {"OuterFloorSurface", "OuterCeilingSurface"}
    if unique_labels & bridge_specific:
        # Could be Bridge or Tunnel, check for other indicators
        if "ClosureSurface" in unique_labels:
            # Both have ClosureSurface, but Tunnel more likely if no OuterFloor
            tunnel_indicators = {"GroundSurface", "WallSurface", "RoofSurface", "ClosureSurface"}
            bridge_indicators = {"OuterFloorSurface", "OuterCeilingSurface", "GroundSurface"}
            
            tunnel_score = len(tunnel_indicators & unique_labels)
            bridge_score = len(bridge_indicators & unique_labels)
            
            if bridge_score >= tunnel_score:
                return "BRIDGE"
            else:
                return "TUNNEL"
        else:
            return "BRIDGE"
    
    # Check for basic construction types (Building/Tunnel)
    basic_types = {"GroundSurface", "WallSurface", "RoofSurface"}
    if unique_labels & basic_types:
        # Has ClosureSurface? More likely Tunnel
        if "ClosureSurface" in unique_labels:
            return "TUNNEL"
        else:
            return "BUILDING"
    
    # Default to Building
    return "BUILDING"


def classify_bridge_complex_type(obj, bm):
    """
    Classify a bridge object into CityGML ComplexTypes based on geometry analysis
    
    Based on CityGML 2.0 Bridge Schema documentation:
    - AbstractBridge/Bridge: Main bridge structure
    - BridgeInstallation: Non-structural elements (stairs, antennas, railways) - removable without collapse
    - BridgeConstructionElement: Structural elements (pylons, anchorages) - essential for stability
    - IntBridgeInstallation: Interior installations (interior stairs, railings, radiators, pipes)
    - BridgeRoom: Enclosed interior spaces (LOD4)
    - BridgeFurniture: Interior furniture (LOD4)
    
    Args:
        obj: Blender mesh object
        bm: BMesh of the object
    
    Returns:
        str: CityGML ComplexType name or None for main bridge structure
    """
    import mathutils
    
    # Calculate object metrics
    bounds = geom.bm_bounds_local(bm)
    if not bounds:
        return None
    
    min_co, max_co = bounds
    bbox_size = max_co - min_co
    bbox_volume = bbox_size.x * bbox_size.y * bbox_size.z
    
    # Calculate mesh volume (approximate using bounding box and face count)
    face_count = len(bm.faces)
    total_area = sum(f.calc_area() for f in bm.faces)
    
    # Calculate aspect ratios
    width = max(bbox_size.x, bbox_size.y)
    height = bbox_size.z
    depth = min(bbox_size.x, bbox_size.y)
    
    aspect_h = height / width if width > 0.01 else 0
    aspect_d = depth / width if width > 0.01 else 0
    
    # Calculate verticality (how many faces are vertical vs horizontal)
    vertical_faces = 0
    horizontal_faces = 0
    up_vector = mathutils.Vector((0, 0, 1))
    
    for face in bm.faces:
        alignment = abs(face.normal.dot(up_vector))
        if alignment > 0.9:  # Nearly horizontal
            horizontal_faces += 1
        elif alignment < 0.2:  # Nearly vertical
            vertical_faces += 1
    
    verticality_ratio = vertical_faces / face_count if face_count > 0 else 0
    horizontality_ratio = horizontal_faces / face_count if face_count > 0 else 0
    
    # Calculate position relative to scene (if possible)
    bbox_center = (min_co + max_co) / 2
    z_position = bbox_center.z
    
    # Debug output
    print(f"  ComplexType Debug for {obj.name}:")
    print(f"    Volume: {bbox_volume:.2f} m³, Area: {total_area:.2f} m²")
    print(f"    Size: {bbox_size.x:.2f} x {bbox_size.y:.2f} x {bbox_size.z:.2f}")
    print(f"    Aspect H: {aspect_h:.2f}, Aspect D: {aspect_d:.2f}")
    print(f"    Verticality: {verticality_ratio:.2f}, Horizontality: {horizontality_ratio:.2f}")
    
    # Decision tree based on CityGML semantic classification
    # Key distinction: BridgeConstructionElement = STRUCTURAL (load-bearing)
    #                  BridgeInstallation = NON-STRUCTURAL (removable)
    
    # 1. BridgeConstructionElement: Structural load-bearing components
    #    - Pylons: Tall vertical structures supporting the bridge
    #    - Anchorages: Massive horizontal/sloped foundations
    #    - Structural beams: Large elongated load-bearing elements
    # XSD: "essential from a structural point of view"
    is_pylon = aspect_h > 2.5 and verticality_ratio > 0.6 and bbox_volume > 3.0
    is_foundation = bbox_volume > 10.0 and horizontality_ratio > 0.4 and bbox_size.z < bbox_size.x * 0.5
    is_beam = aspect_d < 0.2 and bbox_volume > 2.0 and max(bbox_size.x, bbox_size.y) > 5.0
    
    if is_pylon or is_foundation or is_beam:
        print(f"    → BridgeConstructionElement (structural element)")
        return "BridgeConstructionElement"
    
    # 2. BridgeFurniture: Interior furniture (LOD4 only, very small)
    #    Must be checked before BridgeInstallation to capture smallest objects first
    if bbox_volume < 0.5 and total_area < 3.0:
        print(f"    → BridgeFurniture")
        return "BridgeFurniture"
    
    # 3. IntBridgeInstallation: Interior non-structural features
    #    - Interior stairs, railings, pipes, radiators
    #    - Very small to small volume
    # XSD: "interior part...with specific function" (LOD4)
    if bbox_volume < 3.0 and total_area < 15.0:
        # Could be interior installation
        print(f"    → IntBridgeInstallation")
        return "IntBridgeInstallation"
    
    # 4. BridgeRoom: Enclosed interior spaces (LOD4)
    #    - Closed box-like structures
    #    - Mix of vertical walls and horizontal floor/ceiling
    # XSD: "closed parts inside a Bridge"
    try:
        if bm.is_valid and face_count > 6:
            # Room-like: balanced vertical/horizontal, moderate size, box-shaped
            is_room_like = (
                verticality_ratio > 0.25 and horizontality_ratio > 0.2 and
                bbox_volume > 5.0 and bbox_volume < 200.0 and
                aspect_h < 2.0 and aspect_d > 0.25
            )
            if is_room_like:
                print(f"    → BridgeRoom")
                return "BridgeRoom"
    except:
        pass
    
    # 5. BridgeInstallation: Non-structural exterior features
    #    - Stairs, antennas, railways, lighting, signage
    #    - NOT essential for structural integrity
    #    - Can be various sizes (small to medium)
    #    - Often linear or attached to main structure
    # XSD: "not essential from a structural point of view...may be removed without the bridge collapsing"
    is_linear = aspect_d < 0.3  # Thin/linear (railings, cables, stairs)
    is_small_to_medium = bbox_volume < 15.0  # Smaller than structural elements
    is_attached = aspect_h < 2.0 and verticality_ratio < 0.7  # Not purely vertical like pylons
    
    if is_linear or (is_small_to_medium and is_attached):
        print(f"    → BridgeInstallation (non-structural element)")
        return "BridgeInstallation"
    
    # 6. Default: Main Bridge/BridgePart structure (AbstractBridge)
    #    Large structures that don't fit other categories
    print(f"    → AbstractBridge (main bridge structure)")
    return None  # None indicates main bridge structure (AbstractBridge)
