# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Dynamic Namespace Management for CityGML Import
================================================

Dynamische Erkennung von Namespaces aus CityGML-Dateien statt hardcodierter Listen.
Unterstützt CityGML 2.0, 3.0 und Application Domain Extensions (ADEs).

Vorteile:
- Automatische Erkennung neuer Module
- ADE-Unterstützung ohne Code-Änderungen
- Zukunftssicher für CityGML-Updates
"""

from typing import Any, Dict, Set, Tuple, Optional

try:
    from lxml import etree
except ImportError:
    import xml.etree.ElementTree as etree
import re


class DynamicNamespaceManager:
    """
    Verwaltet Namespaces dynamisch basierend auf der Input-Datei.
    
    Ersetzt hardcodierte NS-Dictionaries durch dynamische Extraktion
    aus dem XML root.nsmap.
    """
    
    def __init__(self, root_element: Any):
        """
        Initialisiert Manager mit XML Root-Element.
        
        Args:
            root_element: XML Root (CityModel)
        """
        self.root = root_element
        self.nsmap = root_element.nsmap or {}
        
        # Erkenne Version
        from .version_detection import detect_citygml_version
        self.version = detect_citygml_version(root_element)
        
        # Extrahiere Namespace-Kategorien
        self.gml_ns = self._extract_gml_namespace()
        self.core_ns = self._extract_core_namespace()
        self.module_ns = self._extract_module_namespaces()
        self.ade_ns = self._detect_ades()
        self.external_ns = self._extract_external_namespaces()
        
    def _extract_gml_namespace(self) -> Optional[str]:
        """Findet den GML-Namespace."""
        for uri in self.nsmap.values():
            if uri and 'opengis.net/gml' in uri:
                return uri
        return None
    
    def _extract_core_namespace(self) -> Optional[str]:
        """Findet den CityGML Core-Namespace."""
        # CityGML 3.0: http://www.opengis.net/citygml/3.0
        # CityGML 2.0: http://www.opengis.net/citygml/2.0
        pattern = r'citygml/[23]\.[0-9]$'
        
        for uri in self.nsmap.values():
            if uri and re.search(pattern, uri):
                return uri
        return None
    
    def _extract_module_namespaces(self) -> Dict[str, str]:
        """
        Extrahiert alle CityGML-Modul-Namespaces dynamisch.
        
        Returns:
            Dict mit {module_name: namespace_uri}
        """
        modules = {}
        
        # Pattern für CityGML-Module:
        # CityGML 3.0: http://www.opengis.net/citygml/building/3.0
        # CityGML 2.0: http://www.opengis.net/citygml/building/2.0
        pattern = r'citygml/([^/]+)/[23]\.[0-9]$'
        
        for prefix, uri in self.nsmap.items():
            if uri:
                match = re.search(pattern, uri)
                if match:
                    module_name = match.group(1)
                    modules[module_name] = uri
        
        return modules
    
    def _detect_ades(self) -> Dict[str, str]:
        """
        Erkennt Application Domain Extensions (ADEs) dynamisch.
        
        ADEs sind Namespaces außerhalb der Standard-CityGML/GML-Namespaces,
        die tatsächlich in der Datei verwendet werden.
        
        Returns:
            Dict mit {prefix: namespace_uri} für ADEs
        """
        ades = {}
        
        # Standard-Namespaces die KEINE ADEs sind
        standard_patterns = [
            r'opengis\.net',
            r'w3\.org',
            r'oasis-open\.org',
            r'oasis.*names.*tc.*ciq',  # xAL
        ]
        
        for prefix, uri in self.nsmap.items():
            if not uri:
                continue
            
            # Prüfe ob es ein Standard-Namespace ist
            is_standard = any(re.search(pattern, uri) for pattern in standard_patterns)
            
            if not is_standard:
                # Prüfe ob Elemente in diesem Namespace existieren
                try:
                    elements = self.root.findall(f".//{{{uri}}}*")
                    if elements:
                        ades[prefix or 'default'] = uri
                except Exception:
                    pass
        
        return ades
    
    def _extract_external_namespaces(self) -> Dict[str, str]:
        """
        Extrahiert externe Standard-Namespaces (xAL, xlink, etc.).
        
        Returns:
            Dict mit {prefix: namespace_uri}
        """
        external = {}
        
        external_patterns = {
            'xAL': r'oasis.*xal',
            'xlink': r'w3\.org.*xlink',
            'xsi': r'XMLSchema-instance',
        }
        
        for prefix, uri in self.nsmap.items():
            if uri:
                for name, pattern in external_patterns.items():
                    if re.search(pattern, uri, re.IGNORECASE):
                        external[name] = uri
                        break
        
        return external
    
    def get_all_citygml_namespaces(self) -> Set[str]:
        """
        Gibt alle erkannten CityGML-relevanten Namespace-URIs zurück.
        
        Returns:
            Set aller CityGML Core + Module Namespace-URIs
        """
        namespaces = set()
        
        if self.core_ns:
            namespaces.add(self.core_ns)
        
        namespaces.update(self.module_ns.values())
        
        return namespaces
    
    def get_all_namespaces_including_ades(self) -> Set[str]:
        """
        Gibt alle Namespaces inklusive ADEs zurück.
        
        Returns:
            Set aller Namespace-URIs (CityGML + ADEs)
        """
        namespaces = self.get_all_citygml_namespaces()
        namespaces.update(self.ade_ns.values())
        return namespaces
    
    def is_citygml_namespace(self, uri: str) -> bool:
        """
        Prüft ob URI ein CityGML-Namespace ist (Core oder Module).
        
        Args:
            uri: Namespace URI
            
        Returns:
            True wenn CityGML-Namespace
        """
        return uri in self.get_all_citygml_namespaces()
    
    def is_ade_namespace(self, uri: str) -> bool:
        """
        Prüft ob URI ein ADE-Namespace ist.
        
        Args:
            uri: Namespace URI
            
        Returns:
            True wenn ADE
        """
        return uri in self.ade_ns.values()
    
    def get_namespace_dict(self) -> Dict[str, str]:
        """
        Erstellt ein Namespace-Dictionary kompatibel mit lxml findall().
        
        Returns:
            Dict für XPath-Queries
        """
        ns_dict = {}
        
        # GML
        if self.gml_ns:
            ns_dict['gml'] = self.gml_ns
        
        # Core
        if self.core_ns:
            ns_dict['core'] = self.core_ns
        
        # Module - verwende Kurznamen
        module_shortcuts = {
            'appearance': 'app',
            'building': 'bldg',
            'bridge': 'brid',
            'construction': 'con',
            'relief': 'dem',
            'cityfurniture': 'frn',
            'cityobjectgroup': 'grp',
            'generics': 'gen',
            'landuse': 'luse',
            'pointcloud': 'pcl',
            'texturedsurface': 'tex',
            'transportation': 'tran',
            'tunnel': 'tun',
            'vegetation': 'veg',
            'versioning': 'vers',
            'dynamizer': 'dyn',
            'waterbody': 'wtr',
        }
        
        for module_name, uri in self.module_ns.items():
            shortcut = module_shortcuts.get(module_name, module_name[:4])
            ns_dict[shortcut] = uri
        
        # External
        ns_dict.update(self.external_ns)
        
        # ADEs - behalte Original-Präfixe
        ns_dict.update(self.ade_ns)
        
        return ns_dict
    
    def find_all_features_dynamic(self, feature_type_filter: Optional[Set[str]] = None) -> list:
        """
        Findet alle Feature-Elemente dynamisch ohne hardcodierte Listen.
        
        Args:
            feature_type_filter: Optional Set von Feature-Typ-Namen zum Filtern
            
        Returns:
            Liste von Feature-Elementen
        """
        features = []
        
        # Alle Namespaces durchsuchen (CityGML + ADEs)
        all_ns = self.get_all_namespaces_including_ades()
        
        # Sammle cityObjectMember/featureMember
        member_elements = []
        for elem in self.root:
            tag = elem.tag if isinstance(elem.tag, str) else ""
            localname = tag.split("}")[-1] if "}" in tag else tag
            
            if localname in ("cityObjectMember", "member", "featureMember"):
                member_elements.append(elem)
        
        # Extrahiere Features aus Members
        for member in member_elements:
            for child in member:
                if not isinstance(child.tag, str) or not child.tag.startswith("{"):
                    continue
                
                ns_uri = child.tag.split("}")[0].strip("{")
                localname = child.tag.split("}")[-1]
                
                # Prüfe ob in erlaubtem Namespace
                if ns_uri in all_ns:
                    # Optionaler Feature-Typ-Filter
                    if feature_type_filter and localname not in feature_type_filter:
                        continue
                    
                    features.append(child)
        
        return features
    
    def find_all_subfeatures_dynamic(self, parent_element: Any) -> list:
        """
        Findet verschachtelte Subfeatures dynamisch.
        
        Erkennt Features anhand von gml:id statt hardcodierten XPath-Listen.
        
        Args:
            parent_element: Parent Feature-Element
            
        Returns:
            Liste von Subfeature-Elementen
        """
        subfeatures = []
        gml_id_attr = f"{{{self.gml_ns}}}id" if self.gml_ns else "{http://www.opengis.net/gml}id"
        
        all_ns = self.get_all_namespaces_including_ades()
        
        # Iteriere durch alle Nachkommen
        for elem in parent_element.iter():
            if not isinstance(elem.tag, str) or not elem.tag.startswith("{"):
                continue
            
            # Überspringe Parent selbst
            if elem is parent_element:
                continue
            
            ns_uri = elem.tag.split("}")[0].strip("{")
            
            # Prüfe ob in CityGML/ADE Namespace
            if ns_uri not in all_ns:
                continue
            
            # Hat Element eine gml:id? → Feature
            if elem.get(gml_id_attr):
                # Vermeide doppelte Erfassung von bereits als Top-Level erfassten Features
                if elem not in subfeatures:
                    subfeatures.append(elem)
        
        return subfeatures
    
    def get_info_string(self) -> str:
        """
        Erstellt Info-String über erkannte Namespaces.
        
        Returns:
            Formatierter String für Logging
        """
        lines = [
            "=" * 60,
            "Dynamic Namespace Detection",
            "=" * 60,
            f"CityGML Version: {self.version}",
            f"GML Namespace: {self.gml_ns or 'NOT FOUND'}",
            f"Core Namespace: {self.core_ns or 'NOT FOUND'}",
            "",
            f"Detected Modules ({len(self.module_ns)}):",
        ]
        
        for module, uri in sorted(self.module_ns.items()):
            lines.append(f"  - {module}: {uri}")
        
        if self.ade_ns:
            lines.append("")
            lines.append(f"Detected ADEs ({len(self.ade_ns)}):")
            for prefix, uri in sorted(self.ade_ns.items()):
                lines.append(f"  - {prefix}: {uri}")
        else:
            lines.append("")
            lines.append("No ADEs detected")
        
        lines.append("=" * 60)
        
        return "\n".join(lines)


def extract_citygml_namespaces(root: Any) -> Dict[str, str]:
    """
    Convenience-Funktion: Extrahiert alle CityGML-Namespaces aus XML.
    
    Args:
        root: XML Root-Element
        
    Returns:
        Dict mit Namespace-Präfix → URI Mapping
    """
    manager = DynamicNamespaceManager(root)
    return manager.get_namespace_dict()


def get_allowed_namespace_uris(root: Any) -> Tuple[str, ...]:
    """
    Convenience-Funktion: Gibt Tuple aller erlaubten Namespace-URIs zurück.
    
    Für Kompatibilität mit bestehendem Code der `allowed_ns` Tupel verwendet.
    
    Args:
        root: XML Root-Element
        
    Returns:
        Tuple von Namespace-URIs
    """
    manager = DynamicNamespaceManager(root)
    return tuple(manager.get_all_namespaces_including_ades())
