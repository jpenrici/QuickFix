# =============================================================================
# QuickFix - tests/test_sandbox.py
# =============================================================================
# Tests for core/sandbox.py
#
# Strategy:
#   - Command building tested via build_command_preview() — no subprocess
#   - JSONL parsing and event streaming tested with real subprocess (bash)
#   - Sandbox wrapping (bwrap/firejail) tested only if binary is present;
#     skipped gracefully otherwise — no hard dependency on sandbox tools
#   - Timeout and exit code handling tested with controlled bash scripts
#
# Run from project root:
#   python -m pytest tests/test_sandbox.py -v
# =============================================================================

import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.loader  import PluginLoader
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
from core.session import PluginSession

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"
DUMMY_PLUGIN = FIXTURES_DIR / "dummy_plugin"


def _config(sandbox_required: bool = False):
    """Load dummy_plugin config. sandbox.required is always false — safe
    to run in CI without bubblewrap."""
    return PluginLoader(plugins_dir=FIXTURES_DIR).load("dummy_plugin")


def _txt(path: Path, content: str = "hello quickfix\n") -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _make_plugin(
    tmp_path: Path,
    script: str,
    *,
    timeout: int = 10,
    sandbox_required: bool = False,
    runtime: str = "bash",
) -> tuple:
    """
    Create a temporary plugin with the given bash script and config.
    Returns (plugin_dir, config).
    """
    import json as _json
    import copy

    base_cfg = _json.loads((DUMMY_PLUGIN / "config.json").read_text())

    base_cfg["execution"]["runtime"]         = runtime
    base_cfg["execution"]["entrypoint"]      = "main.sh"
    base_cfg["execution"]["timeout_seconds"] = timeout
    base_cfg["sandbox"]["required"]          = sandbox_required
    base_cfg["plugin"]["name"]               = "test_plugin"
    base_cfg["plugin"]["description"]        = "Temporary test plugin for sandbox tests."

    plugin_dir = tmp_path / "plugins" / "test_plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "config.json").write_text(_json.dumps(base_cfg))

    entry = plugin_dir / "main.sh"
    entry.write_text(script)
    entry.chmod(0o755)

    loader = PluginLoader(plugins_dir=tmp_path / "plugins")
    config = loader.load("test_plugin")
    return plugin_dir, config


# =============================================================================
# PluginEvent
# =============================================================================

class TestPluginEvent:

    def test_start_event(self):
        e = PluginEvent(event="start", raw={"event": "start", "timestamp": "t"})
        assert e.event == "start"
        assert e.percent is None
        assert e.message is None

    def test_progress_event(self):
        e = PluginEvent(
            event="progress",
            raw={"event": "progress", "percent": 50, "message": "halfway"},
        )
        assert e.percent  == 50
        assert e.message  == "halfway"

    def test_done_event(self):
        e = PluginEvent(
            event="done",
            raw={"event": "done", "output_file": "out.txt", "checksum_sha256": "abc"},
        )
        assert e.output_file    == "out.txt"
        assert e.checksum_sha256 == "abc"

    def test_error_event_fatal(self):
        e = PluginEvent(
            event="error",
            raw={"event": "error", "code": "FAIL", "message": "boom", "fatal": True},
        )
        assert e.is_fatal    is True
        assert e.error_code  == "FAIL"

    def test_error_event_not_fatal(self):
        e = PluginEvent(
            event="error",
            raw={"event": "error", "code": "WARN", "fatal": False},
        )
        assert e.is_fatal is False


# =============================================================================
# Command building — no subprocess
# =============================================================================

