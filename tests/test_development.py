"""Предложения по развитию: Bcc, остановка проверки, журнал ошибок, автоопределение,
трей, поиск FTS5, CONDSTORE."""
import os
import tempfile
import threading
import unittest
from email import message_from_bytes, policy
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mailclient import autoconfig, protocols, sync
from mailclient.db import Account, Database, Message, fts_query
from mailclient.errors import ErrorInfo
from test_core import make_raw
from test_flagged import FlagIMAP


class TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.addCleanup(self.db.close)
        self.aid = self.db.save_account(Account(name="T", email="me@example.com", username="me",
                                                imap_host="i", smtp_host="s"))
        self.acc = self.db.account(self.aid)


class TestBcc(TempDb):
    def test_bcc_header_is_kept_for_sent_copy_and_stripped_by_smtplib(self):
        em = protocols.build_message(self.acc, "to@x.test", "S", "body", bcc="hidden@x.test")
        self.assertEqual(em["Bcc"], "hidden@x.test")
        sent = {}

        import smtplib

        class SMTP(smtplib.SMTP):  # без подключения: проверяем, что уходит на сервер
            def ehlo_or_helo_if_needed(self):
                pass

            def has_extn(self, name):
                return False

            def sendmail(self, from_addr, to_addrs, msg, *args, **kwargs):
                sent["rcpts"], sent["data"] = to_addrs, msg

        SMTP().send_message(message_from_bytes(em.as_bytes(), policy=policy.default))
        self.assertEqual(sent["rcpts"], ["to@x.test", "hidden@x.test"])
        self.assertNotIn(b"hidden@x.test", sent["data"] if isinstance(sent["data"], bytes) else sent["data"].encode())


class TestHistoryPause(TempDb):
    def test_pausing_history_keeps_normal_sync_and_does_not_mark_complete(self):
        messages = {uid: make_raw(f"s{uid}@x.ru", f"M{uid}", "b") for uid in range(1, 31)}
        fake = FlagIMAP(messages, {})
        self.db.set_setting("initial_fetch_limit", "5")
        history = threading.Event()
        history.set()  # «Приостановить загрузку истории» нажали сразу
        with mock.patch.object(protocols, "imap_connect", return_value=fake), \
             mock.patch.object(protocols, "get_password", return_value="x"):
            n = protocols.sync_account(self.db, self.acc, full_history=True, history_cancel=history)
        self.assertEqual(n, 5)  # как обычная проверка: последние письма скачаны
        self.assertNotEqual(self.db.get_setting(f"history:{self.aid}"), "complete")

    def test_stop_cancels_whole_check(self):
        fake = FlagIMAP({1: make_raw("a@x.ru", "A", "b")}, {})
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(protocols, "imap_connect", return_value=fake), \
             mock.patch.object(protocols, "get_password", return_value="x"):
            self.assertEqual(protocols.sync_account(self.db, self.acc, cancel=cancel), 0)
        self.assertEqual(self.db.conn().execute("SELECT count(*) FROM messages").fetchone()[0], 0)


class CondstoreIMAP(FlagIMAP):
    capabilities = ("IMAP4REV1", "CONDSTORE", "ENABLE")

    def __init__(self, messages, flags):
        super().__init__(messages, flags)
        self.modseq = 10
        self.commands = []

    def enable(self, what):
        self.commands.append(("ENABLE", what))
        return "OK", [b""]

    def select(self, name, readonly=False):
        self.commands.append(("SELECT", name))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        values = {"UIDVALIDITY": 42, "HIGHESTMODSEQ": self.modseq, "UIDNEXT": max(self.messages, default=0) + 1}
        return code, [str(values.get(code, 42)).encode()]

    def uid(self, cmd, *args):
        self.commands.append((cmd,) + args)
        return super().uid(cmd, *args)


