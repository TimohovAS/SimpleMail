"""Check, cancelable download, and explicit installation of a verified update."""
from __future__ import annotations

import threading

from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QTextBrowser, QVBoxLayout,
)

from .. import __version__
from ..i18n import tr
from .. import updates
from .worker import run_task


class UpdatesDialog(QDialog):
    def __init__(self, parent, install):
        super().__init__(parent)
        self.setWindowTitle(tr("Обновления SimpleMail"))
        self.resize(550, 370)
        self._install_callback = install
        self.release = None
        self.installer = None
        self.busy = False
        self.cancel = threading.Event()
        self._generation = 0
        self.status = QLabel(tr("Текущая версия: {0}", __version__))
        self.status.setWordWrap(True)
        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(False)
        self.progress = QProgressBar()
        self.progress.hide()
        self.check_button = QPushButton(tr("Проверить обновления"))
        self.download_button = QPushButton(tr("Скачать"))
        self.download_button.setEnabled(False)
        self.install_button = QPushButton(tr("Установить и закрыть SimpleMail"))
        self.install_button.setEnabled(False)
        self.cancel_button = QPushButton(tr("Отменить загрузку"))
        self.cancel_button.hide()
        self.close_button = QPushButton(tr("Закрыть"))
        self.check_button.clicked.connect(self.check)
        self.download_button.clicked.connect(self.download)
        self.install_button.clicked.connect(self.install)
        self.cancel_button.clicked.connect(lambda: self.cancel.set())
        self.close_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        for button in (self.check_button, self.download_button, self.install_button):
            buttons.addWidget(button)
        footer = QHBoxLayout()
        footer.addWidget(self.cancel_button)
        footer.addStretch()
        footer.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.notes, 1)
        layout.addWidget(self.progress)
        layout.addLayout(buttons)
        layout.addLayout(footer)

    def _busy(self, value):
        self.busy = value
        self.check_button.setEnabled(not value)
        self.download_button.setEnabled(not value and self.release is not None and self.installer is None)
        self.install_button.setEnabled(not value and self.installer is not None and updates.installation_supported())

    def _begin(self):
        self._generation += 1
        self.cancel = threading.Event()
        self._busy(True)
        return self._generation

    def _current(self, generation):
        return generation == self._generation and not self.parent()._closing

    def check(self, automatic=False):
        if self.busy:
            return
        self.release = self.installer = None
        self.notes.clear()
        self.status.setText(tr("Проверка обновлений…"))
        generation = self._begin()

        def done(release):
            if not self._current(generation):
                return
            self.release = release
            self._busy(False)
            if release is None:
                self.status.setText(tr("Новых обновлений нет. Текущая версия: {0}", __version__))
                return
            self.status.setText(tr("Доступна версия {0}. Размер: {1:.1f} МБ.", release.version, release.size / 1048576))
            self.notes.setPlainText(release.notes)
            if automatic:
                self.show()

        def failed(error):
            if not self._current(generation):
                return
            self._busy(False)
            self.status.setText(tr("Не удалось проверить обновления. Повторите позже."))
            self.notes.setPlainText(error.details)

        run_task(updates.check_update, on_done=done, on_error=failed)

    def download(self):
        if self.busy or self.release is None:
            return
        generation = self._begin()
        release, cancel = self.release, self.cancel
        self.progress.setValue(0)
        self.progress.show()
        self.cancel_button.show()
        self.status.setText(tr("Загрузка обновления…"))

        def download(progress):
            try:
                return updates.download_update(release, cancel=cancel, progress=progress)
            except updates.DownloadCancelled:
                return None

        def done(path):
            if not self._current(generation):
                return
            self.installer = path
            self.cancel_button.hide()
            self._busy(False)
            if path is None:
                self.status.setText(tr("Загрузка отменена."))
            elif updates.installation_supported():
                self.status.setText(tr("Обновление скачано и проверено. Можно установить."))
            else:
                self.status.setText(tr("Для установки обновлений запустите собранную Windows-версию."))

        def failed(error):
            if not self._current(generation):
                return
            self.cancel_button.hide()
            self._busy(False)
            self.status.setText(tr("Не удалось скачать или проверить обновление. Повторите загрузку."))
            self.notes.setPlainText(error.details)

        run_task(download, on_done=done, on_error=failed,
                 on_progress=lambda value: self.progress.setValue(int(value)) if self._current(generation) else None)

    def install(self):
        if self.busy or self.installer is None or not updates.installation_supported():
            return
        generation = self._begin()
        release = self.release

        def done(path):
            if not self._current(generation):
                return
            self._busy(False)
            if self._install_callback(path, release):
                self.accept()

        def failed(error):
            if not self._current(generation):
                return
            self.installer = None
            self._busy(False)
            self.status.setText(tr("Не удалось скачать или проверить обновление. Повторите загрузку."))
            self.notes.setPlainText(error.details)

        run_task(updates.verify_installer, self.installer, release, on_done=done, on_error=failed)

    def reject(self):
        self.cancel.set()
        self._generation += 1
        self.progress.hide()
        self.cancel_button.hide()
        self._busy(False)
        super().reject()
