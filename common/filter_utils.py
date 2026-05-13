"""
Shared filter parsing helpers for import/export workflows.
"""

from __future__ import annotations

from typing import Optional, Sequence


def parse_bbox_coords_string(bbox_str: str) -> Optional[list[float]]:
    """Parse a bbox string in the format ``min_x, min_y, max_x, max_y``."""
    text = str(bbox_str or "").strip()
    if not text:
        return None

    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 4:
        return None

    try:
        min_x, min_y, max_x, max_y = (float(part) for part in parts)
    except (TypeError, ValueError):
        return None

    bbox_coords = [min_x, min_y, max_x, max_y]
    return bbox_coords if is_valid_bbox_coords(bbox_coords) else None


def format_bbox_coords_string(bbox_coords: Sequence[float]) -> str:
    """Format bbox coordinates as ``min_x, min_y, max_x, max_y``."""
    min_x, min_y, max_x, max_y = bbox_coords
    return f"{min_x}, {min_y}, {max_x}, {max_y}"


def is_valid_bbox_coords(bbox_coords: Sequence[float] | None) -> bool:
    """Validate a 2D bbox in the format ``[min_x, min_y, max_x, max_y]``."""
    if not bbox_coords or len(bbox_coords) != 4:
        return False

    try:
        min_x, min_y, max_x, max_y = (float(value) for value in bbox_coords)
    except (TypeError, ValueError):
        return False

    return min_x < max_x and min_y < max_y


def resolve_scene_bbox_filter(scene_props, *, sync_from_string: bool = True) -> Optional[list[float]]:
    """
    Resolve the effective bbox filter from Blender scene properties.

    The numeric bbox properties remain the primary source to preserve existing UI
    behavior. ``bbox_coords_string`` is used as a fallback when the numeric values
    are unset or invalid, which makes scripted imports work without requiring the
    separate parse operator first.
    """
    if scene_props is None or not getattr(scene_props, "use_bbox_filter", False):
        return None

    numeric_bbox = [
        float(getattr(scene_props, "bbox_min_x", 0.0)),
        float(getattr(scene_props, "bbox_min_y", 0.0)),
        float(getattr(scene_props, "bbox_max_x", 0.0)),
        float(getattr(scene_props, "bbox_max_y", 0.0)),
    ]
    if is_valid_bbox_coords(numeric_bbox):
        return numeric_bbox

    parsed_bbox = parse_bbox_coords_string(getattr(scene_props, "bbox_coords_string", ""))
    if parsed_bbox is None:
        return None

    if sync_from_string:
        try:
            scene_props.bbox_min_x = parsed_bbox[0]
            scene_props.bbox_min_y = parsed_bbox[1]
            scene_props.bbox_max_x = parsed_bbox[2]
            scene_props.bbox_max_y = parsed_bbox[3]
        except Exception:
            pass

    return parsed_bbox
