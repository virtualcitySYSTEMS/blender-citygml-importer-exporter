# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Shared feature-type parsing utilities for CityGML 2.0 and 3.0 Import.

This module provides common functions for parsing feature-type-specific attributes
that are identical across CityGML versions.
"""


def parse_simple_feature_attributes(feat, ns, namespace, feature_types, attributes):
    """
    Parse simple text-based feature attributes.
    
    Args:
        feat: XML feature element
        ns: Namespace dictionary
        namespace: Namespace prefix (e.g., "brid", "tun", "wtr")
        feature_types: List of feature type names to check (e.g., ["Bridge", "BridgePart"])
        attributes: List of attribute names to parse (e.g., ["class", "function", "usage"])
        
    Returns:
        Dictionary with parsed attributes or empty dict
    """
    feat_ln = feat.tag.split("}")[-1] if isinstance(feat.tag, str) else ""
    
    if feat_ln not in feature_types:
        return {}
    
    result = {}
    for attr in attributes:
        xpath = f".//{namespace}:{attr}"
        value = feat.findtext(xpath, namespaces=ns)
        if value and value.strip():
            result[f"{namespace}:{attr}"] = value.strip()
    
    return result


def parse_boolean_feature_attribute(feat, ns, namespace, feature_types, attribute_name):
    """
    Parse boolean feature attribute.
    
    Args:
        feat: XML feature element
        ns: Namespace dictionary
        namespace: Namespace prefix
        feature_types: List of feature type names to check
        attribute_name: Name of the boolean attribute
        
    Returns:
        Dictionary with parsed boolean or empty dict
    """
    feat_ln = feat.tag.split("}")[-1] if isinstance(feat.tag, str) else ""
    
    if feat_ln not in feature_types:
        return {}
    
    xpath = f".//{namespace}:{attribute_name}"
    value = feat.findtext(xpath, namespaces=ns)
    if value and value.strip():
        return {f"{namespace}:{attribute_name}": value.strip().lower() == "true"}
    
    return {}


def parse_measurement_attributes(feat, ns, namespace, feature_types, measurements):
    """
    Parse measurement attributes with uom.
    
    Args:
        feat: XML feature element
        ns: Namespace dictionary
        namespace: Namespace prefix
        feature_types: List of feature type names to check
        measurements: List of measurement names (e.g., ["height", "trunkDiameter"])
        
    Returns:
        Dictionary with parsed measurements and uom values
    """
    feat_ln = feat.tag.split("}")[-1] if isinstance(feat.tag, str) else ""
    
    if feat_ln not in feature_types:
        return {}
    
    result = {}
    for measurement in measurements:
        xpath = f".//{namespace}:{measurement}"
        el = feat.find(xpath, namespaces=ns)
        if el is not None and el.text and el.text.strip():
            try:
                result[f"{namespace}:{measurement}"] = float(el.text.strip())
                uom = el.get("uom")
                if uom:
                    result[f"{namespace}:{measurement}:uom"] = uom
            except (ValueError, TypeError):
                pass
    
    return result


def parse_bridge_attributes(feat, ns):
    """
    Parse Bridge-specific attributes (CityGML 2.0 and 3.0).
    
    Args:
        feat: XML feature element
        ns: Namespace dictionary
        
    Returns:
        Dictionary with Bridge attributes
    """
    attrs = {}
    
    # Simple attributes
    simple_attrs = parse_simple_feature_attributes(
        feat, ns, "brid", ["Bridge", "BridgePart"],
        ["class", "function", "usage"]
    )
    attrs.update(simple_attrs)
    
    # Boolean attribute
    boolean_attrs = parse_boolean_feature_attribute(
        feat, ns, "brid", ["Bridge", "BridgePart"],
        "isMovable"
    )
    attrs.update(boolean_attrs)
    
    return attrs


def parse_tunnel_attributes(feat, ns):
    """
    Parse Tunnel-specific attributes (CityGML 2.0 and 3.0).
    
    Args:
        feat: XML feature element
        ns: Namespace dictionary
        
    Returns:
        Dictionary with Tunnel attributes
    """
    return parse_simple_feature_attributes(
        feat, ns, "tun", ["Tunnel", "TunnelPart"],
        ["class", "function", "usage"]
    )


def parse_waterbody_attributes(feat, ns):
    """
    Parse WaterBody-specific attributes (CityGML 2.0 and 3.0).
    
    Args:
        feat: XML feature element
        ns: Namespace dictionary
        
    Returns:
        Dictionary with WaterBody attributes
    """
    return parse_simple_feature_attributes(
        feat, ns, "wtr", ["WaterBody", "WaterSurface"],
        ["class", "function", "usage"]
    )


def parse_vegetation_attributes(feat, ns):
    """
    Parse Vegetation-specific attributes (CityGML 2.0 and 3.0).
    
    Args:
        feat: XML feature element
        ns: Namespace dictionary
        
    Returns:
        Dictionary with Vegetation attributes
    """
    attrs = {}
    
    # Simple attributes
    simple_attrs = parse_simple_feature_attributes(
        feat, ns, "veg", ["SolitaryVegetationObject", "PlantCover"],
        ["class", "function", "usage", "species"]
    )
    attrs.update(simple_attrs)
    
    # Measurements with uom
    measurement_attrs = parse_measurement_attributes(
        feat, ns, "veg", ["SolitaryVegetationObject", "PlantCover"],
        ["height", "trunkDiameter", "crownDiameter"]
    )
    attrs.update(measurement_attrs)
    
    return attrs


def parse_cityfurniture_attributes(feat, ns):
    """
    Parse CityFurniture-specific attributes (CityGML 2.0 and 3.0).
    
    Args:
        feat: XML feature element
        ns: Namespace dictionary
        
    Returns:
        Dictionary with CityFurniture attributes
    """
    return parse_simple_feature_attributes(
        feat, ns, "frn", ["CityFurniture"],
        ["class", "function", "usage"]
    )


def parse_landuse_attributes(feat, ns):
    """
    Parse LandUse-specific attributes (CityGML 2.0 and 3.0).
    
    Args:
        feat: XML feature element
        ns: Namespace dictionary
        
    Returns:
        Dictionary with LandUse attributes
    """
    return parse_simple_feature_attributes(
        feat, ns, "luse", ["LandUse"],
        ["class", "function", "usage"]
    )


def parse_all_feature_type_attributes(feat, ns):
    """
    Parse all feature-type-specific attributes.
    
    This function combines all feature-type parsers and returns
    all applicable attributes.
    
    Args:
        feat: XML feature element
        ns: Namespace dictionary
        
    Returns:
        Dictionary with all applicable feature-type attributes
    """
    attrs = {}
    
    # Bridge
    attrs.update(parse_bridge_attributes(feat, ns))
    
    # Tunnel
    attrs.update(parse_tunnel_attributes(feat, ns))
    
    # WaterBody
    attrs.update(parse_waterbody_attributes(feat, ns))
    
    # Vegetation
    attrs.update(parse_vegetation_attributes(feat, ns))
    
    # CityFurniture
    attrs.update(parse_cityfurniture_attributes(feat, ns))
    
    # LandUse
    attrs.update(parse_landuse_attributes(feat, ns))
    
    return attrs
