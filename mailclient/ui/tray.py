"""Значок в области уведомлений: число непрочитанных и уведомление о новых письмах."""
from __future__ import annotations

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..i18n import tr
from .icons import app_icon

BADGE_COLOR = "#dc2626"


def badge_text(count: int) -> str:
    return "" if count <= 0 else ("99+" if count > 99 else str(count))


def badge_icon(base: QIcon, count: int, size: int = 64) -> QIcon:
    """Значок программы с красным кружком и числом непрочитанных."""
    pixmap = base.pixmap(size, size)
    if pixmap.isNull():
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
    text = badge_text(count)
    if text:
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        diameter = size * (0.62 if len(text) < 3 else 0.72)
        rect = QRectF(size - diameter, size - diameter, diameter, diameter)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(BADGE_COLOR))
        painter.drawEllipse(rect)
        font = QFont()
        font.setBold(True)
        font.setPixelSize(int(diameter * (0.62 if len(text) == 1 else 0.48 if len(text) == 2 else 0.36)))
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(rect, Qt.AlignCenter, text)
        painter.end()
    return QIcon(pixmap)


class MailTray(QObject):
    """Обёртка над QSystemTrayIcon; без области уведомлений (некоторые Linux) ничего не делает."""
    activated = Signal()
    check_requested = Signal()
    compose_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.count = 0
        self._base = app_icon()
        self.icon = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.icon = QSystemTrayIcon(self._base, self)
        menu = QMenu()
        for text, signal in ((tr("Открыть SimpleMail"), self.activated), (tr("Проверить почту"), self.check_requested),
                             (tr("Написать письмо"), self.compose_requested), (None, None),
                             (tr("Выход"), self.quit_requested)):
            if text is None:
                menu.addSeparator()
                continue
            action = QAction(text, menu)
            action.triggered.connect(signal.emit)
            menu.addAction(action)
        self._menu = menu
        self.icon.setContextMenu(menu)
        self.icon.activated.connect(self._on_activated)
        self.icon.messageClicked.connect(self.activated.emit)
        self.set_unread(0)
        self.icon.show()

    @property
    def available(self) -> bool:
        return self.icon is not None

    def _on_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.activated.emit()

    def set_unread(self, count: int) -> None:
        self.count = count
        if not self.icon:
            return
        self.icon.setIcon(badge_icon(self._base, count))
        self.icon.setToolTip(tr("SimpleMail — непрочитанных: {0}", count) if count else "SimpleMail")

    def notify(self, title: str, text: str) -> None:
        if self.icon and QSystemTrayIcon.supportsMessages():
            self.icon.showMessage(title, text, self._base, 10000)

    def hide(self) -> None:
        if self.icon:
            self.icon.hide()
