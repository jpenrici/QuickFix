# =============================================================================
# QuickFix - gui/worker.py
# =============================================================================
# QThread worker — runs the controller pipeline off the main thread
# so the GUI stays responsive during plugin execution.
#
# Responsibilities:
#   - Receive a Controller instance and plugin name from window.py
#   - Execute run_plugin() in a background thread
#   - Emit Qt signals for each ControllerEvent received
#   - Emit a final signal on completion or error
#
# window.py connects to these signals and updates the UI.
# This module contains zero UI code.
# =============================================================================

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QThread, Signal

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.controller import (
    Controller,
    ControllerError,
    ControllerEvent,
    EventKind,
    ExecutionCancelledError,
)


class PluginWorker(QThread):
    """
    Background thread for plugin execution.

    Signals:
        event_received(ControllerEvent)  — emitted for each pipeline event
        finished_ok(Path)                — emitted on success with output path
        finished_error(str)              — emitted on failure with error message
        finished_cancelled()             — emitted when user cancelled
    """

    event_received   = Signal(object)   # ControllerEvent
    finished_ok      = Signal(object)   # Path
    finished_error   = Signal(str)
    finished_cancelled = Signal()

    def __init__(
        self,
        controller:  Controller,
        plugin_name: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._controller  = controller
        self._plugin_name = plugin_name

    def run(self) -> None:
        """Execute the pipeline. Called by QThread.start()."""
        try:
            for event in self._controller.run_plugin(self._plugin_name):
                self.event_received.emit(event)

            if self._controller.has_output:
                self.finished_ok.emit(self._controller.last_output)
            else:
                self.finished_error.emit("Plugin produced no output.")

        except ExecutionCancelledError:
            self.finished_cancelled.emit()

        except ControllerError as exc:
            self.finished_error.emit(str(exc))

        except Exception as exc:  # noqa: BLE001
            self.finished_error.emit(f"Unexpected error: {exc}")
