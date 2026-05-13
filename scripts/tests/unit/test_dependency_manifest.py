import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def load_fetch_deps():
    spec = importlib.util.spec_from_file_location(
        "fetch_deps_under_test",
        ROOT / "scripts/fetch_deps.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


fetch_deps = load_fetch_deps()


def load_manifest():
    manifest = fetch_deps.load_manifest(ROOT / "dependencies.yaml")
    assert isinstance(manifest, dict)
    return manifest


def test_dependency_manifest_has_expected_top_level_sections():
    manifest = load_manifest()

    assert isinstance(manifest.get("tools"), dict)
    assert isinstance(manifest.get("python_modules"), list)
    assert isinstance(manifest.get("python"), dict)


def test_dependency_manifest_tool_entries_are_complete_and_formatable():
    manifest = load_manifest()

    for name, spec in manifest["tools"].items():
        assert spec.get("version"), f"{name}: missing version"
        assert spec.get("url"), f"{name}: missing url"
        assert spec.get("target_dir"), f"{name}: missing target_dir"

        format_values = {"version": spec["version"]}
        assert spec["url"].format(**format_values)
        assert spec["target_dir"].format(**format_values)

        auth = spec.get("auth")
        assert auth in {None, "none", "vcsuite_registry", "gitlab_job_token"}


def test_dependency_manifest_python_modules_are_unique_and_complete():
    manifest = load_manifest()
    names = []

    for spec in manifest["python_modules"]:
        assert spec.get("name"), f"incomplete Python module entry: {spec}"
        assert spec.get("version"), f"incomplete Python module entry: {spec}"
        names.append(spec["name"])

    assert sorted(names) == sorted(set(names))


def test_dependency_manifest_python_targets_are_declared():
    manifest = load_manifest()
    python_spec = manifest["python"]

    assert python_spec.get("versions")
    assert python_spec.get("platforms")
    for version in python_spec["versions"]:
        major, minor = version.split(".")
        assert major.isdigit()
        assert minor.isdigit()
    for platform in python_spec["platforms"]:
        assert platform
