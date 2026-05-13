# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# io/writer/crs_transform.py
from __future__ import annotations
from typing import Tuple
import numpy as np
from pyproj import CRS, Transformer

try:
    import bpy
except Exception:
    bpy = None

# --- EPSG-Policy -------------------------------------------------------------

KNOWN_SINGLE = {
    4326,   # WGS84 2D
    4979,   # WGS84 3D
    3857,   # WebMercator 2D
    25832,  # ETRS89 / UTM32
    25833,  # ETRS89 / UTM33
    4258,   # ETRS89 2D geographisch
    4937,   # ETRS89 3D geographisch
    3035,   # ETRS89 / LAEA Europe
}
RANGES = [
    (31466, 31469),   # DHDN / GK
    (32601, 32660),   # WGS84 / UTM Nord
    (32701, 32760),   # WGS84 / UTM Süd
]

def _parse_epsg(s: str) -> str:
    if not s:
        return ""
    t = str(s).strip()
    if t.upper().startswith("EPSG:"):
        return t.split(":", 1)[1]
    # einfache Extraktion einer Zahl in URNs wie urn:ogc:def:crs,crs:EPSG::25832,...
    digits = "".join(ch for ch in t if ch.isdigit())
    return digits

def is_supported_epsg(code: int) -> bool:
    if code in KNOWN_SINGLE:
        return True
    for lo, hi in RANGES:
        if lo <= code <= hi:
            return True
    return False

def _world_get(name: str, default=None):
    try:
        if bpy is None:
            return default
        w = bpy.data.worlds.get("World")
        if not w:
            return default
        return w.get(name, default)
    except Exception:
        return default

def _world_z_epsg() -> str:
    z = _world_get("Z-EPSG", "")
    return str(z).strip() if z is not None else ""

def _has_z_origin() -> bool:
    return _world_get("Z-Origin", None) is not None

def _compound_urn(hz_epsg: str, z_epsg: str) -> str:
    return f"urn:ogc:def:crs,crs:EPSG::{hz_epsg},crs:EPSG::{z_epsg}"

def normalize_target_crs(tgt_crs_raw: str, export_3d: bool) -> str:
    """
    Normalisiert ein Ziel-CRS fürs Rechnen:
      - Unterstützt geläufige EPSGs (s. Liste/Ranges).
      - Hebt 2D-Geos bei 3D-Export automatisch:
          4326 → 4979, 4258 → 4937
      - Erstellt Compound-URN, wenn World["Z-EPSG"] gesetzt ist.
    """
    t = (tgt_crs_raw or "").strip()
    if not t:
        return t

    hz = _parse_epsg(t)
    z_epsg = _world_z_epsg()

    # Bei 3D-Export: geographische 2D-Ziele heben
    if export_3d:
        if hz == "4326":
            t = _compound_urn("4326", z_epsg) if z_epsg.isdigit() else "EPSG:4979"
            return t
        if hz == "4258":
            t = _compound_urn("4258", z_epsg) if z_epsg.isdigit() else "EPSG:4937"
            return t

    # Wenn vertikales Datum vorhanden, bilde Compound für jedes horizontale EPSG
    if z_epsg.isdigit() and hz.isdigit():
        return _compound_urn(hz, z_epsg)

    # ansonsten unverändert zurück
    return t

def normalize_source_crs(src_crs_raw: str) -> str:
    """
    Normalisiert das Quell-CRS. Ergänzt optional vertikales Datum aus World["Z-EPSG"].
    """
    s = (src_crs_raw or "").strip()
    if not s:
        return s
    hz = _parse_epsg(s)
    z = _world_z_epsg()
    if hz.isdigit() and z.isdigit():
        return _compound_urn(hz, z)
    return s

# --- Transformer -------------------------------------------------------------

class GeoTransformer:
    """
    3D-Transformation Quelle→Ziel mit exakter Origin-Behandlung.
    Rechnet float64. Keine Rundungen. always_xy=True.
    """
    def __init__(self, src_crs: str, tgt_crs: str, export_3d: bool = False):
        src = normalize_source_crs(src_crs)
        tgt = normalize_target_crs(tgt_crs, export_3d or _has_z_origin())

        # einfache Support-Prüfung der Ziel-EPSG, ohne harte Sperre
        hz = _parse_epsg(tgt)
        if hz.isdigit():
            _ = is_supported_epsg(int(hz))  # kann für Logging verwendet werden

        self.src = CRS.from_user_input(src)
        self.tgt = CRS.from_user_input(tgt)
        self._tr = Transformer.from_crs(self.src, self.tgt, always_xy=True)

    def transform_points_abs(self, xyz_abs: np.ndarray) -> np.ndarray:
        x = xyz_abs[:, 0]; y = xyz_abs[:, 1]; z = xyz_abs[:, 2]
        X, Y, Z = self._tr.transform(x, y, z, errcheck=True)
        out = np.empty_like(xyz_abs)
        out[:, 0] = X; out[:, 1] = Y; out[:, 2] = Z
        return out

    def transform_with_origin(
        self,
        xyz_local: np.ndarray,
        origin_src: Tuple[float, float, float]
    ) -> tuple[np.ndarray, tuple[float, float, float]]:
        origin_src_arr = np.asarray(origin_src, dtype=np.float64).reshape(1, 3)
        xyz_abs_src = xyz_local.astype(np.float64) + origin_src_arr
        xyz_abs_tgt = self.transform_points_abs(xyz_abs_src)
        origin_tgt = self.transform_points_abs(origin_src_arr)[0]
        xyz_local_tgt = xyz_abs_tgt - origin_tgt
        return xyz_local_tgt, (float(origin_tgt[0]), float(origin_tgt[1]), float(origin_tgt[2]))
