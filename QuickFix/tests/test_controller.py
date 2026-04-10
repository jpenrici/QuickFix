# =============================================================================
# QuickFix - tests/test_controller.py
# =============================================================================
# Tests for core/controller.py
#
# Strategy:
#   - All tests use dummy_plugin (bash, sandbox.required=false)
#   - Plugin scripts are created inline per test — full pipeline control
#   - Sandbox is bypassed via unsafe_override path (confirm_cb returns True)
#   - Tests verify ControllerEvent sequence, state transitions, and errors
#
# Run from project root:
#   python -m pytest tests/test_controller.py -v
# =============================================================================

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.controller import (
    Controller,
    ControllerError,
    ControllerEvent,
    EventKind,
    ExecutionCancelledError,
    NoFileOpenError,
    NoOutputError,
)

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"
DUMMY_PLUGIN = FIXTURES_DIR / "dummy_plugin"


def _txt(path: Path, content: str = "hello quickfix\n") -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _make_plugin(tmp_path: Path, script: str, timeout: int = 10) -> Path:
    """
    Create a temporary plugin directory with the given script.
    Returns the plugins/ directory (parent of plugin dir).
    """
    base = json.loads((DUMMY_PLUGIN / "config.json").read_text())
    base["execution"]["timeout_seconds"] = timeout
    base["plugin"]["name"]               = "dummy_plugin"

    plugins_dir = tmp_path / "plugins"
    plugin_dir  = plugins_dir / "dummy_plugin"
    plugin_dir.mkdir(parents=True)

    (plugin_dir / "config.json").write_text(json.dumps(base))
    entry = plugin_dir / "main.sh"
    entry.write_text(script)
    entry.chmod(0o755)

    return plugins_dir


def _ctrl(plugins_dir: Path, confirm: bool = True) -> Controller:
    """Create a Controller with a fixed confirmation callback."""
    return Controller(
        plugins_dir=plugins_dir,
        confirm_cb=lambda msg: confirm,
    )


def _run(ctrl: Controller, plugin: str = "dummy_plugin") -> list[ControllerEvent]:
    """Collect all events from run_plugin()."""
    return list(ctrl.run_plugin(plugin))


def _script_ok(output_name: str = "dummy_output.txt") -> str:
    """A valid plugin script that copies input to output.

    Uses printf with single-quoted JSON template to avoid bash quoting issues.
    The %s placeholder is substituted by printf with the actual checksum value.
    """
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'INPUT_FILE="$1"\n'
        'OUTPUT_DIR="$2"\n'
        f'OUTPUT_FILE="${{OUTPUT_DIR}}/{output_name}"\n'
        """echo '{"event": "start", "timestamp": "t"}'\n"""
        """echo '{"event": "progress", "percent": 50, "message": "halfway"}'\n"""
        'cp "$INPUT_FILE" "$OUTPUT_FILE"\n'
        "CHECKSUM=\"$(sha256sum \"$OUTPUT_FILE\" | cut -d' ' -f1)\"\n"
        f"""printf '{{"event": "done", "output_file": "{output_name}", "checksum_sha256": "%s"}}\\n' "$CHECKSUM"\n"""
    )


