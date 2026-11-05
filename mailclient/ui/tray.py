"""Значок в области уведомлений: число непрочитанных и уведомление о новых письмах."""
from __future__ import annotations

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..i18n import tr
from .icons import app_icon

# Жёлтый конверт вместо красного кружка: красное воспринимается как ошибка.
BADGE_COLOR = "#facc15"
BADGE_FLAP_COLOR = "#eab308"
BADGE_EDGE_COLOR = "#854d0e"
BADGE_TEXT_COLOR = "#1f1300"


def badge_text(count: int) -> str:
    return "" if count <= 0 else ("99+" if count > 99 else str(count))


def badge_icon(base: QIcon, count: int, size: int = 64) -> QIcon:
    """Значок программы с жёлтым конвертиком и числом новых (непрочитанных) писем."""
    pixmap = base.pixmap(size, size)
    if pixmap.isNull():
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
    text = badge_text(count)
    if text:
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        width = size * (0.78 if len(text) < 3 else 0.94)
        height = size * 0.62
        edge = max(1.5, size / 24)
        body = QRectF(size - width + edge / 2, size - height + edge / 2, width - edge, height - edge)
        painter.setPen(QPen(QColor(BADGE_EDGE_COLOR), edge, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(QColor(BADGE_COLOR))
        painter.drawRoundedRect(body, edge * 1.5, edge * 1.5)
        flap = QPolygonF([body.topLeft(), QPointF(body.center().x(), body.top() + body.height() * 0.42),
                          body.topRight()])
        painter.setBrush(QColor(BADGE_FLAP_COLOR))
        painter.drawPolygon(flap)
        font = QFont()
        font.setBold(True)
        font.setPixelSize(int(height * (0.78 if len(text) == 1 else 0.66 if len(text) == 2 else 0.52)))
        painter.setFont(font)
        painter.setPen(QColor(BADGE_TEXT_COLOR))
        painter.drawText(body.adjusted(0, body.height() * 0.14, 0, 0), Qt.AlignCenter, text)
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
