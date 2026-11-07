"""Диалоги управления почтовыми ящиками."""
from __future__ import annotations

from ..i18n import tr

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QAbstractItemView, QWidget,
)

from pathlib import Path

from ..db import Account, Database
from .storage_picker import StoragePicker
from ..passwords import delete_password, get_password, set_password
from ..protocols import test_connection
from ..errors import GOOGLE_APP_PASSWORDS
from ..mailops import TRASH_DAYS_MAX, set_trash_days, trash_days
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
        self._closed = False
        self._busy = False
        self._operation = 0
        self._auto_identity = self.account.email
        self._auto_servers = None
        self._preset_domain = None
        self._auth_chosen = bool(self.account.id)
        self._force_signin = False
        self.setWindowTitle(tr("Почтовый ящик"))
        self.setMinimumWidth(580)

        a = self.account
        self.name = QLineEdit(a.name)
        self.email = QLineEdit(a.email)
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
        self.storage_picker = StoragePicker(db, a.email, a.storage_dir)
        self.storage = self.storage_picker.path_edit
        self._old_storage = a.storage_dir
        self.enabled = QCheckBox(tr("Проверять этот ящик")); self.enabled.setChecked(a.enabled)
        # Раньше это было только в меню «Файл → Скачать всю историю», и пункт не находили.
        self._history = db.get_setting(f"history:{a.id}") if a.id else None
        self.full_history = QCheckBox(tr("Скачивать все письма с сервера, включая старые"))
        self.full_history.setToolTip(tr(
            "Без этой отметки при первой проверке скачиваются только последние письма в каждой папке. "
            "Загрузка старых писем идёт в фоне, её можно приостановить: "
            "Файл → Приостановить загрузку истории."))
        self.full_history.setChecked(self._history in ("running", "complete"))
        downloaded, total = db.history_progress(a.id) if a.id else (0, 0)
        if downloaded < total:
            self.full_history.setText(tr("Скачивать все письма с сервера, включая старые (скачано {0} из {1})",
                                         downloaded, total))

        self.trash_days = QSpinBox()
        self.trash_days.setRange(0, TRASH_DAYS_MAX)
        self.trash_days.setSpecialValueText(tr("никогда"))
        self.trash_days.setSuffix(tr(" дн."))
        self.trash_days.setValue(trash_days(db, a.id) if a.id else 0)
        self.trash_days.setFocusPolicy(Qt.StrongFocus)  # колесо мыши не меняет срок случайно
        self.trash_days.installEventFilter(self)
        self.trash_days.setToolTip(tr(
            "Письма, пролежавшие в корзине указанное число дней, удаляются окончательно — "
            "и в программе, и на сервере. Срок считается с момента, когда письмо попало в корзину. "
            "0 — не удалять. Gmail сам удаляет письма из корзины через 30 дней."))

        self.content = QWidget()
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.addRow("E-mail:", self.email)
        self.advanced = QWidget()
        advanced_form = QFormLayout(self.advanced)
        advanced_form.setContentsMargins(0, 0, 0, 0)
        advanced_form.addRow(tr("Название:"), self.name)
        advanced_form.addRow(tr("Логин:"), self.username)
        self.auth_method = QComboBox()
        self.auth_method.addItem(tr("Пароль"), "password")
        self.auth_method.addItem(tr("Google (вход в браузере)"), "google")
        self.auth_method.addItem("Microsoft OAuth2", "microsoft")
        self.auth_method.setCurrentIndex(max(0, self.auth_method.findData(a.auth_method)))
        form.addRow(tr("Способ входа:"), self.auth_method)
        form.addRow(tr("Пароль:"), self.password)
        form.addRow("", self.pw_warning)
        # Одна строка без переноса: перенос в QFormLayout давал пустое место над и под подсказкой.
        self.gmail_hint = QLabel(tr("Нужен <a href='{0}'>пароль приложения Google</a>, обычный пароль не подойдёт",
                                    GOOGLE_APP_PASSWORDS))
        self.gmail_hint.setToolTip(GOOGLE_APP_PASSWORDS)
        self.gmail_hint.setOpenExternalLinks(True)
        self.gmail_hint.setProperty("tone", "warning")
        self.gmail_hint.setWordWrap(True)
        form.addRow("", self.gmail_hint)
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
        # Ключи встроены в программу: поля скрыты, их открывает ссылка «Свой Client ID…».
        self._custom_client = False
        self.custom_client_link = QLabel("<a href='#custom'>{0}</a>".format(tr("Свой Client ID…")))
        self.custom_client_link.setToolTip(tr("Войти через своё приложение Google Cloud или Microsoft Entra "
                                              "вместо встроенного в программу"))
        self.custom_client_link.linkActivated.connect(self._show_custom_client)
        advanced_form.addRow("Client ID:", self.oauth_client_id)
        advanced_form.addRow("Client secret:", self.oauth_client_secret)
        advanced_form.addRow("Tenant:", self.oauth_tenant)
        oauth_buttons = QHBoxLayout()
        oauth_buttons.addWidget(self.oauth_login)
        oauth_buttons.addWidget(self.oauth_logout)
        form.addRow("", oauth_buttons)
        # Gmail: вход по паролю приложения остаётся — ссылка рядом с «Войти в Google…».
        self.app_password_link = QLabel("<a href='#password'>{0}</a>".format(tr("Войти по паролю приложения")))
        self.app_password_link.setToolTip(tr("Выбрать способ входа «Пароль» и ввести пароль приложения Google"))
        self.app_password_link.linkActivated.connect(self._use_app_password)
        self.oauth_links = QWidget()
        links = QHBoxLayout(self.oauth_links)
        links.setContentsMargins(0, 0, 0, 0)
        links.addWidget(self.app_password_link)
        links.addStretch(1)
        links.addWidget(self.custom_client_link)
        form.addRow("", self.oauth_links)
        self._form = form
        self._advanced_form = advanced_form
        self.auth_method.currentIndexChanged.connect(self._update_auth_fields)
        self.auth_method.activated.connect(self._choose_auth)
        imap_row = QHBoxLayout(); imap_row.addWidget(self.imap_host, 1); imap_row.addWidget(self.imap_port); imap_row.addWidget(self.imap_sec)
        advanced_form.addRow(tr("IMAP-сервер:"), imap_row)
        smtp_row = QHBoxLayout(); smtp_row.addWidget(self.smtp_host, 1); smtp_row.addWidget(self.smtp_port); smtp_row.addWidget(self.smtp_sec)
        advanced_form.addRow(tr("SMTP-сервер:"), smtp_row)
        form.addRow(self.storage_picker)
        advanced_form.addRow("", self.enabled)
        form.addRow("", self.full_history)
        trash_row = QHBoxLayout(); trash_row.addWidget(self.trash_days); trash_row.addStretch(1)
        advanced_form.addRow(tr("Очищать корзину через:"), trash_row)

        self.test_btn = QPushButton(tr("Проверить подключение"))
        self.test_btn.clicked.connect(self._test)
        self.detect_btn = QPushButton(tr("Определить настройки автоматически"))
        self.detect_btn.setToolTip(tr("Найти серверы, порты и шифрование по адресу e-mail. "
                                      "Выбирается самый защищённый работающий вариант"))
        self.detect_btn.clicked.connect(self._detect)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save_btn = buttons.button(QDialogButtonBox.Save)
        self.save_btn.setDefault(True)
        buttons.button(QDialogButtonBox.Cancel).setText(tr("Отмена"))
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        content_layout.addLayout(form)
        self.advanced_toggle = QPushButton(tr("Дополнительные настройки"))
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        content_layout.addWidget(self.advanced_toggle)
        content_layout.addWidget(self.advanced)
        self.advanced.hide()
        test_row = QHBoxLayout()
        test_row.addWidget(self.detect_btn)
        test_row.addWidget(self.test_btn)
        advanced_form.addRow(test_row)
        lay.addWidget(self.content)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        lay.addWidget(self.status)
        lay.addWidget(buttons)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(button is self.save_btn)
        self.email.textChanged.connect(self._apply_preset)
        self.oauth_client_id.textEdited.connect(self._update_auth_fields)
        self.oauth_client_secret.textEdited.connect(self._update_auth_fields)
        for field in (self.username, self.password, self.imap_host, self.imap_port, self.smtp_host, self.smtp_port):
            field.textEdited.connect(self._update_auth_fields)
        self.imap_sec.currentIndexChanged.connect(self._update_auth_fields)
        self.smtp_sec.currentIndexChanged.connect(self._update_auth_fields)
        self._update_auth_fields()
        self._original_connection = self._connection_key(self._collect())
        self._original_password = self.password.text()
        self.email.setFocus()

    def _toggle_advanced(self, checked):
        self.advanced.setVisible(checked)
        self.adjustSize()

    def _choose_auth(self, *_):
        self._auth_chosen = True
        if not self.account.id and self.auth_method.currentData() == "google":
            self._set_servers(PRESETS["gmail.com"])
        self._update_auth_fields()

    def reject(self):
        self._closed = True
        self._operation += 1
        super().reject()

    @staticmethod
    def _connection_key(a):
        return (a.email, a.username, a.auth_method, a.imap_host, a.imap_port, a.imap_security,
                a.smtp_host, a.smtp_port, a.smtp_security, a.oauth_client_id,
                a.oauth_client_secret, a.oauth_tenant)

    def _set_busy(self, busy, message=""):
        self._busy = busy
        self.content.setEnabled(not busy)
        self.save_btn.setEnabled(not busy)
        if message:
            self.status.setText(message)

    def _has_session(self):
        from ..oauth import has_session
        return has_session(self.db, self._collect())

    def _password_value(self):
        value = self.password.text()
        if (self.auth_method.currentData() == "password"
                and self.email.text().strip().rpartition("@")[2].lower() in GOOGLE_DOMAINS):
            # Google displays app passwords in four groups separated by spaces.
            return "".join(value.split())
        return value

    def eventFilter(self, obj, event):
        if obj is self.trash_days and event.type() == QEvent.Wheel and not obj.hasFocus():
            event.ignore()
            return True
        return super().eventFilter(obj, event)

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
        # И встроенные, и однажды сохранённые ключи не требуют повторного ввода.
        show_client = oauth and self._custom_client
        self.password.setEnabled(not oauth)
        self.password.setVisible(not oauth)  # при входе через браузер пароль не нужен — строку не показываем
        self._form.labelForField(self.password).setVisible(not oauth)
        self.pw_warning.setVisible(not oauth and not self.password.text().isascii())
        domain = self.email.text().strip().rpartition("@")[2].lower()
        gmail = domain in GOOGLE_DOMAINS
        # Ссылка на создание пароля приложения видна всегда, когда Gmail входит по паролю.
        self.gmail_hint.setVisible(method == "password" and gmail)
        self.app_password_link.setVisible(method == "google" and gmail)
        for widget, visible in ((self.oauth_client_id, show_client),
                                (self.oauth_client_secret, show_client and method == "google"),
                                (self.oauth_tenant, method == "microsoft")):
            widget.setVisible(visible)
            self._advanced_form.labelForField(widget).setVisible(visible)
        self.oauth_login.setVisible(oauth and bool(self.account.id))
        self.oauth_logout.setVisible(oauth and bool(self.account.id))
        has_session = oauth and self._has_session()
        self.oauth_logout.setEnabled(has_session)
        self.custom_client_link.setVisible(oauth and not show_client)
        self.oauth_links.setVisible(not (self.custom_client_link.isHidden() and self.app_password_link.isHidden()))
        self.oauth_login.setText(tr("Войти в {0}…", self._provider_name()))
        self.oauth_logout.setText(tr("Выйти из {0}", self._provider_name()))
        self.save_btn.setText(tr("Сохранить") if self.account.id else
                             tr("Войти в Google и добавить") if method == "google" else
                             tr("Подключить и добавить"))
        if not self._busy:
            if method == "google":
                from ..oauth import google_client_valid
                if not google_client_valid(self.oauth_client_id.text().strip(), self.oauth_client_secret.text().strip()):
                    message = tr("В этой копии программы вход Google не настроен. Используйте пароль приложения или укажите свой Client ID в дополнительных настройках.")
                elif has_session:
                    message = tr("Вход Google выполнен. При добавлении проверим приём и отправку почты.")
                else:
                    message = tr("Откроется браузер Google. После входа проверим приём и отправку и добавим ящик автоматически.")
            else:
                message = tr("Укажите адрес почты и место хранения. Настройки известных сервисов подставятся автоматически.")
            self.status.setText(message)

    def _use_app_password(self, *_):
        self._auth_chosen = True
        self.auth_method.setCurrentIndex(self.auth_method.findData("password"))
        self.password.setFocus()

    def _show_custom_client(self, *_):
        self._custom_client = True
        self.advanced_toggle.setChecked(True)
        self._update_auth_fields()
        self.oauth_client_id.setFocus()
        self.oauth_client_id.selectAll()

    def _oauth_sign_in(self, *_):
        self._apply_preset()
        if self._sign_in():
            self._check_connection(save=False)

    def _sign_in(self):
        from ..oauth import validate_config
        from .oauth_dialog import OAuthDialog
        account = self._collect()
        try:
            validate_config(account)
            if not account.email or "@" not in account.email:
                raise ValueError(tr("Сначала укажите адрес e-mail."))
        except Exception as error:
            QMessageBox.warning(self, tr("Вход в {0}", self._provider_name()), str(error))
            self.advanced_toggle.setChecked(True)
            self._show_custom_client()
            return False
        dialog = OAuthDialog(self.db, account, self)
        if not dialog.exec():
            self.status.setText(tr("Вход отменён. Ящик не добавлен; можно повторить попытку."))
            return False
        if not dialog.persistent:
            QMessageBox.information(self, tr("Вход в {0}", self._provider_name()),
                tr("Вход выполнен. Защищённое хранение недоступно: после перезапуска потребуется повторный вход."))
        self._force_signin = False
        self._update_auth_fields()
        return True

    def _oauth_sign_out(self):
        from ..oauth import logout
        account = self._collect()
        def sign_out():
            from ..protocols import close_pool
            logout(self.db, account)
            close_pool()
        def done(_):
            if self._closed:
                return
            self._set_busy(False)
            self._update_auth_fields()
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
        self._set_busy(True)
        def failed(error):
            if not self._closed:
                self._set_busy(False)
                show_error(self, tr("Вход в {0}", name), error)
        run_task(sign_out,
                 on_done=done,
                 on_error=failed)

    def _server_values(self):
        return (self.imap_host.text(), self.imap_port.value(), self.smtp_host.text(),
                self.smtp_port.value(), self.smtp_sec.currentData(), self.imap_sec.currentData())

    def _set_servers(self, preset):
        ih, ip, sh, sp, sec = preset
        self.imap_host.setText(ih); set_security(self.imap_sec, "ssl"); self.imap_port.setValue(ip)
        self.smtp_host.setText(sh); set_security(self.smtp_sec, sec); self.smtp_port.setValue(sp)
        self._auto_servers = self._server_values()

    def _apply_preset(self, *_):
        email = self.email.text().strip()
        if not self.username.text() or (not self.account.id and self.username.text() == self._auto_identity):
            self.username.setText(email)
        if not self.name.text() or (not self.account.id and self.name.text() == self._auto_identity):
            self.name.setText(email)
        self._auto_identity = email
        self.storage_picker.set_email(email)
        domain = email.rpartition("@")[2].lower() if "@" in email else ""
        preset = PRESETS.get(domain)
        if not self.account.id and domain != self._preset_domain:
            automatic = not self.imap_host.text() or self._server_values() == self._auto_servers
            if automatic:
                if preset:
                    self._set_servers(preset)
                elif self._auto_servers:
                    self.imap_host.clear(); self.smtp_host.clear(); self._auto_servers = None
            if not self._auth_chosen:
                method = "password"
                if domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
                    method = "microsoft"
                elif domain in GOOGLE_DOMAINS:
                    from ..oauth import google_client_valid
                    if google_client_valid(**self._clients["google"]):
                        method = "google"
                self.auth_method.setCurrentIndex(self.auth_method.findData(method))
            self._preset_domain = domain
        self._update_auth_fields()

    def _browse_storage(self):
        self.storage_picker.browse()

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
            storage_dir=self.storage_picker.path(),
            signature_id=self.account.signature_id,
            enabled=self.enabled.isChecked(),
            auth_method=self.auth_method.currentData(),
            oauth_client_id=self.oauth_client_id.text().strip(),
            oauth_tenant=self.oauth_tenant.text().strip() or "common",
            oauth_client_secret=self.oauth_client_secret.text().strip() if self.auth_method.currentData() == "google" else "",
        )
        return a

    def _test(self):
        if self._busy:
            return
        self._apply_preset()
        account = self._collect()
        if account.auth_method in ("microsoft", "google") and (self._force_signin or not self._has_session()):
            if not self._sign_in():
                return
        self._check_connection(save=False)

    def _check_connection(self, save):
        account = self._collect()
        password = self._password_value()
        self._operation += 1
        operation = self._operation
        self._set_busy(True, tr("Проверяем приём (IMAP) и отправку (SMTP)…"))

        def active():
            return not self._closed and operation == self._operation

        def done(results):
            if not active():
                return
            self._set_busy(False)
            ok = len(results) == 2 and all(r.ok for r in results)
            if not ok:
                if account.auth_method in ("google", "microsoft"):
                    # A cached token can be rejected before its local expiry.
                    self.oauth_login.show()
                self.status.setText(tr("Не удалось подключить ящик. Исправьте ошибку и повторите попытку; введённые данные сохранены в этом окне."))
                show_check_results(self, results, password)
            elif save:
                self._persist(account)
            else:
                self.status.setText(tr("Приём и отправка работают. Можно сохранить ящик."))

        def fail(err):
            if not active():
                return
            self._set_busy(False, err.summary)
            if (account.auth_method in ("google", "microsoft")
                    and ("GoogleAuthError:" in err.details or "OAuthError:" in err.details)):
                self._force_signin = True
            show_error(self, tr("Проверка подключения"), err)

        if account.auth_method in ("microsoft", "google"):
            def check_oauth():
                from ..oauth import access_token
                return test_connection(account, access_token(self.db, account))
            run_task(check_oauth, on_done=done, on_error=fail)
        else:
            run_task(test_connection, account, password, on_done=done, on_error=fail)

    def _detect(self):
        if self._busy:
            return
        from ..autoconfig import detect
        self._apply_preset()
        acc = self._collect()
        if "@" not in acc.email:
            QMessageBox.warning(self, tr("Ящик"), tr("Сначала укажите адрес e-mail."))
            self.email.setFocus()
            return
        self._operation += 1
        operation = self._operation
        self._set_busy(True)
        self.detect_btn.setText(tr("Ищу настройки…"))

        def restore():
            self._set_busy(False)
            self.detect_btn.setText(tr("Определить настройки автоматически"))

        def done(result):
            if self._closed or operation != self._operation:
                return
            restore()
            self.apply_detected(result)

        def fail(err):
            if self._closed or operation != self._operation:
                return
            restore()
            show_error(self, tr("Автоопределение настроек"), err)

        run_task(detect, acc, self._password_value() if acc.auth_method == "password" else "", on_done=done, on_error=fail)

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
        if self._busy or self._closed:
            return
        self._apply_preset()
        a = self._collect()
        try:
            from ..protocols import _sender_address
            _sender_address(a)
            if a.auth_method in ("microsoft", "google"):
                from ..oauth import validate_config
                validate_config(a)
        except Exception as error:
            QMessageBox.warning(self, tr("Почтовый ящик"), str(error))
            self.advanced_toggle.setChecked(True)
            if a.auth_method in ("microsoft", "google"):
                self._show_custom_client()
            return
        if not (a.email and a.imap_host and a.smtp_host):
            QMessageBox.warning(self, tr("Ящик"), tr("Заполните e-mail, IMAP- и SMTP-сервер."))
            self.advanced_toggle.setChecked(True)
            return
        if not (1 <= a.imap_port <= 65535 and 1 <= a.smtp_port <= 65535):
            QMessageBox.warning(self, tr("Ящик"), tr("Укажите порты IMAP и SMTP (число от 1 до 65535)."))
            return
        if not a.storage_dir:
            QMessageBox.warning(self, tr("Ящик"), tr("Укажите папку хранения писем."))
            return
        if not Path(a.storage_dir).is_absolute():
            QMessageBox.warning(self, tr("Папка хранения"), tr("Укажите полный путь к папке, например D:\\Mail."))
            self.storage.setFocus()
            return
        for other in self.db.accounts():
            if other.id == a.id:
                continue
            if other.email.casefold() == a.email.casefold():
                QMessageBox.warning(self, tr("Ящик"), tr("Этот адрес уже добавлен. Измените настройки существующего ящика."))
                return
            if other.storage_dir and Path(other.storage_dir).resolve() == Path(a.storage_dir).resolve():
                QMessageBox.warning(self, tr("Папка хранения"), tr("Эта папка уже используется другим ящиком. Выберите отдельную подпапку."))
                return
        try:
            Path(a.storage_dir).mkdir(parents=True, exist_ok=True)
            from tempfile import TemporaryFile
            with TemporaryFile(dir=a.storage_dir):
                pass
        except OSError as e:
            QMessageBox.warning(self, tr("Ящик"), tr("Не удалось создать папку хранения:\n{0}\n\n{1}", a.storage_dir, e.strerror or e))
            return
        needs_check = (not a.id or self._connection_key(a) != self._original_connection
                       or self.password.text() != self._original_password)
        if needs_check:
            if a.auth_method == "password" and not self._password_value():
                QMessageBox.warning(self, tr("Пароль"), tr("Введите пароль приложения или выберите вход через браузер."))
                self.password.setFocus()
                return
            if a.auth_method in ("microsoft", "google") and (self._force_signin or not self._has_session()):
                if not self._sign_in():
                    return
            self._check_connection(save=True)
        else:
            # Смена папки, имени и автопроверки не требует доступа к сети.
            self._persist(a)

    def _persist(self, a):
        if self._closed:
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
                    return
        self.db.save_account(a)
        set_trash_days(self.db, a.id, self.trash_days.value())
        wanted = self.full_history.isChecked()
        if wanted != (self._history in ("running", "complete")):
            # «running» — следующая проверка докачает старые письма; «declined» — больше не спрашивать.
            self.db.set_setting(f"history:{a.id}", "running" if wanted else "declined")
        if a.auth_method in ("microsoft", "google"):
            if self.account.id and self.account.auth_method == "password":
                delete_password(self.db, a.id)
            self.db.set_setting(f"{a.auth_method}_client_id", a.oauth_client_id)
            if a.auth_method == "google":
                self.db.set_setting("google_client_secret", a.oauth_client_secret)
        elif not set_password(self.db, a.id, self._password_value()):
            QMessageBox.warning(self, tr("Пароль"), tr(
                "Хранилище паролей Windows недоступно, поэтому пароль сохранён в файле базы "
                "открытым текстом. Не передавайте файл mail.db другим людям."))
        self.account = a
        self.storage_picker.remember_parent()
        self._closed = True
        self.accept()


class AccountsDialog(QDialog):
    order_changed = Signal()
    account_saved = Signal()

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
            self.account_saved.emit()

    def _edit(self):
        aid = self._current_id()
        if aid and AccountEditDialog(self.db, self.db.account(aid), parent=self).exec():
            self._reload()
            self.account_saved.emit()

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