class TestCondstore(TempDb):
    def sync(self, fake, **kw):
        with mock.patch.object(protocols, "imap_connect", return_value=fake), \
             mock.patch.object(protocols, "get_password", return_value="x"):
            return protocols.sync_account(self.db, self.acc, **kw)

    def test_unchanged_folder_is_skipped_and_changes_use_changedsince(self):
        fake = CondstoreIMAP({1: make_raw("a@x.ru", "A", "1"), 2: make_raw("b@x.ru", "B", "2")}, {})
        self.assertEqual(self.sync(fake), 2)
        fake.commands.clear()
        self.assertEqual(self.sync(fake), 0)
        self.assertFalse([c for c in fake.commands if c[0] in ("SEARCH", "FETCH")])  # только EXAMINE
        fake.modseq = 11
        fake.flags = {1: {"\\Seen"}}
        fake.commands.clear()
        self.sync(fake)
        fetch = [c for c in fake.commands if c[0] == "FETCH"]
        self.assertEqual(fetch, [("FETCH", "1,2", "(UID FLAGS) (CHANGEDSINCE 10)")])
        seen = self.db.conn().execute("SELECT seen FROM messages WHERE uid=1").fetchone()[0]
        self.assertEqual(seen, 1)

    def test_local_pending_flag_prevents_skip(self):
        fake = CondstoreIMAP({1: make_raw("a@x.ru", "A", "1")}, {})
        self.sync(fake)
        mid = self.db.conn().execute("SELECT id FROM messages").fetchone()[0]
        self.db.update_message(mid, flagged=1, pending_flagged=1)
        self.sync(fake)
        self.assertIn((1, "+FLAGS.SILENT", "\\Flagged"), fake.stores)

    def test_new_message_detected_by_uidnext(self):
        fake = CondstoreIMAP({1: make_raw("a@x.ru", "A", "1")}, {})
        self.sync(fake)
        fake.messages[2] = make_raw("b@x.ru", "B", "2")
        self.assertEqual(self.sync(fake), 1)


class TestSearchIndex(TempDb):
    def add(self, subject, body, sender="x@firma.rs"):
        return self.db.add_message(Message(account_id=self.aid, subject=subject, sender=sender,
                                           body_text=body, date_ts=1700000000, seen=True))

    def found(self, query):
        return {r["id"] for r in self.db.search_messages(query)}

    def test_fts_query(self):
        self.assertEqual(fts_query('Иван "счёт"'), '"иван"* "счёт"*')
        self.assertEqual(fts_query("..."), "")

    def test_index_kept_up_to_date(self):
        if not self.db.fts:
            self.skipTest("SQLite без FTS5")
        first = self.add("Договор", "Оплата по счёту Иванову")
        self.assertEqual(self.found("иванов"), {first})
        second = self.add("Акт", "Сверка за квартал")
        self.assertEqual(self.found("иван"), {first})        # начало слова
        self.assertEqual(self.found("квартал сверка"), {second})  # все слова, в любом порядке
        self.assertEqual(self.found("irma.r"), {first, second})   # адрес — по любой части
        self.db.update_message(second, body_text="Новый текст")
        self.assertEqual(self.found("сверка"), set())
        self.assertEqual(self.found("новый"), {second})
        self.db.delete_message(first)
        self.assertEqual(self.found("иванову"), set())

    def test_index_is_consistent(self):
        if not self.db.fts:
            self.skipTest("SQLite без FTS5")
        ids = [self.add(f"T{i}", f"слово{i}") for i in range(5)]
        self.db.delete_message(ids[0])
        self.db.update_message(ids[1], body_text="другое")
        self.assertEqual(self.found("слово3"), {ids[3]})
        self.assertEqual(self.found("слово1"), set())
        # Индекс согласован: integrity-check FTS5 не находит расхождений.
        self.db.conn().execute("INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)")

