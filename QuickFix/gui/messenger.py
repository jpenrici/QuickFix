# =============================================================================
# QuickFix - gui/messenger.py
# =============================================================================
# Message panel widget — the single read-only area that displays all
# pipeline events, progress, warnings and status in real time.
#
# Responsibilities:
#   - Receive ControllerEvent objects and render them as styled log lines
#   - Display a progress bar during execution
#   - Auto-scroll to the latest message
#   - Expose a clear() method for new sessions
#
# This widget never calls the controller — it only receives and displays.
# =============================================================================

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore  import Qt
from PySide6.QtGui   import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QProgressBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.controller import ControllerEvent, EventKind


# -----------------------------------------------------------------------------
# Per-event styling
# -----------------------------------------------------------------------------

_STYLES: dict[EventKind, tuple[str, str]] = {
    # (prefix, hex color)
    EventKind.MESSAGE:            ("  ·  ", "#8899aa"),
    EventKind.PROGRESS:           ("  ●  ", "#4da6e0"),
    EventKind.DONE:               ("  ✓  ", "#4caf7d"),
    EventKind.ERROR:              ("  ✗  ", "#e05c5c"),
    EventKind.INTEGRITY_VIOLATION:("  ⚠  ", "#e07d30"),
    EventKind.SANDBOX_WARNING:    ("  ⚠  ", "#d4a017"),
}


class MessengerWidget(QWidget):
    """
    Read-only log panel with progress bar.
    Placed in the lower half of the main window.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._setup_ui()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def post(self, event: ControllerEvent) -> None:
        """Append a styled line for the given ControllerEvent."""
        prefix, color = _STYLES.get(
            event.kind,
            ("  ·  ", "#8899aa"),
        )

        # Build the display text
        pct_tag = ""
        if event.kind == EventKind.PROGRESS and event.percent is not None:
            pct_tag = f"[{event.percent:3d}%] "
            self._progress_bar.setValue(event.percent)
            self._progress_bar.setVisible(True)

        line = f"{prefix}{pct_tag}{event.message}"
        self._append_colored(line, color)

        # Hide progress bar on terminal events
        if event.kind in (EventKind.DONE, EventKind.ERROR,
                          EventKind.INTEGRITY_VIOLATION):
            self._progress_bar.setVisible(False)
            if event.kind == EventKind.DONE:
                self._progress_bar.setValue(100)

    def post_text(self, text: str, color: str = "#8899aa") -> None:
        """Append a plain styled line — for controller-level messages."""
        self._append_colored(f"  ·  {text}", color)

    def clear(self) -> None:
        """Clear all messages and reset the progress bar."""
        self._log.clear()
        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(False)

    def set_running(self, running: bool) -> None:
        """Show or hide the indeterminate progress state."""
        if running:
            self._progress_bar.setVisible(True)
            self._progress_bar.setRange(0, 0)   # indeterminate
        else:
            self._progress_bar.setRange(0, 100)
            self._progress_bar.setVisible(False)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # Log area
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont("JetBrains Mono, Fira Code, Courier New", 9))
        self._log.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._log.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._log.setStyleSheet("""
            QTextEdit {
                background-color: #0e1117;
                border: 1px solid #1e2530;
                border-radius: 4px;
                padding: 8px;
                color: #8899aa;
                selection-background-color: #1e3a5f;
            }
            QScrollBar:vertical {
                background: #0e1117;
                width: 8px;
            }
            QScrollBar::handle:vertical {
                background: #2a3545;
                border-radius: 4px;
            }
        """)

        # Progress bar
        self._progress_bar = QProgressBar()
        self._progress_bar.setVisible(False)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(3)
        self._progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #1e2530;
                border: none;
                border-radius: 1px;
            }
            QProgressBar::chunk {
                background-color: #4da6e0;
                border-radius: 1px;
            }
        """)

        layout.addWidget(self._log)
        layout.addWidget(self._progress_bar)

    def _append_colored(self, text: str, color: str) -> None:
        """Append a colored line to the log and auto-scroll."""
        cursor = self._log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor.setCharFormat(fmt)
        cursor.insertText(text + "\n")

        self._log.setTextCursor(cursor)
        self._log.ensureCursorVisible()
