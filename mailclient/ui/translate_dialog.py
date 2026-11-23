"""Настройки перевода писем: сервис нейросети, модель, ключ API и чат для перевода в браузере."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from .. import translate
from ..i18n import language, tr
from .worker import run_task

HINTS = {
    "gemini": "Ключ бесплатно выдаётся в Google AI Studio. Подписка Google One на ключ не распространяется. "
              "На бесплатном тарифе Google может использовать тексты писем для улучшения своих сервисов.",
    "claude": "Ключ создаётся в консоли Anthropic, оплата по факту использования (нужна предоплата). "
              "Подписка Claude на ключ не распространяется. Тексты из API для обучения не используются.",
    "openai": "Ключ создаётся на platform.openai.com, оплата по факту использования (нужна предоплата). "
              "Подписка ChatGPT на ключ не распространяется.",
    "custom": "Свой сервер с OpenAI-совместимым API, например Ollama: http://адрес-сервера:11434/v1. "
              "Ключ обычно не нужен. Тексты писем не покидают вашу сеть.",
}

TEST_TEXT = "Dobar dan, u prilogu vam šaljem ponudu za radove. Molim vas da potvrdite prijem."


class TranslateDialog(QDialog):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle(tr("Перевод писем"))
        self.setMinimumWidth(520)
        self._edits: dict[str, translate.Settings] = {}  # несохранённые правки по сервисам
        self._current = None
        self._closed = False

        self.provider = QComboBox()
        for key, p in translate.PROVIDERS.items():
            self.provider.addItem(tr(p.title), key)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.NoInsert)
        self.refresh_models = QPushButton(tr("Список моделей"))
        self.refresh_models.setToolTip(tr("Загрузить модели, доступные с этим ключом"))
        self.refresh_models.clicked.connect(self._load_models)
        model_row = QHBoxLayout(); model_row.setContentsMargins(0, 0, 0, 0)
        model_row.addWidget(self.model, 1); model_row.addWidget(self.refresh_models)
        model_box = QWidget(); model_box.setLayout(model_row)

        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText(tr("Вставьте ключ API"))
        self.show_key = QCheckBox(tr("Показать"))
        self.show_key.toggled.connect(lambda on: self.key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        key_row = QHBoxLayout(); key_row.setContentsMargins(0, 0, 0, 0)
        key_row.addWidget(self.key, 1); key_row.addWidget(self.show_key)
        self.key_box = QWidget(); self.key_box.setLayout(key_row)
        self.key_link = QLabel()
        self.key_link.setOpenExternalLinks(True)
        self.key_link.setTextInteractionFlags(Qt.TextBrowserInteraction)

        self.url = QLineEdit()
        self.url.setPlaceholderText(translate.DEFAULT_CUSTOM_URL)

        self.hint = QLabel(); self.hint.setWordWrap(True); self.hint.setProperty("tone", "muted")

        self.test_btn = QPushButton(tr("Проверить"))
        self.test_btn.clicked.connect(self._test)
        self.test_result = QLabel(); self.test_result.setWordWrap(True)
        self.test_result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        test_row = QHBoxLayout(); test_row.setContentsMargins(0, 0, 0, 0)
        test_row.addWidget(self.test_btn, 0, Qt.AlignTop); test_row.addWidget(self.test_result, 1)
        test_box = QWidget(); test_box.setLayout(test_row)

        self.chat = QComboBox()
        for key, (title, _, _) in translate.CHATS.items():
            self.chat.addItem(title, key)
        chat = db.get_setting("translate_chat", "chatgpt")
        self.chat.setCurrentIndex(max(0, self.chat.findData(chat)))

        self.form = QFormLayout()
        self.form.addRow(tr("Сервис:"), self.provider)
        self.form.addRow(tr("Модель:"), model_box)
        self.form.addRow(tr("Адрес сервера:"), self.url)
        self.form.addRow(tr("Ключ API:"), self.key_box)
        self.form.addRow("", self.key_link)
        self.form.addRow("", self.hint)
        self.form.addRow("", test_box)
        self.form.addRow(tr("Чат в браузере:"), self.chat)
        browser_hint = QLabel(tr("«Перевести в браузере» копирует письмо с просьбой о переводе и открывает "
                                 "сайт чата: работает по вашей обычной подписке, ключ не нужен."))
        browser_hint.setWordWrap(True); browser_hint.setProperty("tone", "muted")
        self.form.addRow("", browser_hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(self.form)
        lay.addWidget(buttons)

        current = translate.load_settings(db)
        self._edits[current.provider] = current
        self.provider.setCurrentIndex(max(0, self.provider.findData(current.provider)))
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self._show(current)

    # ---------- поля ----------
    def _settings(self) -> translate.Settings:
        key = self.provider.currentData()
        return translate.Settings(provider=key, model=self.model.currentText().strip(),
                                  api_key=self.key.text().strip(), url=self.url.text().strip())

    def _show(self, s: translate.Settings):
        self._current = s.provider
        info = s.info
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItems(list(info.models))
        self.model.setEditText(s.model_name)
        self.model.blockSignals(False)
        self.key.setText(s.api_key)
        self.url.setText(s.url)
        self.form.setRowVisible(self.url, info.needs_url)
        self.key.setPlaceholderText(tr("Вставьте ключ API") if info.needs_key else tr("Не обязательно"))
        if info.key_url:
            self.key_link.setText(tr("Получить ключ: <a href=\"{0}\">{0}</a>", info.key_url))
        self.form.setRowVisible(self.key_link, bool(info.key_url))
        self.hint.setText(tr(HINTS.get(s.provider, "")))
        self.test_result.clear()

    def _provider_changed(self):
        if self._current is not None:
            previous = self._settings()
            previous.provider = self._current
            self._edits[self._current] = previous
        key = self.provider.currentData()
        s = self._edits.get(key) or translate.load_settings(self.db, key)
        self._show(s)

    # ---------- проверка и список моделей ----------
    def _test(self):
        s = self._settings()
        if not s.configured:
            self.test_result.setText(tr("Укажите адрес сервера.") if s.info.needs_url else tr("Укажите ключ API."))
            return
        self.test_btn.setEnabled(False)
        self.test_result.setText(tr("Проверка…"))

        def done(result):
            if self._closed:
                return
            self.test_btn.setEnabled(True)
            translated = result.get(TEST_TEXT, "")
            self.test_result.setText(tr("Работает: {0}", translated) if translated
                                     else tr("Сервис ответил, но без перевода."))

        def failed(err):
            if not self._closed:
                self.test_btn.setEnabled(True)
                self.test_result.setText(err.short())

        target = translate.TARGET_NAMES.get(language(), "Russian")
        run_task(lambda: translate.translate_texts(s, [TEST_TEXT], target)[0], on_done=done, on_error=failed)

    def _load_models(self):
        s = self._settings()
        if not s.configured:
            self.test_result.setText(tr("Укажите адрес сервера.") if s.info.needs_url else tr("Укажите ключ API."))
            return
        self.refresh_models.setEnabled(False)

        def done(names):
            if self._closed:
                return
            self.refresh_models.setEnabled(True)
            text = self.model.currentText()
            self.model.clear()
            self.model.addItems(names or list(s.info.models))
            self.model.setEditText(text)
            self.model.showPopup()

        def failed(err):
            if not self._closed:
                self.refresh_models.setEnabled(True)
                self.test_result.setText(err.short())

        run_task(translate.list_models, s, on_done=done, on_error=failed)

    # ---------- сохранение ----------
    def _save(self):
        s = self._settings()
        if not translate.save_settings(self.db, s) and s.api_key:
            QMessageBox.warning(self, tr("Перевод писем"),
                                tr("Защищённое хранилище Windows недоступно: ключ API сохранён в базе программы открытым текстом."))
        self.db.set_setting("translate_chat", self.chat.currentData())
        self.accept()

    def done(self, result):
        self._closed = True
        super().done(result)
