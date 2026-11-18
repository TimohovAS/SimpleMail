"""Регрессионные тесты по итогам аудита версии 0.25.1 (исправления в 0.26.0)."""
from __future__ import annotations

import os
import tempfile
import unittest
from email.header import Header
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QTableWidgetItem

from mailclient import delivery, errors, passwords, protocols, storage, sync, updates
from mailclient.contacts import clean_name, parse_addresses
from mailclient.db import Account, Database, Message, fts_query
from mailclient.mailparse import attachments, decode_mime_header, parse_message
from mailclient.ui.account_dialog import AccountsDialog
from mailclient.ui.compose_dialog import ComposeDialog
from mailclient.ui.header import human_date
from mailclient.ui.message_table import MessageTable
from mailclient.ui.viewer import HAS_WEBENGINE, MessageView, _MailHTML, scope_css

app = QApplication.instance() or QApplication([])


class TestViewerSecurityAndCrashLoop(unittest.TestCase):
    def test_scope_css_neutralizes_style_breakout(self):
        css = 'body { color: red; content: "</sty/**/le>"; } </sty/**/le><img src=https://evil.example/x>'
        scoped = scope_css(css, "#m1")
        self.assertNotIn("</style", scoped.lower())
        self.assertIn(r"<\/style", scoped.lower())

    def test_local_only_blocks_poster_media_and_obfuscated_css_urls(self):
        raw_html = (
            '<div style="background: \\75 rl(https://tracker.example/1.png)"></div>'
            '<div style="list-style: u/*c*/rl(https://tracker.example/2.png)"></div>'
            '<style>@\\69 mport "https://tracker.example/s.css";</style>'
            '<video poster="https://tracker.example/poster.jpg" src="https://tracker.example/v.mp4">'
            '<source src="https://tracker.example/s.mp4">'
            '<track src="https://tracker.example/t.vtt">'
            '</video>'
            '<audio src="https://tracker.example/a.mp3"></audio>'
        )
        parser = _MailHTML(local_images_only=True, fragment=True)
        parser.feed(raw_html)
        out = parser.finish()
        styles = "\n".join(parser.styles)
        self.assertTrue(parser.remote)
        self.assertNotIn("tracker.example", out)
        self.assertNotIn("tracker.example", styles)
        self.assertNotIn("<video", out.lower())
        self.assertNotIn("<audio", out.lower())
        self.assertNotIn("<source", out.lower())
        self.assertNotIn("<track", out.lower())

    def test_render_failure_does_not_loop_on_next_termination(self):
        view = MessageView.__new__(MessageView)
        view._shutdown = False
        view.web = object()
        view._loading = True
        view._render_restarts = [99.0, 99.5, 99.8]
        view._render_failed = False
        view._page_html = ""
        view._allow_remote = False
        view._thread = object()
        view._msg = Message(id=42)
        view._render = mock.Mock()

        with mock.patch("mailclient.ui.viewer.time.monotonic", return_value=100.0):
            with mock.patch("mailclient.ui.viewer.QTimer.singleShot") as shots:
                view._render_process_terminated("CrashedTerminationStatus", 1)
                self.assertEqual(len(shots.call_args_list), 1)
                self.assertEqual(shots.call_args.args[1], view._show_render_failure)

                view._show_render_failure()
                self.assertTrue(view._render_failed)

                shots.reset_mock()
                view._render_process_terminated("CrashedTerminationStatus", 1)
                shots.assert_not_called()

        view._page_html = "<html><body>page</body></html>"
        view._render.reset_mock()
        view._reload_page()
        view._render.assert_called_once_with("<html><body>page</body></html>", False, "m42")

    @unittest.skipUnless(HAS_WEBENGINE, "Qt WebEngine is unavailable")
    def test_scheme_handler_serves_images_and_rejects_non_images(self):
        """cid: отдаёт картинки с любым image/* (image/jpg, SVG) и картинку под octet-stream,
        но не HTML и не прочие части письма."""
        from PySide6.QtCore import QBuffer, QIODevice, QUrl
        from PySide6.QtGui import QImage
        from mailclient.ui.viewer import _SchemeHandler

        image = QImage(2, 2, QImage.Format_RGB32)
        image.fill(0x336699)
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, "PNG")
        png = bytes(buffer.data())

        class FakeView:
            _inline = {
                "svg": ("image/svg+xml", b"<svg/>"),
                "jpg": ("image/jpg", b"\xff\xd8\xff"),
                "octet": ("application/octet-stream", png),
                "html": ("text/html", b"<html></html>"),
                "octet-text": ("application/octet-stream", b"just text"),
            }
            _page_html = ""

        class FakeError:
            UrlNotFound = 1

        handler = _SchemeHandler.__new__(_SchemeHandler)
        handler.view = FakeView()
        with mock.patch("mailclient.ui.viewer.QBuffer"), mock.patch("mailclient.ui.viewer.QByteArray"):
            for cid, mime in (("svg", b"image/svg+xml"), ("jpg", b"image/jpg"), ("octet", b"image/png")):
                job = mock.Mock()
                job.Error = FakeError
                job.requestUrl.return_value = QUrl(f"cid:{cid}")
                handler.requestStarted(job)
                job.fail.assert_not_called()
                self.assertEqual(job.reply.call_args[0][0], mime)
            for cid in ("html", "octet-text", "missing"):
                job = mock.Mock()
                job.Error = FakeError
                job.requestUrl.return_value = QUrl(f"cid:{cid}")
                handler.requestStarted(job)
                job.fail.assert_called_once_with(FakeError.UrlNotFound)
                job.reply.assert_not_called()


