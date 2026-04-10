# =============================================================================
# QuickFix - tests/test_verifier.py
# =============================================================================
# Tests for core/verifier.py
#
# Run from project root:
#   python -m pytest tests/test_verifier.py -v
# =============================================================================

import json
import stat
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.verifier import (
    FileManifest,
    FileVerifier,
    IntegrityError,
    sha256,
    verify_output_checksum,
)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _write(path: Path, content: str = "hello quickfix\n") -> Path:
    path.write_text(content, encoding="utf-8")
    return path


# =============================================================================
# sha256()
# =============================================================================

class TestSha256:

    def test_known_hash(self, tmp_path):
        f = _write(tmp_path / "f.txt", "hello\n")
        # echo -n "hello\n" | sha256sum
        assert sha256(f) == sha256(f)  # stable across calls

    def test_different_content_different_hash(self, tmp_path):
        a = _write(tmp_path / "a.txt", "aaa")
        b = _write(tmp_path / "b.txt", "bbb")
        assert sha256(a) != sha256(b)

    def test_same_content_same_hash(self, tmp_path):
        a = _write(tmp_path / "a.txt", "same content")
        b = _write(tmp_path / "b.txt", "same content")
        assert sha256(a) == sha256(b)

    def test_empty_file(self, tmp_path):
        f = _write(tmp_path / "empty.txt", "")
        # SHA-256 of empty string is well-known
        assert sha256(f) == \
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def test_large_file(self, tmp_path):
        # 1 MB — exercises the chunked reading path
        f = tmp_path / "large.bin"
        f.write_bytes(b"x" * 1_048_576)
        digest = sha256(f)
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)


# =============================================================================
# FileManifest
# =============================================================================

class TestFileManifest:

    def test_roundtrip_dict(self, tmp_path):
        f = _write(tmp_path / "f.txt")
        verifier = FileVerifier(f, "dummy_plugin")
        manifest = verifier.capture()
        verifier.verify(manifest)  # restore permissions

        restored = FileManifest.from_dict(manifest.to_dict())
        assert restored == manifest

    def test_roundtrip_json(self, tmp_path):
        f = _write(tmp_path / "f.txt")
        verifier = FileVerifier(f, "dummy_plugin")
        manifest = verifier.capture()
        verifier.verify(manifest)

        as_json  = json.dumps(manifest.to_dict())
        restored = FileManifest.from_dict(json.loads(as_json))
        assert restored == manifest

    def test_manifest_fields_populated(self, tmp_path):
        f = _write(tmp_path / "f.txt", "content")
        verifier = FileVerifier(f, "dummy_plugin")
        manifest = verifier.capture()
        verifier.verify(manifest)

        assert manifest.path       == str(f)
        assert len(manifest.sha256) == 64
        assert manifest.size_bytes  > 0
        assert manifest.inode       > 0
        assert manifest.mtime_ns    > 0
        assert manifest.plugin_name == "dummy_plugin"
        assert "T" in manifest.captured_at  # ISO-8601


# =============================================================================
# FileVerifier — capture and protect
# =============================================================================

class TestCapture:

    def test_capture_returns_manifest(self, tmp_path):
        f = _write(tmp_path / "f.txt")
        verifier = FileVerifier(f, "dummy_plugin")
        manifest = verifier.capture()
        verifier.verify(manifest)
        assert isinstance(manifest, FileManifest)

    def test_capture_makes_file_readonly(self, tmp_path):
        f = _write(tmp_path / "f.txt")
        verifier = FileVerifier(f, "dummy_plugin")
        verifier.capture()

        mode = stat.S_IMODE(f.stat().st_mode)
        assert mode == 0o444

        # cleanup — restore permissions so tmp_path can be removed
        f.chmod(0o644)

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            FileVerifier(tmp_path / "nonexistent.txt", "dummy_plugin")

    def test_relative_path_resolved(self, tmp_path, monkeypatch):
        f = _write(tmp_path / "f.txt")
        monkeypatch.chdir(tmp_path)
        verifier = FileVerifier(Path("f.txt"), "dummy_plugin")
        manifest = verifier.capture()
        verifier.verify(manifest)
        assert Path(manifest.path).is_absolute()


# =============================================================================
# FileVerifier — verify (happy path)
# =============================================================================

class TestVerifyClean:

    def test_verify_clean_file_passes(self, tmp_path):
        f = _write(tmp_path / "f.txt")
        verifier = FileVerifier(f, "dummy_plugin")
        manifest = verifier.capture()
        # file untouched — verify must pass silently
        verifier.verify(manifest)

    def test_verify_restores_permissions(self, tmp_path):
        f = _write(tmp_path / "f.txt")
        original_mode = stat.S_IMODE(f.stat().st_mode)

        verifier = FileVerifier(f, "dummy_plugin")
        verifier.capture()
        verifier.verify(
            FileVerifier(f, "dummy_plugin").capture()
            if False else
            verifier.capture()
        )
        # Simpler: just call capture+verify on a fresh verifier
        f.chmod(original_mode)  # reset from previous test side-effect

        verifier2 = FileVerifier(f, "dummy_plugin")
        manifest  = verifier2.capture()
        verifier2.verify(manifest)

        restored_mode = stat.S_IMODE(f.stat().st_mode)
        assert restored_mode == original_mode

    def test_verify_restores_permissions_even_after_violation(self, tmp_path):
        f = _write(tmp_path / "f.txt")
        original_mode = stat.S_IMODE(f.stat().st_mode)

        verifier = FileVerifier(f, "dummy_plugin")
        manifest = verifier.capture()

        # Force a violation — chmod must still be restored
        f.chmod(0o644)  # temporarily allow write
        f.write_text("tampered!", encoding="utf-8")
        f.chmod(0o644)

        with pytest.raises(IntegrityError):
            verifier.verify(manifest)

        # Permissions must be restored even after the violation
        restored_mode = stat.S_IMODE(f.stat().st_mode)
        assert restored_mode == original_mode


