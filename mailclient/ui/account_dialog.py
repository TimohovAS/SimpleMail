"""Диалоги управления почтовыми ящиками."""
from __future__ import annotations

from ..i18n import tr

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QAbstractItemView,
)

from pathlib import Path

from ..db import Account, Database
from ..storage import default_storage_dir
from ..passwords import delete_password, get_password, set_password
from ..protocols import test_connection
from ..errors import GOOGLE_APP_PASSWORDS
from ..oauth import default_client
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
GOOGLE_DOMAINS = {"gmail.com", "googlemail.com"}
for alias, original in {"googlemail.com": "gmail.com", "yandex.com": "yandex.ru", "yandex.by": "yandex.ru",
                        "live.com": "outlook.com", "msn.com": "outlook.com"}.items():
    PRESETS[alias] = PRESETS[original]


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
        self.password = QLineEdit(get_password(db, a.id) if a.id and a.auth_method == "password" else "")
        self.password.setEchoMode(QLineEdit.Password)
        self.pw_warning = QLabel(tr("⚠ В пароле есть нелатинские буквы — проверьте раскладку клавиатуры."))
        self.pw_warning.setProperty("tone", "warning")
        self.pw_warning.setWordWrap(True)
        self.pw_warning.setVisible(not self.password.text().isascii())
        self.password.textChanged.connect(lambda t: self.pw_warning.setVisible(not t.isascii()))
        self.imap_host = QLineEdit(a.imap_host)
        self.imap_port = PortEdit(a.imap_port)
        self.imap_sec = security_combo(a.imap_security)
        self.imap_sec.setToolTip(tr("SSL/TLS — порт 993; STARTTLS — порт 143 с шифрованием; без шифрования — порт 143"))
        self._imap_security = a.imap_security
        self.imap_sec.currentIndexChanged.connect(lambda _: self._change_security("imap"))
        self.smtp_host = QLineEdit(a.smtp_host)
        self.smtp_port = PortEdit(a.smtp_port)
        self.smtp_sec = security_combo(a.smtp_security)
        self.smtp_sec.setToolTip(tr("SSL/TLS — порт 465; STARTTLS — порт 587 (или 25); без шифрования — порт 25"))
        self._smtp_security = a.smtp_security
        self.smtp_sec.currentIndexChanged.connect(lambda _: self._change_security("smtp"))
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
        self.auth_method = QComboBox()
        self.auth_method.addItem(tr("Пароль"), "password")
        self.auth_method.addItem(tr("Google (вход в браузере)"), "google")
        self.auth_method.addItem("Microsoft OAuth2", "microsoft")
        self.auth_method.setCurrentIndex(max(0, self.auth_method.findData(a.auth_method)))
        form.addRow(tr("Способ входа:"), self.auth_method)
        form.addRow(tr("Пароль:"), self.password)
        form.addRow("", self.pw_warning)
        self.gmail_hint = QLabel(tr("Gmail не принимает обычный пароль. Выберите вход через Google "
                                    "или <a href='{0}'>создайте пароль приложения</a> и введите его выше.",
                                    GOOGLE_APP_PASSWORDS))
        self.gmail_hint.setOpenExternalLinks(True)
        self.gmail_hint.setWordWrap(True)
        self.gmail_hint.setProperty("tone", "warning")
        form.addRow("", self.gmail_hint)
        self._google_rejected = False  # подсказка — только после отказа Google в пароле
        self.password.textEdited.connect(lambda _: self._reset_google_hint())
        # Client ID для каждого способа отдельно: при переключении поля не смешиваются.
        self._clients = {}
        for provider in ("microsoft", "google"):
            default = default_client(provider)
            self._clients[provider] = {
                "client_id": db.get_setting(f"{provider}_client_id", "") or default["client_id"],
                "client_secret": db.get_setting(f"{provider}_client_secret", "") or default["client_secret"]}
        if a.auth_method in self._clients and a.oauth_client_id:
            self._clients[a.auth_method] = {"client_id": a.oauth_client_id, "client_secret": a.oauth_client_secret}
        self._provider = a.auth_method if a.auth_method in self._clients else None
        current = self._clients.get(self._provider, {"client_id": "", "client_secret": ""})
        self.oauth_client_id = QLineEdit(current["client_id"])
        self.oauth_client_id.setPlaceholderText("Client ID")
        self.oauth_client_secret = QLineEdit(current["client_secret"])
        self.oauth_client_secret.setEchoMode(QLineEdit.Password)
        self.oauth_client_secret.setPlaceholderText("Client secret")
        self.oauth_tenant = QLineEdit(a.oauth_tenant or "common")
        self.oauth_tenant.setToolTip(tr("common — личные и рабочие аккаунты; либо tenant ID вашей организации"))
        self.oauth_login = QPushButton()
        self.oauth_login.clicked.connect(self._oauth_sign_in)
        self.oauth_logout = QPushButton()
        self.oauth_logout.clicked.connect(self._oauth_sign_out)
        form.addRow("Client ID:", self.oauth_client_id)
        form.addRow("Client secret:", self.oauth_client_secret)
        form.addRow("Tenant:", self.oauth_tenant)
        oauth_buttons = QHBoxLayout()
        oauth_buttons.addWidget(self.oauth_login)
        oauth_buttons.addWidget(self.oauth_logout)
        form.addRow("", oauth_buttons)
        self._form = form
        self.auth_method.currentIndexChanged.connect(self._update_auth_fields)
        self.email.textChanged.connect(self._update_auth_fields)
        imap_row = QHBoxLayout(); imap_row.addWidget(self.imap_host, 1); imap_row.addWidget(self.imap_port); imap_row.addWidget(self.imap_sec)
        form.addRow(tr("IMAP-сервер:"), imap_row)
        smtp_row = QHBoxLayout(); smtp_row.addWidget(self.smtp_host, 1); smtp_row.addWidget(self.smtp_port); smtp_row.addWidget(self.smtp_sec)
        form.addRow(tr("SMTP-сервер:"), smtp_row)
        storage_row = QHBoxLayout(); storage_row.addWidget(self.storage, 1); storage_row.addWidget(browse)
        form.addRow(tr("Папка хранения:"), storage_row)
        form.addRow("", self.enabled)

        self.test_btn = QPushButton(tr("Проверить подключение"))
        self.test_btn.clicked.connect(self._test)
        self.detect_btn = QPushButton(tr("Определить настройки автоматически"))
        self.detect_btn.setToolTip(tr("Найти серверы, порты и шифрование по адресу e-mail. "
                                      "Выбирается самый защищённый работающий вариант"))
        self.detect_btn.clicked.connect(self._detect)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText(tr("Сохранить"))
        buttons.button(QDialogButtonBox.Cancel).setText(tr("Отмена"))
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        test_row = QHBoxLayout()
        test_row.addWidget(self.detect_btn)
        test_row.addWidget(self.test_btn)
        lay.addLayout(test_row)
        lay.addWidget(buttons)
        self._update_auth_fields()

    def _reset_google_hint(self):
        if self._google_rejected:
            self._google_rejected = False
            self._update_auth_fields()

    def _provider_name(self):
        return "Google" if self.auth_method.currentData() == "google" else "Microsoft"

    def _change_security(self, kind):
        ports = {"ssl": 993, "starttls": 143, "none": 143} if kind == "imap" else {"ssl": 465, "starttls": 587, "none": 25}
        previous = getattr(self, f"_{kind}_security")
        current = getattr(self, f"{kind}_sec").currentData()
        port = getattr(self, f"{kind}_port")
        if port.value() == ports.get(previous):
            port.setValue(ports[current])
        setattr(self, f"_{kind}_security", current)

    def _update_auth_fields(self, *_):
        method = self.auth_method.currentData()
        if method != self._provider:
            # Запоминаем введённое для прежнего способа и подставляем сохранённое для нового.
            if self._provider in self._clients:
                self._clients[self._provider] = {"client_id": self.oauth_client_id.text().strip(),
                                                 "client_secret": self.oauth_client_secret.text().strip()}
            if method in self._clients:
                self.oauth_client_id.setText(self._clients[method]["client_id"])
                self.oauth_client_secret.setText(self._clients[method]["client_secret"])
            self._provider = method
        oauth = method in ("microsoft", "google")
        builtin = default_client(method)["client_id"] if oauth else ""
        # Client ID встроен в сборку: полей настройки не показываем — только «Войти».
        show_client = oauth and not (builtin and self.oauth_client_id.text().strip() == builtin)
        self.password.setEnabled(not oauth)
        self.pw_warning.setVisible(not oauth and not self.password.text().isascii())
        domain = self.email.text().strip().rpartition("@")[2].lower()
        self.gmail_hint.setVisible(method == "password" and domain in GOOGLE_DOMAINS and self._google_rejected)
        for widget, visible in ((self.oauth_client_id, show_client),
                                (self.oauth_client_secret, show_client and method == "google"),
                                (self.oauth_tenant, method == "microsoft")):
            widget.setVisible(visible)
            self._form.labelForField(widget).setVisible(visible)
        for widget in (self.oauth_login, self.oauth_logout):
            widget.setVisible(oauth)
        self.oauth_login.setText(tr("Войти в {0}…", self._provider_name()))
        self.oauth_logout.setText(tr("Выйти из {0}", self._provider_name()))

    def _oauth_sign_in(self):
        from ..oauth import validate_config
        from .oauth_dialog import OAuthDialog
        account = self._collect()
        try:
            validate_config(account)
            if not account.email or "@" not in account.email:
                raise ValueError(tr("Сначала укажите адрес e-mail."))
        except Exception as error:
            QMessageBox.warning(self, tr("Вход в {0}", self._provider_name()), str(error))
            return
        dialog = OAuthDialog(self.db, account, self)
        if dialog.exec() and not dialog.persistent:
            QMessageBox.information(self, tr("Вход в {0}", self._provider_name()),
                tr("Вход выполнен. Защищённое хранение недоступно: после перезапуска потребуется повторный вход."))

    def _oauth_sign_out(self):
        from ..oauth import logout
        account = self._collect()
        def sign_out():
            from ..protocols import close_pool
            logout(self.db, account)
            close_pool()
        def done(_):
            parent = self.parent()
            while parent is not None:
                if hasattr(parent, "mail_watch"):
                    for watcher in parent.mail_watch.watchers:
                        if watcher.account.id == account.id:
                            watcher.stop()
                    break
                parent = parent.parent()
            QMessageBox.information(self, tr("Вход в {0}", name), tr("Выполнен выход из {0}", name))
        name = self._provider_name()
        run_task(sign_out,
                 on_done=done,
                 on_error=lambda error: show_error(self, tr("Вход в {0}", name), error))

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
            domain = email.split("@", 1)[1].lower()
            if not self.account.id and domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
                self.auth_method.setCurrentIndex(self.auth_method.findData("microsoft"))
            # Gmail: сразу вход через Google, если Client ID уже известен; иначе — подсказка про пароль приложения.
            if not self.account.id and domain in GOOGLE_DOMAINS and self._clients["google"]["client_id"]:
                self.auth_method.setCurrentIndex(self.auth_method.findData("google"))

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
            auth_method=self.auth_method.currentData(),
            oauth_client_id=self.oauth_client_id.text().strip(),
            oauth_tenant=self.oauth_tenant.text().strip() or "common",
            oauth_client_secret=self.oauth_client_secret.text().strip() if self.auth_method.currentData() == "google" else "",
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
            if any(not r.ok and GOOGLE_APP_PASSWORDS in (r.error.hint or "") for r in results):
                self._google_rejected = True
                self._update_auth_fields()
            restore()
            show_check_results(self, results, password)

        def fail(err):
            restore()
            show_error(self, tr("Проверка подключения"), err)

        account = self._collect()
        if account.auth_method in ("microsoft", "google"):
            def check_oauth():
                from ..oauth import access_token
                return test_connection(account, access_token(self.db, account))
            run_task(check_oauth, on_done=done, on_error=fail)
        else:
            run_task(test_connection, account, password, on_done=done, on_error=fail)

    def _detect(self):
        from ..autoconfig import detect
        self._apply_preset()
        acc = self._collect()
        if "@" not in acc.email:
            QMessageBox.warning(self, tr("Ящик"), tr("Сначала укажите адрес e-mail."))
            self.email.setFocus()
            return
        self.detect_btn.setEnabled(False)
        self.detect_btn.setText(tr("Ищу настройки…"))

        def restore():
            self.detect_btn.setEnabled(True)
            self.detect_btn.setText(tr("Определить настройки автоматически"))

        def done(result):
            restore()
            self.apply_detected(result)

        def fail(err):
            restore()
            show_error(self, tr("Автоопределение настроек"), err)

        run_task(detect, acc, self.password.text() if acc.auth_method == "password" else "", on_done=done, on_error=fail)

    def apply_detected(self, result):
        """Заполнить поля найденными настройками и показать итог."""
        rows = []
        labels = dict((data, tr(label)) for label, data in SECURITY_ITEMS)
        for found, host, port, sec, name, login in (
                (result.imap, self.imap_host, self.imap_port, self.imap_sec, tr("Приём (IMAP)"), result.imap_login),
                (result.smtp, self.smtp_host, self.smtp_port, self.smtp_sec, tr("Отправка (SMTP)"), result.smtp_login)):
            if not found:
                continue
            host.setText(found.host)
            set_security(sec, found.security)  # меняет порт на стандартный — ставим найденный после
            port.setValue(found.port)
            line = f"{name}: {found.host}:{found.port}, {labels.get(found.security, found.security)}"
            if result.login_checked and found.security != "none":
                line += " — " + (tr("вход выполнен") if login is None else login.summary.lower())
            rows.append(line)
        rows += result.notes
        failed = not (result.imap and result.smtp) or result.imap_login or result.smtp_login
        (QMessageBox.warning if failed else QMessageBox.information)(
            self, tr("Автоопределение настроек"), "\n\n".join(rows) or tr("Настройки не найдены."))

    def _save(self):
        a = self._collect()
        try:
            from ..protocols import _sender_address
            _sender_address(a)
            if a.auth_method in ("microsoft", "google"):
                from ..oauth import validate_config
                validate_config(a)
        except Exception as error:
            QMessageBox.warning(self, tr("Почтовый ящик"), str(error))
            return
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
        if a.auth_method in ("microsoft", "google"):
            if self.account.id and self.account.auth_method == "password":
                delete_password(self.db, a.id)
            self.db.set_setting(f"{a.auth_method}_client_id", a.oauth_client_id)
            if a.auth_method == "google":
                self.db.set_setting("google_client_secret", a.oauth_client_secret)
        elif not set_password(self.db, a.id, self.password.text()):
            QMessageBox.warning(self, tr("Пароль"), tr(
                "Хранилище паролей Windows недоступно, поэтому пароль сохранён в файле базы "
                "открытым текстом. Не передавайте файл mail.db другим людям."))
        self.account = a
        self.accept()


