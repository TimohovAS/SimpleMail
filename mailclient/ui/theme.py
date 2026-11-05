"""Цвета интерфейса автоматически следуют светлой/тёмной теме системы."""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


def is_dark() -> bool:
    app = QApplication.instance()
    return bool(app and app.palette().color(QPalette.Window).lightness() < 128)


def theme_palette(dark: bool) -> QPalette:
    palette = QPalette()
    colors = {
        QPalette.Window: "#20242c" if dark else "#f4f5f7",
        QPalette.WindowText: "#e5e7eb" if dark else "#1f2937",
        QPalette.Base: "#181c23" if dark else "#ffffff",
        QPalette.AlternateBase: "#222731" if dark else "#f3f4f6",
        QPalette.Text: "#e5e7eb" if dark else "#1f2937",
        QPalette.Button: "#2c323d" if dark else "#eef0f3",
        QPalette.ButtonText: "#e5e7eb" if dark else "#1f2937",
        QPalette.ToolTipBase: "#303744" if dark else "#fffbea",
        QPalette.ToolTipText: "#f3f4f6" if dark else "#1f2937",
        QPalette.Highlight: "#285a94" if dark else "#2563eb",
        QPalette.HighlightedText: "#ffffff",
        QPalette.Link: "#7db7ff" if dark else "#2563eb",
        QPalette.LinkVisited: "#c4a5fa" if dark else "#7c3aed",
        QPalette.PlaceholderText: "#a6aebb" if dark else "#667085",
        QPalette.Light: "#454d5c" if dark else "#ffffff",
        QPalette.Midlight: "#373e4b" if dark else "#e5e7eb",
        QPalette.Mid: "#454d5c" if dark else "#c6cbd3",
        QPalette.Dark: "#11151b" if dark else "#9ca3af",
        QPalette.Shadow: "#090c10" if dark else "#6b7280",
        QPalette.BrightText: "#ffffff",
    }
    if hasattr(QPalette, "Accent"):
        colors[QPalette.Accent] = colors[QPalette.Highlight]
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText, QPalette.PlaceholderText):
        palette.setColor(QPalette.Disabled, role, QColor("#858c99" if dark else "#858b96"))
    palette.setColor(QPalette.Disabled, QPalette.Highlight, palette.color(QPalette.Mid))
    return palette


class SystemTheme(QObject):
    def __init__(self, app: QApplication):
        super().__init__(app)
        self.app = app
        self._fallback_dark = is_dark()
        # Fusion учитывает все роли палитры, в том числе на старых версиях Windows/Qt.
        app.setStyle("Fusion")
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.apply)
        app.styleHints().colorSchemeChanged.connect(self._schedule)
        self.apply()

    def _schedule(self, _scheme):
        # Qt обновляет системную палитру после сигнала; применяем цвета следом.
        self._timer.start(0)

    def apply(self):
        scheme = self.app.styleHints().colorScheme()
        dark = scheme == Qt.ColorScheme.Dark if scheme != Qt.ColorScheme.Unknown else self._fallback_dark
        self.app.setPalette(theme_palette(dark))
        muted = "#a6aebb" if dark else "#667085"
        warning = "#f4c76b" if dark else "#975000"
        warning_bg = "#3b3020" if dark else "#fff7e0"
        warning_border = "#806338" if dark else "#f0d58a"
        self.app.setStyleSheet(
            f'QLabel[tone="muted"] {{ color: {muted}; }}'
            f'QLabel[tone="warning"] {{ color: {warning}; }}'
            f'QFrame[tone="warning"] {{ background: {warning_bg}; border: 1px solid {warning_border}; border-radius: 4px; }}'
            f'QFrame[tone="warning"] QLabel {{ color: {warning}; border: none; background: transparent; }}'
        )


def install_system_theme(app: QApplication) -> SystemTheme:
    """Установить один раз до создания окон; объект живёт вместе с QApplication."""
    manager = getattr(app, "_system_theme", None)
    if manager is None:
        manager = SystemTheme(app)
        app._system_theme = manager
    return manager
