import importlib.util
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def load_bundle_checker():
    spec = importlib.util.spec_from_file_location(
        "check_bundle_contents_under_test",
        ROOT / "scripts/tests/check_bundle_contents.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


bundle_checker = load_bundle_checker()


def write_zip(path: Path, names: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            if name.endswith("/"):
                archive.writestr(name, "")
            else:
                archive.writestr(name, "content")


def common_entries(root: str) -> list[str]:
    return [
        f"{root}/",
        f"{root}/__init__.py",
        f"{root}/blender_manifest.toml",
        f"{root}/version.json",
        f"{root}/presets/",
        f"{root}/icons/vcs_logo.png",
    ]


def write_valid_bundles(tmp_path: Path, addon_name="citygml_importer_exporter", version="1.2.3"):
    write_zip(
        tmp_path / f"{addon_name}-{version}_INTERN.zip",
        [
            *common_entries(f"{addon_name}_INTERN"),
            f"{addon_name}_INTERN/vcdb-tool-1.1.4/vcdb",
        ],
    )
    write_zip(
        tmp_path / f"{addon_name}-{version}_EXTERN.zip",
        [
            *common_entries(f"{addon_name}_EXTERN"),
            f"{addon_name}_EXTERN/citydb-tool-1.3.0/citydb",
        ],
    )


def test_bundle_checker_accepts_expected_intern_and_extern_archives(tmp_path):
    write_valid_bundles(tmp_path)

    result = bundle_checker.main(
        [
            "--addon-name",
            "citygml_importer_exporter",
            "--version",
            "1.2.3",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result == 0


def test_bundle_checker_rejects_missing_expected_tool(tmp_path):
    addon_name = "citygml_importer_exporter"
    version = "1.2.3"
    write_zip(
        tmp_path / f"{addon_name}-{version}_INTERN.zip",
        common_entries(f"{addon_name}_INTERN"),
    )
    write_zip(
        tmp_path / f"{addon_name}-{version}_EXTERN.zip",
        [
            *common_entries(f"{addon_name}_EXTERN"),
            f"{addon_name}_EXTERN/citydb-tool-1.3.0/citydb",
        ],
    )

    result = bundle_checker.main(
        [
            "--addon-name",
            addon_name,
            "--version",
            version,
            "--directory",
            str(tmp_path),
        ]
    )

    assert result == 1


def test_bundle_checker_rejects_missing_blender_manifest(tmp_path):
    addon_name = "citygml_importer_exporter"
    version = "1.2.3"
    write_zip(
        tmp_path / f"{addon_name}-{version}_INTERN.zip",
        [
            f"{addon_name}_INTERN/",
            f"{addon_name}_INTERN/__init__.py",
            f"{addon_name}_INTERN/version.json",
            f"{addon_name}_INTERN/presets/",
            f"{addon_name}_INTERN/vcdb-tool-1.1.4/vcdb",
        ],
    )
    write_zip(
        tmp_path / f"{addon_name}-{version}_EXTERN.zip",
        [
            *common_entries(f"{addon_name}_EXTERN"),
            f"{addon_name}_EXTERN/citydb-tool-1.3.0/citydb",
        ],
    )

    result = bundle_checker.main(
        [
            "--addon-name",
            addon_name,
            "--version",
            version,
            "--directory",
            str(tmp_path),
        ]
    )

    assert result == 1


def test_bundle_checker_rejects_forbidden_files_and_preset_json(tmp_path):
    addon_name = "citygml_importer_exporter"
    version = "1.2.3"
    write_zip(
        tmp_path / f"{addon_name}-{version}_INTERN.zip",
        [
            *common_entries(f"{addon_name}_INTERN"),
            f"{addon_name}_INTERN/vcdb-tool-1.1.4/vcdb",
            f"{addon_name}_INTERN/scripts/test.py",
            f"{addon_name}_INTERN/presets/Soest.json",
        ],
    )
    write_zip(
        tmp_path / f"{addon_name}-{version}_EXTERN.zip",
        [
            *common_entries(f"{addon_name}_EXTERN"),
            f"{addon_name}_EXTERN/citydb-tool-1.3.0/citydb",
        ],
    )

    result = bundle_checker.main(
        [
            "--addon-name",
            addon_name,
            "--version",
            version,
            "--directory",
            str(tmp_path),
        ]
    )

    assert result == 1
