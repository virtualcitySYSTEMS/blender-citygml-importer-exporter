"""
Fetch external dependencies defined in dependencies.yaml.

Usage:
    python scripts/fetch_deps.py [--manifest dependencies.yaml] [--output-dir .]
                                 [--tools-only | --python-only]

Environment variables:
    CI_JOB_TOKEN        - GitLab CI job token for authenticated downloads
    GITLAB_TOKEN        - Fallback personal/project token for local use
"""

import argparse
import hashlib
import io
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
import zipfile
from pathlib import Path

try:
    import yaml
except ImportError:
    try:
        # pip might have vendored yaml
        from pip._vendor import yaml  # type: ignore
    except ImportError:
        yaml = None


# ---------------------------------------------------------------------------
# Minimal YAML parser (subset) when PyYAML is unavailable
# ---------------------------------------------------------------------------

def _parse_yaml_minimal(text):
    """
    Minimal recursive-descent YAML parser supporting:
      - nested dicts, lists, scalars
      - list items with '- key: val' or '- scalar'
      - comments and blank lines
    """
    lines = text.splitlines()
    # Pre-process: remove comments and blank lines, track indents
    entries = []
    for raw in lines:
        stripped = raw.rstrip()
        if not stripped or stripped.lstrip().startswith('#'):
            continue
        indent = len(stripped) - len(stripped.lstrip())
        content = stripped.lstrip()
        entries.append((indent, content))

    def _parse_value(s):
        s = s.strip()
        if not s:
            return None
        if s.startswith('"') and s.endswith('"'):
            return s[1:-1]
        if s.startswith("'") and s.endswith("'"):
            return s[1:-1]
        if s.isdigit():
            return int(s)
        try:
            return float(s)
        except ValueError:
            pass
        return s

    def _parse_block(pos, base_indent):
        """Parse a block at a given indentation level. Returns (result, next_pos)."""
        if pos >= len(entries):
            return None, pos

        indent, content = entries[pos]

        # Determine if this block is a list or a dict
        if content.startswith('- '):
            return _parse_list(pos, base_indent)
        else:
            return _parse_dict(pos, base_indent)

    def _parse_list(pos, base_indent):
        result = []
        while pos < len(entries):
            indent, content = entries[pos]
            if indent < base_indent:
                break
            if indent > base_indent:
                break
            if not content.startswith('- '):
                break

            item_text = content[2:].strip()
            if ':' in item_text and not item_text.startswith('http'):
                # Check if colon is part of a key:value or inside a URL
                colon_idx = item_text.index(':')
                key_part = item_text[:colon_idx]
                if ' ' not in key_part and '/' not in key_part:
                    # Dict item in list: parse as inline + continuation
                    obj = {}
                    k, v = item_text.split(':', 1)
                    k = k.strip()
                    v = v.strip()
                    if v:
                        obj[k] = _parse_value(v)
                    else:
                        pos += 1
                        child, pos = _parse_block(pos, indent + 2)
                        obj[k] = child
                        result.append(obj)
                        continue
                    pos += 1
                    # Continuation keys at deeper indent
                    while pos < len(entries):
                        ni, nc = entries[pos]
                        if ni <= indent:
                            break
                        if nc.startswith('- '):
                            break
                        if ':' in nc:
                            ck, cv = nc.split(':', 1)
                            obj[ck.strip()] = _parse_value(cv.strip())
                        pos += 1
                    result.append(obj)
                    continue

            # Simple scalar list item
            result.append(_parse_value(item_text))
            pos += 1

        return result, pos

    def _parse_dict(pos, base_indent):
        result = {}
        while pos < len(entries):
            indent, content = entries[pos]
            if indent < base_indent:
                break
            if indent > base_indent:
                # Shouldn't happen at top-level call, skip
                pos += 1
                continue
            if content.startswith('- '):
                break

            if ':' not in content:
                pos += 1
                continue

            # Split on first colon (but be careful with URLs)
            colon_idx = content.index(':')
            key = content[:colon_idx].strip()
            val_str = content[colon_idx + 1:].strip()

            if val_str:
                result[key] = _parse_value(val_str)
                pos += 1
            else:
                # Value is a nested block
                pos += 1
                if pos < len(entries):
                    next_indent = entries[pos][0]
                    if next_indent > indent:
                        child, pos = _parse_block(pos, next_indent)
                        result[key] = child
                    else:
                        result[key] = None
                else:
                    result[key] = None

        return result, pos

    result, _ = _parse_block(0, 0)
    return result if result else {}


