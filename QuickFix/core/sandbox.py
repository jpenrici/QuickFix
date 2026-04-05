# =============================================================================
# QuickFix - core/sandbox.py
# =============================================================================
# Plugin execution inside an isolated sandbox.
#
# Responsibilities:
#   - Build the bubblewrap or firejail command for a given session
#   - Execute the plugin as a subprocess with timeout enforcement
#   - Stream and parse JSONL events from the plugin's stdout in real time
#   - Capture stderr for crash diagnostics
#   - Enforce exit code contract and surface structured errors
#   - Emit a SandboxWarning when sandbox.required=false and proceed
#     only after explicit acknowledgment from the caller
#
# Execution model:
#   The plugin receives exactly two positional arguments:
#     $1 — absolute path to the read-only input file (inside session tempdir)
#     $2 — absolute path to the writable output directory (inside session tempdir)
#
#   The plugin communicates via JSONL on stdout:
#     {"event": "start",    "timestamp": "..."}
#     {"event": "progress", "percent": 40, "message": "..."}
#     {"event": "done",     "output_file": "...", "checksum_sha256": "..."}
#     {"event": "error",    "code": "...", "message": "...", "fatal": true}
#
# Usage:
#   from core.sandbox import SandboxRunner
#
#   runner = SandboxRunner(plugin_config, session)
#   for event in runner.run():
#       print(event)   # yields PluginEvent dataclasses in real time
#
# This module never reads or writes files directly — only via subprocess.
# =============================================================================

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator, Iterator

from core.loader import PluginConfig
from core.session import PluginSession

logger = logging.getLogger(__name__)

# Runtime → system binary name mapping
_RUNTIME_BINS: dict[str, str] = {
    "bash":    "bash",
    "lua":     "lua5.4",
    "python3": "python3",
    "ruby":    "ruby",
    "perl":    "perl",
    "binary":  None,   # executed directly — no interpreter prefix
}


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class SandboxError(Exception):
    """Raised when the sandbox cannot be constructed or the plugin crashes."""


class SandboxNotAvailableError(SandboxError):
    """Raised when sandbox.required=true but no sandbox engine is installed."""


class PluginTimeoutError(SandboxError):
    """Raised when the plugin exceeds its declared timeout_seconds."""


class PluginExitError(SandboxError):
    """Raised when the plugin exits with a non-zero exit code."""


class MalformedEventError(SandboxError):
    """Raised when the plugin emits a stdout line that is not valid JSONL."""


# -----------------------------------------------------------------------------
# Plugin events — yielded to the caller in real time
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class PluginEvent:
    """
    A single structured event emitted by the plugin via stdout JSONL.
    Unknown fields are preserved in `extra` for forward compatibility.
    """
    event:    str                    # "start" | "progress" | "done" | "error"
    raw:      dict                   # original parsed dict
    extra:    dict = field(default_factory=dict)

    # Convenience accessors — None if field not present
    @property
    def percent(self) -> int | None:
        return self.raw.get("percent")

    @property
    def message(self) -> str | None:
        return self.raw.get("message")

    @property
    def output_file(self) -> str | None:
        return self.raw.get("output_file")

    @property
    def checksum_sha256(self) -> str | None:
        return self.raw.get("checksum_sha256")

    @property
    def error_code(self) -> str | None:
        return self.raw.get("code")

    @property
    def is_fatal(self) -> bool:
        return bool(self.raw.get("fatal", False))


@dataclass(frozen=True)
class ExecutionResult:
    """
    Summary of a completed plugin execution.
    Returned by SandboxRunner.run_to_completion().
    """
    events:       list[PluginEvent]
    exit_code:    int
    stderr_dump:  str              # raw stderr — empty string if clean
    output_file:  str | None       # from the 'done' event
    checksum:     str | None       # from the 'done' event
    timed_out:    bool = False


# -----------------------------------------------------------------------------
# SandboxRunner — public API
# -----------------------------------------------------------------------------

