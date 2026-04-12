# =============================================================================
# QuickFix - cli/cli.py
# =============================================================================
# Terminal interface for QuickFix.
# Translates command-line arguments into core/controller.py calls.
# Contains zero business logic — all orchestration lives in core/.
#
# Usage:
#   python cli/cli.py --menu
#   python cli/cli.py --help
#   python cli/cli.py run   --file report.txt --plugin reverse_text_phrases
#   python cli/cli.py run   --file report.txt --plugin reverse_text_phrases --save
#   python cli/cli.py run   --file report.txt --plugin reverse_text_phrases --save-as out.txt
#   python cli/cli.py list  --file report.txt
#   python cli/cli.py info  --plugin reverse_text_phrases
#
# Exit codes:
#   0  - success
#   1  - user or input error (bad arguments, file not found, etc.)
#   2  - plugin execution error (timeout, exit code, integrity violation)
#   3  - cancelled by user (sandbox confirmation declined)
# =============================================================================

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Make core/ importable when called from project root or cli/ directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.controller import (
    Controller,
    ControllerError,
    EventKind,
    ExecutionCancelledError,
    NoFileOpenError,
    NoOutputError,
)
from core.loader import PluginError, PluginLoader

# -----------------------------------------------------------------------------
# Exit codes
# -----------------------------------------------------------------------------

EXIT_OK        = 0
EXIT_USER      = 1   # bad args, file not found, plugin not found
EXIT_EXEC      = 2   # plugin execution failed
EXIT_CANCELLED = 3   # user declined confirmation


# -----------------------------------------------------------------------------
# ANSI colors — same detection logic as run.sh / setup.sh
# -----------------------------------------------------------------------------

def _supports_color() -> bool:
    return (
        hasattr(sys.stdout, "isatty")
        and sys.stdout.isatty()
    )


class _C:
    """Color codes — empty strings when terminal does not support color."""
    if _supports_color():
        OK      = "\033[0;32m"
        WARN    = "\033[0;33m"
        FAIL    = "\033[0;31m"
        INFO    = "\033[0;34m"
        DIM     = "\033[0;90m"
        BOLD    = "\033[1m"
        RESET   = "\033[0m"
    else:
        OK = WARN = FAIL = INFO = DIM = BOLD = RESET = ""


# -----------------------------------------------------------------------------
# Output helpers
# -----------------------------------------------------------------------------

APP = "QuickFix"


def _info(msg: str) -> None:
    print(f"{_C.INFO}[{APP}]{_C.RESET} {msg}")


def _ok(msg: str) -> None:
    print(f"  {_C.OK}✓{_C.RESET} {msg}")


def _warn(msg: str) -> None:
    print(f"  {_C.WARN}⚠{_C.RESET} {msg}", file=sys.stderr)


def _err(msg: str) -> None:
    print(f"  {_C.FAIL}✗{_C.RESET} {msg}", file=sys.stderr)


def _progress(percent: int | None, msg: str) -> None:
    pct = f"{_C.DIM}[{percent:3d}%]{_C.RESET} " if percent is not None else "       "
    print(f"  {_C.INFO}●{_C.RESET} {pct}{msg}")


def _dim(msg: str) -> None:
    print(f"  {_C.DIM}{msg}{_C.RESET}")


# -----------------------------------------------------------------------------
# Confirmation callback — prompts the user interactively
# -----------------------------------------------------------------------------

def _confirm(message: str) -> bool:
    """
    Ask the user for explicit confirmation via stdin.
    Returns True if the user types 'y' or 'yes' (case-insensitive).
    Returns False on any other input or EOF (non-interactive).
    """
    _warn(message)
    if not sys.stdin.isatty():
        _err("Non-interactive terminal — unsandboxed execution requires confirmation.")
        return False
    try:
        answer = input(f"  {_C.WARN}Proceed? [y/N]{_C.RESET} ").strip().lower()
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# -----------------------------------------------------------------------------
# _plugins_dir — locate plugins/ relative to this file
# -----------------------------------------------------------------------------