class TestMailParseAndDates(unittest.TestCase):
    def test_koi8r_plain_body_without_declared_charset(self):
        body = "Здравствуйте, отправляем договор на подпись."
        raw = b"From: a@example.com\r\nSubject: Test\r\nContent-Type: text/plain\r\n\r\n" + body.encode("koi8-r")
        parsed = parse_message(raw)
        self.assertEqual(parsed.body_text, body)

    def test_rfc2047_names_with_commas_stay_quoted(self):
        from_h = f"{Header('Иванов, Иван', 'utf-8').encode()} <ivan@example.com>"
        to_h = f"{Header('Петрова, Анна', 'utf-8').encode()} <anna@example.com>, simple@example.com"
        raw = (f"From: {from_h}\r\nTo: {to_h}\r\nSubject: Test\r\n\r\nhi").encode("ascii")
        parsed = parse_message(raw)
        self.assertEqual(parseaddr(parsed.sender), ("Иванов, Иван", "ivan@example.com"))
        self.assertEqual(
            parse_addresses(parsed.recipients),
            [("Петрова, Анна", "anna@example.com"), ("", "simple@example.com")],
        )

    def test_unquoted_names_with_commas_keep_surname(self):
        """8-битное или ASCII имя с запятой без кавычек: фамилия не теряется, адрес на месте."""
        raw = ("From: Иванов, Иван <ivan@example.com>\r\n"
               "To: Smith, John <js@example.com>, plain@example.com\r\nSubject: x\r\n\r\nhi").encode("utf-8")
        parsed = parse_message(raw)
        self.assertEqual(parseaddr(parsed.sender), ("Иванов, Иван", "ivan@example.com"))
        self.assertEqual(parse_addresses(parsed.recipients),
                         [("Smith, John", "js@example.com"), ("", "plain@example.com")])

    def test_decode_mime_header_folds_newlines(self):
        self.assertEqual(decode_mime_header("Первая\r\n\t вторая строка"), "Первая вторая строка")

    def test_clean_name_preserves_inner_quotes(self):
        self.assertEqual(clean_name('ООО "Ромашка"', "info@romashka.ru"), 'ООО "Ромашка"')
        self.assertEqual(clean_name('"Петров, Пётр"', "p@example.com"), "Петров, Пётр")

    def test_out_of_range_dates_do_not_crash_on_windows(self):
        raw = b"From: a@example.com\r\nDate: Wed, 01 Jan 1969 00:00:00 +0000\r\nSubject: Old\r\n\r\nbody"
        parsed = parse_message(raw)
        self.assertEqual(parsed.date_ts, 0)
        self.assertEqual(human_date(-1000), "")
        with tempfile.TemporaryDirectory() as tmp:
            p = storage.eml_path(tmp, "inbox", -1000, 1, raw)
            self.assertTrue(p.name.endswith(".eml"))

    def test_rfc822_without_attachment_marks_stays_visible_in_body(self):
        """Пересланное письмо без пометки «вложение» и без имени файла вложением не считается,
        поэтому его текст должен остаться в теле, иначе его не видно вообще."""
        inner = (b"From: x@example.com\r\nSubject: inner\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
                 b"Inner text body\r\n")
        for disposition in (b"", b"Content-Disposition: inline\r\n"):
            raw = (b"From: a@example.com\r\nSubject: fwd\r\nMIME-Version: 1.0\r\n"
                   b"Content-Type: multipart/mixed; boundary=XX\r\n\r\n--XX\r\nContent-Type: text/plain\r\n\r\n"
                   b"Outer\r\n--XX\r\nContent-Type: message/rfc822\r\n" + disposition + b"\r\n" + inner +
                   b"\r\n--XX--\r\n")
            parsed = parse_message(raw)
            self.assertIn("Outer", parsed.body_text)
            self.assertIn("Inner text body", parsed.body_text)
            self.assertEqual([name for name, _ in attachments(raw)], [])

    def test_inline_pdf_with_cid_stays_attachment_and_rfc822_body_excluded(self):
        em = EmailMessage()
        em["From"] = "a@example.com"
        em["Subject"] = "Outer"
        em.set_content("Outer text")
        em.add_alternative("<p>Outer html</p>", subtype="html")
        em.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="scan.pdf")
        pdf_part = em.get_payload()[-1]
        pdf_part.replace_header("Content-Disposition", 'inline; filename="scan.pdf"')
        pdf_part["Content-ID"] = "<pdf1>"

        inner = EmailMessage()
        inner["From"] = "inner@example.com"
        inner.set_content("Secret inner body")
        em.add_attachment(inner, filename="forwarded.eml")

        parsed = parse_message(em.as_bytes())
        self.assertIn("Outer text", parsed.body_text)
        self.assertNotIn("Secret inner body", parsed.body_text)
        self.assertEqual([name for name, _ in attachments(em.as_bytes())], ["scan.pdf", "forwarded.eml"])


