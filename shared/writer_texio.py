# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
from __future__ import annotations
from typing import Tuple
import os, re, shutil
import bpy

__all__ = ["_rel_image_uri", "_ensure_export_texture"]

def _rel_image_uri(p: str) -> str:
    """
    Gibt eine URI wie 'appearance/<Dateiname>' zurück.
    Erzwingt Vorwärtsschrägstriche, ignoriert Verzeichnispfade.
    """
    if not p:
        return "appearance/"
    fname = os.path.basename(str(p).rstrip("/\\"))
    return f"appearance/{fname}".replace("\\", "/")

def _ensure_export_texture(src_path: str, out_dir: str, used_names: set) -> Tuple[str, str]:
    """
    Kopiert src_path nach <out_dir>/appearance/<basename> und gibt
    (abs_dest, rel_uri) zurück. Erzeugt eindeutige Dateinamen.
    """
    if not src_path:
        return "", ""

    # Immer appearance-Ordner verwenden
    folder = "appearance"
    apdir = os.path.join(out_dir, folder)
    os.makedirs(apdir, exist_ok=True)

    base = os.path.basename(src_path).replace("\\", "/")
    base = base.split("/")[-1]
    name, ext = os.path.splitext(base)
    if not ext:
        ext = ".jpg"  # Fallback
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ext

    cand = safe
    i = 2
    while cand.lower() in used_names:
        cand = f"{safe[:-len(ext)]}_{i}{ext}"
        i += 1
    used_names.add(cand.lower())

    abs_dest = os.path.join(apdir, cand)
    try:
        if os.path.isfile(src_path):
            shutil.copy2(src_path, abs_dest)
        else:
            # packed / nicht gespeicherte Bilder
            for img in bpy.data.images:
                fp = bpy.path.abspath(getattr(img, "filepath", "") or "")
                if fp and os.path.normcase(fp) == os.path.normcase(src_path):
                    img.filepath_raw = abs_dest
                    img.save()
                    break
    except Exception:
        pass

    rel_uri = f"{folder}/{cand}".replace("\\", "/")
    return abs_dest, rel_uri


