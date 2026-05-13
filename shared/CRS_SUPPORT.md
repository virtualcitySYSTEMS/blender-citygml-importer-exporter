# CRS Support - Koordinatenreferenzsysteme

## Übersicht

Das Plugin unterstützt jetzt eine erweiterte Erkennung von Koordinatenreferenzsystemen (CRS), einschließlich der speziellen ADV-URN-Syntax, die in deutschen CityGML-Dateien verwendet wird.

## Unterstützte CRS-Formate

### 1. Standard EPSG-Codes
```xml
srsName="EPSG:25832"
srsName="epsg:4326"
```

### 2. OGC URN Format
```xml
srsName="urn:ogc:def:crs:EPSG::25832"
srsName="urn:x-ogc:def:crs:EPSG:6.18.3:4326"
```

### 3. HTTP URLs
```xml
srsName="http://www.opengis.net/gml/srs/epsg.xml#3857"
srsName="http://www.opengis.net/def/crs/EPSG/0/4258"
```

### 4. Compound CRS (mit vertikalem Bezug)
```xml
srsName="urn:ogc:def:crs,crs:EPSG::25832,crs:EPSG::5783"
srsName="urn:ogc:def:crs,crs:EPSG:6.12:31466,crs:EPSG:6.12:5783"
```

### 5. ADV-URN Format (NEU!)
Das Plugin erkennt jetzt die spezielle ADV (Arbeitsgemeinschaft der Vermessungsverwaltungen) URN-Syntax:

```xml
srsName="urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH"
srsName="urn:adv:crs:ETRS89_UTM33*DE_DHHN92_NH"
srsName="urn:adv:crs:ETRS89_UTM32*DE_DHHN2016_NH"
```

## ADV-URN Mapping-Tabelle

Das Plugin enthält eine interne Mapping-Tabelle für bekannte ADV-URNs:

| ADV-URN | EPSG-Code | Beschreibung |
|---------|-----------|--------------|
| `urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH` | `EPSG:25832+5783` | ETRS89/UTM Zone 32N + DHHN92 |
| `urn:adv:crs:ETRS89_UTM33*DE_DHHN92_NH` | `EPSG:25833+5783` | ETRS89/UTM Zone 33N + DHHN92 |
| `urn:adv:crs:ETRS89_UTM31*DE_DHHN92_NH` | `EPSG:25831+5783` | ETRS89/UTM Zone 31N + DHHN92 |
| `urn:adv:crs:ETRS89_UTM32*DE_DHHN2016_NH` | `EPSG:25832` | ETRS89/UTM Zone 32N, Hoehenbezug DHHN2016 wird separat behandelt |
| `urn:adv:crs:ETRS89_UTM33*DE_DHHN2016_NH` | `EPSG:25833` | ETRS89/UTM Zone 33N, Hoehenbezug DHHN2016 wird separat behandelt |
| `urn:adv:crs:ETRS89_UTM32` | `EPSG:25832` | ETRS89/UTM Zone 32N (2D) |
| `urn:adv:crs:ETRS89_UTM33` | `EPSG:25833` | ETRS89/UTM Zone 33N (2D) |
| `urn:adv:crs:DE_DHDN_3GK2*DE_DHN92_NH` | `EPSG:31466+5783` | Gauss-Krüger Zone 2 + DHHN92 |
| `urn:adv:crs:DE_DHDN_3GK3*DE_DHN92_NH` | `EPSG:31467+5783` | Gauss-Krüger Zone 3 + DHHN92 |
| `urn:adv:crs:DE_DHDN_3GK4*DE_DHN92_NH` | `EPSG:31468+5783` | Gauss-Krüger Zone 4 + DHHN92 |

## Verwendung in Blender

Nach dem Import wird das erkannte CRS in der Blender-Szene gespeichert:

```python
bpy.data.worlds["World"]["CRS"]
```

**Vorher (bei unbekannten ADV-URNs):**
```python
>>> bpy.data.worlds["World"]["CRS"]
"Unknown"
```

**Jetzt (mit ADV-URN-Unterstützung):**
```python
>>> bpy.data.worlds["World"]["CRS"]
"EPSG:25832+5783"
```

## Technische Details

### Haupt-Funktionen

Die CRS-Erkennung wird zentral in `shared/materials_common.py` implementiert:

#### `normalize_crs_to_epsg(srs_name: str) -> str`
Extrahiert und normalisiert CRS-Informationen in EPSG-Format.

**Rückgabewerte:**
- `"EPSG:25832"` - Einzelner EPSG-Code
- `"EPSG:25832+5783"` - Compound CRS (horizontal + vertikal)
- `"Unknown CRS"` - Nicht erkannt

