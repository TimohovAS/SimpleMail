"""Проверяем настоящий обмен IMAP с локальным сервером, без внешней почты."""
import imaplib
import socketserver
import threading
import unittest
from unittest import mock

from mailclient import idle
from mailclient.db import Account


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        def send(data):
            self.wfile.write(data)
            self.wfile.flush()

        self.request.settimeout(3)
        send(b"* OK test IMAP server\r\n")
        idle_tag = None
        try:
            while line := self.rfile.readline():
                self.server.commands.append(line)
                if line == b"DONE\r\n":
                    if self.server.mode == "dead":
                        continue  # соединение молча оборвано сетевым экраном: ответа нет
                    if self.server.mode == "late":
                        send(b"* 2 EXISTS\r\n")
                    elif self.server.mode == "late_flags":
                        send(b"* 1 FETCH (FLAGS (\\Seen))\r\n")
                    elif self.server.mode == "late_expunge":
                        send(b"* 1 EXPUNGE\r\n")
                    send(idle_tag + b" OK IDLE finished\r\n")
                    continue
                tag, cmd, *args = line.strip().split()
                if cmd == b"CAPABILITY":
                    caps = b"IMAP4rev1" if self.server.mode == "unsupported" else b"IMAP4rev1 IDLE"
                    send(b"* CAPABILITY " + caps + b"\r\n" + tag + b" OK capabilities\r\n")
                elif cmd == b"LOGIN":
                    send(tag + b" OK authenticated\r\n")
                elif cmd == b"EXAMINE":
                    send(b"* 1 EXISTS\r\n" + tag + b" OK [READ-ONLY] selected\r\n")
                elif cmd == b"IDLE":
                    idle_tag = tag
                    self.server.entered.set()
                    if self.server.mode == "reject":
                        send(tag + b" NO IDLE not allowed\r\n")
                    elif self.server.mode == "disconnect":
                        send(b"* BYE maintenance\r\n")
                        return
                    elif self.server.mode == "early":
                        send(b"* 2 EXISTS\r\n+ idling\r\n")
                    elif self.server.mode == "push":
                        # Несколько ответов одним пакетом проверяют буферизацию.
                        send(b"+ idling\r\n* 1 FETCH (FLAGS (\\Seen))\r\n* 2 EXISTS\r\n")
                    elif self.server.mode == "complete":
                        send(b"+ idling\r\n" + tag + b" OK idle finished early\r\n")
                    elif self.server.mode == "flags":
                        send(b"+ idling\r\n* 1 FETCH (FLAGS (\\Seen))\r\n")
                    elif self.server.mode == "expunge":
                        send(b"+ idling\r\n* 1 EXPUNGE\r\n")
                    else:
                        send(b"+ idling\r\n")
                elif cmd == b"STATUS":
                    self.server.status_calls += 1
                    if self.server.mode == "nostatus":
                        send(tag + b" NO STATUS not allowed for selected mailbox\r\n")
                        continue
                    # «silent»: письмо пришло, а уведомление IDLE потерял антивирус.
                    uidnext = 2 + (self.server.status_calls > 1 and self.server.mode == "silent")
                    send(b'* STATUS "INBOX" (MESSAGES %d UIDNEXT %d UIDVALIDITY 7)\r\n'
                         % (uidnext - 1, uidnext) + tag + b" OK status\r\n")
                elif cmd == b"LOGOUT":
                    send(b"* BYE closing\r\n" + tag + b" OK logout\r\n")
                    return
        except (OSError, ValueError):
            pass


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, mode):
        super().__init__(("127.0.0.1", 0), Handler)
        self.mode = mode
        self.commands = []
        self.status_calls = 0
        self.entered = threading.Event()

    def __enter__(self):
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": .01}, daemon=True).start()
        return self

    def __exit__(self, *args):
        self.shutdown()
        self.server_close()


class TestIdleProtocol(unittest.TestCase):
    def connect(self, server):
        im = imaplib.IMAP4(*server.server_address, timeout=2)
        self.addCleanup(im.shutdown)
        im.login("test", "dummy")
        idle.prepare_idle(im)
        im.select("INBOX", readonly=True)
        return im

    def test_push_and_early_notification(self):
        for mode in ("push", "early", "late"):
            with self.subTest(mode=mode), Server(mode) as server:
                im = self.connect(server)
                self.assertTrue(idle.idle_once(im, threading.Event(), duration=.1))
                self.assertIn(b"DONE\r\n", server.commands)

    def test_quiet_renewal_and_no_false_notification_from_select(self):
        with Server("quiet") as server:
            im = self.connect(server)
            for _ in range(2):
                self.assertFalse(idle.idle_once(im, threading.Event(), duration=.02))
            self.assertEqual(server.commands.count(b"DONE\r\n"), 2)

    def test_flag_changes_and_deletions_trigger_sync_even_during_done(self):
        for mode in ("flags", "expunge", "late_flags", "late_expunge"):
            with self.subTest(mode=mode), Server(mode) as server:
                im = self.connect(server)
                self.assertTrue(idle.idle_once(im, threading.Event(), duration=.02))
                self.assertIn(b"DONE\r\n", server.commands)

    def test_rejected_idle_and_server_disconnect(self):
        for mode, error in (("reject", idle.IdleUnavailable), ("disconnect", imaplib.IMAP4.abort)):
            with self.subTest(mode=mode), Server(mode) as server:
                im = self.connect(server)
                with self.assertRaises(error):
                    idle.idle_once(im, threading.Event(), duration=.1)
                self.assertNotIn(b"DONE\r\n", server.commands)

    def test_server_completed_idle_does_not_receive_done(self):
        with Server("complete") as server:
            im = self.connect(server)
            self.assertFalse(idle.idle_once(im, threading.Event(), duration=.1))
            self.assertNotIn(b"DONE\r\n", server.commands)

    def test_stop_while_waiting(self):
        with Server("quiet") as server:
            im = self.connect(server)
            stopped = threading.Event()
            results = []
            thread = threading.Thread(target=lambda: results.append(idle.idle_once(im, stopped)))
            thread.start()
            self.assertTrue(server.entered.wait(1))
            stopped.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results, [False])

    def test_dead_connection_detected_after_done(self):
        with Server("dead") as server:
            im = self.connect(server)
            im.sock.settimeout(None)  # как у долгого таймаута через антивирус
            with mock.patch.object(idle, "RESPONSE_TIMEOUT", .2):
                with self.assertRaises(OSError):
                    idle.idle_once(im, threading.Event(), duration=.02)

    def test_mailbox_state(self):
        with Server("quiet") as server:
            im = self.connect(server)
            self.assertEqual(idle.mailbox_state(im), ((b"MESSAGES", 1), (b"UIDNEXT", 2), (b"UIDVALIDITY", 7)))
            self.assertEqual(im.sock.gettimeout(), 2)  # таймаут восстановлен


