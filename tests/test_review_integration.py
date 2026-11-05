"""Regression checks for GitHub changes combined with the Windows updater."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from mailclient import backup
from mailclient.db import Account, Database


class TestRestoreIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "data" / "mail.db")
        self.addCleanup(self.db.close)
        self.storage = self.root / "mailbox"
        self.storage.mkdir()
        self.aid = self.db.save_account(Account(email="original@example.com", username="original",
                                                imap_host="imap.example.com", smtp_host="smtp.example.com",
                                                storage_dir=str(self.storage)))

    def test_restore_replaces_changed_file_even_if_its_size_is_unchanged(self):
        file = self.storage / "saved attachment.pdf"
        file.write_bytes(b"original")
        archive = backup.create_backup(self.db, self.root / "mail.smbak")
        file.write_bytes(b"modified")
        stage = backup.prepare_restore(self.db, archive)
        self.db.close()
        backup.install_restore(stage, Path(self.db.path).parent)
        self.assertEqual(file.read_bytes(), b"original")

    def test_restore_does_not_copy_password_from_different_account_with_same_id(self):
        archive = backup.create_backup(self.db, self.root / "mail.smbak")
        account = self.db.account(self.aid)
        account.email = "another@example.com"
        self.db.save_account(account)
        self.db.set_setting(f"pw:{self.aid}", "another-account-password")
        stage = backup.prepare_restore(self.db, archive)
        with closing(sqlite3.connect(Path(stage) / "mail.db")) as restored:
            password = restored.execute("SELECT value FROM settings WHERE key=?", (f"pw:{self.aid}",)).fetchone()
        self.assertIsNone(password)
