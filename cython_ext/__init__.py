# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# cython_ext/__init__.py
"""
Cython-optimized performance-critical modules for CityGML Import/Export.

This package provides 10-50× speedup for geometry processing, coordinate parsing,
and transformation calculations through Cython compilation.

Modules:
    - coord_parser: Fast coordinate string parsing (parse_poslist optimization)
    - transform: Matrix transformations and coordinate conversions
    - grid_mesh: Grid coverage mesh generation

Usage:
    try:
        from .coord_parser import parse_coords_fast
        USE_CYTHON = True
    except ImportError:
        USE_CYTHON = False
        # Fallback to pure Python implementation
"""

__version__ = "1.0.0"
__all__ = []

# Try to import Cython modules, fallback to pure Python if not available
try:
    from . import coord_parser
    __all__.append('coord_parser')
    CYTHON_AVAILABLE = True
except ImportError:
    CYTHON_AVAILABLE = False

try:
    from . import transform
    __all__.append('transform')
except ImportError:
    pass

try:
    from . import grid_mesh
    __all__.append('grid_mesh')
except ImportError:
    pass


def check_cython_support():
    """
    Check if Cython modules are available and working.
    
    Returns:
        tuple: (available: bool, modules: list, message: str)
    """
    available_modules = []
    
    if 'coord_parser' in __all__:
        available_modules.append('coord_parser')
    if 'transform' in __all__:
        available_modules.append('transform')
    if 'grid_mesh' in __all__:
        available_modules.append('grid_mesh')
    
    if not available_modules:
        return (False, [], 
                "Cython modules not available. Using pure Python implementation. "
                "To enable Cython optimizations, run: python setup.py build_ext --inplace")
    
    return (True, available_modules,
            f"Cython acceleration active: {', '.join(available_modules)}")