class TestMailWatcher(unittest.TestCase):
    def watcher(self, server, on_change, on_unavailable):
        db = mock.Mock()
        acc = Account(id=1, username="test", imap_host=server.server_address[0],
                      imap_port=server.server_address[1], imap_security="none")
        db.account.return_value = acc
        return idle.MailWatcher(db, acc, on_change, on_unavailable)

    def test_startup_and_push_trigger_sync_and_thread_closes(self):
        with Server("push") as server, mock.patch.object(idle, "get_password", return_value="dummy"):
            changes = []
            def changed(aid):
                changes.append(aid)
                if len(changes) == 2:
                    watcher.stop()
            watcher = self.watcher(server, changed, mock.Mock())
            watcher.start()
            watcher.join(3)
            self.assertFalse(watcher.is_alive())
            self.assertEqual(changes, [1, 1])
            watcher.db.close.assert_called_once()

    def test_server_without_idle_or_rejecting_idle_uses_fallback(self):
        for mode in ("unsupported", "reject"):
            with self.subTest(mode=mode), Server(mode) as server, mock.patch.object(idle, "get_password", return_value="dummy"):
                unavailable = mock.Mock()
                watcher = self.watcher(server, mock.Mock(), unavailable)
                watcher.start()
                watcher.join(3)
                self.assertFalse(watcher.is_alive())
                unavailable.assert_called_once_with(1)
                if mode == "unsupported":
                    self.assertFalse(any(b" IDLE" in cmd for cmd in server.commands))

    def test_reconnect_and_stop_during_retry(self):
        with Server("disconnect") as server, mock.patch.object(idle, "get_password", return_value="dummy"), mock.patch.object(idle, "RECONNECT_INITIAL", .01):
            changes = []
            def changed(aid):
                changes.append(aid)
                if len(changes) == 2:
                    watcher.stop()
            watcher = self.watcher(server, changed, mock.Mock())
            watcher.start()
            watcher.join(3)
            self.assertFalse(watcher.is_alive())
            self.assertEqual(changes, [1, 1])

    def test_new_mail_without_idle_notification_is_found(self):
        with Server("silent") as server, mock.patch.object(idle, "get_password", return_value="dummy"), \
                mock.patch.object(idle, "IDLE_DURATION", .02):
            changes = []
            def changed(aid):
                changes.append(aid)
                if len(changes) == 2:
                    watcher.stop()
            watcher = self.watcher(server, changed, mock.Mock())
            watcher.start()
            watcher.join(5)
            self.assertFalse(watcher.is_alive())
            self.assertEqual(changes, [1, 1])  # после подключения и после сверки STATUS

    def test_short_renewal_and_server_without_status(self):
        with Server("nostatus") as server, mock.patch.object(idle, "get_password", return_value="dummy"), \
                mock.patch.object(idle, "IDLE_DURATION", .02):
            changes = []
            watcher = self.watcher(server, changes.append, mock.Mock())
            watcher.start()
            deadline = threading.Event()
            for _ in range(50):
                if server.commands.count(b"DONE\r\n") >= 2:
                    break
                deadline.wait(.1)
            watcher.stop()
            watcher.join(3)
            self.assertFalse(watcher.is_alive())
            self.assertGreaterEqual(server.commands.count(b"DONE\r\n"), 2)
            self.assertEqual(server.status_calls, 1)  # после отказа STATUS больше не спрашиваем
            self.assertEqual(changes, [1])

    def test_stop_interrupts_active_connection(self):
        with Server("quiet") as server, mock.patch.object(idle, "get_password", return_value="dummy"):
            watcher = self.watcher(server, mock.Mock(), mock.Mock())
            watcher.start()
            self.assertTrue(server.entered.wait(2))
            watcher.stop()
            watcher.join(2)
            self.assertFalse(watcher.is_alive())


if __name__ == "__main__":
    unittest.main()
