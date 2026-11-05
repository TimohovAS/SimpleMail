"""«Показать картинки» запоминается для письма: при возврате и после перезапуска."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from mailclient.db import Account, Database, Message
from mailclient.ui import main_window, viewer

HTML = '<p>Hi</p><img src="https://example.com/tracker.png">'


class TestRememberRemoteImages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mail.db"
        self.db = Database(self.path)
        self.account = self.db.save_account(Account(email="me@example.com", storage_dir=self.tmp.name))

    def add(self, **kw):
        fields = dict(account_id=self.account, subject="s", sender="Shop <shop@example.com>",
                      message_id="<a1@example.com>", body_html=HTML)
        fields.update(kw)
        return self.db.message(self.db.add_message(Message(**fields)))

    def test_db_remembers_by_message_id_and_survives_restart(self):
        m = self.add()
        other = self.add(message_id="<b2@example.com>")
        self.assertFalse(self.db.remote_images_allowed(m))
        self.db.allow_remote_images(m)
        self.db.allow_remote_images(m)  # повторное нажатие не ломает
        self.assertTrue(self.db.remote_images_allowed(m))
        self.assertFalse(self.db.remote_images_allowed(other))
        # копия того же письма в другой папке (Gmail) или после повторной загрузки
        copy = self.add(server_folder="[Gmail]/All Mail", uid=77, message_id="<A1@example.com>")
        self.assertTrue(self.db.remote_images_allowed(copy))
        # чужое письмо с тем же Message-ID от другого отправителя — нет
        forged = self.add(sender="spam@evil.test", uid=78)
        self.assertFalse(self.db.remote_images_allowed(forged))
        self.db.close()
        self.db = Database(self.path)
        self.assertTrue(self.db.remote_images_allowed(m))
        self.db.close()

    def test_message_without_message_id(self):
        a = self.add(message_id="")
        b = self.add(message_id="", uid=5)
        self.db.allow_remote_images(a)
        self.assertTrue(self.db.remote_images_allowed(a))
        self.assertFalse(self.db.remote_images_allowed(b))
        self.db.close()

    def test_viewer_gets_permission_when_returning_to_message(self):
        first = self.add()
        second = self.add(message_id="<b2@example.com>", subject="other")
        with mock.patch.object(main_window, "run_task"), \
                mock.patch("mailclient.ui.mail_watch.MailWatcher"), \
                mock.patch.object(viewer, "HAS_WEBENGINE", False):
            window = main_window.MainWindow(self.db)
            try:
                calls = []
                window.viewer.show_message = lambda m, allow_remote=False: calls.append((m.id, allow_remote))
                window._show_message(first.id)
                self.assertEqual(calls[-1], (first.id, False))
                window.viewer.remote_loaded.emit(window._shown)  # нажата «Показать картинки»
                window._show_message(second.id)
                self.assertEqual(calls[-1], (second.id, False))
                window._show_message(first.id)
                self.assertEqual(calls[-1], (first.id, True))
            finally:
                with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
                    window.close()
        self.db.close()


if __name__ == "__main__":
    unittest.main()
