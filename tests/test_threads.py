"""Переписка: сборка цепочки писем и показ её в окне письма."""
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from mailclient import threads
from mailclient.db import Account, Database, Message
from mailclient.mailparse import parse_message
from mailclient.ui import main_window, viewer
from mailclient.ui.viewer import _MailHTML, build_thread_page, scope_css, text_fragment


def raw_mail(mid, subject, sender, to, date="Mon, 2 Nov 2026 09:00:00 +0100", body="text",
             in_reply_to="", references="", html=False):
    headers = [f"From: {sender}", f"To: {to}", f"Subject: {subject}", f"Message-ID: <{mid}>", f"Date: {date}",
               "MIME-Version: 1.0", f"Content-Type: text/{'html' if html else 'plain'}; charset=utf-8",
               "Content-Transfer-Encoding: 8bit"]
    if in_reply_to:
        headers.append(f"In-Reply-To: <{in_reply_to}>")
    if references:
        headers.append(f"References: {references}")
    return ("\r\n".join(headers) + "\r\n\r\n" + body + "\r\n").encode("utf-8")


class ThreadDbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mail.db"
        self.db = Database(self.path)
        self.addCleanup(self.db.close)
        self.acc = self.db.save_account(Account(email="me@aviora.rs", storage_dir=self.tmp.name))
        self.uid = 0

    def add(self, mid, subject, sender, to, ts, folder="inbox", account=None, **kw):
        self.uid += 1
        m = parse_message(raw_mail(mid, subject, sender, to, **kw))
        m.account_id, m.folder, m.server_folder, m.uid, m.date_ts = account or self.acc, folder, folder, self.uid, ts
        return self.db.add_message(m)


class TestThreadKeys(unittest.TestCase):
    def test_message_id_and_references(self):
        self.assertEqual(threads.msgid_key(" <AbC@Example.COM> "), "abc@example.com")
        self.assertEqual(threads.ref_keys("<c@x>", "<a@x>\r\n <B@x> <c@x>"), ["a@x", "b@x", "c@x"])
        self.assertEqual(threads.ref_keys("bare@x", ""), ["bare@x"])
        self.assertEqual(threads.ref_keys("", ""), [])

    def test_subject_prefixes(self):
        for subject in ("Re: Проект", "RE: RE: Проект", "Fwd: re[2]: Проект", "AW: Проект", "Odg: Проект",
                        "Ответ: Проект", "Fw:  проект "):
            with self.subTest(subject=subject):
                self.assertEqual(threads.subject_key(subject), "проект")
                self.assertTrue(threads.has_reply_prefix(subject))
        self.assertFalse(threads.has_reply_prefix("Проект: этап 2"))
        self.assertEqual(threads.subject_key("Recall: x"), "recall: x")

    def test_header_reading_stops_at_body(self):
        raw = raw_mail("a@x", "s", "a@x", "b@x", in_reply_to="p@x", body="References: <fake@x>\r\n" + "x" * 100000)
        path = Path(tempfile.mkdtemp()) / "m.eml"
        path.write_bytes(raw)
        self.assertEqual(threads.raw_refs(threads.read_header_bytes(str(path))), ["p@x"])
        self.assertEqual(threads.read_header_bytes(None), b"")


