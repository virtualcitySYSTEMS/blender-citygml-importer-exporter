from pathlib import Path
import os
import re
import sys


ABI_TAG_PATTERN = re.compile(r"cp(\d{2,3})")


def _required_abi_tag():
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def _is_path_compatible(package_root):
    required_tag = _required_abi_tag()
    for root, _dirs, files in os.walk(package_root):
        for filename in files:
            if not (filename.lower().endswith(".pyd") or ".cpython-" in filename.lower() and filename.lower().endswith(".so")):
                continue
            match = ABI_TAG_PATTERN.search(filename)
            if match and match.group(0) != required_tag:
                return False
    return True


def _candidate_paths(base_dir):
    py_version = f"py{sys.version_info.major}{sys.version_info.minor}"
    yield os.path.join(base_dir, "python_deps", py_version, "site-packages")
    yield os.path.join(base_dir, "Python_Module")
    yield os.path.join(base_dir, ".venv", "Lib", "site-packages")
    # Linux venv layout
    for _d in sorted(Path(os.path.join(base_dir, ".venv", "lib")).glob("python*")) if Path(os.path.join(base_dir, ".venv", "lib")).is_dir() else []:
        yield str(_d / "site-packages")


def get_compatible_bundled_paths(base_dir=None):
    if base_dir is None:
        base_dir = os.path.dirname(__file__)

    compatible_paths = []
    for package_root in _candidate_paths(base_dir):
        if not os.path.isdir(package_root):
            continue
        if not _is_path_compatible(package_root):
            continue
        compatible_paths.append(package_root)
    return compatible_paths


def ensure_bundled_site_packages(base_dir=None):
    if base_dir is None:
        base_dir = os.path.dirname(__file__)

    selected_path = None
    for package_root in get_compatible_bundled_paths(base_dir):
        if package_root in sys.path:
            sys.path.remove(package_root)
        sys.path.insert(0, package_root)
        if selected_path is None:
            selected_path = package_root

    return selected_path