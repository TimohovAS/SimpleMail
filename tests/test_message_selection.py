"""Выбранное письмо открывается при загрузке и обновлении списка."""
import os
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
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
        for patch in (mock.patch.object(main_window, "run_task"),
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

    def test_startup_opens_first_message_in_sort_order(self):
        self.assert_displayed(self.messages[0][1])

    def test_switch_account_opens_first_message_and_click_keeps_it_open(self):
        self.window.table.selectRow(0)
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(1).child(0))
        self.assert_displayed(self.messages[1][1])
        rect = self.window.table.visualItemRect(self.window.table.item(0, 0))
        QTest.mouseClick(self.window.table.viewport(), Qt.LeftButton, pos=rect.center())
        self.assert_displayed(self.messages[1][1])

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

    def test_first_message_respects_filter_and_sort_order(self):
        self.window.search.setText("Message 0")
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(1).child(0))
        self.assert_displayed(self.messages[1][0])
        self.window.search.clear()
        self.window.table.sortByColumn(main_window.COL["date"], Qt.AscendingOrder)
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(0).child(0))
        self.assert_displayed(self.messages[0][0])

    def test_empty_folder_or_no_filter_results_clears_preview(self):
        self.window.table.selectRow(0)
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(0).child(1))
        self.assertIsNone(self.window._selected_id())
        self.assertIsNone(self.window._shown)
        self.assertEqual(self.window.viewer.text.toPlainText(), "")
        self.assertFalse(self.window.acts["reply"].isEnabled())
        self.window.search.setText("no matching messages")
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(1).child(0))
        self.assertIsNone(self.window._selected_id())
        self.assertIsNone(self.window._shown)

    def test_attachment_open_save_and_forward_use_database_without_eml(self):
        em = EmailMessage()
        em.set_content("Offline body")
        em.add_attachment(b"database attachment", maintype="application", subtype="pdf", filename="offline.pdf")
        message = parse_message(em.as_bytes())
        message.account_id = self.accounts[0]
        message.date_ts = 1700000020
        message.seen = True
        mid = self.db.add_message(message)
        Path(self.db.message(mid).raw_path).unlink()
        self.window.reload_messages(keep_selection=False)
        self.assert_displayed(mid)
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

    def finish_mail_operation(self):
        call = main_window.run_task.call_args
        self.assertIs(call.args[0], main_window.change_messages)
        result = call.args[0](*call.args[1:])
        call.kwargs["on_done"](result)
        return result

    def test_delete_waits_for_success_and_undo_restores_last_batch(self):
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
        mid = self.window._selected_id()
        main_window.move_to_trash(self.db, mid)
        self.window.close()
        self.window = main_window.MainWindow(self.db)
        self.app.processEvents()
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(0).child(3))
        self.assertTrue(self.window.acts["restore"].isEnabled())
        main_window.run_task.reset_mock()
        with mock.patch.object(main_window.QMessageBox, "question", return_value=main_window.QMessageBox.No):
            self.window._delete()
        main_window.run_task.assert_not_called()
        self.assertEqual(self.db.message(mid).folder, "trash")
        self.window.acts["restore"].trigger()
        self.finish_mail_operation()
        self.assertEqual(self.db.message(mid).folder, "inbox")


if __name__ == "__main__":
    unittest.main()
