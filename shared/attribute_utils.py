# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026

def write_generic_attributes(feature_el, obj_, version="3.0"):
    """
    Schreibe generische Attribute für CityGML 2.0 und 3.0.
    Für CityGML 2.0: Schreibe <gen:stringAttribute>, <gen:dateAttribute>, <gen:intAttribute>, <gen:doubleAttribute> direkt als Kindelemente, ohne <gen:genericAttribute> Wrapper.
    Für CityGML 3.0: Optional mit <gen:genericAttribute> Wrapper.
    """
    generic_attrs = obj_.get('generic_attributes', {})
    if version == "2.0":
        for k, v in generic_attrs.items():
            # Bestimme Typ
            if isinstance(v, float):
                el = SubElement(feature_el, Q("gen", "doubleAttribute", version))
                el.set("name", k)
                SubElement(el, Q("gen", "value", version)).text = str(v)
            elif isinstance(v, int):
                el = SubElement(feature_el, Q("gen", "intAttribute", version))
                el.set("name", k)
                SubElement(el, Q("gen", "value", version)).text = str(v)
            elif isinstance(v, str):
                el = SubElement(feature_el, Q("gen", "stringAttribute", version))
                el.set("name", k)
                SubElement(el, Q("gen", "value", version)).text = v
            # Optional: dateAttribute
            # ... ggf. weitere Typen ...
    else:
        # Für CityGML 3.0: Schreibe ggf. mit <gen:genericAttribute> Wrapper
        # ...bestehende Logik...
        pass
"""
Shared attribute parsing and writing utilities for CityGML 2.0 and 3.0.

This module provides common functions for handling feature-type-specific attributes
that are similar across CityGML versions but may have different namespaces or formats.
"""

from xml.etree.ElementTree import SubElement

# Namespace mappings for CityGML 2.0 and 3.0
NAMESPACE_MAP = {
    "bldg": "http://www.opengis.net/citygml/building/3.0",
    "brid": "http://www.opengis.net/citygml/bridge/3.0",
    "tun": "http://www.opengis.net/citygml/tunnel/3.0",
    "wtr": "http://www.opengis.net/citygml/waterbody/3.0",
    "veg": "http://www.opengis.net/citygml/vegetation/3.0",
    "frn": "http://www.opengis.net/citygml/cityfurniture/3.0",
    "luse": "http://www.opengis.net/citygml/landuse/3.0",
    "tran": "http://www.opengis.net/citygml/transportation/3.0",
    "dem": "http://www.opengis.net/citygml/relief/3.0",
    "gen": "http://www.opengis.net/citygml/generics/3.0",
    "con": "http://www.opengis.net/citygml/construction/3.0",
}

# CityGML 2.0 namespaces (fallback)
NAMESPACE_MAP_V2 = {
    "bldg": "http://www.opengis.net/citygml/building/2.0",
    "brid": "http://www.opengis.net/citygml/bridge/2.0",
    "tun": "http://www.opengis.net/citygml/tunnel/2.0",
    "wtr": "http://www.opengis.net/citygml/waterbody/2.0",
    # NOTE: For CityGML 3 exports we never want to emit vegetation elements in the 2.0
    # namespace. Legacy values may still exist in Blender custom properties, but they are
    # mapped to veg:* (3.0) during export.
    "veg": "http://www.opengis.net/citygml/vegetation/3.0",
    "frn": "http://www.opengis.net/citygml/cityfurniture/2.0",
    "luse": "http://www.opengis.net/citygml/landuse/2.0",
    "tran": "http://www.opengis.net/citygml/transportation/2.0",
    "dem": "http://www.opengis.net/citygml/relief/2.0",
    "gen": "http://www.opengis.net/citygml/generics/2.0",
}

def Q(ns_key, local_name, version="3.0"):
    """Create qualified XML tag name.
    
    Args:
        ns_key: Namespace prefix key (e.g., 'bldg', 'brid')
        local_name: Local element name
        version: CityGML version ('2.0' or '3.0')
    
    Returns:
        Qualified name string '{namespace}localname'
    """
    ns_map = NAMESPACE_MAP if version == "3.0" else NAMESPACE_MAP_V2
    namespace = ns_map.get(ns_key, NAMESPACE_MAP.get(ns_key))
    return f"{{{namespace}}}{local_name}"
def parse_boolean_attribute(value):
    """
    Parse boolean attribute from string.
    
    Args:
        value: String value ("true", "false", "1", "0")
        
    Returns:
        Boolean value or None if parsing fails
    """
    if value is None:
        return None
    
    val_lower = str(value).strip().lower()
    if val_lower in ("true", "1"):
        return True
    elif val_lower in ("false", "0"):
        return False
    return None


