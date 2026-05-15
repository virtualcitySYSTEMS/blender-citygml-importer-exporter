import argparse
import json
import sys
from datetime import datetime, timezone

import bpy


ADDON_MODULE = "citygml_importer_exporter"
HEADLESS_PREFS_KEY = "cgml3_headless_prefs"
CITYGML_VERSIONS = ("2.0", "3.0")
STREAMING_MODES = ("AUTO", "FORCE_STREAMING", "FORCE_DOM")
IMPORT_MODES = ("import_all", "skip", "delete", "terminate")
SURFACE_TYPES = ("WallSurface", "RoofSurface", "GroundSurface", "ClosureSurface")
ORIGINAL_HEIGHT_PROP = "extori_original_location_z"
SELECTED_HEIGHT_ADJUST_ORIGINAL_PROP = "extori_selected_height_adjust_original_z"
RUNTIME_PROP_ARCHIVE_TEXT_NAME = "CC_TM_RuntimePropertyArchive.json"
RUNTIME_PROP_PREFIXES = ("apt_", "multi_selector_")
RUNTIME_GENERIC_PROP_KEYS = {
    "image_path",
}
CAMERA_RUNTIME_PROP_KEYS = {
    "richtung",
    "resolution_x_px",
    "resolution_y_px",
    "focal_length_mm",
    "principal_point_px_x",
    "principal_point_px_y",
    "pixel_size_mm",
    "sensor_width_mm",
    "sensor_height_mm",
    "height_over_terrain",
}
SCENE_RUNTIME_PROP_KEYS = {
    "CRS",
    "crs",
    "SRID",
    "citygml_origin",
    "_extori_editor_subdivide_guard",
}
RUNTIME_PROP_WARNING_LENGTH = 2000


def _json_safe_runtime_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe_runtime_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe_runtime_value(val) for key, val in value.items()}
    return str(value)


def _runtime_archive_text_block():
    text_block = bpy.data.texts.get(RUNTIME_PROP_ARCHIVE_TEXT_NAME)
    if text_block is None:
        text_block = bpy.data.texts.new(RUNTIME_PROP_ARCHIVE_TEXT_NAME)
    return text_block


def _load_runtime_archive_payload(text_block):
    try:
        payload = json.loads(text_block.as_string() or "")
    except Exception:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("batches"), list):
        return payload
    return {"batches": []}


def _should_archive_runtime_prop(owner_kind: str, owner, key: object) -> bool:
    if not isinstance(key, str):
        return False
    if key in RUNTIME_GENERIC_PROP_KEYS:
        return True
    if any(key.startswith(prefix) for prefix in RUNTIME_PROP_PREFIXES):
        return True
    if owner_kind == "scene" and key in SCENE_RUNTIME_PROP_KEYS:
        return True
    if owner_kind in {"camera_object", "camera_data"} and key in CAMERA_RUNTIME_PROP_KEYS:
        return True
    return False


def _archive_and_remove_runtime_props() -> dict[str, int]:
    entries = []
    removed_count = 0
    oversized_count = 0

    owners = []
    owners.extend(("scene", scene) for scene in bpy.data.scenes)
    owners.extend(("material", material) for material in bpy.data.materials)
    owners.extend(("object", obj) for obj in bpy.data.objects)
    for obj in bpy.data.objects:
        if getattr(obj, "type", "") == "CAMERA":
            owners.append(("camera_object", obj))
            if getattr(obj, "data", None) is not None:
                owners.append(("camera_data", obj.data))

    for owner_kind, owner in owners:
        try:
            keys = list(owner.keys())
        except Exception:
            continue
        for key in keys:
            if not _should_archive_runtime_prop(owner_kind, owner, key):
                continue
            try:
                value = owner.get(key)
            except Exception:
                value = None
            string_length = len(value) if isinstance(value, str) else None
            if string_length is not None and string_length > RUNTIME_PROP_WARNING_LENGTH:
                oversized_count += 1
            entries.append(
                {
                    "owner_kind": owner_kind,
                    "owner_name": str(getattr(owner, "name", owner_kind)),
                    "key": key,
                    "string_length": string_length,
                    "archived_at": datetime.now(timezone.utc).isoformat(),
                    "value": _json_safe_runtime_value(value),
                }
            )
            try:
                del owner[key]
                removed_count += 1
            except Exception:
                continue

    if entries:
        text_block = _runtime_archive_text_block()
        payload = _load_runtime_archive_payload(text_block)
        payload["batches"].append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "removed_count": removed_count,
                "oversized_count": oversized_count,
                "entries": entries,
            }
        )
        text_block.clear()
        text_block.write(json.dumps(payload, ensure_ascii=False, indent=2))

    return {
        "removed_count": removed_count,
        "oversized_count": oversized_count,
    }


