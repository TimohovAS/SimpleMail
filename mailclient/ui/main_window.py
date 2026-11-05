"""Главное окно."""
from __future__ import annotations

from ..i18n import LANGUAGES, language, spam_reason, tr

import html
import threading
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from email.utils import getaddresses, parseaddr

from PySide6.QtCore import QDate, QSignalBlocker, QSize, Qt, QTimer, Slot
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QFileDialog, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QToolBar,
    QTreeWidgetItem, QVBoxLayout, QWidget, QHBoxLayout, QInputDialog, QToolButton, QButtonGroup, QMenu,
)
from PySide6.QtCore import QByteArray
from PySide6.QtGui import QActionGroup

from .. import __version__

from ..db import Database
from ..delivery import deliver_outbox
from ..mailops import change_messages, delete_permanently, move_to_trash, restore_message, set_spam_state
from ..protocols import build_message, redirect_message, send_message, set_server_flag, sync_account
from ..spam import SpamFilter
from ..storage import attachments_dir, default_storage_dir
from ..windows_integration import set_startup_enabled, startup_enabled, startup_supported
from .account_dialog import AccountEditDialog, AccountsDialog
from .compose_dialog import ComposeDialog
from .spam_dialog import SpamDialog
from .account_tree import AccountTree
from .messages import show_error
from .attachments_bar import AttachmentsBar
from .header import MessageHeader, QuickReply, human_date
from .icons import app_icon, icon
from .viewer import MessageView
from .worker import run_task
from .mail_watch import MailWatch

VIEWS = [("inbox", "Входящие"), ("spam", "Спам"), ("sent", "Отправленные"), ("trash", "Корзина"),
         ("drafts", "Черновики"), ("outbox", "Исходящие"), ("archive", "Локальный архив")]
VIEW_ICONS = {"inbox": "inbox", "spam": "shield-alert", "sent": "send", "trash": "trash-2"}
ROLE_ID = Qt.UserRole
ROLE_SORT = Qt.UserRole + 1

# колонки списка писем: (ключ, заголовок)
COLUMNS = [("att", ""), ("from", "От"), ("to", "Кому"), ("subject", "Тема"), ("date", "Получено"), ("size", "Размер"),
           ("account", "Ящик"), ("folder", "Папка"), ("status", "Состояние")]
COL = {k: i for i, (k, _) in enumerate(COLUMNS)}
LAYOUTS = [("classic", "Классическая: список сверху, письмо снизу"),
           ("wide", "Широкая: письмо справа"),
           ("list", "Только список")]


def human_size(n: int | None) -> str:
    if not n:
        return ""
    if n >= 1048576:
        return tr("{0:.2f} МБ", n / 1048576).replace(".", ",")
    return tr("{0} КБ", max(1, round(n / 1024)))


class SortItem(QTableWidgetItem):
    """Ячейка, которая сортируется по скрытому значению (дата, размер), а не по тексту."""
    def __lt__(self, other):
        a, b = self.data(ROLE_SORT), other.data(ROLE_SORT)
        if a is not None and b is not None:
            return a < b
        return super().__lt__(other)


