# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# ops/citydb_cli.py
# citydb-tool wrapper (3DCityDB v5)
# - Abdeckung: --citygml-version, --config-file, --filter (CQL2), --filter-crs, --sql-filter,
#              --type-name, --lod, --lod-mode, --lod-search-depth, --sort-by, --limit, --start-index,
#              --crs, --crs-name, --transform, --no-appearances, --appearance-theme,
#              Zeitfilter (--validity, --validity-at, --validity-between, --validity-reference, --lenient-validity),
#              Ausgabeoptionen (--no-pretty-print, --xsl-transform),
#              Tiling, Threads
# - Docker: Host-Verzeichnis → /data
# Belegt durch 3DCityDB v5 Doku: Export-Optionen inkl. --sql-filter, CQL2, Pretty-Print/XSLT, Docker /data. :contentReference[oaicite:0]{index=0}

import os
import platform
import subprocess
from typing import List, Optional, Tuple

from ..common.os_utils import _is_windows, _is_linux, _is_macos, _os_label


def _quote_cql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _build_objectid_cql2_filter(feature_ids: Optional[List[str]]) -> Optional[str]:
    ids = [str(item).strip() for item in (feature_ids or []) if str(item).strip()]
    if not ids:
        return None

    return f"objectId IN ({','.join(_quote_cql_literal(item) for item in ids)})"

