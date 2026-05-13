# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# ops/citydb_v4_cli.py
# 3DCityDB v4 Importer/Exporter wrapper (Java CLI: impexp-client-cli)

from __future__ import annotations

import os
import platform
import subprocess
from typing import List, Optional, Tuple

from ..common.os_utils import _is_windows, _is_linux, _is_macos, _os_label


class CityDBV4Tool:
    """
    Wrapper around the 3DCityDB v4 Importer/Exporter CLI.

    This add-on vendors the CLI jars in `3dcitydb-tool-v4/` and runs:
      java -jar 3dcitydb-tool-v4/impexp-client-cli-5.5.0.jar {export|import} ...

    prefs (AddonPreferences):
      - citydb_v4_java_exe: str (optional)
      - citydb_v4_impexp_jar: str (optional, defaults to vendored jar)
      - citydb_v4_default_config: str (optional, defaults to vendored impexp_config.xml)
    """

    def __init__(self, prefs):
        self.prefs = prefs

    def log_debug(self, msg: str):
        # Silent by default (was debug print).
        # Keep method for potential future opt-in debug.
        return

    def log_info(self, msg: str):
        print(f"CityGML INFO: {msg}")

    def log_warning(self, msg: str):
        print(f"CityGML WARNING: {msg}")

    def log_error(self, msg: str):
        print(f"CityGML ERROR: {msg}")

    def backend_label(self) -> str:
        return "3DCityDB v4 (Java impexp)"

    def _run(self, args: List[str], cwd: Optional[str] = None, timeout: Optional[int] = None) -> Tuple[bool, str]:
        self.log_info(f"Backend: {self.backend_label()}")
        self.log_info(f"OS: {_os_label()}")
        self.log_debug(f"Running command: {' '.join(args)}")
        try:
            cp = subprocess.run(
                args,
                cwd=cwd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout if timeout and timeout > 0 else None,
            )
            return (cp.returncode == 0), (cp.stdout or "")
        except subprocess.TimeoutExpired as e:
            return False, f"Timeout: {e}"
        except Exception as e:
            return False, f"Exec-Error: {e}"

    def _addon_root(self) -> str:
        return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

    def _resolve_tool_path(self, path: str) -> str:
        path = (path or "").strip()
        if not path or os.path.isabs(path):
            return path

        has_path_part = (
            path.startswith(".")
            or os.path.sep in path
            or (os.path.altsep and os.path.altsep in path)
        )
        addon_candidate = os.path.abspath(os.path.join(self._addon_root(), path))

        if has_path_part or os.path.exists(addon_candidate):
            return addon_candidate

        return path

    def _java_exe(self) -> str:
        java = (getattr(self.prefs, "citydb_v4_java_exe", "") or "").strip()
        return self._resolve_tool_path(java) or "java"

    def _default_jar(self) -> str:
        tool_dir = os.path.join(self._addon_root(), "3dcitydb-tool-v4")
        import glob
        matches = glob.glob(os.path.join(tool_dir, "impexp-client-cli-*.jar"))
        if matches:
            return matches[0]
        return os.path.join(tool_dir, "impexp-client-cli.jar")

    def _default_config(self) -> str:
        return os.path.join(self._addon_root(), "3dcitydb-tool-v4", "impexp_config.xml")

    def _jar_path(self) -> str:
        jar = (getattr(self.prefs, "citydb_v4_impexp_jar", "") or "").strip()
        return self._resolve_tool_path(jar) or self._default_jar()

    def _config_path(self, config_file: Optional[str]) -> Optional[str]:
        if config_file and str(config_file).strip():
            return self._resolve_tool_path(str(config_file))
        cfg = (getattr(self.prefs, "citydb_v4_default_config", "") or "").strip()
        return self._resolve_tool_path(cfg) if cfg else self._default_config()

    def _conn(self, host: str, port: str, db: str, user: str, password: str, db_schema: Optional[str] = None) -> List[str]:
        args = [
            f"--db-host={host}",
            f"--db-port={str(port)}",
            f"--db-name={db}",
            f"--db-username={user}",
            f"--db-password={password}",
        ]
        if db_schema:
            args.append(f"--db-schema={db_schema}")
        return args

    def version(self) -> Tuple[bool, str]:
        jar = self._jar_path()
        if not os.path.isfile(jar):
            return False, f"impexp-client-cli JAR nicht gefunden: {jar}"
        return self._run([self._java_exe(), "-jar", jar, "--version"])

    def export_db_to_citygml(
        self,
        output: str,
        *,
        host: str,
        port: str,
        db: str,
        user: str,
        password: str,
        db_schema: Optional[str] = None,
        # Filter (subset supported by v4 CLI)
        type_names: Optional[List[str]] = None,
        feature_ids: Optional[List[str]] = None,
        lod: Optional[List[str]] = None,
        lod_mode: Optional[str] = None,
        lod_search_depth: Optional[str] = None,
        count: Optional[int] = None,
        start_index: Optional[int] = None,
        bbox: Optional[str] = None,  # "minx,miny,maxx,maxy[,srid]"
        bbox_mode: Optional[str] = None,  # overlaps|within
        # Appearance
        no_appearance: bool = False,
        appearance_themes: Optional[List[str]] = None,
        # Config
        config_file: Optional[str] = None,
        # Logging
        log_level: Optional[str] = None,
        log_file: Optional[str] = None,
    ) -> Tuple[bool, str]:
        jar = self._jar_path()
        if not os.path.isfile(jar):
            return False, f"impexp-client-cli JAR nicht gefunden: {jar}"

        out_abs = os.path.abspath(output)
        out_dir = os.path.dirname(out_abs) or os.getcwd()

        cmd: List[str] = [self._java_exe(), "-jar", jar, "export", f"-o={out_abs}", "--compressed-format=citygml"]

        cfg = self._config_path(config_file)
        if cfg:
            if not os.path.isfile(cfg):
                return False, f"Config-Datei nicht gefunden: {cfg}"
            cmd += ["--config", cfg]

        if log_level:
            cmd += ["--log-level", log_level]
        if log_file:
            cmd += ["--log-file", os.path.abspath(log_file)]

        if type_names:
            cmd += ["--type-name", ",".join(type_names)]
        ids = [str(item).strip() for item in (feature_ids or []) if str(item).strip()]
        if ids:
            cmd += ["--resource-id", ",".join(ids)]
        if lod:
            # v4 CLI expects 0..4 values
            cmd += ["--lod", ",".join(lod)]
        if lod_mode:
            cmd += ["--lod-mode", lod_mode]
        if lod_search_depth:
            cmd += ["--lod-search-depth", lod_search_depth]
        if count and count > 0:
            cmd += ["--count", str(count)]
        if start_index and start_index > 0:
            cmd += ["--start-index", str(start_index)]

        if bbox:
            cmd += ["--bbox", bbox]
        if bbox_mode:
            cmd += ["--bbox-mode", bbox_mode]

        if no_appearance:
            cmd += ["--no-appearance"]
        if appearance_themes:
            cmd += ["--appearance-theme", ",".join(appearance_themes)]

        cmd += self._conn(host, port, db, user, password, db_schema=db_schema)
        return self._run(cmd, cwd=out_dir)

    def import_citygml_to_db(
        self,
        infile: str,
        *,
        host: str,
        port: str,
        db: str,
        user: str,
        password: str,
        db_schema: Optional[str] = None,
        import_mode: Optional[str] = None,  # import_all|skip|delete|terminate
        no_appearance: bool = False,
        config_file: Optional[str] = None,
        log_level: Optional[str] = None,
        log_file: Optional[str] = None,
        # Filter (v4 hat eingeschränkte Filter-Unterstützung)
        type_names: Optional[List[str]] = None,
        bbox: Optional[str] = None,           # "xmin,ymin,xmax,ymax[,srid]"
        bbox_mode: Optional[str] = None,      # overlaps|within
        limit: Optional[int] = None,
        start_index: Optional[int] = None,
    ) -> Tuple[bool, str]:
        jar = self._jar_path()
        if not os.path.isfile(jar):
            return False, f"impexp-client-cli JAR nicht gefunden: {jar}"

        in_abs = os.path.abspath(infile)
        in_dir = os.path.dirname(in_abs) or os.getcwd()

        cmd: List[str] = [self._java_exe(), "-jar", jar, "import", in_abs]

        cfg = self._config_path(config_file)
        if cfg:
            if not os.path.isfile(cfg):
                return False, f"Config-Datei nicht gefunden: {cfg}"
            cmd += ["--config", cfg]

        if log_level:
            cmd += ["--log-level", log_level]
        if log_file:
            cmd += ["--log-file", os.path.abspath(log_file)]

        if import_mode:
            cmd += ["--import-mode", import_mode]
        if no_appearance:
            cmd += ["--no-appearance"]

        # Filter (v4)
        if type_names:
            cmd += ["--type-name", ",".join(type_names)]
        if bbox:
            cmd += ["--bbox", bbox]
        if bbox_mode:
            cmd += ["--bbox-mode", bbox_mode]
        if limit and limit > 0:
            cmd += ["--count", str(limit)]
        if start_index and start_index > 0:
            cmd += ["--start-index", str(start_index)]

        cmd += self._conn(host, port, db, user, password, db_schema=db_schema)
        return self._run(cmd, cwd=in_dir)

    def delete_features_from_db(
        self,
        *,
        host: str,
        port: str,
        db: str,
        user: str,
        password: str,
        db_schema: Optional[str] = None,
        delete_mode: str = "delete",
        feature_ids: Optional[List[str]] = None,
        type_names: Optional[List[str]] = None,
        bbox: Optional[str] = None,
        bbox_mode: Optional[str] = None,
        limit: Optional[int] = None,
        start_index: Optional[int] = None,
        log_level: Optional[str] = None,
        log_file: Optional[str] = None,
        preview: bool = False,
    ) -> Tuple[bool, str]:
        jar = self._jar_path()
        if not os.path.isfile(jar):
            return False, f"impexp-client-cli JAR nicht gefunden: {jar}"

        cmd: List[str] = [self._java_exe(), "-jar", jar, "delete"]

        if delete_mode:
            cmd += ["--delete-mode", delete_mode]
        if preview:
            cmd += ["--preview"]
        if log_level:
            cmd += ["--log-level", log_level]
        if log_file:
            cmd += ["--log-file", os.path.abspath(log_file)]
        if type_names:
            cmd += ["--type-name", ",".join(type_names)]
        if feature_ids:
            cmd += ["--resource-id", ",".join(feature_ids)]
        if bbox:
            cmd += ["--bbox", bbox]
        if bbox_mode:
            cmd += ["--bbox-mode", bbox_mode]
        if limit and limit > 0:
            cmd += ["--count", str(limit)]
        if start_index and start_index > 0:
            cmd += ["--start-index", str(start_index)]

        cmd += self._conn(host, port, db, user, password, db_schema=db_schema)
        return self._run(cmd)