def load_manifest(path):
    """Load and parse the dependencies.yaml manifest."""
    text = Path(path).read_text(encoding='utf-8')
    if yaml:
        return yaml.safe_load(text)
    return _parse_yaml_minimal(text)


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def _get_auth_token(auth_type=None):
    """Retrieve authentication token from environment.
    
    Auth types:
      - vcsuite_registry: uses VCSUITE_REGISTRY_TOKEN
      - gitlab_job_token: uses GITLAB_TOKEN or CI_JOB_TOKEN
    """
    if auth_type == 'vcsuite_registry':
        return os.environ.get('VCSUITE_REGISTRY_TOKEN', '')
    return os.environ.get('GITLAB_TOKEN') or os.environ.get('CI_JOB_TOKEN', '')


def _download(url, auth=None, dest_path=None):
    """Download a URL, optionally with GitLab token auth. Returns bytes or writes to dest_path."""
    headers = {}
    if auth and auth != 'none':
        token = _get_auth_token(auth)
        if token:
            print(f"  Using auth: {auth} ({len(token)} chars)")
            if auth == 'vcsuite_registry':
                headers['PRIVATE-TOKEN'] = token
            elif os.environ.get('GITLAB_TOKEN'):
                headers['PRIVATE-TOKEN'] = token
            else:
                headers['JOB-TOKEN'] = token
        else:
            env_var = 'VCSUITE_REGISTRY_TOKEN' if auth == 'vcsuite_registry' else 'GITLAB_TOKEN'
            print(f"  ERROR: No {env_var} available!")
            print(f"  Ensure {env_var} CI variable is set and NOT marked as 'Protected'")
            print(f"  (unless this branch is a protected branch).")

    req = urllib.request.Request(url, headers=headers)
    print(f"  Downloading: {url}")

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            content_type = resp.headers.get('Content-Type', '')
            if 'text/html' in content_type:
                raise RuntimeError(
                    f"Received HTML instead of binary data from {url}. "
                    f"This usually means authentication failed. "
                    f"Set GITLAB_TOKEN env var or ensure CI_JOB_TOKEN is available."
                )
            if dest_path:
                with open(dest_path, 'wb') as f:
                    shutil.copyfileobj(resp, f)
                return dest_path
            return resp.read()
    except urllib.error.HTTPError as e:
        print(f"  ERROR: HTTP {e.code} for {url}")
        raise


def _extract_zip(data_or_path, target_dir, strip_components=0, source_subpath=None):
    """Extract a zip archive to target_dir.
    
    Args:
        source_subpath: If set, only extract members whose path (after stripping
                        the top-level folder) starts with this subpath. The subpath
                        itself is also stripped from the output.
    """
    if isinstance(data_or_path, (str, Path)):
        zf = zipfile.ZipFile(data_or_path)
    else:
        zf = zipfile.ZipFile(io.BytesIO(data_or_path))

    with zf:
        members = zf.namelist()
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)

        for member in members:
            if member.endswith('/'):
                continue
            parts = Path(member).parts

            # Apply source_subpath filter: match members containing the subpath segment
            if source_subpath:
                # Find the subpath segment in the member parts
                subpath_parts = Path(source_subpath).parts
                found = False
                for idx in range(len(parts) - len(subpath_parts)):
                    if parts[idx:idx + len(subpath_parts)] == subpath_parts:
                        # Strip everything up to and including the subpath
                        remaining = parts[idx + len(subpath_parts):]
                        if remaining:
                            rel = Path(*remaining)
                            found = True
                        break
                if not found:
                    continue
            elif strip_components and len(parts) > strip_components:
                rel = Path(*parts[strip_components:])
            elif strip_components:
                continue
            else:
                rel = Path(member)

            out_path = target / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(out_path, 'wb') as dst:
                shutil.copyfileobj(src, dst)


# ---------------------------------------------------------------------------
# Version marker for incremental downloads
# ---------------------------------------------------------------------------

def _version_file(target_dir):
    return Path(target_dir) / '.dep_version'


def _is_current(target_dir, version):
    vf = _version_file(target_dir)
    if vf.exists():
        return vf.read_text(encoding='utf-8').strip() == version
    return False


def _mark_version(target_dir, version):
    vf = _version_file(target_dir)
    vf.write_text(version, encoding='utf-8')


# ---------------------------------------------------------------------------
# Tool fetching
# ---------------------------------------------------------------------------

