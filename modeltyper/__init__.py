# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Integrated ModelTyper module for the CityGML importer/exporter add-on.
"""

from . import citygml
from . import core
from . import operators
from . import ui

modules = (
    citygml,
    core,
    operators,
    ui,
)


def register():
    for mod in modules:
        if hasattr(mod, "register"):
            mod.register()


def unregister():
    for mod in reversed(modules):
        if hasattr(mod, "unregister"):
            mod.unregister()


__all__ = ["register", "unregister"]