class SandboxRunner:
    """
    Builds and executes a plugin command inside a sandbox.

    Args:
        config:  Validated PluginConfig from loader.py
        session: Active PluginSession from session.py
        unsafe_override: If True, allows running unsandboxed plugins
                         without raising. The controller must set this
                         only after explicit user confirmation.
    """

    def __init__(
        self,
        config:           PluginConfig,
        session:          PluginSession,
        unsafe_override:  bool = False,
    ) -> None:
        self._config          = config
        self._session         = session
        self._unsafe_override = unsafe_override

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def run(self) -> Iterator[PluginEvent]:
        """
        Execute the plugin and yield PluginEvent objects in real time
        as they arrive on stdout.

        Raises:
            SandboxNotAvailableError: sandbox required but engine missing.
            PluginTimeoutError:       plugin exceeded timeout_seconds.
            PluginExitError:          plugin exited with non-zero code.
            MalformedEventError:      plugin emitted non-JSONL on stdout.
        """
        self._check_sandbox_availability()
        cmd = self._build_command()

        logger.info(
            "Running plugin '%s' | sandbox=%s | cmd=%s",
            self._config.plugin.name,
            self._config.sandbox.engine if self._config.sandbox.required else "none",
            " ".join(str(c) for c in cmd),
        )

        yield from self._execute(cmd)

    def run_to_completion(self) -> ExecutionResult:
        """
        Execute the plugin, collect all events, and return an ExecutionResult.
        Convenience wrapper around run() for callers that do not need
        real-time streaming.
        """
        events: list[PluginEvent] = []
        timed_out = False

        try:
            for event in self.run():
                events.append(event)
        except PluginTimeoutError:
            timed_out = True
        except PluginExitError:
            pass   # exit code is in ExecutionResult

        done = next(
            (e for e in reversed(events) if e.event == "done"), None
        )

        return ExecutionResult(
            events=events,
            exit_code=getattr(self, "_last_exit_code", -1),
            stderr_dump=getattr(self, "_last_stderr", ""),
            output_file=done.output_file if done else None,
            checksum=done.checksum_sha256 if done else None,
            timed_out=timed_out,
        )

    def build_command_preview(self) -> list[str]:
        """
        Return the command that would be executed, without running it.
        Useful for logging and dry-run modes.
        """
        self._check_sandbox_availability()
        return [str(c) for c in self._build_command()]

    # ------------------------------------------------------------------
    # Command builders
    # ------------------------------------------------------------------

    def _build_command(self) -> list[str]:
        """
        Assemble the full execution command.

        If sandbox is required and available, wraps with bubblewrap or firejail.
        If sandbox.required=false and unsafe_override=True, runs directly.
        """
        plugin_cmd = self._build_plugin_command()

        if not self._config.sandbox.required:
            return plugin_cmd

        engine = self._config.sandbox.engine
        if engine == "bubblewrap":
            return self._wrap_bubblewrap(plugin_cmd)
        if engine == "firejail":
            return self._wrap_firejail(plugin_cmd)

        raise SandboxError(
            f"Unknown sandbox engine: '{engine}'"
        )

    def _build_plugin_command(self) -> list[str]:
        """
        Build the bare plugin invocation (interpreter + entrypoint + args).
        """
        runtime    = self._config.execution.runtime
        entrypoint = self._session.session_dir / "input" / ".." / ".."
        # entrypoint lives in the plugin directory, not the session
        plugin_dir = self._config.plugin_dir
        entry_file = plugin_dir / self._config.execution.entrypoint

        input_file = str(self._session.input_file)
        output_dir = str(self._session.output_dir)

        if runtime == "binary":
            return [str(entry_file), input_file, output_dir]

        bin_name = _RUNTIME_BINS.get(runtime)
        if not bin_name:
            raise SandboxError(
                f"No binary mapping for runtime '{runtime}'"
            )

        runtime_bin = shutil.which(bin_name)
        if not runtime_bin:
            raise SandboxError(
                f"Runtime binary '{bin_name}' not found in PATH. "
                f"Plugin '{self._config.plugin.name}' requires it."
            )

        return [runtime_bin, str(entry_file), input_file, output_dir]

    def _wrap_bubblewrap(self, plugin_cmd: list[str]) -> list[str]:
        """
        Wrap the plugin command with bubblewrap (bwrap).

        Security profile:
          - New empty root filesystem
          - Bind-mount only: /usr, /lib*, /bin, /proc, session dirs
          - No network namespace
          - No new privileges
          - Read-only input, read-write output only
          - Unshare all namespaces (user, pid, ipc, uts, cgroup)
        """
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise SandboxNotAvailableError(
                "bubblewrap (bwrap) not found in PATH"
            )

        session_dir = self._session.session_dir
        input_file  = self._session.input_file
        output_dir  = self._session.output_dir
        plugin_dir  = self._config.plugin_dir

        cmd = [bwrap]

        # Unshare all namespaces
        cmd += ["--unshare-all"]

        # New empty root
        cmd += ["--tmpfs", "/"]

        # Essential system paths — read-only
        for sys_path in ["/usr", "/bin", "/lib", "/lib64", "/etc"]:
            if Path(sys_path).exists():
                cmd += ["--ro-bind", sys_path, sys_path]

        # /proc — required by many runtimes
        cmd += ["--proc", "/proc"]

        # /dev — minimal device access
        cmd += ["--dev", "/dev"]

        # /tmp — required by some runtimes
        cmd += ["--tmpfs", "/tmp"]

        # Plugin directory — read-only (entrypoint lives here)
        cmd += ["--ro-bind", str(plugin_dir), str(plugin_dir)]

        # Session input — read-only
        cmd += ["--ro-bind", str(input_file), str(input_file)]

        # Session output directory — read-write (only writable path)
        cmd += ["--bind", str(output_dir), str(output_dir)]

        # No network
        cmd += ["--unshare-net"]

        # No new privileges (seccomp safety)
        cmd += ["--new-session"]

        # Die with parent
        cmd += ["--die-with-parent"]

        cmd += ["--"] + plugin_cmd

        return cmd

    def _wrap_firejail(self, plugin_cmd: list[str]) -> list[str]:
        """
        Wrap the plugin command with firejail.

        Security profile:
          - Private filesystem
          - No network
          - No new privileges
          - Whitelist only session dirs
        """
        firejail = shutil.which("firejail")
        if not firejail:
            raise SandboxNotAvailableError(
                "firejail not found in PATH"
            )

        input_file = self._session.input_file
        output_dir = self._session.output_dir
        plugin_dir = self._config.plugin_dir

        cmd = [
            firejail,
            "--quiet",
            "--private",
            "--noroot",
            "--net=none",
            "--no3d",
            "--nosound",
            f"--whitelist={plugin_dir}",
            f"--whitelist={input_file}",
            f"--whitelist={output_dir}",
            "--read-only=" + str(input_file),
            "--read-only=" + str(plugin_dir),
            "--",
        ] + plugin_cmd

        return cmd

    # ------------------------------------------------------------------
    # Execution engine
    # ------------------------------------------------------------------

    def _execute(self, cmd: list[str]) -> Iterator[PluginEvent]:
        """
        Run the command as a subprocess, parse stdout JSONL line by line,
        and yield PluginEvent objects in real time.

        stderr is collected in a background thread to avoid blocking.
        """
        timeout  = self._config.execution.timeout_seconds
        stderr_lines: list[str] = []
        proc = None

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            # Collect stderr in background — avoids pipe deadlock
            stderr_thread = threading.Thread(
                target=_drain_stderr,
                args=(proc.stderr, stderr_lines),
                daemon=True,
            )
            stderr_thread.start()

            # Read stdout line by line — parse each as JSONL
            for raw_line in _iter_stdout(proc.stdout, timeout, proc):
                line = raw_line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise MalformedEventError(
                        f"Plugin '{self._config.plugin.name}' emitted "
                        f"non-JSONL on stdout: {line!r}\n  {exc}"
                    )

                event_type = data.get("event")
                if not event_type:
                    raise MalformedEventError(
                        f"Plugin event missing 'event' field: {data}"
                    )

                event = PluginEvent(event=event_type, raw=data)
                logger.debug("Plugin event: %s", data)
                yield event

                # Fatal error from plugin — stop reading
                if event_type == "error" and event.is_fatal:
                    proc.wait(timeout=5)
                    break

            # Wait for process to finish
            stderr_thread.join(timeout=5)
            exit_code = proc.wait(timeout=5)

            self._last_exit_code = exit_code
            self._last_stderr    = "".join(stderr_lines)

            if exit_code != 0:
                stderr_dump = self._last_stderr.strip()
                raise PluginExitError(
                    f"Plugin '{self._config.plugin.name}' exited with "
                    f"code {exit_code}.\n"
                    + (f"stderr:\n{stderr_dump}" if stderr_dump else "")
                )

        except subprocess.TimeoutExpired:
            if proc:
                proc.kill()
                proc.wait()
            raise PluginTimeoutError(
                f"Plugin '{self._config.plugin.name}' exceeded "
                f"timeout of {timeout}s"
            )

    # ------------------------------------------------------------------
    # Sandbox availability checks
    # ------------------------------------------------------------------

    def _check_sandbox_availability(self) -> None:
        """
        Verify sandbox constraints before attempting execution.

        - sandbox.required=true  → engine binary must exist, no override possible
        - sandbox.required=false → allowed only if unsafe_override=True,
                                   otherwise raises SandboxError
        """
        if not self._config.sandbox.required:
            if not self._unsafe_override:
                raise SandboxError(
                    f"Plugin '{self._config.plugin.name}' declares "
                    "sandbox.required=false. "
                    "Set unsafe_override=True after explicit user confirmation "
                    "to allow unsandboxed execution."
                )
            logger.warning(
                "Running plugin '%s' WITHOUT sandbox — user confirmed.",
                self._config.plugin.name,
            )
            return

        engine = self._config.sandbox.engine

        if engine == "bubblewrap" and not shutil.which("bwrap"):
            raise SandboxNotAvailableError(
                f"Plugin '{self._config.plugin.name}' requires bubblewrap "
                "but 'bwrap' was not found in PATH.\n"
                "Install with: sudo apt install bubblewrap"
            )

        if engine == "firejail" and not shutil.which("firejail"):
            raise SandboxNotAvailableError(
                f"Plugin '{self._config.plugin.name}' requires firejail "
                "but 'firejail' was not found in PATH.\n"
                "Install with: sudo apt install firejail"
            )


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _drain_stderr(stderr_pipe, lines: list[str]) -> None:
    """Read stderr to completion in a background thread."""
    try:
        for line in stderr_pipe:
            lines.append(line)
    except Exception:
        pass


def _iter_stdout(
    stdout_pipe,
    timeout: int,
    proc: subprocess.Popen,
) -> Iterator[str]:
    """
    Iterate over stdout lines with timeout enforcement.
    Uses a background thread to read lines — the main thread checks
    the process deadline on each iteration.
    """
    import time

    lines: list[str]  = []
    done_flag         = threading.Event()

    def _reader():
        try:
            for line in stdout_pipe:
                lines.append(line)
        finally:
            done_flag.set()

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    deadline = time.monotonic() + timeout
    idx = 0

    while True:
        # Yield any lines already collected
        while idx < len(lines):
            yield lines[idx]
            idx += 1

        # Check if reader finished
        if done_flag.is_set() and idx >= len(lines):
            break

        # Check timeout
        if time.monotonic() > deadline:
            proc.kill()
            raise subprocess.TimeoutExpired(proc.args, timeout)

        time.sleep(0.01)

    reader_thread.join(timeout=2)
