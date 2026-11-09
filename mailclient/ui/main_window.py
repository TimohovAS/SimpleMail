"""Главное окно."""
from __future__ import annotations

from ..i18n import LANGUAGES, language, spam_reason, tr

import html
import threading
import json
import re
import os
import subprocess
import sys
from functools import lru_cache
from datetime import datetime, timedelta
from pathlib import Path

from email.utils import getaddresses, parseaddr

from PySide6.QtCore import QDate, QItemSelection, QItemSelectionModel, QModelIndex, QSignalBlocker, QSize, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices, QFont, QFontMetrics, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QFileDialog, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QProgressBar, QSplitter, QTableWidget, QTableWidgetItem, QToolBar,
    QTreeWidgetItem, QVBoxLayout, QWidget, QHBoxLayout, QInputDialog, QToolButton, QButtonGroup, QMenu,
)
from PySide6.QtCore import QByteArray
from PySide6.QtGui import QActionGroup

from .. import __version__

from ..db import MARK_ANSWERED, MARK_FORWARDED, Database
from ..delivery import deliver_outbox
from ..mailops import change_messages, clear_count, delete_permanently, empty_spam, empty_trash, move_to_trash, restore_message, set_spam_state
from ..protocols import build_message, mark_and_push, redirect_message, send_message, set_server_flag, set_server_flags, sync_account
from ..spam import SpamFilter
from ..sync import fetch_message_body
from ..storage import attachments_dir
from ..windows_integration import set_startup_enabled, startup_enabled, startup_supported
from .account_dialog import AccountEditDialog, AccountsDialog
from .compose_dialog import ComposeDialog
from .spam_dialog import SpamDialog
from .account_tree import AccountTree
from .tray import MailTray
from .error_log import ErrorLog, ErrorLogDialog
from .messages import show_error
from .attachments_bar import AttachmentsBar
from .header import MessageHeader, QuickReply, human_date
from .icons import app_icon, icon
from .viewer import MessageView, build_thread_page, message_style, prepare_message, thread_style
from .worker import run_task
from .mail_watch import MailWatch
from .message_table import ROLE_UNREAD, MessageTable

VIEWS = [("inbox", "Входящие"), ("sent", "Отправленные"), ("drafts", "Черновики"),
         ("outbox", "Исходящие"), ("archive", "Локальный архив"), ("spam", "Спам"), ("trash", "Корзина")]
VIEW_ICONS = {"inbox": "inbox", "spam": "shield-alert", "sent": "send", "trash": "trash-2"}
ROLE_ID = Qt.UserRole
ROLE_SORT = Qt.UserRole + 1
ROLE_TITLE = Qt.UserRole + 10  # название папки в дереве без счётчика
FLAG_COLOR = "#dc2626"

# колонки списка писем: (ключ, заголовок)
COLUMNS = [("att", ""), ("from", "От"), ("to", "Кому"), ("subject", "Тема"), ("date", "Получено"), ("size", "Размер"),
           ("account", "Ящик"), ("folder", "Папка"), ("status", "Состояние"),
           ("flag", ""),   # флажок «Важное»
           ("check", "")]  # флажки выбора писем
COL = {k: i for i, (k, _) in enumerate(COLUMNS)}
# Порядок колонок на экране по умолчанию (при первом запуске и по «Порядок колонок по умолчанию»).
DEFAULT_COLUMN_ORDER = ["check", "att", "flag", "from", "to", "subject", "date", "size", "account", "folder", "status"]
LAYOUTS = [("classic", "Классическая: список сверху, письмо снизу"),
           ("wide", "Широкая: письмо справа"),
           ("list", "Только список")]


def mark_text(marks: int) -> str:
    """Подсказка к значку письма в списке: ответили, переслали или и то и другое."""
    if marks & MARK_ANSWERED and marks & MARK_FORWARDED:
        return tr("Вы ответили на это письмо и переслали его")
    return tr("Вы ответили на это письмо") if marks & MARK_ANSWERED else tr("Письмо переслано")


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


def _mark_spam_op(db, mid):
    return set_spam_state(db, mid, True)


def _mark_not_spam_op(db, mid):
    return set_spam_state(db, mid, False)


def _mail_operation(kind):
    return {"trash": move_to_trash, "restore": restore_message, "purge": delete_permanently,
            "spam": _mark_spam_op, "not_spam": _mark_not_spam_op}[kind]


def _finish_queued_operations(db, queued):
    for ids, kind in queued:
        change_messages(db, ids, _mail_operation(kind))