class MainWindow(QMainWindow):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.spam = SpamFilter(db)
        self.current = (None, "inbox")       # (account_id, view)
        self.syncing = 0
        self._mail_busy = False
        self._undo_delete_ids = []
        self._history_cancel = threading.Event()
        self._search_generation = 0
        self._search_cancel = threading.Event()
        self._search_limit = 500
        self._quick_draft_id = None
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._run_search)
        self._closing = False
        self._pending_mail = set()
        self.setWindowTitle(f"SimpleMail {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1200, 760)

        # ---- левая панель: ящики и папки ----
        self.tree = AccountTree()
        self.tree.order_changed.connect(lambda ids: QTimer.singleShot(0, lambda: self._reorder_accounts(ids)))
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(10)          # компактное дерево, без лишнего отступа слева
        self.tree.setIconSize(QSize(16, 16))
        self.tree.setStyleSheet("QTreeView { padding-left: 0px; } QTreeView::item { padding: 1px 0px; }")
        self.tree.itemSelectionChanged.connect(self._on_folder_selected)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._account_menu)

        # ---- список писем ----
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Поиск: отправитель, получатель, тема, текст, вложения…"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_search)
        self.search_scope = QComboBox()
        for title, value in (("Текущая папка", "folder"), ("Текущий ящик", "account"), ("Все ящики", "all")):
            self.search_scope.addItem(tr(title), value)
        self.search_scope.currentIndexChanged.connect(self._apply_search)
        self.date_from_on, self.date_to_on = QCheckBox(tr("С даты")), QCheckBox(tr("По дату"))
        self.date_from, self.date_to = QDateEdit(QDate.currentDate().addMonths(-1)), QDateEdit(QDate.currentDate())
        for check, edit in ((self.date_from_on, self.date_from), (self.date_to_on, self.date_to)):
            edit.setCalendarPopup(True); edit.setDisplayFormat("dd.MM.yyyy"); edit.setEnabled(False)
            check.toggled.connect(edit.setEnabled); check.toggled.connect(self._apply_search)
            edit.dateChanged.connect(self._apply_search)
        self.f_all = QToolButton(); self.f_all.setText(tr("Все")); self.f_all.setCheckable(True); self.f_all.setChecked(True)
        self.f_unread = QToolButton(); self.f_unread.setText(tr("Не прочитано")); self.f_unread.setCheckable(True)
        self.f_unread.setToolTip(tr("Показывать только непрочитанные письма"))
        grp = QButtonGroup(self); grp.setExclusive(True); grp.addButton(self.f_all); grp.addButton(self.f_unread)
        grp.buttonClicked.connect(lambda _: self._apply_search())
        search_row = QHBoxLayout(); search_row.setContentsMargins(0, 0, 0, 0)
        search_row.addWidget(self.search, 1); search_row.addWidget(self.f_all); search_row.addWidget(self.f_unread)
        filters_row = QHBoxLayout()
        filters_row.addWidget(self.search_scope)
        for widget in (self.date_from_on, self.date_from, self.date_to_on, self.date_to):
            filters_row.addWidget(widget)
        filters_row.addStretch(1)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([tr(c[1]) for c in COLUMNS])
        self.table.horizontalHeaderItem(COL["att"]).setIcon(icon("paperclip", "#6b7280"))
        self.table.horizontalHeaderItem(COL["att"]).setToolTip(tr("Вложения"))
        h = self.table.horizontalHeader()
        h.setSectionsMovable(True)
        h.setSectionResizeMode(QHeaderView.Interactive)  # ширину любой колонки можно менять мышью
        h.setStretchLastSection(True)                    # последняя колонка занимает остаток места
        h.setMinimumSectionSize(24)
        for key, width in (("att", 26), ("from", 200), ("to", 170), ("subject", 420), ("date", 130), ("size", 70)):
            self.table.setColumnWidth(COL[key], width)
        h.setContextMenuPolicy(Qt.CustomContextMenu)
        h.customContextMenuRequested.connect(self._header_menu)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COL["date"], Qt.DescendingOrder)
        self.table.itemSelectionChanged.connect(self._on_message_selected)
        self.table.doubleClicked.connect(lambda _: self._open_selected())

        list_box = QWidget()
        lb = QVBoxLayout(list_box); lb.setContentsMargins(0, 0, 0, 0); lb.setSpacing(2)
        self.search_more = QPushButton(tr("Показать ещё"))
        self.search_more.clicked.connect(self._more_results)
        self.search_more.hide()
        lb.addLayout(search_row); lb.addLayout(filters_row); lb.addWidget(self.table); lb.addWidget(self.search_more)

        # ---- просмотр письма ----
        self.header = MessageHeader()
        self.header.reply.connect(self._reply)
        self.header.reply_all.connect(self._reply_all)
        self.header.forward.connect(self._forward)
        self.header.redirect.connect(self._redirect)
        self.header.save_attachments.connect(self._save_attachments)
        self.viewer = MessageView()
        self.viewer.remote_allowed_for_sender.connect(self._trust_sender_images)
        self.viewer.state_changed.connect(lambda: hasattr(self, "acts") and self._update_actions())
        self.att_bar = AttachmentsBar()
        self.att_bar.saved.connect(lambda t: self.status.showMessage(t, 8000))
        self.quick = QuickReply()
        self.quick.send.connect(self._quick_reply)
        self._quick_save_timer = QTimer(self)
        self._quick_save_timer.setSingleShot(True)
        self._quick_save_timer.setInterval(1000)
        self._quick_save_timer.timeout.connect(self._save_quick_draft)
        self.quick.edit.textChanged.connect(lambda: self._quick_save_timer.start())
        self.quick.hide()
        view_box = QWidget()
        self._view_box = view_box
        vb = QVBoxLayout(view_box); vb.setContentsMargins(0, 0, 0, 0); vb.setSpacing(0)
        # письмо и поле быстрого ответа разделены перетаскиваемой границей;
        # панель вложений над письмом сама подстраивает высоту под число файлов
        self.reply_split = QSplitter(Qt.Vertical)
        self.reply_split.addWidget(self.viewer); self.reply_split.addWidget(self.quick)
        for i, stretch in enumerate((1, 0)):
            self.reply_split.setStretchFactor(i, stretch)
            self.reply_split.setCollapsible(i, False)
        self.reply_split.setHandleWidth(6)
        self.reply_split.setSizes([600, 70])
        self.att_bar.set_mode(self.db.get_setting("att_mode", "list") or "list")
        self.att_bar.toggle.setChecked(self.db.get_setting("att_collapsed", "0") == "1")
        self.att_bar.mode_changed.connect(lambda m: self.db.set_setting("att_mode", m))
        self.att_bar.collapsed_changed.connect(lambda c: self.db.set_setting("att_collapsed", int(c)))
        vb.addWidget(self.header); vb.addWidget(self.att_bar); vb.addWidget(self.reply_split, 1)

        self.right = QSplitter(Qt.Vertical)
        self.right.addWidget(list_box); self.right.addWidget(view_box)
        self.right.setChildrenCollapsible(False)
        self.main_split = QSplitter(Qt.Horizontal)
        self.main_split.addWidget(self.tree); self.main_split.addWidget(self.right)
        self.main_split.setSizes([190, 1010])
        self.setCentralWidget(self.main_split)

        self.status = self.statusBar()
        self.counts_label = QLabel()
        self.status.addPermanentWidget(self.counts_label)
        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._restore_ui()
        # вид окна сохраняется сразу после изменения (через 1 с), а не только при закрытии:
        # иначе он теряется, если программу закрыли аварийно или завершили процесс
        self._ui_save_timer = QTimer(self)
        self._ui_save_timer.setSingleShot(True)
        self._ui_save_timer.setInterval(1000)
        self._ui_save_timer.timeout.connect(self._save_ui)
        for sp in (self.main_split, self.right, self.reply_split):
            sp.splitterMoved.connect(self._schedule_ui_save)
        hh = self.table.horizontalHeader()
        hh.sectionResized.connect(self._schedule_ui_save)
        hh.sectionMoved.connect(self._schedule_ui_save)
        hh.sortIndicatorChanged.connect(self._schedule_ui_save)
        self._update_actions()
        run_task(self.db.backfill_meta, on_done=lambda n: n and self.reload_messages())

        # ---- автопроверка ----
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_mail)
        self._restart_timer()
        self._mail_timer = QTimer(self)
        self._mail_timer.setSingleShot(True)
        self._mail_timer.setInterval(300)
        self._mail_timer.timeout.connect(self._check_pending_mail)
        self.mail_watch = MailWatch(self.db, self)
        self.mail_watch.changed.connect(self._mail_changed)
        self.mail_watch.unavailable.connect(self._idle_unavailable)

        # ящики из старой версии: назначаем папку хранения по умолчанию
        for acc in self.db.accounts():
            if not acc.storage_dir:
                acc.storage_dir = default_storage_dir(acc.email)
                self.db.save_account(acc)

        self.reload_tree()
        self.mail_watch.restart()
        QTimer.singleShot(0, self.check_mail)
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

        act("check", tr("Проверить почту"), self.check_mail_manual, "F5", "refresh-cw", tr("Проверить почту во всех ящиках"))
        act("history", tr("Скачать всю историю"), self._download_history, None, "download")
        act("pause_history", tr("Приостановить загрузку истории"), self._pause_history, None, "clock")
        act("backup", tr("Создать резервную копию…"), self._backup_mail, None, "download")
        act("restore_backup", tr("Восстановить резервную копию…"), self._restore_backup, None, "folder-open")
        act("global_search", tr("Поиск по всем ящикам"), self._global_search, "Ctrl+Shift+F", "search")
        act("compose", tr("Написать письмо"), self._compose, "Ctrl+N", "mail-plus", tr("Новое письмо"))
        act("edit_draft", tr("Открыть для редактирования"), self._open_selected, "Ctrl+O", "mail-plus")
        act("reply", tr("Ответить"), self._reply, "Ctrl+R", "reply", tr("Ответить отправителю"))
        act("reply_all", tr("Ответить всем"), self._reply_all, "Ctrl+Shift+R", "reply-all", tr("Ответить всем участникам"))
        act("forward", tr("Переслать"), self._forward, "Ctrl+F", "forward", tr("Переслать письмо"))
        act("spam", tr("Это спам"), lambda: self._mark_spam(True), "Ctrl+J", "mail-warning",
            tr("Это спам: перенести в «Спам» и обучить фильтр"))
        act("not_spam", tr("Не спам"), lambda: self._mark_spam(False), "Ctrl+Shift+J", "mail-check",
            tr("Не спам: вернуть во «Входящие» и обучить фильтр"))
        act("unread", tr("Отметить непрочитанным"), lambda: self._mark_seen(False), "Ctrl+U", "mail",
            tr("Отметить непрочитанным"))
        act("read", tr("Отметить прочитанным"), lambda: self._mark_seen(True), "Ctrl+Q", "mail-open",
            tr("Отметить прочитанным"))
        act("redirect", tr("Перенаправить…"), self._redirect, None, "send",
            tr("Перенаправить: отправить письмо как есть другому адресату"))
        act("delete", tr("Удалить"), self._delete, "Del", "trash-2", tr("Удалить письмо"))
        act("undo_delete", tr("Отменить удаление"), self._undo_delete, "Ctrl+Z", "undo-2")
        act("restore", tr("Восстановить из корзины"), self._restore_selected, "Ctrl+Shift+Z", "undo-2")
        act("save_eml", tr("Сохранить письмо как…"), self._save_eml, "Ctrl+S", "download", tr("Сохранить письмо в файл .eml"))
        act("save_att", tr("Сохранить вложения…"), self._save_attachments, None, "paperclip", tr("Сохранить вложения письма"))
        act("images", tr("Показать картинки"), self.viewer.load_remote, "Ctrl+I", "image",
            tr("Загрузить картинки из интернета в этом письме"))
        act("search", tr("Поиск"), lambda: (self.search.setFocus(), self.search.selectAll()), "Ctrl+E", "search",
            tr("Поиск по отправителю и теме"))
        act("accounts", tr("Почтовые ящики…"), self._manage_accounts, None, "at-sign")
        act("signatures", tr("Подписи…"), self._signatures, None, "signature", tr("Подписи к письмам"))
        act("spam_settings", tr("Спам-фильтр…"), self._spam_settings, None, "shield")
        act("interval", tr("Автопроверка почты…"), self._interval_settings, None, "clock")
        startup = act("autostart", tr("Запускать при входе в Windows"), self._toggle_autostart,
                      None, "log-in", checkable=True)
        startup.setEnabled(startup_supported())
        startup.setChecked(startup_enabled())
        act("exit", tr("Выход"), self.close, "Ctrl+W", "log-out")
        act("about", tr("О программе"), self._about, None, "info")
        act("show_folders", tr("Панель папок"), lambda on: self.tree.setVisible(on), "F9", checkable=True)
        self.acts["show_folders"].setChecked(True)
        self.layout_group = QActionGroup(self)
        for i, (key, title) in enumerate(LAYOUTS):
            a = act("layout_" + key, tr(title), lambda _=False, k=key: self._set_layout(k), f"Ctrl+{i + 1}", checkable=True)
            self.layout_group.addAction(a)
        act("quick_reply", tr("Поле быстрого ответа"), self._toggle_quick, None, checkable=True)
        self.acts["quick_reply"].setChecked(self.db.get_setting("quick_reply", "1") == "1")
        act("labels", tr("Подписи под значками"), self._toggle_labels, None, checkable=True)
        self.acts["labels"].setChecked(self.db.get_setting("toolbar_labels", "1") == "1")

    def _build_menus(self):
        mb = self.menuBar()
        a = self.acts
        m = mb.addMenu(tr("&Файл"))
        for k in ("compose", "check", "history", "pause_history", None, "save_eml", "save_att", None, "backup", "restore_backup", None, "exit"):
            m.addSeparator() if k is None else m.addAction(a[k])
        m = mb.addMenu(tr("&Письмо"))
        for k in ("edit_draft", "reply", "reply_all", "forward", "redirect", None, "read", "unread", None, "spam", "not_spam", None,
                  "images", None, "delete", "undo_delete", "restore"):
            m.addSeparator() if k is None else m.addAction(a[k])
        m = mb.addMenu(tr("&Вид"))
        lm = m.addMenu(tr("Раскладка окна"))
        for key, _ in LAYOUTS:
            lm.addAction(a["layout_" + key])
        for k in (None, "show_folders", "quick_reply", "labels", None, "search", "global_search"):
            m.addSeparator() if k is None else m.addAction(a[k])
        m = mb.addMenu(tr("&Настройки"))
        for k in ("accounts", "signatures", "spam_settings", "interval", "autostart"):
            m.addAction(a[k])
        self.language_menu = m.addMenu(tr("Язык интерфейса"))
        self.language_group = QActionGroup(self)
        self.language_group.setExclusive(True)
        self.language_actions = {}
        selected = self.db.get_setting("language", language())
        if selected not in LANGUAGES:
            selected = language()
        for code, label in LANGUAGES.items():
            action = self.language_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(code == selected)
            action.triggered.connect(lambda checked=False, value=code: self._select_language(value))
            self.language_group.addAction(action)
            self.language_actions[code] = action
        m = mb.addMenu(tr("&Справка"))
        m.addAction(a["about"])

    def _build_toolbar(self):
        tb = QToolBar(tr("Основная панель"))
        tb.setObjectName("main_toolbar")
        tb.setMovable(False)
        tb.setIconSize(QSize(24, 24))
        self.toolbar = tb
        self.addToolBar(tb)
        short = {"check": tr("Проверить"), "compose": tr("Написать"), "reply_all": tr("Всем"), "spam": tr("Спам"),
                 "unread": tr("Непрочит."), "images": tr("Картинки"), "save_att": tr("Вложения"),
                 "undo_delete": tr("Отменить"), "restore": tr("Восстановить")}
        for k in ("check", None, "compose", "reply", "reply_all", "forward", None,
                  "spam", "not_spam", "unread", "delete", "undo_delete", "restore", None, "images", "save_att"):
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
        views = set(self._selected_folders().values())
        view = next(iter(views)) if len(views) == 1 else None
        for k in ("reply", "reply_all", "forward"):
            self.acts[k].setEnabled(one and view not in ("drafts", "outbox"))
        self.acts["save_eml"].setEnabled(one)
        self.acts["edit_draft"].setEnabled(one and view in ("drafts", "outbox"))
        for k in ("delete", "unread", "read"):
            self.acts[k].setEnabled(bool(ids) and not self._mail_busy)
        self.acts["spam"].setEnabled(bool(ids) and view == "inbox" and not self._mail_busy)
        self.acts["not_spam"].setEnabled(bool(ids) and (view == "spam" or self.current[1] == "spam") and not self._mail_busy)
        self.acts["undo_delete"].setEnabled(bool(self._undo_delete_ids) and not self._mail_busy)
        self.acts["restore"].setEnabled(bool(ids) and view == "trash" and not self._mail_busy)
        self.acts["images"].setEnabled(one and self.viewer.banner.isVisible())
        self.acts["save_att"].setEnabled(one and self.att_bar.isVisible())
        self.acts["redirect"].setEnabled(one and view not in ("drafts", "outbox"))

    # ---------- раскладка окна и сохранение вида ----------
    def _set_layout(self, key: str):
        old = getattr(self, "_layout", None)
        if old:
            self.db.set_setting(f"split_right_{old}", bytes(self.right.saveState().toBase64()).decode())
        self._layout = key
        self.acts["layout_" + key].setChecked(True)
        self.right.setOrientation(Qt.Horizontal if key == "wide" else Qt.Vertical)
        self._view_box.setVisible(key != "list")
        state = self.db.get_setting(f"split_right_{key}", "")
        if state:
            self.right.restoreState(QByteArray.fromBase64(state.encode()))
        else:
            total = self.right.width() if key == "wide" else self.right.height()
            self.right.setSizes([int(total * 0.5), int(total * 0.5)] if key == "wide" else [int(total * 0.4), int(total * 0.6)])
        self.db.set_setting("layout", key)

    def _toggle_quick(self, on: bool):
        self.db.set_setting("quick_reply", "1" if on else "0")
        self.quick.setVisible(on and self._shown is not None and self._shown.folder not in ("sent", "drafts", "outbox"))

    def _restore_ui(self):
        def load(key):
            v = self.db.get_setting(key, "")
            return QByteArray.fromBase64(v.encode()) if v else None
        if (g := load("geometry")):
            self.restoreGeometry(g)
        if (st := load("split_main")):
            self.main_split.restoreState(st)
        if (rs := load("split_reply4")):
            self.reply_split.restoreState(rs)
        if not load("header_state"):
            QTimer.singleShot(0, self._fit_subject_column)  # первый запуск: тема занимает свободное место
        if (hs := load("header_state")):
            hh = self.table.horizontalHeader()
            hh.restoreState(hs)
            hh.setSectionResizeMode(QHeaderView.Interactive)  # старые настройки могли «закрепить» ширину темы
            hh.setStretchLastSection(True)
        self._shown = None
        self._set_layout(self.db.get_setting("layout", "classic") or "classic")

    def _fit_subject_column(self):
        others = sum(self.table.columnWidth(i) for i in range(self.table.columnCount())
                     if i not in (COL["subject"], COL["size"]) and not self.table.isColumnHidden(i))
        free = self.table.viewport().width() - others - 80
        self.table.setColumnWidth(COL["subject"], max(250, free))

    def _save_ui(self):
        if not self.isVisible() and not hasattr(self, "_layout"):
            return
        enc = lambda b: bytes(b.toBase64()).decode()
        self.db.set_setting("geometry", enc(self.saveGeometry()))
        self.db.set_setting("split_main", enc(self.main_split.saveState()))
        self.db.set_setting(f"split_right_{self._layout}", enc(self.right.saveState()))
        self.db.set_setting("header_state", enc(self.table.horizontalHeader().saveState()))
        self.db.set_setting("split_reply4", enc(self.reply_split.saveState()))

    def _schedule_ui_save(self, *_):
        if not self._closing and hasattr(self, "_ui_save_timer"):
            self._ui_save_timer.start()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._schedule_ui_save()

    def moveEvent(self, e):
        super().moveEvent(e)
        self._schedule_ui_save()

    def changeEvent(self, e):  # развернули / восстановили окно
        super().changeEvent(e)
        if e.type() == e.Type.WindowStateChange:
            self._schedule_ui_save()

    def closeEvent(self, event):
        self._quick_save_timer.stop()
        if not self._save_quick_draft():
            event.ignore()
            return
        for dialog in list(self.findChildren(ComposeDialog)):
            dialog.close()
            if dialog.isVisible():
                event.ignore()
                return
        self._closing = True
        self._history_cancel.set()
        self._search_timer.stop()
        self._search_generation += 1
        self._search_cancel.set()
        self.timer.stop()
        self._mail_timer.stop()
        self._ui_save_timer.stop()
        self.mail_watch.stop()
        self._save_ui()
        self.viewer.shutdown()
        super().closeEvent(event)

    # ---------- перенаправление и быстрый ответ ----------
    def _redirect(self):
        m = getattr(self, "_shown", None)
        if not m or not m.raw:
            return
        to, ok = QInputDialog.getText(self, tr("Перенаправить"),
                                      tr("Кому перенаправить письмо (оно уйдёт без изменений, от исходного отправителя):"))
        if not ok or not to.strip():
            return
        acc = self.db.account(m.account_id)
        run_task(redirect_message, self.db, acc, m.raw, to.strip(),
                 on_done=lambda _: self.status.showMessage(tr("Письмо перенаправлено: {0}", to.strip()), 5000),
                 on_error=lambda e: show_error(self, tr("Письмо не перенаправлено"), e))

    def _quick_message(self, m, text):
        acc = self.db.account(m.account_id)
        sig_html = self.db.account_signature_html(m.account_id)
        sig = ""
        if sig_html:
            from .editor import RichEditor
            sig = RichEditor.plain_from_html(sig_html).strip()
        quote = "\n".join("> " + line for line in (m.body_text or "").splitlines())
        body = text + (f"\n\n-- \n{sig}" if sig else "") + f"\n\n{self._quote_header(m)}\n{quote}\n"
        em = build_message(acc, m.sender, self._re(m.subject), body, in_reply_to=m.message_id)
        state = dict(to=m.sender, cc="", mode="plain", html=html.escape(body).replace("\n", "<br>"),
                     in_reply_to=m.message_id, quick_source=m.id, quick_text=text)
        return em, state

    def _save_quick_draft(self, queued=False):
        m = getattr(self, "_shown", None)
        if not m or m.folder in ("drafts", "outbox", "sent"):
            return True
        text = self.quick.edit.toPlainText()
        existing = self.db.message(self._quick_draft_id) if self._quick_draft_id else None
        if existing and existing.folder != "drafts" and not queued:
            return True
        try:
            if not text.strip():
                if existing and existing.folder == "drafts":
                    self.db.delete_message(existing.id)
                self._quick_draft_id = None
                return True
            em, state = self._quick_message(m, text)
            self._quick_draft_id = self.db.save_composition(m.account_id, em.as_bytes(), state, self._quick_draft_id, queued)
            return True
        except Exception as error:
            from ..errors import explain
            self._status_error(explain(error))
            return False

    def _quick_reply(self, text: str):
        m = getattr(self, "_shown", None)
        if not m:
            return
        with QSignalBlocker(self.quick.edit):
            self.quick.edit.setPlainText(text)
        self._quick_save_timer.stop()
        if not self._save_quick_draft(queued=True):
            return
        source_id, draft_id = m.id, self._quick_draft_id
        self.quick.set_busy(True)

        def done(result):
            if self._closing:
                return
            self.quick.set_busy(False)
            if self._shown and self._shown.id == source_id and self._quick_draft_id == draft_id:
                self._quick_draft_id = None
                with QSignalBlocker(self.quick.edit):
                    self.quick.clear()
            text = (tr("Ответ отправлен") if result == "sent" else
                    tr("Письмо сохранено в исходящих и будет отправлено при подключении.") if result == "queued" else
                    tr("Результат отправки неизвестен. Проверьте доставку перед повторной отправкой."))
            self.status.showMessage(text, 15000)
            self.reload_tree()

        def fail(err):
            self.quick.set_busy(False)
            show_error(self, tr("Ответ не отправлен"), err)

        run_task(deliver_outbox, self.db, draft_id, on_done=done, on_error=fail)

    def _about(self):
        QMessageBox.about(self, tr("О программе"),
                          tr("<b>SimpleMail {0}</b><br>Простой почтовый клиент.<br><br>Значки: Lucide (лицензия ISC).", __version__))

    def _restart_timer(self):
        minutes = int(self.db.get_setting("check_interval_min", "10"))
        self.timer.stop()
        if minutes > 0:
            self.timer.start(minutes * 60_000)

    # ================= дерево ящиков =================
    def _account_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None or item.parent() is not None:
            return
        account_id, _ = item.data(0, ROLE_ID)
        account_ids = [acc.id for acc in self.db.accounts()]
        if account_id not in account_ids:
            return
        account = self.db.account(account_id)
        if account is None:
            return
        row = account_ids.index(account_id)
        menu = QMenu(self)
        check = menu.addAction(icon("refresh-cw"), tr("Проверить почту"))
        check.setEnabled(account.enabled)
        check.triggered.connect(lambda: self._check_account(account_id))
        compose = menu.addAction(icon("mail-plus"), tr("Написать письмо"))
        compose.triggered.connect(lambda: self._open_compose(account_id=account_id))
        menu.addSeparator()
        properties = menu.addAction(icon("settings"), tr("Настройки ящика…"))
        properties.triggered.connect(lambda: self._edit_account(account_id))
        enabled = menu.addAction(tr("Проверять этот ящик"))
        enabled.setCheckable(True)
        enabled.setChecked(account.enabled)
        enabled.triggered.connect(lambda checked: self._set_account_enabled(account_id, checked))
        menu.addSeparator()
        up = menu.addAction(tr("Вверх"))
        up.setEnabled(row > 0)
        up.triggered.connect(lambda: self._move_account(account_id, -1))
        down = menu.addAction(tr("Вниз"))
        down.setEnabled(row < len(account_ids) - 1)
        down.triggered.connect(lambda: self._move_account(account_id, 1))
        menu.addSeparator()
        settings = menu.addMenu(tr("&Настройки"))
        for key in ("accounts", "signatures", "spam_settings", "interval", "autostart"):
            settings.addAction(self.acts[key])
        settings.addMenu(self.language_menu)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _check_account(self, account_id):
        if self.syncing:
            self._mail_changed(account_id)
        else:
            self.check_mail(manual=True, account_ids={account_id})

    def _edit_account(self, account_id):
        account = self.db.account(account_id)
        if account is None:
            return
        if AccountEditDialog(self.db, account, self).exec():
            self._pending_mail.discard(account_id)
            self.mail_watch.restart()
            self.reload_tree()
            updated = self.db.account(account_id)
            if updated and updated.enabled:
                self._mail_changed(account_id)

    def _set_account_enabled(self, account_id, enabled):
        account = self.db.account(account_id)
        if account is None:
            return
        account.enabled = enabled
        self.db.save_account(account)
        self._pending_mail.discard(account_id)
        self.mail_watch.restart()
        self.reload_tree()
        if enabled:
            self._mail_changed(account_id)

    def _move_account(self, account_id, offset):
        account_ids = [acc.id for acc in self.db.accounts()]
        if account_id not in account_ids:
            return
        row = account_ids.index(account_id)
        destination = row + offset
        if not 0 <= destination < len(account_ids):
            return
        account_ids[row], account_ids[destination] = account_ids[destination], account_ids[row]
        self.db.set_account_order(account_ids)
        self.reload_tree()

    def reload_tree(self):
        keep = self.current
        self.tree.blockSignals(True)
        self.tree.clear()
        select_item = None
        bold = QFont(); bold.setBold(True)
        for acc in self.db.accounts():
            top = QTreeWidgetItem([acc.name + ("" if acc.enabled else tr(" (выкл.)"))])
            top.setIcon(0, icon("at-sign"))
            top.setData(0, ROLE_ID, (acc.id, None))
            self.tree.addTopLevelItem(top)
            views = list(VIEWS) + [(row["view"], row["display_name"] or row["name"]) for row in self.db.server_folders(acc.id) if row["view"].startswith("remote:")]
            for view, title in views:
                title = tr(title)
                unread = self.db.unread_count(acc.id, view) if view in ("inbox", "spam") else 0
                it = QTreeWidgetItem([f"{title} ({unread})" if unread else title])
                if unread:
                    it.setFont(0, bold)
                it.setData(0, ROLE_ID, (acc.id, view))
                it.setIcon(0, icon(VIEW_ICONS.get(view, "folder-open")))
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
        keep_selection = self.current == (acc_id, view)
        if not keep_selection:
            self._search_limit = 500
        self.current = (acc_id, view)
        self.reload_messages(keep_selection=keep_selection)
        self._update_actions()

    # ================= список писем =================
    def _search_parameters(self):
        acc_id, view = self.current
        scope = self.search_scope.currentData()
        since = datetime.combine(self.date_from.date().toPython(), datetime.min.time()).timestamp() if self.date_from_on.isChecked() else None
        until = datetime.combine(self.date_to.date().toPython() + timedelta(days=1), datetime.min.time()).timestamp() if self.date_to_on.isChecked() else None
        return (self.search.text(), None if scope == "all" else acc_id, view if scope == "folder" else None,
                since, until, self.f_unread.isChecked(), self._search_limit + 1)

    def reload_messages(self, keep_selection: bool = True):
        self._search_timer.stop()
        self._search_generation += 1
        self._search_cancel.set()
        if self.search_scope.currentData() != "folder":
            self._run_search()
            return
        selected = self._selected_id() if keep_selection else None
        rows = self.db.search_messages(*self._search_parameters()) if self.current[0] else []
        self._display_rows(rows, selected)

    def _display_rows(self, rows, selected):
        more = len(rows) > self._search_limit
        rows = rows[:self._search_limit]
        self.search_more.setVisible(more)
        scope = self.search_scope.currentData()
        for key in ("account", "folder"):
            self.table.setColumnHidden(COL[key], scope == "folder")
        self.table.setColumnHidden(COL["status"], not any(row["delivery_state"] for row in rows))
        with QSignalBlocker(self.table):
            self.table.clearSelection()
            self.table.setSortingEnabled(False)
            self.table.setRowCount(len(rows))
            bold = QFont(); bold.setBold(True)
            clip = icon("paperclip", "#6b7280")
            for i, r in enumerate(rows):
                unread = not r["seen"] and r["folder"] not in ("drafts", "outbox")
                values = {
                    "att": ("", 1 if r["has_att"] else 0),
                    "from": (self._short_addr(r["sender"]), (r["sender"] or "").lower()),
                    "to": (self._short_addr(r["recipients"]), (r["recipients"] or "").lower()),
                    "subject": (r["subject"] or tr("(без темы)"), None),
                    "date": (human_date(r["date_ts"]), r["date_ts"] or 0),
                    "size": (human_size(r["size"]), r["size"] or 0),
                    "account": (r["account_name"], r["account_name"]),
                    "folder": (tr(dict(VIEWS).get(r["folder"], r["folder"].removeprefix("remote:"))), r["folder"]),
                    "status": ({"pending": tr("Ожидает подключения"), "sending": tr("Проверьте доставку"),
                                "uncertain": tr("Проверьте доставку"), "append_pending": tr("Отправлено; копия ожидает сохранения")}.get(r["delivery_state"], ""), r["delivery_state"] or ""),
                }
                for key, (text, sort) in values.items():
                    item = SortItem(text)
                    item.setData(ROLE_ID, r["id"])
                    item.setData(ROLE_SORT, sort)
                    item.setData(ROLE_SORT + 1, int(unread))
                    item.setData(ROLE_SORT + 2, r["folder"])
                    if unread:
                        item.setFont(bold)
                    if key in ("from", "to"):
                        item.setToolTip(r["sender"] if key == "from" else r["recipients"])
                    elif key == "status" and r["delivery_error"]:
                        item.setToolTip(r["delivery_error"])
                    elif r["spam_reason"]:
                        item.setToolTip(tr("Спам-фильтр: ") + spam_reason(r["spam_reason"]))
                    if key == "att" and r["has_att"]:
                        item.setIcon(clip)
                    if key == "size":
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.table.setItem(i, COL[key], item)
            self.table.setSortingEnabled(True)
            visible_rows = [i for i in range(self.table.rowCount()) if not self.table.isRowHidden(i)]
            select_row = next((i for i in visible_rows
                               if self.table.item(i, 0).data(ROLE_ID) == selected),
                              visible_rows[0] if visible_rows else -1)
            if select_row >= 0:
                self.table.selectRow(select_row)
        # Выделение той же строки не вызывает сигнал Qt: обновляем просмотр явно.
        self._on_message_selected()

    @staticmethod
    def _short_addr(value: str) -> str:
        """«Иван Петров <ivan@x.ru>, …» -> «Иван Петров, …» (короче для списка)."""
        parts = [n or a for n, a in getaddresses([value or ""]) if n or a]
        return ", ".join(parts) if parts else (value or "")

    def _update_counts(self):
        total = sum(1 for i in range(self.table.rowCount()) if not self.table.isRowHidden(i))
        unread = sum(1 for i in range(self.table.rowCount())
                     if not self.table.isRowHidden(i) and self.table.item(i, 0).data(ROLE_SORT + 1))
        sel = len(self._selected_ids())
        text = tr("Писем: {0}", total) + (tr(", непрочитанных: {0}", unread) if unread else "")
        self.counts_label.setText(tr("Выбрано: {0} · ", sel) + text if sel > 1 else text)

    def _header_menu(self, pos):
        menu = QMenu(self)
        for key, title in COLUMNS:
            if key == "subject":
                continue
            a = menu.addAction(tr(title) if title else tr("Вложения"))
            a.setCheckable(True)
            a.setChecked(not self.table.isColumnHidden(COL[key]))
            a.toggled.connect(lambda on, c=COL[key]: self.table.setColumnHidden(c, not on))
        menu.exec(self.table.horizontalHeader().mapToGlobal(pos))

    def _apply_search(self, *_):
        if not hasattr(self, "table"):
            return
        self._search_generation += 1
        self._search_cancel.set()
        self._search_limit = 500
        if self.search.text().strip() or self.search_scope.currentData() != "folder":
            self._search_timer.start()
        else:
            self.reload_messages()

    def _run_search(self):
        self._search_timer.stop()
        self._search_generation += 1
        self._search_cancel.set()
        self._search_cancel = threading.Event()
        generation = self._search_generation
        selected = self._selected_id()
        def done(rows):
            if not self._closing and generation == self._search_generation:
                self._display_rows(rows, selected)
        def fail(error):
            if not self._closing and generation == self._search_generation:
                self._status_error(error)
        run_task(self.db.search_messages, *self._search_parameters(), cancel=self._search_cancel, on_done=done, on_error=fail)

    def _more_results(self):
        self._search_limit += 500
        self._run_search()

    def _global_search(self):
        self.search_scope.setCurrentIndex(self.search_scope.findData("all"))
        self.search.setFocus(); self.search.selectAll()

    def _selected_ids(self) -> list[int]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, 0).data(ROLE_ID) for r in rows]

    def _selected_id(self) -> int | None:
        ids = self._selected_ids()
        return ids[0] if len(ids) == 1 else None

    def _selected_folders(self):
        rows = {index.row() for index in self.table.selectedIndexes()}
        return {self.table.item(row, 0).data(ROLE_ID): self.table.item(row, 0).data(ROLE_SORT + 2) for row in rows}

    def _on_message_selected(self):
        self._show_message(self._selected_id())
        self._update_actions()
        self._update_counts()

    # ================= просмотр =================
    def _show_message(self, mid: int | None):
        self._quick_save_timer.stop()
        if not self._save_quick_draft():
            return
        self._quick_draft_id = None
        with QSignalBlocker(self.quick.edit):
            self.quick.clear()
        self._shown = None
        if mid is None:
            self.header.hide()
            self.att_bar.hide()
            self.quick.hide()
            self.viewer.clear()
            return
        m = self.db.message(mid)
        if not m:
            return
        self._shown = m
        atts = self.db.message_attachments(m.id)
        self.header.show_message(m, [n for n, _ in atts])
        acc = self.db.account(m.account_id)
        self.att_bar.set_files(atts, str(Path(acc.storage_dir) / "Вложения") if acc and acc.storage_dir else "")
        self.viewer.show_message(m, allow_remote=self._sender_trusted(m.sender))
        self.quick.setVisible(m.folder not in ("sent", "drafts", "outbox") and self.acts["quick_reply"].isChecked())
        if m.folder not in ("drafts", "outbox", "sent"):
            for row in self.db.conn().execute("SELECT id,compose_state FROM messages WHERE account_id=? AND folder='drafts' AND compose_state IS NOT NULL", (m.account_id,)):
                state = json.loads(row["compose_state"])
                if state.get("quick_source") == m.id:
                    self._quick_draft_id = row["id"]
                    with QSignalBlocker(self.quick.edit):
                        self.quick.edit.setPlainText(state.get("quick_text", ""))
                    break

        if not m.seen and m.folder not in ("drafts", "outbox"):
            self.db.update_message(m.id, seen=1, pending_seen=1)
            run_task(set_server_flag, self.db, m, "\\Seen", on_error=self._status_error)
            self._set_row_bold(m.id, False)
            self._refresh_counts()

    def _set_row_bold(self, mid: int, bold: bool):
        f = QFont(); f.setBold(bold)
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(ROLE_ID) == mid:
                for j in range(self.table.columnCount()):
                    self.table.item(row, j).setFont(f)
                    self.table.item(row, j).setData(ROLE_SORT + 1, int(bold))
                break
        self._update_counts()

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
            self.status.showMessage(tr("Картинки от {0} будут показываться всегда", addr), 5000)

    def _mark_seen(self, seen: bool):
        for mid in self._selected_ids():
            m = self.db.message(mid)
            if not m or m.folder in ("drafts", "outbox") or m.seen == seen:
                continue
            self.db.update_message(mid, seen=int(seen), pending_seen=int(seen))
            run_task(set_server_flag, self.db, m, "\\Seen", seen, on_error=self._status_error)
            self._set_row_bold(mid, not seen)
        self._refresh_counts()

    def _save_eml(self):
        m = getattr(self, "_shown", None)
        if not m or not m.raw:
            return
        name = "".join(c if c not in '<>:"/\\|?*' else "_" for c in (m.subject or tr("письмо")))[:80] + ".eml"
        acc = self.db.account(m.account_id)
        start = str(Path(acc.storage_dir) / name) if acc and acc.storage_dir else name
        path, _ = QFileDialog.getSaveFileName(self, tr("Сохранить письмо"), start, tr("Письмо (*.eml)"))
        if path:
            Path(path).write_bytes(m.raw)
            self.status.showMessage(tr("Письмо сохранено: {0}", path), 5000)

    def _save_attachments(self):
        self.att_bar.save_all()

    def _refresh_counts(self):
        """Обновить счётчики непрочитанных без перестройки дерева."""
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for k in range(top.childCount()):
                it = top.child(k)
                acc_id, view = it.data(0, ROLE_ID)
                title = tr(dict(VIEWS).get(view, view.removeprefix("remote:")))
                if view in ("inbox", "spam"):
                    n = self.db.unread_count(acc_id, view)
                    it.setText(0, f"{title} ({n})" if n else title)
                    f = QFont(); f.setBold(bool(n)); it.setFont(0, f)

    # ================= действия =================
    def check_mail_manual(self):
        self.check_mail(manual=True)

    @Slot(int)
    def _mail_changed(self, account_id):
        if self._closing:
            return
        self._pending_mail.add(account_id)
        if not self.syncing:
            self._mail_timer.start()

    @Slot(int)
    def _idle_unavailable(self, account_id):
        if not self._closing and (acc := self.db.account(account_id)):
            fallback = (tr("работает проверка по таймеру") if self.timer.isActive()
                        else tr("автопроверка по таймеру отключена"))
            self.status.showMessage(
                tr("{0}: мгновенное получение недоступно, {1}", acc.name, fallback), 10000)

    def _check_pending_mail(self):
        if not self._closing and not self.syncing and self._pending_mail:
            self.check_mail(account_ids=set(self._pending_mail))

    def check_mail(self, manual: bool = False, account_ids=None):
        if self._closing or self.syncing:
            return
        self._manual_check = manual
        accounts = self.db.accounts(only_enabled=True)
        if account_ids is not None:
            accounts = [acc for acc in accounts if acc.id in account_ids]
            self._pending_mail.difference_update(account_ids)
        else:
            self._pending_mail.clear()
        if not accounts:
            self.status.showMessage(tr("Нет ящиков для проверки"), 5000)
            return
        self.syncing = len(accounts)
        self._new_total = 0
        self._errors = []
        self._history_cancel.clear()
        for acc in accounts:
            run_task(sync_account, self.db, acc,
                     on_done=self._sync_done,
                     on_error=lambda err, a=acc: self._sync_error(a.name, err),
                     on_progress=lambda t: self.status.showMessage(t), cancel=self._history_cancel)

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
        msg = tr("Проверка завершена. Новых писем: {0}", self._new_total)
        if self._errors:
            msg += tr(". Не удалось проверить: ") + ", ".join(f"{n} ({e.summary.lower()})" for n, e in self._errors)
        self.status.showMessage(msg, 30000)
        if self._errors and self._manual_check:
            name, err = self._errors[0]
            show_error(self, tr("Ящик «{0}»", name), err)
        self.reload_tree()
        if self._pending_mail and not self._closing:
            self._mail_timer.start()

    def _status_error(self, err):
        self.status.showMessage(tr("Ошибка: ") + err.short(), 15000)

    # ---------- написание писем (окна не модальные: можно читать почту, пока пишешь) ----------
    def _open_compose(self, **kw):
        if kw.get("draft_id") is not None:
            for existing in self.findChildren(ComposeDialog):
                if existing._draft_id == kw["draft_id"] and existing.isVisible():
                    existing.raise_(); existing.activateWindow()
                    return
            m = self.db.message(kw["draft_id"])
            if m and m.compose_state:
                state = json.loads(m.compose_state)
                if "quick_source" in state:
                    state.pop("quick_source", None); state.pop("quick_text", None)
                    self.db.update_message(m.id, compose_state=json.dumps(state, ensure_ascii=False))
                    if self._quick_draft_id == m.id:
                        self._quick_draft_id = None
                        self._quick_save_timer.stop()
                        with QSignalBlocker(self.quick.edit):
                            self.quick.clear()
        dlg = ComposeDialog(self.db, parent=self, **kw)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.sent.connect(self.reload_tree)
        dlg.saved.connect(self.reload_tree)
        dlg.show()

    def _compose(self):
        self._open_compose(account_id=self.current[0])

    def _open_selected(self):
        m = self.db.message(self._selected_id()) if self._selected_id() else None
        if m and m.folder in ("drafts", "outbox"):
            lock = self.db.account_lock(m.account_id)
            if not lock.acquire(blocking=False):
                QMessageBox.information(self, tr("Исходящие"), tr("Письмо сейчас отправляется. Дождитесь завершения."))
                return
            try:
                m = self.db.message(m.id)
                if not m:
                    return
                if m.delivery_state in ("sending", "uncertain"):
                    if QMessageBox.question(self, tr("Исходящие"), tr("Это письмо могло быть отправлено. Открыть для повторной отправки?")) != QMessageBox.Yes:
                        return
                    self.db.update_message(m.id, delivery_state="uncertain")
                self._open_compose(draft_id=m.id)
            except ValueError as error:
                QMessageBox.warning(self, tr("Исходящие"), str(error))
            finally:
                lock.release()
        else:
            self._reply()

    def _download_history(self):
        for acc in self.db.accounts(only_enabled=True):
            self.db.set_setting(f"history:{acc.id}", "running")
        if self.syncing:
            self._pending_mail.update(acc.id for acc in self.db.accounts(only_enabled=True))
        else:
            self.check_mail(manual=True)

    def _pause_history(self):
        self._history_cancel.set()
        for acc in self.db.accounts():
            if self.db.get_setting(f"history:{acc.id}") in ("running", "complete"):
                self.db.set_setting(f"history:{acc.id}", "paused")
        self.status.showMessage(tr("Загрузка истории приостановлена. Уже скачанные письма сохранены."), 15000)

    def _backup_mail(self):
        from ..backup import create_backup
        name, _ = QFileDialog.getSaveFileName(self, tr("Создать резервную копию"),
                                             str(Path.home() / f"SimpleMail-{datetime.now():%Y-%m-%d}.smbak"), "SimpleMail (*.smbak)")
        if not name:
            return
        self.acts["backup"].setEnabled(False)

        def done(path):
            if self._closing:
                return
            self.acts["backup"].setEnabled(True)
            self.status.showMessage(tr("Резервная копия сохранена: {0}", path), 30000)

        def fail(error):
            if self._closing:
                return
            self.acts["backup"].setEnabled(True)
            show_error(self, tr("Ошибка резервного копирования"), error)

        run_task(create_backup, self.db, name, on_done=done, on_error=fail,
                 on_progress=lambda text: self.status.showMessage(text))

    def _restore_backup(self):
        from ..backup import prepare_restore
        name, _ = QFileDialog.getOpenFileName(self, tr("Восстановить резервную копию"), "", "SimpleMail (*.smbak)")
        if not name:
            return
        if QMessageBox.question(self, tr("Восстановление"), tr("Заменить текущую почту и настройки данными из резервной копии? Перед заменой будет сохранена страховочная копия. Программа перезапустится.")) != QMessageBox.Yes:
            return
        for dialog in list(self.findChildren(ComposeDialog)):
            dialog.close()
            if dialog.isVisible():
                return
        self.timer.stop()
        self._mail_timer.stop()
        self.mail_watch.stop()
        self._history_cancel.set()
        self.setEnabled(False)
        self.acts["restore_backup"].setEnabled(False)

        def done(stage):
            if self._closing:
                return
            for dialog in list(self.findChildren(ComposeDialog)):
                dialog.close()
                if dialog.isVisible():
                    self.acts["restore_backup"].setEnabled(True)
                    return
            command = [sys.executable]
            if not getattr(sys, "frozen", False):
                command.append(str(Path(__file__).resolve().parents[2] / "main.py"))
            command += ["--data", str(Path(self.db.path).resolve().parent), "--install-restore", stage, "--wait-pid", str(os.getpid())]
            try:
                subprocess.Popen(command, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as error:
                from ..errors import explain
                fail(explain(error))
                return
            self.close()

        def fail(error):
            if self._closing:
                return
            self.setEnabled(True)
            self._history_cancel.clear()
            self._restart_timer()
            self.mail_watch.restart()
            self.acts["restore_backup"].setEnabled(True)
            show_error(self, tr("Восстановление не выполнено"), error)

        run_task(prepare_restore, self.db, name, on_done=done, on_error=fail,
                 on_progress=lambda text: self.status.showMessage(text))

    @staticmethod
    def _quote_header(m) -> str:
        date = datetime.fromtimestamp(m.date_ts).strftime("%d.%m.%Y %H:%M") if m.date_ts else ""
        return tr("{0}, {1} написал(а):", date, m.sender)

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
        header = tr("-------- Пересылаемое сообщение --------\nОт: {0}\nКому: {1}\nТема: {2}", m.sender, m.recipients, m.subject)
        subj = m.subject if m.subject.lower().startswith("fwd:") else "Fwd: " + m.subject
        self._open_compose(account_id=m.account_id, subject=subj, quote_header=header.replace("\n", " | "),
                           quote_text=m.body_text, attachments=self.db.message_attachments(m.id))

    def _mark_spam(self, is_spam: bool):
        ids = self._selected_ids()
        self._change_messages(ids, "spam" if is_spam else "not_spam")

    def _delete(self):
        ids = self._selected_ids()
        if not ids or self._mail_busy:
            return
        folders = self._selected_folders()
        view = "trash" if all(folder == "trash" for folder in folders.values()) else None
        if view == "trash":
            if QMessageBox.question(self, tr("Удаление"), tr("Удалить навсегда писем: {0}?", len(ids))) != QMessageBox.Yes:
                return
        ids = ids if view == "trash" else [mid for mid in ids if folders.get(mid) != "trash"]
        self._change_messages(ids, "purge" if view == "trash" else "trash")

    def _restore_selected(self):
        ids = [mid for mid, folder in self._selected_folders().items() if folder == "trash"]
        self._change_messages(ids, "restore")

    def _undo_delete(self):
        self._change_messages(list(self._undo_delete_ids), "restore")

    def _change_messages(self, ids, kind):
        if not ids or self._mail_busy:
            return
        self._mail_busy = True
        self._update_actions()
        self.status.showMessage(tr("Выполняется операция с письмами…"))

        def done(result):
            if self._closing:
                return
            changed, errors = result
            self._mail_busy = False
            if kind == "trash" and changed:
                self._undo_delete_ids = changed
            elif kind != "trash":
                self._undo_delete_ids = [mid for mid in self._undo_delete_ids if mid not in changed]
            self.reload_tree()
            self._update_actions()
            text = {"trash": tr("Перемещено в корзину: {0}. Отмена: Ctrl+Z", len(changed)),
                    "restore": tr("Восстановлено писем: {0}", len(changed)),
                    "purge": tr("Удалено навсегда писем: {0}", len(changed)),
                    "spam": tr("Отмечено писем: {0}. Фильтр дообучен.", len(changed)),
                    "not_spam": tr("Отмечено писем: {0}. Фильтр дообучен.", len(changed))}[kind]
            self.status.showMessage(text, 15000)
            if errors:
                show_error(self, tr("Операция с письмами не завершена"), errors[0])

        def fail(error):
            if self._closing:
                return
            self._mail_busy = False
            self._update_actions()
            show_error(self, tr("Операция с письмами не завершена"), error)

        operation = {"trash": move_to_trash, "restore": restore_message, "purge": delete_permanently,
                     "spam": lambda db, mid: set_spam_state(db, mid, True),
                     "not_spam": lambda db, mid: set_spam_state(db, mid, False)}[kind]
        run_task(change_messages, self.db, ids, operation, on_done=done, on_error=fail)

    def _reorder_accounts(self, ids: list[int]):
        # Отложенный drop может прийти после добавления/удаления ящика.
        if set(ids) != {account.id for account in self.db.accounts()}:
            return
        self.db.set_account_order(ids)
        self.reload_tree()

    def _manage_accounts(self):
        dialog = AccountsDialog(self.db, self)
        dialog.order_changed.connect(self.reload_tree)
        dialog.exec()
        self._pending_mail.clear()
        self.mail_watch.restart()
        self.reload_tree()
        self.check_mail()

    def _signatures(self):
        from .signatures_dialog import SignaturesDialog
        SignaturesDialog(self.db, parent=self).exec()

    def _spam_settings(self):
        SpamDialog(self.db, self).exec()

    def _select_language(self, code):
        if code not in LANGUAGES or code == self.db.get_setting("language", language()):
            return
        self.db.set_setting("language", code)
        if code != language():
            QMessageBox.information(
                self, tr("Язык интерфейса изменён"),
                tr("Выбран язык: {0}. Изменение вступит в силу после перезапуска SimpleMail.", LANGUAGES[code]))

    def _toggle_autostart(self, enabled):
        try:
            set_startup_enabled(enabled, Path(self.db.path).resolve().parent)
        except OSError as error:
            self.acts["autostart"].setChecked(startup_enabled())
            QMessageBox.warning(self, tr("Автозапуск"), tr("Не удалось изменить автозапуск: {0}", str(error)))

    def _interval_settings(self):
        cur = int(self.db.get_setting("check_interval_min", "10"))
        val, ok = QInputDialog.getInt(self, tr("Автопроверка"), tr("Проверять почту каждые N минут (0 — выкл.):"),
                                      cur, 0, 1440)
        if ok:
            self.db.set_setting("check_interval_min", val)
            self._restart_timer()
