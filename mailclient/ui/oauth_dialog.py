"""Explicit Microsoft device login without blocking the mail UI."""
import threading
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QVBoxLayout

from .. import oauth
from ..i18n import tr
from .worker import run_task


class OAuthDialog(QDialog):
    def __init__(self, db, account, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Вход в Microsoft"))
        self.resize(510, 210)
        self.cancel = threading.Event()
        self.flow = None
        self.alive = True
        self.persistent = False
        QApplication.instance().aboutToQuit.connect(self.reject)
        self.status = QLabel(tr("Подготовка входа…"))
        self.status.setWordWrap(True)
        self.code = QLabel()
        self.code.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.open_button = QPushButton(tr("Открыть страницу Microsoft"))
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.flow["verification_uri"])))
        close = QPushButton(tr("Отмена"))
        close.clicked.connect(self.reject)
        layout = QVBoxLayout(self)
        for widget in (self.status, self.code, self.open_button, close):
            layout.addWidget(widget)

        def failed(error):
            if self.alive:
                self.status.setText(error.summary)
                self.open_button.setEnabled(False)

        def finished(persistent):
            if self.alive and persistent is not None:
                self.persistent = persistent
                self.alive = False
                self.accept()

        def ready(flow):
            self.flow = flow
            if not self.alive:
                flow["expires_at"] = 0
                return
            self.status.setText(tr("Откройте страницу Microsoft и введите код. После входа это окно закроется."))
            self.code.setText(flow["user_code"])
            self.open_button.setEnabled(True)
            run_task(oauth.finish_sign_in, db, account, flow, self.cancel, on_done=finished, on_error=failed)

        run_task(oauth.begin_sign_in, db, account, on_done=ready, on_error=failed)

    def reject(self):
        self.alive = False
        self.cancel.set()
        if self.flow is not None:
            self.flow["expires_at"] = 0
        super().reject()
