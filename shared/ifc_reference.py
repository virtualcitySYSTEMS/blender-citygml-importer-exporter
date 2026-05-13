# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
IFC GUID Support for CityGML ExternalReference

Provides utilities to create and parse IFC GUID references within CityGML's
ExternalReference element. IFC GUIDs are encoded as URIs with the "ifc://" scheme.

Usage:
    # Create ExternalReference with IFC GUID
    ext_ref = create_ifc_external_reference(
        ifc_guid="2O2Fr$t4X7Zf8NOew3FNr2",
        information_system="IFC Project: MyBuilding.ifc"
    )
    
    # Parse IFC GUID from ExternalReference element
    ifc_guid = parse_ifc_guid_from_external_reference(ext_ref_element)
"""

import re
from xml.etree.ElementTree import Element, SubElement


def is_valid_ifc_guid(guid_str: str) -> bool:
    """
    Validate IFC GUID format (22 characters, Base64-like encoding).
    
    IFC GUID uses a custom Base64 encoding with alphabet:
    0-9, A-Z, a-z, _, $
    
    Args:
        guid_str: IFC GUID string to validate
        
    Returns:
        True if valid IFC GUID format
    """
    if not guid_str or not isinstance(guid_str, str):
        return False
    
    # IFC GUID is exactly 22 characters
    if len(guid_str) != 22:
        return False
    
    # Allowed characters: 0-9, A-Z, a-z, _, $
    if not re.match(r'^[0-9A-Za-z_$]{22}$', guid_str):
        return False
    
    return True


def create_ifc_guid_uri(ifc_guid: str) -> str:
    """
    Create IFC GUID URI in the format: ifc://guid/{GUID}
    
    Args:
        ifc_guid: IFC GUID string (22 characters)
        
    Returns:
        URI string for ExternalReference.targetResource
        
    Raises:
        ValueError: If IFC GUID format is invalid
    """
    if not is_valid_ifc_guid(ifc_guid):
        raise ValueError(f"Invalid IFC GUID format: {ifc_guid} (expected 22 characters)")
    
    return f"ifc://guid/{ifc_guid}"


def parse_ifc_guid_from_uri(uri: str) -> str | None:
    """
    Extract IFC GUID from URI.
    
    Supports formats:
    - ifc://guid/{GUID}
    - ifc:guid:{GUID}
    - {GUID} (if it matches IFC GUID format)
    
    Args:
        uri: URI string from ExternalReference.targetResource
        
    Returns:
        IFC GUID string or None if not an IFC GUID URI
    """
    if not uri or not isinstance(uri, str):
        return None
    
    # Format: ifc://guid/{GUID}
    match = re.match(r'^ifc://guid/([0-9A-Za-z_$]{22})$', uri)
    if match:
        return match.group(1)
    
    # Format: ifc:guid:{GUID}
    match = re.match(r'^ifc:guid:([0-9A-Za-z_$]{22})$', uri)
    if match:
        return match.group(1)
    
    # Direct GUID (no scheme)
    if is_valid_ifc_guid(uri):
        return uri
    
    return None


def create_ifc_external_reference(
    ifc_guid: str,
    information_system: str | None = None,
    relation_type: str | None = None
) -> dict:
    """
    Create ExternalReference dictionary for IFC GUID.
    
    Args:
        ifc_guid: IFC GUID string (22 characters)
        information_system: Optional URI to IFC file or project
        relation_type: Optional relation type URI (e.g., OWL sameAs)
        
    Returns:
        Dictionary with ExternalReference fields
        
    Example:
        >>> ref = create_ifc_external_reference("2O2Fr$t4X7Zf8NOew3FNr2", "file:///building.ifc")
        >>> ref['targetResource']
        'ifc://guid/2O2Fr$t4X7Zf8NOew3FNr2'
    """
    target_resource = create_ifc_guid_uri(ifc_guid)
    
    ref = {
        'targetResource': target_resource
    }
    
    if information_system:
        ref['informationSystem'] = information_system
    
    if relation_type:
        ref['relationType'] = relation_type
    
    return ref


def write_external_reference_xml(
    parent_el: Element,
    target_resource: str,
    information_system: str | None = None,
    relation_type: str | None = None,
    ns_prefix: str = "core",
    ns_uri: str = "http://www.opengis.net/citygml/3.0"
) -> Element:
    """
    Write CityGML 3.0 ExternalReference element.
    
    Args:
        parent_el: Parent XML element (e.g., Building)
        target_resource: URI to external object (required)
        information_system: Optional URI to external system
        relation_type: Optional relation type URI
        ns_prefix: Namespace prefix (default: "core")
        ns_uri: Namespace URI (default: CityGML 3.0 core)
        
    Returns:
        Created ExternalReference element
        
    Example XML:
        <core:externalReference>
          <core:ExternalReference>
            <core:targetResource>ifc://guid/2O2Fr$t4X7Zf8NOew3FNr2</core:targetResource>
            <core:informationSystem>file:///building.ifc</core:informationSystem>
          </core:ExternalReference>
        </core:externalReference>
    """
    ext_ref_prop = SubElement(parent_el, f"{{{ns_uri}}}externalReference")
    ext_ref = SubElement(ext_ref_prop, f"{{{ns_uri}}}ExternalReference")
    
    target_el = SubElement(ext_ref, f"{{{ns_uri}}}targetResource")
    target_el.text = target_resource
    
    if information_system:
        info_el = SubElement(ext_ref, f"{{{ns_uri}}}informationSystem")
        info_el.text = information_system
    
    if relation_type:
        rel_el = SubElement(ext_ref, f"{{{ns_uri}}}relationType")
        rel_el.text = relation_type
    
    return ext_ref


def parse_external_reference_xml(
    ext_ref_el: Element,
    ns: dict | None = None
) -> dict | None:
    """
    Parse ExternalReference element into dictionary.
    
    Args:
        ext_ref_el: ExternalReference XML element
        ns: Namespace dictionary
        
    Returns:
        Dictionary with 'targetResource', 'informationSystem', 'relationType' or None
    """
    if ns is None:
        ns = {
            'core': 'http://www.opengis.net/citygml/3.0',
            'gml': 'http://www.opengis.net/gml/3.2'
        }
    
    target_resource = None
    information_system = None
    relation_type = None
    
    # Try both with and without namespace
    for prefix in ['core:', '']:
        if target_resource is None:
            target_el = ext_ref_el.find(f".//{prefix}targetResource", ns)
            if target_el is not None and target_el.text:
                target_resource = target_el.text.strip()
        
        if information_system is None:
            info_el = ext_ref_el.find(f".//{prefix}informationSystem", ns)
            if info_el is not None and info_el.text:
                information_system = info_el.text.strip()
        
        if relation_type is None:
            rel_el = ext_ref_el.find(f".//{prefix}relationType", ns)
            if rel_el is not None and rel_el.text:
                relation_type = rel_el.text.strip()
    
    if not target_resource:
        return None
    
    ref = {'targetResource': target_resource}
    
    if information_system:
        ref['informationSystem'] = information_system
    
    if relation_type:
        ref['relationType'] = relation_type
    
    return ref


def get_ifc_guid_from_object(obj) -> str | None:
    """
    Extract IFC GUID from Blender object custom properties.
    
    Checks multiple property name variants:
    - ifc_guid
    - IFC_GUID
    - IfcGuid
    - core:externalReference_ifc_guid
    
    Args:
        obj: Blender object with custom properties
        
    Returns:
        IFC GUID string or None
    """
    if not obj:
        return None
    
    # Try common property names
    for prop_name in ['ifc_guid', 'IFC_GUID', 'IfcGuid', 'core:externalReference_ifc_guid']:
        try:
            value = obj.get(prop_name)
            if value and isinstance(value, str):
                # Validate GUID format
                if is_valid_ifc_guid(value):
                    return value
                # Try parsing from URI
                guid = parse_ifc_guid_from_uri(value)
                if guid:
                    return guid
        except Exception:
            continue
    
    return None


def set_ifc_guid_on_object(obj, ifc_guid: str, information_system: str | None = None) -> None:
    """
    Store IFC GUID as custom property on Blender object.
    
    Stores:
    - ifc_guid: IFC GUID string
    - ifc_information_system: Optional IFC file reference
    
    Args:
        obj: Blender object
        ifc_guid: IFC GUID string (22 characters)
        information_system: Optional IFC file path or URI
        
    Raises:
        ValueError: If IFC GUID format is invalid
    """
    if not is_valid_ifc_guid(ifc_guid):
        raise ValueError(f"Invalid IFC GUID format: {ifc_guid}")
    
    obj['ifc_guid'] = ifc_guid
    
    if information_system:
        obj['ifc_information_system'] = information_system


# Example IFC GUIDs for testing
EXAMPLE_IFC_GUIDS = {
    'building': '2O2Fr$t4X7Zf8NOew3FNr2',
    'wall': '3P3Gs$u5Y8Ah9OPfx4GOt3',
    'door': '1N1Er$s3W6Ye7MPdw2EMq1',
    'window': '0M0Dq$r2V5Xd6LObv1DLp0',
}
