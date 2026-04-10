# =============================================================================
# QuickFix - tests/test_session.py
# =============================================================================
# Tests for core/session.py
#
# Run from project root:
#   python -m pytest tests/test_session.py -v
# =============================================================================

import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.loader   import PluginLoader
from core.session  import (
    FileLockError,
    FileSizeError,
    MimeTypeError,
    PluginSession,
    SessionError,
)
from core.verifier import IntegrityError

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"
DUMMY_PLUGIN = FIXTURES_DIR / "dummy_plugin"


def _config():
    """Load the dummy_plugin PluginConfig."""
    return PluginLoader(plugins_dir=FIXTURES_DIR).load("dummy_plugin")


def _txt(path: Path, content: str = "hello quickfix\n") -> Path:
    """Write a plain text file and return its path."""
    path.write_text(content, encoding="utf-8")
    return path


# =============================================================================
# Happy path — context manager lifecycle
# =============================================================================

class TestSessionLifecycle:

    def test_enters_and_exits_cleanly(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        with PluginSession(f, _config()) as session:
            assert session.input_file.exists()
            assert session.output_dir.exists()
            assert session.session_dir.exists()
            assert session.manifest is not None

    def test_session_dir_removed_after_exit(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        with PluginSession(f, _config()) as session:
            session_dir = session.session_dir
        assert not session_dir.exists()

    def test_input_file_is_copy_not_original(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        with PluginSession(f, _config()) as session:
            assert session.input_file != f
            assert session.input_file.read_text() == f.read_text()

    def test_input_file_is_readonly(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        with PluginSession(f, _config()) as session:
            mode = stat.S_IMODE(session.input_file.stat().st_mode)
            assert mode == 0o444

    def test_output_dir_is_writable(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        with PluginSession(f, _config()) as session:
            result = session.output_dir / "result.txt"
            result.write_text("output content")
            assert result.exists()

    def test_input_filename_matches_original(self, tmp_path):
        f = _txt(tmp_path / "report.txt")
        with PluginSession(f, _config()) as session:
            assert session.input_file.name == "report.txt"

    def test_plugin_name_accessible(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        with PluginSession(f, _config()) as session:
            assert session.plugin_name == "dummy_plugin"

    def test_session_dir_prefix(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        with PluginSession(f, _config()) as session:
            assert session.session_dir.name.startswith("quickfix_dummy_plugin_")

    def test_session_dir_in_tmp(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        with PluginSession(f, _config()) as session:
            assert str(session.session_dir).startswith("/tmp/")

    def test_original_file_untouched_after_exit(self, tmp_path):
        content = "original content\n"
        f = _txt(tmp_path / "file.txt", content)
        with PluginSession(f, _config()) as session:
            session.verify_integrity()
        assert f.read_text() == content

    def test_original_permissions_restored_after_exit(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        original_mode = stat.S_IMODE(f.stat().st_mode)
        with PluginSession(f, _config()):
            pass
        assert stat.S_IMODE(f.stat().st_mode) == original_mode


# =============================================================================
# Integrity verification
# =============================================================================

class TestIntegrityVerification:

    def test_verify_integrity_passes_for_clean_file(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        with PluginSession(f, _config()) as session:
            session.verify_integrity()   # must not raise

    def test_verify_integrity_raises_if_original_modified(self, tmp_path):
        f = _txt(tmp_path / "file.txt", "original")
        with pytest.raises(IntegrityError):
            with PluginSession(f, _config()) as session:
                # Bypass read-only protection to simulate a rogue plugin
                f.chmod(0o644)
                f.write_text("tampered", encoding="utf-8")
                session.verify_integrity()

    def test_verify_integrity_raises_outside_context(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        session = PluginSession(f, _config())
        with pytest.raises(SessionError):
            session.verify_integrity()

    def test_original_permissions_restored_even_after_violation(self, tmp_path):
        f = _txt(tmp_path / "file.txt", "original")
        original_mode = stat.S_IMODE(f.stat().st_mode)

        with pytest.raises(IntegrityError):
            with PluginSession(f, _config()) as session:
                f.chmod(0o644)
                f.write_text("tampered", encoding="utf-8")
                session.verify_integrity()

        assert stat.S_IMODE(f.stat().st_mode) == original_mode


# =============================================================================
# File validation — size and MIME type
# =============================================================================

class TestFileValidation:

    def test_file_exceeding_max_size_raises(self, tmp_path):
        f = tmp_path / "big.txt"
        # dummy_plugin allows 1 MB — write 2 MB
        f.write_bytes(b"x" * (2 * 1024 * 1024))
        with pytest.raises(FileSizeError) as exc_info:
            with PluginSession(f, _config()):
                pass
        assert "2.0 MB" in str(exc_info.value)
        assert "1 MB" in str(exc_info.value)

    def test_file_at_exact_max_size_passes(self, tmp_path):
        f = tmp_path / "exact.txt"
        # Exactly 1 MB — must pass
        f.write_bytes(b"x" * (1 * 1024 * 1024))
        with PluginSession(f, _config()) as session:
            assert session.input_file.exists()

    def test_wrong_mime_type_raises(self, tmp_path):
        # dummy_plugin accepts only text/plain — .png triggers rejection
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        with pytest.raises(MimeTypeError) as exc_info:
            with PluginSession(f, _config()):
                pass
        assert "image/png" in str(exc_info.value)

    def test_unknown_mime_type_raises(self, tmp_path):
        f = tmp_path / "file.unknownextension"
        f.write_bytes(b"some binary data")
        with pytest.raises(MimeTypeError):
            with PluginSession(f, _config()):
                pass

    def test_original_file_not_found_raises(self, tmp_path):
        with pytest.raises(SessionError):
            with PluginSession(tmp_path / "nonexistent.txt", _config()):
                pass


# =============================================================================
# File locking
# =============================================================================

class TestFileLocking:

    def test_lock_file_created_during_session(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        with PluginSession(f, _config()) as session:
            lock = session.manifest.path + ".quickfix.lock"
            assert Path(lock).exists() or Path(f).with_suffix(
                f.suffix + ".quickfix.lock"
            ).exists()

    def test_lock_file_removed_after_exit(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        lock = f.with_suffix(f.suffix + ".quickfix.lock")
        with PluginSession(f, _config()):
            pass
        assert not lock.exists()

    def test_concurrent_sessions_raises_lock_error(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        with PluginSession(f, _config()):
            with pytest.raises(FileLockError) as exc_info:
                with PluginSession(f, _config()):
                    pass
            assert "already being processed" in str(exc_info.value)

    def test_lock_released_after_exception(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        with pytest.raises(RuntimeError):
            with PluginSession(f, _config()):
                raise RuntimeError("simulated crash")

        # Lock must be released — second session must succeed
        with PluginSession(f, _config()) as session:
            assert session.input_file.exists()


# =============================================================================
# Cleanup guarantees
# =============================================================================

class TestCleanupGuarantees:

    def test_session_dir_cleaned_up_after_exception(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        session_dir = None
        with pytest.raises(RuntimeError):
            with PluginSession(f, _config()) as session:
                session_dir = session.session_dir
                raise RuntimeError("simulated crash")
        assert session_dir is not None
        assert not session_dir.exists()

    def test_properties_raise_outside_context(self, tmp_path):
        f = _txt(tmp_path / "file.txt")
        session = PluginSession(f, _config())
        with pytest.raises(SessionError):
            _ = session.input_file
        with pytest.raises(SessionError):
            _ = session.output_dir
        with pytest.raises(SessionError):
            _ = session.session_dir
        with pytest.raises(SessionError):
            _ = session.manifest