def _plugins_dir() -> Path:
    return Path(__file__).parent.parent / "plugins"


# -----------------------------------------------------------------------------
# Command: run
# -----------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    """
    Open a file, execute a plugin, and optionally save the output.
    """
    file_path   = Path(args.file)
    plugin_name = args.plugin
    save        = args.save
    save_as     = Path(args.save_as) if args.save_as else None

    ctrl = Controller(
        plugins_dir=_plugins_dir(),
        confirm_cb=_confirm,
    )

    # --- Open file ---
    _info(f"Opening '{file_path.name}'...")
    try:
        mime = ctrl.open_file(file_path)
        _ok(f"{file_path.name}  {_C.DIM}({mime}){_C.RESET}")
    except ControllerError as exc:
        _err(str(exc))
        return EXIT_USER

    # --- Run plugin ---
    _info(f"Running plugin '{plugin_name}'...")
    print()

    execution_failed    = False
    integrity_violated  = False

    try:
        for event in ctrl.run_plugin(plugin_name):

            if event.kind == EventKind.MESSAGE:
                _dim(event.message)

            elif event.kind == EventKind.PROGRESS:
                _progress(event.percent, event.message)

            elif event.kind == EventKind.SANDBOX_WARNING:
                # _confirm was already called inside controller —
                # this event is just for display
                _warn(event.message)

            elif event.kind == EventKind.DONE:
                print()
                _ok(event.message)
                if event.output_path:
                    _dim(f"Output: {event.output_path}")

            elif event.kind == EventKind.ERROR:
                _err(event.message)
                execution_failed = True

            elif event.kind == EventKind.INTEGRITY_VIOLATION:
                print()
                _err("INTEGRITY VIOLATION — original file may have been tampered with.")
                _err(event.message)
                _warn("Forensic log written to ~/.local/share/quickfix/forensics/")
                integrity_violated = True

    except ExecutionCancelledError:
        print()
        _warn("Execution cancelled.")
        return EXIT_CANCELLED

    except ControllerError as exc:
        _err(str(exc))
        return EXIT_EXEC

    if integrity_violated:
        return EXIT_EXEC

    if execution_failed:
        return EXIT_EXEC

    if not ctrl.has_output:
        _err("No output produced.")
        return EXIT_EXEC

    # --- Save output ---
    if save_as:
        try:
            dest = ctrl.save_file_as(save_as)
            print()
            _ok(f"Saved as: {dest}")
        except (ControllerError, NoOutputError) as exc:
            _err(str(exc))
            return EXIT_EXEC

    elif save:
        try:
            dest = ctrl.save_file()
            print()
            _ok(f"Saved:    {dest}")
        except (ControllerError, NoOutputError) as exc:
            _err(str(exc))
            return EXIT_EXEC

    return EXIT_OK


# -----------------------------------------------------------------------------
# Command: list
# -----------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    """
    List plugins compatible with the given file's MIME type.
    """
    file_path = Path(args.file)

    ctrl = Controller(plugins_dir=_plugins_dir())

    try:
        mime = ctrl.open_file(file_path)
    except ControllerError as exc:
        _err(str(exc))
        return EXIT_USER

    plugins = ctrl.compatible_plugins()

    _info(f"File: {file_path.name}  {_C.DIM}({mime}){_C.RESET}")
    print()

    if not plugins:
        _warn(f"No plugins found for MIME type '{mime}'.")
        return EXIT_OK

    print(f"  {_C.BOLD}{'Plugin':<30} {'Version':<10} {'Runtime':<10} Description{_C.RESET}")
    print(f"  {'─' * 75}")

    for p in plugins:
        sandbox_icon = (
            f"{_C.OK}■{_C.RESET}" if p.sandbox.required
            else f"{_C.WARN}□{_C.RESET}"
        )
        print(
            f"  {sandbox_icon} {p.plugin.name:<28} "
            f"{_C.DIM}{p.plugin.version:<10}{_C.RESET} "
            f"{_C.DIM}{p.execution.runtime:<10}{_C.RESET} "
            f"{p.plugin.description}"
        )

    print()
    _dim(f"■ = sandboxed   □ = unsandboxed (requires confirmation)")

    return EXIT_OK


# -----------------------------------------------------------------------------
# Command: info
# -----------------------------------------------------------------------------

def cmd_info(args: argparse.Namespace) -> int:
    """
    Show detailed information about a specific plugin.
    """
    plugin_name = args.plugin
    loader      = PluginLoader(plugins_dir=_plugins_dir())

    try:
        config = loader.load(plugin_name)
    except PluginError as exc:
        _err(str(exc))
        return EXIT_USER

    p = config.plugin
    e = config.execution
    s = config.sandbox
    i = config.input
    o = config.output
    r = config.requirements
    g = config.gui

    sandbox_status = (
        f"{_C.OK}required  ({s.engine}){_C.RESET}"
        if s.required
        else f"{_C.WARN}not required{_C.RESET}"
    )

    print()
    print(f"  {_C.BOLD}{p.name}{_C.RESET}  {_C.DIM}v{p.version}{_C.RESET}")
    print(f"  {p.description}")
    print()
    print(f"  {_C.BOLD}Author{_C.RESET}    {p.author} <{p.contact}>")
    print(f"  {_C.BOLD}License{_C.RESET}   {p.license}")
    print(f"  {_C.BOLD}Runtime{_C.RESET}   {e.runtime}  →  {e.entrypoint}")
    print(f"  {_C.BOLD}Timeout{_C.RESET}   {e.timeout_seconds}s")
    print(f"  {_C.BOLD}Sandbox{_C.RESET}   {sandbox_status}")
    print(f"  {_C.BOLD}Input{_C.RESET}     {', '.join(i.accepts)}  "
          f"{_C.DIM}(max {i.max_size_mb} MB, {i.encoding}){_C.RESET}")
    print(f"  {_C.BOLD}Output{_C.RESET}    {o.produces}  "
          f"{_C.DIM}(suffix: {o.filename_suffix}){_C.RESET}")
    print(f"  {_C.BOLD}Requires{_C.RESET}  {', '.join(r.system_binaries) or 'none'}  "
          f"{_C.DIM}(disk: {r.min_free_disk_mb} MB){_C.RESET}")

    if g.has_own_window:
        print(f"  {_C.BOLD}GUI{_C.RESET}       opens own window"
              + (f" via {g.dialog_tool}" if g.dialog_tool else ""))
    if g.extra_input_required:
        print(f"  {_C.BOLD}Input UI{_C.RESET}  plugin requests extra input at runtime")

    # Check if help.md exists
    help_file = config.plugin_dir / "help.md"
    if help_file.is_file():
        print()
        _dim(f"Help: {help_file}")

    print()
    return EXIT_OK


# -----------------------------------------------------------------------------
# Menu
# -----------------------------------------------------------------------------

def _show_menu_help():
    print(f"""