class TestBackendFixes(unittest.TestCase):
    def test_fts_query_handles_underscores(self):
        self.assertEqual(fts_query("___"), "")
        self.assertEqual(fts_query("foo_bar"), '"foo"* "bar"*')
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "m.db")
            try:
                aid = db.save_account(Account(name="A", email="a@x.rs", username="a", imap_host="h", smtp_host="h"))
                mid = db.add_message(Message(account_id=aid, subject="Report_2026", sender="s@x.rs", body_text="invoice_42"))
                self.assertEqual([r["id"] for r in db.search_messages("invoice_42")], [mid])
            finally:
                db.close()

    def test_delete_account_cleans_recent_floor_and_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "m.db")
            try:
                a1 = db.save_account(Account(name="A1", email="a1@x.rs", username="a1", imap_host="h", smtp_host="h"))
                a2 = db.save_account(Account(name="A2", email="a2@x.rs", username="a2", imap_host="h", smtp_host="h"))
                db.set_account_order([a2, a1])
                db.set_setting(f"body_recent_floor:{a1}:INBOX", "1:10")
                db.delete_account(a1)
                self.assertIsNone(db.get_setting(f"body_recent_floor:{a1}:INBOX"))
                self.assertEqual(db.get_setting("account_order"), f"[{a2}]")
            finally:
                db.close()

    def test_same_content_handles_broken_headers(self):
        self.assertFalse(delivery._same_content(b"From: bad\x80\r\n\r\nhi", b"From: a@b\r\n\r\nhi"))

    def test_mail_value_error_preserves_explanation(self):
        with self.assertRaises(ValueError) as ctx:
            protocols.imap_quote("bad\r\nfolder")
        self.assertIsInstance(ctx.exception, errors.MailValueError)
        self.assertIn("Недопустимое имя", errors.explain(ctx.exception).summary)

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "m.db")
            try:
                aid = db.save_account(Account(name="A", email="a@x.rs", username="a", imap_host="h", smtp_host="h"))
                stale = Account(id=aid, name="A", email="other@x.rs", username="a", imap_host="h", smtp_host="h")
                with self.assertRaises(ValueError) as pctx:
                    passwords.get_password(db, aid, account=stale)
                self.assertIn("Настройки ящика изменились", errors.explain(pctx.exception).summary)
            finally:
                db.close()

    def test_is_bundled_path_handles_quotes_and_invalid_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            inside = root / "sub"
            inside.mkdir()
            self.assertTrue(updates._is_bundled_path(f'"{inside}"', root))
            self.assertFalse(updates._is_bundled_path("Z:\\nonexistent\x00path", root))
            self.assertTrue(updates._is_bundled_path("", root))

    def test_headers_only_watermark_advances_past_already_known_uids(self):
        class PartialIMAP:
            def __init__(self, boxes):
                self.boxes = boxes
                self.capabilities = ()

            def select(self, mailbox, readonly=False):
                return "OK", [b"5"]

            def response(self, name):
                return "UIDVALIDITY", [b"1"]

            def uid(self, command, *args):
                if command == "SEARCH":
                    return "OK", [b" ".join(str(u).encode() for u in sorted(self.boxes["INBOX"]))]
                if command == "FETCH" and "(UID FLAGS)" in args[1]:
                    return "OK", []
                if command == "FETCH":
                    want = [int(x) for x in args[0].split(",")]
                    out = []
                    for u in want:
                        raw = self.boxes["INBOX"][u]
                        out.append((f"1 (UID {u} RFC822.SIZE {len(raw)} BODY[HEADER] {{{len(raw)}}}".encode(), raw))
                    return "OK", out
                return "OK", []

        def raw_for(uid):
            return f"From: s@x.rs\r\nSubject: M{uid}\r\nMessage-ID: <{uid}@x.rs>\r\n\r\nbody".encode()

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "m.db")
            try:
                aid = db.save_account(Account(name="A", email="a@x.rs", username="a", imap_host="h", smtp_host="h"))
                acc = db.account(aid)
                db.set_folder_state(aid, "INBOX", 1, 1)
                db.add_message(Message(
                    account_id=aid, folder="inbox", server_folder="INBOX",
                    uid=1, uidvalidity=1, message_id="<1@x.rs>", subject="M1",
                    sender="s@x.rs", body_downloaded=0,
                ))
                db.add_message(Message(
                    account_id=aid, folder="inbox", server_folder="INBOX",
                    uid=3, uidvalidity=1, message_id="<3@x.rs>", subject="M3",
                    sender="s@x.rs", body_downloaded=0,
                ))
                im = PartialIMAP({
                    "INBOX": {1: raw_for(1), 2: raw_for(2), 3: raw_for(3)}
                })
                folder = sync.ServerFolder("INBOX", "inbox", "INBOX", set())
                sync.sync_mailbox(db, acc, im, folder, lambda _: None, headers_only=True)
                self.assertEqual(db.folder_state(aid, "INBOX"), (1, 3))

                # Первая синхронизация с окном recent_limit: старые письма (< recent[0]) не мешают сдвинуть last_uid.
                db.set_setting("initial_fetch_limit", "2")
                db.set_folder_state(aid, "Other", 1, 0)
                db.add_message(Message(
                    account_id=aid, folder="inbox", server_folder="Other",
                    uid=14, uidvalidity=1, message_id="<14@x.rs>", subject="M14",
                    sender="s@x.rs", body_downloaded=0,
                ))
                im.boxes["Other"] = {u: raw_for(u) for u in (10, 11, 12, 13, 14)}
                other = sync.ServerFolder("Other", "inbox", "Other", set())
                original_select = im.select

                def uid_for_box(command, *args):
                    if command == "SEARCH":
                        return "OK", [b" ".join(str(u).encode() for u in sorted(im.boxes[current_box]))]
                    if command == "FETCH" and "(UID FLAGS)" in args[1]:
                        return "OK", []
                    if command == "FETCH":
                        want = [int(x) for x in args[0].split(",")]
                        return "OK", [
                            (f"1 (UID {u} RFC822.SIZE {len(im.boxes[current_box][u])} BODY[HEADER] {{{len(im.boxes[current_box][u])}}}".encode(), im.boxes[current_box][u])
                            for u in want
                        ]
                    return "OK", []

                current_box = "INBOX"
                def select_box(mailbox, readonly=False):
                    nonlocal current_box
                    current_box = mailbox.decode().strip('"')
                    return original_select(mailbox, readonly)

                im.select = select_box
                im.uid = uid_for_box
                sync.sync_mailbox(db, acc, im, other, lambda _: None, headers_only=True)
                self.assertEqual(db.folder_state(aid, "Other"), (1, 14))
            finally:
                db.close()


