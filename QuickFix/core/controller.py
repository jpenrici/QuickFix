# =============================================================================
# QuickFix - core/controller.py
# =============================================================================
# Public API of the QuickFix core — the single entry point for both
# gui/ and cli/. Neither interface contains any business logic.
#
# Responsibilities:
#   - Open a file and determine its MIME type
#   - Save a file (save / save-as)
#   - Discover plugins compatible with the currently open file
#   - Orchestrate a full plugin execution pipeline:
#       loader → session → sandbox → verifier → output
#   - Stream progress events to the caller via a callback
#   - Gate unsandboxed execution behind explicit caller confirmation
#   - Surface all errors as typed exceptions — never raw tracebacks
#
# What this module does NOT do:
#   - Display anything (no print, no GUI widgets)
#   - Read plugin output content (that is the plugin's or GUI's job)
#   - Make decisions about the UI (confirmations come IN as callbacks)
#
# Usage:
#   from core.controller import Controller, ControllerEvent
#
#   ctrl = Controller(plugins_dir=Path("plugins"))
#   ctrl.open_file(Path("report.txt"))
#
#   for event in ctrl.run_plugin("reverse_text_phrases"):
#       print(event)                     # ControllerEvent — progress, done, error
#
#   ctrl.save_file()                     # overwrite original with output
#   ctrl.save_file_as(Path("out.txt"))   # save to a new path
# =============================================================================

from __future__ import annotations

import logging
import mimetypes
import shutil
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Iterator

from core.loader  import PluginConfig, PluginError, PluginLoader
from core.sandbox import (
    ExecutionResult,
    MalformedEventError,
    PluginEvent,
    PluginExitError,
    PluginTimeoutError,
    SandboxError,
    SandboxNotAvailableError,
    SandboxRunner,
)
from core.session import (
    FileLockError,
    FileSizeError,
    MimeTypeError,
    PluginSession,
    SessionError,
)
from core.verifier import IntegrityError, verify_output_checksum

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Controller events — yielded to GUI / CLI in real time
# -----------------------------------------------------------------------------

class EventKind(Enum):
    PROGRESS         = auto()   # plugin reported progress
    MESSAGE          = auto()   # informational message from controller
    DONE             = auto()   # plugin completed successfully
    ERROR            = auto()   # recoverable error
    INTEGRITY_VIOLATION = auto() # original file was tampered with
    SANDBOX_WARNING  = auto()   # plugin runs without sandbox


@dataclass(frozen=True)
class ControllerEvent:
    """
    A single event emitted by the controller during a pipeline run.
    The GUI/CLI receives these and decides how to display them.
    """
    kind:        EventKind
    message:     str
    percent:     int | None    = None   # 0–100, only for PROGRESS
    output_path: Path | None   = None   # only for DONE
    plugin_name: str | None    = None


# -----------------------------------------------------------------------------
# Confirmation callback type
# Used by the controller to ask the caller whether to proceed
# when sandbox.required=false.
# Must return True to allow, False to cancel.
# -----------------------------------------------------------------------------

ConfirmCallback = Callable[[str], bool]


# -----------------------------------------------------------------------------
# ControllerError — wraps all internal exceptions for callers
# -----------------------------------------------------------------------------

class ControllerError(Exception):
    """Base class for all errors surfaced by the controller."""


class NoFileOpenError(ControllerError):
    """Raised when an operation requires an open file but none is loaded."""


class NoOutputError(ControllerError):
    """Raised when save is attempted but the plugin produced no output."""


class ExecutionCancelledError(ControllerError):
    """Raised when the user cancelled a confirmation prompt."""


# -----------------------------------------------------------------------------
# Controller
# -----------------------------------------------------------------------------

