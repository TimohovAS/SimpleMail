"""Отправка в фоне: окно письма закрывается сразу, письмо ждёт в «Исходящих»,
после отправки переходит в «Отправленные», при ошибке остаётся в «Исходящих» с сообщением."""
import os
import smtplib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mailclient import delivery, protocols
from mailclient.db import Account, Database


class BackgroundSendCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from mailclient.ui import compose_dialog, main_window, viewer
        self.main_window, self.compose_dialog = main_window, compose_dialog
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.addCleanup(self.db.close)
        self.aid = self.db.save_account(Account(name="A", email="a@example.com", username="a",
                                                imap_host="i", smtp_host="s", storage_dir=self.tmp.name))
        self.tasks = []  # сетевые задачи не запускаются сами: тест решает, когда «ответит сервер»
        for patch in (mock.patch.object(main_window, "run_task", side_effect=self.capture),
                      mock.patch("mailclient.ui.mail_watch.MailWatcher"),
                      mock.patch.object(viewer, "HAS_WEBENGINE", False),
                      mock.patch.object(protocols, "get_password", return_value="x"),
                      mock.patch.object(delivery, "append_sent")):
            patch.start()
            self.addCleanup(patch.stop)
        self.window = main_window.MainWindow(self.db)
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self.close_window)

    def close_window(self):
        for box in self.notices():
            box.close()
        self.window.close()
        self.app.processEvents()

    def capture(self, fn, *args, local=False, on_done=None, on_error=None, on_progress=None, **kwargs):
        if local:
            result = fn(*args, **kwargs)
            if on_done:
                on_done(result)
            return
        self.tasks.append((fn, args, kwargs, on_done, on_error))

    def run_delivery(self):
        sends = [task for task in self.tasks if task[0] is delivery.deliver_outbox]
        self.assertEqual(len(sends), 1)
        self.tasks.remove(sends[0])
        fn, args, kwargs, on_done, on_error = sends[0]
        try:
            result = fn(*args, **kwargs)
        except Exception as error:
            from mailclient.errors import explain
            on_error(explain(error))
        else:
            on_done(result)
        self.app.processEvents()

    def notices(self):
        from PySide6.QtWidgets import QMessageBox
        return [box for box in self.window.findChildren(QMessageBox) if box.isVisible()]

    def compose_and_send(self, subject="Привет"):
        dlg = self.compose_dialog.ComposeDialog(self.db, account_id=self.aid, to="b@example.com",
                                                subject=subject, body="Текст", parent=self.window)
        dlg.queued.connect(self.window._deliver_in_background)
        dlg.show()
        self.app.processEvents()
        dlg._send()
        self.app.processEvents()
        return dlg

    def folder_of(self, mid):
        m = self.db.message(mid, with_raw=False)
        return m.folder, m.delivery_state

    def outbox_ids(self):
        return [r[0] for r in self.db.conn().execute("SELECT id FROM messages WHERE folder='outbox'")]

    def test_window_closes_at_once_and_message_moves_from_outbox_to_sent(self):
        dlg = self.compose_and_send()
        # Окно закрылось, не дожидаясь сервера; письмо уже в «Исходящих».
        self.assertFalse(dlg.isVisible())
        [mid] = self.outbox_ids()
        self.assertEqual(self.folder_of(mid), ("outbox", "pending"))
        self.assertIn(mid, self.window._delivering)
        smtp = mock.Mock(); smtp.send_message.return_value = {}
        with mock.patch.object(protocols, "smtp_connect", return_value=smtp):
            self.run_delivery()
        smtp.send_message.assert_called_once()
        self.assertEqual(self.folder_of(mid)[0], "sent")
        self.assertEqual(self.window._delivering, {})
        self.assertIn("отправлено", self.window.status.currentMessage())
        self.assertEqual(self.notices(), [])

    def test_refused_message_stays_in_outbox_with_reason(self):
        self.compose_and_send("Отказ")
        [mid] = self.outbox_ids()
        refused = smtplib.SMTPRecipientsRefused({"b@example.com": (550, b"No such user")})
        smtp = mock.Mock(); smtp.send_message.side_effect = refused
        with mock.patch.object(protocols, "smtp_connect", return_value=smtp):
            self.run_delivery()
        self.assertEqual(self.folder_of(mid), ("outbox", "failed"))
        [box] = self.notices()
        self.assertFalse(box.isModal())  # сообщение не останавливает работу с программой
        self.assertIn("«Отказ»", box.text())
        self.assertIn("«Исходящих»", box.text())
        self.assertIn("No such user", box.text())

    def test_no_connection_keeps_message_queued_for_next_check(self):
        self.compose_and_send("Без связи")
        [mid] = self.outbox_ids()
        with mock.patch.object(protocols, "smtp_connect", side_effect=ConnectionRefusedError("refused")):
            self.run_delivery()
        self.assertEqual(self.folder_of(mid), ("outbox", "pending"))
        [box] = self.notices()
        self.assertIn("повторит отправку", box.text())

    def test_outbox_shows_sending_while_delivery_runs(self):
        self.compose_and_send("В пути")
        [mid] = self.outbox_ids()
        self.window.current = (self.aid, "outbox")
        self.window.reload_messages()
        table, mw = self.window.table, self.main_window
        row = next(r for r in range(table.rowCount()) if table.item(r, 0).data(mw.ROLE_ID) == mid)
        self.assertEqual(table.item(row, mw.COL["status"]).text(), "Отправляется…")
        smtp = mock.Mock(); smtp.send_message.return_value = {}
        with mock.patch.object(protocols, "smtp_connect", return_value=smtp):
            self.run_delivery()

    def test_exit_waits_for_message_being_sent(self):
        self.compose_and_send("При выходе")
        events = list(self.window._delivering.values())
        self.window._quitting = True
        self.window.close()
        self.app.processEvents()
        for event in events:
            self.assertIn(event, self.window.pending_mail_operations)
        self.tasks.clear()


if __name__ == "__main__":
    unittest.main()