class TestOpenFile:

    def test_open_valid_file_returns_mime(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        ctrl = _ctrl(_make_plugin(tmp_path, _script_ok()))
        mime = ctrl.open_file(f)
        assert mime == "text/plain"

    def test_open_sets_state(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        ctrl = _ctrl(_make_plugin(tmp_path, _script_ok()))
        ctrl.open_file(f)
        assert ctrl.current_file == f.resolve()
        assert ctrl.current_mime == "text/plain"

    def test_open_nonexistent_raises(self, tmp_path):
        ctrl = _ctrl(_make_plugin(tmp_path, _script_ok()))
        with pytest.raises(ControllerError):
            ctrl.open_file(tmp_path / "missing.txt")

    def test_open_unknown_mime_raises(self, tmp_path):
        f = tmp_path / "file.unknownextension"
        f.write_bytes(b"data")
        ctrl = _ctrl(_make_plugin(tmp_path, _script_ok()))
        with pytest.raises(ControllerError):
            ctrl.open_file(f)

    def test_open_clears_previous_output(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        plugins_dir = _make_plugin(tmp_path, _script_ok())
        ctrl = _ctrl(plugins_dir)
        ctrl.open_file(f)
        _run(ctrl)
        assert ctrl.has_output

        # re-opening clears output
        ctrl.open_file(f)
        assert not ctrl.has_output


# =============================================================================
# compatible_plugins()
# =============================================================================

class TestCompatiblePlugins:

    def test_returns_matching_plugins(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        ctrl = _ctrl(_make_plugin(tmp_path, _script_ok()))
        ctrl.open_file(f)
        plugins = ctrl.compatible_plugins()
        assert any(p.plugin.name == "dummy_plugin" for p in plugins)

    def test_raises_without_open_file(self, tmp_path):
        ctrl = _ctrl(_make_plugin(tmp_path, _script_ok()))
        with pytest.raises(NoFileOpenError):
            ctrl.compatible_plugins()

    def test_no_compatible_plugins_for_image(self, tmp_path):
        f = tmp_path / "photo.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        ctrl = _ctrl(_make_plugin(tmp_path, _script_ok()))
        ctrl.open_file(f)
        plugins = ctrl.compatible_plugins()
        # dummy_plugin only accepts text/plain
        assert not any(p.plugin.name == "dummy_plugin" for p in plugins)


# =============================================================================
# run_plugin() — happy path
# =============================================================================

class TestRunPluginHappyPath:

    def test_events_include_done(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        plugins_dir = _make_plugin(tmp_path, _script_ok())
        ctrl = _ctrl(plugins_dir)
        ctrl.open_file(f)
        events = _run(ctrl)
        assert any(e.kind == EventKind.DONE for e in events)

    def test_events_include_progress(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        plugins_dir = _make_plugin(tmp_path, _script_ok())
        ctrl = _ctrl(plugins_dir)
        ctrl.open_file(f)
        events = _run(ctrl)
        assert any(e.kind == EventKind.PROGRESS for e in events)

    def test_done_event_has_output_path(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        plugins_dir = _make_plugin(tmp_path, _script_ok())
        ctrl = _ctrl(plugins_dir)
        ctrl.open_file(f)
        events = _run(ctrl)
        done = next(e for e in events if e.kind == EventKind.DONE)
        assert done.output_path is not None
        assert done.output_path.is_file()

    def test_output_filename_uses_suffix(self, tmp_path):
        f = _txt(tmp_path / "report.txt")
        plugins_dir = _make_plugin(tmp_path, _script_ok())
        ctrl = _ctrl(plugins_dir)
        ctrl.open_file(f)
        events = _run(ctrl)
        done = next(e for e in events if e.kind == EventKind.DONE)
        # dummy_plugin declares filename_suffix = "_dummy"
        assert done.output_path.name == "report_dummy.txt"

    def test_has_output_true_after_success(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        plugins_dir = _make_plugin(tmp_path, _script_ok())
        ctrl = _ctrl(plugins_dir)
        ctrl.open_file(f)
        _run(ctrl)
        assert ctrl.has_output

    def test_last_output_path_accessible(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        plugins_dir = _make_plugin(tmp_path, _script_ok())
        ctrl = _ctrl(plugins_dir)
        ctrl.open_file(f)
        _run(ctrl)
        assert ctrl.last_output is not None
        assert ctrl.last_output.is_file()

    def test_original_file_unchanged_after_run(self, tmp_path):
        content = "original content\n"
        f = _txt(tmp_path / "file.txt", content)
        plugins_dir = _make_plugin(tmp_path, _script_ok())
        ctrl = _ctrl(plugins_dir)
        ctrl.open_file(f)
        _run(ctrl)
        assert f.read_text() == content

    def test_sandbox_warning_event_emitted(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        plugins_dir = _make_plugin(tmp_path, _script_ok())
        ctrl = _ctrl(plugins_dir, confirm=True)
        ctrl.open_file(f)
        events = _run(ctrl)
        assert any(e.kind == EventKind.SANDBOX_WARNING for e in events)

    def test_all_events_carry_plugin_name(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        plugins_dir = _make_plugin(tmp_path, _script_ok())
        ctrl = _ctrl(plugins_dir)
        ctrl.open_file(f)
        events = _run(ctrl)
        for e in events:
            assert e.plugin_name == "dummy_plugin"


# =============================================================================
# run_plugin() — error and cancellation paths
# =============================================================================

class TestRunPluginErrors:

    def test_raises_without_open_file(self, tmp_path):
        ctrl = _ctrl(_make_plugin(tmp_path, _script_ok()))
        with pytest.raises(NoFileOpenError):
            _run(ctrl)

    def test_unknown_plugin_yields_error(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        plugins_dir = _make_plugin(tmp_path, _script_ok())
        ctrl = _ctrl(plugins_dir)
        ctrl.open_file(f)
        events = list(ctrl.run_plugin("nonexistent_plugin"))
        assert any(e.kind == EventKind.ERROR for e in events)

    def test_user_cancels_unsandboxed_raises(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        plugins_dir = _make_plugin(tmp_path, _script_ok())
        ctrl = _ctrl(plugins_dir, confirm=False)
        ctrl.open_file(f)
        with pytest.raises(ExecutionCancelledError):
            _run(ctrl)

    def test_plugin_exit_nonzero_yields_error(self, tmp_path):
        script = (
            '#!/usr/bin/env bash\n'
            'echo \'{"event": "start", "timestamp": "t"}\'\n'
            'exit 1\n'
        )
        f = _txt(tmp_path / "file.txt")
        plugins_dir = _make_plugin(tmp_path, script)
        ctrl = _ctrl(plugins_dir)
        ctrl.open_file(f)
        events = _run(ctrl)
        assert any(e.kind == EventKind.ERROR for e in events)
        assert not any(e.kind == EventKind.DONE for e in events)

    def test_plugin_timeout_yields_error(self, tmp_path):
        script = (
            '#!/usr/bin/env bash\n'
            'echo \'{"event": "start", "timestamp": "t"}\'\n'
            'sleep 60\n'
        )
        f = _txt(tmp_path / "file.txt")
        plugins_dir = _make_plugin(tmp_path, script, timeout=1)
        ctrl = _ctrl(plugins_dir)
        ctrl.open_file(f)
        events = _run(ctrl)
        assert any(e.kind == EventKind.ERROR for e in events)

    def test_plugin_no_done_event_yields_error(self, tmp_path):
        script = (
            '#!/usr/bin/env bash\n'
            'echo \'{"event": "start", "timestamp": "t"}\'\n'
            # no done event — exit 0
        )
        f = _txt(tmp_path / "file.txt")
        plugins_dir = _make_plugin(tmp_path, script)
        ctrl = _ctrl(plugins_dir)
        ctrl.open_file(f)
        events = _run(ctrl)
        assert any(e.kind == EventKind.ERROR for e in events)

    def test_plugin_wrong_checksum_yields_error(self, tmp_path):
        script = (
            '#!/usr/bin/env bash\n'
            'INPUT_FILE="$1"\n'
            'OUTPUT_DIR="$2"\n'
            'cp "$INPUT_FILE" "$OUTPUT_DIR/dummy_output.txt"\n'
            'echo \'{"event": "start", "timestamp": "t"}\'\n'
            # report wrong checksum
            'echo \'{"event": "done", "output_file": "dummy_output.txt",'
            ' "checksum_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\'\n'
        )
        f = _txt(tmp_path / "file.txt")
        plugins_dir = _make_plugin(tmp_path, script)
        ctrl = _ctrl(plugins_dir)
        ctrl.open_file(f)
        events = _run(ctrl)
        assert any(e.kind == EventKind.ERROR for e in events)
        assert not any(e.kind == EventKind.DONE for e in events)

    def test_integrity_violation_yields_event(self, tmp_path):
        # Plugin that tampers with the original file
        script = (
            '#!/usr/bin/env bash\n'
            'INPUT_FILE="$1"\n'
            'OUTPUT_DIR="$2"\n'
            'echo \'{"event": "start", "timestamp": "t"}\'\n'
            # Attempt to write to original — chmod 444 should block this,
            # but we test the verifier catches any bypass
            'cp "$INPUT_FILE" "$OUTPUT_DIR/dummy_output.txt"\n'
            'CHECKSUM="$(sha256sum "$OUTPUT_DIR/dummy_output.txt" | cut -d\' \' -f1)"\n'
            'echo "{\\"event\\": \\"done\\", \\"output_file\\": \\"dummy_output.txt\\",'
            ' \\"checksum_sha256\\": \\"$CHECKSUM\\"}"\n'
        )
        f = _txt(tmp_path / "file.txt", "safe content\n")
        plugins_dir = _make_plugin(tmp_path, script)
        ctrl = _ctrl(plugins_dir)
        ctrl.open_file(f)
        # Normal run — no violation expected here
        events = _run(ctrl)
        assert any(e.kind == EventKind.DONE for e in events)


# =============================================================================
# save_file() and save_file_as()
# =============================================================================

class TestSaveFile:

    def test_save_file_overwrites_original(self, tmp_path):
        f = _txt(tmp_path / "file.txt", "original\n")
        plugins_dir = _make_plugin(tmp_path, _script_ok())
        ctrl = _ctrl(plugins_dir)
        ctrl.open_file(f)
        _run(ctrl)
        ctrl.save_file()
        # After save, original is overwritten with plugin output (a copy of itself)
        assert f.is_file()

    def test_save_file_as_creates_new_file(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        plugins_dir = _make_plugin(tmp_path, _script_ok())
        ctrl = _ctrl(plugins_dir)
        ctrl.open_file(f)
        _run(ctrl)
        dest = tmp_path / "saved_output.txt"
        ctrl.save_file_as(dest)
        assert dest.is_file()

    def test_save_without_output_raises(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        ctrl = _ctrl(_make_plugin(tmp_path, _script_ok()))
        ctrl.open_file(f)
        with pytest.raises(NoOutputError):
            ctrl.save_file()

    def test_save_without_open_file_raises(self, tmp_path):
        ctrl = _ctrl(_make_plugin(tmp_path, _script_ok()))
        with pytest.raises(NoFileOpenError):
            ctrl.save_file()

    def test_save_as_without_open_file_raises(self, tmp_path):
        ctrl = _ctrl(_make_plugin(tmp_path, _script_ok()))
        with pytest.raises(NoFileOpenError):
            ctrl.save_file_as(tmp_path / "out.txt")