class TestThreadQuery(ThreadDbCase):
    def test_chain_in_both_directions_across_folders_and_accounts(self):
        other = self.db.save_account(Account(email="second@aviora.rs", storage_dir=self.tmp.name))
        a = self.add("a@x", "Поставка", "Ivan <ivan@el.rs>", "me@aviora.rs", 100)
        b = self.add("b@x", "Re: Поставка", "me@aviora.rs", "ivan@el.rs", 200, folder="sent",
                     in_reply_to="a@x", references="<a@x>")
        c = self.add("c@x", "RE: Поставка", "Ivan <ivan@el.rs>", "me@aviora.rs, second@aviora.rs", 300,
                     in_reply_to="b@x", references="<a@x> <b@x>")
        c_copy = self.add("c@x", "RE: Поставка", "Ivan <ivan@el.rs>", "me@aviora.rs, second@aviora.rs", 300,
                          account=other, in_reply_to="b@x", references="<a@x> <b@x>")
        # ответ на середину цепочки без полного References
        d = self.add("d@x", "Re: Поставка", "Petar <p@el.rs>", "ivan@el.rs", 250, folder="archive", in_reply_to="b@x")
        for start in (a, b, c, d):
            with self.subTest(start=start):
                ids = self.db.thread(start)
                self.assertEqual(sorted(ids, key=lambda i: self.db.message(i, with_raw=False).date_ts), ids)
                self.assertEqual(len(ids), 4)  # копия письма из второго ящика — один раз
                self.assertIn(a, ids); self.assertIn(b, ids); self.assertIn(d, ids)
        self.assertIn(c, self.db.thread(a))
        self.assertIn(c_copy, self.db.thread(c_copy))  # открыто из второго ящика — показывается эта копия
        self.assertNotIn(c, self.db.thread(c_copy))

    def test_drafts_trash_and_spam_are_not_shown(self):
        a = self.add("a@x", "S", "ivan@el.rs", "me@aviora.rs", 100)
        for i, folder in enumerate(("drafts", "outbox", "trash", "spam")):
            self.add(f"r{i}@x", "Re: S", "ivan@el.rs", "me@aviora.rs", 200 + i, folder=folder, in_reply_to="a@x")
        self.assertEqual(self.db.thread(a), [a])
        trashed = self.db.conn().execute("SELECT id FROM messages WHERE folder='trash'").fetchone()[0]
        self.assertEqual(self.db.thread(trashed), [a, trashed])  # открытое из корзины письмо — в переписке

    def test_subject_fallback_needs_reply_prefix_and_common_person(self):
        a = self.add("a@x", "Счёт за октябрь", "Ivan <ivan@el.rs>", "me@aviora.rs", 100)
        reply = self.add("b@x", "Re: Счёт за октябрь", "me@aviora.rs", "ivan@el.rs", 200, folder="sent")
        stranger = self.add("c@x", "Re: Счёт за октябрь", "x@other.rs", "me@aviora.rs", 300)
        monthly = self.add("d@x", "Счёт за октябрь", "Ivan <ivan@el.rs>", "me@aviora.rs", 400 + 365 * 86400)
        self.add("z@x", "Счёт за сентябрь", "ivan@el.rs", "me@aviora.rs", 50)
        linked = self.add("e@x", "Re: Счёт за октябрь", "ivan@el.rs", "me@aviora.rs", 500, in_reply_to="z@x")
        dangling = self.add("f@x", "Re: Счёт за октябрь", "ivan@el.rs", "me@aviora.rs", 600, in_reply_to="lost@x")
        ids = self.db.thread(a)
        self.assertEqual(ids, [a, reply, dangling])  # ответ на письмо, которого нет в программе, — по теме
        self.assertNotIn(stranger, ids)   # общий только наш ящик
        self.assertNotIn(monthly, ids)    # без «Re:» и через год — другое письмо
        self.assertNotIn(linked, ids)     # ссылается на другую переписку
        self.assertEqual(self.db.thread(reply), [a, reply, dangling])

    def test_generic_short_subject_does_not_merge(self):
        a = self.add("a@x", "Re: ok", "ivan@el.rs", "me@aviora.rs", 100)
        self.add("b@x", "Re: ok", "ivan@el.rs", "me@aviora.rs", 200)
        self.assertEqual(self.db.thread(a), [a])

    def test_limit_keeps_latest_and_opened(self):
        first = self.add("m0@x", "T", "ivan@el.rs", "me@aviora.rs", 1)
        for i in range(1, 12):
            self.add(f"m{i}@x", "Re: T", "ivan@el.rs", "me@aviora.rs", 1 + i, in_reply_to=f"m{i - 1}@x")
        ids = self.db.thread(first, limit=5)
        self.assertEqual(len(ids), 5)
        self.assertEqual(ids[0], first)

    def test_compositions_are_indexed(self):
        a = self.add("a@x", "Тема", "ivan@el.rs", "me@aviora.rs", 100)
        raw = raw_mail("own@aviora.rs", "Re: Тема", "me@aviora.rs", "ivan@el.rs", in_reply_to="a@x",
                       references="<a@x>")
        draft = self.db.save_composition(self.acc, raw, {}, queued=True)
        self.db.update_message(draft, folder="sent")  # письмо ушло
        self.assertEqual(self.db.thread(a), [a, draft])
        self.db.delete_message(draft)
        self.assertEqual(self.db.conn().execute("SELECT count(*) FROM message_refs").fetchone()[0], 0)


