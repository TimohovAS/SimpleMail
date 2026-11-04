"""Главное окно."""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from email.utils import getaddresses, parseaddr

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QToolBar, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget, QHBoxLayout, QInputDialog,
)

from .. import __version__

from ..db import Database
from ..mailparse import attachments
from ..protocols import set_server_flag, sync_account
from ..spam import SpamFilter
from ..storage import attachments_dir, default_storage_dir
from .account_dialog import AccountsDialog
from .compose_dialog import ComposeDialog
from .spam_dialog import SpamDialog
from .messages import show_error
from .icons import icon
from .viewer import MessageView
from .worker import run_task

VIEWS = [("inbox", "Входящие"), ("spam", "Спам"), ("sent", "Отправленные"), ("trash", "Корзина")]
VIEW_ICONS = {"inbox": "inbox", "spam": "shield-alert", "sent": "send", "trash": "trash-2"}
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
        self.viewer = MessageView()
        self.viewer.remote_allowed_for_sender.connect(self._trust_sender_images)
        self.viewer.state_changed.connect(lambda: hasattr(self, "acts") and self._update_actions())
        view_box = QWidget()
        self._view_box = view_box
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

        self.status = self.statusBar()
        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._update_actions()

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

    # ================= действия, меню, панель =================
    def _build_actions(self):
        self.acts: dict[str, QAction] = {}

        def act(key, text, slot, shortcut=None, icon_name=None, tip=None, checkable=False):
            a = QAction(text, self)
            if icon_name:
                a.setIcon(icon(icon_name))
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.setCheckable(checkable)
            hint = tip or text.rstrip("…")
            a.setToolTip(f"{hint} ({a.shortcut().toString(QKeySequence.NativeText)})" if shortcut else hint)
            a.setStatusTip(hint)
            a.triggered.connect(slot)
            self.acts[key] = a
            return a

        act("check", "Проверить почту", self.check_mail_manual, "F5", "refresh-cw", "Проверить почту во всех ящиках")
        act("compose", "Написать письмо", self._compose, "Ctrl+N", "mail-plus", "Новое письмо")
        act("reply", "Ответить", self._reply, "Ctrl+R", "reply", "Ответить отправителю")
        act("reply_all", "Ответить всем", self._reply_all, "Ctrl+Shift+R", "reply-all", "Ответить всем участникам")
        act("forward", "Переслать", self._forward, "Ctrl+F", "forward", "Переслать письмо")
        act("spam", "Это спам", lambda: self._mark_spam(True), "Ctrl+J", "mail-warning",
            "Это спам: перенести в «Спам» и обучить фильтр")
        act("not_spam", "Не спам", lambda: self._mark_spam(False), "Ctrl+Shift+J", "mail-check",
            "Не спам: вернуть во «Входящие» и обучить фильтр")
        act("unread", "Отметить непрочитанным", lambda: self._mark_seen(False), "Ctrl+U", "mail",
            "Отметить непрочитанным")
        act("read", "Отметить прочитанным", lambda: self._mark_seen(True), "Ctrl+Q", "mail-open",
            "Отметить прочитанным")
        act("delete", "Удалить", self._delete, "Del", "trash-2", "Удалить письмо")
        act("save_eml", "Сохранить письмо как…", self._save_eml, "Ctrl+S", "download", "Сохранить письмо в файл .eml")
        act("save_att", "Сохранить вложения…", self._save_attachments, None, "paperclip", "Сохранить вложения письма")
        act("images", "Показать картинки", self.viewer.load_remote, "Ctrl+I", "image",
            "Загрузить картинки из интернета в этом письме")
        act("search", "Поиск", lambda: (self.search.setFocus(), self.search.selectAll()), "Ctrl+E", "search",
            "Поиск по отправителю и теме")
        act("accounts", "Почтовые ящики…", self._manage_accounts, None, "at-sign")
        act("spam_settings", "Спам-фильтр…", self._spam_settings, None, "shield")
        act("interval", "Автопроверка почты…", self._interval_settings, None, "clock")
        act("exit", "Выход", self.close, "Ctrl+W", "log-out")
        act("about", "О программе", self._about, None, "info")
        act("show_folders", "Панель папок", lambda on: self.tree.setVisible(on), "F9", checkable=True)
        self.acts["show_folders"].setChecked(True)
        act("show_preview", "Область просмотра", lambda on: self._view_box.setVisible(on), "F8", checkable=True)
        self.acts["show_preview"].setChecked(True)
        act("labels", "Подписи под значками", self._toggle_labels, None, checkable=True)
        self.acts["labels"].setChecked(self.db.get_setting("toolbar_labels", "1") == "1")

    def _build_menus(self):
        mb = self.menuBar()
        a = self.acts
        m = mb.addMenu("&Файл")
        for k in ("compose", "check", None, "save_eml", "save_att", None, "exit"):
            m.addSeparator() if k is None else m.addAction(a[k])
        m = mb.addMenu("&Письмо")
        for k in ("reply", "reply_all", "forward", None, "read", "unread", None, "spam", "not_spam", None,
                  "images", None, "delete"):
            m.addSeparator() if k is None else m.addAction(a[k])
        m = mb.addMenu("&Вид")
        for k in ("show_folders", "show_preview", "labels", None, "search"):
            m.addSeparator() if k is None else m.addAction(a[k])
        m = mb.addMenu("&Настройки")
        for k in ("accounts", "spam_settings", "interval"):
            m.addAction(a[k])
        m = mb.addMenu("&Справка")
        m.addAction(a["about"])

    def _build_toolbar(self):
        tb = QToolBar("Основная панель")
        tb.setObjectName("main_toolbar")
        tb.setMovable(False)
        tb.setIconSize(QSize(24, 24))
        self.toolbar = tb
        self.addToolBar(tb)
        short = {"check": "Проверить", "compose": "Написать", "reply_all": "Всем", "spam": "Спам",
                 "unread": "Непрочит.", "images": "Картинки", "save_att": "Вложения"}
        for k in ("check", None, "compose", "reply", "reply_all", "forward", None,
                  "spam", "not_spam", "unread", "delete", None, "images", "save_att"):
            if k is None:
                tb.addSeparator()
                continue
            a = self.acts[k]
            if k in short:
                a.setIconText(short[k])
            tb.addAction(a)
        self._toggle_labels(self.acts["labels"].isChecked())

    def _toggle_labels(self, on: bool):
        self.toolbar.setToolButtonStyle(Qt.ToolButtonTextUnderIcon if on else Qt.ToolButtonIconOnly)
        self.db.set_setting("toolbar_labels", "1" if on else "0")

    def _update_actions(self):
        """Включить/выключить кнопки по текущему выбору."""
        ids = self._selected_ids()
        one = getattr(self, "_shown", None) is not None
        view = self.current[1]
        for k in ("reply", "reply_all", "forward", "save_eml"):
            self.acts[k].setEnabled(one)
        for k in ("delete", "unread", "read"):
            self.acts[k].setEnabled(bool(ids))
        self.acts["spam"].setEnabled(bool(ids) and view == "inbox")
        self.acts["not_spam"].setEnabled(bool(ids) and view == "spam")
        self.acts["images"].setEnabled(one and self.viewer.banner.isVisible())
        self.acts["save_att"].setEnabled(one and self.save_att_btn.isVisible())

    def closeEvent(self, event):
        self.viewer.shutdown()
        super().closeEvent(event)

    def _about(self):
        QMessageBox.about(self, "О программе",
                          f"<b>SimpleMail {__version__}</b><br>Простой почтовый клиент.<br><br>"
                          "Значки: Lucide (лицензия ISC).")

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
            top.setIcon(0, icon("at-sign"))
            top.setData(0, ROLE_ID, (acc.id, None))
            self.tree.addTopLevelItem(top)
            for view, title in VIEWS:
                unread = self.db.unread_count(acc.id, view) if view in ("inbox", "spam") else 0
                it = QTreeWidgetItem([f"{title} ({unread})" if unread else title])
                if unread:
                    it.setFont(0, bold)
                it.setData(0, ROLE_ID, (acc.id, view))
                it.setIcon(0, icon(VIEW_ICONS[view]))
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
        self._update_actions()

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
        self._update_actions()

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
        self.viewer.show_message(m, allow_remote=self._sender_trusted(m.sender))

        if not m.seen and m.folder == "inbox":
            self.db.update_message(m.id, seen=1)
            run_task(set_server_flag, self.db, m, "\\Seen", on_error=self._status_error)
            self._set_row_bold(m.id, False)
            self._refresh_counts()

    def _set_row_bold(self, mid: int, bold: bool):
        f = QFont(); f.setBold(bold)
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(ROLE_ID) == mid:
                for j in range(3):
                    self.table.item(row, j).setFont(f)
                break

    # ---------- картинки из интернета ----------
    @staticmethod
    def _addr(sender: str) -> str:
        return parseaddr(sender or "")[1].lower()

    def _sender_trusted(self, sender: str) -> bool:
        trusted = (self.db.get_setting("trusted_image_senders", "") or "").split()
        return bool(self._addr(sender)) and self._addr(sender) in trusted

    def _trust_sender_images(self, sender: str):
        addr = self._addr(sender)
        trusted = set((self.db.get_setting("trusted_image_senders", "") or "").split())
        if addr:
            trusted.add(addr)
            self.db.set_setting("trusted_image_senders", " ".join(sorted(trusted)))
            self.status.showMessage(f"Картинки от {addr} будут показываться всегда", 5000)

    def _mark_seen(self, seen: bool):
        for mid in self._selected_ids():
            m = self.db.message(mid)
            if not m or m.folder not in ("inbox",) or m.seen == seen:
                continue
            self.db.update_message(mid, seen=int(seen))
            run_task(set_server_flag, self.db, m, "\\Seen", seen, on_error=self._status_error)
            self._set_row_bold(mid, not seen)
        self._refresh_counts()

    def _save_eml(self):
        m = getattr(self, "_shown", None)
        if not m or not m.raw:
            return
        name = "".join(c if c not in '<>:"/\\|?*' else "_" for c in (m.subject or "письмо"))[:80] + ".eml"
        acc = self.db.account(m.account_id)
        start = str(Path(acc.storage_dir) / name) if acc and acc.storage_dir else name
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить письмо", start, "Письмо (*.eml)")
        if path:
            Path(path).write_bytes(m.raw)
            self.status.showMessage(f"Письмо сохранено: {path}", 5000)

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

    # ---------- написание писем (окна не модальные: можно читать почту, пока пишешь) ----------
    def _open_compose(self, **kw):
        dlg = ComposeDialog(self.db, parent=self, **kw)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.sent.connect(lambda: self.current[1] == "sent" and self.reload_messages())
        dlg.sent.connect(lambda: self.status.showMessage("Письмо отправлено", 5000))
        dlg.show()

    def _compose(self):
        self._open_compose(account_id=self.current[0])

    @staticmethod
    def _quote_header(m) -> str:
        date = datetime.fromtimestamp(m.date_ts).strftime("%d.%m.%Y %H:%M") if m.date_ts else ""
        return f"{date}, {m.sender} написал(а):"

    @staticmethod
    def _re(subject: str) -> str:
        return subject if subject.lower().startswith(("re:", "ответ:")) else "Re: " + subject

    def _reply(self):
        m = getattr(self, "_shown", None)
        if not m:
            return
        to = m.recipients if m.folder == "sent" else m.sender
        self._open_compose(account_id=m.account_id, to=to, subject=self._re(m.subject),
                           quote_header=self._quote_header(m), quote_text=m.body_text, in_reply_to=m.message_id)

    def _reply_all(self):
        m = getattr(self, "_shown", None)
        if not m:
            return
        acc = self.db.account(m.account_id)
        me = (acc.email if acc else "").lower()
        to = m.sender if m.folder != "sent" else m.recipients
        to_addrs = {a.lower() for _, a in getaddresses([to])}
        cc = ", ".join(f"{n} <{a}>" if n else a for n, a in getaddresses([m.recipients])
                       if a and a.lower() != me and a.lower() not in to_addrs)
        self._open_compose(account_id=m.account_id, to=to, cc=cc, subject=self._re(m.subject),
                           quote_header=self._quote_header(m), quote_text=m.body_text, in_reply_to=m.message_id)

    def _forward(self):
        m = getattr(self, "_shown", None)
        if not m:
            return
        header = f"-------- Пересылаемое сообщение --------\nОт: {m.sender}\nКому: {m.recipients}\nТема: {m.subject}"
        subj = m.subject if m.subject.lower().startswith("fwd:") else "Fwd: " + m.subject
        self._open_compose(account_id=m.account_id, subject=subj, quote_header=header.replace("\n", " | "),
                           quote_text=m.body_text, attachments=attachments(m.raw) if m.raw else [])

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