class CityDBTool:
    """
    prefs:
      - use_docker: bool
      - docker_image: str
      - citydb_exe: str
    """

    def __init__(self, prefs):
        self.prefs = prefs

    def _addon_root(self) -> str:
        return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

    def _resolve_executable(self, exe: str) -> str:
        exe = (exe or "").strip()
        if not exe or os.path.isabs(exe):
            return exe

        has_path_part = (
            exe.startswith(".")
            or os.path.sep in exe
            or (os.path.altsep and os.path.altsep in exe)
        )
        addon_candidate = os.path.abspath(os.path.join(self._addon_root(), exe))

        if has_path_part or os.path.exists(addon_candidate):
            return addon_candidate

        return exe

    # ---------------- intern ----------------
    def log_debug(self, msg: str):
        """Silent by default (was debug print)."""
        return

    def log_info(self, msg: str):
        """Log info message to Blender's info area"""
        print(f"CityGML INFO: {msg}")

    def log_warning(self, msg: str):
        """Log warning message to Blender's info area"""
        print(f"CityGML WARNING: {msg}")

    def log_error(self, msg: str):
        """Log error message to Blender's info area"""
        print(f"CityGML ERROR: {msg}")

    def backend_label(self) -> str:
        return "3DCityDB v5 (citydb-tool)"

    def _run(self, args: List[str], cwd: Optional[str]=None, timeout: Optional[int]=None) -> Tuple[bool, str]:
        self.log_info(f"Backend: {self.backend_label()}")
        self.log_info(f"OS: {_os_label()}")
        self.log_debug(f"Running command: {' '.join(args)}")
        try:
            # Unter Windows benötigen .bat/.cmd-Dateien shell=True
            use_shell = False
            if _is_windows() and args:
                exe_path = args[0]
                ext = os.path.splitext(exe_path)[1].lower()
                if ext in ('.bat', '.cmd'):
                    use_shell = True
                    self.log_debug(f"Windows: .bat/.cmd erkannt → shell=True")

            cp = subprocess.run(
                args,
                cwd=cwd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout if timeout and timeout > 0 else None,
                shell=use_shell
            )
            return (cp.returncode == 0), cp.stdout
        except subprocess.TimeoutExpired as e:
            return False, f"Timeout: {e}"
        except FileNotFoundError as e:
            hint = ""
            if _is_windows():
                hint = (" Hinweis (Windows): Stellen Sie sicher, dass der Pfad zu vcdb.bat "
                        "in den Add-on-Einstellungen korrekt gesetzt ist.")
            else:
                hint = (" Hinweis (Linux/macOS): Stellen Sie sicher, dass 'vcdb' im PATH liegt "
                        "oder der vollständige Pfad in den Add-on-Einstellungen angegeben ist. "
                        "Prüfen Sie auch die Ausführungsrechte (chmod +x).")
            return False, f"Datei nicht gefunden: {e}.{hint}"
        except PermissionError as e:
            hint = ""
            if not _is_windows():
                hint = " Tipp: chmod +x auf das vcdb-Skript ausführen."
            return False, f"Berechtigung verweigert: {e}.{hint}"
        except Exception as e:
            return False, f"Exec-Error: {e}"

    def _base_native(self) -> List[str]:
        """Erstellt den Basis-Befehl für die native Ausführung.
        
        Windows: vcdb.bat (oder konfigurierter Pfad)
        Linux/macOS: vcdb (oder konfigurierter Pfad)
        """
        exe = self._resolve_executable(getattr(self.prefs, "citydb_exe", ""))
        if exe:
            # Benutzer hat einen Pfad konfiguriert
            if not _is_windows() and not os.access(exe, os.X_OK) and os.path.isfile(exe):
                self.log_warning(f"vcdb-Skript ist nicht ausführbar: {exe} – Versuche trotzdem...")
            return [exe]
        # Fallback-Defaults pro Betriebssystem
        if _is_windows():
            return ["vcdb.bat"]
        else:
            # Linux/macOS: 'vcdb' muss im PATH liegen oder als vollständiger Pfad angegeben werden
            return ["vcdb"]

    def _base_docker(self, mount_dir: str) -> List[str]:
        image = (self.prefs.docker_image or "").strip() or "ghcr.io/3dcitydb/citydb-tool:latest"
        return ["docker", "run", "--rm", "-v", f"{mount_dir}:/data", image]

    def _conn(self, host: str, port: str, db: str, user: str, password: str, db_schema: Optional[str]=None) -> List[str]:
        args = ["-H", host, "-P", str(port), "-d", db, "-u", user, "-p", password]
        if db_schema:
            args += ["-S", db_schema]
        return args

    # ---------------- öffentlich ----------------

    def version(self) -> Tuple[bool, str]:
        base = self._base_native() if not self.prefs.use_docker else self._base_docker(os.getcwd())
        return self._run(base + ["--version"])

    def export_citygml_to_file(
        self,
        infile: str,
        outfile: str,
        *,
        extra_args: Optional[List[str]] = None
    ) -> Tuple[bool, str]:
        """
        Export CityGML mit Filteroptionen in eine neue Datei.
        """
        base = self._base_native() if not self.prefs.use_docker else self._base_docker(os.path.dirname(infile))
        cmd = base + ["export", "citygml", "-i", infile, "-o", outfile]
        
        if extra_args:
            cmd.extend(extra_args)
            
        print("Running citydb-tool command:", " ".join(cmd))
        return self._run(cmd)

    def import_citygml_to_db(
        self,
        infile: str,
        *,
        host: str, port: str, db: str, user: str, password: str,
        db_schema: Optional[str]=None,
        config_file: Optional[str]=None,
        # Import-Optionen
        import_mode: Optional[str]=None,         # import_all|skip|delete|terminate
        threads: Optional[int]=None,
        compute_extent: bool=False,
        transform: Optional[str]=None,            # "swap-xy" oder "m0,...,m11"
        # Filter
        type_names: Optional[List[str]]=None,     # ["bldg:Building","brid:Bridge"]
        feature_ids: Optional[List[str]]=None,    # GML-IDs: ["ID_001","ID_002"]
        bbox: Optional[str]=None,                 # "xmin,ymin,xmax,ymax[,srid]"
        bbox_mode: Optional[str]=None,            # intersects|contains|on_tile
        limit: Optional[int]=None,
        start_index: Optional[int]=None,
        # Appearance
        no_appearances: bool=False,
        appearance_themes: Optional[List[str]]=None,
        # CityGML-spezifisch
        xsl_transform: Optional[str]=None,
        import_xal_source: bool=False,
        # Upgrade 2.0/1.0
        use_lod4_as_lod3: bool=False,
        map_lod0_roof_edge: bool=False,
        map_lod1_surface: bool=False,
        # Logging
        log_level: Optional[str]=None,
        log_file: Optional[str]=None,
    ) -> Tuple[bool, str]:
        """
        vcdb import citygml [OPTIONS] <file>
        Unterstützt: Filter (--type-name, --id, --bbox, --limit, --start-index),
                     Appearance (--no-appearances, --appearance-theme),
                     Import-Optionen (--import-mode, --threads, --compute-extent, --transform),
                     CityGML-Optionen (--xsl-transform, --import-xal-source),
                     Upgrade-Optionen (--use-lod4-as-lod3, --map-lod0-roof-edge, --map-lod1-surface)
        """
        in_abs = os.path.abspath(infile)
        in_dir = os.path.dirname(in_abs)
        in_name = os.path.basename(in_abs)

        if self.prefs.use_docker:
            base = self._base_docker(in_dir)
            in_arg = f"/data/{in_name}"
        else:
            base = self._base_native()
            in_arg = in_abs

        cmd = ["import", "citygml", in_arg]

        if config_file:
            cf_abs = os.path.abspath(config_file)
            if self.prefs.use_docker:
                if os.path.dirname(cf_abs) != in_dir:
                    return False, "Config-Datei muss im selben gemounteten Ordner wie die Eingabedatei liegen."
                cmd += ["--config-file", f"/data/{os.path.basename(cf_abs)}"]
            else:
                cmd += ["--config-file", cf_abs]

        # Import-Optionen
        if import_mode:
            cmd += ["--import-mode", import_mode]
        if threads and threads > 0:
            cmd += ["--threads", str(threads)]
        if compute_extent:
            cmd += ["--compute-extent"]
        if transform:
            cmd += ["--transform", transform]

        # Filter
        if type_names:
            cmd += ["--type-name", ",".join(type_names)]
        if feature_ids:
            cmd += ["--id", ",".join(feature_ids)]
        if bbox:
            cmd += ["--bbox", bbox]
        if bbox_mode:
            cmd += ["--bbox-mode", bbox_mode]
        if limit and limit > 0:
            cmd += ["--limit", str(limit)]
        if start_index and start_index > 0:
            cmd += ["--start-index", str(start_index)]

        # Appearance
        if no_appearances:
            cmd += ["--no-appearances"]
        if appearance_themes:
            cmd += ["--appearance-theme", ",".join(appearance_themes)]

        # CityGML-spezifisch
        if xsl_transform:
            xs_abs = os.path.abspath(xsl_transform)
            if self.prefs.use_docker:
                if os.path.dirname(xs_abs) != in_dir:
                    return False, "The XSL stylesheet must be located in the same mounted folder as the input file."
                cmd += ["--xsl-transform", f"/data/{os.path.basename(xs_abs)}"]
            else:
                cmd += ["--xsl-transform", xs_abs]
        if import_xal_source:
            cmd += ["--import-xal-source"]

        # Upgrade-Options (CityGML 2.0/1.0)
        if use_lod4_as_lod3:
            cmd += ["--use-lod4-as-lod3"]
        if map_lod0_roof_edge:
            cmd += ["--map-lod0-roof-edge"]
        if map_lod1_surface:
            cmd += ["--map-lod1-surface"]

        # Logging
        if log_level:
            cmd += ["--log-level", log_level]
        if log_file:
            cmd += ["--log-file", log_file]

        args = base + cmd + self._conn(host, port, db, user, password, db_schema=db_schema)
        return self._run(args, cwd=in_dir)

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
        type_names: Optional[List[str]] = None,
        feature_ids: Optional[List[str]] = None,
        cql2_filter: Optional[str] = None,
        sql_filter: Optional[str] = None,
        limit: Optional[int] = None,
        start_index: Optional[int] = None,
        log_level: Optional[str] = None,
        log_file: Optional[str] = None,
        preview: bool = False,
    ) -> Tuple[bool, str]:
        # Windows command-line length limit (~8191 chars for .bat with shell=True).
        # Split large ID lists into batches to avoid WinError 206.
        _MAX_IDS_PER_BATCH = 200

        ids = [str(item).strip() for item in (feature_ids or []) if str(item).strip()]
        if len(ids) > _MAX_IDS_PER_BATCH and not cql2_filter:
            all_logs: list[str] = []
            for batch_start in range(0, len(ids), _MAX_IDS_PER_BATCH):
                batch = ids[batch_start:batch_start + _MAX_IDS_PER_BATCH]
                batch_num = batch_start // _MAX_IDS_PER_BATCH + 1
                total_batches = (len(ids) + _MAX_IDS_PER_BATCH - 1) // _MAX_IDS_PER_BATCH
                self.log_info(f"Delete batch {batch_num}/{total_batches} ({len(batch)} IDs)")
                ok, log = self.delete_features_from_db(
                    host=host, port=port, db=db, user=user, password=password,
                    db_schema=db_schema, delete_mode=delete_mode,
                    type_names=type_names, feature_ids=batch,
                    sql_filter=sql_filter, limit=limit, start_index=start_index,
                    log_level=log_level, log_file=log_file, preview=preview,
                )
                all_logs.append(log)
                if not ok:
                    return False, "\n".join(all_logs)
            return True, "\n".join(all_logs)

        base = self._base_native() if not self.prefs.use_docker else self._base_docker(os.getcwd())
        cmd = ["delete"]

        if delete_mode:
            cmd += ["--delete-mode", delete_mode]
        if preview:
            cmd += ["--preview"]
        if type_names:
            cmd += ["--type-name", ",".join(type_names)]

        combined_filter = cql2_filter
        objectid_filter = _build_objectid_cql2_filter(ids if ids else None)
        if objectid_filter:
            if combined_filter:
                combined_filter = f"({combined_filter}) AND ({objectid_filter})"
            else:
                combined_filter = objectid_filter
        if combined_filter:
            cmd += ["--filter", combined_filter]
        if sql_filter:
            cmd += ["--sql-filter", sql_filter]
        if limit and limit > 0:
            cmd += ["--limit", str(limit)]
        if start_index and start_index > 0:
            cmd += ["--start-index", str(start_index)]
        if log_level:
            cmd += ["--log-level", log_level]
        if log_file:
            cmd += ["--log-file", log_file]

        args = base + cmd + self._conn(host, port, db, user, password, db_schema=db_schema)
        return self._run(args)

    def export_db_to_citygml(
        self,
        output: str,
        *,
        host: str, port: str, db: str, user: str, password: str,
        db_schema: Optional[str]=None,
        # Version
        citygml_version: str = "3.0",
        # Config
        config_file: Optional[str]=None,
        # General export
        crs: Optional[str]=None,
        crs_name: Optional[str]=None,
        transform: Optional[str]=None,  # "swap-xy" oder "m0,...,m11"
        threads: Optional[int]=None,
        # Query & filter
        cql2_filter: Optional[str]=None,
        filter_crs: Optional[str]=None,
        sql_filter: Optional[str]=None,
        feature_ids: Optional[List[str]]=None,
        type_names: Optional[List[str]]=None,   # ["bldg:Building","brid:Bridge"]
        lod: Optional[List[str]]=None,          # ["1","2","3"]
        lod_mode: Optional[str]=None,           # or|and|minimum|maximum
        lod_search_depth: Optional[str]=None,   # "0..n"|"all"
        sort_by: Optional[List[str]]=None,
        limit: Optional[int]=None,
        start_index: Optional[int]=None,
        # Temporal validity
        validity: Optional[str]=None,           # latest|at|between|terminated|terminated_at|all
        validity_at: Optional[str]=None,        # "YYYY-MM-DD" oder ISO timestamp
        validity_between: Optional[str]=None,   # "startISO,endISO"
        validity_reference: Optional[str]=None, # "transaction"|"validity"
        lenient_validity: bool=False,
        # Appearance
        no_appearances: bool=False,
        appearance_themes: Optional[List[str]]=None,
        # Output formatting
        no_pretty_print: bool=False,
        xsl_transform: Optional[str]=None,
        log_level: Optional[str]=None, 
        log_file: Optional[str]=None,
        # Tiling
        tile_matrix: Optional[str]=None,        # "cols,rows"
        tile_dimension: Optional[str]=None,     # "width[unit],height[unit]"
        tile_extent: Optional[str]=None,        # "xmin,ymin,xmax,ymax[,srid]"
        tile_origin: Optional[str]=None,        # "top_left"|"bottom_left"
    ) -> Tuple[bool, str]:
        """
        citydb export citygml -o <output> --citygml-version <ver> [OPTIONS]
        Relevant flags see documentation: Filter including --sql-filter, CQL2; Pretty-Print/XSLT; Temporal filter (configuration and CLI). :contentReference[oaicite:1]{index=1}
        """

        out_abs = os.path.abspath(output)
        out_dir = os.path.dirname(out_abs) or os.getcwd()
        out_name = os.path.basename(out_abs)

        if self.prefs.use_docker:
            base = self._base_docker(out_dir)
            out_arg = f"/data/{out_name}"
        else:
            base = self._base_native()
            out_arg = out_abs

        cmd = ["export", "citygml", "-o", out_arg, "--citygml-version", citygml_version]

        # general
        if threads and threads > 0:
            cmd += ["--threads", str(threads)]
        if crs:
            cmd += ["--crs", crs]
        if crs_name:
            cmd += ["--crs-name", crs_name]
        if transform:
            cmd += ["--transform", transform]

        # config
        if config_file:
            cf_abs = os.path.abspath(config_file)
            if self.prefs.use_docker:
                if os.path.dirname(cf_abs) != out_dir:
                    return False, "The configuration file must be located in the same mounted folder as the output file."
                cmd += ["--config-file", f"/data/{os.path.basename(cf_abs)}"]
            else:
                cmd += ["--config-file", cf_abs]

        # filter
        if type_names:
            cmd += ["--type-name", ",".join(type_names)]
        combined_filter = cql2_filter.strip() if cql2_filter else None
        objectid_filter = _build_objectid_cql2_filter(feature_ids)
        if objectid_filter:
            if combined_filter:
                combined_filter = f"({combined_filter}) AND ({objectid_filter})"
            else:
                combined_filter = objectid_filter
        if combined_filter:
            cmd += ["--filter", combined_filter]
        if filter_crs:
            cmd += ["--filter-crs", filter_crs]
        if sql_filter:
            cmd += ["--sql-filter", sql_filter]
        if lod:
            cmd += ["--lod", ",".join(lod)]
        if lod_mode:
            cmd += ["--lod-mode", lod_mode]
        if lod_search_depth:
            cmd += ["--lod-search-depth", lod_search_depth]
        if sort_by:
            cmd += ["--sort-by", ",".join(sort_by)]
        if limit and limit > 0:
            cmd += ["--limit", str(limit)]
        if start_index and start_index > 0:
            cmd += ["--start-index", str(start_index)]

        # temporal validity
        if validity:
            cmd += ["--validity", validity]
        if validity_at:
            cmd += ["--validity-at", validity_at]
        if validity_between:
            cmd += ["--validity-between", validity_between]
        if validity_reference:
            cmd += ["--validity-reference", validity_reference]
        if lenient_validity:
            cmd += ["--lenient-validity"]

        # appearance
        if no_appearances:
            cmd += ["--no-appearances"]
        if appearance_themes:
            cmd += ["--appearance-theme", ",".join(appearance_themes)]

        # output formatting
        if no_pretty_print:
            cmd += ["--no-pretty-print"]
        if xsl_transform:
            xs_abs = os.path.abspath(xsl_transform)
            if self.prefs.use_docker:
                if os.path.dirname(xs_abs) != out_dir:
                    return False, "XSL-Stylesheet must be located in the same mounted folder as the output file."
                cmd += ["--xsl-transform", f"/data/{os.path.basename(xs_abs)}"]
            else:
                cmd += ["--xsl-transform", xs_abs]

        # tiling
        if tile_matrix:
            cmd += ["--tile-matrix", tile_matrix]
        if tile_dimension:
            cmd += ["--tile-dimension", tile_dimension]
        if tile_extent:
            cmd += ["--tile-extent", tile_extent]
        if tile_origin:
            cmd += ["--tile-origin", tile_origin]

        if log_level:
            cmd += ["--log-level", log_level]
        if log_file:
            cmd += ["--log-file", log_file]
        args = base + cmd + self._conn(host, port, db, user, password, db_schema=db_schema)
        return self._run(args, cwd=out_dir)
