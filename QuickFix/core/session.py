# =============================================================================
# QuickFix - core/session.py
# =============================================================================
# Isolated execution environment for a single plugin run.
#
# Responsibilities:
#   - Create an isolated temporary directory per session
#   - Acquire an exclusive file lock on the original file
#   - Copy the original file as read-only into the session input directory
#   - Validate input file size and MIME type against plugin constraints
#   - Expose clean input/output paths to the caller (sandbox, controller)
#   - Guarantee cleanup of the temp directory on exit — including on crash
#   - Release the file lock on exit
#   - Verify original file integrity via FileVerifier (before and after)
#
# Usage (context manager):
#   from core.session import PluginSession
#
#   with PluginSession(original_path, plugin_config) as session:
#       print(session.input_file)   # read-only copy of original
#       print(session.output_dir)   # writable directory for plugin output
#       print(session.session_dir)  # full temp directory path
#       print(session.manifest)     # FileManifest captured before execution
#
# The session object is passed to sandbox.py for execution.
# After the plugin exits, the controller calls session.verify_integrity()
# before accepting any output.
#
# This module never executes plugins.
# =============================================================================

from __future__ import annotations

import fcntl
import logging
import mimetypes
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

from core.loader import PluginConfig
from core.verifier import FileManifest, FileVerifier, IntegrityError

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class SessionError(Exception):
    """Raised when a session cannot be created or a precondition fails."""


class FileLockError(SessionError):
    """Raised when the original file is already locked by another session."""


class FileSizeError(SessionError):
    """Raised when the original file exceeds the plugin's max_size_mb."""


class MimeTypeError(SessionError):
    """Raised when the original file MIME type is not accepted by the plugin."""


# -----------------------------------------------------------------------------
# SessionPaths — resolved paths exposed to callers
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionPaths:
    """
    Resolved filesystem paths for a single plugin session.
    All paths are absolute and exist at the time of construction.
    """
    session_dir: Path   # root temp directory — cleaned up on exit
    input_file:  Path   # read-only copy of the original inside session_dir
    output_dir:  Path   # writable output directory for plugin results
    lock_file:   Path   # exclusive lock file path (outside session_dir)


# -----------------------------------------------------------------------------
# PluginSession — context manager
# -----------------------------------------------------------------------------

