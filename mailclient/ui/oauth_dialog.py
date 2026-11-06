"""Вход Microsoft (код устройства) и Google (браузер) без блокировки окна почты."""
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
        self.db, self.account = db, account
        self.google = account.auth_method == "google"
        self.setWindowTitle(tr("Вход в {0}", "Google" if self.google else "Microsoft"))
        self.resize(510, 210)
        self.cancel = threading.Event()
        self.flow = None
        self.alive = True
        self.persistent = False
        self._attempt = 0
        QApplication.instance().aboutToQuit.connect(self.reject)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        self.code = QLabel()
        self.code.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.open_button = QPushButton()
        self.open_button.clicked.connect(self._open_browser)
        self.copy_button = QPushButton(tr("Скопировать ссылку для входа"))
        self.copy_button.clicked.connect(self._copy_link)
        self.retry_button = QPushButton(tr("Повторить вход"))
        self.retry_button.clicked.connect(self._start)
        self.close_button = QPushButton(tr("Отмена"))
        self.close_button.clicked.connect(self.reject)
        layout = QVBoxLayout(self)
        for widget in (self.status, self.code, self.open_button, self.copy_button,
                       self.retry_button, self.close_button):
            layout.addWidget(widget)
        self._start()

    @staticmethod
    def _discard_flow(flow):
        """A preparation task can finish after its dialog has been closed."""
        flow["expires_at"] = 0
        if flow.get("provider") == "google":
            flow["server"].server_close()

    def _start(self):
        self._attempt += 1
        attempt = self._attempt
        self.cancel = threading.Event()
        cancel = self.cancel
        self.flow = None
        self.status.setText(tr("Подготовка входа…"))
        self.code.clear()
        self.open_button.setText(tr("Открыть страницу Google") if self.google
                                 else tr("Открыть страницу Microsoft"))
        self.open_button.setEnabled(False)
        self.copy_button.setEnabled(False)
        self.retry_button.hide()
        self.close_button.setText(tr("Отмена"))

        def current():
            return self.alive and attempt == self._attempt

        def failed(error):
            if current():
                self.status.setText(error.short())
                self.open_button.setEnabled(False)
                self.copy_button.setEnabled(False)
                self.retry_button.show()
                self.close_button.setText(tr("Закрыть"))

        def finished(persistent):
            if not current():
                return
            if persistent is None:
                self.status.setText(tr("Вход не завершён. Нажмите «Повторить вход», чтобы попробовать снова."))
                self.open_button.setEnabled(False)
                self.copy_button.setEnabled(False)
                self.retry_button.show()
                self.close_button.setText(tr("Закрыть"))
                return
            self.persistent = persistent
            self.alive = False
            self.accept()

        def ready(flow):
            if not current():
                self._discard_flow(flow)
                return
            self.flow = flow
            self.open_button.setEnabled(True)
            self.copy_button.setEnabled(True)
            if flow.get("provider") == "google":
                self.open_button.setText(tr("Открыть страницу Google снова"))
                self._open_browser()
            else:
                self.status.setText(tr("Откройте страницу Microsoft и введите код. После входа это окно закроется."))
                self.code.setText(flow["user_code"])
            run_task(oauth.finish_sign_in, self.db, self.account, flow, cancel,
                     on_done=finished, on_error=failed)

        run_task(oauth.begin_sign_in, self.db, self.account, on_done=ready, on_error=failed)

    def _open_browser(self):
        if not self.flow:
            return
        opened = QDesktopServices.openUrl(QUrl(self.flow["verification_uri"]))
        if not opened:
            self.status.setText(tr("Не удалось открыть браузер. Скопируйте ссылку и откройте её в браузере вручную."))
        elif self.google:
            self.status.setText(tr("В браузере открылась страница Google. Выберите нужный аккаунт и отметьте "
                                   "разрешение на работу с Gmail (чтение, отправка и удаление писем), затем "
                                   "нажмите «Продолжить». После входа это окно закроется."))

    def _copy_link(self):
        if self.flow:
            QApplication.clipboard().setText(self.flow["verification_uri"])
            self.status.setText(tr("Ссылка скопирована. Вставьте её в адресную строку браузера и завершите вход."))

    def reject(self):
        self.alive = False
        self.cancel.set()
        if self.flow is not None:
            self.flow["expires_at"] = 0
        super().reject()
