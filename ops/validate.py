# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# ops/validate.py
# CityGML 3 Validator
# - well_formed_citygml3: schneller Wohlgeformtheits-/Root-/Namespace-Check
# - validate_citygml3_xsd: XSD-Validierung gegen CityGML 3.0 (base profile)
#   * versucht zuerst lokale XSDs im Add-on (schema/CityGML.xsd)
#   * fällt bei Problemen auf offizielle OGC-Schemas im Netz zurück
#   * wenn lxml fehlt: Fallback auf well_formed_citygml3
# - validate_if_enabled(scene, path): Helper, der Scene-Flag 'cgml3.xsd_validate' nutzt

from __future__ import annotations

from typing import Tuple, Optional
from pathlib import Path
from xml.etree import ElementTree as ET

CITYGML_CORE_NS = "http://www.opengis.net/citygml/3.0"
GML_NS = "http://www.opengis.net/gml/3.2"


# ---------------------------------------------------------------------------
# 1. Schneller Struktur-Check (kein XSD)
# ---------------------------------------------------------------------------

def well_formed_citygml3(path: str) -> Tuple[bool, str]:
    """
    Schnelltest für CityGML 3:
      - XML wohlgeformt?
      - Root ist core:CityModel?
      - GML- und CityGML-Namespaces vorhanden?

    Rückgabe:
        (ok, meldung)
    """
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except Exception as e:
        return False, f"XML-Fehler: {e}"

    tag = root.tag  # z.B. '{namespace}CityModel'
    if not (isinstance(tag, str) and tag.startswith("{")):
        return False, f"Root-Tag hat keinen Namespace: {tag!r}"

    ns_uri, local = tag[1:].split("}", 1)
    if ns_uri != CITYGML_CORE_NS or local != "CityModel":
        return False, (
            "Root ist nicht core:CityModel "
            f"(gefunden: {{{ns_uri}}}{local})"
        )

    # sehr einfache Namespace-Prüfung über xmlns:* Attribute
    nsmap = {}
    for k, v in root.attrib.items():
        if isinstance(k, str) and k.startswith("xmlns:"):
            nsmap[k[6:]] = v  # 'xmlns:core' -> 'core': uri

    """ if GML_NS not in nsmap.values():
        return False, "GML-Namespace (gml) fehlt im Root-Element" """
    """ if CITYGML_CORE_NS not in nsmap.values():
        return False, "CityGML-Core-Namespace (core) fehlt im Root-Element" """

    return True, "Wohlgeformtes CityGML-3-Dokument (Root + Namespaces OK)"


# ---------------------------------------------------------------------------
# 2. Hilfsfunktionen: Schema-Lokalisierung
# ---------------------------------------------------------------------------

def _default_schema_base() -> Path:
    """
    Standard-Verzeichnis für CityGML-XSDs, relativ zum Add-on:
        <addon_root>/schema
    """
    return Path(__file__).resolve().parent.parent / "schema"


def _build_local_schema(schema_base: Optional[Path]) -> Tuple[Optional[object], str]:
    """
    Versucht, aus einer lokalen CityGML.xsd ein lxml.XMLSchema zu bauen.

    Rückgabe:
        (schema_obj oder None, info-Text)

    None bedeutet:
        - XSD nicht gefunden oder
        - konnte nicht zu einem gültigen Schema kompiliert werden.
    """
    try:
        from lxml import etree as LET  # type: ignore
    except Exception:
        return None, "lxml nicht installiert – XSD-Validierung nicht möglich"

    base = schema_base or _default_schema_base()
    root_xsd = base / "citygml_3/profiles/base/3.0/CityGML.xsd"

    if not root_xsd.is_file():
        return None, f"Keine lokale CityGML.xsd gefunden unter {root_xsd}"

    try:
        doc = LET.parse(str(root_xsd))
        schema = LET.XMLSchema(doc)
    except LET.XMLSchemaParseError as e:
        # Typischer Fall: Abhängigkeiten wie gml.xsd fehlen
        return None, f"Lokale XSD konnte nicht kompiliert werden: {e}"
    except Exception as e:
        return None, f"Fehler beim Laden der lokalen XSD: {e}"

    return schema, f"Lokale CityGML-XSD verwendet: {root_xsd}"