class MainWindow(QMainWindow):
    closed = Signal()  # окно действительно закрыто (не свёрнуто в трей) — main.py завершает программу
    profile_changed = Signal()  # первый сохранённый ящик закрепляет профиль для следующих запусков
    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.spam = SpamFilter(db)
        self.current = (None, "inbox")       # (account_id, view)
        self.syncing = 0
        self._sync_accounts = set()
        self._mail_busy = False
        self._mail_queue = []  # операции с письмами по очереди: [(ids, kind)]
        self._hidden_ids = set()  # письма, убранные из списка до конца операции на сервере
        self._undo_delete_ids = []
        self._history_cancel = threading.Event()
        self._history_not_now = set()  # «Не сейчас» в вопросе о старых письмах — до перезапуска
        self._sync_cancel = threading.Event()
        self._search_generation = 0
        self._search_cancel = threading.Event()
        self._unread_counts = {}
        self._counts_running = False
        self._counts_pending = False
        self._body_requests = {}
        self._body_errors = {}
        self._message_generation = 0
        self._message_loading = None
        self._rows_generation = 0
        self._list_target_loading = False
        self._sync_refresh_accounts = set()
        self._sync_refresh_ids = set()
        self._sync_refresh_pending = False
        self._sync_refresh_structural = False
        self._sync_refresh_timer = QTimer(self)
        self._sync_refresh_timer.setSingleShot(True)
        self._sync_refresh_timer.setInterval(250)
        self._sync_refresh_timer.timeout.connect(self._refresh_sync_messages)
        self._thread_click = None         # письмо, по которому щёлкнули на странице переписки
        self._thread_generation = 0
        self._select_after_reload = None  # какое письмо выделить при следующей загрузке списка
        self._search_limit = 500
        self._quick_draft_id = None
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._run_search)
        self._closing = False
        self._pending_mail = set()
        self._inbox_only = set()  # ящики, где по сигналу IDLE изменились только «Входящие»
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
        self.f_flagged = QToolButton(); self.f_flagged.setText(tr("Важные")); self.f_flagged.setCheckable(True)
        self.f_flagged.setIcon(icon("flag", FLAG_COLOR)); self.f_flagged.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.f_flagged.setToolTip(tr("Показывать только письма с флажком «Важное»"))
        grp = QButtonGroup(self); grp.setExclusive(True); grp.addButton(self.f_all); grp.addButton(self.f_unread)
        grp.addButton(self.f_flagged)
        grp.buttonClicked.connect(lambda _: self._apply_search())
        search_row = QHBoxLayout(); search_row.setContentsMargins(0, 0, 0, 0)
        search_row.addWidget(self.search, 1); search_row.addWidget(self.f_all); search_row.addWidget(self.f_unread)
        search_row.addWidget(self.f_flagged)
        filters_row = QHBoxLayout()
        filters_row.addWidget(self.search_scope)
        for widget in (self.date_from_on, self.date_from, self.date_to_on, self.date_to):
            filters_row.addWidget(widget)
        filters_row.addStretch(1)

        self.table = MessageTable(0, len(COLUMNS), COL["check"])
        self.table.setHorizontalHeaderLabels([tr(c[1]) for c in COLUMNS])
        self.table.horizontalHeaderItem(COL["att"]).setIcon(icon("paperclip", "#6b7280"))
        self.table.horizontalHeaderItem(COL["att"]).setToolTip(tr("Вложения"))
        self.table.horizontalHeaderItem(COL["flag"]).setIcon(icon("flag", "#6b7280"))
        self.table.horizontalHeaderItem(COL["flag"]).setToolTip(tr("Важное"))
        self.table.horizontalHeaderItem(COL["check"]).setToolTip(tr("Выделить все письма или снять выделение"))
        h = self.table.horizontalHeader()
        h.setSectionsMovable(True)
        h.setSectionResizeMode(QHeaderView.Interactive)  # ширину любой колонки можно менять мышью
        h.setStretchLastSection(False)  # ширину последней колонки тоже можно менять; справа может остаться пустое место
        h.setMinimumSectionSize(24)
        for key, width in (("check", 24), ("att", 26), ("flag", 26), ("from", 200), ("to", 170), ("subject", 420), ("date", 130), ("size", 70)):
            self.table.setColumnWidth(COL[key], width)
        h.setSectionResizeMode(COL["check"], QHeaderView.Fixed)
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
        self.table.doubleClicked.connect(lambda index: index.column() != COL["check"] and self._open_selected())
        self.table.cellClicked.connect(self._cell_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._message_menu)

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
        self.header.compose_to.connect(lambda to: self._open_compose(account_id=self.current[0], to=to))
        self.header.thread_toggled.connect(lambda on: self.acts["thread_view"].setChecked(on) or self._toggle_thread(on))
        self.viewer = MessageView()
        self.viewer.remote_allowed_for_sender.connect(self._trust_sender_images)
        self.viewer.remote_loaded.connect(self._remember_message_images)
        self.viewer.mailto_requested.connect(lambda params: self._open_compose(account_id=self.current[0], **params))
        self.viewer.link_blocked.connect(lambda url: self.status.showMessage(
            tr("Ссылка не открыта: такие адреса могут запустить программу на компьютере ({0})", url), 15000))
        self.viewer.link_hovered.connect(self._show_link_target)
        self.viewer.state_changed.connect(lambda: hasattr(self, "acts") and self._update_actions())
        self.viewer.thread_order_changed.connect(lambda on: self.acts["thread_newest"].setChecked(on)
                                                 or self.db.set_setting("thread_newest_first", int(on)))
        self.viewer.thread_message_clicked.connect(self._activate_thread_message)
        self.att_bar = AttachmentsBar()
        self.att_bar.saved.connect(lambda t: self.status.showMessage(t, 8000))
        self.body_notice = QWidget()
        notice_layout = QHBoxLayout(self.body_notice)
        self.body_notice_text = QLabel()
        self.body_notice_text.setWordWrap(True)
        self.body_notice_text.setTextFormat(Qt.PlainText)
        self.body_retry = QPushButton(tr("Повторить загрузку"))
        self.body_retry.clicked.connect(self._retry_message_body)
        notice_layout.addWidget(self.body_notice_text, 1)
        notice_layout.addWidget(self.body_retry)
        self.body_notice.hide()
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
        vb.addWidget(self.header); vb.addWidget(self.att_bar); vb.addWidget(self.body_notice); vb.addWidget(self.reply_split, 1)

        self.right = QSplitter(Qt.Vertical)
        self.right.addWidget(list_box); self.right.addWidget(view_box)
        self.right.setChildrenCollapsible(False)
        self.main_split = QSplitter(Qt.Horizontal)
        self.main_split.addWidget(self.tree); self.main_split.addWidget(self.right)
        self.main_split.setSizes([190, 1010])
        self.setCentralWidget(self.main_split)

        self.status = self.statusBar()
        self.counts_label = QLabel()
        # Индикатор идущей проверки и кнопка её остановки (раньше ход был виден только в тексте строки состояния).
        self.sync_bar = QProgressBar()
        self.sync_bar.setRange(0, 0)
        self.sync_bar.setMaximumWidth(160)
        self.sync_bar.setMaximumHeight(16)
        self.sync_bar.setTextVisible(False)
        self.sync_bar.setFormat("%v / %m")
        self.sync_bar.setToolTip(tr("Идёт проверка почты"))
        self.sync_stop = QToolButton()
        self.sync_stop.setText(tr("Остановить"))
        self.sync_stop.setIcon(icon("circle-stop"))
        self.sync_stop.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.sync_stop.setToolTip(tr("Остановить проверку почты. Уже скачанные письма сохранятся"))
        self.sync_stop.setAutoRaise(True)
        self.sync_stop.clicked.connect(self._stop_check)
        # Значок ошибок фоновых действий: не пропадает через 15 секунд, как текст в строке состояния.
        self.error_log = ErrorLog()
        self.error_btn = QToolButton()
        self.error_btn.setIcon(icon("triangle-alert"))
        self.error_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.error_btn.setAutoRaise(True)
        self.error_btn.clicked.connect(self._open_log)
        self.error_btn.hide()
        self.status.addPermanentWidget(self.error_btn)
        self.status.addPermanentWidget(self.sync_bar)
        self.status.addPermanentWidget(self.sync_stop)
        self.sync_bar.hide()
        self.sync_stop.hide()
        self.status.addPermanentWidget(self.counts_label)
        # адрес ссылки под мышью в письме — в строке состояния под областью письма (как в браузере);
        # поверх текста состояния, не меняя раскладку строки
        self.link_target = QLabel(self.status)
        self.link_target.setTextFormat(Qt.PlainText)
        self.link_target.setAutoFillBackground(True)
        self.link_target.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.link_target.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.link_target.hide()
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

        # ---- автопроверка ----
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_mail)
        self._restart_timer()
        self._mail_timer = QTimer(self)
        self._mail_timer.setSingleShot(True)
        self._mail_timer.setInterval(300)
        self._mail_timer.timeout.connect(self._check_pending_mail)
        self.mail_watch = MailWatch(self.db, self)
        self.mail_watch.changed.connect(self._idle_changed)
        self.mail_watch.unavailable.connect(self._idle_unavailable)
        self._quitting = False
        self.tray = MailTray(self)
        self.tray.activated.connect(self.bring_to_front)
        self.tray.check_requested.connect(self.check_mail_manual)
        self.tray.compose_requested.connect(self._compose)
        self.tray.quit_requested.connect(self._quit)
        self.acts["close_to_tray"].setEnabled(self.tray.available)
        self.acts["notify"].setEnabled(self.tray.available)

        self.reload_tree()
        self.mail_watch.restart()
        QTimer.singleShot(0, self.check_mail)
        self._compacting = False
        if not self.db.accounts():
            QTimer.singleShot(300, self._manage_accounts)
        self._updates_dialog = None
        from ..updates import installation_supported
        if installation_supported():
            QTimer.singleShot(8000, self._auto_check_updates)

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
        act("stop_check", tr("Остановить проверку"), self._stop_check, None, "circle-stop",
            tr("Остановить проверку почты. Уже скачанные письма сохранятся"))
        self.acts["stop_check"].setEnabled(False)
        act("history", tr("Скачать всю историю"), self._download_history, None, "download")
        act("pause_history", tr("Приостановить загрузку истории"), self._pause_history, None, "clock")
        act("backup", tr("Создать резервную копию…"), self._backup_mail, None, "download")
        act("restore_backup", tr("Восстановить резервную копию…"), self._restore_backup, None, "folder-open")
        act("compact", tr("Сжать базу писем"), self._compact_db, None, "download",
            tr("Уменьшить файл mail.db после удаления писем и вложений"))
        act("global_search", tr("Поиск по всем ящикам"), self._global_search, "Ctrl+Shift+F", "search")
        act("compose", tr("Написать письмо"), self._compose, "Ctrl+N", "mail-plus", tr("Новое письмо"))
        act("address_book", tr("Адресная книга…"), self._address_book, "Ctrl+Shift+B", "book-user",
            tr("Адресная книга: кому вы писали и кто писал вам"))
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
        act("flag", tr("Важное"), self._toggle_flag, "Insert", "flag", tr("Поставить или снять флажок «Важное» (Insert)"))
        act("select_all", tr("Выделить все письма"), self._select_all_messages, "Ctrl+A", "square-check",
            tr("Выделить все письма в списке"))
        # Ctrl+A выбирает письма только в списке, в полях ввода он по-прежнему выделяет текст
        self.acts["select_all"].setShortcutContext(Qt.WidgetShortcut)
        self.table.addAction(self.acts["select_all"])
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
                      None, "log-in", checkable=True,
                      tip=tr("SimpleMail запускается вместе с Windows без окна: значок у часов, почта проверяется"))
        startup.setEnabled(startup_supported())
        startup.setChecked(startup_enabled())
        act("exit", tr("Выход"), self._quit, "Ctrl+W", "log-out")
        act("close_to_tray", tr("Сворачивать в область уведомлений при закрытии"),
            lambda on: self.db.set_setting("close_to_tray", "1" if on else "0"), None, checkable=True,
            tip=tr("Крестик окна прячет программу к часам, проверка почты продолжается"))
        self.acts["close_to_tray"].setChecked(self.db.get_setting("close_to_tray", "0") == "1")
        act("notify", tr("Уведомлять о новых письмах"),
            lambda on: self.db.set_setting("notify_new_mail", "1" if on else "0"), None, "bell", checkable=True)
        self.acts["notify"].setChecked(self.db.get_setting("notify_new_mail", "1") == "1")
        act("about", tr("О программе"), self._about, None, "info")
        act("log", tr("Журнал ошибок"), self._open_log, None, "triangle-alert")
        act("updates", tr("Обновления…"), self._check_updates, None, "download")
        auto_update = act("auto_updates", tr("Проверять обновления при запуске"),
                          lambda on: self.db.set_setting("auto_updates", int(on)), checkable=True)
        auto_update.setChecked(self.db.get_setting("auto_updates", "1") == "1")
        act("show_folders", tr("Панель папок"), lambda on: self.tree.setVisible(on), "F9", checkable=True)
        self.acts["show_folders"].setChecked(True)
        self.layout_group = QActionGroup(self)
        for i, (key, title) in enumerate(LAYOUTS):
            a = act("layout_" + key, tr(title), lambda _=False, k=key: self._set_layout(k), f"Ctrl+{i + 1}", checkable=True)
            self.layout_group.addAction(a)
        act("quick_reply", tr("Поле быстрого ответа"), self._toggle_quick, None, checkable=True)
        act("thread_view", tr("Показывать переписку"), self._toggle_thread, "Ctrl+T", "messages-square",
            tr("Показывать в окне письма всю переписку: письма с ответами из всех папок и ящиков"), checkable=True)
        self.acts["thread_view"].setChecked(self.db.get_setting("thread_view", "1") == "1")
        act("thread_newest", tr("Новые письма переписки сверху"), self._toggle_thread_order, None, checkable=True,
            tip=tr("Порядок писем в переписке: новые сверху (старые внизу) или старые сверху"))
        self.acts["thread_newest"].setChecked(self.db.get_setting("thread_newest_first", "1") == "1")
        self.acts["quick_reply"].setChecked(self.db.get_setting("quick_reply", "1") == "1")
        act("labels", tr("Подписи под значками"), self._toggle_labels, None, checkable=True)
        self.acts["labels"].setChecked(self.db.get_setting("toolbar_labels", "1") == "1")

    def _build_menus(self):
        mb = self.menuBar()
        a = self.acts
        m = mb.addMenu(tr("&Файл"))
        for k in ("compose", "address_book", "check", "stop_check", "history", "pause_history", None, "save_eml", "save_att", None, "backup", "restore_backup", "compact", None, "exit"):
            m.addSeparator() if k is None else m.addAction(a[k])
        m = mb.addMenu(tr("&Письмо"))
        for k in ("edit_draft", "reply", "reply_all", "forward", "redirect", None, "select_all", None, "flag", "read", "unread", None, "spam", "not_spam", None,
                  "images", None, "delete", "undo_delete", "restore"):
            m.addSeparator() if k is None else m.addAction(a[k])
        m = mb.addMenu(tr("&Вид"))
        lm = m.addMenu(tr("Раскладка окна"))
        for key, _ in LAYOUTS:
            lm.addAction(a["layout_" + key])
        for k in (None, "show_folders", "quick_reply", "thread_view", "thread_newest", "labels", None, "search", "global_search"):
            m.addSeparator() if k is None else m.addAction(a[k])
        m = mb.addMenu(tr("&Настройки"))
        for k in ("accounts", "signatures", "spam_settings", "interval", "autostart", "close_to_tray", "notify", "auto_updates"):
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
        m.addAction(a["log"])
        m.addAction(a["updates"])
        m.addAction(a["about"])

    def _build_toolbar(self):
        tb = QToolBar(tr("Основная панель"))
        tb.setObjectName("main_toolbar")
        tb.setMovable(False)
        tb.setIconSize(QSize(24, 24))
        self.toolbar = tb
        self.addToolBar(tb)
        short = {"check": tr("Проверить"), "compose": tr("Написать"), "reply_all": tr("Всем"), "spam": tr("Спам"),
                 "select_all": tr("Выделить все"), "read": tr("Прочитано"), "unread": tr("Непрочит."), "images": tr("Картинки"), "save_att": tr("Вложения"),
                 "undo_delete": tr("Отменить"), "restore": tr("Восстановить"), "address_book": tr("Адреса")}
        for k in ("check", None, "compose", "reply", "reply_all", "forward", None, "select_all",
                  "flag", "spam", "not_spam", "read", "unread", "delete", "undo_delete", "restore", None, "images", "save_att", None, "address_book"):
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
        ready = one and self._shown.body_downloaded
        views = set(self._selected_folders().values())
        view = next(iter(views)) if len(views) == 1 else None
        for k in ("reply", "reply_all", "forward"):
            self.acts[k].setEnabled(ready and view not in ("drafts", "outbox"))
        self.acts["save_eml"].setEnabled(ready)
        self.acts["edit_draft"].setEnabled(one and view in ("drafts", "outbox"))
        # Удаление, спам и восстановление ставятся в очередь: кнопки доступны и во время операции.
        for k in ("delete", "unread", "read"):
            self.acts[k].setEnabled(bool(ids))
        self.acts["flag"].setEnabled(bool(ids) and not views & {"drafts", "outbox"})
        self.acts["spam"].setEnabled(bool(ids) and view == "inbox")
        self.acts["not_spam"].setEnabled(bool(ids) and (view == "spam" or self.current[1] == "spam"))
        self.acts["undo_delete"].setEnabled(bool(self._undo_delete_ids))
        self.acts["restore"].setEnabled(bool(ids) and view == "trash")
        self.acts["images"].setEnabled(one and self.viewer.banner.isVisible())
        self.acts["save_att"].setEnabled(one and self.att_bar.isVisible())
        self.acts["redirect"].setEnabled(ready and view not in ("drafts", "outbox"))

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

    def _show_link_target(self, url: str):
        label = self.link_target
        if not url:
            label.hide()
            return
        bar = self.status
        left = max(0, bar.mapFromGlobal(self.viewer.mapToGlobal(self.viewer.rect().topLeft())).x())
        right = bar.width() - 4
        for widget in (self.error_btn, self.sync_bar, self.sync_stop, self.counts_label):
            if widget.isVisible():
                right = min(right, widget.x() - 8)
        if right - left < 150:  # область письма узкая или левее строки состояния — берём всю строку
            left = 4
        label.setGeometry(left, 0, max(0, right - left), bar.height())
        label.setText(label.fontMetrics().elidedText(url, Qt.ElideMiddle, max(0, right - left - 4)))
        label.show()
        label.raise_()

    def _toggle_quick(self, on: bool):
        self.db.set_setting("quick_reply", "1" if on else "0")
        self.quick.setVisible(on and self._shown is not None and self._shown.body_downloaded
                              and self._shown.folder not in ("sent", "drafts", "outbox"))

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
        hh = self.table.horizontalHeader()
        if (hs := load("header_state")):
            hh.restoreState(hs)
            hh.setSectionResizeMode(QHeaderView.Interactive)  # старые настройки могли «закрепить» ширину темы
            hh.setStretchLastSection(False)  # растянутую последнюю колонку нельзя сузить мышью
        else:
            self._default_column_order()
            QTimer.singleShot(0, self._fit_subject_column)  # первый запуск: тема занимает свободное место
        hh.setSectionResizeMode(COL["check"], QHeaderView.Fixed)
        self.table.setColumnWidth(COL["check"], 24)
        self._shown = None
        self._set_layout(self.db.get_setting("layout", "classic") or "classic")

    def _default_column_order(self):
        """Расставляет колонки списка в порядке по умолчанию; ширина и видимость не меняются."""
        hh = self.table.horizontalHeader()
        for visual, key in enumerate(DEFAULT_COLUMN_ORDER):
            hh.moveSection(hh.visualIndex(COL[key]), visual)

    def _fit_subject_column(self):
        others = sum(self.table.columnWidth(i) for i in range(self.table.columnCount())
                     if i not in (COL["subject"], COL["size"]) and not self.table.isColumnHidden(i))
        free = self.table.viewport().width() - others - 80
        self.table.setColumnWidth(COL["subject"], max(250, free))

    def _save_ui(self):
        if not self.isVisible() and not hasattr(self, "_layout"):
            return
        if not getattr(self, "_was_shown", False):
            return  # окно ещё не открывали (запуск в трее): сохранённый вид не трогаем
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

    def can_start_hidden(self) -> bool:
        """Запуск без окна возможен, только если есть значок в трее и ящик уже настроен."""
        return self.tray.available and bool(self.db.accounts())

    def showEvent(self, e):
        super().showEvent(e)
        self._was_shown = True

    def bring_to_front(self):
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        self._quitting = True
        if not self.close():
            self._quitting = False

    def closeEvent(self, event):
        if not self._quitting and self.tray.available and self.acts["close_to_tray"].isChecked():
            # Крестик прячет окно к часам: программа продолжает проверять почту.
            event.ignore()
            self._save_ui()
            self.hide()
            if self.db.get_setting("tray_hint_shown") != "1":
                self.db.set_setting("tray_hint_shown", "1")
                self.tray.notify("SimpleMail", tr("Программа продолжает работать и проверять почту. "
                                                  "Выход — правой кнопкой по значку или «Файл → Выход»."))
            return
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
        if self._mail_queue:
            # Удаления, которые ещё ждали очереди, письма уже убраны из списка: доводим их до конца.
            queued, self._mail_queue = self._mail_queue, []
            run_task(_finish_queued_operations, self.db, queued)
        self._history_cancel.set()
        self._sync_cancel.set()
        self._search_timer.stop()
        self._search_generation += 1
        self._search_cancel.set()
        self._sync_refresh_timer.stop()
        for body_cancel in self._body_requests.values():
            body_cancel.set()
        self.timer.stop()
        self._mail_timer.stop()
        self._ui_save_timer.stop()
        self.mail_watch.stop()
        if self._updates_dialog is not None:
            self._updates_dialog.reject()
        self._save_ui()
        self.viewer.shutdown()
        self.tray.hide()
        super().closeEvent(event)
        self.closed.emit()

    # ---------- перенаправление и быстрый ответ ----------
    def _redirect(self):
        m = getattr(self, "_shown", None)
        if not m or not m.body_downloaded or not m.raw:
            return
        from .address_book import ask_addresses
        to = ask_addresses(self.db, self, tr("Перенаправить"),
                           tr("Кому перенаправить письмо (оно уйдёт без изменений, от исходного отправителя):"))
        if not to:
            return
        acc = self.db.account(m.account_id)

        def redirected(_):
            self.status.showMessage(tr("Письмо перенаправлено: {0}", to.strip()), 5000)
            # Значок «переслано»; на сервере — $Forwarded (нет связи — допишет проверка почты).
            run_task(mark_and_push, self.db, m.id, MARK_FORWARDED, on_done=lambda _: self.reload_messages(),
                     on_error=lambda _: self.reload_messages())
        run_task(redirect_message, self.db, acc, m.raw, to.strip(), on_done=redirected,
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
        to, references = self._reply_address(m), self._references(m)
        em = build_message(acc, to, self._re(m.subject), body, in_reply_to=m.message_id, references=references)
        state = dict(to=to, cc="", mode="plain", html=html.escape(body).replace("\n", "<br>"),
                     in_reply_to=m.message_id, references=references, quick_source=m.id, quick_text=text,
                     mark_source=[m.id, MARK_ANSWERED])
        return em, state

    def _save_quick_draft(self, queued=False):
        m = getattr(self, "_shown", None)
        if not m or not m.body_downloaded or m.folder in ("drafts", "outbox", "sent"):
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
        if not m or not m.body_downloaded:
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
            if result == "failed":
                reason = (self.db.message(draft_id).delivery_error or "") if self.db.message(draft_id) else ""
                QMessageBox.warning(self, tr("Ответ не отправлен"),
                                    tr("Сервер отказался принять письмо. Оно лежит в «Исходящих» — откройте его, исправьте адрес и отправьте снова.\n\n{0}", reason))
                self.reload_tree()
                return
            text = (tr("Ответ отправлен") if result == "sent" else
                    tr("Письмо сохранено в исходящих и будет отправлено при подключении.") if result == "queued" else
                    tr("Результат отправки неизвестен. Проверьте доставку перед повторной отправкой."))
            self.status.showMessage(text, 15000)
            self.reload_tree()

        def fail(err):
            self.quick.set_busy(False)
            show_error(self, tr("Ответ не отправлен"), err)

        run_task(deliver_outbox, self.db, draft_id, on_done=done, on_error=fail)

    def _compact_db(self):
        if self.syncing or self._compacting:
            self.status.showMessage(tr("Дождитесь окончания проверки почты и повторите."), 10000)
            return
        size = Path(self.db.path).stat().st_size
        self._compacting = True
        self.status.showMessage(tr("Сжатие базы писем… Это может занять несколько минут."))

        def done(_):
            self._compacting = False
            after = Path(self.db.path).stat().st_size
            self.status.showMessage(tr("База писем сжата: {0} → {1} МБ", round(size / 1048576, 1), round(after / 1048576, 1)), 15000)

        def fail(err):
            self._compacting = False
            show_error(self, tr("Сжать базу писем"), err)

        run_task(self.db.compact, on_done=done, on_error=fail)

    def _open_log(self):
        from ..logs import log_dir
        folder = log_dir(Path(self.db.path).parent)
        folder.mkdir(parents=True, exist_ok=True)
        ErrorLogDialog(self.error_log, folder / "simplemail.log", self).exec()
        self._update_error_button()

    def _update_error_button(self):
        n = self.error_log.unseen
        self.error_btn.setVisible(bool(n))
        self.error_btn.setText(tr("Ошибки: {0}", n))
        last = self.error_log.entries[-1].error.short() if self.error_log.entries else ""
        self.error_btn.setToolTip(tr("Последняя: {0}\nНажмите, чтобы открыть журнал ошибок", last))

    def _about(self):
        QMessageBox.about(self, tr("О программе"),
                          tr("<b>SimpleMail {0}</b><br>Простой почтовый клиент.<br><br>Значки: Lucide (лицензия ISC).", __version__))

    def _auto_check_updates(self):
        if not self._closing and self.db.get_setting("auto_updates", "1") == "1":
            self._check_updates(automatic=True)

    def _check_updates(self, _checked=False, *, automatic=False):
        if self._closing:
            return
        if self._updates_dialog is None:
            from .updates_dialog import UpdatesDialog
            self._updates_dialog = UpdatesDialog(self, self._install_update)
        dialog = self._updates_dialog
        if not automatic:
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
        if dialog.release is None and not dialog.busy:
            dialog.check(automatic=automatic)

    def _install_update(self, installer, release):
        if self._closing:
            return False
        from PySide6.QtWidgets import QApplication
        from .worker import _active
        if self._closing or _active or self.syncing or self._mail_busy:
            QMessageBox.information(self, tr("Обновления SimpleMail"),
                                    tr("Дождитесь завершения почтовых операций и повторите установку."))
            return False
        if QMessageBox.question(self, tr("Обновления SimpleMail"),
                                tr("Установить версию {0}? SimpleMail закроется, черновики будут сохранены. После установки можно снова запустить программу.", release.version),
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return False
        if self._closing:
            return False
        if _active or self.syncing or self._mail_busy:
            QMessageBox.information(self, tr("Обновления SimpleMail"),
                                    tr("Дождитесь завершения почтовых операций и повторите установку."))
            return False
        app = QApplication.instance()
        app._pending_update = (installer, release, Path(self.db.path).resolve().parent, language())
        self._quitting = True  # установка должна закрыть программу, даже при включённом трее
        if not self.close():  # editor refused to close or a draft could not be saved
            self._quitting = False
            app._pending_update = None
            return False
        return True

    def _restart_timer(self):
        minutes = int(self.db.get_setting("check_interval_min", "10"))
        self.timer.stop()
        if minutes > 0:
            self.timer.start(minutes * 60_000)

    # ================= дерево ящиков =================
    def _account_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        if item.parent() is not None:
            self._folder_menu(item, pos)
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
        downloaded, total = self.db.history_progress(account_id)
        if downloaded < total:  # пункт нужен, только пока на сервере есть нескачанные письма
            rest = menu.addAction(icon("download"), tr("Скачать остальные письма (скачано {0} из {1})",
                                                       downloaded, total))
            rest.setEnabled(account.enabled)
            rest.triggered.connect(lambda: self._download_account_history(account_id))
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

    def _folder_menu(self, item, pos):
        """Меню папки ящика: у «Корзины» — «Очистить корзину», у «Спама» — «Очистить спам»."""
        account_id, view = item.data(0, ROLE_ID)
        if view not in ("trash", "spam") or self.db.account(account_id) is None:
            return
        menu = QMenu(self)
        title = tr("Очистить корзину") if view == "trash" else tr("Очистить спам")
        action = menu.addAction(icon("trash-2"), title)
        action.setEnabled(clear_count(self.db, account_id, view) > 0 and not self._mail_busy)
        action.triggered.connect(lambda: self._clear_folder(account_id, view))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _clear_folder(self, account_id, view):
        """Окончательно удалить все письма корзины или спама ящика (после подтверждения)."""
        account = self.db.account(account_id)
        count = clear_count(self.db, account_id, view) if account is not None else 0
        if not count or self._mail_busy:
            return
        if view == "trash":
            title = tr("Очистка корзины")
            question = tr("Удалить навсегда все письма из корзины ящика «{0}» (писем: {1})? "
                          "Восстановить их будет нельзя.", account.name, count)
            progress = tr("Очистка корзины ящика «{0}»…", account.name)
        else:
            title = tr("Очистка спама")
            question = tr("Удалить навсегда все письма из спама ящика «{0}» (писем: {1})? "
                          "Восстановить их будет нельзя.", account.name, count)
            progress = tr("Очистка спама ящика «{0}»…", account.name)
        answer = QMessageBox.question(self, title, question, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self._mail_busy = True
        self._update_actions()
        self.status.showMessage(progress)

        def finish():
            self._mail_busy = False
            self._undo_delete_ids = [mid for mid in self._undo_delete_ids
                                     if self.db.message(mid, with_raw=False) is not None]
            self.reload_tree()
            self._update_actions()

        def done(removed):
            if self._closing:
                return
            finish()
            text = (tr("Корзина очищена, удалено писем: {0}", removed) if view == "trash"
                    else tr("Спам очищен, удалено писем: {0}", removed))
            self.status.showMessage(text, 15000)

        def fail(error):
            if self._closing:
                return
            finish()
            show_error(self, tr("Корзина очищена не полностью") if view == "trash"
                       else tr("Спам очищен не полностью"), error)

        run_task(empty_trash if view == "trash" else empty_spam, self.db, account_id, on_done=done, on_error=fail)

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
            self.profile_changed.emit()
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
        # Свёрнутые пользователем ящики остаются свёрнутыми после проверки почты.
        collapsed = {self.tree.topLevelItem(i).data(0, ROLE_ID)[0] for i in range(self.tree.topLevelItemCount())
                     if not self.tree.topLevelItem(i).isExpanded()}
        tree_scroll = (self.tree.verticalScrollBar().value(), self.tree.horizontalScrollBar().value())
        self.tree.blockSignals(True)
        self.tree.clear()
        select_item = None
        bold = QFont(); bold.setBold(True)
        # Ящики отличаются от папок: жирный шрифт крупнее и отступ сверху.
        account_font = QFont(self.tree.font())
        account_font.setBold(True)
        if account_font.pointSizeF() > 0:
            account_font.setPointSizeF(account_font.pointSizeF() + 1.5)
        account_height = QFontMetrics(account_font).height() + 14
        for acc in self.db.accounts():
            top = QTreeWidgetItem([acc.name + ("" if acc.enabled else tr(" (выкл.)"))])
            top.setFont(0, account_font)
            top.setSizeHint(0, QSize(0, account_height))
            top.setIcon(0, icon("at-sign"))
            top.setData(0, ROLE_ID, (acc.id, None))
            self.tree.addTopLevelItem(top)
            # Everyday folders and custom server folders come before spam and
            # trash; discovered folders must not push those two into the middle.
            views = [item for item in VIEWS if item[0] not in ("spam", "trash")]
            views += [(row["view"], row["display_name"] or row["name"]) for row in self.db.server_folders(acc.id)
                      if row["view"].startswith("remote:") and self._show_remote_folder(acc.id, row)]
            views += [item for item in VIEWS if item[0] in ("spam", "trash")]
            for view, title in views:
                title = tr(title)
                unread = self._unread_counts.get((acc.id, view), 0) if self._counts_unread(view) else 0
                it = QTreeWidgetItem([f"{title} ({unread})" if unread else title])
                it.setData(0, ROLE_TITLE, title)
                if unread:
                    it.setFont(0, bold)
                it.setData(0, ROLE_ID, (acc.id, view))
                it.setIcon(0, icon(VIEW_ICONS.get(view, "folder-open")))
                top.addChild(it)
                if (acc.id, view) == keep or (select_item is None and keep[0] is None and view == "inbox"):
                    select_item = it
            top.setExpanded(acc.id not in collapsed)
        self.tree.blockSignals(False)
        if select_item:
            self.tree.setCurrentItem(select_item)
        else:
            self.table.setRowCount(0)
        if keep[0] is not None:  # после проверки почты дерево папок не прокручивается
            self.tree.doItemsLayout()
            self.tree.verticalScrollBar().setValue(tree_scroll[0])
            self.tree.horizontalScrollBar().setValue(tree_scroll[1])
        self._refresh_counts()

    def _show_remote_folder(self, account_id, row):
        """Пустые служебные папки хостинга (INBOX.Drafts, INBOX.Archive, второй Junk) не засоряют дерево."""
        from ..sync import STANDARD_REMOTE, standard_remote
        standard = standard_remote(row["name"]) or row["display_name"] in STANDARD_REMOTE.values()
        if not standard or (account_id, row["view"]) == self.current:
            return True
        return self.db.conn().execute("SELECT 1 FROM messages WHERE account_id=? AND folder=? LIMIT 1",
                                      (account_id, row["view"])).fetchone() is not None

    def _update_tray(self):
        if getattr(self, "tray", None) is not None:
            self.tray.set_unread(self._unread_counts.get((None, "inbox"), 0))

    def _notify_new_mail(self, count: int):
        """Уведомление о новых письмах, если окно не на переднем плане."""
        if not count or not self.acts["notify"].isChecked() or self.isActiveWindow():
            return
        row = self.db.conn().execute(
            "SELECT sender, subject FROM messages WHERE folder='inbox' AND is_spam=0 AND seen=0 "
            "ORDER BY date_ts DESC, id DESC LIMIT 1").fetchone()
        title = tr("Новых писем: {0}", count)
        text = f"{self._short_addr(row['sender'] or '')}: {row['subject'] or tr('(без темы)')}" if row else ""
        self.tray.notify(title, text)

    @staticmethod
    def _counts_unread(view: str) -> bool:
        """Счётчик непрочитанных — у входящих, спама и остальных серверных папок."""
        return view in ("inbox", "spam") or view.startswith("remote:")

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

    def reload_messages(self, keep_selection: bool = True, *, refresh_shown: bool = True):
        # Even a plain folder query can take hundreds of milliseconds while
        # IMAP writes bodies/FTS pages. Keep every list read off the GUI thread.
        self._run_search(keep_selection=keep_selection, refresh_shown=refresh_shown)

    def _list_view(self) -> dict:
        """Выделенные письма и прокрутка списка — чтобы вернуть их после обновления списка."""
        table = self.table
        current = table.item(table.currentRow(), 0) if table.currentRow() >= 0 else None
        top_row = table.rowAt(0)
        bar = table.verticalScrollBar()
        return {"ids": self._selected_ids(), "current": current.data(ROLE_ID) if current else None,
                "at_top": bar.value() <= bar.minimum(),
                "top": table.item(top_row, 0).data(ROLE_ID) if top_row >= 0 and table.item(top_row, 0) else None,
                "offset": table.rowViewportPosition(top_row) if top_row >= 0 else 0,
                "h": table.horizontalScrollBar().value()}

    def _restore_list_view(self, view, rows_by_id) -> bool:
        """Выделить прежние письма, не прокручивая список. False — ни одного из них в списке нет."""
        table = self.table
        keep = [rows_by_id[mid] for mid in view["ids"] if mid in rows_by_id]
        if not keep:
            return False
        model, last = table.model(), table.columnCount() - 1
        selection = QItemSelection()
        for row in keep:
            selection.select(model.index(row, 0), model.index(row, last))
        current = rows_by_id.get(view["current"], keep[0])
        selection_model = table.selectionModel()
        selection_model.setCurrentIndex(model.index(current, 0), QItemSelectionModel.NoUpdate)
        selection_model.select(selection, QItemSelectionModel.ClearAndSelect)
        return True

    def _scroll_list_back(self, view, rows_by_id):
        """Вернуть прокрутку списка: вверху — остаёмся вверху (видны новые письма), иначе на месте
        остаётся письмо, которое было первым в окне, хотя бы над ним появились новые."""
        table = self.table
        table.doItemsLayout()
        bar = table.verticalScrollBar()
        if view["at_top"]:
            bar.setValue(bar.minimum())
        elif view["top"] in rows_by_id:
            table.scrollTo(table.model().index(rows_by_id[view["top"]], 0), QAbstractItemView.PositionAtTop)
            if table.verticalScrollMode() == QAbstractItemView.ScrollPerPixel:
                bar.setValue(bar.value() - view["offset"])
        elif table.currentItem() is not None:
            table.scrollToItem(table.currentItem())
        table.horizontalScrollBar().setValue(view["h"])

    def _display_rows(self, rows, selected, view=None, *, refresh_shown=True, thread_click=None):
        self._rows_generation += 1
        generation = self._rows_generation
        existing = {self.table.item(i, 0).data(ROLE_ID) for i in range(self.table.rowCount())
                    if self.table.item(i, 0) is not None} if view is not None else set()
        visible, extra = rows[:self._search_limit], rows[self._search_limit:]
        kept = [row for row in visible if row["id"] in existing]
        added = [row for row in visible if row["id"] not in existing]
        if len(added) <= 100:
            self._apply_rows(rows, selected, view, refresh_shown=refresh_shown, thread_click=thread_click)
            return
        # Build a newly opened folder in small event-loop turns. A 500-row
        # QTableWidget contains 5,500 items; filling it in one callback alone
        # was enough to stall wheel/keyboard events, even with SQL in a worker.
        rows = kept + added + extra
        end = 0
        def batch():
            nonlocal end
            if self._closing or generation != self._rows_generation:
                return
            first = end == 0
            end = min((len(kept) if first else end) + 50, len(rows))
            now_view = view if first else self._list_view()
            now_selected = selected if first else self._selected_id()
            if now_selected is None and selected is not None:
                now_selected = selected
            self._apply_rows(rows[:end], now_selected, now_view, refresh_shown=refresh_shown if first else False,
                             thread_click=thread_click if now_selected == selected else None,
                             defer_missing_selection=end < len(rows))
            if end < len(rows):
                self.search_more.hide()
                QTimer.singleShot(0, batch)
        batch()

    def _apply_rows(self, rows, selected, view=None, *, refresh_shown=True,
                    thread_click=None, defer_missing_selection=False):
        more = len(rows) > self._search_limit
        rows = rows[:self._search_limit]
        self.search_more.setVisible(more)
        scope = self.search_scope.currentData()
        for key in ("account", "folder"):
            self.table.setColumnHidden(COL[key], scope == "folder")
        self.table.setColumnHidden(COL["status"], not any(row["delivery_state"] or not row["body_downloaded"] for row in rows))
        snapshots = {row["id"]: tuple(row) for row in rows}
        previous = getattr(self, "_row_snapshots", {}) if view is not None else {}
        incremental = view is not None and hasattr(self, "_row_snapshots")
        existing = {self.table.item(i, 0).data(ROLE_ID): i for i in range(self.table.rowCount())
                    if self.table.item(i, 0) is not None}
        changed = (not incremental or set(existing) != set(snapshots)
                   or any(previous.get(mid) != value for mid, value in snapshots.items()))
        with QSignalBlocker(self.table):
            if changed:
                self.table.setSortingEnabled(False)
            if not incremental:
                self.table.clearSelection()
                self.table.setRowCount(len(rows))
            else:
                for i in sorted((row for mid, row in existing.items() if mid not in snapshots), reverse=True):
                    self.table.removeRow(i)
                existing = {self.table.item(i, 0).data(ROLE_ID): i for i in range(self.table.rowCount())}
            bold = QFont(); bold.setBold(True)
            clip = icon("paperclip", "#6b7280")
            mark_icons = {MARK_ANSWERED: icon("reply"), MARK_FORWARDED: icon("forward"),
                          MARK_ANSWERED | MARK_FORWARDED: icon("reply-forward")}
            for i, r in enumerate(rows):
                if incremental:
                    if r["id"] in existing and previous.get(r["id"]) == snapshots[r["id"]]:
                        continue
                    i = existing.get(r["id"], self.table.rowCount())
                    if i == self.table.rowCount():
                        self.table.insertRow(i)
                marks = (r["marks"] or 0) & (MARK_ANSWERED | MARK_FORWARDED)
                unread = not r["seen"] and r["folder"] not in ("drafts", "outbox")
                values = {
                    "att": ("", 1 if r["has_att"] else 0),
                    "flag": ("", 1 if r["flagged"] else 0),
                    "check": ("", 0),
                    "from": (self._short_addr(r["sender"]), (r["sender"] or "").lower()),
                    "to": (self._short_addr(r["recipients"]), (r["recipients"] or "").lower()),
                    "subject": (r["subject"] or tr("(без темы)"), None),
                    "date": (human_date(r["date_ts"]), r["date_ts"] or 0),
                    "size": (human_size(r["size"]), r["size"] or 0),
                    "account": (r["account_name"], r["account_name"]),
                    "folder": (tr(dict(VIEWS).get(r["folder"], r["folder"].removeprefix("remote:"))), r["folder"]),
                    "status": ({"pending": tr("Ожидает подключения"), "sending": tr("Проверьте доставку"),
                                "uncertain": tr("Проверьте доставку"), "failed": tr("Не отправлено: сервер отказал"),
                                "append_pending": tr("Отправлено; копия ожидает сохранения")}.get(r["delivery_state"],
                                    tr("Содержимое ещё не загружено") if not r["body_downloaded"] else ""), r["delivery_state"] or ""),
                }
                for key, (text, sort) in values.items():
                    item = SortItem(text)
                    item.setData(ROLE_ID, r["id"])
                    item.setData(ROLE_SORT, sort)
                    item.setData(ROLE_UNREAD, int(unread))
                    item.setData(ROLE_SORT + 2, r["folder"])
                    if unread:
                        item.setFont(bold)
                    if key in ("from", "to"):
                        item.setToolTip(r["sender"] if key == "from" else r["recipients"])
                    elif key == "status" and r["delivery_error"]:
                        item.setToolTip(r["delivery_error"])
                    elif r["spam_reason"]:
                        item.setToolTip(tr("Спам-фильтр: ") + spam_reason(r["spam_reason"]))
                    elif key == "subject" and marks:
                        item.setToolTip(mark_text(marks))
                    if key == "subject" and marks:
                        item.setIcon(mark_icons[marks])
                    if key == "att" and r["has_att"]:
                        item.setIcon(clip)
                    if key == "flag":
                        self._paint_flag(item, bool(r["flagged"]))
                    if key == "size":
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.table.setItem(i, COL[key], item)
            if changed:
                self.table.setSortingEnabled(True)
            visible_rows = [i for i in range(self.table.rowCount()) if not self.table.isRowHidden(i)]
            rows_by_id = {self.table.item(i, 0).data(ROLE_ID): i for i in visible_rows}
            if not (view and self._restore_list_view(view, rows_by_id)):
                # Выделяется только письмо, которое уже было открыто. Первое письмо списка само не
                # открывается (при запуске, переходе в ящик или папку, после поиска): иначе новое
                # письмо становилось прочитанным без действия пользователя.
                select_row = rows_by_id.get(selected, -1) if selected is not None else -1
                if select_row >= 0:
                    self.table.selectRow(select_row)
                else:
                    self.table.setCurrentIndex(QModelIndex())  # стрелка «вниз» начинает с первого письма
            if view:
                self._scroll_list_back(view, rows_by_id)
        self._row_snapshots = snapshots
        # Background bodies cannot change an unrelated open message. Keep its
        # raw file and thread work out of the UI refresh path entirely.
        shown_id = self._shown.id if self._shown is not None else None
        defer = defer_missing_selection and selected is not None and selected not in rows_by_id
        if not defer and (refresh_shown or self._selected_id() != shown_id):
            previous_click = self._thread_click
            self._thread_click = thread_click
            try:
                self._on_message_selected()
            finally:
                self._thread_click = previous_click
        else:
            self._update_actions()
            self._update_counts()

    @staticmethod
    @lru_cache(maxsize=4096)
    def _short_addr(value: str) -> str:
        """«Иван Петров <ivan@x.ru>, …» -> «Иван Петров, …» (короче для списка)."""
        parts = [n or a for n, a in getaddresses([value or ""]) if n or a]
        return ", ".join(parts) if parts else (value or "")

    def _update_counts(self):
        total = sum(1 for i in range(self.table.rowCount()) if not self.table.isRowHidden(i))
        unread = sum(1 for i in range(self.table.rowCount())
                     if not self.table.isRowHidden(i) and self.table.item(i, 0).data(ROLE_UNREAD))
        sel = len(self._selected_ids())
        text = tr("Писем: {0}", total) + (tr(", непрочитанных: {0}", unread) if unread else "")
        self.counts_label.setText(tr("Выбрано: {0} · ", sel) + text if sel > 1 else text)

    def _header_menu(self, pos):
        menu = QMenu(self)
        for key, title in COLUMNS:
            if key == "subject":
                continue
            a = menu.addAction(tr(title) if title else {"flag": tr("Важное"), "check": tr("Флажки выбора")}.get(key, tr("Вложения")))
            a.setCheckable(True)
            a.setChecked(not self.table.isColumnHidden(COL[key]))
            a.toggled.connect(lambda on, c=COL[key]: self.table.setColumnHidden(c, not on))
        menu.addSeparator()
        menu.addAction(tr("Порядок колонок по умолчанию"), self._default_column_order)
        menu.exec(self.table.horizontalHeader().mapToGlobal(pos))

    def _apply_search(self, *_):
        if not hasattr(self, "table"):
            return
        self._search_generation += 1
        self._rows_generation += 1
        self._list_target_loading = False
        self._search_cancel.set()
        self._search_limit = 500
        if self.search.text().strip() or self.search_scope.currentData() != "folder":
            self._search_timer.start()
        else:
            self.reload_messages()

    def _run_search(self, *, keep_selection=True, refresh_shown=True):
        self._search_timer.stop()
        self._search_generation += 1
        self._search_cancel.set()
        self._search_cancel = threading.Event()
        generation = self._search_generation
        target = self._select_after_reload
        self._list_target_loading = target is not None
        thread_click = self._thread_click
        current = self.current
        if not keep_selection:
            self._rows_generation += 1
            # Old-account rows must not remain actionable during the query.
            with QSignalBlocker(self.table):
                self.table.setRowCount(0)
            self._row_snapshots = {}
            if target is None:
                self._show_message(None)
            self._update_counts()
        parameters = self._search_parameters()
        flagged = self.f_flagged.isChecked()
        cancel = self._search_cancel
        def done(rows):
            if self._hidden_ids:
                rows = [row for row in rows if row["id"] not in self._hidden_ids]
            if not self._closing and generation == self._search_generation:
                # The user may have scrolled/selected another row while SQL
                # was running. Preserve the current view, never its old copy.
                selected = target if target is not None else self._selected_id()
                view = self._list_view() if keep_selection and target is None else None
                self._display_rows(rows, selected, view, refresh_shown=refresh_shown, thread_click=thread_click)
        def fail(error):
            if not self._closing and generation == self._search_generation:
                self._list_target_loading = False
                self._status_error(error)
        if target is not None and current[0]:
            def work():
                position = self.db.list_position(target, *current)
                params = list(parameters)
                if position is not None:
                    params[-1] = max(params[-1], (position // 500 + 1) * 500 + 1)
                return self.db.search_messages(*params, cancel=cancel, flagged=flagged), params[-1] - 1
            def selected_done(result):
                if not self._closing and generation == self._search_generation:
                    rows, limit = result
                    self._search_limit = limit
                    done(rows)
            run_task(work, on_done=selected_done, on_error=fail, local=True)
        elif current[0] or self.search_scope.currentData() == "all":
            run_task(self.db.search_messages, *parameters, cancel=cancel, flagged=flagged,
                     on_done=done, on_error=fail, local=True)
        else:
            done([])

    def _more_results(self):
        self._search_limit += 500
        self._run_search()

    def _global_search(self):
        self.search_scope.setCurrentIndex(self.search_scope.findData("all"))
        self.search.setFocus(); self.search.selectAll()

    def _select_all_messages(self):
        self.table.setFocus()
        self.table.selectAll()

    def _message_menu(self, pos):
        """Меню по правой кнопке: действия над всеми выбранными письмами."""
        if not self._selected_ids():
            return
        menu = QMenu(self)
        for k in ("reply", "reply_all", "forward", None, "read", "unread", "flag", None,
                  "spam", "not_spam", None, "delete", "restore", None, "select_all"):
            if k is None:
                menu.addSeparator()
            elif self.acts[k].isEnabled():
                menu.addAction(self.acts[k])
        menu.exec(self.table.viewport().mapToGlobal(pos))

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
        self._list_target_loading = False
        self._show_message(self._selected_id())
        self._update_actions()
        self._update_counts()

    # ================= просмотр =================
    def _show_message(self, mid: int | None):
        for requested, event in self._body_requests.items():
            if requested != mid:
                event.set()
        if mid is not None and self._shown is not None and self._shown.id == mid and self._refresh_shown(mid):
            return
        if mid is not None and self._message_loading == mid:
            return
        self._quick_save_timer.stop()
        if not self._save_quick_draft():
            return
        self._quick_draft_id = None
        with QSignalBlocker(self.quick.edit):
            self.quick.clear()
        previous = self._shown.id if self._shown is not None and self._shown.body_downloaded else None
        self._message_generation += 1
        generation = self._message_generation
        self._message_loading = mid
        self._shown = None
        self.body_notice.hide()
        if mid is None:
            self.header.hide()
            self.att_bar.hide()
            self.quick.hide()
            self.viewer.clear()
            return
        self.header.hide()
        self.att_bar.hide()
        self.quick.hide()
        style = message_style()
        thread_click = self._thread_click

        def work():
            if self._closing or generation != self._message_generation:
                return None
            m = self.db.message(mid)
            if m is None:
                return None
            names = self.db.attachment_names(m.id, m.raw)
            acc = self.db.account(m.account_id)
            folder = str(Path(acc.storage_dir) / "Вложения") if acc and acc.storage_dir else ""
            prepared = prepare_message(m, style) if m.body_downloaded else None
            trusted = set((self.db.get_setting("trusted_image_senders", "") or "").split())
            remote = self._addr(m.sender) in trusted or self.db.remote_images_allowed(m)
            draft = None
            if m.folder not in ("drafts", "outbox", "sent"):
                for row in self.db.conn().execute("SELECT id,compose_state FROM messages WHERE account_id=? AND folder='drafts' AND compose_state IS NOT NULL", (m.account_id,)):
                    state = json.loads(row["compose_state"])
                    if state.get("quick_source") == m.id:
                        draft = row["id"], state.get("quick_text", "")
                        break
            return m, names, folder, prepared, remote, draft

        def done(result):
            if self._closing or generation != self._message_generation:
                return
            self._message_loading = None
            if result is None:
                self.viewer.clear()
                self._update_actions()
                return
            self._display_message(result, previous, thread_click)
            self._update_actions()

        def failed(error):
            if not self._closing and generation == self._message_generation:
                self._message_loading = None
                self.viewer.clear()
                self._status_error(error)

        run_task(work, on_done=done, on_error=failed, local=True)

    def _display_message(self, result, previous, thread_click):
        m, names, folder, prepared, remote, draft = result
        self._shown = m
        self.header.show_message(m, names)
        if not m.body_downloaded:
            self.att_bar.hide()
            self.quick.hide()
            self.viewer.clear()
            self.header.set_thread_count(0)
            self._request_message_body(m)
            return
        # Содержимое вложений (может быть десятки МБ) читаем в фоне, чтобы окно не подвисало.
        self.att_bar.set_files([], folder)
        shown_id = m.id

        def attachments_loaded(atts):
            if not self._closing and self._shown is not None and self._shown.id == shown_id:
                self.att_bar.set_files(atts, folder)
        run_task(self.db.message_attachments, m.id, on_done=attachments_loaded, on_error=self._status_error, local=True)
        # Другое письмо из уже показанной переписки: страница остаётся, выделение переходит на него
        # (после щелчка по нему на странице — без прокрутки). То же письмо после обновления
        # списка показываем заново: в переписке могли появиться новые письма.
        if m.id != previous and self.viewer.select_in_thread(m, scroll=thread_click != m.id):
            self.header.set_thread_count(len(self.viewer.thread_ids()))
            self.header.set_thread_checked(True)
        else:
            self.viewer.show_message(m, allow_remote=remote, prepared=prepared)
            self._load_thread(m)
        self.quick.setVisible(m.folder not in ("sent", "drafts", "outbox") and self.acts["quick_reply"].isChecked())
        if draft is not None:
            self._quick_draft_id = draft[0]
            with QSignalBlocker(self.quick.edit):
                self.quick.edit.setPlainText(draft[1])
        self._mark_shown_seen(m, previous)

    def _refresh_shown(self, mid: int) -> bool:
        """Список обновился (проверка почты, пометки), а выбрано то же письмо: страница не
        перерисовывается — прокрутка, свёрнутые письма и набранный быстрый ответ остаются.
        Переписка перестраивается, только если в ней появились или пропали письма.
        «Прочитано» не ставим: письмо могли вручную отметить непрочитанным.
        False — письмо изменилось (например, пересохранён черновик): его нужно показать заново."""
        m = self.db.message(mid, with_raw=False)
        if not m:
            return False
        content = lambda x: (x.subject, x.sender, x.recipients, x.date_ts, x.body_text, x.body_html,
                             x.folder, x.body_downloaded, x.raw_path, x.size, x.has_att)
        if content(m) != content(self._shown):
            return False
        m.raw = self._shown.raw
        self._shown = m
        if not m.body_downloaded:
            self._request_message_body(m)
            return True
        self._load_thread(m, refresh=True)
        return True

    def _request_message_body(self, m):
        """Открытое письмо получает приоритет; смена выбора отменяет устаревшую заявку."""
        error = self._body_errors.get(m.id)
        self.body_notice_text.setText(error or tr("Загружается содержимое письма… Можно читать другие письма."))
        self.body_retry.setVisible(bool(error))
        self.body_notice.show()
        if self._closing or error or m.id in self._body_requests:
            return
        cancel = threading.Event()
        self._body_requests[m.id] = cancel

        def done(result):
            self._body_requests.pop(m.id, None)
            if self._closing:
                return
            if result is not None and result.body_downloaded:
                self._queue_sync_refresh(m.account_id, (m.id,), "body")
            if result is None and not cancel.is_set():
                self._body_errors[m.id] = tr("Письмо недоступно. Проверьте подключение и повторите загрузку.")
            if self._shown is not None and self._shown.id == m.id:
                self._show_message(m.id)
                self._update_actions()

        def failed(err):
            self._body_requests.pop(m.id, None)
            if self._closing:
                return
            if cancel.is_set():
                if self._shown is not None and self._shown.id == m.id:
                    self._show_message(m.id)
                return
            self._body_errors[m.id] = tr("Не удалось загрузить письмо: {0}", err.short())
            self._status_error(err)
            if self._shown is not None and self._shown.id == m.id:
                self._request_message_body(self._shown)

        run_task(fetch_message_body, self.db, m.id, cancel=cancel, on_done=done, on_error=failed)

    def _retry_message_body(self):
        m = self._shown
        if m is not None and not m.body_downloaded:
            self._body_errors.pop(m.id, None)
            self._request_message_body(m)

    def _mark_shown_seen(self, m, previous):
        # Прочитанным письмо становится, когда его открыли. Повторный показ того же письма после
        # обновления списка не снимает отметку «непрочитанное», поставленную вручную (Ctrl+U).
        if m.id != previous and not m.seen and m.folder not in ("drafts", "outbox"):
            self.db.update_message(m.id, seen=1, pending_seen=1)
            run_task(set_server_flag, self.db, m, "\\Seen", queued=True, on_error=self._status_error)
            self._set_row_bold(m.id, False)
            self._refresh_counts()

    # ---------- переписка ----------
    def _toggle_thread(self, on: bool):
        self.db.set_setting("thread_view", "1" if on else "0")
        self.header.set_thread_checked(on)
        m = self._shown
        if m is None or not m.body_downloaded:
            return
        if on:
            self._load_thread(m)
        elif self.viewer.thread_shown:
            self.viewer.show_message(m, allow_remote=self._sender_trusted(m.sender) or self.db.remote_images_allowed(m))

    def _activate_thread_message(self, mid: int):
        """Щелчок по письму на странице переписки: письмо выделяется в списке писем.
        Если его нет в списке — открывается его папка (поиск и фильтры списка сбрасываются)."""
        if self._shown is not None and self._shown.id == mid:
            return
        self._thread_click = mid
        try:
            if self._select_row(mid):
                return
            m = self.db.message(mid, with_raw=False)
            item = self._folder_item(m.account_id, "spam" if m.folder == "inbox" and m.is_spam else m.folder) if m else None
            if item is None:
                self.status.showMessage(tr("Письмо не найдено в списке писем"), 8000)
                return
            self._reset_list_filters()
            self._select_after_reload = mid
            if item.parent() is not None:
                item.parent().setExpanded(True)
            if self.tree.currentItem() is item:
                self.reload_messages()
            else:
                self.tree.setCurrentItem(item)  # → _on_folder_selected → reload_messages
            # The list query completes asynchronously and selects the captured
            # target there; trying again here would report a false "not found".
        finally:
            self._thread_click = None
            self._select_after_reload = None

    def _select_row(self, mid: int) -> bool:
        """Выделить письмо в списке, если оно там есть (выделение показывает его в окне просмотра)."""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item.data(ROLE_ID) == mid and not self.table.isRowHidden(row):
                if self._selected_ids() != [mid]:
                    self.table.selectRow(row)
                self.table.scrollToItem(item)
                return True
        return False

    def _folder_item(self, account_id: int, view: str):
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                if top.child(j).data(0, ROLE_ID) == (account_id, view):
                    return top.child(j)
        return None

    def _reset_list_filters(self):
        """Поиск по папке без текста и фильтров (без перезагрузки списка)."""
        self._search_timer.stop()
        with QSignalBlocker(self.search):
            self.search.clear()
        with QSignalBlocker(self.search_scope):
            self.search_scope.setCurrentIndex(self.search_scope.findData("folder"))
        for check, edit in ((self.date_from_on, self.date_from), (self.date_to_on, self.date_to)):
            with QSignalBlocker(check):
                check.setChecked(False)
            edit.setEnabled(False)
        self.f_all.setChecked(True)

    def _toggle_thread_order(self, newest_first: bool):
        self.db.set_setting("thread_newest_first", int(newest_first))
        self.viewer.set_thread_order(newest_first)

    def _load_thread(self, m, refresh: bool = False):
        """Найти переписку письма в фоне; в режиме «Переписка» показать её целиком.
        Пока она собирается, показано само письмо (переписка открывается на нём же).
        refresh — письмо уже показано: страницу меняем, только если изменился состав переписки,
        и с прежней прокруткой."""
        self._thread_generation += 1
        generation = self._thread_generation
        if not refresh:
            self.header.set_thread_count(0)
            self.header.set_thread_checked(self.acts["thread_view"].isChecked())
        if m.folder in ("drafts", "outbox"):
            return
        show = self.acts["thread_view"].isChecked()
        known = self.viewer.thread_state()
        newest_first = self.acts["thread_newest"].isChecked()
        trusted = set((self.db.get_setting("trusted_image_senders", "") or "").split())
        style = thread_style() if show else ""
        db = self.db

        def work():
            ids = db.thread(m.id)
            if not show or len(ids) < 2:
                return ids, None
            items = []
            for mid in ids:
                item = db.message(mid, with_raw=False)
                if item is None:
                    continue
                if item.body_html and "cid:" in item.body_html.lower():
                    item = db.message(mid)  # встроенные картинки берутся из самого письма
                items.append(item)
            if refresh and {(item.id, item.body_downloaded) for item in items} == known:
                return ids, None
            allowed = {x.id for x in items
                       if self._addr(x.sender) in trusted or db.remote_images_allowed(x)}
            page = build_thread_page(items, m.id, allowed, False, style, newest_first)
            return ids, (items, allowed, page)

        def done(result):
            ids, shown = result
            current = self._shown
            if self._closing or current is None or current.id != m.id or generation != self._thread_generation:
                return  # пользователь уже открыл другое письмо
            self.header.set_thread_count(len(ids))
            if refresh and not shown:
                if self.viewer.thread_shown and len(ids) < 2:  # из переписки удалили письма
                    self.viewer.show_message(current, keep_view=True, allow_remote=self._sender_trusted(current.sender)
                                             or self.db.remote_images_allowed(current))
                return
            if shown and self.acts["thread_view"].isChecked():
                items, allowed, page = shown
                if refresh and {(item.id, item.body_downloaded) for item in items} == self.viewer.thread_state():
                    return
                if newest_first != self.acts["thread_newest"].isChecked():
                    page = None  # порядок сменили, пока переписка собиралась
                self.viewer.show_thread(current, items, allowed, page=page,
                                        newest_first=self.acts["thread_newest"].isChecked(), keep_view=refresh)

        run_task(work, on_done=done, on_error=self._thread_error, local=True)

    def _thread_error(self, err):
        self._status_error(err, tr("Переписка"))

    def _set_row_bold(self, mid: int, bold: bool):
        f = QFont(); f.setBold(bold)
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(ROLE_ID) == mid:
                for j in range(self.table.columnCount()):
                    self.table.item(row, j).setFont(f)
                    self.table.item(row, j).setData(ROLE_UNREAD, int(bold))
                break
        self._update_counts()

    # ---------- картинки из интернета ----------
    @staticmethod
    def _addr(sender: str) -> str:
        return parseaddr(sender or "")[1].lower()

    def _sender_trusted(self, sender: str) -> bool:
        trusted = (self.db.get_setting("trusted_image_senders", "") or "").split()
        return bool(self._addr(sender)) and self._addr(sender) in trusted

    def _remember_message_images(self, m):
        """«Показать картинки» запоминается для письма: при возврате к нему и после перезапуска
        картинки загружаются сразу."""
        try:
            self.db.allow_remote_images(m)
        except Exception as e:  # не мешаем показу письма, если база занята
            self.status.showMessage(tr("Не удалось запомнить разрешение на картинки: {0}", e), 5000)

    def _trust_sender_images(self, sender: str):
        addr = self._addr(sender)
        trusted = set((self.db.get_setting("trusted_image_senders", "") or "").split())
        if addr:
            trusted.add(addr)
            self.db.set_setting("trusted_image_senders", " ".join(sorted(trusted)))
            self.status.showMessage(tr("Картинки от {0} будут показываться всегда", addr), 5000)

    # ---------- флажок «Важное» (IMAP \\Flagged, виден и в других почтовых программах) ----------
    def _paint_flag(self, item, flagged: bool):
        item.setIcon(icon("flag", FLAG_COLOR) if flagged else QIcon())
        item.setData(ROLE_SORT, int(flagged))
        item.setToolTip(tr("Важное — щёлкните, чтобы снять флажок") if flagged else tr("Щёлкните, чтобы отметить как важное"))

    def _cell_clicked(self, row: int, column: int):
        if column == COL["flag"] and (item := self.table.item(row, 0)) is not None:
            self._toggle_flag([item.data(ROLE_ID)])

    def _toggle_flag(self, ids=None):
        """Поставить флажок всем выбранным письмам; если у всех уже стоит — снять."""
        messages = [m for m in (self.db.message(mid, with_raw=False) for mid in (ids or self._selected_ids()))
                    if m and m.folder not in ("drafts", "outbox")]
        if not messages:
            return
        flagged = not all(m.flagged for m in messages)
        changed = [m.id for m in messages if m.flagged != flagged]
        for mid in changed:
            self.db.update_message(mid, flagged=int(flagged), pending_flagged=int(flagged))
        if changed:
            # Одна фоновая задача на все письма: пачка UID STORE вместо задачи на каждое письмо.
            run_task(set_server_flags, self.db, changed, "\\Flagged", flagged, queued=True, on_error=self._status_error)
        rows = {self.table.item(row, 0).data(ROLE_ID): row for row in range(self.table.rowCount())}
        for mid in changed:
            if mid in rows:
                self._paint_flag(self.table.item(rows[mid], COL["flag"]), flagged)
        if self._shown is not None and any(m.id == self._shown.id for m in messages):
            self._shown.flagged = flagged

    def _mark_seen(self, seen: bool):
        changed = []
        for mid in self._selected_ids():
            m = self.db.message(mid, with_raw=False)
            if not m or m.folder in ("drafts", "outbox") or m.seen == seen:
                continue
            self.db.update_message(mid, seen=int(seen), pending_seen=int(seen))
            self._set_row_bold(mid, not seen)
            changed.append(mid)
        if changed:
            # Одна фоновая задача на все письма: раньше сотня писем — сотня задач, каждая ждала
            # блокировку ящика, занимала фоновый поток и прерывала проверку почты.
            run_task(set_server_flags, self.db, changed, "\\Seen", seen, queued=True, on_error=self._status_error)
        self._refresh_counts()

    def _save_eml(self):
        m = getattr(self, "_shown", None)
        if not m or not m.body_downloaded or not m.raw:
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
        """One coalesced background read for all badges, including the tray."""
        if self._closing:
            return
        if self._counts_running:
            self._counts_pending = True
            return
        self._counts_running = True
        self._counts_pending = False

        def finish():
            self._counts_running = False
            if self._counts_pending and not self._closing:
                self._refresh_counts()

        def done(counts):
            if not self._closing:
                self._unread_counts = counts
                self._apply_unread_counts()
            finish()

        def failed(error):
            finish()
            if not self._closing:
                self._status_error(error)

        run_task(self.db.unread_counts, on_done=done, on_error=failed, local=True)

    def _apply_unread_counts(self):
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for k in range(top.childCount()):
                it = top.child(k)
                acc_id, view = it.data(0, ROLE_ID)
                title = it.data(0, ROLE_TITLE) or tr(dict(VIEWS).get(view, view.removeprefix("remote:")))
                if self._counts_unread(view):
                    n = self._unread_counts.get((acc_id, view), 0)
                    text = f"{title} ({n})" if n else title
                    if it.text(0) != text:
                        it.setText(0, text)
                        f = QFont(); f.setBold(bool(n)); it.setFont(0, f)
        self._update_tray()

    # ================= действия =================
    def check_mail_manual(self):
        self.check_mail(manual=True)

    @Slot(int)
    def _idle_changed(self, account_id):
        """IDLE следит за «Входящими»: проверяем только эту папку, а не весь ящик."""
        if self._closing:
            return
        if account_id not in self._pending_mail:
            self._inbox_only.add(account_id)
        self._mail_changed(account_id, inbox_only=True)

    @Slot(int)
    def _mail_changed(self, account_id, inbox_only=False):
        if self._closing:
            return
        if not inbox_only:
            self._inbox_only.discard(account_id)
        self._pending_mail.add(account_id)
        if (not self.syncing or not self._sync_cancel.is_set()) and not self._mail_timer.isActive():
            self._mail_timer.start()

    @Slot(int)
    def _idle_unavailable(self, account_id):
        if not self._closing and (acc := self.db.account(account_id)):
            fallback = (tr("работает проверка по таймеру") if self.timer.isActive()
                        else tr("автопроверка по таймеру отключена"))
            self.status.showMessage(
                tr("{0}: мгновенное получение недоступно, {1}", acc.name, fallback), 10000)

    def _check_pending_mail(self):
        if not self._closing and self._pending_mail:
            self.check_mail(account_ids=set(self._pending_mail))

    def check_mail(self, manual: bool = False, account_ids=None):
        if self._closing or getattr(self, "_compacting", False):
            return
        # A stopping batch must finish before a later user request starts another.
        if self.syncing and self._sync_cancel.is_set():
            return
        accounts = self.db.accounts(only_enabled=True)
        enabled = {acc.id for acc in accounts}
        self._pending_mail.intersection_update(enabled)
        self._inbox_only.intersection_update(enabled)
        if not self.syncing:
            self._sync_accounts = set()
        active = self._sync_accounts
        inbox_only = set()
        if account_ids is not None:
            accounts = [acc for acc in accounts if acc.id in account_ids]
            inbox_only = self._inbox_only & set(account_ids)
        accounts = [acc for acc in accounts if acc.id not in active]
        if not accounts:
            if not self.syncing:
                self.status.showMessage(tr("Нет ящиков для проверки"), 5000)
            return
        started = {acc.id for acc in accounts}
        self._pending_mail.difference_update(started)
        self._inbox_only.difference_update(started)
        if not self.syncing:
            self._manual_check = manual
            self._new_total = 0
            self._errors = []
            # Keep these events shared by accounts joining an ongoing check.
            self._sync_cancel = threading.Event()
            self._history_cancel = threading.Event()
            self._set_syncing_ui(True)
        else:
            self._manual_check = self._manual_check or manual
            if self._history_cancel.is_set():
                # Old tasks retain their set event. New tasks read the persisted
                # paused/running policy and can resume explicitly requested history.
                self._history_cancel = threading.Event()
        active.update(started)
        self.syncing += len(accounts)
        for acc in accounts:
            run_task(sync_account, self.db, acc,
                     on_done=lambda n, aid=acc.id: self._sync_done(n, aid),
                     on_error=lambda err, a=acc: self._sync_error(a.name, err, a.id),
                     on_progress=self._sync_progress, cancel=self._sync_cancel,
                     history_cancel=self._history_cancel, inbox_only=acc.id in inbox_only)

    def _sync_progress(self, text):
        if self._sync_cancel.is_set():
            return
        self.status.showMessage(text)
        total = getattr(text, "total", 0)
        if total:
            # Загрузка писем: полоса показывает «загружено / всего», подсказка — полный текст.
            self.sync_bar.setRange(0, total)
            self.sync_bar.setValue(min(text.done, total))
            self.sync_bar.setTextVisible(True)
        else:
            self.sync_bar.setRange(0, 0)
            self.sync_bar.setTextVisible(False)
        self.sync_bar.setToolTip(str(text))
        if getattr(text, "messages_changed", False):
            self._queue_sync_refresh(getattr(text, "account_id", None),
                                     getattr(text, "message_ids", ()), getattr(text, "change_kind", None))

    def _queue_sync_refresh(self, account_id=None, message_ids=(), change_kind=None):
        self._sync_refresh_pending = True
        self._sync_refresh_structural |= change_kind != "body" or account_id is None
        if account_id is not None:
            self._sync_refresh_accounts.add(account_id)
        self._sync_refresh_ids.update(message_ids)
        if not self._sync_refresh_timer.isActive():
            self._sync_refresh_timer.start()

    def _refresh_sync_messages(self):
        # A background body tick must not cancel an explicit jump to a thread
        # message before its folder query/row batches have selected that target.
        if self._list_target_loading and not self._closing:
            self._sync_refresh_timer.start()
            return
        structural = self._sync_refresh_structural or not self._sync_refresh_pending
        accounts, ids = self._sync_refresh_accounts, self._sync_refresh_ids
        self._sync_refresh_accounts, self._sync_refresh_ids = set(), set()
        self._sync_refresh_pending = self._sync_refresh_structural = False
        if self._closing:
            return
        if structural:
            self.reload_tree()
            return
        self._refresh_counts()
        shown_id = self._shown.id if self._shown is not None else None
        affected = shown_id in ids or bool(ids.intersection(self.viewer.thread_ids()))
        if self.current[0] in accounts or self.search_scope.currentData() != "folder":
            self.reload_messages(refresh_shown=affected)
        elif affected and shown_id is not None:
            self._show_message(shown_id)
            self._update_actions()

    def _set_syncing_ui(self, on: bool):
        self.sync_bar.setRange(0, 0)
        self.sync_bar.setTextVisible(False)
        self.sync_bar.setToolTip(tr("Идёт проверка почты"))
        self.sync_bar.setVisible(on)
        self.sync_stop.setVisible(on)
        self.sync_stop.setEnabled(on)
        self.acts["stop_check"].setEnabled(on)
        self.acts["check"].setEnabled(not on)

    def _stop_check(self):
        if not self.syncing:
            return
        self._sync_cancel.set()
        self._mail_timer.stop()
        self.sync_stop.setEnabled(False)
        self.acts["stop_check"].setEnabled(False)
        self.status.showMessage(tr("Проверка останавливается…"))

    def _sync_done(self, n, account_id=None):
        self._new_total += n or 0
        self._sync_finished(account_id)

    def _sync_error(self, name, err, account_id=None):
        self._errors.append((name, err))
        self.error_log.add(tr("Проверка ящика «{0}»", name), err)
        self._update_error_button()
        self._sync_finished(account_id, offer_history=False)

    def _sync_finished(self, account_id=None, *, offer_history=True):
        active = getattr(self, "_sync_accounts", set())
        if account_id is None and active:
            account_id = next(iter(active))
        active.discard(account_id)
        self.syncing -= 1
        # A follow-up belongs to its own account. It need not wait for another
        # account to finish downloading a large history.
        if self._pending_mail and not self._closing and not self._sync_cancel.is_set():
            self._mail_timer.start()
        if self.syncing:
            if offer_history and account_id is not None:
                self._offer_history({account_id})
            return
        self._set_syncing_ui(False)
        stopped = self._sync_cancel.is_set() and not self._closing
        msg = (tr("Проверка остановлена. Новых писем: {0}", self._new_total) if stopped
               else tr("Проверка завершена. Новых писем: {0}", self._new_total))
        if self._errors:
            msg += tr(". Не удалось проверить: ") + ", ".join(f"{n} ({e.summary.lower()})" for n, e in self._errors)
        self.status.showMessage(msg, 30000)
        if self._errors and self._manual_check:
            if len(self._errors) == 1:
                name, err = self._errors[0]
                show_error(self, tr("Ящик «{0}»", name), err)
            else:  # раньше показывалась только первая ошибка
                from ..errors import ErrorInfo
                show_error(self, tr("Проверка почты"), ErrorInfo(
                    tr("Не удалось проверить ящиков: {0}", len(self._errors)),
                    "\n".join(f"{name}: {err.short()}" for name, err in self._errors),
                    "\n\n".join(f"{name}: {err.details}" for name, err in self._errors)))
            self.error_log.mark_seen()  # ошибки уже показаны в окне
            self._update_error_button()
        self.reload_tree()
        if not stopped:
            self._notify_new_mail(self._new_total)
        if offer_history and account_id is not None and not stopped and not self._closing:
            self._offer_history({account_id})

    def _offer_history(self, account_ids=None):
        """После первой проверки ящика: на сервере есть старые письма — спросить, скачать ли их все."""
        if self._closing or self._sync_cancel.is_set():
            return
        pending = getattr(self, "_history_offer_accounts", None)
        if pending is None:
            pending = self._history_offer_accounts = set()
        # Automatic callers supply only the account whose check just completed.
        # Retain nested completions while another question's event loop is open.
        pending.update(account_ids if account_ids is not None else
                       {acc.id for acc in self.db.accounts(only_enabled=True)} - self._sync_accounts)
        if getattr(self, "_offering_history", False):
            return
        self._offering_history = True
        try:
            while pending and not self._closing and not self._sync_cancel.is_set():
                pending.intersection_update(acc.id for acc in self.db.accounts(only_enabled=True))
                ready = pending - self._sync_accounts
                if not ready:
                    break
                pending.difference_update(ready)
                self._ask_history(ready)
        finally:
            self._offering_history = False

    def _ask_history(self, account_ids):
        if self.db.get_setting("history_ask", "1") != "1":
            return  # «Больше не спрашивать»: скачать можно из меню ящика (правая кнопка мыши)
        asked = self._history_not_now
        for acc in self.db.accounts(only_enabled=True):
            if self._closing or self._sync_cancel.is_set():
                return
            if acc.id not in account_ids or acc.id in self._sync_accounts:
                continue
            if self.db.get_setting(f"history:{acc.id}") is not None or acc.id in asked:
                continue
            skipped = sum(int(row["value"] or 0) for row in self.db.conn().execute(
                "SELECT value FROM settings WHERE key LIKE ?", (f"history_skipped:{acc.id}:%",)))
            if not skipped:
                continue
            limit = self.db.get_setting("initial_fetch_limit", "300")
            box = QMessageBox(QMessageBox.Question, tr("Старые письма"), tr(
                "В ящике «{0}» на сервере ещё {1} более старых писем. Сейчас скачаны последние {2} "
                "в каждой папке.\n\nСкачать все письма? Загрузка идёт в фоне, её можно приостановить.",
                acc.name, skipped, limit), parent=self)
            download = box.addButton(tr("Скачать все"), QMessageBox.AcceptRole)
            box.addButton(tr("Не сейчас"), QMessageBox.RejectRole)
            box.setDefaultButton(download)
            never = QCheckBox(tr("Больше не спрашивать"))
            box.setCheckBox(never)
            box.exec()
            if self._closing or self._sync_cancel.is_set():
                return
            if box.clickedButton() is download:
                self.db.set_setting(f"history:{acc.id}", "running")
                self.check_mail(manual=True, account_ids={acc.id})
                continue
            self.status.showMessage(tr("Старые письма можно скачать позже: правая кнопка мыши на ящике → "
                                       "«Скачать остальные письма»"), 15000)
            if never.isChecked():
                self.db.set_setting(f"history:{acc.id}", "declined")
                self.db.set_setting("history_ask", "0")
                return
            asked.add(acc.id)  # «Не сейчас» — напомним при следующем запуске программы

    def _status_error(self, err, title=None):
        self.status.showMessage(tr("Ошибка: ") + err.short(), 15000)
        self.error_log.add(title or tr("Фоновое действие"), err)
        self._update_error_button()

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

    def _address_book(self):
        from .address_book import AddressBookDialog
        dlg = AddressBookDialog(self.db, parent=self)
        dlg.compose_requested.connect(lambda to: self._open_compose(account_id=self.current[0], to=to))
        dlg.exec()

    def _open_selected(self):
        m = self.db.message(self._selected_id()) if self._selected_id() else None
        if m and m.folder in ("drafts", "outbox"):
            lock = self.db.account_lock(m.account_id)
            # Проверка почты уступает ящик за одну пачку писем; отправка может занять дольше.
            if not lock.acquire(timeout=3):
                QMessageBox.information(self, tr("Исходящие"), tr("Ящик сейчас занят отправкой или проверкой почты. Повторите через несколько секунд."))
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
        accounts = self.db.accounts(only_enabled=True)
        for acc in accounts:
            self.db.set_setting(f"history:{acc.id}", "running")
        self._pending_mail.update(acc.id for acc in accounts)
        self._inbox_only.clear()
        self.check_mail(manual=True, account_ids={acc.id for acc in accounts})

    def _download_account_history(self, account_id):
        self.db.set_setting(f"history:{account_id}", "running")
        self._check_account(account_id)
        if self._pending_mail:
            self._inbox_only.discard(account_id)  # после текущей проверки — весь ящик, не только входящие

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
        self._sync_cancel.set()
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
            if getattr(self, "isolated_profile", False):
                command.append("--isolated-profile")
            try:
                subprocess.Popen(command, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as error:
                from ..errors import explain
                fail(explain(error))
                return
            self._quit()

        def fail(error):
            if self._closing:
                return
            self.setEnabled(True)
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
        # Не наращиваем «Re: Re:»: учитываем и чужие приставки (Отв:, AW:, SV:, RE[2]:).
        if re.match(r"\s*(re|ответ|отв|aw|sv|antw)(\[\d+\])?\s*:", subject or "", re.IGNORECASE):
            return subject
        return "Re: " + (subject or "")

    @staticmethod
    def _reply_address(m) -> str:
        """Кому отвечать: Reply-To (рассылки, адреса noreply), иначе отправитель."""
        if m.folder == "sent":
            return m.recipients
        from ..mailparse import header
        return (header(m.raw, "Reply-To").strip() if m.raw else "") or m.sender

    @staticmethod
    def _references(m) -> str:
        from ..mailparse import header
        return " ".join(header(m.raw, "References").split()) if m.raw else ""

    def _reply(self):
        m = getattr(self, "_shown", None)
        if not m or not m.body_downloaded:
            return
        self._open_compose(account_id=m.account_id, to=self._reply_address(m), subject=self._re(m.subject),
                           quote_header=self._quote_header(m), quote_text=m.body_text, in_reply_to=m.message_id,
                           references=self._references(m), mark_source=[m.id, MARK_ANSWERED], **self._rich_quote(m))

    def _reply_all(self):
        m = getattr(self, "_shown", None)
        if not m or not m.body_downloaded:
            return
        acc = self.db.account(m.account_id)
        me = (acc.email if acc else "").lower()
        to = self._reply_address(m)
        to_addrs = {a.lower() for _, a in getaddresses([to])}
        others = [m.recipients] + ([m.sender] if m.folder != "sent" and to != m.sender else [])
        cc = ", ".join(f"{n} <{a}>" if n else a for n, a in getaddresses(others)
                       if a and a.lower() != me and a.lower() not in to_addrs)
        self._open_compose(account_id=m.account_id, to=to, cc=cc, subject=self._re(m.subject),
                           quote_header=self._quote_header(m), quote_text=m.body_text, in_reply_to=m.message_id,
                           references=self._references(m), mark_source=[m.id, MARK_ANSWERED], **self._rich_quote(m))

    def _forward(self):
        m = getattr(self, "_shown", None)
        if not m or not m.body_downloaded:
            return
        header = tr("-------- Пересылаемое сообщение --------\nОт: {0}\nКому: {1}\nТема: {2}", m.sender, m.recipients, m.subject)
        subj = m.subject if m.subject.lower().startswith("fwd:") else "Fwd: " + m.subject
        self._open_compose(account_id=m.account_id, subject=subj, quote_header=header,
                           quote_text=m.body_text, attachments=self.db.message_attachments(m.id),
                           mark_source=[m.id, MARK_FORWARDED], **self._rich_quote(m))

    def _rich_quote(self, message):
        from .viewer import inline_parts
        displayed = getattr(self.viewer, "_msg", None)
        parts = self.viewer._inline if displayed and displayed.id == message.id else inline_parts(message.raw)
        return dict(quote_body_html=message.body_html,
                    quote_images=[(cid, mime, data) for cid, (mime, data) in parts.items()])

    def _mark_spam(self, is_spam: bool):
        ids = self._selected_ids()
        self._change_messages(ids, "spam" if is_spam else "not_spam")

    def _delete(self):
        ids = self._selected_ids()
        if not ids:
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
        """Письма сразу исчезают из списка, а операция на сервере идёт в фоне. Она может ждать,
        пока проверка почты закончит текущий шаг (у Gmail через Kaspersky это десятки секунд):
        ждать этого в интерфейсе не нужно. Не получилось — письма возвращаются в список."""
        ids = [mid for mid in ids if mid not in self._hidden_ids]
        if not ids:
            return
        self._hide_rows(ids)
        self._mail_queue.append((ids, kind))
        if not self._mail_busy:
            self._next_mail_operation()

    def _hide_rows(self, ids):
        hide = set(ids)
        self._hidden_ids |= hide
        rows = [i for i in range(self.table.rowCount())
                if self.table.item(i, 0) is not None and self.table.item(i, 0).data(ROLE_ID) in hide]
        if not rows:
            return
        with QSignalBlocker(self.table):
            for i in reversed(rows):
                self.table.removeRow(i)
            # Как и после обновления списка: следующее письмо само не открывается
            # (иначе оно стало бы прочитанным без действия пользователя).
            self.table.setCurrentIndex(QModelIndex())
        snapshots = getattr(self, "_row_snapshots", None)
        if snapshots:
            self._row_snapshots = {mid: value for mid, value in snapshots.items() if mid not in hide}
        self._on_message_selected()

    def _next_mail_operation(self):
        if self._closing or not self._mail_queue:
            self._mail_busy = False
            self._update_actions()
            return
        ids, kind = self._mail_queue.pop(0)
        self._mail_busy = True
        self.status.showMessage(tr("Выполняется операция с письмами…"))

        def finish(changed, error):
            if self._closing:
                return
            self._hidden_ids -= set(ids)
            if kind == "trash" and changed:
                self._undo_delete_ids = changed
            elif kind != "trash":
                self._undo_delete_ids = [mid for mid in self._undo_delete_ids if mid not in changed]
            self._next_mail_operation()
            if not self._mail_busy:
                self.reload_tree()  # одна перезагрузка после всей очереди
            if changed or error is None:
                text = {"trash": tr("Перемещено в корзину: {0}. Отмена: Ctrl+Z", len(changed)),
                        "restore": tr("Восстановлено писем: {0}", len(changed)),
                        "purge": tr("Удалено навсегда писем: {0}", len(changed)),
                        "spam": tr("Отмечено писем: {0}. Фильтр дообучен.", len(changed)),
                        "not_spam": tr("Отмечено писем: {0}. Фильтр дообучен.", len(changed))}[kind]
                self.status.showMessage(text, 15000)
            if error is not None:
                if self._mail_busy:
                    self.reload_messages()  # письма, которые не удалось изменить, — обратно в список
                show_error(self, tr("Операция с письмами не завершена"), error)

        def done(result):
            changed, errors = result
            finish(changed, errors[0] if errors else None)

        def fail(error):
            finish([], error)

        run_task(change_messages, self.db, ids, _mail_operation(kind), on_done=done, on_error=fail)

    def _reorder_accounts(self, ids: list[int]):
        # Отложенный drop может прийти после добавления/удаления ящика.
        if set(ids) != {account.id for account in self.db.accounts()}:
            return
        self.db.set_account_order(ids)
        self.reload_tree()

    def _manage_accounts(self):
        dialog = AccountsDialog(self.db, self)
        dialog.order_changed.connect(self.reload_tree)
        dialog.account_saved.connect(self.profile_changed)
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
