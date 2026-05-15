import argparse
import sys

import bpy


ADDON_MODULE = "citygml_importer_exporter"
HEADLESS_PREFS_KEY = "cgml3_headless_prefs"
LOD_MODES = ("NONE", "or", "and", "minimum", "maximum")
VALIDITY_MODES = ("NONE", "latest", "at", "between", "terminated", "terminated_at", "all")
VALIDITY_REFERENCES = ("NONE", "transaction", "validity")


def _extract_blender_args() -> list[str]:
    if "--" not in sys.argv:
        return []
    return sys.argv[sys.argv.index("--") + 1 :]


def _split_csv(value: str) -> str:
    return ",".join(part.strip() for part in value.split(",") if part.strip())


def _parse_bbox(value: str) -> tuple[float, float, float, float, str]:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) not in {4, 5}:
        raise ValueError("bbox must use xmin,ymin,xmax,ymax[,srid]")

    min_x = float(parts[0])
    min_y = float(parts[1])
    max_x = float(parts[2])
    max_y = float(parts[3])
    srid = parts[4] if len(parts) == 5 else ""
    return min_x, min_y, max_x, max_y, srid


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Headless 3DCityDB import via Blender add-on operator cgml3.import_from_db"
    )
    parser.add_argument("output_blend", help="Path to the output .blend file")

    parser.add_argument("--db-host", required=True)
    parser.add_argument("--db-port", default="5432")
    parser.add_argument("--db-name", required=True)
    parser.add_argument("--db-user", required=True)
    parser.add_argument("--db-pass", required=True)
    parser.add_argument("--db-schema", default="")

    parser.add_argument("--crs", default="")
    parser.add_argument("--crs-name", default="")
    parser.add_argument("--transform", default="")
    parser.add_argument(
        "--bbox",
        default="",
        help="Bounding box as xmin,ymin,xmax,ymax[,srid]",
    )

    parser.add_argument("--type-names", default="", help="Comma separated feature types")
    parser.add_argument("--lod", default="", help="Comma separated LODs, e.g. 1,2,3")
    parser.add_argument("--lod-mode", choices=LOD_MODES, default="NONE")
    parser.add_argument("--lod-search-depth", default="")
    parser.add_argument("--cql2-filter", default="")
    parser.add_argument("--filter-crs", default="")
    parser.add_argument("--sql-filter", default="")
    parser.add_argument("--sort-by", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start-index", type=int, default=0)

    parser.add_argument("--validity", choices=VALIDITY_MODES, default="NONE")
    parser.add_argument("--validity-at", default="")
    parser.add_argument("--validity-between", default="")
    parser.add_argument("--validity-reference", choices=VALIDITY_REFERENCES, default="NONE")
    parser.add_argument("--lenient-validity", action="store_true")

    parser.add_argument("--no-appearances", action="store_true")
    parser.add_argument("--appearance-themes", default="")
    parser.add_argument("--no-pretty-print", action="store_true")
    parser.add_argument("--xsl-transform", default="")
    parser.add_argument("--export-config", default="")

    parser.add_argument("--xsd-validate", action="store_true")
    parser.add_argument("--xsd-lenient", action="store_true")

    parser.add_argument("--citydb-exe", default="")
    parser.add_argument("--use-docker", action="store_true")
    parser.add_argument("--docker-image", default="")
    parser.add_argument("--citydb-v4-java-exe", default="")
    parser.add_argument("--citydb-v4-impexp-jar", default="")
    parser.add_argument("--citydb-v4-default-config", default="")

    return parser.parse_args(_extract_blender_args())


def _fail(message: str, code: int = 1) -> None:
    print(f"[headless-db-import] ERROR: {message}")
    raise SystemExit(code)


def _print_blender_text_logs(prefixes: tuple[str, ...]) -> None:
    matched = []
    for text_block in bpy.data.texts:
        lower_name = text_block.name.lower()
        if any(lower_name.startswith(prefix.lower()) for prefix in prefixes):
            matched.append(text_block)

    if not matched:
        print("[headless-db-import] No Blender text logs matched the requested prefixes.")
        return

    matched.sort(key=lambda text_block: text_block.name)
    for text_block in matched[-5:]:
        print(f"[headless-db-import] BEGIN_TEXT_LOG {text_block.name}")
        try:
            print(text_block.as_string()[:2_000_000])
        except Exception as exc:
            print(f"[headless-db-import] Failed to read text block {text_block.name}: {exc}")
        print(f"[headless-db-import] END_TEXT_LOG {text_block.name}")


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


def _disable_xsd_validation() -> None:
    scene = getattr(bpy.context, "scene", None)
    scene_props = getattr(scene, "cgml3", None)
    if scene_props is None:
        return
    if hasattr(scene_props, "xsd_validate"):
        scene_props.xsd_validate = False


def main() -> None:
    args = _parse_args()

    if not hasattr(bpy.ops, "cgml3") or not hasattr(bpy.ops.cgml3, "import_from_db"):
        _fail(
            f"Add-on operator cgml3.import_from_db not available. Start Blender with --addons {ADDON_MODULE}."
        )

    _configure_preferences(args)
    _disable_xsd_validation()

    scene_props = bpy.context.scene.cgml3
    scene_props.xsd_lenient_validation = bool(args.xsd_lenient)

    filter_crs = args.filter_crs
    bbox_for_log = ""
    if args.bbox:
        min_x, min_y, max_x, max_y, srid = _parse_bbox(args.bbox)
        scene_props.use_bbox_filter = True
        scene_props.bbox_min_x = min_x
        scene_props.bbox_min_y = min_y
        scene_props.bbox_max_x = max_x
        scene_props.bbox_max_y = max_y
        scene_props.bbox_coords_string = f"{min_x},{min_y},{max_x},{max_y}"
        bbox_for_log = args.bbox
        if srid and not filter_crs:
            filter_crs = srid if not srid.isdigit() else f"EPSG:{srid}"
    else:
        scene_props.use_bbox_filter = False

    output_blend = bpy.path.abspath(args.output_blend)

    print(f"[headless-db-import] output={output_blend}")
    print(
        "[headless-db-import] db="
        f"host={args.db_host}, port={args.db_port}, db={args.db_name}, schema={args.db_schema or '<default>'}"
    )
    if bbox_for_log:
        print(f"[headless-db-import] bbox={bbox_for_log}")

    try:
        result = bpy.ops.cgml3.import_from_db(
            "EXEC_DEFAULT",
            db_host=args.db_host,
            db_port=str(args.db_port),
            db_name=args.db_name,
            db_user=args.db_user,
            db_pass=args.db_pass,
            db_schema=args.db_schema,
            crs=args.crs,
            crs_name=args.crs_name,
            transform=args.transform,
            type_names=_split_csv(args.type_names),
            lod=_split_csv(args.lod),
            lod_mode=args.lod_mode,
            lod_search_depth=args.lod_search_depth,
            cql2_filter=args.cql2_filter,
            filter_crs=filter_crs,
            sql_filter=args.sql_filter,
            sort_by=_split_csv(args.sort_by),
            limit=args.limit,
            start_index=args.start_index,
            validity=args.validity,
            validity_at=args.validity_at,
            validity_between=args.validity_between,
            validity_reference=args.validity_reference,
            lenient_validity=args.lenient_validity,
            no_appearances=args.no_appearances,
            appearance_themes=_split_csv(args.appearance_themes),
            no_pretty_print=args.no_pretty_print,
            xsl_transform=args.xsl_transform,
            export_config=args.export_config,
        )
    except Exception as exc:
        print(f"[headless-db-import] operator_exception={exc}")
        _print_blender_text_logs(("citydb_export_", "citydb_import_", "citygml_xsd_check_"))
        raise
    print(f"[headless-db-import] operator_result={set(result)}")

    if "FINISHED" not in result:
        _print_blender_text_logs(("citydb_export_", "citydb_import_", "citygml_xsd_check_"))
        _fail(f"DB import operator did not finish successfully: {result}")

    # Viewport clip_end auf 100 km setzen (georeferenzierte Szenen)
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.clip_end = 100_000

    save_result = bpy.ops.wm.save_as_mainfile(filepath=output_blend)
    print(f"[headless-db-import] save_result={set(save_result)}")
    if "FINISHED" not in save_result:
        _fail(f"Failed to save blend file: {save_result}")

    print("[headless-db-import] completed")


if __name__ == "__main__":
    main()
