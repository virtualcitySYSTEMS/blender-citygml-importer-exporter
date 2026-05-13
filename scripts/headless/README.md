# Headless Blender Launcher

Dieser Ordner enthaelt Python-Launcher fuer den headless Betrieb des Add-ons mit Blender im Background-Modus.

Eine ausfuehrlichere Dokumentation mit Parametererklaerungen steht in `citygml-importer-exporter/HEADLESS.md`.

## Verfuegbare Skripte

- `run_import.py`: CityGML-Datei nach Blender importieren und als `.blend` speichern
- `run_export.py`: `.blend` nach CityGML exportieren
- `run_db_import.py`: Daten aus 3DCityDB nach Blender importieren und als `.blend` speichern
- `run_db_export.py`: `.blend` ueber CityGML in 3DCityDB exportieren
- `headless_cli.py`: gemeinsamer Wrapper mit den Subcommands `import`, `export`, `db-import`, `db-export`
- `run_headless.ps1`: Windows PowerShell-Wrapper fuer den produktiven Aufruf von Blender
- `run_headless.bat`: einfacher Windows-Shortcut fuer `run_headless.ps1`

## Empfohlener Blender-Aufruf

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.0\blender.exe" `
  --background `
  --factory-startup `
  --addons "citygml-importer-exporter" `
  --python "C:\...\scripts\headless\headless_cli.py" `
  -- <subcommand> [optionen]
```

`--factory-startup` vermeidet Seiteneffekte durch gespeicherte Benutzerkonfiguration.

## Beispiele

### CityGML nach Blend

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.0\blender.exe" `
  --background `
  --factory-startup `
  --addons "citygml-importer-exporter" `
  --python "C:\Users\ofoerster\AppData\Roaming\Blender Foundation\Blender\5.0\scripts\addons\citygml-importer-exporter\scripts\headless\headless_cli.py" `
  -- import "C:\daten\modell.gml" "C:\daten\modell.blend"
```

### Blend nach CityGML

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.0\blender.exe" `
  --background `
  --factory-startup `
  --addons "citygml-importer-exporter" `
  --python "C:\Users\ofoerster\AppData\Roaming\Blender Foundation\Blender\5.0\scripts\addons\citygml-importer-exporter\scripts\headless\headless_cli.py" `
  -- export "C:\daten\modell.blend" "C:\daten\export.gml" --citygml-version 3.0 --srs-name EPSG:25832
```

### 3DCityDB nach Blend

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.0\blender.exe" `
  --background `
  --factory-startup `
  --addons "citygml-importer-exporter" `
  --python "C:\Users\ofoerster\AppData\Roaming\Blender Foundation\Blender\5.0\scripts\addons\citygml-importer-exporter\scripts\headless\headless_cli.py" `
  -- db-import "C:\daten\db_import.blend" --db-host localhost --db-port 5432 --db-name citydb --db-user postgres --db-pass secret --bbox "24487802,6820388.5,24488468,6820804.5,25832" --no-appearances
```

### Blend nach 3DCityDB

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.0\blender.exe" `
  --background `
  --factory-startup `
  --addons "citygml-importer-exporter" `
  --python "C:\Users\ofoerster\AppData\Roaming\Blender Foundation\Blender\5.0\scripts\addons\citygml-importer-exporter\scripts\headless\headless_cli.py" `
  -- db-export "C:\daten\modell.blend" --db-host localhost --db-port 5432 --db-name citydb --db-user postgres --db-pass secret --with-appearances --overwrite-existing
```

Fuer vorhandene Features in der Datenbank stehen im Headless-Export jetzt direkte Alias-Optionen zur Verfuegung:

- `--overwrite-existing`: vorhandene Features loeschen und neu importieren
- `--skip-existing`: vorhandene Features unveraendert lassen

Appearance ist standardmaessig aktiv. Optional koennen die Flags explizit gesetzt werden:

- `--with-appearances`: Appearance/Texturen mit in die DB uebernehmen
- `--no-appearances`: Appearance/Texturen beim DB-Import der exportierten CityGML-Datei auslassen

## Hilfe

Globale Hilfe des Wrappers:

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.0\blender.exe" `
  --background `
  --factory-startup `
  --addons "citygml-importer-exporter" `
  --python "C:\Users\ofoerster\AppData\Roaming\Blender Foundation\Blender\5.0\scripts\addons\citygml-importer-exporter\scripts\headless\headless_cli.py" `
  -- --help
```

Hilfe fuer ein Subcommand:

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.0\blender.exe" `
  --background `
  --factory-startup `
  --addons "citygml-importer-exporter" `
  --python "C:\Users\ofoerster\AppData\Roaming\Blender Foundation\Blender\5.0\scripts\addons\citygml-importer-exporter\scripts\headless\headless_cli.py" `
  -- export --help
```

Wrapper-Hilfe unter Windows:

```powershell
.\run_headless.ps1 -Action export -ShowHelp
```

Oder ueber die Batch-Datei:

```bat
run_headless.bat -Action export -ShowHelp
```

## Windows Wrapper Beispiele

### CityGML nach Blend

```powershell
.\run_headless.ps1 -Action import -Arguments "C:\daten\modell.gml","C:\daten\modell.blend"
```

### Blend nach CityGML

```powershell
.\run_headless.ps1 -Action export -Arguments "C:\daten\modell.blend","C:\daten\export.gml","--citygml-version","3.0","--srs-name","EPSG:25832"
```

### Blend nach 3DCityDB

```powershell
\.\run_headless.ps1 -Action db-export -Arguments "C:\daten\modell.blend","--db-host","localhost","--db-port","5432","--db-name","citydb","--db-user","postgres","--db-pass","secret","--with-appearances","--overwrite-existing"
```

### 3DCityDB nach Blend mit Bounding Box ohne Appearance

```powershell
.\run_headless.ps1 -Action db-import -Arguments "C:\daten\db_bbox_import.blend","--db-host","localhost","--db-port","5432","--db-name","citydb","--db-user","postgres","--db-pass","secret","--bbox","24487802,6820388.5,24488468,6820804.5,25832","--no-appearances"
```

`--bbox` verwendet das Format `xmin,ymin,xmax,ymax[,srid]`. Wenn ein `srid` angegeben wird, wird es automatisch als `filter_crs` fuer den DB-Import verwendet.

## Hinweise

- Die DB-Skripte fuehren echte Datenbankoperationen aus. Zugangsdaten und Zielsystem muessen korrekt sein.
- Optional koennen `--citydb-exe`, `--use-docker`, `--docker-image` sowie die v4-Fallback-Pfade uebergeben werden.
- Die DB-Launcher selbst wurden syntaktisch validiert, aber in dieser Session nicht gegen eine reale Datenbank ausgefuehrt.