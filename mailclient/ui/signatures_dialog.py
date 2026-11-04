"""Окно «Подписи»: список подписей и редактор с оформлением.

Подписи общие для всех ящиков. В настройках ящика выбирается подпись по умолчанию,
а в окне письма её можно сменить на любую другую.
"""
from __future__ import annotations

from ..i18n import tr

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from ..db import Database, Signature
from .editor import RichEditor
from .icons import icon


class SignaturesDialog(QDialog):
    def __init__(self, db: Database, select_id: int | None = None, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle(tr("Подписи"))
        self.resize(860, 480)
        self._items: list[Signature] = [replace(s) for s in db.signatures()]
        self._original = {s.id: (s.name, s.html) for s in self._items}
        self._deleted: set[int] = set()
        self._row = -1
        self.selected_id: int | None = select_id  # какая подпись была выбрана при закрытии

        # ---- список ----
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._row_changed)
        add = QPushButton(icon("plus"), tr("Добавить")); add.clicked.connect(self._add)
        self.del_btn = QPushButton(icon("trash-2"), tr("Удалить")); self.del_btn.clicked.connect(self._delete)
        lb = QHBoxLayout(); lb.addWidget(add); lb.addWidget(self.del_btn)
        left = QWidget(); ll = QVBoxLayout(left); ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.list, 1); ll.addLayout(lb)

        # ---- редактор ----
        self.name = QLineEdit(); self.name.setPlaceholderText(tr("Название подписи, например «Рабочая»"))
        self.name.textEdited.connect(self._name_edited)
        self.editor = RichEditor(allow_images=False)
        self.used = QLabel(); self.used.setStyleSheet("color:#6b7280"); self.used.setWordWrap(True)
        self.empty = QLabel(tr("Подписей пока нет. Нажмите «Добавить»."))
        self.empty.setAlignment(Qt.AlignCenter); self.empty.setStyleSheet("color:#6b7280")
        self.right = QWidget(); rl = QVBoxLayout(self.right); rl.setContentsMargins(0, 0, 0, 0)
        nl = QHBoxLayout(); nl.addWidget(QLabel(tr("Название:"))); nl.addWidget(self.name, 1)
        rl.addLayout(nl); rl.addWidget(self.editor, 1); rl.addWidget(self.used)

        split = QSplitter(); split.setChildrenCollapsible(False)
        split.addWidget(left); split.addWidget(self.right); split.addWidget(self.empty)
        split.setSizes([220, 640, 0])
        self.split = split

        hint = QLabel(tr("Подпись добавляется в конец новых писем и ответов после строки «-- ». Какая подпись ставится по умолчанию, выбирается в настройках почтового ящика."))
        hint.setWordWrap(True)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText(tr("Сохранить"))
        bb.button(QDialogButtonBox.Cancel).setText(tr("Отмена"))
        bb.accepted.connect(self._save); bb.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(hint); lay.addWidget(split, 1); lay.addWidget(bb)

        self._reload_list()
        row = next((i for i, s in enumerate(self._items) if s.id == select_id), 0)
        self.list.setCurrentRow(row if self._items else -1)
        self._update_state()

    # ---------- список ----------
    def _reload_list(self):
        self.list.blockSignals(True)
        self.list.clear()
        for s in self._items:
            self.list.addItem(QListWidgetItem(icon("signature", "#374151"), s.name or tr("(без названия)")))
        self.list.blockSignals(False)

    def _update_state(self):
        has = bool(self._items)
        self.right.setVisible(has)
        self.empty.setVisible(not has)
        self.del_btn.setEnabled(has)

    def _flush(self):
        """Текст из редактора — в текущую подпись."""
        if 0 <= self._row < len(self._items):
            s = self._items[self._row]
            s.name = self.name.text().strip()
            if self.editor.edit.document().isModified():
                s.html = self.editor.to_html() if self.editor.to_plain().strip() else ""
                self.editor.edit.document().setModified(False)

    def _row_changed(self, row: int):
        self._flush()
        self._row = row
        if not (0 <= row < len(self._items)):
            self.name.clear(); self.editor.set_html(""); self.used.clear()
            return
        s = self._items[row]
        self.name.setText(s.name)
        self.editor.set_html(s.html)
        self.editor.edit.document().setModified(False)
        users = [a.name or a.email for a in self.db.accounts() if s.id is not None and a.signature_id == s.id]
        self.used.setText(tr("По умолчанию в ящиках: ") + ", ".join(users) if users
                          else tr("Пока не выбрана по умолчанию ни в одном ящике."))

    def _name_edited(self, text: str):
        it = self.list.item(self._row)
        if it:
            it.setText(text.strip() or tr("(без названия)"))

    def _add(self):
        self._flush()
        n = len(self._items) + 1
        names = {s.name for s in self._items}
        name = tr("Новая подпись") if tr("Новая подпись") not in names else tr("Новая подпись {0}", n)
        self._items.append(Signature(name=name))
        self.list.addItem(QListWidgetItem(icon("signature", "#374151"), name))
        self._update_state()
        self.list.setCurrentRow(len(self._items) - 1)
        self.name.setFocus(); self.name.selectAll()

    def _delete(self):
        row = self._row
        if not (0 <= row < len(self._items)):
            return
        s = self._items[row]
        if QMessageBox.question(self, tr("Подписи"), tr("Удалить подпись «{0}»?\nЯщики, где она стоит по умолчанию, останутся без подписи.", s.name or tr('без названия'))) != QMessageBox.Yes:
            return
        if s.id is not None:
            self._deleted.add(s.id)
        self._row = -1  # не сохранять редактор в удалённую подпись
        del self._items[row]
        self._reload_list()
        self._update_state()
        if self._items:
            self.list.setCurrentRow(min(row, len(self._items) - 1))
        else:
            self._row_changed(-1)

    # ---------- сохранение ----------
    def _changed(self) -> bool:
        self._flush()
        if self._deleted:
            return True
        return any(s.id is None or self._original.get(s.id) != (s.name, s.html) for s in self._items)

    def _save(self):
        self._flush()
        for i, s in enumerate(self._items):
            if not s.name:
                s.name = tr("Подпись {0}", i + 1)
        for sid in self._deleted:
            self.db.delete_signature(sid)
        for s in self._items:
            if s.id is None or self._original.get(s.id) != (s.name, s.html):
                self.db.save_signature(s)
        cur = self._items[self._row] if 0 <= self._row < len(self._items) else None
        self.selected_id = cur.id if cur else None
        self.accept()

    def reject(self):
        if self._changed() and QMessageBox.question(
                self, tr("Подписи"), tr("Закрыть без сохранения? Изменения в подписях будут потеряны.")) != QMessageBox.Yes:
            return
        super().reject()
