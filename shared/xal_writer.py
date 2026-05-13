# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
xAL (eXtensible Address Language) Writer for CityGML Export

Supports both xAL 2.0 (CityGML 2.0) and xAL 3.0 (CityGML 3.0).

Creates structured xAL Address elements from dictionary data.
"""

from typing import Dict, Any, Callable


def Q(ns_prefix: str, tag: str) -> str:
    """Helper to create qualified tag name."""
    # This will be overridden by the actual Q function from the exporter
    return f"{{{ns_prefix}}}{tag}"


class XALWriter:
    """
    Writer for xAL (eXtensible Address Language) in CityGML Export.
    
    Supports:
    - xAL 2.0 (CityGML 2.0)
    - xAL 3.0 (CityGML 3.0)
    """
    
    def __init__(self, version="3.0"):
        """
        Initialize xAL writer.
        
        Args:
            version: xAL version ("2.0" or "3.0")
        """
        self.version = version
        self.SubElement = None  # Will be set by caller
    
    def write_address(self, xal_root, addr_data: Dict[str, Any], Q_func=None, SubElement_func=None):
        """
        Write structured address data to xAL:Address element.
        
        Args:
            xal_root: xAL:Address XML element (parent)
            addr_data: Dictionary with address fields
            Q_func: Function to create qualified tag names
            SubElement_func: SubElement function from xml.etree or lxml
        """
        if Q_func:
            global Q
            Q = Q_func
        
        if SubElement_func:
            self.SubElement = SubElement_func
        else:
            # Fallback to xml.etree.ElementTree
            from xml.etree.ElementTree import SubElement
            self.SubElement = SubElement
        
        if self.version == "3.0":
            self._write_xal3(xal_root, addr_data)
        else:
            self._write_xal2(xal_root, addr_data)
    
    def _write_xal3(self, xal_root, data: Dict[str, Any]):
        """Write xAL 3.0 Address (CityGML 3.0)."""
        
        # Country
        if "country" in data or "countryCode" in data:
            country_el = self.SubElement(xal_root, Q("xAL", "Country"))
            
            if "countryCode" in data and data["countryCode"]:
                code_el = self.SubElement(country_el, Q("xAL", "Code"))
                code_el.text = str(data["countryCode"])
            
            if "country" in data and data["country"]:
                name_el = self.SubElement(country_el, Q("xAL", "NameElement"))
                name_el.set(Q("xAL", "NameType"), "Name")
                name_el.text = str(data["country"])
        
        # AdministrativeArea (State/Province)
        if "administrativeArea" in data and data["administrativeArea"]:
            admin_el = self.SubElement(xal_root, Q("xAL", "AdministrativeArea"))
            
            if "administrativeAreaType" in data and data["administrativeAreaType"]:
                admin_el.set(Q("xAL", "Type"), str(data["administrativeAreaType"]))
            
            name_el = self.SubElement(admin_el, Q("xAL", "NameElement"))
            name_el.set(Q("xAL", "NameType"), "Name")
            name_el.text = str(data["administrativeArea"])
        
        # Locality (City/Town)
        if "locality" in data and data["locality"]:
            locality_el = self.SubElement(xal_root, Q("xAL", "Locality"))
            
            if "localityType" in data and data["localityType"]:
                locality_el.set(Q("xAL", "Type"), str(data["localityType"]))
            
            name_el = self.SubElement(locality_el, Q("xAL", "NameElement"))
            name_el.set(Q("xAL", "NameType"), "Name")
            name_el.text = str(data["locality"])
        
        # DependentLocality (Suburb/District)
        if "dependentLocality" in data and data["dependentLocality"]:
            dep_el = self.SubElement(xal_root, Q("xAL", "DependentLocality"))
            name_el = self.SubElement(dep_el, Q("xAL", "NameElement"))
            name_el.set(Q("xAL", "NameType"), "Name")
            name_el.text = str(data["dependentLocality"])
        
        # Thoroughfare (Street)
        if "street" in data or "streetNumber" in data:
            thoroughfare_el = self.SubElement(xal_root, Q("xAL", "Thoroughfare"))
            
            if "streetType" in data and data["streetType"]:
                thoroughfare_el.set(Q("xAL", "Type"), str(data["streetType"]))
            else:
                thoroughfare_el.set(Q("xAL", "Type"), "Street")
            
            if "streetNumber" in data and data["streetNumber"]:
                number_el = self.SubElement(thoroughfare_el, Q("xAL", "Number"))
                number_el.set(Q("xAL", "Type"), "Number")
                number_el.text = str(data["streetNumber"])
            
            if "street" in data and data["street"]:
                name_el = self.SubElement(thoroughfare_el, Q("xAL", "NameElement"))
                name_el.text = str(data["street"])
        
        # Premises (Building Number/Name)
        if "buildingNumber" in data or "buildingName" in data or "unitNumber" in data:
            premises_el = self.SubElement(xal_root, Q("xAL", "Premises"))
            
            if "buildingNumber" in data and data["buildingNumber"]:
                number_el = self.SubElement(premises_el, Q("xAL", "Number"))
                number_el.set(Q("xAL", "Type"), "Number")
                number_el.text = str(data["buildingNumber"])
            
            if "buildingName" in data and data["buildingName"]:
                name_el = self.SubElement(premises_el, Q("xAL", "NameElement"))
                name_el.text = str(data["buildingName"])
            
            # SubPremises (Unit/Apartment)
            if "unitNumber" in data and data["unitNumber"]:
                sub_el = self.SubElement(premises_el, Q("xAL", "SubPremises"))
                unit_el = self.SubElement(sub_el, Q("xAL", "Number"))
                unit_el.set(Q("xAL", "Type"), "Unit")
                unit_el.text = str(data["unitNumber"])
        
        # PostCode
        if "postCode" in data and data["postCode"]:
            postcode_el = self.SubElement(xal_root, Q("xAL", "PostCode"))
            id_el = self.SubElement(postcode_el, Q("xAL", "Identifier"))
            id_el.set(Q("xAL", "Type"), "Number")
            id_el.text = str(data["postCode"])
        
        # PostBox
        if "postBox" in data and data["postBox"]:
            postbox_el = self.SubElement(xal_root, Q("xAL", "PostBox"))
            id_el = self.SubElement(postbox_el, Q("xAL", "Identifier"))
            id_el.set(Q("xAL", "Type"), "Number")
            id_el.text = str(data["postBox"])
    
    def _write_xal2(self, xal_root, data: Dict[str, Any]):
        """Write xAL 2.0 Address (CityGML 2.0)."""
        
        # Country
        if "country" in data or "countryCode" in data:
            country_el = self.SubElement(xal_root, Q("xAL", "Country"))
            
            if "countryCode" in data and data["countryCode"]:
                code_el = self.SubElement(country_el, Q("xAL", "CountryNameCode"))
                code_el.text = str(data["countryCode"])
            
            if "country" in data and data["country"]:
                name_el = self.SubElement(country_el, Q("xAL", "CountryName"))
                name_el.text = str(data["country"])
        
        # AdministrativeArea (State/Province)
        if "administrativeArea" in data and data["administrativeArea"]:
            admin_el = self.SubElement(xal_root, Q("xAL", "AdministrativeArea"))
            name_el = self.SubElement(admin_el, Q("xAL", "AdministrativeAreaName"))
            name_el.text = str(data["administrativeArea"])
        
        # Locality (City/Town)
        if "locality" in data and data["locality"]:
            locality_el = self.SubElement(xal_root, Q("xAL", "Locality"))
            name_el = self.SubElement(locality_el, Q("xAL", "LocalityName"))
            name_el.text = str(data["locality"])
        
        # DependentLocality (Suburb/District)
        if "dependentLocality" in data and data["dependentLocality"]:
            dep_el = self.SubElement(xal_root, Q("xAL", "DependentLocality"))
            name_el = self.SubElement(dep_el, Q("xAL", "DependentLocalityName"))
            name_el.text = str(data["dependentLocality"])
        
        # Thoroughfare (Street)
        if "street" in data or "streetNumber" in data:
            thoroughfare_el = self.SubElement(xal_root, Q("xAL", "Thoroughfare"))
            
            if "streetNumber" in data and data["streetNumber"]:
                number_el = self.SubElement(thoroughfare_el, Q("xAL", "ThoroughfareNumber"))
                number_el.text = str(data["streetNumber"])
            
            if "street" in data and data["street"]:
                name_el = self.SubElement(thoroughfare_el, Q("xAL", "ThoroughfareName"))
                name_el.text = str(data["street"])
        
        # Premises (Building Number/Name)
        if "buildingNumber" in data or "buildingName" in data or "unitNumber" in data:
            premises_el = self.SubElement(xal_root, Q("xAL", "Premises"))
            
            if "buildingNumber" in data and data["buildingNumber"]:
                number_el = self.SubElement(premises_el, Q("xAL", "PremisesNumber"))
                number_el.text = str(data["buildingNumber"])
            
            if "buildingName" in data and data["buildingName"]:
                name_el = self.SubElement(premises_el, Q("xAL", "PremisesName"))
                name_el.text = str(data["buildingName"])
            
            # SubPremises (Unit/Apartment)
            if "unitNumber" in data and data["unitNumber"]:
                sub_el = self.SubElement(premises_el, Q("xAL", "SubPremises"))
                unit_el = self.SubElement(sub_el, Q("xAL", "SubPremisesNumber"))
                unit_el.text = str(data["unitNumber"])
        
        # PostalCode (xAL 2.0)
        if "postCode" in data and data["postCode"]:
            postcode_el = self.SubElement(xal_root, Q("xAL", "PostalCode"))
            number_el = self.SubElement(postcode_el, Q("xAL", "PostalCodeNumber"))
            number_el.text = str(data["postCode"])
        
        # PostBox
        if "postBox" in data and data["postBox"]:
            postbox_el = self.SubElement(xal_root, Q("xAL", "PostBox"))
            number_el = self.SubElement(postbox_el, Q("xAL", "PostBoxNumber"))
            number_el.text = str(data["postBox"])


def write_xal_address(xal_root, addr_data: Dict[str, Any], version="3.0", Q_func=None, SubElement_func=None):
    """
    Convenience function to write xAL Address.
    
    Args:
        xal_root: xAL:Address XML element (parent)
        addr_data: Dictionary with address fields
        version: xAL version ("2.0" or "3.0")
        Q_func: Function to create qualified tag names
        SubElement_func: SubElement function from xml.etree or lxml
    """
    writer = XALWriter(version=version)
    writer.write_address(xal_root, addr_data, Q_func=Q_func, SubElement_func=SubElement_func)

