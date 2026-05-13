# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
xAL (eXtensible Address Language) Parser for CityGML

Supports both xAL 2.0 (CityGML 2.0) and xAL 3.0 (CityGML 3.0).

xAL 2.0 (OASIS CIQ TC): urn:oasis:names:tc:ciq:xsdschema:xAL:2.0
xAL 3.0 (OASIS CIQ TC): urn:oasis:names:tc:ciq:xal:3

Structure:
- Country
- AdministrativeArea (State, Province, Region)
- Locality (City, Town)
- Thoroughfare (Street)
- PostalCode
- Premises (Building Number, Building Name)
- SubAdministrativeArea
- DependentLocality
- PostBox

References:
- xAL 2.0: http://docs.oasis-open.org/election/external/xAL.html
- xAL 3.0: https://www.oasis-open.org/committees/ciq/
"""

from typing import Dict, Optional, Any
try:
    from lxml import etree as ET
except ImportError:
    import xml.etree.ElementTree as ET


class XALParser:
    """
    Parser for xAL (eXtensible Address Language) in CityGML.
    
    Supports:
    - xAL 2.0 (CityGML 2.0)
    - xAL 3.0 (CityGML 3.0)
    """
    
    def __init__(self, version="3.0"):
        """
        Initialize xAL parser.
        
        Args:
            version: xAL version ("2.0" or "3.0")
        """
        self.version = version
        
        if version == "3.0":
            self.ns = "urn:oasis:names:tc:ciq:xal:3"
        else:  # xAL 2.0
            self.ns = "urn:oasis:names:tc:ciq:xsdschema:xAL:2.0"
    
    def parse_address(self, xal_address_element, namespaces: Dict[str, str]) -> Dict[str, Any]:
        """
        Parse xAL Address element to structured dictionary.
        
        Args:
            xal_address_element: xAL:Address XML element
            namespaces: XML namespace dictionary
        
        Returns:
            Dictionary with structured address fields
        """
        if xal_address_element is None:
            return {}
        
        if self.version == "3.0":
            return self._parse_xal3(xal_address_element, namespaces)
        else:
            return self._parse_xal2(xal_address_element, namespaces)
    
    def _parse_xal3(self, addr_el, ns_dict) -> Dict[str, Any]:
        """Parse xAL 3.0 Address (CityGML 3.0)."""
        result = {}
        
        # Country
        country = addr_el.find(".//xAL:Country", namespaces=ns_dict)
        if country is not None:
            # xAL 3.0: NameElement
            country_name = country.findtext(".//xAL:NameElement", namespaces=ns_dict)
            if country_name and country_name.strip():
                result["country"] = country_name.strip()
                
            # xAL 3.0: Code (e.g., ISO 3166-1 alpha-2)
            country_code = country.findtext(".//xAL:Code", namespaces=ns_dict)
            if country_code and country_code.strip():
                result["countryCode"] = country_code.strip()
        
        # AdministrativeArea (State/Province)
        admin_area = addr_el.find(".//xAL:AdministrativeArea", namespaces=ns_dict)
        if admin_area is not None:
            admin_name = admin_area.findtext(".//xAL:NameElement", namespaces=ns_dict)
            if admin_name and admin_name.strip():
                result["administrativeArea"] = admin_name.strip()
            
            # xAL 3.0: Type attribute (e.g., "State", "Province")
            admin_type = admin_area.get("{%s}Type" % self.ns)
            if admin_type:
                result["administrativeAreaType"] = admin_type
        
        # Locality (City/Town)
        locality = addr_el.find(".//xAL:Locality", namespaces=ns_dict)
        if locality is not None:
            locality_name = locality.findtext(".//xAL:NameElement", namespaces=ns_dict)
            if locality_name and locality_name.strip():
                result["locality"] = locality_name.strip()
            
            # xAL 3.0: Type attribute
            locality_type = locality.get("{%s}Type" % self.ns)
            if locality_type:
                result["localityType"] = locality_type
        
        # DependentLocality (Suburb, District, Neighborhood)
        dep_locality = addr_el.find(".//xAL:DependentLocality", namespaces=ns_dict)
        if dep_locality is not None:
            dep_name = dep_locality.findtext(".//xAL:NameElement", namespaces=ns_dict)
            if dep_name and dep_name.strip():
                result["dependentLocality"] = dep_name.strip()
        
        # Thoroughfare (Street)
        thoroughfare = addr_el.find(".//xAL:Thoroughfare", namespaces=ns_dict)
        if thoroughfare is not None:
            # xAL 3.0: NameElement for street name
            street_name = thoroughfare.findtext(".//xAL:NameElement", namespaces=ns_dict)
            if street_name and street_name.strip():
                result["street"] = street_name.strip()
            
            # xAL 3.0: Number for street number
            street_number = thoroughfare.findtext(".//xAL:Number", namespaces=ns_dict)
            if street_number and street_number.strip():
                result["streetNumber"] = street_number.strip()
            
            # xAL 3.0: Type attribute (e.g., "Street", "Avenue", "Road")
            thoroughfare_type = thoroughfare.get("{%s}Type" % self.ns)
            if thoroughfare_type:
                result["streetType"] = thoroughfare_type
        
        # Premises (Building Number/Name)
        premises = addr_el.find(".//xAL:Premises", namespaces=ns_dict)
        if premises is not None:
            # Building Number
            building_number = premises.findtext(".//xAL:Number", namespaces=ns_dict)
            if building_number and building_number.strip():
                result["buildingNumber"] = building_number.strip()
            
            # Building Name
            building_name = premises.findtext(".//xAL:NameElement", namespaces=ns_dict)
            if building_name and building_name.strip():
                result["buildingName"] = building_name.strip()
            
            # SubPremises (Unit/Apartment Number)
            sub_premises = premises.find(".//xAL:SubPremises", namespaces=ns_dict)
            if sub_premises is not None:
                unit_number = sub_premises.findtext(".//xAL:Number", namespaces=ns_dict)
                if unit_number and unit_number.strip():
                    result["unitNumber"] = unit_number.strip()
        
        # PostCode (Postal Code / ZIP)
        postcode = addr_el.find(".//xAL:PostCode", namespaces=ns_dict)
        if postcode is not None:
            postcode_id = postcode.findtext(".//xAL:Identifier", namespaces=ns_dict)
            if postcode_id and postcode_id.strip():
                result["postCode"] = postcode_id.strip()
        
        # PostBox
        postbox = addr_el.find(".//xAL:PostBox", namespaces=ns_dict)
        if postbox is not None:
            postbox_number = postbox.findtext(".//xAL:Identifier", namespaces=ns_dict)
            if postbox_number and postbox_number.strip():
                result["postBox"] = postbox_number.strip()
        
        return result
    
    def _parse_xal2(self, addr_el, ns_dict) -> Dict[str, Any]:
        """Parse xAL 2.0 Address (CityGML 2.0)."""
        result = {}
        
        # xAL 2.0 structure is different (more flat)
        
        # Country
        country = addr_el.find(".//xAL:Country", namespaces=ns_dict)
        if country is not None:
            # xAL 2.0: CountryName or CountryNameCode
            country_name = country.findtext(".//xAL:CountryName", namespaces=ns_dict)
            if country_name and country_name.strip():
                result["country"] = country_name.strip()
            
            country_code = country.findtext(".//xAL:CountryNameCode", namespaces=ns_dict)
            if country_code and country_code.strip():
                result["countryCode"] = country_code.strip()
        
        # AdministrativeArea (State/Province)
        admin_area = addr_el.find(".//xAL:AdministrativeArea", namespaces=ns_dict)
        if admin_area is not None:
            admin_name = admin_area.findtext(".//xAL:AdministrativeAreaName", namespaces=ns_dict)
            if admin_name and admin_name.strip():
                result["administrativeArea"] = admin_name.strip()
        
        # Locality (City/Town)
        locality = addr_el.find(".//xAL:Locality", namespaces=ns_dict)
        if locality is not None:
            locality_name = locality.findtext(".//xAL:LocalityName", namespaces=ns_dict)
            if locality_name and locality_name.strip():
                result["locality"] = locality_name.strip()
        
        # DependentLocality (Suburb, District)
        dep_locality = addr_el.find(".//xAL:DependentLocality", namespaces=ns_dict)
        if dep_locality is not None:
            dep_name = dep_locality.findtext(".//xAL:DependentLocalityName", namespaces=ns_dict)
            if dep_name and dep_name.strip():
                result["dependentLocality"] = dep_name.strip()
        
        # Thoroughfare (Street)
        thoroughfare = addr_el.find(".//xAL:Thoroughfare", namespaces=ns_dict)
        if thoroughfare is not None:
            # xAL 2.0: ThoroughfareName
            street_name = thoroughfare.findtext(".//xAL:ThoroughfareName", namespaces=ns_dict)
            if street_name and street_name.strip():
                result["street"] = street_name.strip()
            
            # xAL 2.0: ThoroughfareNumber
            street_number = thoroughfare.findtext(".//xAL:ThoroughfareNumber", namespaces=ns_dict)
            if street_number and street_number.strip():
                result["streetNumber"] = street_number.strip()
        
        # Premises (Building Number/Name)
        premises = addr_el.find(".//xAL:Premises", namespaces=ns_dict)
        if premises is not None:
            # xAL 2.0: PremisesNumber
            building_number = premises.findtext(".//xAL:PremisesNumber", namespaces=ns_dict)
            if building_number and building_number.strip():
                result["buildingNumber"] = building_number.strip()
            
            # xAL 2.0: PremisesName
            building_name = premises.findtext(".//xAL:PremisesName", namespaces=ns_dict)
            if building_name and building_name.strip():
                result["buildingName"] = building_name.strip()
            
            # SubPremises (Unit/Apartment)
            sub_premises = premises.find(".//xAL:SubPremises", namespaces=ns_dict)
            if sub_premises is not None:
                unit_number = sub_premises.findtext(".//xAL:SubPremisesNumber", namespaces=ns_dict)
                if unit_number and unit_number.strip():
                    result["unitNumber"] = unit_number.strip()
        
        # PostalCode (xAL 2.0)
        postcode = addr_el.find(".//xAL:PostalCode", namespaces=ns_dict)
        if postcode is not None:
            postcode_number = postcode.findtext(".//xAL:PostalCodeNumber", namespaces=ns_dict)
            if postcode_number and postcode_number.strip():
                result["postCode"] = postcode_number.strip()
        
        # PostBox
        postbox = addr_el.find(".//xAL:PostBox", namespaces=ns_dict)
        if postbox is not None:
            postbox_number = postbox.findtext(".//xAL:PostBoxNumber", namespaces=ns_dict)
            if postbox_number and postbox_number.strip():
                result["postBox"] = postbox_number.strip()
        
        return result


def parse_xal_address(xal_element, namespaces: Dict[str, str], version="3.0") -> Dict[str, Any]:
    """
    Convenience function to parse xAL Address element.
    
    Args:
        xal_element: xAL:Address XML element
        namespaces: XML namespace dictionary
        version: xAL version ("2.0" or "3.0")
    
    Returns:
        Dictionary with structured address fields
    """
    parser = XALParser(version=version)
    return parser.parse_address(xal_element, namespaces)
