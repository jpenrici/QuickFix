# =============================================================================
# QuickFix - core/verifier.py
# =============================================================================
# File integrity verification — before and after plugin execution.
#
# Responsibilities:
#   - Compute and compare SHA-256 checksums of files
#   - Build and persist a session manifest before plugin execution
#   - Verify the original file was not modified after plugin execution
#   - Write forensic logs when a violation is detected
#   - Apply and restore filesystem-level read-only protection
#
# Usage:
#   from core.verifier import FileVerifier
#
#   verifier = FileVerifier(original_path=Path("report.txt"))
#   manifest = verifier.capture()           # before execution
#   verifier.verify(manifest)               # after execution — raises on violation
#
# This module never executes plugins — it only inspects files.
# =============================================================================

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Forensic logs directory — created by setup.sh
_FORENSICS_DIR = Path.home() / ".local" / "share" / "quickfix" / "forensics"

# Read chunk size for SHA-256 — 64 KB
_CHUNK_SIZE = 65_536


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class IntegrityError(Exception):
    """
    Raised when the original file has been modified after plugin execution.
    Carries the manifest for forensic inspection.
    """
    def __init__(self, message: str, manifest: "FileManifest") -> None:
        super().__init__(message)
        self.manifest = manifest


# -----------------------------------------------------------------------------
# FileManifest — immutable snapshot of a file before execution
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class FileManifest:
    """
    Immutable snapshot of a file's identity at a point in time.
    Used as the ground truth for post-execution integrity checks.
    """
    path:           str       # absolute path as string
    sha256:         str       # hex digest
    size_bytes:     int       # file size in bytes
    inode:          int       # inode number
    mtime_ns:       int       # modification time in nanoseconds
    captured_at:    str       # ISO-8601 UTC timestamp of capture
    plugin_name:    str       # plugin that will process this file

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "FileManifest":
        return cls(**data)


# -----------------------------------------------------------------------------
# FileVerifier — public API
# -----------------------------------------------------------------------------

