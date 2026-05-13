# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""OS detection utilities shared by CLI wrappers."""
from __future__ import annotations

import os
import platform


def _is_windows() -> bool:
    """Erkennt Windows-Betriebssystem."""
    return os.name == "nt"


def _is_linux() -> bool:
    """Erkennt Linux-Betriebssystem."""
    return os.name == "posix" and platform.system() == "Linux"


def _is_macos() -> bool:
    """Erkennt macOS-Betriebssystem."""
    return os.name == "posix" and platform.system() == "Darwin"


def _os_label() -> str:
    """Gibt einen lesbaren OS-String zurück."""
    if _is_windows():
        return f"Windows ({platform.version()})"
    if _is_linux():
        return f"Linux ({platform.release()})"
    if _is_macos():
        return f"macOS ({platform.mac_ver()[0]})"
    return f"{platform.system()} ({platform.release()})"
