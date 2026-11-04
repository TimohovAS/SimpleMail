"""Диалоги управления почтовыми ящиками."""
from __future__ import annotations

from ..i18n import tr

from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
)

from pathlib import Path

from ..db import Account, Database
from ..storage import default_storage_dir
from ..passwords import delete_password, get_password, set_password
from ..protocols import test_connection
from .messages import show_check_results, show_error
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


SECURITY_ITEMS = [("SSL/TLS", "ssl"), ("STARTTLS", "starttls"), ("Без шифрования", "none")]


def security_combo(value: str) -> QComboBox:
    c = QComboBox()
    for label, data in SECURITY_ITEMS:
        c.addItem(tr(label), data)
    set_security(c, value)
    return c


def set_security(combo: QComboBox, value: str) -> None:
    i = combo.findData(value)
    combo.setCurrentIndex(i if i >= 0 else 0)


class PortEdit(QLineEdit):
    """Поле порта: только цифры, без стрелок и прокрутки колесом."""
    def __init__(self, value: int):
        super().__init__(str(value))
        self.setValidator(QIntValidator(1, 65535, self))
        self.setMaxLength(5)
        self.setFixedWidth(70)

    def value(self) -> int:
        try:
            return int(self.text())
        except ValueError:
            return 0

    def setValue(self, v: int) -> None:
        self.setText(str(v))