class TestThreadBackfill(ThreadDbCase):
    def test_old_database_is_indexed_from_eml_headers(self):
        a = self.add("a@x", "S", "ivan@el.rs", "me@aviora.rs", 100)
        b = self.add("b@x", "Re: S", "me@aviora.rs", "ivan@el.rs", 200, folder="sent", in_reply_to="a@x")
        self.db.close()
        # база версии 0.13: без новых колонок и таблицы ссылок
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("DROP TABLE message_refs")
            # DROP COLUMN в некоторых SQLite портит DDL с комментариями --.
            # Воссоздаём настоящую старую таблицу с её типами и ограничениями.
            schema = c.execute("SELECT sql FROM sqlite_schema WHERE name='messages'").fetchone()[0]
            schema = re.sub(r"--[^\n]*", "", schema)
            schema = re.sub(r"^\s*(?:msgid_key|subject_key|refs)\s+TEXT[^\n]*", "", schema, flags=re.M)
            schema = re.sub(r",\s*\)$", ")", schema.strip())
            schema = schema.replace("CREATE TABLE messages", "CREATE TABLE messages_legacy", 1)
            c.execute(schema)
            columns = [r[1] for r in c.execute("PRAGMA table_info(messages_legacy)")]
            names = ",".join(columns)
            c.execute(f"INSERT INTO messages_legacy({names}) SELECT {names} FROM messages")
            c.execute("DROP TABLE messages")
            c.execute("ALTER TABLE messages_legacy RENAME TO messages")
            c.commit()
        self.db = Database(self.path)
        self.addCleanup(self.db.close)
        self.assertFalse(self.db.threads_ready())
        self.assertEqual(self.db.build_threads(), 2)
        self.assertTrue(self.db.threads_ready())
        self.assertEqual(self.db.thread(a), [a, b])
        self.assertEqual(self.db.build_threads(), 0)

    def test_opened_message_is_indexed_before_background_task(self):
        a = self.add("a@x", "S", "ivan@el.rs", "me@aviora.rs", 100)
        b = self.add("b@x", "Re: S", "me@aviora.rs", "ivan@el.rs", 200, folder="sent", in_reply_to="a@x")
        self.db.conn().execute("UPDATE messages SET refs=NULL")
        self.db.conn().execute("DELETE FROM message_refs")
        self.db.conn().commit()
        self.assertEqual(self.db.thread(b), [a, b])

    def test_message_without_file_and_headers(self):
        mid = self.db.add_message(Message(account_id=self.acc, subject="x", sender="a@b.rs", uid=5))
        self.assertEqual(self.db.thread(mid), [mid])
        self.assertTrue(self.db.threads_ready())


