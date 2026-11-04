"""Диалоги управления почтовыми ящиками."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
)

from ..db import Account, Database
from ..passwords import delete_password, get_password, set_password
from ..protocols import test_connection
from .worker import run_task

# Готовые настройки популярных почтовых сервисов
PRESETS = {
    "gmail.com":  ("imap.gmail.com", 993, "smtp.gmail.com", 465, "ssl"),
    "yandex.ru":  ("imap.yandex.ru", 993, "smtp.yandex.ru", 465, "ssl"),
    "ya.ru":      ("imap.yandex.ru", 993, "smtp.yandex.ru", 465, "ssl"),
    "mail.ru":    ("imap.mail.ru", 993, "smtp.mail.ru", 465, "ssl"),
    "bk.ru":      ("imap.mail.ru", 993, "smtp.mail.ru", 465, "ssl"),
    "inbox.ru":   ("imap.mail.ru", 993, "smtp.mail.ru", 465, "ssl"),
    "list.ru":    ("imap.mail.ru", 993, "smtp.mail.ru", 465, "ssl"),
    "rambler.ru": ("imap.rambler.ru", 993, "smtp.rambler.ru", 465, "ssl"),
    "outlook.com": ("outlook.office365.com", 993, "smtp.office365.com", 587, "starttls"),
    "hotmail.com": ("outlook.office365.com", 993, "smtp.office365.com", 587, "starttls"),
}


class AccountEditDialog(QDialog):
    def __init__(self, db: Database, account: Account | None = None, parent=None):
        super().__init__(parent)
        self.db = db
        self.account = account or Account()
        self.setWindowTitle("Почтовый ящик")
        self.setMinimumWidth(420)

        a = self.account
        self.name = QLineEdit(a.name)
        self.email = QLineEdit(a.email)
        self.email.editingFinished.connect(self._apply_preset)
        self.username = QLineEdit(a.username)
        self.password = QLineEdit(get_password(db, a.id) if a.id else "")
        self.password.setEchoMode(QLineEdit.Password)
        self.imap_host = QLineEdit(a.imap_host)
        self.imap_port = QSpinBox(); self.imap_port.setRange(1, 65535); self.imap_port.setValue(a.imap_port)
        self.imap_ssl = QCheckBox("SSL/TLS"); self.imap_ssl.setChecked(a.imap_ssl)
        self.smtp_host = QLineEdit(a.smtp_host)
        self.smtp_port = QSpinBox(); self.smtp_port.setRange(1, 65535); self.smtp_port.setValue(a.smtp_port)
        self.smtp_sec = QComboBox(); self.smtp_sec.addItems(["ssl", "starttls", "none"])
        self.smtp_sec.setCurrentText(a.smtp_security)
        self.enabled = QCheckBox("Проверять этот ящик"); self.enabled.setChecked(a.enabled)

        form = QFormLayout()
        form.addRow("Название:", self.name)
        form.addRow("E-mail:", self.email)
        form.addRow("Логин:", self.username)
        form.addRow("Пароль:", self.password)
        imap_row = QHBoxLayout(); imap_row.addWidget(self.imap_host, 1); imap_row.addWidget(self.imap_port); imap_row.addWidget(self.imap_ssl)
        form.addRow("IMAP-сервер:", imap_row)
        smtp_row = QHBoxLayout(); smtp_row.addWidget(self.smtp_host, 1); smtp_row.addWidget(self.smtp_port); smtp_row.addWidget(self.smtp_sec)
        form.addRow("SMTP-сервер:", smtp_row)
        form.addRow("", self.enabled)

        self.test_btn = QPushButton("Проверить подключение")
        self.test_btn.clicked.connect(self._test)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.test_btn)
        lay.addWidget(buttons)

    def _apply_preset(self):
        email = self.email.text().strip()
        if "@" not in email:
            return
        if not self.username.text():
            self.username.setText(email)
        if not self.name.text():
            self.name.setText(email)
        preset = PRESETS.get(email.split("@", 1)[1].lower())
        if preset and not self.imap_host.text():
            ih, ip, sh, sp, sec = preset
            self.imap_host.setText(ih); self.imap_port.setValue(ip); self.imap_ssl.setChecked(True)
            self.smtp_host.setText(sh); self.smtp_port.setValue(sp); self.smtp_sec.setCurrentText(sec)

    def _collect(self) -> Account:
        a = Account(
            id=self.account.id,
            name=self.name.text().strip() or self.email.text().strip(),
            email=self.email.text().strip(),
            username=self.username.text().strip() or self.email.text().strip(),
            imap_host=self.imap_host.text().strip(),
            imap_port=self.imap_port.value(),
            imap_ssl=self.imap_ssl.isChecked(),
            smtp_host=self.smtp_host.text().strip(),
            smtp_port=self.smtp_port.value(),
            smtp_security=self.smtp_sec.currentText(),
            enabled=self.enabled.isChecked(),
        )
        return a

    def _test(self):
        self.test_btn.setEnabled(False)
        self.test_btn.setText("Проверяю…")

        def finish(text, ok):
            self.test_btn.setEnabled(True)
            self.test_btn.setText("Проверить подключение")
            (QMessageBox.information if ok else QMessageBox.warning)(self, "Проверка", text)

        run_task(test_connection, self._collect(), self.password.text(),
                 on_done=lambda t: finish(t, True), on_error=lambda e: finish(e, False))

    def _save(self):
        a = self._collect()
        if not (a.email and a.imap_host and a.smtp_host):
            QMessageBox.warning(self, "Ящик", "Заполните e-mail, IMAP- и SMTP-сервер.")
            return
        self.db.save_account(a)
        set_password(self.db, a.id, self.password.text())
        self.account = a
        self.accept()


class AccountsDialog(QDialog):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Почтовые ящики")
        self.resize(420, 300)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self._edit())
        add = QPushButton("Добавить"); add.clicked.connect(self._add)
        edit = QPushButton("Изменить"); edit.clicked.connect(self._edit)
        delete = QPushButton("Удалить"); delete.clicked.connect(self._delete)
        close = QPushButton("Закрыть"); close.clicked.connect(self.accept)
        btns = QVBoxLayout()
        for b in (add, edit, delete):
            btns.addWidget(b)
        btns.addStretch(1); btns.addWidget(close)
        lay = QHBoxLayout(self)
        lay.addWidget(self.list, 1)
        lay.addLayout(btns)
        self._reload()

    def _reload(self):
        self.list.clear()
        for a in self.db.accounts():
            it = QListWidgetItem(f"{a.name}  <{a.email}>" + ("" if a.enabled else "  (выключен)"))
            it.setData(256, a.id)
            self.list.addItem(it)

    def _current_id(self):
        it = self.list.currentItem()
        return it.data(256) if it else None

    def _add(self):
        if AccountEditDialog(self.db, parent=self).exec():
            self._reload()

    def _edit(self):
        aid = self._current_id()
        if aid and AccountEditDialog(self.db, self.db.account(aid), parent=self).exec():
            self._reload()

    def _delete(self):
        aid = self._current_id()
        if not aid:
            return
        if QMessageBox.question(self, "Удаление", "Удалить ящик и все его письма из программы?\n"
                                "(На сервере письма останутся.)") == QMessageBox.Yes:
            delete_password(self.db, aid)
            self.db.delete_account(aid)
            self._reload()