quickfix> help

  Commands:

    list  --file PATH
          List plugins compatible with the file's MIME type.

    info  --plugin NAME
          Show plugin details (runtime, sandbox, requirements).

    run   --file PATH --plugin NAME [--save] [--save-as PATH]
          Execute a plugin. Output is kept alongside the original.
          --save          overwrite original with output
          --save-as PATH  save output to a new path

    help  Show this message.
    quit  Exit interactive mode.  (also: Ctrl+D)
    """)


def cmd_menu() -> int:
    """
    Interactive loop with readline support.
    Each iteration is fully independent — no state between commands.
    Type 'help' for available commands, 'quit' or Ctrl+D to exit.
    """
    import shlex
    try:
        import readline  # noqa: F401 — importing activates readline in input()
    except ImportError:
        pass  # readline not available on all platforms — degrades gracefully

    _info("Interactive mode  —  readline active")
    _dim("Commands: list | info | run | help | example | quit")
    _dim("Example:  run --file report.txt --plugin reverse_text_phrases")
    print()

    parser = _build_parser()

    while True:
        try:
            response = input("quickfix> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            _info("Exiting.")
            return EXIT_OK

        if not response:
            continue

        if response.lower() in ("quit", "exit", "q"):
            _info("Exiting.")
            return EXIT_OK

        if response.lower() in ("help", "h", "?"):
            cmd_menu_help()
            print()
            continue

        if response.lower() in ("example", "ex", "?"):
            # TO DO
            continue

        # shlex.split preserves quoted paths with spaces:
        # run --file "my report.txt" → ['run', '--file', 'my report.txt']
        try:
            tokens = shlex.split(response)
        except ValueError as exc:
            _err(f"Parse error: {exc}")
            print()
            continue

        # argparse calls sys.exit on error — capture to keep the loop alive
        try:
            args = parser.parse_args(tokens)
        except SystemExit:
            print()
            continue

        # --menu inside --menu is a no-op
        if getattr(args, "menu", False):
            _warn("Already in interactive mode.")
            print()
            continue

        if not args.command:
            cmd_menu_help()
            print()
            continue

        commands = {
            "run":  cmd_run,
            "list": cmd_list,
            "info": cmd_info,
        }
        handler = commands.get(args.command)
        if handler:
            handler(args)

        print()

    return EXIT_OK  # unreachable — satisfies type checker


# -----------------------------------------------------------------------------
# Argument parser
# -----------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:

    epilog="""examples:
  python cli/cli.py list --file report.txt
  python cli/cli.py info --plugin reverse_text_phrases
  python cli/cli.py run  --file report.txt --plugin reverse_text_phrases
  python cli/cli.py run  --file report.txt --plugin reverse_text_phrases --save
  python cli/cli.py run  --file report.txt --plugin reverse_text_phrases --save-as out.txt"""

    parser = argparse.ArgumentParser(
        prog="python cli/cli.py",
        description="QuickFix — file manipulation through sandboxed plugins",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )

    parser.add_argument(
        "--menu",
        action="store_true",
        help="Enter interactive menu mode with command history (readline)",
    )

    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging", )

    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = False

    # --- run ---
    run_p = sub.add_parser(
        "run",
        help="Execute a plugin on a file",
        description="Execute a plugin on a file and optionally save the output.",
    )
    run_p.add_argument("--file",   "-f", required=True, metavar="PATH",
                       help="Path to the file to process")
    run_p.add_argument("--plugin", "-p", required=True, metavar="NAME",
                       help="Plugin name to execute")
    run_p.add_argument("--save",         action="store_true",
                       help="Overwrite original file with plugin output")
    run_p.add_argument("--save-as",      metavar="PATH",
                       help="Save plugin output to a new path")

    # --- list ---
    list_p = sub.add_parser(
        "list",
        help="List plugins compatible with a file",
        description="List plugins that accept the MIME type of the given file.",
    )
    list_p.add_argument("--file", "-f", required=True, metavar="PATH",
                        help="Path to the file to check")

    # --- info ---
    info_p = sub.add_parser(
        "info",
        help="Show plugin details",
        description="Show detailed information about a specific plugin.",
    )
    info_p.add_argument("--plugin", "-p", required=True, metavar="NAME",
                        help="Plugin name to inspect")

    return parser


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main() -> int:
    parser = _build_parser()

    if len(sys.argv) == 1:
        parser.print_help()
        return EXIT_OK

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(levelname)s %(name)s: %(message)s",
        )

    if args.menu:
        return cmd_menu()

    if not args.command:
        parser.print_help()
        return EXIT_USER

    commands = {
        "run":  cmd_run,
        "list": cmd_list,
        "info": cmd_info,
    }

    handler = commands.get(args.command)
    if handler is None:
        parser.print_help()
        return EXIT_USER

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
