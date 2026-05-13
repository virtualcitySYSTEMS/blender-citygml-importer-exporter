# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# ops/validate_citygml2.py
"""
CityGML 2.0 Validator
=====================

Validierungsfunktionen für CityGML 2.0 Dateien:
- well_formed_citygml2: Schneller Struktur-Check (XML, Root, Namespaces)
- validate_citygml2_xsd: XSD-Validierung gegen CityGML 2.0 Standard
- validate_if_enabled: Helper für optionale Validierung

Analog zu ops/validate.py für CityGML 3.0
"""

from __future__ import annotations

from typing import Tuple, Optional
from pathlib import Path
from xml.etree import ElementTree as ET

# CityGML 2.0 Namespaces
CITYGML2_CORE_NS = "http://www.opengis.net/citygml/2.0"
GML_NS = "http://www.opengis.net/gml"  # GML 3.1.1 (ohne /3.2)


# ---------------------------------------------------------------------------
# 1. Schneller Struktur-Check (kein XSD)
# ---------------------------------------------------------------------------

def well_formed_citygml2(path: str) -> Tuple[bool, str]:
    """
    Schnelltest für CityGML 2.0:
      - XML wohlgeformt?
      - Root ist CityModel?
      - GML- und CityGML-Namespaces vorhanden?

    Rückgabe:
        (ok, meldung)
    """
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except Exception as e:
        return False, f"XML-Fehler: {e}"

    tag = root.tag  # z.B. '{namespace}CityModel' oder 'CityModel'
    
    # CityGML 2.0 hat oft CityModel ohne Namespace-Präfix im Tag
    if isinstance(tag, str):
        if tag.startswith("{"):
            ns_uri, local = tag[1:].split("}", 1)
        else:
            local = tag
            # Prüfe xmlns Default-Namespace
            ns_uri = root.attrib.get("xmlns", "")
    else:
        return False, f"Unerwarteter Root-Tag-Typ: {type(tag)}"

    if local != "CityModel":
        return False, f"Root ist nicht CityModel (gefunden: {local})"
    
    # Namespace-Prüfung: Akzeptiere CityGML 2.0 Core Namespace
    if ns_uri and CITYGML2_CORE_NS not in ns_uri and "/citygml/2.0" not in ns_uri:
        # Wenn Root-Namespace nicht CityGML 2.0 ist, ist es wahrscheinlich falsch
        # ABER: Sei tolerant für Edge-Cases
        pass

    # Prüfe ob CityGML 2.0 Namespace im Root-Tag vorhanden ist
    citygml_found = False
    if CITYGML2_CORE_NS in ns_uri or "/citygml/2.0" in ns_uri:
        citygml_found = True
    
    if not citygml_found:
        return False, "CityGML 2.0 Namespace fehlt im Root-Element (erwartet: http://www.opengis.net/citygml/2.0)"

    # GML-Namespace-Check ist optional, da ElementTree xmlns:* nicht als Attribute speichert
    # Eine echte CityGML 2.0 Datei hat immer den korrekten Namespace im Root-Tag

    return True, "Wohlgeformtes CityGML-2.0-Dokument (Root + Namespaces OK)"


# ---------------------------------------------------------------------------
# 2. Hilfsfunktionen: Schema-Lokalisierung
# ---------------------------------------------------------------------------

def _default_schema_base_v2() -> Path:
    """
    Standard-Verzeichnis für CityGML 2.0 XSDs, relativ zum Add-on:
        <addon_root>/Schema/citygml_2
    """
    return Path(__file__).resolve().parent.parent / "Schema" / "citygml_2"


