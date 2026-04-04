# =============================================================================
# QuickFix - core/loader.py
# =============================================================================
# Discovers, validates, and loads plugins from the plugins/ directory.
#
# Responsibilities:
#   - Parse and strictly validate config.json against the full schema
#   - Reject any plugin with missing, empty, ambiguous, or forbidden values
#   - Return a clean PluginConfig dataclass — callers never touch raw dicts
#   - Provide a CLI interface for setup.sh integration (--validate flag)
#
# Usage (programmatic):
#   from core.loader import PluginLoader
#   loader = PluginLoader(plugins_dir=Path("plugins"))
#   config = loader.load("reverse_text_phrases")   # raises PluginError on failure
#   plugins = loader.discover()                    # returns list[PluginConfig]
#
# Usage (CLI — called by setup.sh):
#   python core/loader.py --validate plugins/reverse_text_phrases/config.json
#
# Exit codes (CLI):
#   0 - valid
#   1 - invalid (errors printed to stderr)
# =============================================================================

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# -----------------------------------------------------------------------------
# Allowlists — only values present here are accepted
# -----------------------------------------------------------------------------

ALLOWED_RUNTIMES: frozenset[str] = frozenset({
    "bash",
    "binary",
    "lua",
    "perl",
    "python3",
    "ruby",
})

ALLOWED_SANDBOX_ENGINES: frozenset[str] = frozenset({
    "bubblewrap",
    "firejail",
})

ALLOWED_DIALOG_TOOLS: frozenset[str | None] = frozenset({
    None,
    "yad",
    "zenity",
})

ALLOWED_OS: frozenset[str] = frozenset({
    "linux",
})

# Validates a MIME type: must be "type/subtype" with no wildcards
# Accepts: "text/plain", "image/png"
# Rejects: "*", "text/*", "*/plain", "text/"
_VALID_MIME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9!#$&\-^_]*\/[a-zA-Z0-9][a-zA-Z0-9!#$&\-^_.+]*$")

# Semver pattern — major.minor.patch only
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Plugin name pattern — lowercase, digits, underscores only
_PLUGIN_NAME_RE = re.compile(r"^[a-z0-9_]+$")

# Timeout bounds (seconds)
_TIMEOUT_MIN = 1
_TIMEOUT_MAX = 300

# File size bounds (MB)
_MAX_SIZE_MIN = 1
_MAX_SIZE_MAX = 500

# Disk bounds (MB)
_DISK_MIN = 1
_DISK_MAX = 10_000


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class PluginError(Exception):
    """Raised when a plugin fails any validation check."""


class PluginNotFoundError(PluginError):
    """Raised when a plugin directory or config.json cannot be found."""


# -----------------------------------------------------------------------------
# PluginConfig — immutable dataclass returned to callers
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class PluginMeta:
    name:        str
    version:     str
    description: str
    author:      str
    contact:     str
    license:     str


@dataclass(frozen=True)
class PluginExecution:
    runtime:         str
    entrypoint:      str
    timeout_seconds: int
    args_extra:      bool


@dataclass(frozen=True)
class PluginSandbox:
    required:           bool
    engine:             str
    allow_network:      bool
    allow_new_processes: bool
    writable_paths:     tuple[str, ...]


@dataclass(frozen=True)
class PluginInput:
    accepts:     tuple[str, ...]
    max_size_mb: int
    encoding:    str


@dataclass(frozen=True)
class PluginOutput:
    produces:        str
    filename_suffix: str
    overwrites_input: bool


@dataclass(frozen=True)
class PluginRequirements:
    system_binaries: tuple[str, ...]
    min_free_disk_mb: int
    os:              str


@dataclass(frozen=True)
class PluginGui:
    has_own_window:       bool
    dialog_tool:          str | None
    extra_input_required: bool


@dataclass(frozen=True)
class PluginConfig:
    """
    Fully validated plugin configuration.
    Constructed exclusively by PluginLoader — never instantiated directly.
    """
    plugin:       PluginMeta
    execution:    PluginExecution
    sandbox:      PluginSandbox
    input:        PluginInput
    output:       PluginOutput
    requirements: PluginRequirements
    gui:          PluginGui

    # Path to the plugin directory — set by loader after validation
    plugin_dir: Path = field(compare=False)


