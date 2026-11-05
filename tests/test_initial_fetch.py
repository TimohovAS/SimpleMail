"""Первая проверка ящика: свежие письма сразу, число оставшихся на сервере, прогресс с числами."""
import unittest
from unittest import mock

from mailclient import protocols, sync
from test_complete_features import FolderIMAP
from test_grok_fixes import Fixture


class TestInitialFetch(Fixture):
    def test_first_sync_counts_older_messages_and_reports_numbers(self):
        self.db.set_setting("initial_fetch_limit", "2")
        mails = {uid: (protocols.build_message(self.acc, "b@example.com", f"Mail {uid}", "body").as_bytes(), "")
                 for uid in range(1, 6)}
        fake = FolderIMAP({"INBOX": dict(mails)})
        progress = []
        with mock.patch.object(protocols, "imap_connect", return_value=fake), \
                mock.patch.object(protocols, "get_password", return_value="test"):
            protocols.sync_account(self.db, self.acc, progress=progress.append)
        subjects = {row[0] for row in self.db.conn().execute("SELECT subject FROM messages")}
        self.assertEqual(subjects, {"Mail 4", "Mail 5"})  # самые свежие
        self.assertEqual(self.db.get_setting(f"history_skipped:{self.acc.id}:INBOX"), "3")
        numbered = [item for item in progress if getattr(item, "total", 0)]
        self.assertEqual((numbered[-1].done, numbered[-1].total), (2, 2))
        self.assertIn("2", str(numbered[-1]))
        # Следующая проверка не пересчитывает «старые» и не скачивает их без согласия.
        fake.boxes["INBOX"][6] = (protocols.build_message(self.acc, "b@example.com", "Mail 6", "body").as_bytes(), "")
        with mock.patch.object(protocols, "imap_connect", return_value=fake), \
                mock.patch.object(protocols, "get_password", return_value="test"):
            protocols.sync_account(self.db, self.acc)
        self.assertEqual(self.db.conn().execute("SELECT count(*) FROM messages").fetchone()[0], 3)
        self.assertEqual(self.db.get_setting(f"history_skipped:{self.acc.id}:INBOX"), "3")
        self.db.delete_account(self.acc.id)
        self.assertIsNone(self.db.get_setting(f"history_skipped:{self.acc.id}:INBOX"))

    def test_account_synced_before_the_question_still_gets_it(self):
        mails = {uid: (protocols.build_message(self.acc, "b@example.com", f"Mail {uid}", "body").as_bytes(), "")
                 for uid in range(1, 6)}
        fake = FolderIMAP({"INBOX": dict(mails)})
        self.db.set_setting("initial_fetch_limit", "2")
        with mock.patch.object(protocols, "imap_connect", return_value=fake), \
                mock.patch.object(protocols, "get_password", return_value="test"):
            protocols.sync_account(self.db, self.acc)
            # Как у ящика старой версии: отметки о пропущенных нет.
            self.db.conn().execute("DELETE FROM settings WHERE key LIKE 'history_skipped:%'")
            self.db.conn().commit()
            protocols.sync_account(self.db, self.acc)
        self.assertEqual(self.db.get_setting(f"history_skipped:{self.acc.id}:INBOX"), "3")
        # После решения пользователя отметка больше не обновляется.
        self.db.set_setting(f"history:{self.acc.id}", "declined")
        self.db.set_setting(f"history_skipped:{self.acc.id}:INBOX", "0")
        with mock.patch.object(protocols, "imap_connect", return_value=fake), \
                mock.patch.object(protocols, "get_password", return_value="test"):
            protocols.sync_account(self.db, self.acc)
        self.assertEqual(self.db.get_setting(f"history_skipped:{self.acc.id}:INBOX"), "0")

    def test_intercepted_connection_gets_longer_timeout(self):
        sock = mock.Mock()
        sock.getpeercert.return_value = {"issuer": ((("organizationName", "AO Kaspersky Lab"),),)}
        im = mock.Mock(sock=sock, capabilities=("IMAP4rev1",))
        acc = protocols.Account(email="u@gmail.com", username="u@gmail.com", imap_host="imap.gmail.com")
        with mock.patch.object(protocols.imaplib, "IMAP4_SSL", return_value=im):
            protocols.imap_connect(acc, "password")
        sock.settimeout.assert_called_once_with(protocols.INTERCEPTED_TIMEOUT)

    def test_progress_text_is_plain_string(self):
        text = sync.ProgressText("Загрузка 1 из 2", 1, 2)
        self.assertEqual(text, "Загрузка 1 из 2")
        self.assertEqual((text.done, text.total), (1, 2))


if __name__ == "__main__":
    unittest.main()