class TestUIRegressions(unittest.TestCase):
    def test_count_selected_works_with_hidden_first_column(self):
        table = MessageTable(3, 3, check_column=1)
        for row in range(3):
            for col in range(3):
                table.setItem(row, col, QTableWidgetItem(f"{row},{col}"))
        table.setSelectionBehavior(MessageTable.SelectRows)
        table.setColumnHidden(0, True)
        table.selectRow(0)
        table.setCurrentCell(2, 1, QItemSelectionModel.Select | QItemSelectionModel.Rows)
        self.assertEqual(table._selected_rows, 2)
        table.close()

    def test_compose_plain_draft_and_empty_without_accounts_close_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "m.db")
            try:
                empty_dlg = ComposeDialog(db)
                empty_dlg._dirty = True
                empty_dlg.reject()
                self.assertFalse(empty_dlg._dirty)
                empty_dlg.close()

                aid = db.save_account(Account(name="A", email="a@x.rs", username="a", imap_host="h", smtp_host="h"))
                draft_id = db.add_message(Message(
                    account_id=aid, folder="drafts", subject="Plain draft",
                    body_text="Line 1\nLine 2", body_html="", message_id="<d@x.rs>",
                ))
                with mock.patch.object(QMessageBox, "question", side_effect=AssertionError("unexpected modal prompt")):
                    plain_dlg = ComposeDialog(db, draft_id=draft_id)
                self.assertEqual(plain_dlg.mode.currentData(), "plain")
                self.assertFalse(plain_dlg.editor.edit.acceptRichText())
                self.assertEqual(plain_dlg.editor.to_plain(), "Line 1\nLine 2")
                plain_dlg.close()
            finally:
                db.close()

    def test_accounts_dialog_blocks_reject_while_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "m.db")
            try:
                dlg = AccountsDialog(db)
                dlg._busy = True
                dlg.reject()
                self.assertFalse(dlg._closed)
                dlg._busy = False
                dlg.reject()
                self.assertTrue(dlg._closed)
                dlg.close()
            finally:
                db.close()


