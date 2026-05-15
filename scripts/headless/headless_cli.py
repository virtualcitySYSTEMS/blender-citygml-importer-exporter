import os
import sys


COMMANDS = {
    "import": "run_import",
    "export": "run_export",
    "db-import": "run_db_import",
    "db-export": "run_db_export",
}


def _extract_blender_args() -> list[str]:
    if "--" not in sys.argv:
        return []
    return sys.argv[sys.argv.index("--") + 1 :]


def _print_help() -> None:
    print("Headless CLI wrapper for the CityGML Blender add-on")
    print()
    print("Usage:")
    print("  blender --background --factory-startup --addons citygml_importer_exporter --python headless_cli.py -- <command> [args]")
    print()
    print("Commands:")
    print("  import      CityGML file -> .blend")
    print("  export      .blend -> CityGML file")
    print("  db-import   3DCityDB -> .blend")
    print("  db-export   .blend -> 3DCityDB")
    print()
    print("Examples:")
    print("  -- import input.gml output.blend")
    print("  -- export input.blend output.gml --citygml-version 3.0")
    print("  -- db-import output.blend --db-host localhost --db-name citydb --db-user postgres --db-pass secret")
    print("  -- db-export input.blend --db-host localhost --db-name citydb --db-user postgres --db-pass secret --with-appearances --overwrite-existing")


def _dispatch() -> int:
    blender_args = _extract_blender_args()
    if not blender_args or blender_args[0] in {"-h", "--help", "help"}:
        _print_help()
        return 0

    command = blender_args[0]
    module_name = COMMANDS.get(command)
    if module_name is None:
        print(f"Unknown command: {command}")
        print()
        _print_help()
        return 2

    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

    module = __import__(module_name)

    before_sep = sys.argv[: sys.argv.index("--") + 1]
    sys.argv = before_sep + blender_args[1:]

    try:
        module.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return code
    except Exception as exc:
        print(f"[headless-cli] ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_dispatch())