# -----------------------------------------------------------------------------
# Validator — internal, not for direct use
# -----------------------------------------------------------------------------

class _ConfigValidator:
    """
    Validates a raw dict parsed from config.json.
    Collects all errors before raising — the user sees everything at once.
    """

    def __init__(self, config: dict[str, Any], config_path: Path) -> None:
        self._cfg = config
        self._path = config_path
        self._errors: list[str] = []

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def validate(self) -> PluginConfig:
        """
        Run all validations. Returns PluginConfig on success.
        Raises PluginError with all collected errors on failure.
        """
        meta         = self._validate_plugin()
        execution    = self._validate_execution()
        sandbox      = self._validate_sandbox()
        input_cfg    = self._validate_input()
        output_cfg   = self._validate_output()
        requirements = self._validate_requirements()
        gui          = self._validate_gui()

        if self._errors:
            formatted = "\n".join(f"  - {e}" for e in self._errors)
            raise PluginError(
                f"Plugin config invalid: {self._path}\n{formatted}"
            )

        return PluginConfig(
            plugin=meta,
            execution=execution,
            sandbox=sandbox,
            input=input_cfg,
            output=output_cfg,
            requirements=requirements,
            gui=gui,
            plugin_dir=self._path.parent,
        )

    # ------------------------------------------------------------------
    # Section validators
    # ------------------------------------------------------------------

    def _validate_plugin(self) -> PluginMeta:
        sec = self._require_section("plugin")

        name        = self._require_str(sec, "plugin.name")
        version     = self._require_str(sec, "plugin.version")
        description = self._require_str(sec, "plugin.description")
        author      = self._require_str(sec, "plugin.author")
        contact     = self._require_str(sec, "plugin.contact")
        license_    = self._require_str(sec, "plugin.license")

        if name and not _PLUGIN_NAME_RE.match(name):
            self._errors.append(
                f"plugin.name '{name}' is invalid — "
                "use lowercase letters, digits, and underscores only"
            )

        if version and not _SEMVER_RE.match(version):
            self._errors.append(
                f"plugin.version '{version}' is invalid — use semver (e.g. 1.0.0)"
            )

        if description and len(description.strip()) < 10:
            self._errors.append(
                "plugin.description is too short — provide a meaningful description"
            )

        return PluginMeta(
            name=name,
            version=version,
            description=description,
            author=author,
            contact=contact,
            license=license_,
        )

    def _validate_execution(self) -> PluginExecution:
        sec = self._require_section("execution")

        runtime    = self._require_str(sec, "execution.runtime")
        entrypoint = self._require_str(sec, "execution.entrypoint")
        timeout    = self._require_int(sec, "execution.timeout_seconds")
        args_extra = self._require_bool(sec, "execution.args_extra")

        if runtime and runtime not in ALLOWED_RUNTIMES:
            self._errors.append(
                f"execution.runtime '{runtime}' is not allowed — "
                f"allowed: {sorted(ALLOWED_RUNTIMES)}"
            )

        if entrypoint and ("/" in entrypoint or "\\" in entrypoint):
            self._errors.append(
                f"execution.entrypoint '{entrypoint}' must be a filename, not a path"
            )

        if timeout is not None and not (_TIMEOUT_MIN <= timeout <= _TIMEOUT_MAX):
            self._errors.append(
                f"execution.timeout_seconds {timeout} is out of range "
                f"[{_TIMEOUT_MIN}, {_TIMEOUT_MAX}]"
            )

        return PluginExecution(
            runtime=runtime,
            entrypoint=entrypoint,
            timeout_seconds=timeout or 0,
            args_extra=args_extra or False,
        )

    def _validate_sandbox(self) -> PluginSandbox:
        sec = self._require_section("sandbox")

        required            = self._require_bool(sec, "sandbox.required")
        engine              = self._require_str(sec, "sandbox.engine")
        allow_network       = self._require_bool(sec, "sandbox.allow_network")
        allow_new_processes = self._require_bool(sec, "sandbox.allow_new_processes")
        writable_paths      = self._require_list_str(sec, "sandbox.writable_paths")

        if engine and engine not in ALLOWED_SANDBOX_ENGINES:
            self._errors.append(
                f"sandbox.engine '{engine}' is not allowed — "
                f"allowed: {sorted(ALLOWED_SANDBOX_ENGINES)}"
            )

        # sandbox.required=false is allowed but flagged.
        # The controller is responsible for prompting the user before execution.
        # No error is added here — the PluginConfig.sandbox.required field
        # carries this information to the caller.

        if writable_paths is not None:
            for p in writable_paths:
                if p != "OUTPUT_DIR":
                    self._errors.append(
                        f"sandbox.writable_paths contains '{p}' — "
                        "only 'OUTPUT_DIR' is allowed"
                    )

        return PluginSandbox(
            required=required or False,
            engine=engine,
            allow_network=allow_network or False,
            allow_new_processes=allow_new_processes or False,
            writable_paths=tuple(writable_paths or []),
        )

    def _validate_input(self) -> PluginInput:
        sec = self._require_section("input")

        accepts     = self._require_list_str(sec, "input.accepts")
        max_size_mb = self._require_int(sec, "input.max_size_mb")
        encoding    = self._require_str(sec, "input.encoding")

        if accepts is not None:
            if len(accepts) == 0:
                self._errors.append(
                    "input.accepts is empty — specify at least one MIME type"
                )
            for mime in accepts:
                if "*" in mime or not _VALID_MIME_RE.match(mime):
                    self._errors.append(
                        f"input.accepts '{mime}' is invalid — "
                        "wildcards and partial types are not allowed. "
                        "Use specific MIME types (e.g. 'text/plain', 'image/png')"
                    )

        if max_size_mb is not None and not (_MAX_SIZE_MIN <= max_size_mb <= _MAX_SIZE_MAX):
            self._errors.append(
                f"input.max_size_mb {max_size_mb} is out of range "
                f"[{_MAX_SIZE_MIN}, {_MAX_SIZE_MAX}]"
            )

        if encoding and encoding.strip().lower() not in ("utf-8", "utf-16", "ascii", "latin-1"):
            self._errors.append(
                f"input.encoding '{encoding}' is not a recognised safe encoding"
            )

        return PluginInput(
            accepts=tuple(accepts or []),
            max_size_mb=max_size_mb or 0,
            encoding=encoding,
        )

    def _validate_output(self) -> PluginOutput:
        sec = self._require_section("output")

        produces         = self._require_str(sec, "output.produces")
        filename_suffix  = self._require_str(sec, "output.filename_suffix")
        overwrites_input = self._require_bool(sec, "output.overwrites_input")

        if produces and ("*" in produces or not _VALID_MIME_RE.match(produces)):
            self._errors.append(
                f"output.produces '{produces}' is invalid — "
                "use a specific MIME type (e.g. 'text/plain', 'image/png')"
            )

        if filename_suffix is not None:
            if not filename_suffix.startswith("_"):
                self._errors.append(
                    f"output.filename_suffix '{filename_suffix}' must start with '_'"
                )
            if len(filename_suffix.strip()) < 2:
                self._errors.append(
                    "output.filename_suffix is too short — provide a meaningful suffix"
                )

        # Hard rule — overwrites_input must be literally false
        if overwrites_input is not False:
            self._errors.append(
                "output.overwrites_input must be false — "
                "plugins are never allowed to overwrite the original file"
            )

        return PluginOutput(
            produces=produces,
            filename_suffix=filename_suffix,
            overwrites_input=False,  # always forced to False regardless of input
        )

    def _validate_requirements(self) -> PluginRequirements:
        sec = self._require_section("requirements")

        system_binaries  = self._require_list_str(sec, "requirements.system_binaries")
        min_free_disk_mb = self._require_int(sec, "requirements.min_free_disk_mb")
        os_              = self._require_str(sec, "requirements.os")

        if os_ and os_ not in ALLOWED_OS:
            self._errors.append(
                f"requirements.os '{os_}' is not allowed — "
                f"allowed: {sorted(ALLOWED_OS)}"
            )

        if min_free_disk_mb is not None and not (_DISK_MIN <= min_free_disk_mb <= _DISK_MAX):
            self._errors.append(
                f"requirements.min_free_disk_mb {min_free_disk_mb} is out of range "
                f"[{_DISK_MIN}, {_DISK_MAX}]"
            )

        return PluginRequirements(
            system_binaries=tuple(system_binaries or []),
            min_free_disk_mb=min_free_disk_mb or 0,
            os=os_,
        )

    def _validate_gui(self) -> PluginGui:
        sec = self._require_section("gui")

        has_own_window       = self._require_bool(sec, "gui.has_own_window")
        dialog_tool          = self._require_nullable_str(sec, "gui.dialog_tool")
        extra_input_required = self._require_bool(sec, "gui.extra_input_required")

        if dialog_tool is not None and dialog_tool not in ALLOWED_DIALOG_TOOLS:
            self._errors.append(
                f"gui.dialog_tool '{dialog_tool}' is not allowed — "
                f"allowed: {sorted(str(t) for t in ALLOWED_DIALOG_TOOLS if t)}"
            )

        return PluginGui(
            has_own_window=has_own_window or False,
            dialog_tool=dialog_tool,
            extra_input_required=extra_input_required or False,
        )

    # ------------------------------------------------------------------
    # Low-level field extractors
    # ------------------------------------------------------------------

    def _require_section(self, key: str) -> dict[str, Any]:
        val = self._cfg.get(key)
        if not isinstance(val, dict) or not val:
            self._errors.append(f"'{key}' section is missing or empty")
            return {}
        return val

    def _require_str(self, sec: dict, path: str) -> str:
        key = path.split(".")[-1]
        val = sec.get(key)
        if val is None:
            self._errors.append(f"'{path}' is required")
            return ""
        if not isinstance(val, str):
            self._errors.append(f"'{path}' must be a string, got {type(val).__name__}")
            return ""
        if not val.strip():
            self._errors.append(f"'{path}' must not be empty or whitespace-only")
            return ""
        return val

    def _require_nullable_str(self, sec: dict, path: str) -> str | None:
        key = path.split(".")[-1]
        if key not in sec:
            self._errors.append(f"'{path}' is required (use null if not applicable)")
            return None
        val = sec[key]
        if val is None:
            return None
        if not isinstance(val, str):
            self._errors.append(f"'{path}' must be a string or null")
            return None
        if not val.strip():
            self._errors.append(f"'{path}' must not be empty — use null if not applicable")
            return None
        return val

    def _require_bool(self, sec: dict, path: str) -> bool | None:
        key = path.split(".")[-1]
        val = sec.get(key)
        if val is None:
            self._errors.append(f"'{path}' is required")
            return None
        if not isinstance(val, bool):
            self._errors.append(
                f"'{path}' must be a boolean (true/false), got {type(val).__name__}"
            )
            return None
        return val

    def _require_int(self, sec: dict, path: str) -> int | None:
        key = path.split(".")[-1]
        val = sec.get(key)
        if val is None:
            self._errors.append(f"'{path}' is required")
            return None
        # JSON booleans are subclass of int in Python — reject them explicitly
        if isinstance(val, bool) or not isinstance(val, int):
            self._errors.append(
                f"'{path}' must be an integer, got {type(val).__name__}"
            )
            return None
        return val

    def _require_list_str(self, sec: dict, path: str) -> list[str] | None:
        key = path.split(".")[-1]
        val = sec.get(key)
        if val is None:
            self._errors.append(f"'{path}' is required")
            return None
        if not isinstance(val, list):
            self._errors.append(f"'{path}' must be a list")
            return None
        for i, item in enumerate(val):
            if not isinstance(item, str) or not item.strip():
                self._errors.append(
                    f"'{path}[{i}]' must be a non-empty string"
                )
        return val