def _restore_original_object_heights_before_export() -> dict[str, int]:
    restored_count = 0
    cleared_invalid_count = 0

    for obj in bpy.data.objects:
        source_key = None
        if ORIGINAL_HEIGHT_PROP in obj:
            source_key = ORIGINAL_HEIGHT_PROP
        elif SELECTED_HEIGHT_ADJUST_ORIGINAL_PROP in obj:
            source_key = SELECTED_HEIGHT_ADJUST_ORIGINAL_PROP

        if source_key is None:
            continue

        target_z = None
        try:
            target_z = float(obj[source_key])
        except Exception:
            target_z = None

        try:
            if target_z is not None:
                obj.location.z = target_z
                restored_count += 1
            else:
                cleared_invalid_count += 1

            if ORIGINAL_HEIGHT_PROP in obj:
                del obj[ORIGINAL_HEIGHT_PROP]
            if SELECTED_HEIGHT_ADJUST_ORIGINAL_PROP in obj:
                del obj[SELECTED_HEIGHT_ADJUST_ORIGINAL_PROP]
        except Exception as exc:
            print(f"[headless-db-export] WARN: failed to restore height for {obj.name}: {exc}")

    return {
        "restored_count": restored_count,
        "cleared_invalid_count": cleared_invalid_count,
    }


def _extract_blender_args() -> list[str]:
    if "--" not in sys.argv:
        return []
    return sys.argv[sys.argv.index("--") + 1 :]


def _split_csv(value: str) -> str:
    return ",".join(part.strip() for part in value.split(",") if part.strip())


def _resolve_import_mode(args: argparse.Namespace) -> str:
    if getattr(args, "overwrite_existing", False):
        return "delete"
    if getattr(args, "skip_existing", False):
        return "skip"
    return args.import_mode


def _configure_scene_import_filters(args: argparse.Namespace) -> None:
    scene = getattr(bpy.context, "scene", None)
    cgml3 = getattr(scene, "cgml3", None)
    if cgml3 is None:
        return

    if getattr(args, "respect_scene_import_filters", False):
        return

    for attr_name in ("use_feature_type_filter", "use_gmlid_filter", "use_bbox_filter"):
        if hasattr(cgml3, attr_name):
            setattr(cgml3, attr_name, False)


def _disable_xsd_validation() -> None:
    scene = getattr(bpy.context, "scene", None)
    cgml3 = getattr(scene, "cgml3", None)
    if cgml3 is None:
        return
    if hasattr(cgml3, "xsd_validate"):
        cgml3.xsd_validate = False


def _operator_supports_property(operator, property_name: str) -> bool:
    try:
        return property_name in operator.get_rna_type().properties.keys()
    except Exception:
        return True


