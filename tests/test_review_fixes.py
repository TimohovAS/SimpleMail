"""Исправления по ревью 0.9: кодировки заголовков, безопасные ссылки, блокировки, синхронизация."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mailclient.mailparse import attachments, decode_mime_header, parse_message


class TestEightBitHeaders(unittest.TestCase):
    def raw(self, encoding):
        return ("Subject: Привет мир\r\nFrom: Иван Петров <ivan@example.com>\r\n"
                "To: Пётр <petr@example.com>, x@example.com\r\nContent-Type: text/plain\r\n\r\nтекст").encode(encoding)

    def test_unencoded_headers_in_common_charsets(self):
        for encoding in ("utf-8", "cp1251", "koi8-r"):
            with self.subTest(encoding=encoding):
                m = parse_message(self.raw(encoding))
                self.assertEqual(m.subject, "Привет мир")
                self.assertEqual(m.sender, "Иван Петров <ivan@example.com>")
                self.assertEqual(m.recipients, "Пётр <petr@example.com>, x@example.com")

    def test_rfc2047_spacing_unchanged(self):
        cases = {
            b"=?utf-8?B?0J/RgNC40LLQtdGC?= =?utf-8?B?INC80LjRgA==?= tail": "Привет мир tail",
            b"Re: [list] =?utf-8?Q?=D0=BF=D1=80=D0=B8?= done": "Re: [list] при done",
            b"plain ascii": "plain ascii",
            b"=?x-unknown?Q?abc?=": "abc",
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(parse_message(b"Subject: " + header + b"\r\n\r\nx").subject, expected)

    def test_unencoded_attachment_name(self):
        for encoding in ("utf-8", "cp1251"):
            raw = ("Content-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\nContent-Type: text/plain\r\n\r\nhi\r\n"
                   "--b\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=\"Счёт 1.pdf\"\r\n\r\n"
                   "PDF\r\n--b--\r\n").encode(encoding)
            with self.subTest(encoding=encoding):
                self.assertEqual([name for name, _ in attachments(raw)], ["Счёт 1.pdf"])

    def test_plain_string_passthrough(self):
        self.assertEqual(decode_mime_header("Обычный текст"), "Обычный текст")
        self.assertEqual(decode_mime_header(None), "")


class TestLinkSafety(unittest.TestCase):
    def test_only_web_links_open_externally(self):
        from PySide6.QtCore import QUrl
        from mailclient.ui.viewer import link_action
        expected = {
            "https://example.com/x": "open", "HTTP://example.com": "open",
            "mailto:a@example.com": "mailto",
            "file:///C:/Windows/System32/calc.exe": "block", "ms-msdt:/id PCWDiagnostic": "block",
            "search-ms:query=x": "block", "javascript:alert(1)": "block", "http:///nohost": "block",
            "\\\\server\\share\\x.exe": "block",
        }
        for url, action in expected.items():
            with self.subTest(url=url):
                self.assertEqual(link_action(QUrl(url)), action)

    def test_mailto_parameters(self):
        from PySide6.QtCore import QUrl
        from mailclient.ui.viewer import parse_mailto
        url = QUrl("mailto:a@example.com?subject=%D0%9F%D1%80%D0%B8&cc=c@example.com&body=hi%20there")
        self.assertEqual(parse_mailto(url), {"to": "a@example.com", "cc": "c@example.com",
                                             "subject": "При", "body": "hi there"})


class TestSyncYieldsToUser(unittest.TestCase):
    """п.1: удаление/отправка/пометка не ждут конца долгой синхронизации."""

    def test_user_operation_runs_between_batches(self):
        import tempfile
        import threading
        import time
        from pathlib import Path
        from unittest import mock
        from mailclient import protocols, sync
        from mailclient.db import Account, AccountLock, Database
        from test_core import FakeIMAP, HAM, make_raw

        class SlowIMAP(FakeIMAP):
            def __init__(self, messages):
                super().__init__(messages)
                self.body_fetches = 0
                self.flag_fetches = 0
                self.first_fetch = threading.Event()

            def uid(self, cmd, *args):
                if cmd == "FETCH" and "BODY.PEEK[]" in args[1]:
                    self.body_fetches += 1
                    self.first_fetch.set()
                    time.sleep(0.2)
                elif cmd == "FETCH":
                    self.flag_fetches += 1
                return super().uid(cmd, *args)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "t.db")
        self.addCleanup(db.close)
        acc_id = db.save_account(Account(name="T", email="me@example.com", username="me",
                                         imap_host="i", smtp_host="s"))
        messages = {uid: make_raw(f"user{uid} <u{uid}@example.com>", f"Тема {uid}", f"Текст {uid}")
                    for uid in range(1, 7)}
        fake = SlowIMAP(messages)
        results, events = [], []

        def run_sync():
            try:
                results.append(protocols.sync_account(db, db.account(acc_id)))
            finally:
                events.append(("sync_done", time.monotonic()))
                db.close()

        with mock.patch.object(protocols, "imap_connect", return_value=fake), \
             mock.patch.object(protocols, "get_password", return_value="x"), \
             mock.patch.object(sync, "FETCH_BATCH", 1):
            worker = threading.Thread(target=run_sync)
            worker.start()
            self.assertTrue(fake.first_fetch.wait(5))
            with db.account_lock(acc_id):
                events.append(("user_op", time.monotonic()))
            worker.join(20)

        self.assertFalse(worker.is_alive())
        self.assertEqual([name for name, _ in events], ["user_op", "sync_done"])
        self.assertEqual(results, [6])
        self.assertEqual(db.conn().execute("SELECT COUNT(*) FROM messages").fetchone()[0], 6)
        self.assertLessEqual(fake.body_fetches, 7)  # одна пачка могла повториться после уступки

    def test_lock_reports_waiters(self):
        import threading
        from mailclient.db import AccountLock, SyncPreempted
        lock = AccountLock()
        lock.acquire()
        self.assertFalse(lock.contended())
        lock.yield_point()  # никто не ждёт — продолжаем
        got = threading.Event()
        waiter = threading.Thread(target=lambda: (lock.acquire(), got.set(), lock.release()))
        waiter.start()
        for _ in range(200):
            if lock.contended():
                break
            threading.Event().wait(0.01)
        self.assertTrue(lock.contended())
        with self.assertRaises(SyncPreempted):
            lock.yield_point()
        lock.release()
        self.assertTrue(got.wait(5))
        waiter.join(5)
        self.assertFalse(lock.contended())


class TestIdleOnlyOnChange(unittest.TestCase):
    """п.2: IDLE запускает проверку только при новых письмах."""

    def test_no_sync_after_quiet_idle_cycle(self):
        import threading
        from unittest import mock
        from mailclient import idle
        from mailclient.db import Account

        changes = []
        watcher = idle.MailWatcher(mock.Mock(), Account(id=1), changes.append, lambda aid: None)
        fake = mock.Mock()
        fake.capability.return_value = ("OK", [b"IMAP4rev1 IDLE"])
        fake.select.return_value = ("OK", [b"1"])
        cycles = iter([False, False, True])

        def idle_once(im, stopped, duration=idle.IDLE_DURATION):
            try:
                return next(cycles)
            except StopIteration:
                watcher.stopped.set()
                return False

        with mock.patch.object(idle, "imap_connect", return_value=fake), \
             mock.patch.object(idle, "get_password", return_value="x"), \
             mock.patch.object(idle, "prepare_idle"), \
             mock.patch.object(idle, "idle_once", side_effect=idle_once), \
             mock.patch.object(watcher.stopped, "wait", return_value=False):
            watcher.run()
        # Одна проверка после подключения и одна — после цикла с новыми письмами.
        self.assertEqual(changes, [1, 1])


class TestClearErrors(unittest.TestCase):
    """п.6: понятные тексты вместо [b'...']."""

    def test_imap_errors_are_readable(self):
        import imaplib
        from mailclient.errors import explain
        from mailclient.sync import checked
        with self.assertRaises(imaplib.IMAP4.error) as ctx:
            checked(("NO", [b"[NONEXISTENT] Mailbox doesn't exist: Spam"]))
        info = explain(ctx.exception)
        self.assertNotIn("b'", info.short())
        self.assertEqual(info.summary, "Папка не найдена на сервере")
        self.assertEqual(explain(imaplib.IMAP4.abort("socket error: EOF")).summary,
                         "Связь с сервером приёма почты прервалась")
        # слово «auth» в чужом тексте больше не означает неверный пароль
        self.assertNotEqual(explain(imaplib.IMAP4.error("AUTHORITY server busy")).summary,
                            "Сервер не принял логин или пароль")
        self.assertEqual(explain(imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Authentication failed.")).summary,
                         "Сервер не принял логин или пароль")

    def test_smtp_error_description(self):
        import smtplib
        from mailclient.errors import describe
        text = describe(smtplib.SMTPRecipientsRefused({"x@example.com": (550, b"5.1.1 User unknown")}))
        self.assertIn("550 5.1.1 User unknown", text)
        self.assertNotIn("b'", text)


class TestPermanentSmtpFailure(unittest.TestCase):
    """п.7: отказ 5xx не переотправляется при каждой проверке, 4xx — повторяется."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from mailclient.db import Account, Database
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.acc = self.db.account(self.db.save_account(Account(
            name="T", email="me@example.com", username="me", imap_host="i", smtp_host="s")))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def send(self, error):
        import smtplib
        from unittest import mock
        from mailclient import delivery
        from mailclient.protocols import build_message
        mid = self.db.save_composition(self.acc.id, build_message(self.acc, "x@example.com", "T", "b").as_bytes(), {}, queued=True)
        smtp = mock.Mock()
        smtp.send_message.side_effect = error
        with mock.patch.object(delivery.wire, "smtp_connect", return_value=smtp), \
             mock.patch.object(delivery.wire, "get_password", return_value="x"):
            result = delivery.deliver_outbox(self.db, mid)
            delivery.process_outbox(self.db, self.acc)
        return mid, result, smtp

    def test_5xx_marks_failed_and_is_not_retried(self):
        import smtplib
        mid, result, smtp = self.send(smtplib.SMTPRecipientsRefused({"x@example.com": (550, b"User unknown")}))
        self.assertEqual(result, "failed")
        self.assertEqual(smtp.send_message.call_count, 1)  # process_outbox не повторил
        m = self.db.message(mid)
        self.assertEqual((m.folder, m.delivery_state), ("outbox", "failed"))
        self.assertIn("User unknown", m.delivery_error)

    def test_4xx_stays_queued(self):
        import smtplib
        mid, result, smtp = self.send(smtplib.SMTPDataError(451, b"Try again later"))
        self.assertEqual(result, "queued")
        self.assertEqual(smtp.send_message.call_count, 2)  # повтор при проверке
        self.assertEqual(self.db.message(mid).delivery_state, "pending")


