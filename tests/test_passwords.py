"""Credential backend initialization must not race between IMAP workers."""
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from mailclient import passwords
from mailclient.db import Account


class TestCredentialConcurrency(unittest.TestCase):
    def test_parallel_password_reads_do_not_enter_native_backend_concurrently(self):
        db = mock.Mock()
        db.account.return_value = Account()
        db.get_setting.side_effect = lambda key, default=None: default
        barrier = threading.Barrier(6)
        active = 0
        peak = 0
        guard = threading.Lock()

        def native_read(*args):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(.02)
            with guard:
                active -= 1
            return "test-password"

        def read(account_id):
            barrier.wait(timeout=5)
            return passwords.get_password(db, account_id)

        with mock.patch.object(passwords, "keyring") as keyring:
            keyring.get_password.side_effect = native_read
            with ThreadPoolExecutor(max_workers=6) as pool:
                self.assertEqual(list(pool.map(read, range(6))), ["test-password"] * 6)
        self.assertEqual(peak, 1)
