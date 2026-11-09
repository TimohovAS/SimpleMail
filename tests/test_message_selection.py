"""Выбранное письмо остаётся открытым при обновлении списка; само письмо не открывается и
не становится прочитанным без действия пользователя."""
import os
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock
from tests.ui_tasks import run_local

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from mailclient.db import Account, Database, Message
from mailclient.mailparse import parse_message
from mailclient.ui import main_window, viewer
from mailclient.ui import attachments_bar


class TestMessageSelection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.addCleanup(self.db.close)
        self.accounts = [self.db.save_account(Account(
            name=f"Account {i}", email=f"a{i}@example.com", storage_dir=self.tmp.name,
        )) for i in range(2)]
        self.messages = []
        for account_id in self.accounts:
            self.messages.append([self.db.add_message(Message(
                account_id=account_id, subject=f"Message {i}", sender="sender@example.com",
                recipients="recipient@example.com", body_text=f"Body {account_id}/{i}",
                date_ts=1700000000 + i, seen=True,
            )) for i in range(2)])
        for patch in (mock.patch.object(main_window, "run_task", side_effect=run_local),
                      mock.patch("mailclient.ui.mail_watch.MailWatcher"),
                      mock.patch.object(viewer, "HAS_WEBENGINE", False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.window = main_window.MainWindow(self.db)
        self.addCleanup(self.close_window)
        self.window.show()
        self.app.processEvents()

    def close_window(self):
        self.window.close()
        self.app.processEvents()

    def assert_displayed(self, message_id):
        self.assertEqual(self.window._selected_id(), message_id)
        self.assertIsNotNone(self.window._shown)
        self.assertEqual(self.window._shown.id, message_id)
        self.assertEqual(self.window.viewer.text.toPlainText(), self.db.message(message_id).body_text.rstrip("\n"))
        self.assertFalse(self.window.header.isHidden())
        self.assertTrue(self.window.acts["reply"].isEnabled())

    def assert_nothing_open(self):
        self.assertIsNone(self.window._selected_id())
        self.assertIsNone(self.window._shown)
        self.assertEqual(self.window.viewer.text.toPlainText(), "")
        self.assertFalse(self.window.acts["reply"].isEnabled())

    def add_unread(self, account_index=0):
        return self.db.add_message(Message(
            account_id=self.accounts[account_index], subject="New", sender="new@example.com",
            body_text="New body", date_ts=1700000050, seen=False))

    def test_startup_does_not_open_new_message(self):
        mid = self.add_unread()
        self.window.close()
        self.window = main_window.MainWindow(self.db)
        self.window.show()
        self.app.processEvents()
        self.assertEqual(self.window.table.item(0, 0).data(main_window.ROLE_ID), mid)
        self.assert_nothing_open()
        self.assertFalse(self.db.message(mid).seen)
        # Стрелка «вниз» из пустого выделения открывает первое письмо — тогда оно и прочитано.
        self.window.table.setFocus()
        QTest.keyClick(self.window.table, Qt.Key_Down)
        self.assert_displayed(mid)
        self.assertTrue(self.db.message(mid).seen)

    def test_switch_account_does_not_open_message_until_click(self):
        mid = self.add_unread(1)
        self.window.table.selectRow(0)
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(1).child(0))
        self.assert_nothing_open()
        self.assertFalse(self.db.message(mid).seen)
        rect = self.window.table.visualItemRect(self.window.table.item(0, 0))
        QTest.mouseClick(self.window.table.viewport(), Qt.LeftButton, pos=rect.center())
        self.assert_displayed(mid)
        self.assertTrue(self.db.message(mid).seen)
        QTest.mouseClick(self.window.table.viewport(), Qt.LeftButton, pos=rect.center())
        self.assert_displayed(mid)

    def test_new_mail_check_does_not_mark_open_message_read_again(self):
        mid = self.add_unread()
        self.window.reload_messages()
        self.window.table.selectRow(0)
        self.assert_displayed(mid)
        self.window._mark_seen(False)
        self.assertFalse(self.db.message(mid).seen)
        self.window.reload_tree()  # как после проверки почты
        self.assert_displayed(mid)
        self.assertFalse(self.db.message(mid).seen)

    def test_selected_message_gone_after_refresh_opens_nothing(self):
        self.window.table.selectRow(0)
        unread = self.messages[0][0]
        self.db.update_message(unread, seen=0)
        self.db.delete_message(self.messages[0][1])
        self.window.reload_tree()
        self.assert_nothing_open()
        self.assertFalse(self.db.message(unread).seen)

    def test_refresh_restores_preview_even_when_selection_does_not_change(self):
        self.window.table.selectRow(0)
        self.window._show_message(None)
        self.window.reload_messages()
        self.assert_displayed(self.messages[0][1])

    def test_refresh_preserves_message_by_id_after_new_mail_changes_row(self):
        self.window.table.selectRow(1)
        selected = self.messages[0][0]
        self.db.add_message(Message(account_id=self.accounts[0], subject="New",
                                    date_ts=1700000010, seen=True))
        self.window.reload_tree()
        self.assert_displayed(selected)

    def test_first_row_respects_filter_and_sort_order(self):
        self.window.search.setText("Message 0")
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(1).child(0))
        self.window.table.selectRow(0)
        self.assert_displayed(self.messages[1][0])
        self.window.search.clear()
        self.window.table.sortByColumn(main_window.COL["date"], Qt.AscendingOrder)
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(0).child(0))
        self.window.table.selectRow(0)
        self.assert_displayed(self.messages[0][0])

    def test_empty_folder_or_no_filter_results_clears_preview(self):
        self.window.table.selectRow(0)
        self.window.tree.setCurrentItem(self.window._folder_item(self.accounts[0], "spam"))
        self.assertIsNone(self.window._selected_id())
        self.assertIsNone(self.window._shown)
        self.assertEqual(self.window.viewer.text.toPlainText(), "")
        self.assertFalse(self.window.acts["reply"].isEnabled())
        self.window.search.setText("no matching messages")
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(1).child(0))
        self.assertIsNone(self.window._selected_id())
        self.assertIsNone(self.window._shown)

    def test_attachment_open_save_and_forward(self):
        em = EmailMessage()
        em.set_content("Offline body")
        em.add_attachment(b"database attachment", maintype="application", subtype="pdf", filename="offline.pdf")
        message = parse_message(em.as_bytes())
        message.account_id = self.accounts[0]
        message.date_ts = 1700000020
        message.seen = True
        mid = self.db.add_message(message)
        self.window.reload_messages(keep_selection=False)
        self.window.table.selectRow(0)
        self.assert_displayed(mid)
        # Содержимое вложений загружается в фоне (п.9 ревью): выполняем эту задачу.
        call = next(c for c in reversed(main_window.run_task.call_args_list)
                    if c.args and c.args[0] == self.db.message_attachments)
        call.kwargs["on_done"](call.args[0](*call.args[1:]))
        self.assertEqual(self.window.att_bar._files, [("offline.pdf", b"database attachment")])
        with mock.patch.object(attachments_bar.tempfile, "gettempdir", return_value=self.tmp.name), \
                mock.patch.object(attachments_bar.QDesktopServices, "openUrl") as open_url:
            self.window.att_bar._open(0)
            opened = Path(open_url.call_args.args[0].toLocalFile())
            self.assertEqual(opened.read_bytes(), b"database attachment")
        destination = Path(self.tmp.name) / "saved.pdf"
        with mock.patch.object(attachments_bar.QFileDialog, "getSaveFileName", return_value=(str(destination), "")):
            self.window.att_bar._save_one(0)
        self.assertEqual(destination.read_bytes(), b"database attachment")
        with mock.patch.object(self.window, "_open_compose") as compose:
            self.window._forward()
        self.assertEqual(compose.call_args.kwargs["attachments"], [("offline.pdf", b"database attachment")])

    def test_tree_keeps_collapsed_accounts_and_counts_server_folders(self):
        # Ревью, раздел интерфейса: свёрнутый ящик не разворачивается после проверки,
        # у серверных папок виден счётчик непрочитанных.
        with self.db.conn() as c:
            c.execute("INSERT INTO folders(account_id,name,view,display_name) VALUES(?,?,?,?)",
                      (self.accounts[0], "Work", "remote:Work", "Work"))
        self.db.add_message(Message(account_id=self.accounts[0], folder="remote:Work", subject="Unread",
                                    sender="x@example.com", date_ts=1700000100, seen=False))
        self.window.tree.topLevelItem(1).setExpanded(False)
        self.window.reload_tree()
        self.assertTrue(self.window.tree.topLevelItem(0).isExpanded())
        self.assertFalse(self.window.tree.topLevelItem(1).isExpanded())
        top = self.window.tree.topLevelItem(0)
        work = next(top.child(i) for i in range(top.childCount())
                    if top.child(i).data(0, main_window.ROLE_ID)[1] == "remote:Work")
        self.assertEqual(work.text(0), "Work (1)")
        self.window._refresh_counts()
        self.assertEqual(work.text(0), "Work (1)")
        self.window.tree.setCurrentItem(work)
        with self.db.conn() as c:
            c.execute("INSERT INTO folders(account_id,name,view,display_name) VALUES(?,?,?,?)",
                      (self.accounts[0], "Projects", "remote:Projects", "Projects"))
        self.window.reload_tree()
        self.assertEqual(self.window.current, (self.accounts[0], "remote:Work"))
        top = self.window.tree.topLevelItem(0)
        folders = [top.child(i).data(0, main_window.ROLE_ID)[1] for i in range(top.childCount())]
        self.assertEqual(folders[:2], ["inbox", "sent"])
        self.assertEqual(folders[-2:], ["spam", "trash"])
        self.assertIn("remote:Projects", folders[2:-2])
        self.assertIn("remote:Work", folders[2:-2])
        self.assertEqual([self.window.tree.topLevelItem(i).data(0, main_window.ROLE_ID)[0]
                          for i in range(self.window.tree.topLevelItemCount())], self.accounts)

    def finish_mail_operation(self):
        call = main_window.run_task.call_args
        self.assertIs(call.args[0], main_window.change_messages)
        result = call.args[0](*call.args[1:])
        call.kwargs["on_done"](result)
        return result

    def test_delete_waits_for_success_and_undo_restores_last_batch(self):
        self.window.table.selectRow(0)
        mid = self.window._selected_id()
        self.window._delete()
        self.assertEqual(self.db.message(mid).folder, "inbox")
        self.assertFalse(self.window.acts["delete"].isEnabled())
        self.assertFalse(self.window.acts["undo_delete"].isEnabled())
        self.finish_mail_operation()
        self.assertEqual(self.db.message(mid).folder, "trash")
        self.assertTrue(self.window.acts["undo_delete"].isEnabled())
        self.window.acts["undo_delete"].trigger()
        self.finish_mail_operation()
        self.assertEqual(self.db.message(mid).folder, "inbox")
        self.assertFalse(self.window.acts["undo_delete"].isEnabled())

    def test_failed_delete_keeps_message_and_reports_error(self):
        self.window.table.selectRow(0)
        mid = self.window._selected_id()
        with mock.patch.object(main_window, "move_to_trash", side_effect=ConnectionError("offline")), \
                mock.patch.object(main_window, "show_error") as error:
            self.window._delete()
            self.finish_mail_operation()
        error.assert_called_once()
        self.assertEqual(self.db.message(mid).folder, "inbox")
        self.assertFalse(self.window._mail_busy)
        self.assertTrue(self.window.acts["delete"].isEnabled())
        self.assertFalse(self.window.acts["undo_delete"].isEnabled())

    def test_partial_delete_only_adds_successful_messages_to_undo(self):
        original = main_window.move_to_trash
        self.window.table.selectRow(0)
        failing_id = self.window._selected_id()

        def move(db, mid):
            if mid == failing_id:
                raise ConnectionError("offline")
            return original(db, mid)

        self.window.table.selectAll()
        with mock.patch.object(main_window, "move_to_trash", side_effect=move), \
                mock.patch.object(main_window, "show_error"):
            self.window._delete()
            changed, errors = self.finish_mail_operation()
        self.assertEqual(len(changed), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.window._undo_delete_ids, changed)
        self.assertEqual(self.db.message(failing_id).folder, "inbox")
        self.window._undo_delete()
        self.finish_mail_operation()
        self.assertTrue(all(self.db.message(mid).folder == "inbox" for mid in self.messages[0]))

    def test_restore_is_available_after_restart_and_purge_requires_confirmation(self):
        self.window.table.selectRow(0)
        mid = self.window._selected_id()
        main_window.move_to_trash(self.db, mid)
        self.window.close()
        self.window = main_window.MainWindow(self.db)
        self.app.processEvents()
        self.window.tree.setCurrentItem(self.window._folder_item(self.accounts[0], "trash"))
        self.window.table.selectRow(0)
        self.assertTrue(self.window.acts["restore"].isEnabled())
        main_window.run_task.reset_mock()
        with mock.patch.object(main_window.QMessageBox, "question", return_value=main_window.QMessageBox.No):
            self.window._delete()
        main_window.run_task.assert_not_called()
        self.assertEqual(self.db.message(mid).folder, "trash")
        self.window.acts["restore"].trigger()
        self.finish_mail_operation()
        self.assertEqual(self.db.message(mid).folder, "inbox")


    def test_select_all_and_mark_several_read_from_toolbar_and_menu(self):
        self.assertIn(self.window.acts["read"], self.window.toolbar.actions())
        self.assertEqual(self.window.acts["read"].iconText(), "Прочитано")
        self.window.search.setFocus()
        self.window.acts["select_all"].trigger()
        ids = self.window._selected_ids()
        self.assertEqual(len(ids), 2)
        self.assertTrue(self.window.table.hasFocus())
        self.window.acts["unread"].trigger()
        self.assertTrue(all(not self.db.message(mid).seen for mid in ids))
        self.window.acts["read"].trigger()
        self.assertTrue(all(self.db.message(mid).seen for mid in ids))
        shown = []

        class Menu(main_window.QMenu):
            def exec(self, *args):
                shown.extend(self.actions())

        with mock.patch.object(main_window, "QMenu", Menu):
            self.window._message_menu(self.window.table.viewport().rect().center())
        self.assertIn(self.window.acts["read"], shown)
        self.assertIn(self.window.acts["delete"], shown)
        self.assertNotIn(self.window.acts["reply"], shown)  # ответ — только на одно письмо


    def click_check(self, row):
        table = self.window.table
        rect = table.visualRect(table.model().index(row, main_window.COL["check"]))
        QTest.mouseClick(table.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
        self.app.processEvents()

    def click_header_check(self):
        header = self.window.table.horizontalHeader()
        column = main_window.COL["check"]
        x = header.sectionViewportPosition(column) + header.sectionSize(column) // 2
        QTest.mouseClick(header.viewport(), Qt.LeftButton, Qt.NoModifier, QPoint(x, header.height() // 2))
        self.app.processEvents()

    def test_check_column_toggles_rows_and_header_selects_all(self):
        table = self.window.table
        header = table.horizontalHeader()
        self.assertEqual(header.visualIndex(main_window.COL["check"]), 0)  # первая колонка слева
        self.assertIn(self.window.acts["select_all"], self.window.toolbar.actions())
        self.assertEqual(self.window.acts["select_all"].iconText(), "Выделить все")
        table.clearSelection()
        self.click_check(0)
        first = self.window._selected_ids()
        self.assertEqual(len(first), 1)
        self.click_check(1)  # второй флажок добавляет письмо, а не заменяет выделение
        self.assertEqual(len(self.window._selected_ids()), 2)
        self.assertEqual(table.selection_state(), Qt.Checked)
        self.click_check(1)
        self.assertEqual(self.window._selected_ids(), first)
        self.assertEqual(table.selection_state(), Qt.PartiallyChecked)
        order = header.sortIndicatorSection(), header.sortIndicatorOrder()
        self.click_header_check()
        self.assertEqual(len(self.window._selected_ids()), 2)
        self.assertEqual((header.sortIndicatorSection(), header.sortIndicatorOrder()), order)  # без сортировки
        self.click_header_check()
        self.assertEqual(self.window._selected_ids(), [])
        self.assertEqual(table.selection_state(), Qt.Unchecked)
        # обычный щелчок по письму по-прежнему выделяет только его
        rect = table.visualRect(table.model().index(1, main_window.COL["subject"]))
        self.click_header_check()
        QTest.mouseClick(table.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
        self.assertEqual(len(self.window._selected_ids()), 1)

    def header_mouse(self, kind, x, buttons=Qt.LeftButton):
        header = self.window.table.horizontalHeader()
        pos = QPointF(x, header.height() / 2)
        button = Qt.NoButton if kind == QEvent.MouseMove else Qt.LeftButton
        QApplication.sendEvent(header.viewport(), QMouseEvent(
            kind, pos, QPointF(header.viewport().mapToGlobal(pos.toPoint())), button, buttons, Qt.NoModifier))
        self.app.processEvents()

    def test_check_column_can_be_dragged_and_position_survives_restart(self):
        header = self.window.table.horizontalHeader()
        check, subject = main_window.COL["check"], main_window.COL["subject"]
        start = header.sectionViewportPosition(check) + header.sectionSize(check) // 2
        target = header.sectionViewportPosition(subject) + header.sectionSize(subject) - 3
        self.window.table.clearSelection()
        self.header_mouse(QEvent.MouseButtonPress, start)
        for x in range(start + 4, target, 20):
            self.header_mouse(QEvent.MouseMove, x)
        self.header_mouse(QEvent.MouseMove, target)
        self.header_mouse(QEvent.MouseButtonRelease, target, Qt.NoButton)
        self.assertGreater(header.visualIndex(check), header.visualIndex(subject) - 1)
        self.assertNotEqual(header.visualIndex(check), 0)
        self.assertEqual(self.window._selected_ids(), [])  # перетаскивание не выделяет все письма
        moved = [header.logicalIndex(i) for i in range(header.count())]
        self.window._save_ui()
        self.close_window()
        self.window = main_window.MainWindow(self.db)
        self.window.show()
        self.app.processEvents()
        header = self.window.table.horizontalHeader()
        self.assertEqual([header.logicalIndex(i) for i in range(header.count())], moved)
        # ширину последней видимой колонки можно менять, как и остальных
        self.assertFalse(header.stretchLastSection())
        last = max((i for i in range(header.count()) if not header.isSectionHidden(i)), key=header.visualIndex)
        self.assertEqual(header.sectionResizeMode(last), header.ResizeMode.Interactive)
        self.window.table.setColumnWidth(last, 61)
        self.assertEqual(self.window.table.columnWidth(last), 61)
        # «Порядок колонок по умолчанию» возвращает флажки выбора в начало
        self.window._default_column_order()
        self.assertEqual([main_window.COLUMNS[header.logicalIndex(i)][0] for i in range(header.count())],
                         main_window.DEFAULT_COLUMN_ORDER)

    def test_double_click_on_check_column_does_not_open_message(self):
        table = self.window.table
        table.clearSelection()
        with mock.patch.object(self.window, "_open_selected") as open_selected:
            rect = table.visualRect(table.model().index(0, main_window.COL["check"]))
            QTest.mouseDClick(table.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
            self.app.processEvents()
            open_selected.assert_not_called()


if __name__ == "__main__":
    unittest.main()
