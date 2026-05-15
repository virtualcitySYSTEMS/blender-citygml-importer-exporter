<a id="deutsch"></a>

# 🇩🇪 Deutsch

---

# VCS 3DCityDB Importer/Exporter – Blender Add-on

Ein Blender-Add-on zum Import und Export von **CityGML 2.0** und **CityGML 3.0** Dateien sowie zur direkten Anbindung an die **3DCityDB** (v4 und v5).

Entwickelt von [Virtual City Systems](https://vc.systems).

<a id="inhaltsverzeichnis"></a>

## Inhaltsverzeichnis

- [Deutsch](#deutsch)
  - [Features](#features)
  - [Systemvoraussetzungen](#systemvoraussetzungen)
  - [Installation](#installation)
    - [Add-on herunterladen](#1-add-on-herunterladen)
    - [In Blender installieren](#2-in-blender-installieren)
    - [Add-on-Einstellungen konfigurieren](#3-add-on-einstellungen-konfigurieren-optional)
  - [Verwendung](#verwendung)
    - [CityGML-Datei importieren](#citygml-datei-importieren)
    - [CityGML-Datei exportieren](#citygml-datei-exportieren)
    - [3DCityDB Import/Export](#3dcitydb-importexport)
    - [Filter](#filter)
    - [ModelTyper](#modeltyper--semantische-oberflächenklassifizierung)
    - [Openings Cutter](#openings-cutter--türen-und-fenster-erzeugen)
    - [Join Object Parts](#join-object-parts--teile-zusammenführen)
    - [Assign Object Part](#assign-object-part--gebäudeteile-semantisch-zuweisen)
    - [Viewport Performance](#viewport-performance)
    - [Headless-Betrieb](#headless-betrieb-cli)
  - [Georeferenzierte Orthophotos](#georeferenzierte-orthophotos)
    - [Workflow: CityGML + Orthophoto](#workflow-citygml--orthophoto)
  - [Projektstruktur](#projektstruktur)
  - [Abhängigkeiten](#abhängigkeiten)
  - [Lizenz](#lizenz)

---

<a id="de-features"></a>

## Features

- **CityGML 2.0 & 3.0** Import/Export (`.gml`, `.xml`)
- **3DCityDB-Anbindung** – Daten direkt aus PostgreSQL/PostGIS importieren und exportieren
  - Automatische Versionserkennung (v5 mit Fallback auf v4 Java impexp)
  - Unterstützung für `citydb-tool` (v5) und `3dcitydb-tool-v4` (Java)
- **Appearance** – Texturen, Materialfarben und UV-Koordinaten
- **Drag & Drop** – `.gml`/`.xml` Dateien direkt in den Viewport ziehen
- **Filter** – BBox, GML-ID, LOD (0–4) und Feature-Typ Filter
- **Streaming-Modus** – für sehr große Dateien (konfigurierbare Schwelle)
- **XSD-Validierung** – automatische Schema-Validierung nach Import/Export
- **Semantik** – Building Parts, Openings (Doors/Windows), Surfaces (Wall/Roof/Ground)
- **Headless-Betrieb** – Blender im Hintergrund per CLI steuern (siehe `scripts/headless/`)
- **Presets** – Speichern und Laden von Konfigurationen (z.B. DB-Verbindungen)
- **ModelTyper** – Semantische Klassifizierung von Gebäudeteilen

<a id="de-systemvoraussetzungen"></a>

## Systemvoraussetzungen

| Anforderung | Minimum |
|---|---|
| Blender | 4.0+ | 5.0+ | 5.1+ |
| Betriebssystem | Windows 10/11, Linux (x86_64), macOS |
| Python | wird von Blender mitgeliefert (3.11+) |
| Java (optional) | JRE 17+ (nur für 3DCityDB v4 Backend) |

<a id="de-installation"></a>

## Installation

<a id="de-addon-herunterladen"></a>

### 1. Add-on herunterladen

Das Add-on wird als ZIP-Archiv bereitgestellt.

Für Blender 4.2 und neuer bitte ein Release-ZIP verwenden, das
`blender_manifest.toml` enthält (zum Beispiel das generierte `*_INTERN.zip`
oder `*_EXTERN.zip`). Kein beliebiges Source-Code-ZIP installieren, dessen
entpackter Ordnername eine Versionsnummer enthält.

<a id="de-in-blender-installieren"></a>

### 2. In Blender installieren

1. **Bearbeiten → Einstellungen → Add-ons**
2. Oben rechts auf **Installieren…** klicken
3. Die heruntergeladene `.zip`-Datei auswählen
4. Das Add-on **„VCS 3DCityDB Importer/Exporter"** in der Liste aktivieren (Häkchen setzen)

Blender 4.2+ / 5 liest die stabile Extension-ID `citygml_importer_exporter`
aus `blender_manifest.toml`. Bei Legacy-Installationen ohne dieses Manifest
muss der entpackte Add-on-Ordner exakt `citygml_importer_exporter` heißen.

> **Hinweis:** Das Add-on wird nach `%APPDATA%\Blender Foundation\Blender\<version>\scripts\addons\` (Windows) bzw. `~/.config/blender/<version>/scripts/addons/` (Linux) entpackt.

<a id="de-addon-einstellungen"></a>

### 3. Add-on-Einstellungen konfigurieren (optional)

In den Add-on-Einstellungen können folgende Pfade angepasst werden:

- **CityBD-Tool executable** – Pfad zum `citydb-tool` (Standard: mitgeliefert)
- **3DCityDB v4 Java** – Pfad zu `java.exe` und impexp-JAR (optional, für v4-Datenbanken)
- **Docker** – Alternativ kann das citydb-tool via Docker-Container gestartet werden

<a id="de-verwendung"></a>

## Verwendung

<a id="de-citygml-importieren"></a>

### CityGML-Datei importieren

1. **Datei → Importieren → CityGML (.gml/.xml)**
2. Datei auswählen – die CityGML-Version wird automatisch erkannt
3. Optional: Appearance-Import aktivieren (Texturen, Materialien)

Alternativ: `.gml`- oder `.xml`-Datei per **Drag & Drop** in den Viewport ziehen.

<a id="de-citygml-exportieren"></a>

### CityGML-Datei exportieren

1. **Datei → Exportieren → CityGML (.gml)**
2. Zieldatei und CityGML-Version wählen (wird automatisch aus der Szene erkannt)
3. Export starten – anschließend wird optional eine XSD-Validierung durchgeführt

<a id="de-3dcitydb"></a>

### 3DCityDB Import/Export

Im **Sidebar-Panel** (N-Taste → Tab „CityGML") stehen DB-Import und DB-Export zur Verfügung:

1. Verbindungsdaten eingeben (Host, Port, Datenbank, User, Passwort, Schema)
2. Filter konfigurieren (BBox, Feature-Typen, GML-IDs)
3. Import/Export starten

Die DB-Version wird automatisch erkannt. Bei einer v4-Datenbank erfolgt ein automatischer Fallback auf den Java-basierten Importer/Exporter.

![CityGML importieren](Tutorial_Gifs/Import_CityGML.gif)

<a id="de-filter"></a>

### Filter

Im Sidebar-Panel können verschiedene Filter kombiniert werden:

| Filter | Beschreibung |
|---|---|
| **BBox** | Geografischer Begrenzungsrahmen (MinX, MinY, MaxX, MaxY) |
| **GML-ID** | Komma-separierte Liste von Feature-IDs |
| **LOD** | Level of Detail 0–4 (LOD 4 nur CityGML 2.0) |
| **Feature-Typ** | Buildings, Bridges, Tunnels, Vegetation, Water, Transportation, etc. |

<a id="de-modeltyper"></a>

### ModelTyper – Semantische Oberflächenklassifizierung

Der ModelTyper weist Mesh-Flächen CityGML-konforme Oberflächentypen zu (z.B. WallSurface, RoofSurface, GroundSurface). Er ist im Sidebar-Panel unter **CityGML → ModelTyper** verfügbar.

**Automatische Zuweisung:**

1. Objekte im Object Mode selektieren (oder eine Collection wählen)
2. Feature-Typ einstellen (Building, Bridge, Tunnel, WaterBody, Transportation)
3. CityGML-Version wählen (2.0 oder 3.0)
4. **„Assign Surface Types (Auto)"** klicken – die Geometrie wird analysiert und Flächen automatisch klassifiziert (Dach, Wand, Boden etc.)

**FBX, OBJ, GLTF, etc... to CityGML:**

![FBX to CityGML](Tutorial_Gifs/FBX_to_CityGML.gif)

**Custom-Property-Mapping:**

Das Mapping-Tool ist für Modelle gedacht, die aus Quellen ohne CityGML-Semantik kommen, z.B. FBX, OBJ oder glTF. Es liest Custom Properties am Objekt und am aktuell pro Face zugewiesenen Material und erzeugt daraus pro passendem Face ein neues CityGML-semantisches Material. Der Exporter kann diese Materialien anschließend wie importierte oder manuell zugewiesene CityGML-Surfaces auswerten.

**Workflow:**

1. Objekte im **Object Mode** selektieren oder eine Collection auswählen
2. Im Bereich **Custom Property Mapping** eine Mapping-Zeile anlegen
3. Object- und/oder Material-Custom-Property mit optionalem Wert eintragen
4. Ziel-Feature-Type und Ziel-Surface-Type aus den CityGML-Listen wählen
5. Optional **Quellmaterial kopieren** aktiv lassen, um Farben, Texturen und vorhandene nicht-semantische Materialdaten zu erhalten
6. **Apply Property Mapping** klicken

| Feld | Bedeutung |
|---|---|
| **Object Property / Object Value** | Custom Property am Blender-Objekt. Wenn kein Wert angegeben ist, reicht das Vorhandensein der Property. |
| **Material Property / Material Value** | Custom Property am Material des jeweiligen Faces. Wenn kein Wert angegeben ist, reicht das Vorhandensein der Property. |
| **Match Mode** | Vergleichsart für Werte: `Exact`, `Contains` oder `Regex`. |
| **Case Sensitive** | Aktiviert Groß-/Kleinschreibung für Property-Namen und Werte. |
| **Feature Type** | Ziel-Feature für das Objekt, z.B. `BUILDING`, `BRIDGE`, `TRANSPORTATION`. |
| **Surface Type** | Ziel-Semantik für passende Faces, z.B. `WallSurface`, `RoofSurface`, `GroundSurface`. |
| **Quellmaterial kopieren** | Kopiert das ursprüngliche Material und fügt CityGML-Semantik hinzu, statt ein rein farbcodiertes Material zu erzeugen. |
| **Bestehende Semantik überschreiben** | Überschreibt auch Faces, deren Material bereits CityGML-Eigenschaften enthält. |
| **Ungenutzte Materialien entfernen** | Entfernt nach dem Mapping Materialslots, die nicht mehr von Faces verwendet werden. |

Die erste aktivierte und passende Mapping-Zeile gewinnt. Wenn sowohl Objekt- als auch Materialkriterium gesetzt sind, müssen beide passen. Die Zeilen lassen sich mit den Pfeiltasten priorisieren.

**Beispiel für FBX-Mapping:**

| Object Property | Object Value | Material Property | Material Value | Feature Type | Surface Type |
|---|---|---|---|---|---|
| `asset_type` | `building` | `surface` | `wall` | `BUILDING` | `WallSurface` |
| `asset_type` | `building` | `surface` | `roof` | `BUILDING` | `RoofSurface` |
| `asset_type` | `building` | `surface` | `ground` | `BUILDING` | `GroundSurface` |

Pro passendem Face erzeugt das Tool ein neues Material mit `SurfaceTyp`, `surface_type`, `Typ`, `FeatureType`, `gml_polygon_id`, `gml_ring_id`, `con_surface_id`, `gml_multisurface_id` und `lod`. Am Objekt wird `ModelType` auf den dominanten gemappten Feature-Type gesetzt.

**Hinweis zu Generic Attributes und Custom Properties:**

In den **Material Properties** der einzelnen Surfaces gibt es die normalen Blender-**Custom Properties** und zusätzlich den Reiter **Generic Attributes**. In den normalen Custom Properties werden neben den fachlichen Attributen auch Blender-interne bzw. laufzeitbezogene Attribute gespeichert. Diese werden **nicht** als CityGML-Generic-Attribute der Surface exportiert. Für den Export relevant sind ausschließlich die Einträge im Reiter **Generic Attributes**.

Das gleiche Prinzip gilt für die **Objekt Properties**: Auch dort können normale Custom Properties Blender-interne oder temporäre Daten enthalten. Als CityGML-Generic-Attribute des Objekts werden nur die Werte aus dem Reiter **Generic Attributes** berücksichtigt.

**Manuelle Zuweisung:**

1. In den **Edit Mode** wechseln
2. Einzelne Faces selektieren
3. Gewünschten Oberflächentyp im Dropdown wählen (z.B. WallSurface, Window, Door)
4. **„Assign Surface Type (Manual)"** klicken
5. Bei **Window** oder **Door**: Optional die Checkbox **„Interior Surface erzeugen"** aktivieren – es wird automatisch eine Interior-Face mit umgekehrtem Winding erzeugt (Loch in der Eltern-Wandfläche)

> **Voraussetzung:** Das Erzeugen von Openings (Window/Door) über "Assign Surface Type (Manual)" ist nur sinnvoll, wenn das Objekt bereits CityGML-Semantik besitzt – entweder durch **CityGML-Import** oder durch vorherige Ausführung von **"Assign Surface Types (Auto)"** im ModelTyper. Ohne bestehende Metadaten (`gml_polygon_id`, `con_surface_id`) kann die Eltern-Kind-Beziehung zwischen Wandfläche und Opening beim Export nicht hergestellt werden.

> **LoD-Hinweis:** Wenn über **"Assign Surface Type (Manual)"** Faces die Opening-Semantik **Window** oder **Door** erhalten, wird das LoD des zugehörigen Top-Level-Objekts automatisch auf **LoD = 3** angehoben, damit Opening und Elternobjekt konsistent exportiert werden.

> **Hinweis:** Jede zugewiesene Face erhält ein eigenes Material mit eindeutiger `gml_polygon_id` und `gml_ring_id`, sodass der CityGML-Export korrekte, eindeutige Geometrie-IDs schreibt.

**Analyse:**

Mit **„Analyze Model"** wird die aktuelle Oberflächenzuweisung geprüft und Verbesserungsvorschläge in der Konsole ausgegeben (Verteilung der Typen, Validierungsfehler, Feature-Typ-Erkennung).

> **Tipp:** Das Materialfarben-Icon neben den Buttons bewahrt vorhandene Texturen beim Zuweisen neuer Oberflächentypen.

<a id="de-openings-cutter"></a>

### Openings Cutter – Türen und Fenster erzeugen

Der Openings Cutter erzeugt CityGML-exportkompatible Window- und Door-Flächen auf der Ebene einer selektierten Referenz-Face. Er ist im Sidebar-Panel unter **CityGML → Openings Cutter** verfügbar.

> **Voraussetzung:** Das Objekt muss bereits CityGML-Semantik besitzen (Materialien mit `gml_polygon_id`, `con_surface_id` etc.), bevor Openings erzeugt werden. Diese Semantik entsteht entweder durch **CityGML-Import** oder durch vorherige Zuweisung über den **ModelTyper** ("Assign Surface Types (Auto)"). Ohne diese Metadaten können Openings beim Export nicht korrekt ihren Eltern-Flächen zugeordnet werden.

**Drei Möglichkeiten, Openings zu erzeugen oder zu bestimmen:**

1. **Openings Cutter:** Openings manuell mit **F8** als Rechteck oder mit **F9** als Polygon auf einer selektierten Referenz-Face erzeugen
2. **Assign Surface Type (Manual):** Vorhandenen Faces im Edit Mode direkt die Opening-Semantik **Window** oder **Door** zuweisen
3. **Custom Property Mapping:** Openings automatisiert über die Mapping-Tabelle bestimmen, wenn bereits Faces bzw. Materialien vorhanden sind, deren Custom Properties Openings ausweisen

![Erzeuge Openings](Tutorial_Gifs/Openings.gif)

**Workflow:**

1. Im **Edit Mode** genau **eine Face mit Material** selektieren (die Wandfläche, auf der die Öffnung liegen soll)
2. Opening zeichnen:
   - **F8** – Rechteck aufziehen (Klicken + Ziehen)
   - **F9** – Polygon-Punkte setzen (LMB = Punkt, RMB/Enter = Abschließen, Ctrl+Z = Undo)
  - Bei aktivem Blender-**Snapping** werden Start-, End- und Polygonpunkte an nahe **Vertices** und **Kanten** sichtbarer Mesh-/Curve-Geometrie eingerastet; das funktioniert auch für Hilfsgeometrie im X-Ray-Workflow
3. Im Dialog den Typ wählen: **Window** oder **Door**
4. Optional: Checkbox **„Interior Surface erzeugen"** (de)aktivieren (Standard: aktiviert)
5. Es wird eine Exterior-Opening-Face erzeugt; bei aktivierter Option zusätzlich eine Interior-Face (Innenring/Loch)

**Snapping-Hilfslinien mit Cube, Shrinkwrap und Wireframe:**

Für regelmäßige Fenster- oder Türreihen kann eine separate Hilfsgeometrie als Snapping-Raster verwendet werden:

1. Im **Object Mode** einen **Cube** anlegen und so skalieren, dass er das Gebäude oder den relevanten Fassadenbereich leicht umhüllt
2. Den Cube im **Edit Mode** gleichmäßig **subdividen**; die Anzahl der Unterteilungen bestimmt den Abstand der späteren Hilfslinien
3. Auf dem Cube einen **Shrinkwrap Modifier** anlegen und das CityGML-/Gebäudeobjekt als Target setzen
   - **Nearest Surface Point** eignet sich für eine allgemeine Hülle um das Gebäude
   - **Project** eignet sich, wenn das Raster gezielt aus einer Richtung auf eine Fassade projiziert werden soll
   - Ein kleiner **Offset** kann helfen, die Hilfsgeometrie minimal vor der Fassade sichtbar zu halten
4. Danach einen **Wireframe Modifier** hinzufügen, damit aus den subdivideten Flächen sichtbare Linien entstehen
5. Die Hilfsgeometrie sichtbar lassen, optional **In Front** oder X-Ray aktivieren, und Blender-**Snapping** auf **Vertex** und/oder **Edge** stellen
6. Das eigentliche CityGML-Objekt im **Edit Mode** bearbeiten und mit **F8** oder **F9** Openings setzen; der Openings Cutter snappt dann auf die Vertices und Kanten des Wireframe-Rasters

Die Modifier müssen dafür nicht angewendet werden, solange sie im Viewport sichtbar sind. Die Hilfsgeometrie sollte nicht mit exportiert werden; sie kann nach dem Zeichnen der Openings ausgeblendet, gelöscht oder in eine nicht exportierte Collection verschoben werden.

![Snapping Openings](Tutorial_Gifs/Snapping_Openings.gif)

**Was übernommen wird:**

- Material und Texturen der Quell-Face
- UV-Koordinaten (affin interpoliert)
- Relevante Custom Properties (SurfaceTyp etc.)

> **LoD-Hinweis:** Wenn Openings über den Openings Cutter erzeugt werden, wird bei CityGML-2 das LoD des zugehörigen Top-Level-Objekts automatisch auf **LoD = 3** und bei CityGML-3 auf **LoD = 2** angehoben.

> **Hinweis:** Der Openings Cutter schneidet keine Boolean-Öffnung aus, sondern erzeugt die semantischen Opening-Flächen als separate Faces im gleichen Mesh.

<a id="de-join-object-parts"></a>

### Join Object Parts – Teile zusammenführen

Join Object Parts fügt material-lose Mesh-Teile in berührende Ziel-Meshes mit Materialien ein. Nützlich z.B. wenn nach einem Import separate Geometrieteile einem Gebäude zugeordnet werden sollen.

**Workflow:**

1. In den **Object Mode** wechseln
2. Die material-losen Mesh-Objekte selektieren, die zusammengeführt werden sollen
3. **Rechtsklick** im 3D-Viewport → **„Join Object Parts"** wählen
4. Das Tool sucht automatisch berührende Ziel-Meshes (mit Material/CityGML-Metadaten) und fügt die selektierten Teile dort ein
5. Bei der Kontaktfläche zwischen dem erzuegtem Objekt Part und dem Hauptobjekt wirde eine gml:interior Surface (Loch) erzeugt
6. Das Tool fragt den Nutzer, ob es eine zusätzlich ClosureSurface an der Kontaktfläche erzeugen soll (ACHTUNG: Die CityGML konformer Export von ClosureSurfaces ist noch nicht vollständig implementiert, bitte verzichten Sie evtl. zunächst auf die Erzeugung von ClosureSurfaces)

> **Hinweis:** Nur Meshes ohne zugewiesene Materialien werden verarbeitet. Wird kein berührendes Ziel-Mesh gefunden, passiert nichts.

<a id="de-assign-object-part"></a>

### Assign Object Part – Gebäudeteile semantisch zuweisen

Assign Object Part erstellt aus separaten Mesh-Objekten eine hierarchische CityGML-Gebäudestruktur. Ein Gebäude-Mesh wird dabei zum Container (EMPTY) umgewandelt, und weitere Objekte werden als **BuildingPart** oder **BuildingInstallation** zugewiesen.

**Ergebnis im Outliner:**

```
📦 Gebäude_Building  (EMPTY, structure_type="hierarchical")
  ├── 🔶 Gebäude       (MESH, structure_part="outer_shell")
  ├── 🔶 Anbau         (MESH, cgml3_feature="BuildingPart")
  └── 🔶 Balkon        (MESH, cgml3_feature="BuildingInstallation")
```

**Workflow:**

1. Im **Object Mode** die zuzuweisenden Objekte selektieren (Quell-Meshes)
2. Das Zielobjekt (Gebäude) **zuletzt** selektieren (= aktives Objekt)
3. **Rechtsklick → Assign Object Part** → Typ wählen:
   - **BuildingPart** – eigenständiger Gebäudeteil (z.B. Anbau, Garage)
   - **BuildingInstallation** – am Gebäude angebrachtes Element (z.B. Balkon, Treppe, Antenne)

**Was automatisch passiert:**

- Ist das Ziel ein Mesh, wird ein EMPTY als Building-Container erzeugt
- CityGML-Metadaten (`gml_id`, `cgml3_feature`, Attribute wie `measuredHeight` etc.) migrieren vom Mesh zum EMPTY
- Das Original-Mesh wird zum `outer_shell` (Hauptgeometrie des Gebäudes)
- Die Quell-Objekte erhalten `cgml3_feature` und werden unter dem EMPTY eingehängt
- Beim CityGML-2-Export wird die Hierarchie korrekt als `<bldg:consistsOfBuildingPart>` bzw. `<bldg:outerBuildingInstallation>` geschrieben – inklusive Geometrie, Appearance (Texturen + Materialfarben) und Koordinatentransformation

**Detach (Trennen):**

Bereits zugewiesene Teile können über **Rechtsklick → Assign Object Part → Detach (make independent)** wieder aus der Hierarchie gelöst werden. Das Objekt wird zu einem eigenständigen Building befördert. Der `outer_shell` kann nicht getrennt werden.

> **Hinweis:** Das Kontextmenü erscheint nur wenn mindestens 2 Objekte selektiert sind und ein aktives Objekt vorhanden ist.

![Erzeuge und Verknüpfe Objekte](Tutorial_Gifs/Add_BuildingParts.gif)

<a id="de-viewport-performance"></a>

### Viewport Performance

Große CityGML-Szenen können sehr viele Objekte, Materialien und Texturen enthalten. Deshalb werden importierte Objekte standardmäßig im **Solid View** angezeigt. Das spart Ressourcen, weil Blender nicht sofort alle Materialien, Texturen und Shader für den **Material Preview** laden und kompilieren muss.

Im Sidebar-Panel **CityGML → Viewport Performance** stehen Werkzeuge bereit, um gezielt nur die gerade relevanten Objekte visuell zu prüfen:

- **Material Preview (Selection Only)** schaltet in den Material Preview und reduziert nicht selektierte Objekte auf eine einfache Darstellung.
- **Local View + Material Preview** zeigt nur die selektierten Objekte im Material Preview.
- **Bounds** und **Wire** stellen nicht selektierte Objekte vereinfacht dar.
- **Hide** blendet nicht selektierte Objekte schnell aus.
- **Restore Original** stellt die ursprüngliche Sichtbarkeit und Darstellung wieder her und schaltet zurück in den Solid View.

Damit lassen sich große Modelle flüssiger navigieren, während Texturen und Materialien nur für die ausgewählten Objekte detailliert betrachtet werden.

![Viewport-Performance](Tutorial_Gifs/Viewport-Performance.gif)

<a id="de-headless"></a>

### Headless-Betrieb (CLI)

Das Add-on kann ohne GUI im Hintergrund betrieben werden:

```bash
blender --background --factory-startup --addons citygml_importer_exporter \
  --python scripts/headless/headless_cli.py -- import --input datei.gml --output ergebnis.blend
```

Verfügbare Subcommands: `import`, `export`, `db-import`, `db-export`

Weitere Details: siehe `scripts/headless/README.md`

<a id="de-orthophotos"></a>

## Georeferenzierte Orthophotos

Für den Import von georeferenzierten Orthophotos (Luftbilder, Satellitenbilder) als Hintergrund oder Geländetextur empfehlen wir das externe Blender-Add-on **[BlenderGIS](https://github.com/domlysz/BlenderGIS)**.

BlenderGIS ermöglicht:
- Import von GeoTIFF, World-Files und anderen georeferenzierten Rasterformaten als **Plane** (Mesh-Ebene mit Textur)
- Automatische CRS-Transformation und Positionierung
- Kombination mit den CityGML-Modellen dieses Add-ons im gleichen Koordinatenraum

> Zuletzt getestet mit BlenderGIS **v2.2.15**.

![GeoRaster Import mit BlenderGIS](Tutorial_Gifs/Import_Orthophoto.gif)

<a id="de-workflow-orthophoto"></a>

### Workflow: CityGML + Orthophoto

Die Reihenfolge ist frei wählbar – man kann entweder zuerst das Orthophoto oder zuerst die CityGML-Objekte importieren.

1. In BlenderGIS den benötigten EPSG-Code über die Schaltfläche **(+)** in der CRS-Liste hinzufügen und auswählen
2. CityGML-Datei mit diesem Add-on importieren (mit Georeferenz, nicht „Lokaler Import")
3. Orthophoto über **BlenderGIS → GIS → Import Georaster as Plane** importieren → beide Layer liegen automatisch übereinander

<a id="de-projektstruktur"></a>

## Projektstruktur

```
citygml_importer_exporter/
├── __init__.py              # Add-on Registrierung, Operatoren, UI
├── bundled_deps.py          # Python-Abhängigkeiten (lxml, pyproj)
├── io/                      # CityGML 3.0 Reader/Writer
├── io_v2/                   # CityGML 2.0 Reader/Writer
├── common/                  # Gemeinsame Utilities (CRS, Filter, Version)
├── shared/                  # Geometrie, Materialien, XML-Parsing
├── ops/                     # Blender-Operatoren (Validierung, DB-CLI, etc.)
├── ui/                      # Sidebar-Panels
├── modeltyper/              # Semantische Gebäudeklassifizierung
├── Openings_Cutter/         # Openings (Türen/Fenster) ausschneiden
├── scripts/headless/        # CLI-Skripte für Headless-Betrieb
├── presets/                 # Gespeicherte Konfigurationen
├── Schema/                  # XSD-Schemas für Validierung
├── Python_Module/           # Mitgelieferte Python-Pakete (lxml, pyproj)
├── citydb-tool-1.3.0/      # Mitgeliefertes citydb-tool (EXTERN)
└── 3dcitydb-tool-v4/       # Java impexp für 3DCityDB v4
```

<a id="de-abhaengigkeiten"></a>

## Abhängigkeiten

Das Add-on liefert alle benötigten Python-Abhängigkeiten mit:

- **lxml** – XML-Parsing mit XPath und Namespace-Unterstützung
- **pyproj** – CRS-Transformationen (EPSG-Codes)

Diese werden automatisch aus `Python_Module/` bzw. `python_deps/` geladen. Eine manuelle Installation ist nicht erforderlich.

<a id="de-lizenz"></a>

## Lizenz

MIT License

Copyright © 2026 virtualcitySYSTEMS

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

---

<br>