class AccountEditDialog(QDialog):
    def __init__(self, db: Database, account: Account | None = None, parent=None):
        super().__init__(parent)
        self.db = db
        self.account = account or Account()
        self.setWindowTitle(tr("Почтовый ящик"))
        self.setMinimumWidth(520)

        a = self.account
        self.name = QLineEdit(a.name)
        self.email = QLineEdit(a.email)
        self.email.editingFinished.connect(self._apply_preset)
        self.username = QLineEdit(a.username)
        self.password = QLineEdit(get_password(db, a.id) if a.id else "")
        self.password.setEchoMode(QLineEdit.Password)
        self.pw_warning = QLabel(tr("⚠ В пароле есть нелатинские буквы — проверьте раскладку клавиатуры."))
        self.pw_warning.setStyleSheet("color: #b35c00")
        self.pw_warning.setWordWrap(True)
        self.pw_warning.setVisible(not self.password.text().isascii())
        self.password.textChanged.connect(lambda t: self.pw_warning.setVisible(not t.isascii()))
        self.imap_host = QLineEdit(a.imap_host)
        self.imap_port = PortEdit(a.imap_port)
        self.imap_sec = security_combo(a.imap_security)
        self.imap_sec.setToolTip(tr("SSL/TLS — порт 993; STARTTLS — порт 143 с шифрованием; без шифрования — порт 143"))
        self.imap_sec.currentIndexChanged.connect(
            lambda _: self.imap_port.setValue({"ssl": 993, "starttls": 143, "none": 143}[self.imap_sec.currentData()]))
        self.smtp_host = QLineEdit(a.smtp_host)
        self.smtp_port = PortEdit(a.smtp_port)
        self.smtp_sec = security_combo(a.smtp_security)
        self.smtp_sec.setToolTip(tr("SSL/TLS — порт 465; STARTTLS — порт 587 (или 25); без шифрования — порт 25"))
        self.smtp_sec.currentIndexChanged.connect(
            lambda _: self.smtp_port.setValue({"ssl": 465, "starttls": 587, "none": 25}[self.smtp_sec.currentData()]))
        self.storage = QLineEdit(a.storage_dir or (default_storage_dir(a.email) if a.email else ""))
        self.storage.setToolTip(tr("Здесь хранятся письма (.eml) и сохранённые вложения этого ящика"))
        browse = QPushButton(tr("Обзор…")); browse.clicked.connect(self._browse_storage)
        self._old_storage = a.storage_dir
        self._auto_storage = "" if a.storage_dir else self.storage.text()
        self.enabled = QCheckBox(tr("Проверять этот ящик")); self.enabled.setChecked(a.enabled)

        form = QFormLayout()
        form.addRow(tr("Название:"), self.name)
        form.addRow("E-mail:", self.email)
        form.addRow(tr("Логин:"), self.username)
        form.addRow(tr("Пароль:"), self.password)
        form.addRow("", self.pw_warning)
        imap_row = QHBoxLayout(); imap_row.addWidget(self.imap_host, 1); imap_row.addWidget(self.imap_port); imap_row.addWidget(self.imap_sec)
        form.addRow(tr("IMAP-сервер:"), imap_row)
        smtp_row = QHBoxLayout(); smtp_row.addWidget(self.smtp_host, 1); smtp_row.addWidget(self.smtp_port); smtp_row.addWidget(self.smtp_sec)
        form.addRow(tr("SMTP-сервер:"), smtp_row)
        storage_row = QHBoxLayout(); storage_row.addWidget(self.storage, 1); storage_row.addWidget(browse)
        form.addRow(tr("Папка хранения:"), storage_row)
        form.addRow("", self.enabled)

        self.test_btn = QPushButton(tr("Проверить подключение"))
        self.test_btn.clicked.connect(self._test)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText(tr("Сохранить"))
        buttons.button(QDialogButtonBox.Cancel).setText(tr("Отмена"))
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
        if not self.storage.text() or self.storage.text() == self._auto_storage:
            self.storage.setText(default_storage_dir(email))
            self._auto_storage = self.storage.text()
        preset = PRESETS.get(email.split("@", 1)[1].lower())
        if preset and not self.imap_host.text():
            ih, ip, sh, sp, sec = preset
            self.imap_host.setText(ih); set_security(self.imap_sec, "ssl"); self.imap_port.setValue(ip)
            self.smtp_host.setText(sh); set_security(self.smtp_sec, sec); self.smtp_port.setValue(sp)

    def _browse_storage(self):
        start = self.storage.text() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, tr("Папка для писем и вложений"), start)
        if folder:
            self.storage.setText(str(Path(folder)))

    def _collect(self) -> Account:
        a = Account(
            id=self.account.id,
            name=self.name.text().strip() or self.email.text().strip(),
            email=self.email.text().strip(),
            username=self.username.text().strip() or self.email.text().strip(),
            imap_host=self.imap_host.text().strip(),
            imap_port=self.imap_port.value(),
            imap_security=self.imap_sec.currentData(),
            smtp_host=self.smtp_host.text().strip(),
            smtp_port=self.smtp_port.value(),
            smtp_security=self.smtp_sec.currentData(),
            storage_dir=self.storage.text().strip(),
            signature_id=self.account.signature_id,
            enabled=self.enabled.isChecked(),
        )
        return a

    def _test(self):
        self.test_btn.setEnabled(False)
        self.test_btn.setText(tr("Проверяю…"))

        password = self.password.text()

        def restore():
            self.test_btn.setEnabled(True)
            self.test_btn.setText(tr("Проверить подключение"))

        def done(results):
            restore()
            show_check_results(self, results, password)

        def fail(err):
            restore()
            show_error(self, tr("Проверка подключения"), err)

        run_task(test_connection, self._collect(), password, on_done=done, on_error=fail)

    def _save(self):
        a = self._collect()
        if not (a.email and a.imap_host and a.smtp_host):
            QMessageBox.warning(self, tr("Ящик"), tr("Заполните e-mail, IMAP- и SMTP-сервер."))
            return
        if not (1 <= a.imap_port <= 65535 and 1 <= a.smtp_port <= 65535):
            QMessageBox.warning(self, tr("Ящик"), tr("Укажите порты IMAP и SMTP (число от 1 до 65535)."))
            return
        if not a.storage_dir:
            QMessageBox.warning(self, tr("Ящик"), tr("Укажите папку хранения писем."))
            return
        try:
            Path(a.storage_dir).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, tr("Ящик"), tr("Не удалось создать папку хранения:\n{0}\n\n{1}", a.storage_dir, e.strerror or e))
            return
        old = self._old_storage
        if a.id and old and Path(old).resolve() != Path(a.storage_dir).resolve():
            if QMessageBox.question(self, tr("Папка хранения"),
                                    tr("Перенести уже скачанные письма и вложения в новую папку?\n\nИз: {0}\nВ: {1}", old, a.storage_dir)) == QMessageBox.Yes:
                try:
                    n = self.db.move_storage(a.id, old, a.storage_dir)
                    QMessageBox.information(self, tr("Папка хранения"), tr("Перенесено писем: {0}.", n))
                except OSError as e:
                    QMessageBox.warning(self, tr("Папка хранения"), tr("Перенос прерван: {0}", e))
        self.db.save_account(a)
        set_password(self.db, a.id, self.password.text())
        self.account = a
        self.accept()


class AccountsDialog(QDialog):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle(tr("Почтовые ящики"))
        self.resize(420, 300)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self._edit())
        add = QPushButton(tr("Добавить")); add.clicked.connect(self._add)
        edit = QPushButton(tr("Изменить")); edit.clicked.connect(self._edit)
        delete = QPushButton(tr("Удалить")); delete.clicked.connect(self._delete)
        close = QPushButton(tr("Закрыть")); close.clicked.connect(self.accept)
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
            it = QListWidgetItem(f"{a.name}  <{a.email}>" + ("" if a.enabled else tr("  (выключен)")))
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
        if QMessageBox.question(self, tr("Удаление"), tr("Удалить ящик и все его письма из программы?\n(На сервере письма останутся.)")) == QMessageBox.Yes:
            delete_password(self.db, aid)
            self.db.delete_account(aid)
            self._reload()
