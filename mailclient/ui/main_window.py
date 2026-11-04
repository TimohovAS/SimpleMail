"""Главное окно."""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QTextBrowser, QToolBar, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget, QHBoxLayout, QSpinBox, QInputDialog,
)

from ..db import Database
from ..mailparse import attachments
from ..protocols import set_server_flag, sync_account
from ..spam import SpamFilter
from ..storage import attachments_dir, default_storage_dir
from .account_dialog import AccountsDialog
from .compose_dialog import ComposeDialog
from .spam_dialog import SpamDialog
from .messages import show_error
from .worker import run_task

VIEWS = [("inbox", "Входящие"), ("spam", "Спам"), ("sent", "Отправленные"), ("trash", "Корзина")]
ROLE_ID = Qt.UserRole


class MainWindow(QMainWindow):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.spam = SpamFilter(db)
        self.current = (None, "inbox")       # (account_id, view)
        self.syncing = 0
        self.setWindowTitle("SimpleMail")
        self.resize(1200, 760)

        # ---- левая панель: ящики и папки ----
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemSelectionChanged.connect(self._on_folder_selected)

        # ---- список писем ----
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по отправителю и теме…")
        self.search.textChanged.connect(self._apply_search)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["От / Кому", "Тема", "Дата"])
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Interactive)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setColumnWidth(0, 260)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.itemSelectionChanged.connect(self._on_message_selected)
        self.table.doubleClicked.connect(lambda _: self._reply())

        list_box = QWidget()
        lb = QVBoxLayout(list_box); lb.setContentsMargins(0, 0, 0, 0)
        lb.addWidget(self.search); lb.addWidget(self.table)

        # ---- просмотр письма ----
        self.header = QLabel()
        self.header.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.header.setWordWrap(True)
        self.save_att_btn = QPushButton("Сохранить вложения…")
        self.save_att_btn.clicked.connect(self._save_attachments)
        self.save_att_btn.hide()
        self.viewer = QTextBrowser()
        self.viewer.setOpenExternalLinks(True)
        view_box = QWidget()
        vb = QVBoxLayout(view_box); vb.setContentsMargins(4, 4, 4, 4)
        hdr = QHBoxLayout(); hdr.addWidget(self.header, 1); hdr.addWidget(self.save_att_btn, 0, Qt.AlignTop)
        vb.addLayout(hdr); vb.addWidget(self.viewer, 1)

        right = QSplitter(Qt.Vertical)
        right.addWidget(list_box); right.addWidget(view_box)
        right.setSizes([300, 460])
        main = QSplitter(Qt.Horizontal)
        main.addWidget(self.tree); main.addWidget(right)
        main.setSizes([230, 970])
        self.setCentralWidget(main)

        self._build_toolbar()
        self.status = self.statusBar()

        # ---- автопроверка ----
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_mail)
        self._restart_timer()

        # ящики из старой версии: назначаем папку хранения по умолчанию
        for acc in self.db.accounts():
            if not acc.storage_dir:
                acc.storage_dir = default_storage_dir(acc.email)
                self.db.save_account(acc)

        self.reload_tree()
        if not self.db.accounts():
            QTimer.singleShot(300, self._manage_accounts)

    # ================= toolbar =================
    def _build_toolbar(self):
        tb = QToolBar("Основное")
        tb.setMovable(False)
        self.addToolBar(tb)

        def act(text, slot, shortcut=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            tb.addAction(a)
            return a

        act("Проверить почту", self.check_mail_manual, "F5")
        act("Написать", self._compose, "Ctrl+N")
        act("Ответить", self._reply, "Ctrl+R")
        act("Переслать", self._forward, "Ctrl+Shift+F")
        tb.addSeparator()
        act("Спам", lambda: self._mark_spam(True), "Ctrl+J")
        act("Не спам", lambda: self._mark_spam(False), "Ctrl+Shift+J")
        act("Удалить", self._delete, "Del")
        tb.addSeparator()
        act("Ящики…", self._manage_accounts)
        act("Спам-фильтр…", self._spam_settings)
        act("Автопроверка…", self._interval_settings)

    def _restart_timer(self):
        minutes = int(self.db.get_setting("check_interval_min", "10"))
        self.timer.stop()
        if minutes > 0:
            self.timer.start(minutes * 60_000)

    # ================= дерево ящиков =================
    def reload_tree(self):
        keep = self.current
        self.tree.blockSignals(True)
        self.tree.clear()
        select_item = None
        bold = QFont(); bold.setBold(True)
        for acc in self.db.accounts():
            top = QTreeWidgetItem([acc.name + ("" if acc.enabled else " (выкл.)")])
            top.setData(0, ROLE_ID, (acc.id, None))
            self.tree.addTopLevelItem(top)
            for view, title in VIEWS:
                unread = self.db.unread_count(acc.id, view) if view in ("inbox", "spam") else 0
                it = QTreeWidgetItem([f"{title} ({unread})" if unread else title])
                if unread:
                    it.setFont(0, bold)
                it.setData(0, ROLE_ID, (acc.id, view))
                top.addChild(it)
                if (acc.id, view) == keep or (select_item is None and keep[0] is None and view == "inbox"):
                    select_item = it
            top.setExpanded(True)
        self.tree.blockSignals(False)
        if select_item:
            self.tree.setCurrentItem(select_item)
        else:
            self.table.setRowCount(0)

    def _on_folder_selected(self):
        it = self.tree.currentItem()
        if not it:
            return
        acc_id, view = it.data(0, ROLE_ID)
        if view is None:
            it.setSelected(False)
            self.tree.setCurrentItem(it.child(0))
            return
        self.current = (acc_id, view)
        self.reload_messages()

    # ================= список писем =================
    def reload_messages(self, keep_selection: bool = True):
        acc_id, view = self.current
        selected = self._selected_id() if keep_selection else None
        rows = self.db.list_messages(acc_id, view) if acc_id else []
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        bold = QFont(); bold.setBold(True)
        select_row = -1
        for i, r in enumerate(rows):
            who = r["recipients"] if view == "sent" else r["sender"]
            date = datetime.fromtimestamp(r["date_ts"]).strftime("%d.%m.%Y %H:%M") if r["date_ts"] else ""
            for j, text in enumerate((who or "", r["subject"] or "(без темы)", date)):
                item = QTableWidgetItem(text)
                item.setData(ROLE_ID, r["id"])
                if not r["seen"] and view in ("inbox", "spam"):
                    item.setFont(bold)
                if r["spam_reason"]:
                    item.setToolTip("Спам-фильтр: " + r["spam_reason"])
                self.table.setItem(i, j, item)
            if r["id"] == selected:
                select_row = i
        self.table.blockSignals(False)
        self._apply_search()
        if select_row >= 0:
            self.table.selectRow(select_row)
        else:
            self._show_message(None)

    def _apply_search(self):
        q = self.search.text().strip().lower()
        for i in range(self.table.rowCount()):
            text = " ".join(self.table.item(i, j).text().lower() for j in (0, 1))
            self.table.setRowHidden(i, bool(q) and q not in text)

    def _selected_ids(self) -> list[int]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, 0).data(ROLE_ID) for r in rows]

    def _selected_id(self) -> int | None:
        ids = self._selected_ids()
        return ids[0] if len(ids) == 1 else None

    def _on_message_selected(self):
        self._show_message(self._selected_id())

    # ================= просмотр =================
    def _show_message(self, mid: int | None):
        self._shown = None
        if mid is None:
            self.header.setText("")
            self.viewer.clear()
            self.save_att_btn.hide()
            return
        m = self.db.message(mid)
        if not m:
            return
        self._shown = m
        date = datetime.fromtimestamp(m.date_ts).strftime("%d.%m.%Y %H:%M") if m.date_ts else ""
        e = html.escape
        lines = [f"<b>{e(m.subject or '(без темы)')}</b>",
                 f"От: {e(m.sender)}", f"Кому: {e(m.recipients)}", f"Дата: {date}"]
        if m.spam_reason:
            lines.append(f"<span style='color:gray'>Спам-фильтр: {e(m.spam_reason)}</span>")
        atts = attachments(m.raw) if m.raw else []
        if atts:
            lines.append("Вложения: " + e(", ".join(n for n, _ in atts)))
        self.header.setText("<br>".join(lines))
        self.save_att_btn.setVisible(bool(atts))
        if m.body_html:
            # QTextBrowser не загружает картинки из интернета — это защищает от «маячков» спамеров.
            self.viewer.setHtml(m.body_html)
        else:
            self.viewer.setPlainText(m.body_text)

        if not m.seen and m.folder == "inbox":
            self.db.update_message(m.id, seen=1)
            run_task(set_server_flag, self.db, m, "\\Seen", on_error=self._status_error)
            for j in range(3):
                row = self.table.currentRow()
                if row >= 0:
                    self.table.item(row, j).setFont(QFont())
            self._refresh_counts()

    def _save_attachments(self):
        m = getattr(self, "_shown", None)
        if not m:
            return
        acc = self.db.account(m.account_id)
        start = str(attachments_dir(acc.storage_dir)) if acc and acc.storage_dir else ""
        folder = QFileDialog.getExistingDirectory(self, "Куда сохранить вложения", start)
        if not folder:
            return
        for name, data in attachments(m.raw):
            target = Path(folder) / Path(name).name
            n = 1
            while target.exists():
                target = Path(folder) / f"{Path(name).stem} ({n}){Path(name).suffix}"
                n += 1
            target.write_bytes(data)
        self.status.showMessage(f"Вложения сохранены в {folder}", 5000)

    def _refresh_counts(self):
        """Обновить счётчики непрочитанных без перестройки дерева."""
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for k in range(top.childCount()):
                it = top.child(k)
                acc_id, view = it.data(0, ROLE_ID)
                title = dict(VIEWS)[view]
                if view in ("inbox", "spam"):
                    n = self.db.unread_count(acc_id, view)
                    it.setText(0, f"{title} ({n})" if n else title)
                    f = QFont(); f.setBold(bool(n)); it.setFont(0, f)

    # ================= действия =================
    def check_mail_manual(self):
        self.check_mail(manual=True)

    def check_mail(self, manual: bool = False):
        self._manual_check = manual
        if self.syncing:
            return
        accounts = self.db.accounts(only_enabled=True)
        if not accounts:
            self.status.showMessage("Нет ящиков для проверки", 5000)
            return
        self.syncing = len(accounts)
        self._new_total = 0
        self._errors = []
        for acc in accounts:
            run_task(sync_account, self.db, acc,
                     on_done=self._sync_done,
                     on_error=lambda err, a=acc: self._sync_error(a.name, err),
                     on_progress=lambda t: self.status.showMessage(t))

    def _sync_done(self, n):
        self._new_total += n or 0
        self._sync_finished()

    def _sync_error(self, name, err):
        self._errors.append((name, err))
        self._sync_finished()

    def _sync_finished(self):
        self.syncing -= 1
        if self.syncing:
            return
        msg = f"Проверка завершена. Новых писем: {self._new_total}"
        if self._errors:
            msg += ". Не удалось проверить: " + ", ".join(f"{n} ({e.summary.lower()})" for n, e in self._errors)
        self.status.showMessage(msg, 30000)
        if self._errors and self._manual_check:
            name, err = self._errors[0]
            show_error(self, f"Ящик «{name}»", err)
        self.reload_tree()

    def _status_error(self, err):
        self.status.showMessage("Ошибка: " + err.short(), 15000)

    def _compose(self):
        ComposeDialog(self.db, account_id=self.current[0], parent=self).exec()
        if self.current[1] == "sent":
            self.reload_messages()

    def _quote(self, m) -> str:
        date = datetime.fromtimestamp(m.date_ts).strftime("%d.%m.%Y %H:%M") if m.date_ts else ""
        quoted = "\n".join("> " + line for line in (m.body_text or "").splitlines())
        return f"\n\n{date}, {m.sender} написал(а):\n{quoted}\n"

    def _reply(self):
        m = getattr(self, "_shown", None)
        if not m:
            return
        subj = m.subject if m.subject.lower().startswith("re:") else "Re: " + m.subject
        to = m.recipients if m.folder == "sent" else m.sender
        ComposeDialog(self.db, m.account_id, to=to, subject=subj, body=self._quote(m),
                      in_reply_to=m.message_id, parent=self).exec()

    def _forward(self):
        m = getattr(self, "_shown", None)
        if not m:
            return
        body = (f"\n\n-------- Пересылаемое сообщение --------\nОт: {m.sender}\nТема: {m.subject}\n\n"
                f"{m.body_text}")
        ComposeDialog(self.db, m.account_id, subject="Fwd: " + m.subject, body=body, parent=self).exec()

    def _mark_spam(self, is_spam: bool):
        ids = self._selected_ids()
        for mid in ids:
            m = self.db.message(mid)
            if not m or m.folder != "inbox":
                continue
            self.spam.train(m, is_spam)
            reason = "вручную: спам" if is_spam else "вручную: не спам"
            self.db.update_message(mid, is_spam=int(is_spam), spam_reason=reason)
        if ids:
            self.status.showMessage(f"Отмечено писем: {len(ids)}. Фильтр дообучен.", 5000)
            self.reload_tree()

    def _delete(self):
        ids = self._selected_ids()
        if not ids:
            return
        view = self.current[1]
        if view == "trash":
            if QMessageBox.question(self, "Удаление", f"Удалить навсегда писем: {len(ids)}?") != QMessageBox.Yes:
                return
        for mid in ids:
            m = self.db.message(mid)
            if not m:
                continue
            if view == "trash" or m.folder == "sent":
                self.db.delete_message(mid)
            else:
                # Удаляем с сервера, локальную копию переносим в «Корзину».
                self.db.update_message(mid, folder="trash")
                run_task(set_server_flag, self.db, m, "\\Deleted", on_error=self._status_error)
        self.reload_tree()

    def _manage_accounts(self):
        AccountsDialog(self.db, self).exec()
        self.reload_tree()

    def _spam_settings(self):
        SpamDialog(self.db, self).exec()

    def _interval_settings(self):
        cur = int(self.db.get_setting("check_interval_min", "10"))
        val, ok = QInputDialog.getInt(self, "Автопроверка", "Проверять почту каждые N минут (0 — выкл.):",
                                      cur, 0, 1440)
        if ok:
            self.db.set_setting("check_interval_min", val)
            self._restart_timer()
