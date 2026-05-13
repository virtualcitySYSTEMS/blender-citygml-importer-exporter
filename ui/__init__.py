# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026

from . import generic_attributes_ui
from . import citygml_attributes_panel
from . import import_scanner_panel
from . import header_panel
from ..Openings_Cutter import register as register_openings_cutter
from ..Openings_Cutter import unregister as unregister_openings_cutter


def register():
    header_panel.register()
    generic_attributes_ui.register()
    # The CityGML Attributes tab is intentionally disabled in the GUI.
    # Keep the module in the codebase so it can be re-enabled later if needed.
    import_scanner_panel.register()
    register_openings_cutter()


def unregister():
    header_panel.unregister()
    unregister_openings_cutter()
    import_scanner_panel.unregister()
    # The panel is not registered while the tool is intentionally hidden from the GUI.
    generic_attributes_ui.unregister()


__all__ = ["register", "unregister"]