class TestSecondInstance(unittest.TestCase):
    """п.5: повторный запуск показывает окно работающей копии."""

    def test_activation_roundtrip(self):
        import subprocess
        import sys
        import tempfile
        root = str(__import__("pathlib").Path(__file__).resolve().parents[1])
        server_code = (
            "import sys; from PySide6.QtCore import QCoreApplication, QTimer\n"
            "from mailclient.instance import start_activation_server\n"
            "app = QCoreApplication([]); QTimer.singleShot(10000, app.quit)\n"
            "s = start_activation_server(sys.argv[1], lambda: (print('shown', flush=True), app.quit()))\n"
            "print('ready', flush=True); app.exec()\n")
        client_code = ("import sys; from mailclient.instance import notify_running\n"
                       "print(notify_running(sys.argv[1]))\n")
        env = dict(os.environ, PYTHONPATH=root)
        with tempfile.TemporaryDirectory() as data:
            idle = subprocess.run([sys.executable, "-c", client_code, data], env=env, cwd=root,
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(idle.stdout.strip(), "False")  # никто не слушает
            server = subprocess.Popen([sys.executable, "-c", server_code, data], env=env, cwd=root,
                                      stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(server.stdout.readline().strip(), "ready")
                client = subprocess.run([sys.executable, "-c", client_code, data], env=env, cwd=root,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(client.stdout.strip(), "True")
                self.assertEqual(server.stdout.readline().strip(), "shown")
            finally:
                server.kill()
                server.wait()

class TestConnectionReuse(unittest.TestCase):
    """п.8: несколько действий подряд — одно подключение и один вход."""

    def test_reuse_and_reconnect(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from mailclient import protocols
        from mailclient.db import Account, Database
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "t.db"); self.addCleanup(db.close)
        acc = db.account(db.save_account(Account(name="T", email="me@example.com", username="me",
                                                 imap_host="i", smtp_host="s")))
        connect = mock.Mock(side_effect=lambda *a: mock.Mock())
        protocols.close_pool()
        with mock.patch.object(protocols, "get_password", return_value="x"):
            for _ in range(5):
                with protocols.account_imap(db, acc, connect):
                    pass
            self.assertEqual(connect.call_count, 1)
            with self.assertRaises(RuntimeError):
                with protocols.account_imap(db, acc, connect):
                    raise RuntimeError("ошибка посреди операции")
            with protocols.account_imap(db, acc, connect):
                pass
            self.assertEqual(connect.call_count, 2)  # после ошибки — новое подключение
            with mock.patch.object(protocols.time, "monotonic", return_value=protocols.time.monotonic() + 3600):
                with protocols.account_imap(db, acc, connect):
                    pass
            self.assertEqual(connect.call_count, 3)  # давно не пользовались — переподключение
        with mock.patch.object(protocols, "get_password", return_value="другой"):
            with protocols.account_imap(db, acc, connect):
                pass
        self.assertEqual(connect.call_count, 4)  # сменили пароль — переподключение
        protocols.close_pool()


class TestPasswordSafety(unittest.TestCase):
    """п.11: пароль не попадает в резервную копию; совет включить STARTTLS."""

    def test_backup_has_no_plaintext_password_and_restore_keeps_current(self):
        import sqlite3
        import tempfile
        import zipfile
        from pathlib import Path
        from unittest import mock
        from mailclient import backup, passwords
        from mailclient.db import Account, Database
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        db = Database(root / "data" / "mail.db"); self.addCleanup(db.close)
        aid = db.save_account(Account(name="T", email="me@example.com", username="me", imap_host="i",
                                      smtp_host="s", storage_dir=str(root / "box")))
        with mock.patch.object(passwords, "keyring", None):
            self.assertFalse(passwords.set_password(db, aid, "Sekretnyj-Parol-123"))
        archive = backup.create_backup(db, root / "copy.smbak")
        with zipfile.ZipFile(archive) as z:
            data = z.read("mail.db")
        self.assertNotIn(b"Sekretnyj-Parol-123", data)
        stage = backup.prepare_restore(db, archive)
        with sqlite3.connect(Path(stage) / "mail.db") as restored:
            value = restored.execute("SELECT value FROM settings WHERE key=?", (f"pw:{aid}",)).fetchone()
        self.assertEqual(value, ("Sekretnyj-Parol-123",))

    def test_starttls_note(self):
        from unittest import mock
        from mailclient import protocols
        from mailclient.db import Account
        acc = Account(imap_host="i", smtp_host="s", imap_security="none", smtp_security="none")
        ok = mock.Mock()
        with mock.patch.object(protocols, "imap_connect", return_value=ok), \
             mock.patch.object(protocols, "smtp_connect", return_value=ok), \
             mock.patch.object(protocols, "_imap_offers_starttls", return_value=True), \
             mock.patch.object(protocols, "_smtp_offers_starttls", return_value=False):
            imap, smtp = protocols.test_connection(acc, "x")
        self.assertIn("STARTTLS", imap.note)
        self.assertEqual(smtp.note, "")


class TestReplyAddressing(unittest.TestCase):
    """п.13: ответ уходит на Reply-To; цепочка References сохраняется."""

    def test_reply_to_and_references(self):
        from mailclient.db import Account
        from mailclient.mailparse import parse_message
        from mailclient.protocols import build_message
        from mailclient.ui.main_window import MainWindow
        raw = (b"From: List robot <noreply@list.example>\r\nReply-To: discuss@list.example\r\n"
               b"To: me@example.com\r\nSubject: Topic\r\nMessage-ID: <3@x>\r\n"
               b"References: <1@x> <2@x>\r\n\r\nbody")
        m = parse_message(raw)
        self.assertEqual(MainWindow._reply_address(m), "discuss@list.example")
        refs = MainWindow._references(m)
        em = build_message(Account(email="me@example.com"), "discuss@list.example", "Re: Topic", "x",
                           in_reply_to=m.message_id, references=refs)
        self.assertEqual(em["In-Reply-To"], "<3@x>")
        self.assertEqual(em["References"], "<1@x> <2@x> <3@x>")
        plain = parse_message(b"From: a@example.com\r\nSubject: x\r\n\r\nb")
        self.assertEqual(MainWindow._reply_address(plain), "a@example.com")

    def test_reply_prefix_not_duplicated(self):
        from mailclient.ui.main_window import MainWindow
        for subject in ("Re: x", "RE[2]: x", "Отв: x", "AW: x", "Ответ: x"):
            self.assertEqual(MainWindow._re(subject), subject)
        self.assertEqual(MainWindow._re("Привет"), "Re: Привет")


class TestDangerousAttachments(unittest.TestCase):
    """п.14: полный список опасных расширений."""

    def test_extensions(self):
        from mailclient.ui.attachments_bar import is_dangerous
        for name in ("счёт.pdf.exe", "invoice.LNK", "a.hta", "b.jar", "c.reg", "d.iso", "e.docm",
                     "f.exe. ", "g.wsf", "h.cpl"):
            with self.subTest(name=name):
                self.assertTrue(is_dangerous(name))
        for name in ("отчёт.pdf", "фото.jpg", "table.xlsx", "архив.zip"):
            with self.subTest(name=name):
                self.assertFalse(is_dangerous(name))


class TestInternalDate(unittest.TestCase):
    """п.15: письмо без Date получает дату получения сервером, а не 1970 год."""

    def test_missing_date_uses_internaldate(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from mailclient import protocols
        from mailclient.db import Account, Database
        from test_core import FakeIMAP

        class DatedIMAP(FakeIMAP):
            def uid(self, cmd, *args):
                if cmd == "FETCH" and "BODY.PEEK[]" in args[1]:
                    self.assert_internaldate = "INTERNALDATE" in args[1]
                    return "OK", [(b'1 (UID 1 FLAGS () INTERNALDATE "04-Nov-2026 10:00:00 +0000" BODY[] {1}',
                                   self.messages[1]), b")"]
                return super().uid(cmd, *args)

        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "t.db"); self.addCleanup(db.close)
        acc = db.account(db.save_account(Account(name="T", email="me@example.com", username="me",
                                                 imap_host="i", smtp_host="s")))
        fake = DatedIMAP({1: b"From: a@example.com\r\nSubject: no date\r\n\r\nbody"})
        with mock.patch.object(protocols, "imap_connect", return_value=fake), \
             mock.patch.object(protocols, "get_password", return_value="x"):
            protocols.sync_account(db, acc)
        self.assertTrue(fake.assert_internaldate)
        self.assertEqual(db.conn().execute("SELECT date_ts FROM messages").fetchone()[0], 1793786400)


class TestBackupDoesNotBlock(unittest.TestCase):
    """п.17: архивация файлов идёт без блокировки ящиков; удалённое во время копии письмо не ломает её."""

    def test_lock_free_during_archiving_and_deleted_message(self):
        import tempfile
        import threading
        import zipfile
        from pathlib import Path
        from unittest import mock
        from mailclient import backup
        from mailclient.db import Account, Database
        from mailclient.mailparse import parse_message
        from test_core import make_raw
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        db = Database(root / "data" / "mail.db"); self.addCleanup(db.close)
        aid = db.save_account(Account(name="T", email="me@example.com", username="me", imap_host="i",
                                      smtp_host="s", storage_dir=str(root / "box")))
        ids = []
        for i in range(3):
            m = parse_message(make_raw(f"a{i}@example.com", f"S{i}", f"B{i}"))
            m.account_id, m.uid, m.server_folder, m.uidvalidity = aid, i + 1, "INBOX", 1
            ids.append(db.add_message(m))
        victim = Path(db.message(ids[0]).raw_path)
        original_write = zipfile.ZipFile.write
        state = {"checked": False, "free": None}

        def write(archive, filename, arcname=None, *args, **kwargs):
            if not state["checked"]:
                state["checked"] = True
                result = []
                t = threading.Thread(target=lambda: result.append(db.account_lock(aid).acquire(timeout=1)) or
                                     db.account_lock(aid).release())
                t.start(); t.join(5)
                state["free"] = result == [True]
                db.delete_message(ids[0])  # пользователь удалил письмо во время копирования
            if Path(filename) == victim and not victim.exists():
                raise FileNotFoundError(filename)
            return original_write(archive, filename, arcname, *args, **kwargs)

        with mock.patch.object(zipfile.ZipFile, "write", write):
            archive = backup.create_backup(db, root / "copy.smbak")
        self.assertTrue(state["free"])
        with zipfile.ZipFile(archive) as z:
            import json, sqlite3
            manifest = json.loads(z.read("manifest.json"))
            (root / "check.db").write_bytes(z.read("mail.db"))
        with sqlite3.connect(root / "check.db") as check:
            left = {row[0] for row in check.execute("SELECT id FROM messages")}
        self.assertEqual(left, set(ids[1:]))
        self.assertEqual(set(manifest["raw_paths"]), {str(i) for i in ids[1:]})


if __name__ == "__main__":
    unittest.main()
