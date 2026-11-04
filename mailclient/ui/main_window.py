"""Главное окно."""
from __future__ import annotations

from ..i18n import LANGUAGES, language, spam_reason, tr

import html
from datetime import datetime
from pathlib import Path

from email.utils import getaddresses, parseaddr

from PySide6.QtCore import QSize, Qt, QTimer, Slot
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QToolBar, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget, QHBoxLayout, QInputDialog, QToolButton, QButtonGroup, QMenu,
)
from PySide6.QtCore import QByteArray
from PySide6.QtGui import QActionGroup

from .. import __version__

from ..db import Database
from ..mailparse import attachments
from ..protocols import build_message, redirect_message, send_message, set_server_flag, sync_account
from ..spam import SpamFilter
from ..storage import attachments_dir, default_storage_dir
from .account_dialog import AccountsDialog
from .compose_dialog import ComposeDialog
from .spam_dialog import SpamDialog
from .messages import show_error
from .attachments_bar import AttachmentsBar
from .header import MessageHeader, QuickReply, human_date
from .icons import icon
from .viewer import MessageView
from .worker import run_task
from .mail_watch import MailWatch

VIEWS = [("inbox", "Входящие"), ("spam", "Спам"), ("sent", "Отправленные"), ("trash", "Корзина")]
VIEW_ICONS = {"inbox": "inbox", "spam": "shield-alert", "sent": "send", "trash": "trash-2"}
ROLE_ID = Qt.UserRole
ROLE_SORT = Qt.UserRole + 1