# -----------------------------------------------------------------------------
# PluginLoader — public API
# -----------------------------------------------------------------------------

class PluginLoader:
    """
    Discovers and loads plugins from a given directory.

    Each plugin must reside in its own subdirectory named after the plugin,
    containing at minimum a config.json and the declared entrypoint file.
    """

    def __init__(self, plugins_dir: Path) -> None:
        self._plugins_dir = plugins_dir

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def load(self, plugin_name: str) -> PluginConfig:
        """
        Load and validate a single plugin by name.

        Args:
            plugin_name: Must match the plugin directory name exactly.

        Returns:
            PluginConfig on success.

        Raises:
            PluginNotFoundError: Plugin directory or config.json not found.
            PluginError: Validation failed — message contains all errors.
        """
        plugin_dir  = self._plugins_dir / plugin_name
        config_path = plugin_dir / "config.json"

        if not plugin_dir.is_dir():
            raise PluginNotFoundError(
                f"Plugin directory not found: {plugin_dir}"
            )

        if not config_path.is_file():
            raise PluginNotFoundError(
                f"config.json not found in plugin directory: {plugin_dir}"
            )

        config = self._load_and_validate(config_path)

        # Verify the declared entrypoint file actually exists
        entrypoint = plugin_dir / config.execution.entrypoint
        if not entrypoint.is_file():
            raise PluginError(
                f"Plugin '{plugin_name}': entrypoint not found: {entrypoint}"
            )

        # Verify the plugin name in config matches the directory name
        if config.plugin.name != plugin_name:
            raise PluginError(
                f"Plugin directory name '{plugin_name}' does not match "
                f"config.json plugin.name '{config.plugin.name}'"
            )

        return config

    def discover(self) -> list[PluginConfig]:
        """
        Discover all valid plugins in the plugins directory.

        Invalid plugins are skipped and their errors are printed to stderr.
        Returns a (possibly empty) list of valid PluginConfig instances.
        """
        if not self._plugins_dir.is_dir():
            return []

        results: list[PluginConfig] = []

        for plugin_dir in sorted(self._plugins_dir.iterdir()):
            if not plugin_dir.is_dir():
                continue

            try:
                config = self.load(plugin_dir.name)
                results.append(config)
            except PluginError as exc:
                print(f"[loader] Skipping '{plugin_dir.name}': {exc}", file=sys.stderr)

        return results

    def accepts_mime(self, config: PluginConfig, mime_type: str) -> bool:
        """
        Return True if the plugin declares support for the given MIME type.
        Exact match only — no wildcards.
        """
        return mime_type in config.input.accepts

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_and_validate(self, config_path: Path) -> PluginConfig:
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PluginError(
                f"config.json is not valid JSON: {config_path}\n  {exc}"
            ) from exc
        except OSError as exc:
            raise PluginError(
                f"Cannot read config.json: {config_path}\n  {exc}"
            ) from exc

        if not isinstance(raw, dict):
            raise PluginError(
                f"config.json must be a JSON object: {config_path}"
            )

        validator = _ConfigValidator(raw, config_path)
        config = validator.validate()

        # Surface sandbox warning — not a hard failure, just informational
        if not config.sandbox.required:
            print(
                f"[loader] WARNING: plugin '{config.plugin.name}' runs without sandbox. "
                "User confirmation will be required before execution.",
                file=sys.stderr,
            )

        return config


# -----------------------------------------------------------------------------
# CLI interface — called by setup.sh
# -----------------------------------------------------------------------------

def _cli() -> None:
    parser = argparse.ArgumentParser(
        prog="loader.py",
        description="Validate a QuickFix plugin config.json",
    )
    parser.add_argument(
        "--validate",
        metavar="CONFIG_PATH",
        required=True,
        help="Path to the plugin config.json to validate",
    )
    args = parser.parse_args()

    config_path = Path(args.validate)

    if not config_path.is_file():
        print(f"[loader] ERROR: file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    plugins_dir = config_path.parent.parent
    plugin_name = config_path.parent.name

    loader = PluginLoader(plugins_dir=plugins_dir)

    try:
        config = loader.load(plugin_name)
        print(f"[loader] OK: '{config.plugin.name}' v{config.plugin.version}")
        sys.exit(0)
    except PluginError as exc:
        print(f"[loader] FAIL: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _cli()