class Controller:
    """
    Orchestrates the full QuickFix pipeline.
    Stateful — holds the currently open file and last execution result.

    Args:
        plugins_dir: Path to the plugins/ directory.
        confirm_cb:  Callback invoked when user confirmation is required
                     (e.g. unsandboxed execution). Must return True to
                     proceed, False to cancel. Defaults to always-deny.
    """

    def __init__(
        self,
        plugins_dir: Path,
        confirm_cb: ConfirmCallback | None = None,
    ) -> None:
        self._plugins_dir  = plugins_dir
        self._loader       = PluginLoader(plugins_dir)
        self._confirm_cb   = confirm_cb or _deny_all
        self._open_file:   Path | None          = None
        self._open_mime:   str | None           = None
        self._last_result: ExecutionResult | None = None
        self._last_output: Path | None          = None

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def open_file(self, path: Path) -> str:
        """
        Load a file into the controller.

        Args:
            path: Path to the file to open.

        Returns:
            Detected MIME type string.

        Raises:
            ControllerError: File not found or MIME type undetectable.
        """
        if not path.is_file():
            raise ControllerError(f"File not found: {path}")

        mime, _ = mimetypes.guess_type(str(path))
        if mime is None:
            raise ControllerError(
                f"Cannot determine MIME type of '{path.name}'. "
                "The file extension may be missing or unrecognised."
            )

        self._open_file  = path.resolve()
        self._open_mime  = mime
        self._last_result = None
        self._last_output = None

        logger.info("Opened '%s' (%s)", path.name, mime)
        return mime

    def save_file(self) -> Path:
        """
        Overwrite the original file with the plugin's output.

        Returns:
            Path to the saved file.

        Raises:
            NoFileOpenError: No file is currently open.
            NoOutputError:   No plugin output available to save.
            ControllerError: Copy operation failed.
        """
        self._require_open_file()
        output = self._require_last_output()

        try:
            shutil.copy2(output, self._open_file)
        except OSError as exc:
            raise ControllerError(
                f"Failed to save file: {exc}"
            ) from exc

        logger.info("Saved output to '%s'", self._open_file)
        return self._open_file

    def save_file_as(self, dest: Path) -> Path:
        """
        Save the plugin's output to a new path.

        Args:
            dest: Destination path.

        Returns:
            Path to the saved file.

        Raises:
            NoFileOpenError: No file is currently open.
            NoOutputError:   No plugin output available to save.
            ControllerError: Copy operation failed.
        """
        self._require_open_file()
        output = self._require_last_output()

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output, dest)
        except OSError as exc:
            raise ControllerError(
                f"Failed to save file as '{dest}': {exc}"
            ) from exc

        logger.info("Saved output as '%s'", dest)
        return dest

    # ------------------------------------------------------------------
    # Plugin discovery
    # ------------------------------------------------------------------

    def compatible_plugins(self) -> list[PluginConfig]:
        """
        Return plugins that accept the MIME type of the currently open file.

        Raises:
            NoFileOpenError: No file is currently open.
        """
        self._require_open_file()

        all_plugins = self._loader.discover()
        return [
            p for p in all_plugins
            if self._open_mime in p.input.accepts
        ]

    def load_plugin(self, plugin_name: str) -> PluginConfig:
        """
        Load and validate a plugin by name.

        Raises:
            ControllerError: Plugin not found or invalid config.
        """
        try:
            return self._loader.load(plugin_name)
        except PluginError as exc:
            raise ControllerError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Plugin execution pipeline
    # ------------------------------------------------------------------

    def run_plugin(
        self,
        plugin_name: str,
    ) -> Iterator[ControllerEvent]:
        """
        Execute a plugin against the currently open file.

        Orchestrates the full pipeline:
            loader → session → sandbox → verifier → output

        Yields ControllerEvent objects in real time. The caller
        (GUI worker or CLI) consumes these to display progress.

        The last event is always either DONE or ERROR.

        Raises:
            NoFileOpenError:       No file is currently open.
            ControllerError:       Unrecoverable pipeline error.
            ExecutionCancelledError: User cancelled a confirmation prompt.
        """
        self._require_open_file()

        # --- Step 1: load and validate plugin ---
        yield ControllerEvent(
            kind=EventKind.MESSAGE,
            message=f"Loading plugin '{plugin_name}'...",
            plugin_name=plugin_name,
        )

        try:
            config = self._loader.load(plugin_name)
        except PluginError as exc:
            yield ControllerEvent(
                kind=EventKind.ERROR,
                message=f"Plugin load failed: {exc}",
                plugin_name=plugin_name,
            )
            return

        # --- Step 2: sandbox confirmation if required ---
        if not config.sandbox.required:
            yield ControllerEvent(
                kind=EventKind.SANDBOX_WARNING,
                message=(
                    f"Plugin '{plugin_name}' declares sandbox.required=false. "
                    "It will run WITHOUT isolation. "
                    "All responsibility lies with the plugin developer."
                ),
                plugin_name=plugin_name,
            )
            if not self._confirm_cb(
                f"Plugin '{plugin_name}' will run without sandbox. Proceed?"
            ):
                raise ExecutionCancelledError(
                    "User cancelled unsandboxed execution."
                )

        unsafe = not config.sandbox.required

        # --- Step 3: create session ---
        yield ControllerEvent(
            kind=EventKind.MESSAGE,
            message="Preparing session...",
            plugin_name=plugin_name,
        )

        try:
            with PluginSession(self._open_file, config) as session:

                yield ControllerEvent(
                    kind=EventKind.MESSAGE,
                    message=(
                        f"Session ready. Input: {session.input_file.name} "
                        f"(sha256={session.manifest.sha256[:12]}...)"
                    ),
                    plugin_name=plugin_name,
                )

                # --- Step 4: execute plugin ---
                yield ControllerEvent(
                    kind=EventKind.MESSAGE,
                    message=f"Executing '{plugin_name}'...",
                    plugin_name=plugin_name,
                )

                runner = SandboxRunner(config, session, unsafe_override=unsafe)

                execution_result: ExecutionResult | None = None

                try:
                    execution_result = runner.run_to_completion()
                except (SandboxNotAvailableError, SandboxError) as exc:
                    yield ControllerEvent(
                        kind=EventKind.ERROR,
                        message=f"Sandbox error: {exc}",
                        plugin_name=plugin_name,
                    )
                    return
                except PluginTimeoutError as exc:
                    yield ControllerEvent(
                        kind=EventKind.ERROR,
                        message=str(exc),
                        plugin_name=plugin_name,
                    )
                    return
                except MalformedEventError as exc:
                    yield ControllerEvent(
                        kind=EventKind.ERROR,
                        message=f"Plugin protocol error: {exc}",
                        plugin_name=plugin_name,
                    )
                    return

                # Relay plugin progress events to caller
                for event in execution_result.events:
                    if event.event == "progress":
                        yield ControllerEvent(
                            kind=EventKind.PROGRESS,
                            message=event.message or "",
                            percent=event.percent,
                            plugin_name=plugin_name,
                        )
                    elif event.event == "error":
                        yield ControllerEvent(
                            kind=EventKind.ERROR,
                            message=f"Plugin error [{event.error_code}]: "
                                    f"{event.message}",
                            plugin_name=plugin_name,
                        )
                        if event.is_fatal:
                            return

                if execution_result.timed_out:
                    yield ControllerEvent(
                        kind=EventKind.ERROR,
                        message=f"Plugin '{plugin_name}' timed out.",
                        plugin_name=plugin_name,
                    )
                    return

                # --- Step 5: verify original integrity ---
                yield ControllerEvent(
                    kind=EventKind.MESSAGE,
                    message="Verifying original file integrity...",
                    plugin_name=plugin_name,
                )

                try:
                    session.verify_integrity()
                except IntegrityError as exc:
                    yield ControllerEvent(
                        kind=EventKind.INTEGRITY_VIOLATION,
                        message=str(exc),
                        plugin_name=plugin_name,
                    )
                    return

                # --- Step 6: verify output checksum ---
                if not execution_result.output_file:
                    yield ControllerEvent(
                        kind=EventKind.ERROR,
                        message=(
                            f"Plugin '{plugin_name}' did not report an "
                            "output file in the 'done' event."
                        ),
                        plugin_name=plugin_name,
                    )
                    return

                output_path = session.output_dir / execution_result.output_file

                if execution_result.checksum:
                    try:
                        verify_output_checksum(
                            output_path,
                            execution_result.checksum,
                        )
                    except IntegrityError as exc:
                        yield ControllerEvent(
                            kind=EventKind.ERROR,
                            message=f"Output checksum mismatch: {exc}",
                            plugin_name=plugin_name,
                        )
                        return
                    except FileNotFoundError as exc:
                        yield ControllerEvent(
                            kind=EventKind.ERROR,
                            message=f"Output file not found: {exc}",
                            plugin_name=plugin_name,
                        )
                        return

                # --- Step 7: copy output to a stable location ---
                # The session tempdir will be deleted on exit — copy output
                # to a sibling path of the original so it survives cleanup.
                suffix    = config.output.filename_suffix
                stem      = self._open_file.stem
                extension = self._open_file.suffix
                stable    = self._open_file.parent / f"{stem}{suffix}{extension}"

                try:
                    shutil.copy2(output_path, stable)
                except OSError as exc:
                    yield ControllerEvent(
                        kind=EventKind.ERROR,
                        message=f"Failed to copy output: {exc}",
                        plugin_name=plugin_name,
                    )
                    return

                self._last_result = execution_result
                self._last_output = stable

                yield ControllerEvent(
                    kind=EventKind.DONE,
                    message=(
                        f"Done. Output saved as '{stable.name}'."
                    ),
                    percent=100,
                    output_path=stable,
                    plugin_name=plugin_name,
                )

        except FileLockError as exc:
            yield ControllerEvent(
                kind=EventKind.ERROR,
                message=str(exc),
                plugin_name=plugin_name,
            )
        except FileSizeError as exc:
            yield ControllerEvent(
                kind=EventKind.ERROR,
                message=str(exc),
                plugin_name=plugin_name,
            )
        except MimeTypeError as exc:
            yield ControllerEvent(
                kind=EventKind.ERROR,
                message=str(exc),
                plugin_name=plugin_name,
            )
        except SessionError as exc:
            yield ControllerEvent(
                kind=EventKind.ERROR,
                message=f"Session error: {exc}",
                plugin_name=plugin_name,
            )

    # ------------------------------------------------------------------
    # State accessors
    # ------------------------------------------------------------------

    @property
    def current_file(self) -> Path | None:
        """Currently open file path, or None."""
        return self._open_file

    @property
    def current_mime(self) -> str | None:
        """MIME type of the currently open file, or None."""
        return self._open_mime

    @property
    def last_output(self) -> Path | None:
        """Path to the last successful plugin output, or None."""
        return self._last_output

    @property
    def has_output(self) -> bool:
        """True if a plugin output is available to save."""
        return self._last_output is not None and self._last_output.is_file()

    # ------------------------------------------------------------------
    # Internal guards
    # ------------------------------------------------------------------

    def _require_open_file(self) -> None:
        if self._open_file is None:
            raise NoFileOpenError(
                "No file is currently open. Call open_file() first."
            )

    def _require_last_output(self) -> Path:
        if self._last_output is None or not self._last_output.is_file():
            raise NoOutputError(
                "No plugin output available. Run a plugin first."
            )
        return self._last_output


# -----------------------------------------------------------------------------
# Default confirmation callback — denies everything
# -----------------------------------------------------------------------------

def _deny_all(message: str) -> bool:
    """Default confirm callback — always denies. Safe default."""
    logger.warning("Confirmation denied (no callback set): %s", message)
    return False