def _build_export_operator_kwargs(args: argparse.Namespace, resolved_import_mode: str) -> dict[str, object]:
    kwargs = {
        "db_host": args.db_host,
        "db_port": str(args.db_port),
        "db_name": args.db_name,
        "db_user": args.db_user,
        "db_pass": args.db_pass,
        "db_schema": args.db_schema,
        "citygml_version": args.citygml_version,
        "srs_name": args.srs_name,
        "write_offset_back": not args.no_write_offset_back,
        "use_inner_outer_script": args.use_inner_outer_script,
        "autofill_new_surfaces": args.autofill_new_surfaces,
        "split_wall_roof_surfaces": args.split_wall_roof_surfaces,
        "write_lod_solid_refs": args.write_lod_solid_refs,
        "use_streaming": args.use_streaming,
        "enable_memory_tracking": args.enable_memory_tracking,
        "compress_textures": args.compress_textures,
        "texture_quality": args.texture_quality,
        "texture_max_size": args.texture_max_size,
        "texture_workers": args.texture_workers,
        "export_buildings": not args.no_buildings,
        "export_bridges": not args.no_bridges,
        "export_tunnels": not args.no_tunnels,
        "export_vegetation": not args.no_vegetation,
        "export_waterbodies": not args.no_waterbodies,
        "export_construction": not args.no_construction,
        "export_cityfurniture": not args.no_cityfurniture,
        "export_landuse": not args.no_landuse,
        "export_transportation": not args.no_transportation,
        "export_relief": not args.no_relief,
        "export_generics": not args.no_generics,
        "import_config": args.import_config,
        "import_mode": resolved_import_mode,
        "pre_delete_existing_features": args.pre_delete_existing_features,
        "pre_delete_mode": args.pre_delete_mode,
        "import_threads": args.import_threads,
        "import_compute_extent": args.import_compute_extent,
        "import_transform": args.import_transform,
        "import_type_names": _split_csv(args.import_type_names),
        "import_feature_ids": _split_csv(args.import_feature_ids),
        "import_bbox": args.import_bbox,
        "import_bbox_mode": args.import_bbox_mode,
        "import_limit": args.import_limit,
        "import_start_index": args.import_start_index,
        "import_no_appearances": args.import_no_appearances,
        "import_appearance_themes": _split_csv(args.import_appearance_themes),
        "import_xsl_transform": args.import_xsl_transform,
        "import_xal_source": args.import_xal_source,
        "use_lod4_as_lod3": args.use_lod4_as_lod3,
        "map_lod0_roof_edge": args.map_lod0_roof_edge,
        "map_lod1_surface": args.map_lod1_surface,
    }

    operator = bpy.ops.cgml3.export_to_db
    missing_optional_props = []

    for property_name in ("pre_delete_existing_features", "pre_delete_mode"):
        if _operator_supports_property(operator, property_name):
            continue
        kwargs.pop(property_name, None)
        missing_optional_props.append(property_name)

    if not missing_optional_props:
        return kwargs

    if args.pre_delete_existing_features:
        missing_props = ", ".join(missing_optional_props)
        _fail(
            "Die geladene CityGML-Add-on-Version unterstuetzt die angeforderten DB-Export-Optionen nicht "
            f"({missing_props}). Wahrscheinlich ist in Blender eine aeltere Add-on-Version registriert als im Workspace."
        )

    print(
        "[headless-db-export] WARN: loaded export operator lacks optional properties "
        f"{', '.join(missing_optional_props)}; continuing with operator defaults"
    )
    return kwargs


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Headless 3DCityDB export via Blender add-on operator cgml3.export_to_db"
    )
    parser.add_argument("input_blend", help="Path to the input .blend file")

    parser.add_argument("--db-host", required=True)
    parser.add_argument("--db-port", default="5432")
    parser.add_argument("--db-name", required=True)
    parser.add_argument("--db-user", required=True)
    parser.add_argument("--db-pass", required=True)
    parser.add_argument("--db-schema", default="")

    parser.add_argument("--citygml-version", choices=CITYGML_VERSIONS, default="3.0")
    parser.add_argument("--srs-name", default="")
    parser.add_argument("--use-streaming", choices=STREAMING_MODES, default="AUTO")
    parser.add_argument("--no-write-offset-back", action="store_true")
    parser.add_argument("--use-inner-outer-script", action="store_true")
    parser.add_argument("--autofill-new-surfaces", action="store_true")
    parser.add_argument("--split-wall-roof-surfaces", action="store_true")
    parser.add_argument("--write-lod-solid-refs", action="store_true")
    parser.add_argument("--enable-memory-tracking", action="store_true")

    parser.add_argument("--compress-textures", action="store_true")
    parser.add_argument("--texture-quality", type=int, default=85)
    parser.add_argument("--texture-max-size", type=int, default=0)
    parser.add_argument("--texture-workers", type=int, default=4)

    parser.add_argument("--no-buildings", action="store_true")
    parser.add_argument("--no-bridges", action="store_true")
    parser.add_argument("--no-tunnels", action="store_true")
    parser.add_argument("--no-vegetation", action="store_true")
    parser.add_argument("--no-waterbodies", action="store_true")
    parser.add_argument("--no-construction", action="store_true")
    parser.add_argument("--no-cityfurniture", action="store_true")
    parser.add_argument("--no-landuse", action="store_true")
    parser.add_argument("--no-transportation", action="store_true")
    parser.add_argument("--no-relief", action="store_true")
    parser.add_argument("--no-generics", action="store_true")

    parser.add_argument("--import-mode", choices=IMPORT_MODES, default="import_all")
    parser.add_argument(
        "--pre-delete-existing-features",
        action="store_true",
        help="Loescht oder terminiert vorhandene DB-Features zuerst separat per GML-ID und importiert danach normal.",
    )
    parser.add_argument(
        "--pre-delete-mode",
        choices=("delete", "terminate"),
        default="delete",
        help="Modus fuer den separaten Vorab-Delete-Schritt.",
    )
    parser.add_argument("--import-threads", type=int, default=0)
    parser.add_argument("--import-compute-extent", action="store_true")
    parser.add_argument("--import-transform", default="")
    parser.add_argument("--import-type-names", default="")
    parser.add_argument("--import-feature-ids", default="")
    parser.add_argument("--import-bbox", default="")
    parser.add_argument("--import-bbox-mode", choices=("NONE", "intersects", "contains", "on_tile"), default="NONE")
    parser.add_argument("--import-limit", type=int, default=0)
    parser.add_argument("--import-start-index", type=int, default=0)

    appearance_group = parser.add_mutually_exclusive_group()
    appearance_group.add_argument(
        "--with-appearances",
        dest="import_no_appearances",
        action="store_false",
        help="Exportiert mit Appearance/Texturen in die DB-Importphase hinein (Standard).",
    )
    appearance_group.add_argument(
        "--no-appearances",
        dest="import_no_appearances",
        action="store_true",
        help="Ueberspringt Appearance/Texturen beim DB-Import der exportierten CityGML-Datei.",
    )
    appearance_group.add_argument(
        "--import-no-appearances",
        dest="import_no_appearances",
        action="store_true",
        help="Rueckwaertskompatibler Alias fuer --no-appearances.",
    )
    parser.set_defaults(import_no_appearances=False)
    parser.add_argument("--import-appearance-themes", default="")
    parser.add_argument("--import-xsl-transform", default="")
    parser.add_argument("--import-xal-source", action="store_true")
    parser.add_argument("--use-lod4-as-lod3", action="store_true")
    parser.add_argument("--map-lod0-roof-edge", action="store_true")
    parser.add_argument("--map-lod1-surface", action="store_true")
    parser.add_argument("--import-config", default="")
    parser.add_argument(
        "--respect-scene-import-filters",
        action="store_true",
        help="Uebernimmt Feature-Typ-, GML-ID- und BBox-Importfilter aus der Blend-Szene. Standardmaessig sind diese im Headless-db-export deaktiviert.",
    )

    existing_group = parser.add_mutually_exclusive_group()
    existing_group.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="Loescht vorhandene DB-Features und importiert die exportierten Daten neu (Alias fuer --import-mode delete).",
    )
    existing_group.add_argument(
        "--skip-existing",
        action="store_true",
        help="Ueberspringt bereits vorhandene DB-Features (Alias fuer --import-mode skip).",
    )

    parser.add_argument("--citydb-exe", default="")
    parser.add_argument("--use-docker", action="store_true")
    parser.add_argument("--docker-image", default="")
    parser.add_argument("--citydb-v4-java-exe", default="")
    parser.add_argument("--citydb-v4-impexp-jar", default="")
    parser.add_argument("--citydb-v4-default-config", default="")

    return parser.parse_args(_extract_blender_args())


