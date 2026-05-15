@echo off
setlocal enabledelayedexpansion
REM ============================================================================
REM Blender Add-on Tests - Runner Script
REM Führt alle Tests in getrennten Blender-Prozessen aus.
REM
REM Voraussetzungen:
REM   - Blender muss im PATH sein oder BLENDER_EXE gesetzt
REM   - Das Add-on muss im Blender-Addons-Pfad installiert sein
REM
REM Aufruf:
REM   scripts\tests\run_tests.bat
REM   scripts\tests\run_tests.bat "C:\Program Files\Blender Foundation\Blender 5.0\blender.exe"
REM ============================================================================

set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR%..\.."

REM Blender-Pfad bestimmen
if not "%~1"=="" (
    set "BLENDER=%~1"
) else if defined BLENDER_EXE (
    set "BLENDER=%BLENDER_EXE%"
) else (
    set "BLENDER=blender"
)

echo ============================================
echo  CityGML Importer/Exporter - Blender Tests
echo ============================================
echo Blender: %BLENDER%
echo Projekt: %PROJECT_DIR%
echo.

set PASS=0
set FAIL=0

REM --- Test 1: Plugin-Aktivierung ---
echo [1/3] Test: Plugin aktivieren...
"%BLENDER%" --background --python "%SCRIPT_DIR%test_activate.py" 2>&1 | findstr /C:"[TEST]"
if !ERRORLEVEL! equ 0 (
    set /a PASS+=1
) else (
    set /a FAIL+=1
    echo        FEHLGESCHLAGEN
)
echo.

REM --- Test 2: Import alle Feature-Typen ---
echo [2/3] Test: Import alle Feature-Typen...
"%BLENDER%" --background --addons citygml_importer_exporter --python "%SCRIPT_DIR%test_import.py" -- --dir "%PROJECT_DIR%\test_files" 2>&1 | findstr /C:"[TEST]" /C:"PASS" /C:"FAIL"
if !ERRORLEVEL! equ 0 (
    set /a PASS+=1
) else (
    set /a FAIL+=1
    echo        FEHLGESCHLAGEN
)
echo.

REM --- Test 3: Export alle Feature-Typen ---
echo [3/3] Test: Export alle Feature-Typen...
"%BLENDER%" --background --addons citygml_importer_exporter --python "%SCRIPT_DIR%test_export.py" -- --dir "%PROJECT_DIR%\test_files" 2>&1 | findstr /C:"[TEST]" /C:"PASS" /C:"FAIL"
if !ERRORLEVEL! equ 0 (
    set /a PASS+=1
) else (
    set /a FAIL+=1
    echo        FEHLGESCHLAGEN
)
echo.

REM --- Zusammenfassung ---
echo ============================================
echo  Ergebnis: %PASS% bestanden, %FAIL% fehlgeschlagen
echo ============================================

if !FAIL! gtr 0 (
    exit /b 1
)
exit /b 0