class AccountsDialog(QDialog):
    order_changed = Signal()

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle(tr("Почтовые ящики"))
        self.resize(420, 300)
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.setDragDropOverwriteMode(False)
        self.list.setToolTip(tr("Перетащите ящики мышью, чтобы изменить порядок"))
        self.list.itemDoubleClicked.connect(lambda _: self._edit())
        add = QPushButton(tr("Добавить")); add.clicked.connect(self._add)
        edit = QPushButton(tr("Изменить")); edit.clicked.connect(self._edit)
        delete = QPushButton(tr("Удалить")); delete.clicked.connect(self._delete)
        self.up_btn = QPushButton("↑ " + tr("Вверх"))
        self.up_btn.setToolTip(tr("Поднять ящик в списке (Ctrl+↑)"))
        self.up_btn.setShortcut("Ctrl+Up")
        self.up_btn.clicked.connect(lambda: self._move(-1))
        self.down_btn = QPushButton("↓ " + tr("Вниз"))
        self.down_btn.setToolTip(tr("Опустить ящик в списке (Ctrl+↓)"))
        self.down_btn.setShortcut("Ctrl+Down")
        self.down_btn.clicked.connect(lambda: self._move(1))
        close = QPushButton(tr("Закрыть")); close.clicked.connect(self.accept)
        btns = QVBoxLayout()
        for b in (add, edit, delete):
            btns.addWidget(b)
        btns.addSpacing(12)
        btns.addWidget(self.up_btn)
        btns.addWidget(self.down_btn)
        btns.addStretch(1); btns.addWidget(close)
        lay = QHBoxLayout(self)
        lay.addWidget(self.list, 1)
        lay.addLayout(btns)
        self.list.currentRowChanged.connect(self._update_move_buttons)
        self.list.model().rowsMoved.connect(self._save_order)
        self._reload()

    def _reload(self):
        selected_id = self._current_id()
        previous_row = self.list.currentRow()
        self.list.clear()
        for a in self.db.accounts():
            it = QListWidgetItem(f"{a.name}  <{a.email}>" + ("" if a.enabled else tr("  (выключен)")))
            it.setData(256, a.id)
            self.list.addItem(it)
        row = next((i for i in range(self.list.count())
                    if self.list.item(i).data(Qt.UserRole) == selected_id),
                   min(max(previous_row, 0), self.list.count() - 1))
        self.list.setCurrentRow(row)
        self._update_move_buttons()

    def _update_move_buttons(self, *_):
        row = self.list.currentRow()
        self.up_btn.setEnabled(row > 0)
        self.down_btn.setEnabled(0 <= row < self.list.count() - 1)

    def _save_order(self, *_):
        self.db.set_account_order([self.list.item(i).data(Qt.UserRole)
                                   for i in range(self.list.count())])
        self._update_move_buttons()
        self.order_changed.emit()

    def _move(self, offset):
        row = self.list.currentRow()
        destination = row + offset
        if row < 0 or not 0 <= destination < self.list.count():
            return
        item = self.list.takeItem(row)
        self.list.insertItem(destination, item)
        self.list.setCurrentRow(destination)
        self._save_order()

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
        box = QMessageBox(QMessageBox.Question, tr("Удаление"),
                          tr("Удалить ящик и все его письма из программы?\n(На сервере письма останутся.)"),
                          QMessageBox.Yes | QMessageBox.No, self)
        box.setDefaultButton(QMessageBox.No)
        remove_files = QCheckBox(tr("Удалить скачанные файлы писем (.eml) с диска"))
        remove_files.setToolTip(tr("Другие файлы и вручную сохранённые вложения останутся в папке."))
        box.setCheckBox(remove_files)
        if box.exec() != QMessageBox.Yes:
            return
        delete_files = remove_files.isChecked()
        self.setEnabled(False)

        def remove():
            try:
                account = self.db.account(aid)
                if account and account.auth_method in ("microsoft", "google"):
                    from ..oauth import logout
                    logout(self.db, account)
                failed = self.db.delete_account(aid, delete_files=delete_files)
                delete_password(self.db, aid, forget=True)
                return failed
            finally:
                self.db.close()

        def done(failed):
            self.setEnabled(True)
            self._reload()
            self.order_changed.emit()
            if failed:
                QMessageBox.warning(self, tr("Удаление"), tr("Не удалось удалить файлы:\n{0}", "\n".join(failed)))

        def error(info):
            self.setEnabled(True)
            show_error(self, tr("Удаление"), info)

        run_task(remove, on_done=done, on_error=error)