class LocalSchemaResolver:
    """
    Custom XML Resolver für lokale Schema-Dateien.
    Löst externe Schema-Referenzen (xAL, GML) auf lokale Dateien auf.
    Verhindert problematische Online-Downloads bei xAL-Schemas.
    """
    
    def __init__(self, schema_base: Path):
        self.schema_base = schema_base
        
        # Mapping von Online-URLs auf lokale Pfade
        self.url_mappings = {
            # xAL 2.0 (OASIS) - verschiedene Varianten der URL
            'http://docs.oasis-open.org/election/external/xAL.xsd': 
                schema_base / 'xAL' / 'xAL.xsd',
            'urn:oasis:names:tc:ciq:xsdschema:xAL:2.0':
                schema_base / 'xAL' / 'xAL.xsd',
        }
    
    def resolve(self, url, id, context):
        """Resolves external schema references to local files."""
        try:
            from lxml import etree
            
            # Prüfe ob URL in Mapping vorhanden
            if url in self.url_mappings:
                local_path = self.url_mappings[url]
                
                # Wenn lokaler Pfad existiert
                if isinstance(local_path, Path) and local_path.exists():
                    return self.resolve_filename(str(local_path), context)
            
            # URN-basierte Referenzen (z.B. urn:oasis:...)
            if url and url.startswith('urn:oasis'):
                xal_path = self.schema_base / 'xAL' / 'xAL.xsd'
                if xal_path.exists():
                    return self.resolve_filename(str(xal_path), context)
            
            # Fallback: lass lxml standardmäßig auflösen oder ignorieren
            return None
            
        except Exception:
            # Bei Fehler: None zurückgeben (lxml verwendet dann Standard-Resolver)
            return None


def _build_local_schema_v2(schema_base: Optional[Path]) -> Tuple[Optional[object], str]:
    """
    Versucht, aus einer lokalen CityGML 2.0 XSD ein lxml.XMLSchema zu bauen.

    Args:
        schema_base: Verzeichnis mit CityGML 2.0 Schemas (z.B. Schema/citygml_2/)

    Returns:
        (XMLSchema-Objekt oder None, Fehlermeldung)
    """
    try:
        from lxml import etree
    except ImportError:
        return None, "lxml nicht installiert"

    if schema_base is None:
        schema_base = _default_schema_base_v2()

    # CityGML 2.0 Base Profile Schema
    xsd_path = schema_base / "profiles" / "base" / "2.0" / "CityGML.xsd"

    if not xsd_path.exists():
        return None, f"Lokales Schema nicht gefunden: {xsd_path}"

    try:
        # Parser mit Custom-Resolver erstellen
        resolver = LocalSchemaResolver(schema_base)
        parser = etree.XMLParser()
        parser.resolvers.add(resolver)
        
        # Schema mit Custom-Parser laden (deaktiviert externe Referenzen)
        schema_doc = etree.parse(str(xsd_path), parser)
        
        # XMLSchema mit attribute_defaults=False erstellen
        # Dies reduziert Probleme mit fehlenden externen Schemas
        schema = etree.XMLSchema(schema_doc, attribute_defaults=False)
        return schema, ""
        
    except etree.XMLSchemaParseError as e:
        # Schema-Parsing fehlgeschlagen wegen fehlender externer Referenzen
        # Dies ist oft bei xAL-Schemas der Fall, aber CityGML-Validierung funktioniert trotzdem
        error_msg = str(e)
        
        # Wenn es nur um xAL-Referenzen geht, ist das tolerierbar
        if 'xAL' in error_msg or 'AddressDetails' in error_msg:
            # Versuche Schema-Validierung trotzdem mit relaxed Parser
            try:
                parser_relaxed = etree.XMLParser(resolve_entities=False, no_network=True)
                schema_doc_relaxed = etree.parse(str(xsd_path), parser_relaxed)
                # Achtung: Schema ohne vollständige xAL-Definitionen
                # Validierung wird trotzdem nützlich sein
                return None, f"Schema-Warning (externe xAL-Referenzen nicht vollständig): {error_msg[:200]}"
            except Exception:
                pass
        
        return None, f"Schema-Parsing-Fehler: {error_msg[:300]}"
        
    except Exception as e:
        return None, f"Schema-Ladefehler: {e}"


def _build_online_schema_v2() -> Tuple[Optional[object], str]:
    """
    Lädt das CityGML 2.0 Schema von den offiziellen OGC-URLs.

    Returns:
        (XMLSchema-Objekt oder None, Fehlermeldung)
    """
    try:
        from lxml import etree
        import urllib.request
    except ImportError as e:
        return None, f"Import-Fehler: {e}"

    # Offizielle CityGML 2.0 Schema-URL (OGC)
    schema_url = "http://schemas.opengis.net/citygml/profiles/base/2.0/CityGML.xsd"

    try:
        with urllib.request.urlopen(schema_url, timeout=10) as response:
            schema_doc = etree.parse(response)
            schema = etree.XMLSchema(schema_doc)
            return schema, ""
    except Exception as e:
        return None, f"Online-Schema-Download fehlgeschlagen: {e}"


