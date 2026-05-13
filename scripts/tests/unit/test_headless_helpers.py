import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class FakeOperator:
    def __init__(self, supported_properties=()):
        self.supported_properties = set(supported_properties)

    def get_rna_type(self):
        return types.SimpleNamespace(
            properties={name: object() for name in self.supported_properties}
        )


def make_fake_bpy(*, supported_export_properties=()):
    return types.SimpleNamespace(
        app=types.SimpleNamespace(driver_namespace={}),
        context=types.SimpleNamespace(
            scene=types.SimpleNamespace(cgml3=types.SimpleNamespace()),
            preferences=types.SimpleNamespace(addons={}),
        ),
        data=types.SimpleNamespace(
            texts=[],
            scenes=[],
            materials=[],
            objects=[],
            screens=[],
        ),
        ops=types.SimpleNamespace(
            cgml3=types.SimpleNamespace(
                export_to_db=FakeOperator(supported_export_properties),
            )
        ),
        path=types.SimpleNamespace(abspath=lambda value: f"/abs/{value}"),
    )


def load_headless_module(relative_path: str, module_name: str, *, supported_export_properties=()):
    sys.modules["bpy"] = make_fake_bpy(
        supported_export_properties=supported_export_properties
    )
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def db_export_args(**overrides):
    values = {
        "db_host": "localhost",
        "db_port": "5432",
        "db_name": "citydb",
        "db_user": "postgres",
        "db_pass": "secret",
        "db_schema": "",
        "citygml_version": "3.0",
        "srs_name": "",
        "no_write_offset_back": False,
        "use_inner_outer_script": False,
        "autofill_new_surfaces": False,
        "split_wall_roof_surfaces": False,
        "write_lod_solid_refs": False,
        "use_streaming": "AUTO",
        "enable_memory_tracking": False,
        "compress_textures": False,
        "texture_quality": 85,
        "texture_max_size": 0,
        "texture_workers": 4,
        "no_buildings": False,
        "no_bridges": False,
        "no_tunnels": False,
        "no_vegetation": False,
        "no_waterbodies": False,
        "no_construction": False,
        "no_cityfurniture": False,
        "no_landuse": False,
        "no_transportation": False,
        "no_relief": False,
        "no_generics": False,
        "import_config": "",
        "pre_delete_existing_features": False,
        "pre_delete_mode": "delete",
        "import_threads": 0,
        "import_compute_extent": False,
        "import_transform": "",
        "import_type_names": "",
        "import_feature_ids": "",
        "import_bbox": "",
        "import_bbox_mode": "NONE",
        "import_limit": 0,
        "import_start_index": 0,
        "import_no_appearances": False,
        "import_appearance_themes": "",
        "import_xsl_transform": "",
        "import_xal_source": False,
        "use_lod4_as_lod3": False,
        "map_lod0_roof_edge": False,
        "map_lod1_surface": False,
        "overwrite_existing": False,
        "skip_existing": False,
        "import_mode": "import_all",
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def assert_system_exit(fn, expected_code=1):
    try:
        fn()
    except SystemExit as exc:
        assert exc.code == expected_code
    else:
        raise AssertionError("Expected SystemExit")


def test_db_import_bbox_parser_accepts_four_and_five_part_values():
    run_db_import = load_headless_module(
        "scripts/headless/run_db_import.py",
        "run_db_import_under_test",
    )

    assert run_db_import._parse_bbox("1,2,3,4") == (1.0, 2.0, 3.0, 4.0, "")
    assert run_db_import._parse_bbox("1, 2, 3, 4, 25832") == (
        1.0,
        2.0,
        3.0,
        4.0,
        "25832",
    )


def test_db_import_bbox_parser_rejects_invalid_values():
    run_db_import = load_headless_module(
        "scripts/headless/run_db_import.py",
        "run_db_import_invalid_under_test",
    )

    for value in ("1,2,3", "1,2,3,4,5,6", "1,two,3,4"):
        try:
            run_db_import._parse_bbox(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected ValueError for {value!r}")


def test_db_export_resolves_existing_feature_mode_aliases():
    run_db_export = load_headless_module(
        "scripts/headless/run_db_export.py",
        "run_db_export_modes_under_test",
    )

    assert run_db_export._resolve_import_mode(db_export_args()) == "import_all"
    assert run_db_export._resolve_import_mode(db_export_args(overwrite_existing=True)) == "delete"
    assert run_db_export._resolve_import_mode(db_export_args(skip_existing=True)) == "skip"
    assert run_db_export._resolve_import_mode(db_export_args(import_mode="terminate")) == "terminate"


def test_db_export_splits_csv_values_for_operator_arguments():
    run_db_export = load_headless_module(
        "scripts/headless/run_db_export.py",
        "run_db_export_csv_under_test",
    )

    assert run_db_export._split_csv(" bldg:Building, , brid:Bridge ,, ") == "bldg:Building,brid:Bridge"


def test_db_export_builds_operator_kwargs_from_cli_args():
    run_db_export = load_headless_module(
        "scripts/headless/run_db_export.py",
        "run_db_export_kwargs_under_test",
        supported_export_properties=("pre_delete_existing_features", "pre_delete_mode"),
    )

    kwargs = run_db_export._build_export_operator_kwargs(
        db_export_args(
            citygml_version="2.0",
            no_buildings=True,
            import_type_names=" bldg:Building, brid:Bridge ",
            import_feature_ids=" id-1, id-2 ",
            import_no_appearances=True,
            pre_delete_existing_features=True,
            pre_delete_mode="terminate",
        ),
        "skip",
    )

    assert kwargs["citygml_version"] == "2.0"
    assert kwargs["export_buildings"] is False
    assert kwargs["export_bridges"] is True
    assert kwargs["import_type_names"] == "bldg:Building,brid:Bridge"
    assert kwargs["import_feature_ids"] == "id-1,id-2"
    assert kwargs["import_no_appearances"] is True
    assert kwargs["import_mode"] == "skip"
    assert kwargs["pre_delete_existing_features"] is True
    assert kwargs["pre_delete_mode"] == "terminate"


def test_db_export_drops_optional_pre_delete_kwargs_for_older_addon_operator():
    run_db_export = load_headless_module(
        "scripts/headless/run_db_export.py",
        "run_db_export_old_operator_under_test",
        supported_export_properties=(),
    )

    kwargs = run_db_export._build_export_operator_kwargs(
        db_export_args(pre_delete_existing_features=False),
        "import_all",
    )

    assert "pre_delete_existing_features" not in kwargs
    assert "pre_delete_mode" not in kwargs


def test_db_export_rejects_unsupported_pre_delete_request_for_older_addon_operator():
    run_db_export = load_headless_module(
        "scripts/headless/run_db_export.py",
        "run_db_export_old_operator_error_under_test",
        supported_export_properties=(),
    )

    assert_system_exit(
        lambda: run_db_export._build_export_operator_kwargs(
            db_export_args(pre_delete_existing_features=True),
            "import_all",
        )
    )


def test_db_export_rejects_unsafe_delete_mode_combinations():
    run_db_export = load_headless_module(
        "scripts/headless/run_db_export.py",
        "run_db_export_validation_under_test",
    )

    assert_system_exit(
        lambda: run_db_export._validate_import_combination(
            db_export_args(pre_delete_existing_features=True),
            "delete",
        )
    )
    assert_system_exit(
        lambda: run_db_export._validate_import_combination(
            db_export_args(import_no_appearances=False),
            "delete",
        )
    )
    run_db_export._validate_import_combination(
        db_export_args(import_no_appearances=True),
        "delete",
    )


def test_run_export_parser_reads_blender_separator_args():
    run_export = load_headless_module(
        "scripts/headless/run_export.py",
        "run_export_parser_under_test",
    )
    original_argv = sys.argv[:]
    sys.argv = [
        "blender",
        "--background",
        "--",
        "input.blend",
        "output.gml",
        "--citygml-version",
        "2.0",
        "--no-buildings",
        "--compress-textures",
    ]
    try:
        args = run_export._parse_args()
    finally:
        sys.argv = original_argv

    assert args.input_blend == "input.blend"
    assert args.output_gml == "output.gml"
    assert args.citygml_version == "2.0"
    assert args.no_buildings is True
    assert args.compress_textures is True
