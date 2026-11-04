"""Окно написания письма."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from ..db import Database
from ..protocols import build_message, send_message
from .worker import run_task


class ComposeDialog(QDialog):
    def __init__(self, db: Database, account_id: int | None = None, to: str = "", subject: str = "",
                 body: str = "", in_reply_to: str = "", parent=None):
        super().__init__(parent)
        self.db = db
        self.in_reply_to = in_reply_to
        self.attachments: list[tuple[str, bytes]] = []
        self.setWindowTitle("Новое письмо")
        self.resize(640, 520)

        self.account = QComboBox()
        for a in db.accounts():
            self.account.addItem(f"{a.name} <{a.email}>", a.id)
            if a.id == account_id:
                self.account.setCurrentIndex(self.account.count() - 1)
        self.to = QLineEdit(to)
        self.cc = QLineEdit()
        self.subject = QLineEdit(subject)
        self.body = QPlainTextEdit(body)
        self.att_label = QLabel("")

        form = QFormLayout()
        form.addRow("От:", self.account)
        form.addRow("Кому:", self.to)
        form.addRow("Копия:", self.cc)
        form.addRow("Тема:", self.subject)

        self.send_btn = QPushButton("Отправить"); self.send_btn.clicked.connect(self._send)
        attach = QPushButton("Вложить файл…"); attach.clicked.connect(self._attach)
        cancel = QPushButton("Отмена"); cancel.clicked.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(attach); bottom.addWidget(self.att_label, 1)
        bottom.addWidget(cancel); bottom.addWidget(self.send_btn)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.body, 1)
        lay.addLayout(bottom)
        if to:
            self.body.setFocus()
            self.body.moveCursor(self.body.textCursor().MoveOperation.Start)

    def _attach(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Вложить файлы")
        for f in files:
            self.attachments.append((Path(f).name, Path(f).read_bytes()))
        self.att_label.setText(", ".join(n for n, _ in self.attachments))

    def _send(self):
        aid = self.account.currentData()
        if aid is None:
            QMessageBox.warning(self, "Отправка", "Сначала добавьте почтовый ящик.")
            return
        if not self.to.text().strip():
            QMessageBox.warning(self, "Отправка", "Укажите получателя.")
            return
        acc = self.db.account(aid)
        em = build_message(acc, self.to.text().strip(), self.subject.text(), self.body.toPlainText(),
                           cc=self.cc.text().strip(), in_reply_to=self.in_reply_to, attachments=self.attachments)
        self.send_btn.setEnabled(False)
        self.send_btn.setText("Отправляю…")

        def fail(err):
            self.send_btn.setEnabled(True)
            self.send_btn.setText("Отправить")
            QMessageBox.warning(self, "Ошибка отправки", err)

        run_task(send_message, self.db, acc, em, on_done=lambda _: self.accept(), on_error=fail)