class TestCommandBuilding:

    def test_preview_returns_list_of_strings(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        script = '#!/usr/bin/env bash\necho \'{"event":"start","timestamp":"t"}\'\necho \'{"event":"done","output_file":"x","checksum_sha256":"y"}\'\n'
        _, config = _make_plugin(tmp_path, script)

        with PluginSession(f, config) as session:
            runner  = SandboxRunner(config, session, unsafe_override=True)
            preview = runner.build_command_preview()

        assert isinstance(preview, list)
        assert all(isinstance(c, str) for c in preview)

    def test_preview_contains_entrypoint(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        script = '#!/usr/bin/env bash\necho \'{"event":"start","timestamp":"t"}\'\necho \'{"event":"done","output_file":"x","checksum_sha256":"y"}\'\n'
        _, config = _make_plugin(tmp_path, script)

        with PluginSession(f, config) as session:
            runner  = SandboxRunner(config, session, unsafe_override=True)
            preview = runner.build_command_preview()

        assert any("main.sh" in c for c in preview)

    def test_preview_contains_input_and_output_paths(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        script = '#!/usr/bin/env bash\necho \'{"event":"start","timestamp":"t"}\'\necho \'{"event":"done","output_file":"x","checksum_sha256":"y"}\'\n'
        _, config = _make_plugin(tmp_path, script)

        with PluginSession(f, config) as session:
            runner  = SandboxRunner(config, session, unsafe_override=True)
            preview = runner.build_command_preview()
            # input and output paths must appear as arguments
            assert str(session.input_file) in preview
            assert str(session.output_dir) in preview

    @pytest.mark.skipif(
        not shutil.which("bwrap"),
        reason="bubblewrap not installed"
    )
    def test_bwrap_preview_starts_with_bwrap(self, tmp_path):
        import json as _json
        f = _txt(tmp_path / "file.txt")
        script = '#!/usr/bin/env bash\necho \'{"event":"start","timestamp":"t"}\'\necho \'{"event":"done","output_file":"x","checksum_sha256":"y"}\'\n'
        _, config = _make_plugin(tmp_path, script, sandbox_required=True)

        with PluginSession(f, config) as session:
            runner  = SandboxRunner(config, session)
            preview = runner.build_command_preview()

        assert Path(preview[0]).name == "bwrap"

    @pytest.mark.skipif(
        not shutil.which("bwrap"),
        reason="bubblewrap not installed"
    )
    def test_bwrap_preview_contains_unshare_all(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        script = '#!/usr/bin/env bash\necho \'{"event":"start","timestamp":"t"}\'\necho \'{"event":"done","output_file":"x","checksum_sha256":"y"}\'\n'
        _, config = _make_plugin(tmp_path, script, sandbox_required=True)

        with PluginSession(f, config) as session:
            runner  = SandboxRunner(config, session)
            preview = runner.build_command_preview()

        assert "--unshare-all" in preview

    @pytest.mark.skipif(
        not shutil.which("bwrap"),
        reason="bubblewrap not installed"
    )
    def test_bwrap_output_dir_is_writable_bind(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        script = '#!/usr/bin/env bash\necho \'{"event":"start","timestamp":"t"}\'\necho \'{"event":"done","output_file":"x","checksum_sha256":"y"}\'\n'
        _, config = _make_plugin(tmp_path, script, sandbox_required=True)

        with PluginSession(f, config) as session:
            runner  = SandboxRunner(config, session)
            preview = runner.build_command_preview()
            output_dir = str(session.output_dir)

        # --bind (not --ro-bind) must be used for output_dir
        pairs = list(zip(preview, preview[1:]))
        assert ("--bind", output_dir) in pairs


# =============================================================================
# Sandbox availability checks
# =============================================================================

class TestSandboxAvailability:

    def test_unsandboxed_without_override_raises(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        script = '#!/usr/bin/env bash\necho \'{"event":"start","timestamp":"t"}\'\necho \'{"event":"done","output_file":"x","checksum_sha256":"y"}\'\n'
        _, config = _make_plugin(tmp_path, script, sandbox_required=False)

        with PluginSession(f, config) as session:
            runner = SandboxRunner(config, session, unsafe_override=False)
            with pytest.raises(SandboxError) as exc_info:
                list(runner.run())
        assert "unsafe_override" in str(exc_info.value)

    def test_unsandboxed_with_override_allowed(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        script = (
            '#!/usr/bin/env bash\n'
            'echo \'{"event": "start", "timestamp": "t"}\'\n'
            'echo \'{"event": "done", "output_file": "x", "checksum_sha256": "y"}\'\n'
        )
        _, config = _make_plugin(tmp_path, script, sandbox_required=False)

        with PluginSession(f, config) as session:
            runner = SandboxRunner(config, session, unsafe_override=True)
            events = list(runner.run())

        assert any(e.event == "done" for e in events)


# =============================================================================
# JSONL streaming — real subprocess execution
# =============================================================================

class TestJsonlStreaming:

    def test_start_progress_done_events_received(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        script = (
            '#!/usr/bin/env bash\n'
            'echo \'{"event": "start", "timestamp": "t"}\'\n'
            'echo \'{"event": "progress", "percent": 50, "message": "halfway"}\'\n'
            'echo \'{"event": "done", "output_file": "out.txt", "checksum_sha256": "abc"}\'\n'
        )
        _, config = _make_plugin(tmp_path, script)

        with PluginSession(f, config) as session:
            runner = SandboxRunner(config, session, unsafe_override=True)
            events = list(runner.run())

        types = [e.event for e in events]
        assert "start"    in types
        assert "progress" in types
        assert "done"     in types

    def test_done_event_carries_output_file(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        script = (
            '#!/usr/bin/env bash\n'
            'echo \'{"event": "start", "timestamp": "t"}\'\n'
            'echo \'{"event": "done", "output_file": "result.txt", "checksum_sha256": "xyz"}\'\n'
        )
        _, config = _make_plugin(tmp_path, script)

        with PluginSession(f, config) as session:
            runner = SandboxRunner(config, session, unsafe_override=True)
            events = list(runner.run())

        done = next(e for e in events if e.event == "done")
        assert done.output_file     == "result.txt"
        assert done.checksum_sha256 == "xyz"

    def test_empty_lines_ignored(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        script = (
            '#!/usr/bin/env bash\n'
            'echo ""\n'
            'echo \'{"event": "start", "timestamp": "t"}\'\n'
            'echo ""\n'
            'echo \'{"event": "done", "output_file": "x", "checksum_sha256": "y"}\'\n'
        )
        _, config = _make_plugin(tmp_path, script)

        with PluginSession(f, config) as session:
            runner = SandboxRunner(config, session, unsafe_override=True)
            events = list(runner.run())

        assert len(events) == 2

    def test_malformed_jsonl_raises(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        script = (
            '#!/usr/bin/env bash\n'
            'echo "this is not json"\n'
        )
        _, config = _make_plugin(tmp_path, script)

        with PluginSession(f, config) as session:
            runner = SandboxRunner(config, session, unsafe_override=True)
            with pytest.raises(MalformedEventError) as exc_info:
                list(runner.run())
        assert "non-JSONL" in str(exc_info.value)

    def test_missing_event_field_raises(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        script = (
            '#!/usr/bin/env bash\n'
            'echo \'{"percent": 50, "message": "no event field"}\'\n'
        )
        _, config = _make_plugin(tmp_path, script)

        with PluginSession(f, config) as session:
            runner = SandboxRunner(config, session, unsafe_override=True)
            with pytest.raises(MalformedEventError) as exc_info:
                list(runner.run())
        assert "'event' field" in str(exc_info.value)

    def test_fatal_error_event_stops_iteration(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        script = (
            '#!/usr/bin/env bash\n'
            'echo \'{"event": "start", "timestamp": "t"}\'\n'
            'echo \'{"event": "error", "code": "FAIL", "message": "boom", "fatal": true}\'\n'
            'echo \'{"event": "done", "output_file": "x", "checksum_sha256": "y"}\'\n'
        )
        _, config = _make_plugin(tmp_path, script)

        with PluginSession(f, config) as session:
            runner = SandboxRunner(config, session, unsafe_override=True)
            events = list(runner.run())

        # done must NOT be received — iteration stopped at fatal error
        assert not any(e.event == "done" for e in events)
        assert any(e.event == "error" for e in events)


# =============================================================================
# Exit code and timeout handling
# =============================================================================

class TestExitCodeAndTimeout:

    def test_nonzero_exit_raises_plugin_exit_error(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        script = (
            '#!/usr/bin/env bash\n'
            'echo \'{"event": "start", "timestamp": "t"}\'\n'
            'exit 1\n'
        )
        _, config = _make_plugin(tmp_path, script)

        with PluginSession(f, config) as session:
            runner = SandboxRunner(config, session, unsafe_override=True)
            with pytest.raises(PluginExitError) as exc_info:
                list(runner.run())
        assert "exit code 1" in str(exc_info.value) or "exited with" in str(exc_info.value)

    def test_timeout_raises_plugin_timeout_error(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        script = (
            '#!/usr/bin/env bash\n'
            'echo \'{"event": "start", "timestamp": "t"}\'\n'
            'sleep 60\n'
        )
        _, config = _make_plugin(tmp_path, script, timeout=1)

        with PluginSession(f, config) as session:
            runner = SandboxRunner(config, session, unsafe_override=True)
            with pytest.raises(PluginTimeoutError) as exc_info:
                list(runner.run())
        assert "timeout" in str(exc_info.value).lower()


# =============================================================================
# run_to_completion()
# =============================================================================

class TestRunToCompletion:

    def test_returns_execution_result(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        script = (
            '#!/usr/bin/env bash\n'
            'echo \'{"event": "start", "timestamp": "t"}\'\n'
            'echo \'{"event": "done", "output_file": "out.txt", "checksum_sha256": "abc"}\'\n'
        )
        _, config = _make_plugin(tmp_path, script)

        with PluginSession(f, config) as session:
            runner = SandboxRunner(config, session, unsafe_override=True)
            result = runner.run_to_completion()

        assert isinstance(result, ExecutionResult)
        assert result.exit_code    == 0
        assert result.output_file  == "out.txt"
        assert result.checksum     == "abc"
        assert result.timed_out    is False

    def test_timed_out_flag_set(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        script = (
            '#!/usr/bin/env bash\n'
            'echo \'{"event": "start", "timestamp": "t"}\'\n'
            'sleep 60\n'
        )
        _, config = _make_plugin(tmp_path, script, timeout=1)

        with PluginSession(f, config) as session:
            runner = SandboxRunner(config, session, unsafe_override=True)
            result = runner.run_to_completion()

        assert result.timed_out is True

    def test_dummy_plugin_real_execution(self, tmp_path):
        """End-to-end: run the actual dummy_plugin main.sh."""
        f = _txt(tmp_path / "file.txt", "real content\n")
        config = _config()

        with PluginSession(f, config) as session:
            runner = SandboxRunner(config, session, unsafe_override=True)
            result = runner.run_to_completion()

        assert result.exit_code   == 0
        assert result.timed_out   is False
        assert result.output_file == "dummy_output.txt"
        assert result.checksum    is not None
        assert len(result.checksum) == 64  # valid SHA-256 hex