**Wichtig für Material-Properties:** 
Beim Speichern in Material-Properties sollte `"Unknown CRS"` gefiltert werden:
```python
epsg = normalize_crs_to_epsg(srs_poly)
if epsg and epsg != "Unknown CRS":
    mat["EPSG"] = epsg
```

**Beispiel:**
```python
from shared.materials_common import normalize_crs_to_epsg

# ADV-URN
result = normalize_crs_to_epsg("urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH")
# Ergebnis: "EPSG:25832+5783"

# Standard EPSG
result = normalize_crs_to_epsg("EPSG:25832")
# Ergebnis: "EPSG:25832"

# Compound CRS
result = normalize_crs_to_epsg("urn:ogc:def:crs,crs:EPSG::25832,crs:EPSG::5783")
# Ergebnis: "EPSG:25832+5783"
```

#### `extract_vertical_epsg(srs_name: str) -> str`
Extrahiert nur die vertikale EPSG-Komponente aus einem Compound-CRS.

**Rückgabewerte:**
- `"5783"` - Vertikale EPSG-Code
- `""` - Kein vertikales CRS vorhanden

**Beispiel:**
```python
from shared.materials_common import extract_vertical_epsg

result = extract_vertical_epsg("urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH")
# Ergebnis: "5783"
```

### Erkennungs-Strategie

Die Funktion `normalize_crs_to_epsg` verwendet folgende Priorität:

1. **ADV-URN Exact Match** (höchste Priorität)
   - Prüft gegen bekannte ADV-URN Mapping-Tabelle

2. **ADV-URN Partial Match** (case-insensitive)
   - Sucht nach ADV-URN Mustern im String

3. **Standard EPSG-Extraktion**
   - Regex-basierte Suche nach EPSG-Codes
   - Unterstützt alle gängigen OGC/ISO-Formate

## Export-Funktionalität

Beim Export von CityGML-Dateien verwendet das Plugin automatisch das in `World["CRS"]` gespeicherte CRS:

```python
# In Export-Operators:
world = bpy.data.worlds.get("World")
if world and "CRS" in world:
    srsName = world["CRS"]
```

Das Plugin kann EPSG-Codes in verschiedenen Formaten ausgeben, abhängig von der gewählten Export-Option.

## Erweiterung der Mapping-Tabelle

Um weitere ADV-URNs oder andere spezielle CRS-Bezeichnungen hinzuzufügen, bearbeiten Sie die `ADV_CRS_MAPPING` Dictionary in `shared/materials_common.py`:

```python
ADV_CRS_MAPPING = {
    # Ihre eigenen Mappings hinzufügen:
    "urn:adv:crs:CUSTOM_CRS_NAME": "EPSG:12345+6789",
    # ...
}
```

## Bekannte CRS in Deutschland

### ETRS89 / UTM (aktuell, empfohlen)
- **Zone 31N**: `EPSG:25831` (westliches Deutschland)
- **Zone 32N**: `EPSG:25832` (mittleres Deutschland, am häufigsten)
- **Zone 33N**: `EPSG:25833` (östliches Deutschland)

### Höhenbezugssysteme
- **DHHN92**: `EPSG:5783` (Deutsches Haupthöhennetz 1992)
- **DHHN2016**: `EPSG:7837` (Deutsches Haupthöhennetz 2016, aktuell)

### Gauss-Krüger (historisch, Legacy)
- **Zone 2**: `EPSG:31466`
- **Zone 3**: `EPSG:31467`
- **Zone 4**: `EPSG:31468`
- **Zone 5**: `EPSG:31469`

## Fehlerbehebung

### Problem: `World["CRS"]` zeigt "Unknown"

**Ursache:** Das CRS-Format in der GML-Datei wird nicht erkannt.

**Lösung:**
1. Prüfen Sie das `srsName`-Attribut in Ihrer GML-Datei
2. Wenn es eine ADV-URN ist, die nicht in der Mapping-Tabelle steht, fügen Sie sie hinzu
3. Erstellen Sie ein Issue auf GitHub mit der verwendeten CRS-Schreibweise

### Problem: Import funktioniert, aber Koordinaten sind falsch

**Ursache:** Das CRS wurde zwar erkannt, aber die Koordinatentransformation ist fehlerhaft.

**Lösung:**
- Stellen Sie sicher, dass das erkannte EPSG-CRS korrekt ist
- Prüfen Sie den `Reference Origin` in den Blender-Szene-Eigenschaften
- Bei Compound-CRS: Prüfen Sie, ob der vertikale Teil korrekt erkannt wurde

## Referenzen

- [EPSG.io](https://epsg.io/) - EPSG-Code-Datenbank
- [AdV-Arbeitskreis Koordinatenreferenzsysteme](http://www.adv-online.de/)
- [OGC URN Policy](http://www.opengeospatial.org/ogcUrnPolicy)
- [GeoInfoDok (AdV-Standard)](http://www.adv-online.de/GeoInfoDok/)