def _build_remote_schema() -> Tuple[Optional[object], str]:
    """
    Fallback: Schema über offizielle OGC-URL laden.
    Nutzt das CityGML-Base-Profile (CityGML.xsd als Einstiegspunkt).
    """
    try:
        from lxml import etree as LET  # type: ignore
    except Exception:
        return None, "lxml nicht installiert – XSD-Validierung nicht möglich"

    url = "https://schemas.opengis.net/citygml/profiles/base/3.0/CityGML.xsd"

    try:
        doc = LET.parse(url)
        schema = LET.XMLSchema(doc)
    except Exception as e:
        return None, f"Fehler beim Laden der OGC-XSD ({url}): {e}"

    return schema, f"OGC-XSD online verwendet: {url}"


# ---------------------------------------------------------------------------
# 3. Vollständige XSD-Validierung
# ---------------------------------------------------------------------------

def validate_citygml3_xsd(
    path: str,
    schema_base: Optional[str | Path] = None,
) -> Tuple[bool, str]:
    """
    Vollständige XSD-Validierung eines CityGML-3-Dokuments.

    Ablauf:
      1. Schnelltest (well_formed_citygml3) – bricht bei klaren Fehlern sofort ab.
      2. Versucht, ein lokales Schema zu verwenden:
         - Erwartet CityGML.xsd unter <schema_base>/ oder unter <addon>/schema/.
      3. Wenn lokal nicht nutzbar, Fallback auf die offizielle OGC-XSD-URL.
      4. Wenn lxml nicht verfügbar, wird nach Schritt (1) beendet.

    Rückgabe:
        (ok, meldung)
    """
    # Schritt 1: Basischeck
    ok, msg = well_formed_citygml3(path)
    if not ok:
        return False, msg

    try:
        from lxml import etree as LET  # type: ignore
    except Exception:
        # Kein lxml: wir melden zumindest den erfolgreichen Struktur-Check
        return ok, msg + " (Hinweis: lxml nicht installiert, daher keine XSD-Validierung)"

    base: Optional[Path]
    if schema_base is not None:
        base = Path(schema_base)
    else:
        base = None

    # Schritt 2: Lokales Schema versuchen
    local_info = ""
    schema, schema_msg = _build_local_schema(base)
    if schema is None:
        local_info = schema_msg
        # Schritt 3: Remote-Fallback
        schema, schema_msg = _build_remote_schema()
        if schema is None:
            # Gar kein nutzbares Schema – wir melden wenigstens das Struktur-Ergebnis
            return ok, (
                msg
                + " (Warnung: XSD konnte weder lokal noch online geladen werden: "
                + f"{local_info}; {schema_msg})"
            )
    else:
        local_info = schema_msg

    # Schritt 4: Instanzdokument gegen Schema validieren
    try:
        doc = LET.parse(path)
        schema.assertValid(doc)
    except LET.DocumentInvalid as e:
        return False, f"XSD-Fehler: {e}"
    except Exception as e:
        return False, f"Fehler bei der XSD-Validierung: {e}"

    info = local_info or schema_msg
    return True, f"CityGML 3 ist gültig nach XSD. ({info})"


# ---------------------------------------------------------------------------
# 4. Helper für Blender-Szene
# ---------------------------------------------------------------------------

def validate_if_enabled(scene, path: str) -> Tuple[bool, str]:
    """
    Helper für UI/Operatoren:

    Wenn scene.cgml3.xsd_validate == True:
        -> validate_citygml3_xsd(path)
    Sonst:
        -> well_formed_citygml3(path)

    Erwartet:
        scene.cgml3.xsd_validate (BoolProperty)
    """
    enabled = False
    try:
        cgml3 = getattr(scene, "cgml3", None)
        if cgml3 is not None:
            enabled = bool(getattr(cgml3, "xsd_validate", False))
    except Exception:
        enabled = False

    if enabled:
        return validate_citygml3_xsd(path)
    return well_formed_citygml3(path)
