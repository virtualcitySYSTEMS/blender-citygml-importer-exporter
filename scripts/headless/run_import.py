import argparse
import sys

import bpy


ADDON_MODULE = "citygml-importer-exporter"


def _extract_blender_args() -> list[str]:
    if "--" not in sys.argv:
        return []
    return sys.argv[sys.argv.index("--") + 1 :]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Headless CityGML import via Blender add-on operator cgml3.import_gml_file"
    )
    parser.add_argument("input_gml", help="Path to the input CityGML .gml/.xml file")
    parser.add_argument("output_blend", help="Path to the output .blend file")
    parser.add_argument(
        "--no-appearance",
        action="store_true",
        help="Disable appearance, texture and material import",
    )
    parser.add_argument(
        "--xsd-validate",
        action="store_true",
        help="Enable XSD validation before import",
    )
    parser.add_argument(
        "--xsd-lenient",
        action="store_true",
        help="Treat known 3DCityDB XSD problems as warnings instead of hard failures",
    )
    return parser.parse_args(_extract_blender_args())


def _fail(message: str, code: int = 1) -> None:
    print(f"[headless-import] ERROR: {message}")
    raise SystemExit(code)


def main() -> None:
    args = _parse_args()

    if not hasattr(bpy.ops, "cgml3") or not hasattr(bpy.ops.cgml3, "import_gml_file"):
        _fail(
            f"Add-on operator cgml3.import_gml_file not available. Start Blender with --addons {ADDON_MODULE}."
        )

    scene_props = bpy.context.scene.cgml3
    scene_props.xsd_validate = bool(args.xsd_validate)
    scene_props.xsd_lenient_validation = bool(args.xsd_lenient)

    input_gml = bpy.path.abspath(args.input_gml)
    output_blend = bpy.path.abspath(args.output_blend)

    print(f"[headless-import] input={input_gml}")
    print(f"[headless-import] output={output_blend}")
    print(
        "[headless-import] settings="
        f"import_appearance={not args.no_appearance}, "
        f"xsd_validate={scene_props.xsd_validate}, "
        f"xsd_lenient_validation={scene_props.xsd_lenient_validation}"
    )

    result = bpy.ops.cgml3.import_gml_file(
        "EXEC_DEFAULT",
        filepath=input_gml,
        import_appearance=not args.no_appearance,
    )
    print(f"[headless-import] operator_result={set(result)}")

    if "FINISHED" not in result:
        _fail(f"Import operator did not finish successfully: {result}")

    save_result = bpy.ops.wm.save_as_mainfile(filepath=output_blend)
    print(f"[headless-import] save_result={set(save_result)}")

    if "FINISHED" not in save_result:
        _fail(f"Failed to save blend file: {save_result}")

    print("[headless-import] completed")


if __name__ == "__main__":
    main()
