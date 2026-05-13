import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[3]


def load_module(relative_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


filter_utils = load_module("common/filter_utils.py", "filter_utils_under_test")


def test_parse_bbox_coords_string_accepts_valid_values():
    assert filter_utils.parse_bbox_coords_string("1, 2.5, 3, 4.75") == [1.0, 2.5, 3.0, 4.75]


def test_parse_bbox_coords_string_rejects_invalid_values():
    invalid_values = [
        "",
        "1,2,3",
        "1,2,3,4,5",
        "1,two,3,4",
        "4,2,1,3",
        "1,3,4,2",
    ]

    for value in invalid_values:
        assert filter_utils.parse_bbox_coords_string(value) is None


def test_format_bbox_coords_string_uses_expected_order():
    assert filter_utils.format_bbox_coords_string([1, 2, 3, 4]) == "1, 2, 3, 4"


def test_resolve_scene_bbox_filter_prefers_valid_numeric_values():
    props = SimpleNamespace(
        use_bbox_filter=True,
        bbox_min_x=10,
        bbox_min_y=20,
        bbox_max_x=30,
        bbox_max_y=40,
        bbox_coords_string="1, 2, 3, 4",
    )

    assert filter_utils.resolve_scene_bbox_filter(props) == [10.0, 20.0, 30.0, 40.0]


def test_resolve_scene_bbox_filter_falls_back_to_string_and_syncs_values():
    props = SimpleNamespace(
        use_bbox_filter=True,
        bbox_min_x=0,
        bbox_min_y=0,
        bbox_max_x=0,
        bbox_max_y=0,
        bbox_coords_string="1, 2, 3, 4",
    )

    assert filter_utils.resolve_scene_bbox_filter(props) == [1.0, 2.0, 3.0, 4.0]
    assert (props.bbox_min_x, props.bbox_min_y, props.bbox_max_x, props.bbox_max_y) == (
        1.0,
        2.0,
        3.0,
        4.0,
    )


def test_resolve_scene_bbox_filter_returns_none_when_disabled():
    props = SimpleNamespace(use_bbox_filter=False)

    assert filter_utils.resolve_scene_bbox_filter(props) is None