def fetch_tools(manifest, output_dir):
    """Download and extract all tool dependencies."""
    tools = manifest.get('tools', {})
    for name, spec in tools.items():
        version = str(spec['version'])
        url = spec['url'].format(version=version)
        target_dir_template = spec.get('target_dir', name)
        target_dir = Path(output_dir) / target_dir_template.format(version=version)
        strip = int(spec.get('strip_components', 0))
        source_subpath = spec.get('source_subpath')
        auth = spec.get('auth')
        preserve = spec.get('preserve', [])
        if isinstance(preserve, str):
            preserve = [preserve]

        print(f"\n[TOOL] {name} v{version}")

        if _is_current(target_dir, version):
            print(f"  Already up-to-date at {target_dir}")
            continue

        # Backup preserved files
        preserved_files = {}
        for fname in preserve:
            fpath = target_dir / fname
            if fpath.exists():
                preserved_files[fname] = fpath.read_bytes()

        # Clean existing directory
        if target_dir.exists():
            print(f"  Removing outdated {target_dir}")
            shutil.rmtree(target_dir)

        # Download
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
            tmp_path = tmp.name

        try:
            _download(url, auth=auth, dest_path=tmp_path)
            print(f"  Extracting to {target_dir}")
            _extract_zip(tmp_path, target_dir, strip_components=strip,
                         source_subpath=source_subpath)
            _mark_version(target_dir, version)

            # Restore preserved files
            for fname, data in preserved_files.items():
                fpath = target_dir / fname
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_bytes(data)
                print(f"  Preserved: {fname}")

            print(f"  Done.")
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Python module fetching
# ---------------------------------------------------------------------------

def fetch_python_modules(manifest, output_dir):
    """Download Python wheels for all configured versions and platforms."""
    modules = manifest.get('python_modules', [])
    py_config = manifest.get('python', {})
    py_versions = py_config.get('versions', ['3.11'])
    py_platforms = py_config.get('platforms', ['win_amd64'])

    if not modules:
        print("\n[PYTHON] No modules defined.")
        return

    deps_base = Path(output_dir) / 'python_deps'

    for py_ver in py_versions:
        py_tag = f"py{py_ver.replace('.', '')}"
        dest = deps_base / py_tag / 'site-packages'

        # Build version key from modules
        version_key = "|".join(f"{m['name']}=={m['version']}" for m in modules)
        version_key += f"|platforms={'|'.join(py_platforms)}"

        if _is_current(dest, version_key):
            print(f"\n[PYTHON] {py_tag}: Already up-to-date.")
            continue

        print(f"\n[PYTHON] Downloading wheels for {py_tag}...")

        # Clean
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)

        # Use pip download to get wheels
        for mod in modules:
            pkg_spec = f"{mod['name']}=={mod['version']}"
            print(f"  {pkg_spec}")

            # Build platform args
            platform_args = []
            for plat in py_platforms:
                platform_args.extend(['--platform', plat])

            cmd = [
                sys.executable, '-m', 'pip', 'download',
                '--no-deps',
                '--only-binary=:all:',
                '--python-version', py_ver,
                *platform_args,
                '--dest', str(dest),
                pkg_spec,
            ]

            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, check=True
                )
                if result.stdout:
                    for line in result.stdout.strip().splitlines():
                        print(f"    {line}")
            except subprocess.CalledProcessError as e:
                print(f"    WARNING: pip download failed for {pkg_spec} ({py_tag})")
                print(f"    {e.stderr.strip()}")
                # Try without platform constraint as fallback (pure Python wheel)
                cmd_fallback = [
                    sys.executable, '-m', 'pip', 'download',
                    '--no-deps',
                    '--only-binary=:all:',
                    '--python-version', py_ver,
                    '--dest', str(dest),
                    pkg_spec,
                ]
                try:
                    subprocess.run(cmd_fallback, capture_output=True, text=True, check=True)
                    print(f"    Fallback (no platform constraint) succeeded.")
                except subprocess.CalledProcessError:
                    print(f"    ERROR: Could not download {pkg_spec} for {py_tag}")

        # Unpack wheels into site-packages layout
        _unpack_wheels(dest)
        _mark_version(dest, version_key)


def _unpack_wheels(dest):
    """Unpack .whl files in dest directory into flat site-packages layout."""
    dest = Path(dest)
    wheels = list(dest.glob('*.whl'))
    for whl in wheels:
        print(f"    Unpacking {whl.name}")
        with zipfile.ZipFile(whl) as zf:
            zf.extractall(dest)
        whl.unlink()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Fetch external dependencies')
    parser.add_argument('--manifest', default='dependencies.yaml',
                        help='Path to dependencies.yaml')
    parser.add_argument('--output-dir', default='.',
                        help='Base output directory')
    parser.add_argument('--tools-only', action='store_true',
                        help='Only fetch tool dependencies')
    parser.add_argument('--python-only', action='store_true',
                        help='Only fetch Python modules')
    args = parser.parse_args()

    manifest_path = Path(args.output_dir) / args.manifest
    if not manifest_path.exists():
        # Try relative to script
        manifest_path = Path(__file__).parent.parent / args.manifest
    if not manifest_path.exists():
        print(f"ERROR: Manifest not found: {args.manifest}")
        sys.exit(1)

    manifest = load_manifest(manifest_path)
    output_dir = args.output_dir

    if not args.python_only:
        fetch_tools(manifest, output_dir)

    if not args.tools_only:
        fetch_python_modules(manifest, output_dir)

    print("\n✓ All dependencies fetched successfully.")


if __name__ == '__main__':
    main()
