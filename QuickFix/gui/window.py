# =============================================================================
# QuickFix - gui/window.py
# =============================================================================
# Main window — the control panel of QuickFix.
# Responsibility: render the UI and translate user actions into
# core/controller.py calls. Zero business logic lives here.
#
# Layout:
#   ┌─────────────────────────────────────────────────────┐
#   │  Menu bar  (File | Help)                            │
#   ├─────────────────────────────────────────────────────┤
#   │  Toolbar   [Open] [Save] [Save As]   file info      │
#   ├──────────────────────────────┬──────────────────────┤
#   │  Plugin selector  [▼]        │  [▶ Run]             │
#   ├──────────────────────────────┴──────────────────────┤
#   │  Messenger (log + progress bar)                     │
#   ├─────────────────────────────────────────────────────┤
#   │  Status bar                                         │
#   └─────────────────────────────────────────────────────┘
# =============================================================================

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui  import (
    QAction, QColor, QFont, QFontDatabase,
    QIcon, QPalette, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QTextBrowser,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.controller import (
    Controller,
    ControllerError,
    ControllerEvent,
    EventKind,
    ExecutionCancelledError,
    NoOutputError,
)
from gui.messenger import MessengerWidget
from gui.worker    import PluginWorker


# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------

_ROOT        = Path(__file__).parent.parent
_PLUGINS_DIR = _ROOT / "plugins"


# -----------------------------------------------------------------------------
# Palette & stylesheet — dark industrial-minimal
# -----------------------------------------------------------------------------

_PALETTE_DARK = {
    "bg":          "#0b0f14",
    "bg_panel":    "#0e1117",
    "bg_control":  "#141920",
    "bg_hover":    "#1a2230",
    "border":      "#1e2530",
    "border_focus":"#2e4060",
    "text":        "#c8d4e0",
    "text_dim":    "#556677",
    "text_bright": "#eaf0f8",
    "accent":      "#4da6e0",
    "accent_dim":  "#2a6090",
    "ok":          "#4caf7d",
    "warn":        "#d4a017",
    "error":       "#e05c5c",
}

_APP_STYLESHEET = """
QMainWindow, QWidget#central {{
    background-color: {bg};
    color: {text};
}}

QMenuBar {{
    background-color: {bg_panel};
    color: {text};
    border-bottom: 1px solid {border};
    padding: 2px 4px;
    font-size: 12px;
    letter-spacing: 0.3px;
}}
QMenuBar::item:selected {{
    background-color: {bg_hover};
    color: {text_bright};
    border-radius: 3px;
}}
QMenu {{
    background-color: {bg_panel};
    color: {text};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 4px;
}}
QMenu::item:selected {{
    background-color: {bg_hover};
    color: {text_bright};
    border-radius: 3px;
}}
QMenu::separator {{
    height: 1px;
    background-color: {border};
    margin: 4px 8px;
}}

QToolBar {{
    background-color: {bg_panel};
    border-bottom: 1px solid {border};
    spacing: 4px;
    padding: 4px 8px;
}}

QPushButton {{
    background-color: {bg_control};
    color: {text};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 5px 14px;
    font-size: 12px;
    letter-spacing: 0.3px;
}}
QPushButton:hover {{
    background-color: {bg_hover};
    border-color: {border_focus};
    color: {text_bright};
}}
QPushButton:pressed {{
    background-color: {accent_dim};
    border-color: {accent};
}}
QPushButton:disabled {{
    color: {text_dim};
    border-color: {border};
    background-color: {bg_panel};
}}
QPushButton#run_btn {{
    background-color: {accent_dim};
    border-color: {accent};
    color: {text_bright};
    font-weight: 600;
    padding: 5px 20px;
    letter-spacing: 0.5px;
}}
QPushButton#run_btn:hover {{
    background-color: {accent};
    color: #ffffff;
}}
QPushButton#run_btn:disabled {{
    background-color: {bg_control};
    border-color: {border};
    color: {text_dim};
}}

QComboBox {{
    background-color: {bg_control};
    color: {text};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 4px 10px;
    font-size: 12px;
    min-width: 200px;
}}
QComboBox:hover {{
    border-color: {border_focus};
    color: {text_bright};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background-color: {bg_panel};
    color: {text};
    border: 1px solid {border};
    selection-background-color: {bg_hover};
    selection-color: {text_bright};
    outline: none;
    padding: 4px;
}}

QLabel {{
    color: {text};
    font-size: 12px;
}}
QLabel#file_label {{
    color: {text_bright};
    font-size: 12px;
    font-weight: 500;
    letter-spacing: 0.3px;
}}
QLabel#mime_label {{
    color: {text_dim};
    font-size: 11px;
    letter-spacing: 0.2px;
}}
QLabel#header_label {{
    color: {text_dim};
    font-size: 10px;
    letter-spacing: 1.2px;
    text-transform: uppercase;
}}

QStatusBar {{
    background-color: {bg_panel};
    color: {text_dim};
    border-top: 1px solid {border};
    font-size: 11px;
    padding: 0 8px;
}}

QSplitter::handle {{
    background-color: {border};
    height: 1px;
}}
""".format(**_PALETTE_DARK)


# -----------------------------------------------------------------------------
# ConfirmDialog — sandbox warning modal
# -----------------------------------------------------------------------------

class _ConfirmDialog(QDialog):
    """
    Modal dialog shown when a plugin declares sandbox.required=false.
    Blocks until the user explicitly accepts or rejects.
    """

    def __init__(self, message: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Security Warning")
        self.setModal(True)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 20)

        # Icon + title row
        title_row = QHBoxLayout()
        icon_lbl = QLabel("⚠")
        icon_lbl.setStyleSheet(
            f"color: {_PALETTE_DARK['warn']}; font-size: 22px;"
        )
        title_lbl = QLabel("Unsandboxed Plugin")
        title_lbl.setStyleSheet(
            f"color: {_PALETTE_DARK['text_bright']}; "
            "font-size: 14px; font-weight: 600; letter-spacing: 0.3px;"
        )
        title_row.addWidget(icon_lbl)
        title_row.addWidget(title_lbl)
        title_row.addStretch()
        layout.addLayout(title_row)

        # Message
        msg_lbl = QLabel(message)
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet(
            f"color: {_PALETTE_DARK['text']}; font-size: 12px; "
            f"line-height: 1.5; padding: 8px 0;"
        )
        layout.addWidget(msg_lbl)

        # Disclaimer
        disc = QLabel(
            "All responsibility for the plugin's actions lies with "
            "the plugin developer. QuickFix cannot guarantee isolation."
        )
        disc.setWordWrap(True)
        disc.setStyleSheet(
            f"color: {_PALETTE_DARK['text_dim']}; font-size: 11px;"
        )
        layout.addWidget(disc)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Yes |
            QDialogButtonBox.StandardButton.No,
        )
        buttons.button(QDialogButtonBox.StandardButton.Yes).setText(
            "I understand — proceed"
        )
        buttons.button(QDialogButtonBox.StandardButton.No).setText(
            "Cancel"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setStyleSheet(f"""
            QDialog {{
                background-color: {_PALETTE_DARK['bg_panel']};
                color: {_PALETTE_DARK['text']};
                border: 1px solid {_PALETTE_DARK['border']};
                border-radius: 6px;
            }}
            QDialogButtonBox QPushButton {{
                min-width: 130px;
            }}
        """)


# -----------------------------------------------------------------------------
# HelpDialog — plugin help.md viewer
# -----------------------------------------------------------------------------

class _HelpDialog(QDialog):
    """Displays the plugin's help.md in a scrollable dialog."""

    def __init__(self, plugin_name: str, help_path: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Help — {plugin_name}")
        self.setMinimumSize(560, 440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)

        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setMarkdown(help_path.read_text(encoding="utf-8"))
        browser.setStyleSheet(f"""
            QTextBrowser {{
                background-color: {_PALETTE_DARK['bg_panel']};
                color: {_PALETTE_DARK['text']};
                border: none;
                font-size: 12px;
                padding: 8px;
            }}
        """)
        layout.addWidget(browser)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)

        self.setStyleSheet(f"""
            QDialog {{
                background-color: {_PALETTE_DARK['bg_panel']};
                border: 1px solid {_PALETTE_DARK['border']};
                border-radius: 6px;
            }}
        """)


# -----------------------------------------------------------------------------
# MainWindow
# -----------------------------------------------------------------------------

class MainWindow(QMainWindow):
    """
    QuickFix main window.
    All user actions here translate into Controller API calls.
    """

    def __init__(self) -> None:
        super().__init__()

        self._controller = Controller(
            plugins_dir=_PLUGINS_DIR,
            confirm_cb=self._confirm_sandbox,
        )
        self._worker: PluginWorker | None = None

        self._setup_window()
        self._setup_menu()
        self._setup_toolbar()
        self._setup_plugin_bar()
        self._setup_messenger()
        self._setup_statusbar()
        self._refresh_plugin_list()
        self._update_controls()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _setup_window(self) -> None:
        self.setWindowTitle("QuickFix")
        self.setMinimumSize(720, 520)
        self.resize(860, 580)
        self.setStyleSheet(_APP_STYLESHEET)

        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)

        self._main_layout = QVBoxLayout(central)
        self._main_layout.setContentsMargins(0, 0, 0, 0)
        self._main_layout.setSpacing(0)

    def _setup_menu(self) -> None:
        mb = self.menuBar()

        # File menu
        file_menu = mb.addMenu("File")

        self._act_open    = QAction("Open…",    self)
        self._act_save    = QAction("Save",      self)
        self._act_save_as = QAction("Save As…", self)
        self._act_quit    = QAction("Quit",      self)

        self._act_open.setShortcut("Ctrl+O")
        self._act_save.setShortcut("Ctrl+S")
        self._act_save_as.setShortcut("Ctrl+Shift+S")
        self._act_quit.setShortcut("Ctrl+Q")

        self._act_open.triggered.connect(self._on_open)
        self._act_save.triggered.connect(self._on_save)
        self._act_save_as.triggered.connect(self._on_save_as)
        self._act_quit.triggered.connect(self.close)

        file_menu.addAction(self._act_open)
        file_menu.addSeparator()
        file_menu.addAction(self._act_save)
        file_menu.addAction(self._act_save_as)
        file_menu.addSeparator()
        file_menu.addAction(self._act_quit)

        # Help menu
        help_menu = mb.addMenu("Help")
        act_plugin_help = QAction("Plugin Help…", self)
        act_about       = QAction("About QuickFix", self)
        act_plugin_help.triggered.connect(self._on_plugin_help)
        act_about.triggered.connect(self._on_about)
        help_menu.addAction(act_plugin_help)
        help_menu.addSeparator()
        help_menu.addAction(act_about)

    def _setup_toolbar(self) -> None:
        tb = QToolBar("Main Toolbar")
        tb.setMovable(False)
        tb.setIconSize(QSize(16, 16))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(tb)

        # Action buttons
        btn_open    = QPushButton("Open")
        btn_save    = QPushButton("Save")
        btn_save_as = QPushButton("Save As")

        for btn in (btn_open, btn_save, btn_save_as):
            btn.setFixedHeight(28)

        btn_open.clicked.connect(self._on_open)
        btn_save.clicked.connect(self._on_save)
        btn_save_as.clicked.connect(self._on_save_as)

        self._btn_save    = btn_save
        self._btn_save_as = btn_save_as

        tb.addWidget(btn_open)
        tb.addWidget(btn_save)
        tb.addWidget(btn_save_as)

        # Separator + file info on the right
        spacer = QWidget()
        spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        tb.addWidget(spacer)

        self._file_label = QLabel("No file open")
        self._file_label.setObjectName("file_label")
        self._mime_label = QLabel("")
        self._mime_label.setObjectName("mime_label")

        info_widget = QWidget()
        info_layout = QVBoxLayout(info_widget)
        info_layout.setContentsMargins(0, 0, 8, 0)
        info_layout.setSpacing(1)
        info_layout.addWidget(self._file_label,
                               alignment=Qt.AlignmentFlag.AlignRight)
        info_layout.addWidget(self._mime_label,
                               alignment=Qt.AlignmentFlag.AlignRight)
        tb.addWidget(info_widget)

    def _setup_plugin_bar(self) -> None:
        """Plugin selector row between toolbar and messenger."""
        bar = QWidget()
        bar.setObjectName("plugin_bar")
        bar.setStyleSheet(f"""
            QWidget#plugin_bar {{
                background-color: {_PALETTE_DARK['bg_panel']};
                border-bottom: 1px solid {_PALETTE_DARK['border']};
            }}
        """)
        bar.setFixedHeight(44)

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(8)

        plugin_lbl = QLabel("PLUGIN")
        plugin_lbl.setObjectName("header_label")

        self._plugin_combo = QComboBox()
        self._plugin_combo.setPlaceholderText("— select a plugin —")
        self._plugin_combo.currentIndexChanged.connect(self._update_controls)

        self._run_btn = QPushButton("▶  Run")
        self._run_btn.setObjectName("run_btn")
        self._run_btn.setFixedHeight(30)
        self._run_btn.clicked.connect(self._on_run)

        layout.addWidget(plugin_lbl)
        layout.addWidget(self._plugin_combo, 1)
        layout.addWidget(self._run_btn)

        self._main_layout.addWidget(bar)

    def _setup_messenger(self) -> None:
        """Log panel fills the remaining space."""
        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(10, 10, 10, 6)
        wrapper_layout.setSpacing(0)

        self._messenger = MessengerWidget()
        wrapper_layout.addWidget(self._messenger)

        self._main_layout.addWidget(wrapper, 1)

    def _setup_statusbar(self) -> None:
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Ready")

    # ------------------------------------------------------------------
    # Plugin list management
    # ------------------------------------------------------------------

    def _refresh_plugin_list(self) -> None:
        """
        Populate the plugin combo with plugins compatible with the
        currently open file, or all plugins if no file is open.
        """
        self._plugin_combo.blockSignals(True)
        self._plugin_combo.clear()

        if self._controller.current_file:
            plugins = self._controller.compatible_plugins()
        else:
            from core.loader import PluginLoader
            plugins = PluginLoader(_PLUGINS_DIR).discover()

        for p in plugins:
            sandbox_icon = "■" if p.sandbox.required else "□"
            label = f"{sandbox_icon}  {p.plugin.name}  —  {p.plugin.description}"
            self._plugin_combo.addItem(label, userData=p.plugin.name)

        self._plugin_combo.blockSignals(False)
        self._update_controls()

    def _selected_plugin_name(self) -> str | None:
        idx = self._plugin_combo.currentIndex()
        if idx < 0:
            return None
        return self._plugin_combo.itemData(idx)

    # ------------------------------------------------------------------
    # Control state
    # ------------------------------------------------------------------

    def _update_controls(self) -> None:
        """Enable/disable controls based on current state."""
        has_file   = self._controller.current_file is not None
        has_plugin = self._selected_plugin_name() is not None
        has_output = self._controller.has_output
        running    = self._worker is not None and self._worker.isRunning()

        self._run_btn.setEnabled(has_file and has_plugin and not running)
        self._btn_save.setEnabled(has_output and not running)
        self._btn_save_as.setEnabled(has_output and not running)
        self._act_save.setEnabled(has_output and not running)
        self._act_save_as.setEnabled(has_output and not running)

    def _set_running(self, running: bool) -> None:
        self._messenger.set_running(running)
        self._update_controls()
        if running:
            self._status.showMessage("Running plugin…")
            self._run_btn.setText("■  Stop")
        else:
            self._status.showMessage("Ready")
            self._run_btn.setText("▶  Run")

    # ------------------------------------------------------------------
    # File actions
    # ------------------------------------------------------------------

    def _on_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open File",
            str(Path.home()),
            "All Files (*)",
        )
        if not path:
            return

        self._messenger.clear()

        try:
            mime = self._controller.open_file(Path(path))
        except ControllerError as exc:
            self._show_error(str(exc))
            return

        file_path = self._controller.current_file
        self._file_label.setText(file_path.name)
        self._mime_label.setText(mime)
        self._status.showMessage(f"Opened: {file_path.name}")

        self._messenger.post_text(
            f"Opened  {file_path.name}",
            _PALETTE_DARK["text"],
        )
        self._messenger.post_text(
            f"MIME    {mime}  ·  "
            f"{file_path.stat().st_size / 1024:.1f} KB",
            _PALETTE_DARK["text_dim"],
        )

        self._refresh_plugin_list()

    def _on_save(self) -> None:
        try:
            dest = self._controller.save_file()
            self._messenger.post_text(
                f"Saved → {dest.name}",
                _PALETTE_DARK["ok"],
            )
            self._status.showMessage(f"Saved: {dest.name}")
        except (ControllerError, NoOutputError) as exc:
            self._show_error(str(exc))

    def _on_save_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save As",
            str(Path.home()),
            "All Files (*)",
        )
        if not path:
            return
        try:
            dest = self._controller.save_file_as(Path(path))
            self._messenger.post_text(
                f"Saved as → {dest}",
                _PALETTE_DARK["ok"],
            )
            self._status.showMessage(f"Saved: {dest.name}")
        except (ControllerError, NoOutputError) as exc:
            self._show_error(str(exc))

    # ------------------------------------------------------------------
    # Plugin execution
    # ------------------------------------------------------------------

    def _on_run(self) -> None:
        # If running — stop (TODO: implement cancellation in worker)
        if self._worker and self._worker.isRunning():
            return

        plugin_name = self._selected_plugin_name()
        if not plugin_name:
            return

        self._messenger.clear()
        self._set_running(True)

        self._worker = PluginWorker(
            controller=self._controller,
            plugin_name=plugin_name,
            parent=self,
        )
        self._worker.event_received.connect(self._on_event)
        self._worker.finished_ok.connect(self._on_finished_ok)
        self._worker.finished_error.connect(self._on_finished_error)
        self._worker.finished_cancelled.connect(self._on_finished_cancelled)
        self._worker.start()

    def _on_event(self, event: ControllerEvent) -> None:
        self._messenger.post(event)

    def _on_finished_ok(self, output_path: Path) -> None:
        self._set_running(False)
        self._update_controls()
        self._status.showMessage(
            f"Done — {output_path.name if output_path else 'output ready'}"
        )

    def _on_finished_error(self, message: str) -> None:
        self._set_running(False)
        self._update_controls()
        self._status.showMessage("Error — see log")

    def _on_finished_cancelled(self) -> None:
        self._set_running(False)
        self._update_controls()
        self._messenger.post_text("Cancelled.", _PALETTE_DARK["text_dim"])
        self._status.showMessage("Cancelled")

    # ------------------------------------------------------------------
    # Help actions
    # ------------------------------------------------------------------

    def _on_plugin_help(self) -> None:
        plugin_name = self._selected_plugin_name()
        if not plugin_name:
            QMessageBox.information(
                self, "Plugin Help", "Select a plugin first."
            )
            return

        from core.loader import PluginLoader
        try:
            config    = PluginLoader(_PLUGINS_DIR).load(plugin_name)
            help_file = config.plugin_dir / "help.md"
            if not help_file.is_file():
                QMessageBox.information(
                    self, "Plugin Help",
                    f"No help.md found for '{plugin_name}'."
                )
                return
            dlg = _HelpDialog(plugin_name, help_file, self)
            dlg.exec()
        except Exception as exc:
            self._show_error(str(exc))

    def _on_about(self) -> None:
        QMessageBox.about(
            self,
            "About QuickFix",
            "<b>QuickFix</b><br>"
            "File manipulation through sandboxed plugins.<br><br>"
            "GUI: PySide6 · Core: Python 3.11+<br>"
            "Plugins: Lua, Bash, Python, Ruby, Perl",
        )

    # ------------------------------------------------------------------
    # Confirmation callback (called from worker thread via controller)
    # ------------------------------------------------------------------

    def _confirm_sandbox(self, message: str) -> bool:
        """
        Show a modal dialog to confirm unsandboxed execution.
        This is called from the worker thread — must be thread-safe.
        Using a blocking dialog on the main thread via invokeMethod
        would be ideal, but for simplicity we use a direct call since
        PySide6 will marshal it to the main thread via the event loop
        when connected via Qt.BlockingQueuedConnection.
        """
        dlg = _ConfirmDialog(message, self)
        return dlg.exec() == QDialog.DialogCode.Accepted

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(self, "Error", message)
        self._status.showMessage("Error")


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("QuickFix")
    app.setApplicationVersion("1.0.0")

    # Apply global dark palette to native widgets
    palette = QPalette()
    bg   = QColor(_PALETTE_DARK["bg"])
    text = QColor(_PALETTE_DARK["text"])
    palette.setColor(QPalette.ColorRole.Window,          bg)
    palette.setColor(QPalette.ColorRole.WindowText,      text)
    palette.setColor(QPalette.ColorRole.Base,            QColor(_PALETTE_DARK["bg_panel"]))
    palette.setColor(QPalette.ColorRole.AlternateBase,   QColor(_PALETTE_DARK["bg_control"]))
    palette.setColor(QPalette.ColorRole.Text,            text)
    palette.setColor(QPalette.ColorRole.ButtonText,      text)
    palette.setColor(QPalette.ColorRole.Button,          QColor(_PALETTE_DARK["bg_control"]))
    palette.setColor(QPalette.ColorRole.Highlight,       QColor(_PALETTE_DARK["accent"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