class TestThreadHtml(unittest.TestCase):
    def fragment(self, source):
        p = _MailHTML(fragment=True, fold_quotes=True)
        p.feed(source)
        return p.finish(), p.styles

    def test_document_tags_scripts_and_styles(self):
        body, styles = self.fragment("<html><head><title>T</title><style>p{color:red}</style>"
                                     "<script>alert(1)</script></head><body bgcolor=red><p>Hi</p></body></html>")
        self.assertEqual(body, "<p>Hi</p>")
        self.assertEqual(styles, ["p{color:red}"])

    def test_unclosed_tags_are_closed_and_stray_end_tags_dropped(self):
        self.assertEqual(self.fragment("<div><table><tr><td>x")[0], "<div><table><tr><td>x</td></tr></table></div>")
        self.assertEqual(self.fragment("<p>a</p></div></details>")[0], "<p>a</p>")

    def test_quotes_are_folded(self):
        body, _ = self.fragment("<p>new</p><blockquote type=cite><p>old</blockquote><p>after</p>")
        self.assertRegex(body, r"^<p>new</p><details class=\"quote\"><summary[^>]*>•••</summary>"
                               r"<blockquote type=cite><p>old</p></blockquote></details><p>after</p>$")
        body, _ = self.fragment("<div class=\"gmail_quote\"><div>On x wrote:</div><blockquote>q</blockquote></div>")
        self.assertEqual(body.count("<details"), 1)

    def test_outlook_quote_folds_to_the_end(self):
        body, _ = self.fragment(
            "<div class=WordSection1><p>reply</p><div><div style='border:none;border-top:solid #E1E1E1 1.0pt;"
            "padding:3.0pt 0cm 0cm 0cm'><p><b>From:</b> x</p></div></div><p>quoted</p></div>")
        self.assertTrue(body.startswith("<div class=WordSection1><p>reply</p><div><details"))
        self.assertLess(body.index("<details"), body.index("quoted"))
        self.assertLess(body.index("quoted"), body.index("</details>"))
        self.assertEqual(body.count("<div"), body.count("</div>"))
        body, _ = self.fragment("<p>reply</p><div id=\"divRplyFwdMsg\">From: x</div><p>quoted</p>")
        self.assertLess(body.index("<details"), body.index("divRplyFwdMsg"))
        self.assertTrue(body.endswith("<p>quoted</p></details>"))

    def test_plain_text_quotes(self):
        out = text_fragment("Ответ\nOn Mon wrote:\n> a\n> b\n> c\nmy\n> one")
        self.assertEqual(out.count("<details"), 1)
        self.assertIn("my\n<span class=\"q\">&gt; one</span>", out)  # короткая цитата видна
        out = text_fragment("Да.\n\n-----Original Message-----\nFrom: x\nold")
        self.assertTrue(out.startswith("Да.\n"))
        self.assertIn("<details", out)
        out = text_fragment("Ok\nОт: Иван\nОтправлено: вчера\nКому: я\nстарое")
        self.assertLess(out.index("<details"), out.index("От: Иван"))
        self.assertIn("&lt;script&gt;", text_fragment("<script>"))

    def test_css_is_scoped_to_message(self):
        css = scope_css("@import url(x.css); /* c */ body{margin:0} html body p, :root .a{b:2}"
                        "@media screen{td{c:3}} @font-face{font-family:X} *{d:4}", "#m1>.mb")
        self.assertEqual(css.splitlines(), ["#m1>.mb{margin:0}", "#m1>.mb p,#m1>.mb .a{b:2}",
                                            "@media screen{#m1>.mb td{c:3}}", "#m1>.mb *{d:4}"])


class TestThreadPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def items(self):
        return [
            Message(id=1, sender="Ivan <ivan@el.rs>", recipients="me@aviora.rs", date_ts=100, body_text="Первое"),
            Message(id=2, sender="Shop <shop@x.rs>", recipients="me@aviora.rs", date_ts=200, folder="sent",
                    body_html='<p>Второе</p><img src="https://x.rs/t.png">', body_text="Второе"),
            Message(id=3, sender="Ivan <ivan@el.rs>", recipients="me@aviora.rs", date_ts=300,
                    body_html='<style>p{color:red}</style><p>Третье</p><img src="https://x.rs/b.png">',
                    body_text="Третье"),
        ]

    def test_layout_and_collapsing(self):
        page = build_thread_page(self.items(), 2, set(), False, "")
        self.assertEqual(page.html.count("<details id=\"m"), 3)
        self.assertEqual(len(re.findall(r'<details id="m\d+" class="msg[^"]*" open>', page.html)), 3)
        self.assertIn('<details id="m2" class="msg sel" open>', page.html)
        self.assertIn("#m3>.mb p{color:red}", page.html)
        self.assertLess(page.html.index("Первое"), page.html.index("Второе"))
        page = build_thread_page(self.items(), 2, set(), True, "")
        self.assertEqual(len(re.findall(r'<details id="m\d+" class="msg[^"]*" open>', page.html)), 1)
        self.assertIn('<details id="m2" class="msg sel" open>', page.html)
        self.assertIn("simplemail:/cmd/newest", page.html)

    def test_newest_first_order(self):
        page = build_thread_page(self.items(), 2, set(), False, "", newest_first=True)
        self.assertEqual(re.findall(r'<details id="m(\d+)"', page.html), ["3", "2", "1"])
        self.assertIn("simplemail:/cmd/oldest", page.html)

    def test_remote_images_per_message(self):
        page = build_thread_page(self.items(), 2, set(), False, "")
        self.assertEqual(page.blocked_ids, [2, 3])
        self.assertFalse(page.allow_remote)
        page = build_thread_page(self.items(), 2, {2}, False, "")
        self.assertTrue(page.allow_remote)
        self.assertEqual(page.blocked_ids, [3])
        self.assertIn("https://x.rs/t.png", page.html)
        self.assertNotIn("https://x.rs/b.png", page.html)  # у неразрешённого письма внешние картинки вырезаны

    def test_view_commands_and_load_remote(self):
        with mock.patch.object(viewer, "HAS_WEBENGINE", False):
            v = viewer.MessageView()
        items = self.items()
        loaded, trusted = [], []
        v.remote_loaded.connect(lambda m: loaded.append(m.id))
        v.remote_allowed_for_sender.connect(trusted.append)
        v.show_thread(items[1], items, set())
        self.assertTrue(v.thread_shown)
        self.assertFalse(v.banner.isHidden())
        self.assertTrue(v.trust_btn.isHidden())  # два разных отправителя
        v.open_link(viewer.QUrl("simplemail:/cmd/collapse"))
        self.assertTrue(v._thread["collapsed"])
        v.run_command("/cmd/expand")
        self.assertFalse(v._thread["collapsed"])
        order = []
        v.thread_order_changed.connect(order.append)
        v.run_command("/cmd/newest")
        self.assertTrue(v._thread["newest_first"])
        self.assertLess(v._page_html.index('id="m3"'), v._page_html.index('id="m1"'))
        v.run_command("/cmd/oldest")
        self.assertEqual(order, [True, False])
        self.assertLess(v._page_html.index('id="m1"'), v._page_html.index('id="m3"'))
        v.show_message(items[0])
        self.assertFalse(v.thread_shown)
        self.assertFalse(v.trust_btn.isHidden())