class FileVerifier:
    """
    Verifies the integrity of the original file around plugin execution.

    Typical usage inside session context:
        verifier = FileVerifier(original_path, plugin_name)
        manifest = verifier.capture()     # call before execution
        # ... plugin runs ...
        verifier.verify(manifest)         # call after execution
    """

    def __init__(self, original_path: Path, plugin_name: str) -> None:
        if not original_path.is_absolute():
            original_path = original_path.resolve()

        if not original_path.is_file():
            raise FileNotFoundError(
                f"Original file not found: {original_path}"
            )

        self._path       = original_path
        self._plugin     = plugin_name
        self._saved_mode: int | None = None

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def capture(self) -> FileManifest:
        """
        Snapshot the original file and make it read-only.

        Returns a FileManifest to be passed to verify() after execution.
        Must be called before applying read-only protection to avoid
        stat race conditions.
        """
        st = self._path.stat()

        manifest = FileManifest(
            path=str(self._path),
            sha256=sha256(self._path),
            size_bytes=st.st_size,
            inode=st.st_ino,
            mtime_ns=st.st_mtime_ns,
            captured_at=_utcnow(),
            plugin_name=self._plugin,
        )

        self._protect()

        logger.debug(
            "Captured manifest for '%s' (sha256=%s)",
            self._path.name,
            manifest.sha256[:16] + "...",
        )

        return manifest

    def verify(self, manifest: FileManifest) -> None:
        """
        Verify the original file matches the manifest captured before execution.

        Restores the original file permissions regardless of outcome.
        Raises IntegrityError if any discrepancy is detected.
        Writes a forensic log on violation.

        Args:
            manifest: The FileManifest returned by capture().

        Raises:
            IntegrityError: Original file was modified during plugin execution.
        """
        self._restore()

        violations: list[str] = []

        # Check 1 — SHA-256 (content)
        current_hash = sha256(self._path)
        if current_hash != manifest.sha256:
            violations.append(
                f"SHA-256 mismatch: expected {manifest.sha256}, "
                f"got {current_hash}"
            )

        # Check 2 — file size
        st = self._path.stat()
        if st.st_size != manifest.size_bytes:
            violations.append(
                f"Size mismatch: expected {manifest.size_bytes} bytes, "
                f"got {st.st_size} bytes"
            )

        # Check 3 — inode (detects replacement with a different file)
        if st.st_ino != manifest.inode:
            violations.append(
                f"Inode mismatch: expected {manifest.inode}, "
                f"got {st.st_ino} — file may have been replaced"
            )

        # Check 4 — modification time
        if st.st_mtime_ns != manifest.mtime_ns:
            violations.append(
                f"mtime mismatch: expected {manifest.mtime_ns}, "
                f"got {st.st_mtime_ns}"
            )

        if violations:
            self._write_forensic_log(manifest, violations)
            raise IntegrityError(
                f"INTEGRITY VIOLATION: original file was modified "
                f"during execution of plugin '{manifest.plugin_name}'.\n"
                + "\n".join(f"  - {v}" for v in violations),
                manifest=manifest,
            )

        logger.debug(
            "Integrity verified for '%s' — no violations.",
            self._path.name,
        )

    # ------------------------------------------------------------------
    # Filesystem protection
    # ------------------------------------------------------------------

    def _protect(self) -> None:
        """Set the original file to read-only (chmod 444)."""
        current_mode = stat.S_IMODE(self._path.stat().st_mode)
        self._saved_mode = current_mode
        self._path.chmod(0o444)
        logger.debug("Protected '%s' (saved mode=%o)", self._path.name, current_mode)

    def _restore(self) -> None:
        """Restore the original file permissions saved by _protect()."""
        if self._saved_mode is not None:
            self._path.chmod(self._saved_mode)
            logger.debug(
                "Restored '%s' permissions (mode=%o)",
                self._path.name,
                self._saved_mode,
            )
            self._saved_mode = None

    # ------------------------------------------------------------------
    # Forensic log
    # ------------------------------------------------------------------

    def _write_forensic_log(
        self,
        manifest: FileManifest,
        violations: list[str],
    ) -> None:
        """
        Write a forensic log entry for post-incident inspection.
        Failures here are logged but never re-raised — the IntegrityError
        must always propagate regardless of log write success.
        """
        try:
            _FORENSICS_DIR.mkdir(parents=True, exist_ok=True)

            timestamp = _utcnow().replace(":", "-").replace("+", "")
            log_name  = f"violation_{manifest.plugin_name}_{timestamp}.json"
            log_path  = _FORENSICS_DIR / log_name

            entry = {
                "event":      "integrity_violation",
                "detected_at": _utcnow(),
                "plugin":     manifest.plugin_name,
                "file":       manifest.path,
                "violations": violations,
                "manifest":   manifest.to_dict(),
                "current": {
                    "sha256":     sha256(self._path),
                    "size_bytes": self._path.stat().st_size,
                    "inode":      self._path.stat().st_ino,
                    "mtime_ns":   self._path.stat().st_mtime_ns,
                },
            }

            log_path.write_text(
                json.dumps(entry, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            logger.warning("Forensic log written: %s", log_path)

        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to write forensic log: %s", exc)


# -----------------------------------------------------------------------------
# Output checksum verification
# -----------------------------------------------------------------------------

def verify_output_checksum(output_file: Path, expected_sha256: str) -> None:
    """
    Verify the checksum of the plugin's output file against the value
    reported by the plugin itself in the JSONL 'done' event.

    Args:
        output_file:     Path to the output file produced by the plugin.
        expected_sha256: SHA-256 hex digest from the plugin's 'done' event.

    Raises:
        FileNotFoundError: Output file does not exist.
        IntegrityError:    Checksum mismatch between file and plugin report.
    """
    if not output_file.is_file():
        raise FileNotFoundError(
            f"Plugin output file not found: {output_file}"
        )

    actual = sha256(output_file)

    if actual != expected_sha256.lower():
        raise IntegrityError(
            f"Output checksum mismatch for '{output_file.name}':\n"
            f"  Plugin reported: {expected_sha256}\n"
            f"  Actual:          {actual}",
            manifest=None,  # type: ignore[arg-type]
        )

    logger.debug(
        "Output checksum verified for '%s' (sha256=%s)",
        output_file.name,
        actual[:16] + "...",
    )


# -----------------------------------------------------------------------------
# Standalone utility
# -----------------------------------------------------------------------------

def sha256(path: Path) -> str:
    """
    Compute the SHA-256 hex digest of a file.
    Reads in chunks — safe for large files.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _utcnow() -> str:
    """Return current UTC time as an ISO-8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()