def parse_measurement_with_uom(element, ns, tag_name):
    """
    Parse measurement element with uom attribute.
    
    Args:
        element: XML element to search
        ns: Namespace dictionary
        tag_name: Name of the measurement element
        
    Returns:
        Tuple (value, uom) or (None, None)
    """
    measurement_el = element.find(f"./{tag_name}", ns)
    if measurement_el is not None and measurement_el.text:
        value = measurement_el.text.strip()
        uom = measurement_el.get("uom", "m")
        try:
            return float(value), uom
        except ValueError:
            return value, uom
    return None, None


def write_simple_attributes(feature_el, obj_, namespace, attributes, version="3.0"):
    for prop_key, element_name in attributes:
        val = obj_.get(prop_key, None)
        if val is None:
            continue
        # Werte können Listen/Strings sein
        if isinstance(val, (list, tuple)):
            vals = val
        else:
            vals = [val]
        for v in vals:
            text = str(v).strip()
            if text:
                SubElement(feature_el, Q(namespace, element_name, version=version)).text = text


def write_measurement_with_uom(feature_el, obj_, namespace, element_name, value_key, uom_key, default_uom="m", version="3.0"):
    value = obj_.get(value_key, None)
    if value is None:
        return
    el = SubElement(feature_el, Q(namespace, element_name, version=version))
    el.text = str(value).strip()
    uom = obj_.get(uom_key, default_uom)
    if uom:
        el.set("uom", str(uom).strip())


def write_boolean_attribute(feature_el, obj_, namespace, element_name, property_key):
    """
    Write boolean attribute.
    
    Args:
        feature_el: XML element to write to
        obj_: Blender object with custom properties
        namespace: Namespace prefix
        element_name: Name of the boolean element
        property_key: Property key for the boolean value
    """
    value = obj_.get(property_key, None)
    if value is not None:
        bool_val = parse_boolean_attribute(value)
        if bool_val is not None:
            el = SubElement(feature_el, Q(namespace, element_name))
            el.text = "true" if bool_val else "false"


def _parse_generic_attributes_common_impl(feat_el, ns, version="3.0"):
    """
    Parse generic attributes (CityGML 2.0 and 3.0).
    
    Args:
        feat_el: Feature XML element
        ns: Namespace dictionary
        version: CityGML version ("2.0" or "3.0")
        
    Returns:
        Dictionary of generic attributes
    """
    def _cast(ln_lower, txt):
        t = (txt or "").strip()
        if ln_lower.endswith("doubleattribute"):
            try: 
                return float(t)
            except: 
                return t
        if ln_lower.endswith("intattribute"):
            try: 
                return int(t)
            except: 
                return t
        if ln_lower.endswith("dateattribute"):
            return t
        if ln_lower.endswith("datetimeattribute"):
            return t
        # stringAttribute or unknown → Text
        return t

    attrs = {}

    # Wrapper variant: <core:genericAttribute>…<gen:*Attribute>…</gen:*Attribute>…
    # CityGML 3.0 erlaubt auch die unpräfixierte Variante <genericAttribute> im Default-NS (core),
    # abhängig von der Serialisierung. Daher beide Varianten abdecken.
    if version == "3.0":
        # Suche nach prefixed Varianten
        holders = list(feat_el.findall("./core:genericAttribute", ns)) + \
                  list(feat_el.findall("./gen:genericAttribute", ns))
        
        # Suche nach unprefixed genericAttribute (wenn core Default-NS ist)
        for child in feat_el:
            if not isinstance(child.tag, str):
                continue
            # Tag kann sein: "{http://www.opengis.net/citygml/3.0}genericAttribute" oder "genericAttribute"
            tag_local = child.tag.split("}")[-1]
            if tag_local == "genericAttribute":
                # Prüfe, ob es im core-Namespace ist (oder unprefixed)
                tag_ns = child.tag.split("}")[0].strip("{") if "}" in child.tag else ""
                if not tag_ns or tag_ns == ns.get("core", ""):
                    holders.append(child)
        
        for holder in holders:
            for el in list(holder):
                if not isinstance(el.tag, str): 
                    continue
                ln = el.tag.split("}")[-1].lower()
                name = (el.findtext("./gen:name", default="", namespaces=ns) or "").strip()
                val = (el.findtext("./gen:value", default="", namespaces=ns) or "").strip()
                if name:
                    attrs[name] = _cast(ln, val)

    # Direct variant: <gen:*Attribute> as immediate child of feature
    for el in feat_el.findall("./gen:*", ns):
        if not isinstance(el.tag, str): 
            continue
        ln = el.tag.split("}")[-1].lower()
        if not ln.endswith("attribute"):
            continue
        
        # CityGML 2.0: name as XML attribute <gen:stringAttribute name="TYPE">
        if version == "2.0":
            name_attr = el.get("name")
            if name_attr:
                val = (el.findtext("./gen:value", default="", namespaces=ns) or "").strip()
                attrs[name_attr] = _cast(ln, val)
            else:
                # Fallback to element-based name
                name = (el.findtext("./gen:name", default="", namespaces=ns) or "").strip()
                val = (el.findtext("./gen:value", default="", namespaces=ns) or "").strip()
                if name:
                    attrs[name] = _cast(ln, val)
        else:
            # CityGML 3.0: name as element <gen:name>
            name = (el.findtext("./gen:name", default="", namespaces=ns) or "").strip()
            val = (el.findtext("./gen:value", default="", namespaces=ns) or "").strip()
            if name:
                attrs[name] = _cast(ln, val)

    return attrs