def _fail(message: str, code: int = 1) -> None:
    print(f"[headless-db-export] ERROR: {message}")
    raise SystemExit(code)


def _validate_import_combination(args: argparse.Namespace, resolved_import_mode: str) -> None:
    if getattr(args, "pre_delete_existing_features", False) and resolved_import_mode in {"delete", "terminate"}:
        _fail(
            "--pre-delete-existing-features kann nicht mit --overwrite-existing oder --import-mode delete/terminate kombiniert werden. "
            "Verwenden Sie fuer den anschliessenden Import import_all oder skip."
        )

    if resolved_import_mode != "delete" or args.import_no_appearances:
        return

    _fail(
        "--overwrite-existing/--import-mode delete kann mit Appearance nicht sicher verwendet werden: "
        "citydb-tool v5 importiert in diesem Modus keine app:Appearance-Eintraege. "
        "Loeschen oder terminieren Sie vorhandene Features zuerst separat und starten Sie den Export danach ohne delete-Modus.")


def _latest_text_log(prefixes: tuple[str, ...]) -> str:
    texts = []
    for text_block in bpy.data.texts:
        name = str(getattr(text_block, "name", "") or "")
        if any(name.startswith(prefix) for prefix in prefixes):
            texts.append(text_block)

    if not texts:
        return ""

    texts.sort(key=lambda item: getattr(item, "name", ""))
    parts = []
    for tb in texts:
        header = f"--- {getattr(tb, 'name', '?')} ---"
        parts.append(header)
        parts.append(tb.as_string())
    return "\n".join(parts)