# колонки списка писем: (ключ, заголовок)
COLUMNS = [("att", ""), ("from", "От"), ("to", "Кому"), ("subject", "Тема"), ("date", "Получено"), ("size", "Размер")]
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
        self._closing = False
        self._pending_mail = set()
        self.setWindowTitle("SimpleMail")
        self.resize(1200, 760)

        # ---- левая панель: ящики и папки ----
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(10)          # компактное дерево, без лишнего отступа слева
        self.tree.setIconSize(QSize(16, 16))
        self.tree.setStyleSheet("QTreeView { padding-left: 0px; } QTreeView::item { padding: 1px 0px; }")
        self.tree.itemSelectionChanged.connect(self._on_folder_selected)

        # ---- список писем ----
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Быстрый поиск: отправитель, получатель, тема…"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_search)
        self.f_all = QToolButton(); self.f_all.setText(tr("Все")); self.f_all.setCheckable(True); self.f_all.setChecked(True)
        self.f_unread = QToolButton(); self.f_unread.setText(tr("Не прочитано")); self.f_unread.setCheckable(True)
        self.f_unread.setToolTip(tr("Показывать только непрочитанные письма"))
        grp = QButtonGroup(self); grp.setExclusive(True); grp.addButton(self.f_all); grp.addButton(self.f_unread)
        grp.buttonClicked.connect(lambda _: self._apply_search())
        search_row = QHBoxLayout(); search_row.setContentsMargins(0, 0, 0, 0)
        search_row.addWidget(self.search, 1); search_row.addWidget(self.f_all); search_row.addWidget(self.f_unread)

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
        self.table.doubleClicked.connect(lambda _: self._reply())

        list_box = QWidget()
        lb = QVBoxLayout(list_box); lb.setContentsMargins(0, 0, 0, 0); lb.setSpacing(2)
        lb.addLayout(search_row); lb.addWidget(self.table)

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
        act("compose", tr("Написать письмо"), self._compose, "Ctrl+N", "mail-plus", tr("Новое письмо"))
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
        for k in ("compose", "check", None, "save_eml", "save_att", None, "exit"):
            m.addSeparator() if k is None else m.addAction(a[k])
        m = mb.addMenu(tr("&Письмо"))
        for k in ("reply", "reply_all", "forward", "redirect", None, "read", "unread", None, "spam", "not_spam", None,
                  "images", None, "delete"):
            m.addSeparator() if k is None else m.addAction(a[k])
        m = mb.addMenu(tr("&Вид"))
        lm = m.addMenu(tr("Раскладка окна"))
        for key, _ in LAYOUTS:
            lm.addAction(a["layout_" + key])
        for k in (None, "show_folders", "quick_reply", "labels", None, "search"):
            m.addSeparator() if k is None else m.addAction(a[k])
        m = mb.addMenu(tr("&Настройки"))
        for k in ("accounts", "signatures", "spam_settings", "interval"):
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
                 "unread": tr("Непрочит."), "images": tr("Картинки"), "save_att": tr("Вложения")}
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
        self.acts["save_att"].setEnabled(one and self.att_bar.isVisible())
        self.acts["redirect"].setEnabled(one)

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
        self.quick.setVisible(on and self._shown is not None and self._shown.folder != "sent")

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
        self._closing = True
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

    def _quick_reply(self, text: str):
        m = getattr(self, "_shown", None)
        if not m:
            return
        acc = self.db.account(m.account_id)
        sig_html = self.db.account_signature_html(m.account_id)
        sig = ""
        if sig_html:
            from .editor import RichEditor
            sig = RichEditor.plain_from_html(sig_html).strip()
        quote = "\n".join("> " + line for line in (m.body_text or "").splitlines())
        body = text + (f"\n\n-- \n{sig}" if sig else "") + f"\n\n{self._quote_header(m)}\n{quote}\n"
        em = build_message(acc, m.sender, self._re(m.subject), body, in_reply_to=m.message_id)
        self.quick.set_busy(True)

        def done(_):
            self.quick.set_busy(False); self.quick.clear()
            self.status.showMessage(tr("Ответ отправлен"), 5000)

        def fail(err):
            self.quick.set_busy(False)
            show_error(self, tr("Ответ не отправлен"), err)

        run_task(send_message, self.db, acc, em, on_done=done, on_error=fail)

    def _about(self):
        QMessageBox.about(self, tr("О программе"),
                          tr("<b>SimpleMail {0}</b><br>Простой почтовый клиент.<br><br>Значки: Lucide (лицензия ISC).", __version__))

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
            top = QTreeWidgetItem([acc.name + ("" if acc.enabled else tr(" (выкл.)"))])
            top.setIcon(0, icon("at-sign"))
            top.setData(0, ROLE_ID, (acc.id, None))
            self.tree.addTopLevelItem(top)
            for view, title in VIEWS:
                title = tr(title)
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
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        bold = QFont(); bold.setBold(True)
        clip = icon("paperclip", "#6b7280")
        for i, r in enumerate(rows):
            unread = not r["seen"] and view in ("inbox", "spam")
            values = {
                "att": ("", 1 if r["has_att"] else 0),
                "from": (self._short_addr(r["sender"]), (r["sender"] or "").lower()),
                "to": (self._short_addr(r["recipients"]), (r["recipients"] or "").lower()),
                "subject": (r["subject"] or tr("(без темы)"), None),
                "date": (human_date(r["date_ts"]), r["date_ts"] or 0),
                "size": (human_size(r["size"]), r["size"] or 0),
            }
            for key, (text, sort) in values.items():
                item = SortItem(text)
                item.setData(ROLE_ID, r["id"])
                item.setData(ROLE_SORT, sort)
                item.setData(ROLE_SORT + 1, int(unread))
                if unread:
                    item.setFont(bold)
                if key in ("from", "to"):
                    item.setToolTip(r["sender"] if key == "from" else r["recipients"])
                elif r["spam_reason"]:
                    item.setToolTip(tr("Спам-фильтр: ") + spam_reason(r["spam_reason"]))
                if key == "att" and r["has_att"]:
                    item.setIcon(clip)
                if key == "size":
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(i, COL[key], item)
        self.table.setSortingEnabled(True)
        self.table.blockSignals(False)
        self._apply_search()
        select_row = next((i for i in range(self.table.rowCount())
                           if self.table.item(i, 0).data(ROLE_ID) == selected), -1)
        if select_row >= 0:
            self.table.selectRow(select_row)
        else:
            self._show_message(None)
        self._update_counts()

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

    def _apply_search(self):
        q = self.search.text().strip().lower()
        only_unread = self.f_unread.isChecked()
        cols = [COL["from"], COL["to"], COL["subject"]]
        for i in range(self.table.rowCount()):
            hide = False
            if only_unread and not self.table.item(i, 0).data(ROLE_SORT + 1):
                hide = True
            elif q:
                text = " ".join((self.table.item(i, j).text() + " " + (self.table.item(i, j).toolTip() or "")).lower()
                                for j in cols)
                hide = q not in text
            self.table.setRowHidden(i, hide)
        self._update_counts()

    def _selected_ids(self) -> list[int]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, 0).data(ROLE_ID) for r in rows]

    def _selected_id(self) -> int | None:
        ids = self._selected_ids()
        return ids[0] if len(ids) == 1 else None

    def _on_message_selected(self):
        self._show_message(self._selected_id())
        self._update_actions()
        self._update_counts()

    # ================= просмотр =================
    def _show_message(self, mid: int | None):
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
        atts = attachments(m.raw) if m.raw else []
        self.header.show_message(m, [n for n, _ in atts])
        acc = self.db.account(m.account_id)
        self.att_bar.set_files(atts, str(Path(acc.storage_dir) / "Вложения") if acc and acc.storage_dir else "")
        self.viewer.show_message(m, allow_remote=self._sender_trusted(m.sender))
        self.quick.setVisible(m.folder != "sent" and self.acts["quick_reply"].isChecked())
        self.quick.clear()

        if not m.seen and m.folder == "inbox":
            self.db.update_message(m.id, seen=1)
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
                title = tr(dict(VIEWS)[view])
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
        dlg = ComposeDialog(self.db, parent=self, **kw)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.sent.connect(lambda: self.current[1] == "sent" and self.reload_messages())
        dlg.sent.connect(lambda: self.status.showMessage(tr("Письмо отправлено"), 5000))
        dlg.show()

    def _compose(self):
        self._open_compose(account_id=self.current[0])

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
            self.status.showMessage(tr("Отмечено писем: {0}. Фильтр дообучен.", len(ids)), 5000)
            self.reload_tree()

    def _delete(self):
        ids = self._selected_ids()
        if not ids:
            return
        view = self.current[1]
        if view == "trash":
            if QMessageBox.question(self, tr("Удаление"), tr("Удалить навсегда писем: {0}?", len(ids))) != QMessageBox.Yes:
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

    def _interval_settings(self):
        cur = int(self.db.get_setting("check_interval_min", "10"))
        val, ok = QInputDialog.getInt(self, tr("Автопроверка"), tr("Проверять почту каждые N минут (0 — выкл.):"),
                                      cur, 0, 1440)
        if ok:
            self.db.set_setting("check_interval_min", val)
            self._restart_timer()
