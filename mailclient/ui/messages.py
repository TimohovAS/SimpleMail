"""Окна с сообщениями об ошибках: понятный текст + технические детали под «Подробнее»."""
from __future__ import annotations

from ..i18n import tr

import html

from PySide6.QtWidgets import QMessageBox

from ..errors import ErrorInfo


def show_error(parent, title: str, err: ErrorInfo) -> None:
    box = QMessageBox(QMessageBox.Warning, title, err.summary, QMessageBox.Ok, parent)
    if err.hint:
        box.setInformativeText(err.hint)
    box.setDetailedText(err.details)
    box.exec()


def show_check_results(parent, results, password: str = "") -> None:
    """results: list[protocols.CheckResult]"""
    ok = all(r.ok for r in results)
    rows = []
    for r in results:
        if r.ok:
            rows.append(tr("<p><span style='color:#1a7f37'>✔</span> <b>{0}</b>: работает.</p>", r.name))
            if getattr(r, "note", ""):
                rows.append(f"<p>⚠ {html.escape(r.note)}</p>")
        else:
            hint = f"<br>{html.escape(r.error.hint)}" if r.error.hint else ""
            summary = r.error.summary[:1].lower() + r.error.summary[1:]
            rows.append(f"<p><span style='color:#cf222e'>✖</span> <b>{r.name}</b>: "
                        f"{html.escape(summary)}.{hint}</p>")
    if not ok and password and not password.isascii():
        rows.append(tr("<p>В пароле есть русские буквы — возможно, он набран не в той раскладке.</p>"))
    box = QMessageBox(QMessageBox.Information if ok else QMessageBox.Warning, tr("Проверка подключения"),
                      tr("Подключение работает.") if ok else tr("Есть проблемы с подключением."),
                      QMessageBox.Ok, parent)
    box.setInformativeText("".join(rows))
    box.setStyleSheet("QLabel#qt_msgbox_informativelabel { min-width: 420px; }")
    details = "\n".join(f"{r.name}: {r.error.details}" for r in results if not r.ok)
    if details:
        box.setDetailedText(details)
    box.exec()