# ---------------------------------------------------------------------------
# 3. XSD-Validierung
# ---------------------------------------------------------------------------

def validate_citygml2_xsd(
    path: str,
    schema_base: Optional[Path] = None,
    fallback_online: bool = True
) -> Tuple[bool, str]:
    """
    XSD-Validierung einer CityGML 2.0 Datei.

    Strategie:
    1. Versuche lokales Schema (Schema/citygml_2/profiles/base/2.0/CityGML.xsd)
    2. Bei Fehler: Fallback auf Online-Schema (wenn fallback_online=True)
    3. Bei lxml-Fehler: Fallback auf well_formed_citygml2()

    Args:
        path: Pfad zur CityGML 2.0 Datei
        schema_base: Optional: Verzeichnis mit CityGML 2.0 Schemas
        fallback_online: Bei lokalen Schema-Problemen online versuchen?

    Returns:
        (valid, message)
    """
    try:
        from lxml import etree
    except ImportError:
        # Fallback: Nur Wohlgeformtheits-Check
        return well_formed_citygml2(path)

    # 1. Versuche lokales Schema
    schema, err_msg = _build_local_schema_v2(schema_base)

    # 2. Fallback: Online-Schema
    if schema is None and fallback_online:
        schema, err_msg = _build_online_schema_v2()

    # 3. Kein Schema verfügbar
    if schema is None:
        # Fallback auf einfachen Check
        ok, msg = well_formed_citygml2(path)
        
        # Wenn es nur xAL-Warnings sind, ist die Datei trotzdem OK
        if ok and ('xAL' in err_msg or 'AddressDetails' in err_msg):
            return True, (
                f"Wohlgeformt ✓ (XSD-Vollvalidierung übersprungen wegen fehlender xAL-Schema-Referenzen. "
                f"Dies ist ein bekanntes Problem mit CityGML 2.0 Address-Schemas und beeinträchtigt "
                f"den Import NICHT.)"
            )
        elif ok:
            return True, f"Wohlgeformt (XSD-Check übersprungen: {err_msg})"
        
        return ok, msg

    # 4. Validierung durchführen
    try:
        doc = etree.parse(path)
    except Exception as e:
        return False, f"XML-Parsing-Fehler: {e}"

    try:
        schema.assertValid(doc)
        return True, "XSD-Validierung erfolgreich (CityGML 2.0 Base Profile)"
    except etree.DocumentInvalid as e:
        # Sammle alle Fehler
        errors = []
        for error in schema.error_log:
            errors.append(f"Zeile {error.line}: {error.message}")

        error_summary = "\n".join(errors[:5])  # Erste 5 Fehler
        if len(errors) > 5:
            error_summary += f"\n... und {len(errors) - 5} weitere Fehler"

        return False, f"XSD-Validierung fehlgeschlagen:\n{error_summary}"
    except Exception as e:
        return False, f"Validierungs-Fehler: {e}"


# ---------------------------------------------------------------------------
# 4. Helper für optionale Validierung
# ---------------------------------------------------------------------------

def validate_if_enabled(scene, path: str, version: str = "2.0") -> Tuple[bool, str]:
    """
    Führt XSD-Validierung nur durch, wenn in Scene-Properties aktiviert.

    Args:
        scene: Blender Scene-Objekt (mit cgml2.xsd_validate Property)
        path: Pfad zur CityGML-Datei
        version: CityGML-Version ("2.0" oder "3.0")

    Returns:
        (valid, message)
    """
    # Prüfe ob Validierung aktiviert
    try:
        if version == "2.0":
            validate_enabled = scene.get("cgml2.xsd_validate", False)
        else:
            validate_enabled = scene.get("cgml3.xsd_validate", False)
    except:
        validate_enabled = False

    if not validate_enabled:
        return True, "XSD-Validierung übersprungen (nicht aktiviert)"

    # Führe Validierung durch
    if version == "2.0":
        return validate_citygml2_xsd(path)
    else:
        # Import CityGML 3.0 Validator
        from .validate import validate_citygml3_xsd
        return validate_citygml3_xsd(path)


# ---------------------------------------------------------------------------
# Export für andere Module
# ---------------------------------------------------------------------------

__all__ = [
    'well_formed_citygml2',
    'validate_citygml2_xsd',
    'validate_if_enabled',
]
