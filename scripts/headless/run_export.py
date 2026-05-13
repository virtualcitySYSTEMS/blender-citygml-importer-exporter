import argparse
import sys

import bpy


ADDON_MODULE = "citygml-importer-exporter"
SURFACE_TYPES = ("WallSurface", "RoofSurface", "GroundSurface", "ClosureSurface")
STREAMING_MODES = ("AUTO", "FORCE_STREAMING", "FORCE_DOM")
CITYGML_VERSIONS = ("2.0", "3.0")


def _extract_blender_args() -> list[str]:
    if "--" not in sys.argv:
        return []
    return sys.argv[sys.argv.index("--") + 1 :]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Headless CityGML export via Blender add-on operator cgml3.export_gml_file"
    )
    parser.add_argument("input_blend", help="Path to the input .blend file")
    parser.add_argument("output_gml", help="Path to the output .gml file")
    parser.add_argument(
        "--citygml-version",
        choices=CITYGML_VERSIONS,
        default="3.0",
        help="Target CityGML version",
    )
    parser.add_argument(
        "--srs-name",
        default="",
        help="Target CRS, e.g. EPSG:25832",
    )
    parser.add_argument(
        "--use-streaming",
        choices=STREAMING_MODES,
        default="AUTO",
        help="Exporter selection mode",
    )
    parser.add_argument(
        "--unclassified-surface-type",
        choices=SURFACE_TYPES,
        default="WallSurface",
        help="Fallback surface type for unclassified faces",
    )
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
    return parser.parse_args(_extract_blender_args())


def _fail(message: str, code: int = 1) -> None:
    print(f"[headless-export] ERROR: {message}")
    raise SystemExit(code)


def main() -> None:
    args = _parse_args()

    if not hasattr(bpy.ops, "cgml3") or not hasattr(bpy.ops.cgml3, "export_gml_file"):
        _fail(
            f"Add-on operator cgml3.export_gml_file not available. Start Blender with --addons {ADDON_MODULE}."
        )

    input_blend = bpy.path.abspath(args.input_blend)
    output_gml = bpy.path.abspath(args.output_gml)

    open_result = bpy.ops.wm.open_mainfile(filepath=input_blend)
    print(f"[headless-export] open_result={set(open_result)}")
    if "FINISHED" not in open_result:
        _fail(f"Failed to open blend file: {open_result}")

    print(f"[headless-export] input={input_blend}")
    print(f"[headless-export] output={output_gml}")
    print(
        "[headless-export] settings="
        f"citygml_version={args.citygml_version}, "
        f"srs_name={args.srs_name or '<auto>'}, "
        f"use_streaming={args.use_streaming}, "
        f"compress_textures={args.compress_textures}"
    )

    result = bpy.ops.cgml3.export_gml_file(
        "EXEC_DEFAULT",
        filepath=output_gml,
        citygml_version=args.citygml_version,
        srs_name=args.srs_name,
        write_offset_back=not args.no_write_offset_back,
        use_inner_outer_script=args.use_inner_outer_script,
        autofill_new_surfaces=args.autofill_new_surfaces,
        unclassified_surface_type=args.unclassified_surface_type,
        split_wall_roof_surfaces=args.split_wall_roof_surfaces,
        use_streaming=args.use_streaming,
        enable_memory_tracking=args.enable_memory_tracking,
        write_lod_solid_refs=args.write_lod_solid_refs,
        export_buildings=not args.no_buildings,
        export_bridges=not args.no_bridges,
        export_tunnels=not args.no_tunnels,
        export_vegetation=not args.no_vegetation,
        export_waterbodies=not args.no_waterbodies,
        export_construction=not args.no_construction,
        export_cityfurniture=not args.no_cityfurniture,
        export_landuse=not args.no_landuse,
        export_transportation=not args.no_transportation,
        export_relief=not args.no_relief,
        export_generics=not args.no_generics,
        compress_textures=args.compress_textures,
        texture_quality=args.texture_quality,
        texture_max_size=args.texture_max_size,
        texture_workers=args.texture_workers,
    )
    print(f"[headless-export] operator_result={set(result)}")

    if "FINISHED" not in result:
        _fail(f"Export operator did not finish successfully: {result}")

    print("[headless-export] completed")


if __name__ == "__main__":
    main()