def write_feature_type_attributes_bridge(feature_el, obj_, namespace, version="2.0"):
    """
    Write Bridge-specific attributes (common for CityGML 2.0 and 3.0).
    
    Args:
        feature_el: XML element to write to
        obj_: Blender object with custom properties
        namespace: Namespace prefix ("brid")
    """
    # Common attributes: class, function, usage
    write_simple_attributes(feature_el, obj_, namespace, [
        (f"{namespace}:class", "class"),
        (f"{namespace}:function", "function"),
        (f"{namespace}:usage", "usage"),
    ])
    
    # isMovable (boolean)
    write_boolean_attribute(feature_el, obj_, namespace, "isMovable", f"{namespace}:isMovable")


def write_feature_type_attributes_tunnel(feature_el, obj_, namespace, version="2.0"):
    """
    Write Tunnel-specific attributes (common for CityGML 2.0 and 3.0).
    
    Args:
        feature_el: XML element to write to
        obj_: Blender object with custom properties
        namespace: Namespace prefix ("tun")
    """
    # Common attributes: class, function, usage
    write_simple_attributes(feature_el, obj_, namespace, [
        (f"{namespace}:class", "class"),
        (f"{namespace}:function", "function"),
        (f"{namespace}:usage", "usage"),
    ])


def write_feature_type_attributes_waterbody(feature_el, obj_, namespace, version="2.0"):
    """
    Write WaterBody-specific attributes (common for CityGML 2.0 and 3.0).
    
    Args:
        feature_el: XML element to write to
        obj_: Blender object with custom properties
        namespace: Namespace prefix ("wtr")
    """
    # Common attributes: class, function, usage, waterLevel
    write_simple_attributes(feature_el, obj_, namespace, [
        (f"{namespace}:class", "class"),
        (f"{namespace}:function", "function"),
        (f"{namespace}:usage", "usage"),
        (f"{namespace}:waterLevel", "waterLevel"),
    ])


def write_feature_type_attributes_vegetation(feature_el, obj_, namespace, version="2.0"):
    """
    Write Vegetation-specific attributes (common for CityGML 2.0 and 3.0).
    
    Args:
        feature_el: XML element to write to
        obj_: Blender object with custom properties
        namespace: Namespace prefix ("veg")
    """
    # Common attributes: class, function, usage, species
    write_simple_attributes(feature_el, obj_, namespace, [
        (f"{namespace}:class", "class"),
        (f"{namespace}:function", "function"),
        (f"{namespace}:usage", "usage"),
        (f"{namespace}:species", "species"),
    ], version=version)

    def _pick_measurement(base_key: str):
        # Prefer CityGML 3 keys, but accept legacy CityGML 2 keys stored on the object
        # (e.g. from older imports) and export them as proper CityGML 3 vegetation attributes.
        val = obj_.get(f"{namespace}:{base_key}", None)
        uom = obj_.get(f"{namespace}:{base_key}:uom", None)

        if val is None:
            legacy_key = f"ns10:{base_key}"
            val = obj_.get(legacy_key, None)
            if uom is None:
                uom = obj_.get(f"{legacy_key}:uom", None)

        return val, uom

    for key, default_uom in (("height", "m"), ("trunkDiameter", "m"), ("crownDiameter", "m")):
        val, uom = _pick_measurement(key)
        if val is None:
            continue
        el = SubElement(feature_el, Q(namespace, key, version=version))
        el.text = str(val).strip()
        if uom:
            el.set("uom", str(uom).strip())
        else:
            el.set("uom", default_uom)