class TestThreadInWindow(ThreadDbCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_selected_message_shows_thread_and_toggle(self):
        a = self.add("a@x", "S", "Ivan <ivan@el.rs>", "me@aviora.rs", 100, body="Первое")
        b = self.add("b@x", "Re: S", "me@aviora.rs", "ivan@el.rs", 200, folder="sent", in_reply_to="a@x",
                     body="Второе")
        single = self.add("c@x", "Другое", "ivan@el.rs", "me@aviora.rs", 300)

        def run_now(fn, *args, on_done=None, on_error=None, **kw):
            if getattr(fn, "__name__", "") != "work":
                return  # проверка почты и прочие фоновые задачи здесь не нужны
            result = fn(*args, **kw)
            if on_done:
                on_done(result)

        with mock.patch.object(main_window, "run_task", side_effect=run_now), \
                mock.patch("mailclient.ui.mail_watch.MailWatcher"), \
                mock.patch.object(main_window.MainWindow, "check_mail"), \
                mock.patch.object(viewer, "HAS_WEBENGINE", False):
            window = main_window.MainWindow(self.db)
            try:
                window.acts["thread_view"].setChecked(True)
                window._show_message(b)
                self.assertTrue(window.viewer.thread_shown)
                self.assertEqual([m.id for m in window.viewer._thread["items"]], [a, b])
                self.assertEqual(window.viewer._msg.id, b)
                # по умолчанию новые письма сверху, порядок меняется в меню «Вид» и запоминается
                html_order = lambda: re.findall(r'<details id="m(\d+)"', window.viewer._page_html)
                self.assertEqual(html_order(), [str(b), str(a)])
                window.acts["thread_newest"].trigger()
                self.assertEqual(html_order(), [str(a), str(b)])
                self.assertEqual(self.db.get_setting("thread_newest_first"), "0")
                window.viewer.run_command("/cmd/newest")
                self.assertTrue(window.acts["thread_newest"].isChecked())
                self.assertEqual(self.db.get_setting("thread_newest_first"), "1")
                self.assertEqual(html_order(), [str(b), str(a)])
                self.assertFalse(window.header.thread_btn.isHidden())
                self.assertIn("2", window.header.thread_btn.text())
                window.header.thread_btn.click()  # выключить: только это письмо
                self.assertFalse(window.viewer.thread_shown)
                self.assertFalse(window.acts["thread_view"].isChecked())
                self.assertEqual(self.db.get_setting("thread_view"), "0")
                self.assertFalse(window.header.thread_btn.isHidden())  # число писем видно и так
                window.acts["thread_view"].trigger()
                self.assertTrue(window.viewer.thread_shown)
                self.assertTrue(window.header.thread_btn.isChecked())
                window._show_message(single)
                self.assertFalse(window.viewer.thread_shown)
                self.assertTrue(window.header.thread_btn.isHidden())
            finally:
                with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
                    window.close()


if __name__ == "__main__":
    unittest.main()
