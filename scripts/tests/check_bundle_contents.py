"""
Validate generated add-on ZIP bundles.

Usage:
  python scripts/tests/check_bundle_contents.py --addon-name citygml_importer_exporter --version 1.2.3
"""
import argparse
import sys
import zipfile
from pathlib import Path


FORBIDDEN_PARTS = {
    ".git",
    ".cache",
    "__pycache__",
    "scripts",
    "devops-how-to-master",
    "test_files",
}
FORBIDDEN_FILENAMES = {
    ".gitignore",
    ".gitlab-ci.yml",
    "dependencies.yaml",
}


def _zip_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return archive.namelist()


def _normalized_parts(name: str) -> tuple[str, ...]:
    return tuple(part for part in name.replace("\\", "/").split("/") if part)


def _contains_dir(names: list[str], dirname: str) -> bool:
    return any(dirname in _normalized_parts(name) for name in names)


def _contains_file(names: list[str], filename: str) -> bool:
    return any(_normalized_parts(name)[-1:] == (filename,) for name in names)


def _contains_glob_suffix(names: list[str], suffix: str) -> bool:
    return any(name.lower().endswith(suffix.lower()) for name in names)


def _validate_common(names: list[str], label: str) -> list[str]:
    errors = []

    if not _contains_file(names, "__init__.py"):
        errors.append(f"{label}: missing __init__.py")
    if not _contains_file(names, "blender_manifest.toml"):
        errors.append(f"{label}: missing blender_manifest.toml")
    if not _contains_file(names, "version.json"):
        errors.append(f"{label}: missing version.json")
    if not _contains_dir(names, "presets"):
        errors.append(f"{label}: missing presets directory")

    for part in FORBIDDEN_PARTS:
        if _contains_dir(names, part):
            errors.append(f"{label}: forbidden directory included: {part}")
    for filename in FORBIDDEN_FILENAMES:
        if _contains_file(names, filename):
            errors.append(f"{label}: forbidden file included: {filename}")
    if _contains_glob_suffix(names, ".pyc"):
        errors.append(f"{label}: compiled Python files included")
    if _contains_glob_suffix(names, ".zip"):
        errors.append(f"{label}: nested zip file included")

    preset_json = [
        name for name in names
        if "/presets/" in name.replace("\\", "/") and name.lower().endswith(".json")
    ]
    if preset_json:
        errors.append(f"{label}: presets JSON files were not cleared: {preset_json[:5]}")

    return errors


def _validate_bundle(path: Path, label: str) -> list[str]:
    if not path.is_file():
        return [f"{label}: zip not found: {path}"]

    try:
        names = _zip_names(path)
    except zipfile.BadZipFile as exc:
        return [f"{label}: bad zip file: {exc}"]

    errors = _validate_common(names, label)
    has_citydb_tool = any(part.startswith("citydb-tool-") for name in names for part in _normalized_parts(name))
    has_vcdb_tool = any(part.startswith("vcdb-tool-") for name in names for part in _normalized_parts(name))

    if label == "INTERN":
        if not has_vcdb_tool:
            errors.append("INTERN: vcdb-tool-* is missing")
        if has_citydb_tool:
            errors.append("INTERN: citydb-tool-* must be excluded")
    elif label == "EXTERN":
        if not has_citydb_tool:
            errors.append("EXTERN: citydb-tool-* is missing")
        if has_vcdb_tool:
            errors.append("EXTERN: vcdb-tool-* must be excluded")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate generated add-on ZIP bundles")
    parser.add_argument("--addon-name", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--directory", default=".")
    args = parser.parse_args(argv)

    base_dir = Path(args.directory)
    intern_zip = base_dir / f"{args.addon_name}-{args.version}_INTERN.zip"
    extern_zip = base_dir / f"{args.addon_name}-{args.version}_EXTERN.zip"

    errors = []
    errors.extend(_validate_bundle(intern_zip, "INTERN"))
    errors.extend(_validate_bundle(extern_zip, "EXTERN"))

    if errors:
        print("Bundle validation FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("Bundle validation OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