class TestAutoConfig(unittest.TestCase):
    def test_prefers_most_secure_variant_and_checks_password_once(self):
        working = {("mail.firma.rs", 993, "ssl"), ("imap.firma.rs", 143, "none"),
                   ("smtp.firma.rs", 587, "starttls"), ("smtp.firma.rs", 25, "none")}
        logins = []

        def probe(host, port, security, timeout):
            return (host, port, security) in working

        def connect_imap(acc, password):
            logins.append(("imap", acc.imap_host, acc.imap_port, acc.imap_security))
            return mock.Mock()

        def connect_smtp(acc, password):
            logins.append(("smtp", acc.smtp_host, acc.smtp_port, acc.smtp_security))
            raise protocols.smtplib.SMTPAuthenticationError(535, b"bad")

        result = autoconfig.detect(Account(email="me@firma.rs"), "pw", probe_imap=probe, probe_smtp=probe,
                                   connect_imap=connect_imap, connect_smtp=connect_smtp)
        self.assertEqual((result.imap.host, result.imap.port, result.imap.security), ("mail.firma.rs", 993, "ssl"))
        self.assertEqual((result.smtp.host, result.smtp.port, result.smtp.security), ("smtp.firma.rs", 587, "starttls"))
        self.assertEqual(logins, [("imap", "mail.firma.rs", 993, "ssl"), ("smtp", "smtp.firma.rs", 587, "starttls")])
        self.assertIsNone(result.imap_login)
        self.assertIsNotNone(result.smtp_login)

    def test_nothing_found_and_plaintext_warning(self):
        result = autoconfig.detect(Account(email="me@firma.rs"), "",
                                   probe_imap=lambda h, p, s, timeout: (p, s) == (143, "none"),
                                   probe_smtp=lambda *a, **k: False)
        self.assertEqual(result.imap.security, "none")
        self.assertIsNone(result.smtp)
        self.assertFalse(result.login_checked)
        self.assertEqual(len(result.notes), 2)

    def test_candidate_hosts(self):
        self.assertEqual(autoconfig.candidate_hosts("a@Firma.rs", "imap", "my.host"),
                         ["my.host", "imap.firma.rs", "mail.firma.rs", "firma.rs"])
    def test_auto_detection_never_sends_password_over_plaintext_connection(self):
        imap, smtp = mock.Mock(), mock.Mock()
        result = autoconfig.detect(Account(email="me@firma.rs"), "private-password",
                                   probe_imap=lambda host, port, security, **kw: security == "none",
                                   probe_smtp=lambda host, port, security, **kw: security == "none",
                                   connect_imap=imap, connect_smtp=smtp)
        self.assertEqual(result.imap.security, "none")
        self.assertEqual(result.smtp.security, "none")
        imap.assert_not_called()
        smtp.assert_not_called()
        self.assertTrue(any("IMAP" in note for note in result.notes))
        self.assertTrue(any("SMTP" in note for note in result.notes))


class QtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])


class TestTrayAndDialogs(QtCase):
    def test_badge(self):
        from mailclient.ui.tray import badge_icon, badge_text
        from mailclient.ui.icons import app_icon
        self.assertEqual([badge_text(n) for n in (0, 7, 100)], ["", "7", "99+"])
        self.assertFalse(badge_icon(app_icon(), 5).isNull())

    def test_badge_is_yellow_envelope_not_red(self):
        from PySide6.QtGui import QColor
        from mailclient.ui.tray import BADGE_COLOR, badge_icon
        from mailclient.ui.icons import app_icon
        image = badge_icon(app_icon(), 5).pixmap(64, 64).toImage()
        colors = {image.pixelColor(x, y).name() for x in range(64) for y in range(64)}
        self.assertIn(QColor(BADGE_COLOR).name(), colors)
        self.assertNotIn("#dc2626", colors)

    def test_copy_details_button_does_not_close_box(self):
        from PySide6.QtWidgets import QApplication, QMessageBox
        self.addCleanup(QApplication.clipboard().clear)
        from mailclient.ui.messages import add_copy_button, error_report
        box = QMessageBox()
        text = error_report("Заголовок", ErrorInfo("Что случилось", "Что проверить", "Traceback"))
        button = add_copy_button(box, text)
        box.show()
        button.click()
        self.assertTrue(box.isVisible())
        self.assertEqual(QApplication.clipboard().text(), text)
        self.assertIn("Traceback", text)
        box.close()

    def test_error_log_dialog(self):
        from mailclient.ui.error_log import ErrorLog, ErrorLogDialog
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "simplemail.log"
            path.write_text("строка журнала\n", encoding="utf-8")
            log = ErrorLog()
            dialog = ErrorLogDialog(log, path)
            self.assertIn("строка журнала", dialog.details.toPlainText())
            log.add("Пометка прочитанным", ErrorInfo("Нет связи", "", "details"))
            self.assertEqual(log.unseen, 1)
            dialog = ErrorLogDialog(log, path)
            self.assertEqual(log.unseen, 0)
            self.assertIn("Нет связи", dialog.details.toPlainText())

    def test_compose_bcc_field_saved_in_draft(self):
        from mailclient.ui.compose_dialog import ComposeDialog
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "mail.db")
            try:
                db.save_account(Account(name="T", email="me@example.com", storage_dir=tmp))
                dialog = ComposeDialog(db, to="to@x.test", subject="S")
                self.assertTrue(dialog.bcc.isHidden())
                dialog.bcc_toggle.setChecked(True)
                dialog.bcc.setText("hidden@x.test")
                dialog._save_draft()
                draft = dialog._draft_id
                self.assertIn(b"Bcc: hidden@x.test", db.message(draft).raw)
                dialog._dirty = False
                dialog.close()
                reopened = ComposeDialog(db, draft_id=draft)
                self.assertEqual(reopened.bcc.text(), "hidden@x.test")
                self.assertFalse(reopened.bcc.isHidden())
                reopened._dirty = False
                reopened.close()
            finally:
                db.close()


