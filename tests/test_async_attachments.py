"""Вложения исходного письма и черновика читаются в фоне: окно письма открывается сразу."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest import mock

from PySide6.QtWidgets import QApplication

from mailclient import protocols
from mailclient.db import Account
from mailclient.ui.compose_dialog import ComposeDialog
from mailclient.ui.worker import local_pool
from test_complete_features import Fixture


def wait():
    local_pool().waitForDone(5000)
    QApplication.processEvents()


class TestAsyncAttachments(Fixture):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        raw = protocols.build_message(Account(email="a@example.com"), "me@example.com", "С файлом", "Текст",
                                      attachments=[("big.pdf", b"%PDF" * 100)]).as_bytes()
        self.mid = self.add(raw)

    def test_forward_window_opens_before_attachments_are_read(self):
        with mock.patch.object(self.db, "message_attachments", wraps=self.db.message_attachments) as read:
            dialog = ComposeDialog(self.db, account_id=self.acc.id, subject="Fwd: С файлом",
                                   quote_header="Пересылаемое", attachments_from=self.mid)
            self.assertEqual(dialog.attachments, [])
            self.assertFalse(dialog.att_loading.isHidden())
            wait()
        read.assert_called_once_with(self.mid)
        self.assertEqual(dialog.attachments, [("big.pdf", b"%PDF" * 100)])
        self.assertTrue(dialog.att_loading.isHidden())
        self.assertFalse(dialog._dirty)  # вложения исходного письма — не правка
        dialog.reject()
        self.assertIsNone(dialog._draft_id)

    def test_send_or_close_before_background_read_keeps_attachments(self):
        dialog = ComposeDialog(self.db, account_id=self.acc.id, subject="Fwd", quote_header="Пересылаемое",
                               attachments_from=self.mid)
        dialog.to.setText("x@example.com")
        dialog._save_draft()  # автосохранение ждёт вложения
        self.assertIsNone(dialog._draft_id)
        dialog.reject()  # закрытие дочитывает их сразу и сохраняет черновик целиком
        self.assertEqual(self.db.message_attachments(dialog._draft_id), [("big.pdf", b"%PDF" * 100)])
        wait()  # запоздавший фоновый результат не добавляет вложения второй раз
        self.assertEqual(len(dialog.attachments), 1)

        reopened = ComposeDialog(self.db, draft_id=dialog._draft_id)
        self.assertEqual(reopened.attachments, [])
        wait()
        self.assertEqual(reopened.attachments, [("big.pdf", b"%PDF" * 100)])
        reopened._autosave.stop()