def _configure_preferences(args: argparse.Namespace) -> None:
    overrides = {
        "use_docker": bool(args.use_docker),
    }
    if args.citydb_exe:
        overrides["citydb_exe"] = bpy.path.abspath(args.citydb_exe)
    if args.docker_image:
        overrides["docker_image"] = args.docker_image
    if args.citydb_v4_java_exe:
        overrides["citydb_v4_java_exe"] = bpy.path.abspath(args.citydb_v4_java_exe)
    if args.citydb_v4_impexp_jar:
        overrides["citydb_v4_impexp_jar"] = bpy.path.abspath(args.citydb_v4_impexp_jar)
    if args.citydb_v4_default_config:
        overrides["citydb_v4_default_config"] = bpy.path.abspath(args.citydb_v4_default_config)

    bpy.app.driver_namespace[HEADLESS_PREFS_KEY] = overrides

    addon_entry = bpy.context.preferences.addons.get(ADDON_MODULE)
    if addon_entry is not None:
        prefs = addon_entry.preferences
        for key, value in overrides.items():
            setattr(prefs, key, value)


def main() -> None:
    args = _parse_args()

    if not hasattr(bpy.ops, "cgml3") or not hasattr(bpy.ops.cgml3, "export_to_db"):
        _fail(
            f"Add-on operator cgml3.export_to_db not available. Start Blender with --addons {ADDON_MODULE}."
        )

    _configure_preferences(args)

    input_blend = bpy.path.abspath(args.input_blend)
    open_result = bpy.ops.wm.open_mainfile(filepath=input_blend)
    print(f"[headless-db-export] open_result={set(open_result)}")
    if "FINISHED" not in open_result:
        _fail(f"Failed to open blend file: {open_result}")

    height_restore_summary = _restore_original_object_heights_before_export()
    if (
        height_restore_summary["restored_count"]
        or height_restore_summary["cleared_invalid_count"]
    ):
        print(
            "[headless-db-export] restored_object_heights="
            f"{height_restore_summary['restored_count']} "
            f"cleared_invalid_height_markers={height_restore_summary['cleared_invalid_count']}"
        )

    _disable_xsd_validation()
    cleanup_summary = _archive_and_remove_runtime_props()
    if cleanup_summary["removed_count"]:
        print(
            "[headless-db-export] runtime_custom_props_removed="
            f"{cleanup_summary['removed_count']} oversized_values={cleanup_summary['oversized_count']} "
            f"archive={RUNTIME_PROP_ARCHIVE_TEXT_NAME}"
        )

    print(f"[headless-db-export] input={input_blend}")
    print(
        "[headless-db-export] db="
        f"host={args.db_host}, port={args.db_port}, db={args.db_name}, schema={args.db_schema or '<default>'}"
    )
    resolved_import_mode = _resolve_import_mode(args)
    print(
        "[headless-db-export] options="
        f"appearances={'on' if not args.import_no_appearances else 'off'}, existing={resolved_import_mode}, "
        f"pre_delete={'on' if args.pre_delete_existing_features else 'off'}:{args.pre_delete_mode}"
    )

    _validate_import_combination(args, resolved_import_mode)

    _configure_scene_import_filters(args)
    print(
        "[headless-db-export] scene_import_filters="
        f"{'on' if args.respect_scene_import_filters else 'off'}"
    )

    operator_kwargs = _build_export_operator_kwargs(args, resolved_import_mode)

    try:
        result = bpy.ops.cgml3.export_to_db("EXEC_DEFAULT", **operator_kwargs)
    except RuntimeError as exc:
        debug_log = _latest_text_log(("citydb_delete", "citydb_import", "citydb_export", "citygml_xsd_check"))
        if debug_log:
            print("[headless-db-export] captured_text_log_begin")
            print(debug_log)
            print("[headless-db-export] captured_text_log_end")
        raise
    print(f"[headless-db-export] operator_result={set(result)}")

    if "FINISHED" not in result:
        debug_log = _latest_text_log(("citydb_delete", "citydb_import", "citydb_export", "citygml_xsd_check"))
        if debug_log:
            print("[headless-db-export] captured_text_log_begin")
            print(debug_log)
            print("[headless-db-export] captured_text_log_end")
        _fail(f"DB export operator did not finish successfully: {result}")

    print("[headless-db-export] completed")


if __name__ == "__main__":
    main()