class PluginSession:
    """
    Manages the isolated environment for a single plugin execution.

    Must be used as a context manager. Guarantees cleanup regardless
    of whether the plugin succeeds, fails, or raises an exception.

    Attributes (available inside the `with` block):
        input_file  (Path): read-only copy of the original file
        output_dir  (Path): writable directory for plugin output
        session_dir (Path): root of the temp directory tree
        manifest (FileManifest): integrity snapshot taken before execution
    """

    def __init__(
        self,
        original_path: Path,
        plugin_config: PluginConfig,
    ) -> None:
        if not original_path.is_absolute():
            original_path = original_path.resolve()

        self._original    = original_path
        self._config      = plugin_config
        self._lock_fh     = None       # file handle for fcntl lock
        self._verifier: FileVerifier | None = None
        self._manifest:  FileManifest | None = None
        self._paths:     SessionPaths | None = None
        self._tmp_dir    = None        # TemporaryDirectory instance

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "PluginSession":
        try:
            self._validate_original()
            self._acquire_lock()
            self._create_session_dirs()
            self._copy_input()
            self._capture_integrity()
        except Exception:
            self._cleanup()
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self._cleanup()
        return False   # never suppress exceptions

    # ------------------------------------------------------------------
    # Public properties (available inside `with` block)
    # ------------------------------------------------------------------

    @property
    def input_file(self) -> Path:
        self._require_active()
        return self._paths.input_file

    @property
    def output_dir(self) -> Path:
        self._require_active()
        return self._paths.output_dir

    @property
    def session_dir(self) -> Path:
        self._require_active()
        return self._paths.session_dir

    @property
    def manifest(self) -> FileManifest:
        self._require_active()
        return self._manifest

    @property
    def plugin_name(self) -> str:
        return self._config.plugin.name

    # ------------------------------------------------------------------
    # Integrity verification (called by controller after plugin exits)
    # ------------------------------------------------------------------

    def verify_integrity(self) -> None:
        """
        Verify the original file was not modified during plugin execution.

        Must be called after the plugin exits and before accepting output.
        Delegates to FileVerifier — raises IntegrityError on violation.

        Raises:
            IntegrityError: Original file was tampered with.
            SessionError:   Called before or after the session context.
        """
        self._require_active()

        if self._verifier is None or self._manifest is None:
            raise SessionError(
                "verify_integrity() called before integrity was captured"
            )

        self._verifier.verify(self._manifest)

        logger.info(
            "Session '%s': integrity verified for '%s'",
            self._config.plugin.name,
            self._original.name,
        )

    # ------------------------------------------------------------------
    # Internal setup steps
    # ------------------------------------------------------------------

    def _validate_original(self) -> None:
        """Check that the original file exists, is readable, and meets
        the plugin's size and MIME type constraints."""

        if not self._original.is_file():
            raise SessionError(
                f"Original file not found: {self._original}"
            )

        # Size check
        size_mb = self._original.stat().st_size / (1024 * 1024)
        max_mb  = self._config.input.max_size_mb

        if size_mb > max_mb:
            raise FileSizeError(
                f"File '{self._original.name}' is {size_mb:.1f} MB — "
                f"plugin '{self._config.plugin.name}' accepts up to {max_mb} MB"
            )

        # MIME type check — uses file extension as the signal
        # The controller may refine this with python-magic if available
        detected_mime, _ = mimetypes.guess_type(str(self._original))

        if detected_mime is None:
            raise MimeTypeError(
                f"Cannot determine MIME type of '{self._original.name}'. "
                f"Plugin '{self._config.plugin.name}' accepts: "
                f"{list(self._config.input.accepts)}"
            )

        accepted = self._config.input.accepts

        if detected_mime not in accepted:
            raise MimeTypeError(
                f"File '{self._original.name}' is '{detected_mime}' — "
                f"plugin '{self._config.plugin.name}' accepts: {list(accepted)}"
            )

        logger.debug(
            "Validated '%s' (%.1f MB, %s)",
            self._original.name, size_mb, detected_mime,
        )

    def _acquire_lock(self) -> None:
        """
        Acquire an exclusive non-blocking fcntl lock on the original file.

        Lock file lives alongside the original as <original>.quickfix.lock
        to avoid polluting /tmp with lock files that might outlive the process.

        Raises:
            FileLockError: Another session is already processing this file.
        """
        lock_path = self._original.with_suffix(
            self._original.suffix + ".quickfix.lock"
        )

        try:
            self._lock_fh = open(lock_path, "w")
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_path = lock_path
            logger.debug("Lock acquired: %s", lock_path)
        except BlockingIOError:
            if self._lock_fh:
                self._lock_fh.close()
                self._lock_fh = None
            raise FileLockError(
                f"File '{self._original.name}' is already being processed "
                f"by another session. Wait for it to complete."
            )

    def _create_session_dirs(self) -> None:
        """
        Create the session temporary directory tree.

        Structure:
            /tmp/quickfix_<plugin>_XXXXXX/
                input/    — contains the read-only copy of the original
                output/   — writable, for plugin results
        """
        self._tmp_dir = tempfile.TemporaryDirectory(
            prefix=f"quickfix_{self._config.plugin.name}_",
            dir="/tmp",
        )
        session_dir = Path(self._tmp_dir.name)
        input_dir   = session_dir / "input"
        output_dir  = session_dir / "output"

        input_dir.mkdir()
        output_dir.mkdir()

        # Store paths — input_file set after copy in _copy_input()
        self._paths = SessionPaths(
            session_dir = session_dir,
            input_file  = input_dir / self._original.name,
            output_dir  = output_dir,
            lock_file   = self._lock_path,
        )

        logger.debug("Session directory: %s", session_dir)

    def _copy_input(self) -> None:
        """
        Copy the original file into the session input directory as read-only.

        The copy is made with shutil.copy2 to preserve metadata.
        chmod 444 is applied to the copy — the plugin must never modify it.
        """
        dest = self._paths.input_file
        shutil.copy2(self._original, dest)
        dest.chmod(0o444)

        logger.debug(
            "Input copy created: %s (read-only)",
            dest.name,
        )

    def _capture_integrity(self) -> None:
        """
        Snapshot the original file's integrity before execution.
        Also applies read-only protection to the original.
        """
        self._verifier = FileVerifier(self._original, self._config.plugin.name)
        self._manifest = self._verifier.capture()

        logger.debug(
            "Integrity captured for '%s' (sha256=%s...)",
            self._original.name,
            self._manifest.sha256[:16],
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _cleanup(self) -> None:
        """
        Release the lock, restore file permissions, and remove the temp dir.
        Called on both normal exit and exception — order matters:
          1. Restore verifier permissions (chmod back to original)
          2. Release and remove the lock file
          3. Remove the temp directory
        """
        # Step 1 — restore original file permissions
        # _restore() is idempotent — safe to call even if capture() failed
        if self._verifier is not None:
            try:
                self._verifier._restore()
            except Exception as exc:
                logger.warning("Failed to restore file permissions: %s", exc)

        # Step 2 — release lock
        if self._lock_fh is not None:
            try:
                fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
                self._lock_fh.close()
            except Exception as exc:
                logger.warning("Failed to release lock: %s", exc)
            finally:
                self._lock_fh = None

        if hasattr(self, "_lock_path") and self._lock_path.exists():
            try:
                self._lock_path.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning("Failed to remove lock file: %s", exc)

        # Step 3 — remove temp directory
        if self._tmp_dir is not None:
            try:
                self._tmp_dir.cleanup()
            except Exception as exc:
                logger.warning("Failed to clean up session directory: %s", exc)
            finally:
                self._tmp_dir = None

        logger.debug(
            "Session cleaned up for plugin '%s'",
            self._config.plugin.name,
        )

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def _require_active(self) -> None:
        if self._paths is None:
            raise SessionError(
                "Session is not active — use PluginSession as a context manager"
            )