class TestMainWindowFeatures(QtCase):
    def setUp(self):
        from mailclient.ui import main_window, viewer
        self.mw = main_window
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.addCleanup(self.db.close)
        aid = self.db.save_account(Account(name="A", email="a@example.com", storage_dir=self.tmp.name))
        for i in range(2):  # при запуске письма сами не открываются и остаются непрочитанными
            self.db.add_message(Message(account_id=aid, subject=f"Unread {i}", sender="s@example.com",
                                        date_ts=1700000000 + i, seen=False))
        for patch in (mock.patch.object(main_window, "run_task"),
                      mock.patch("mailclient.ui.mail_watch.MailWatcher"),
                      mock.patch.object(viewer, "HAS_WEBENGINE", False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.window = main_window.MainWindow(self.db)
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self.close)
        for call in self.sync_calls():  # проверка при запуске
            call.kwargs["on_done"](0)

    def close(self):
        self.window._quitting = True
        self.window.close()
        self.app.processEvents()

    def sync_calls(self):
        return [c for c in self.mw.run_task.call_args_list if c.args and c.args[0] is protocols.sync_account]

    def test_stop_button_cancels_only_this_check(self):
        self.mw.run_task.reset_mock()
        self.window.check_mail(manual=True)
        call = self.sync_calls()[-1]
        self.assertFalse(self.window.sync_bar.isHidden())
        self.assertFalse(self.window.sync_stop.isHidden())
        self.assertFalse(self.window.acts["check"].isEnabled())
        self.window._pause_history()
        self.assertTrue(call.kwargs["history_cancel"].is_set())
        self.assertFalse(call.kwargs["cancel"].is_set())  # пауза истории не останавливает проверку
        self.window.sync_stop.click()
        self.assertTrue(call.kwargs["cancel"].is_set())
        call.kwargs["on_done"](0)
        self.assertTrue(self.window.sync_bar.isHidden())
        self.assertTrue(self.window.acts["check"].isEnabled())
        self.assertIn("остановлена", self.window.status.currentMessage())

    def test_idle_checks_only_inbox(self):
        self.mw.run_task.reset_mock()
        aid = self.db.accounts()[0].id
        self.window._idle_changed(aid)
        self.window._check_pending_mail()
        self.assertTrue(self.sync_calls()[-1].kwargs["inbox_only"])
        self.sync_calls()[-1].kwargs["on_done"](0)
        self.mw.run_task.reset_mock()
        self.window.check_mail(manual=True)
        self.assertFalse(self.sync_calls()[-1].kwargs["inbox_only"])

    def test_background_error_indicator(self):
        self.assertTrue(self.window.error_btn.isHidden())
        self.window._status_error(ErrorInfo("Нет связи с сервером", "", "details"))
        self.assertFalse(self.window.error_btn.isHidden())
        self.assertIn("1", self.window.error_btn.text())
        with mock.patch.object(self.mw.ErrorLogDialog, "exec"):
            self.window.error_btn.click()
        self.assertTrue(self.window.error_btn.isHidden())

    def test_tray_unread_and_close_to_tray(self):
        # Оба письма непрочитаны: при запуске первое письмо само не открывается и не читается.
        self.assertEqual(self.window.tray.count, 2)
        self.window.tray.icon = mock.Mock()  # в offscreen области уведомлений нет
        self.window.acts["close_to_tray"].setChecked(True)
        closed = []
        self.window.closed.connect(lambda: closed.append(True))
        self.window.close()
        self.assertTrue(self.window.isHidden())
        self.assertEqual(closed, [])  # программа продолжает работать
        self.window.bring_to_front()
        self.assertTrue(self.window.isVisible())
        self.window._quit()
        self.assertEqual(closed, [True])

    def test_new_mail_notification_only_when_window_inactive(self):
        self.window.tray.notify = mock.Mock()
        with mock.patch.object(self.window, "isActiveWindow", return_value=True):
            self.window._notify_new_mail(1)
        self.window.tray.notify.assert_not_called()
        with mock.patch.object(self.window, "isActiveWindow", return_value=False):
            self.window._notify_new_mail(2)
        title, text = self.window.tray.notify.call_args.args
        self.assertIn("2", title)
        self.assertIn("Unread", text)


if __name__ == "__main__":
    unittest.main()