class TestImprovements_0_26(unittest.TestCase):
    def tearDown(self):
        protocols.close_pool()

    def test_pool_noop_reconnects_on_stale_connection(self):
        from mailclient import mailops  # noqa: F401
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "m.db")
            try:
                acc = db.account(db.save_account(Account(
                    name="T", email="me@x.rs", username="me", imap_host="i", smtp_host="s")))
                im1 = mock.Mock()
                im1.noop.return_value = ("OK", [b"Still here"])
                im2 = mock.Mock()
                im2.noop.return_value = ("OK", [b"OK"])
                connect = mock.Mock(side_effect=[im1, im2])
                with mock.patch.object(protocols, "get_password", return_value="pw"):
                    with protocols.account_imap(db, acc, connect) as first:
                        self.assertIs(first, im1)
                    with protocols.account_imap(db, acc, connect) as second:
                        self.assertIs(second, im1)
                    self.assertEqual(connect.call_count, 1)

                    im1.noop.side_effect = OSError("connection reset")
                    with protocols.account_imap(db, acc, connect) as third:
                        self.assertIs(third, im2)
                    self.assertEqual(connect.call_count, 2)
            finally:
                db.close()

    def test_mailops_and_flags_skip_raw_disk_read_for_uidvalidity_messages(self):
        from mailclient import mailops
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "m.db")
            try:
                store = Path(tmp) / "store"
                aid = db.save_account(Account(
                    name="A", email="me@x.rs", username="me", imap_host="i", smtp_host="s", storage_dir=str(store)))
                raw = b"From: s@x.rs\r\nTo: me@x.rs\r\nSubject: Subj\r\nMessage-ID: <1@x.rs>\r\n\r\nhi"
                m = parse_message(raw)
                m.account_id, m.folder, m.server_folder = aid, "inbox", "INBOX"
                m.uid, m.uidvalidity = 10, 42
                mid = db.add_message(m)
                saved = db.message(mid, with_raw=False)
                self.assertTrue(saved.raw_path)

                im = mock.Mock()
                im.capability.return_value = ("OK", [b"IMAP4rev1 MOVE UIDPLUS"])
                im.select.return_value = ("OK", [b"1"])
                im.response.return_value = ("UIDVALIDITY", [b"42"])
                im.uid.return_value = ("OK", [b"1 (UID 10)"])

                with mock.patch.object(storage, "read_eml", side_effect=AssertionError("unexpected disk read")), \
                     mock.patch.object(protocols, "get_password", return_value="pw"), \
                     mock.patch.object(mailops, "get_password", return_value="pw"), \
                     mock.patch.object(protocols, "imap_connect", return_value=im), \
                     mock.patch.object(mailops, "imap_connect", return_value=im):
                    protocols.set_server_flag(db, saved, "\\Seen", True)
                    db.update_message(mid, pending_marks=1)
                    protocols.push_marks(db, [mid])
                    db.update_message(mid, folder="trash", trash_origin_folder="inbox", trash_origin_server_folder="INBOX")
                    mailops.delete_permanently(db, mid)
                self.assertIsNone(db.message(mid))
            finally:
                db.close()

    def test_cleared_autosaved_draft_is_deleted_on_reject_and_saved_avoids_tree_reload(self):
        from mailclient.ui.main_window import MainWindow
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "m.db")
            try:
                aid = db.save_account(Account(name="A", email="a@x.rs", username="a", imap_host="h", smtp_host="h"))
                dlg = ComposeDialog(db, account_id=aid)
                dlg.subject.setText("Temporary")
                dlg._dirty = True
                dlg._save_draft()
                mid = dlg._draft_id
                self.assertIsNotNone(mid)
                self.assertIsNotNone(db.message(mid))

                dlg.subject.setText("")
                dlg.editor.edit.setPlainText("")
                dlg.reject()
                self.assertIsNone(dlg._draft_id)
                self.assertIsNone(db.message(mid))
                dlg.close()

                fake_win = mock.Mock(spec=MainWindow)
                fake_win._closing = False
                fake_win.current = (aid, "inbox")
                fake_win.search = mock.Mock()
                fake_win.search.text.return_value = ""
                fake_win.search_scope = mock.Mock()
                fake_win.search_scope.currentData.return_value = "folder"
                MainWindow._refresh_after_draft_save(fake_win)
                fake_win.reload_messages.assert_not_called()

                fake_win.current = (aid, "drafts")
                MainWindow._refresh_after_draft_save(fake_win)
                fake_win.reload_messages.assert_called_once_with(keep_selection=True)

                fake_win.reload_messages.reset_mock()
                fake_win.current = (aid, "inbox")
                fake_win.search.text.return_value = "счёт"
                MainWindow._refresh_after_draft_save(fake_win)
                fake_win.reload_messages.assert_called_once_with(keep_selection=True)
            finally:
                db.close()

    def test_csv_import_with_first_and_last_name_columns_keeps_surname(self):
        """CSV из русского Outlook: колонки «Имя» и «Фамилия» — фамилия не теряется."""
        from mailclient.contacts import parse_contacts_import
        text = "Имя;Фамилия;Адрес эл. почты\nИван;Петров;ip@example.com\n;;bare@example.com\n"
        self.assertEqual(parse_contacts_import(text, "contacts.csv"),
                         [("Иван Петров", "ip@example.com"), ("", "bare@example.com")])

    def test_split_recipients_header_and_thread_block(self):
        from mailclient.ui.header import MessageHeader
        from mailclient.ui.viewer import _block_header
        em = EmailMessage()
        em["From"] = "Отправитель <sender@x.rs>"
        em["To"] = "Анна <anna@x.rs>"
        em["Cc"] = "Борис <boris@x.rs>, simple@x.rs"
        em["Subject"] = "Test"
        em.set_content("Привет")
        m = parse_message(em.as_bytes())
        h = MessageHeader()
        h.show_message(m, [])
        self.assertIn("<b>Кому:</b> Анна", h.recipients.text())
        self.assertIn("<b>Копия:</b> Борис, simple@x.rs", h.recipients.text())
        h.close()

        block = _block_header(m, own_folder_tag=False)
        self.assertIn("Кому: Анна", block)
        self.assertIn("Копия: Борис, simple@x.rs", block)

    def test_viewer_find_bar_and_export_pdf_fallback(self):
        from mailclient.ui import viewer
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(viewer, "HAS_WEBENGINE", False):
                view = viewer.MessageView()
            view.show_message(Message(id=1, body_text="Первый абзац. Ключевое слово здесь. Конец."))
            view.open_find()
            self.assertFalse(view.find_bar.isHidden())
            view.find_edit.setText("Ключевое")
            self.assertTrue(view.find_next())
            self.assertEqual(view.text.textCursor().selectedText(), "Ключевое")
            self.assertTrue(view.find_prev())
            self.assertEqual(view.text.textCursor().selectedText(), "Ключевое")
            view.close_find()
            self.assertTrue(view.find_bar.isHidden())

            done = []
            pdf_path = Path(tmp) / "letter.pdf"
            view.export_pdf(str(pdf_path), on_done=done.append)
            self.assertEqual(done, [True])
            self.assertTrue(pdf_path.is_file())
            self.assertTrue(pdf_path.read_bytes().startswith(b"%PDF"))
            view.close()

    def test_contacts_export_and_import_csv_and_vcard(self):
        from mailclient.contacts import export_contacts_csv, export_contacts_vcard, parse_contacts_import
        from mailclient.db import Contact
        contacts = [
            Contact(name="Петров, Иван", email="ivan@x.rs"),
            Contact(name='ООО "Ромашка"', email="info@romashka.rs"),
        ]
        csv_text = export_contacts_csv(contacts)
        self.assertEqual(
            parse_contacts_import(csv_text, "contacts.csv"),
            [("Петров, Иван", "ivan@x.rs"), ('ООО "Ромашка"', "info@romashka.rs")],
        )
        vcf_text = export_contacts_vcard(contacts)
        self.assertEqual(
            parse_contacts_import(vcf_text, "contacts.vcf"),
            [("Петров, Иван", "ivan@x.rs"), ('ООО "Ромашка"', "info@romashka.rs")],
        )

        # vCard 2.1 с Quoted-Printable и мягким переносом строки
        qp_vcard = (
            "BEGIN:VCARD\r\nVERSION:2.1\r\n"
            "FN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=D0=98=D0=B2=D0=B0=D0=BD=\r\n"
            " =D0=9F=D0=B5=D1=82=D1=80=D0=BE=D0=B2\r\n"
            "EMAIL;INTERNET:qp@x.rs\r\nEND:VCARD\r\n"
        )
        self.assertEqual(parse_contacts_import(qp_vcard, "qp.vcf"), [("Иван Петров", "qp@x.rs")])

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "m.db")
            try:
                db.save_account(Account(name="Я", email="me@x.rs", username="me", imap_host="h", smtp_host="h"))
                old_id = db.save_contact(Contact(name="Удалённый", email="hidden@x.rs"))
                db.delete_contacts([old_id])
                added = db.import_contacts([
                    ("Свой", "me@x.rs"),
                    ("Плохой", "not-an-email"),
                    ("Скрытый снова", "hidden@x.rs"),
                    ("Новый", "new@x.rs"),
                ])
                self.assertEqual(added, 2)
                by_email = {c.email: c.name for c in db.contacts()}
                self.assertEqual(by_email, {"hidden@x.rs": "Скрытый снова", "new@x.rs": "Новый"})
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
