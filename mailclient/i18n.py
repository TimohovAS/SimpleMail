"""Переводы интерфейса. Содержимое писем и пути хранения не переводятся."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

LANGUAGES = {"ru": "Русский", "sr_Latn": "Srpski (latinica)"}
_language = "ru"


def language() -> str:
    return _language


def set_language(code: str) -> str:
    global _language
    _language = code if code in LANGUAGES else "ru"
    return _language


@lru_cache(maxsize=2)
def catalog(code: str) -> dict[str, str]:
    if code == "ru":
        return {}
    path = Path(__file__).with_name("locales") / f"{code}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def tr(text: str, *args, **kwargs) -> str:
    translated = catalog(_language).get(text, text)
    return translated.format(*args, **kwargs) if args or kwargs else translated


def spam_reason(text: str) -> str:
    """Старые причины спама в базе остаются языково независимыми от интерфейса."""
    if _language == "ru":
        return text
    match = re.fullmatch(r"(белый|чёрный) список: (from|subject|body) содержит «(.*)»", text, re.DOTALL)
    if match:
        kind, field, pattern = match.groups()
        kind = tr("белый список") if kind == "белый" else tr("чёрный список")
        field = tr({"from": "отправитель", "subject": "тема", "body": "текст письма"}[field])
        return tr("{0}: {1} содержит «{2}»", kind, field, pattern)
    if text.startswith("байес: "):
        return tr("байес: ") + text[len("байес: "):]
    return tr(text)


def install_qt_translations(app):
    """Qt-кнопки и диалоги: русский каталог Qt или сербская латиница."""
    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
    locale = QLocale("sr_Latn_RS" if _language == "sr_Latn" else "ru_RU")
    QLocale.setDefault(locale)
    # Выбор файлов оставляем системным: диски, сеть и быстрый доступ Windows.
    for previous in getattr(app, "_mail_translators", []):
        app.removeTranslator(previous)
    translators = []
    if _language == "ru":
        translator = QTranslator(app)
        if translator.load(locale, "qtbase", "_", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
            app.installTranslator(translator)
            translators.append(translator)
    else:
        from .locales.qt_sr_Latn import QT_TRANSLATIONS

        class SerbianTranslator(QTranslator):
            def isEmpty(self):
                return False

            def translate(self, context, sourceText, disambiguation=None, n=-1):
                return QT_TRANSLATIONS.get(sourceText, "")

        translator = SerbianTranslator(app)
        app.installTranslator(translator)
        translators.append(translator)
    # installTranslator() не владеет переводчиками; храним Python-ссылки.
    app._mail_translators = translators