def write_feature_type_attributes_cityfurniture(feature_el, obj_, namespace, version="2.0"):
    """
    Write CityFurniture-specific attributes (common for CityGML 2.0 and 3.0).
    
    Args:
        feature_el: XML element to write to
        obj_: Blender object with custom properties
        namespace: Namespace prefix ("frn")
    """
    # Common attributes: class, function, usage
    write_simple_attributes(feature_el, obj_, namespace, [
        (f"{namespace}:class", "class"),
        (f"{namespace}:function", "function"),
        (f"{namespace}:usage", "usage"),
    ])


def write_feature_type_attributes_landuse(feature_el, obj_, namespace, version="2.0"):
    """
    Write LandUse-specific attributes (common for CityGML 2.0 and 3.0).
    
    Args:
        feature_el: XML element to write to
        obj_: Blender object with custom properties
        namespace: Namespace prefix ("luse")
    """
    # Common attributes: class, function, usage
    write_simple_attributes(feature_el, obj_, namespace, [
        (f"{namespace}:class", "class"),
        (f"{namespace}:function", "function"),
        (f"{namespace}:usage", "usage"),
    ])


def write_feature_type_attributes_transportation(feature_el, obj_, namespace, version="2.0"):
    """
    Write Transportation-specific attributes (common for CityGML 2.0 and 3.0).
    
    Args:
        feature_el: XML element to write to
        obj_: Blender object with custom properties
        namespace: Namespace prefix ("tran")
    """
    # Common attributes: class, function, usage
    write_simple_attributes(feature_el, obj_, namespace, [
        (f"{namespace}:class", "class"),
        (f"{namespace}:function", "function"),
        (f"{namespace}:usage", "usage"),
    ])


def write_feature_type_attributes_hollowspace(feature_el, obj_, namespace, version="3.0"):
    """
    Write HollowSpace-specific attributes (CityGML 3.0 only).
    
    Args:
        feature_el: XML element to write to
        obj_: Blender object with custom properties
        namespace: Namespace prefix ("tun")
    """
    # Common attributes: class, function, usage
    write_simple_attributes(feature_el, obj_, namespace, [
        (f"{namespace}:class", "class"),
        (f"{namespace}:function", "function"),
        (f"{namespace}:usage", "usage"),
    ], version=version)


def write_feature_type_attributes_tunnelfurniture(feature_el, obj_, namespace, version="3.0"):
    """
    Write TunnelFurniture-specific attributes (CityGML 3.0 only).
    
    Args:
        feature_el: XML element to write to
        obj_: Blender object with custom properties
        namespace: Namespace prefix ("tun")
    """
    # Common attributes: class, function, usage
    write_simple_attributes(feature_el, obj_, namespace, [
        (f"{namespace}:class", "class"),
        (f"{namespace}:function", "function"),
        (f"{namespace}:usage", "usage"),
    ], version=version)


def write_feature_type_attributes_bridgeroom(feature_el, obj_, namespace, version="3.0"):
    """
    Write BridgeRoom-specific attributes (CityGML 3.0 only).
    
    Args:
        feature_el: XML element to write to
        obj_: Blender object with custom properties
        namespace: Namespace prefix ("brid")
    """
    # Common attributes: class, function, usage
    write_simple_attributes(feature_el, obj_, namespace, [
        (f"{namespace}:class", "class"),
        (f"{namespace}:function", "function"),
        (f"{namespace}:usage", "usage"),
    ], version=version)


def write_feature_type_attributes_bridgefurniture(feature_el, obj_, namespace, version="3.0"):
    """
    Write BridgeFurniture-specific attributes (CityGML 3.0 only).
    
    Args:
        feature_el: XML element to write to
        obj_: Blender object with custom properties
        namespace: Namespace prefix ("brid")
    """
    # Common attributes: class, function, usage
    write_simple_attributes(feature_el, obj_, namespace, [
        (f"{namespace}:class", "class"),
        (f"{namespace}:function", "function"),
        (f"{namespace}:usage", "usage"),
    ], version=version)


# NOTE: BuildingUnit and Storey export is handled inline in io/writer/exporter.py
# (lines 891-960) due to complex cross-referencing logic (buildingUnit ↔ storey refs).
# No separate export functions are needed here.


def parse_generic_attributes_common(feat_el, ns, version="3.0"):
    """
    Backward-compat wrapper. This second definition previously shadowed the real implementation.
    Keep this signature but delegate to the primary implementation above.
    """
    return _parse_generic_attributes_common_impl(feat_el, ns, version=version)