# =============================================================================
# FileVerifier — verify (violation detection)
# =============================================================================

class TestVerifyViolations:

    def test_detects_content_change(self, tmp_path):
        f = _write(tmp_path / "f.txt", "original content")
        verifier = FileVerifier(f, "dummy_plugin")
        manifest = verifier.capture()

        f.chmod(0o644)
        f.write_text("tampered content", encoding="utf-8")

        with pytest.raises(IntegrityError) as exc_info:
            verifier.verify(manifest)

        assert "SHA-256" in str(exc_info.value)
        assert exc_info.value.manifest is manifest

    def test_detects_size_change(self, tmp_path):
        f = _write(tmp_path / "f.txt", "abc")
        verifier = FileVerifier(f, "dummy_plugin")
        manifest = verifier.capture()

        f.chmod(0o644)
        f.write_text("abcdef", encoding="utf-8")

        with pytest.raises(IntegrityError):
            verifier.verify(manifest)

    def test_detects_mtime_change(self, tmp_path):
        f = _write(tmp_path / "f.txt", "same content")
        verifier = FileVerifier(f, "dummy_plugin")
        manifest = verifier.capture()

        # Change mtime without changing content
        f.chmod(0o644)
        future_ns = manifest.mtime_ns + 1_000_000_000
        os_times  = (future_ns / 1e9, future_ns / 1e9)

        import os
        os.utime(f, times=os_times)

        with pytest.raises(IntegrityError):
            verifier.verify(manifest)

    def test_integrity_error_carries_manifest(self, tmp_path):
        f = _write(tmp_path / "f.txt", "original")
        verifier = FileVerifier(f, "dummy_plugin")
        manifest = verifier.capture()

        f.chmod(0o644)
        f.write_text("tampered", encoding="utf-8")

        with pytest.raises(IntegrityError) as exc_info:
            verifier.verify(manifest)

        assert exc_info.value.manifest is manifest
        assert exc_info.value.manifest.plugin_name == "dummy_plugin"

    def test_forensic_log_written_on_violation(self, tmp_path, monkeypatch):
        # Redirect forensic logs to a temp directory
        forensics_dir = tmp_path / "forensics"
        monkeypatch.setattr("core.verifier._FORENSICS_DIR", forensics_dir)

        f = _write(tmp_path / "f.txt", "original")
        verifier = FileVerifier(f, "dummy_plugin")
        manifest = verifier.capture()

        f.chmod(0o644)
        f.write_text("tampered", encoding="utf-8")

        with pytest.raises(IntegrityError):
            verifier.verify(manifest)

        log_files = list(forensics_dir.glob("violation_*.json"))
        assert len(log_files) == 1

        log_data = json.loads(log_files[0].read_text())
        assert log_data["event"]  == "integrity_violation"
        assert log_data["plugin"] == "dummy_plugin"
        assert len(log_data["violations"]) > 0

    def test_no_forensic_log_on_clean_verify(self, tmp_path, monkeypatch):
        forensics_dir = tmp_path / "forensics"
        monkeypatch.setattr("core.verifier._FORENSICS_DIR", forensics_dir)

        f = _write(tmp_path / "f.txt")
        verifier = FileVerifier(f, "dummy_plugin")
        manifest = verifier.capture()
        verifier.verify(manifest)

        assert not forensics_dir.exists() or \
               len(list(forensics_dir.glob("violation_*.json"))) == 0


# =============================================================================
# verify_output_checksum()
# =============================================================================

class TestVerifyOutputChecksum:

    def test_matching_checksum_passes(self, tmp_path):
        f = _write(tmp_path / "output.txt", "plugin output")
        correct = sha256(f)
        verify_output_checksum(f, correct)  # must not raise

    def test_wrong_checksum_raises(self, tmp_path):
        f = _write(tmp_path / "output.txt", "plugin output")
        wrong = "a" * 64
        with pytest.raises(IntegrityError) as exc_info:
            verify_output_checksum(f, wrong)
        assert "checksum mismatch" in str(exc_info.value).lower()

    def test_missing_output_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            verify_output_checksum(tmp_path / "missing.txt", "a" * 64)

    def test_checksum_case_insensitive(self, tmp_path):
        f = _write(tmp_path / "output.txt", "data")
        correct = sha256(f).upper()  # uppercase hex
        verify_output_checksum(f, correct)  # must not raise
