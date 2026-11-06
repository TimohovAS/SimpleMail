"""Тесты ядра без сети: python -m unittest discover tests"""
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from mailclient import protocols
from mailclient.db import Account, Database
from mailclient.mailparse import parse_message
from mailclient.spam import SpamFilter


def make_raw(sender, subject, body, extra_headers=None, html=None):
    em = EmailMessage()
    em["From"] = sender
    em["To"] = "me@example.com"
    em["Subject"] = subject
    em["Date"] = "Wed, 04 Nov 2026 10:00:00 +0300"
    em["Message-ID"] = f"<{abs(hash((sender, subject, body)))}@test>"
    for k, v in (extra_headers or {}).items():
        em[k] = v
    em.set_content(body)
    if html:
        em.add_alternative(html, subtype="html")
    return em.as_bytes()


SPAM = [
    ("Казино <win@casino-luck.biz>", "Вы выиграли миллион!", "Срочно заберите выигрыш, переходите по ссылке http://x.biz бесплатно"),
    ("Promo <promo@cheap-pills.top>", "Скидка 90% только сегодня", "Купить дешево, бесплатно доставка, выигрыш гарантирован http://p.top"),
    ("Lottery <lotto@mega.win>", "Выигрыш ждёт вас", "Поздравляем, вы выиграли приз! Заберите бесплатно http://l.win"),
    ("Bank <secure@bank-verify.ru.com>", "Срочно подтвердите карту", "Ваш аккаунт заблокирован, срочно перейдите по ссылке http://b.ru.com"),
    ("Казино <bonus@casino-luck.biz>", "Бонус 500% на депозит", "Бесплатно бонус казино выигрыш http://c.biz"),
    ("Earn <job@money-fast.xyz>", "Заработок от 100000 в день", "Бесплатно, без вложений, срочно http://m.xyz"),
]
HAM = [
    ("Иван Петров <ivan@company.ru>", "Отчёт за октябрь", "Привет, прикладываю отчёт за октябрь. Посмотри, пожалуйста, до пятницы."),
    ("Мария <maria@company.ru>", "Совещание в среду", "Коллеги, совещание переносится на среду, 11:00, переговорная 3."),
    ("Бухгалтерия <buh@company.ru>", "Счёт на оплату", "Добрый день. Направляем счёт на оплату по договору № 15."),
    ("Иван Петров <ivan@company.ru>", "Re: договор", "Согласовал договор с юристами, правки в приложении."),
    ("Олег <oleg@partner.ru>", "Поставка", "Поставка придёт в четверг, водитель позвонит заранее."),
    ("Мария <maria@company.ru>", "Протокол", "Протокол совещания во вложении, проверьте свои задачи."),
]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.acc_id = self.db.save_account(Account(name="Test", email="me@example.com", username="me",
                                                   imap_host="imap.example.com", smtp_host="smtp.example.com"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def add(self, raw, folder="inbox"):
        m = parse_message(raw)
        m.account_id, m.folder = self.acc_id, folder
        m.id = self.db.add_message(m)
        return m


class TestParse(Base):
    def test_parse_cyrillic_and_html(self):
        raw = make_raw("Иван <ivan@company.ru>", "Привет, мир", "Текст письма", html="<p>Текст <b>письма</b></p>")
        m = parse_message(raw)
        self.assertEqual(m.subject, "Привет, мир")
        self.assertIn("Иван", m.sender)
        self.assertIn("Текст письма", m.body_text)
        self.assertIn("<b>", m.body_html)
        self.assertGreater(m.date_ts, 0)


class TestSpam(Base):
    def test_rules(self):
        f = SpamFilter(self.db)
        self.db.add_spam_rule("black", "subject", "выиграли")
        self.db.add_spam_rule("white", "from", "@company.ru")
        spam = parse_message(make_raw(*SPAM[0]))
        self.assertTrue(f.classify(spam).is_spam)
        # белый список важнее чёрного
        ham = parse_message(make_raw("ivan@company.ru", "Вы выиграли спор", "шучу"))
        self.assertFalse(f.classify(ham).is_spam)

    def test_server_flag(self):
        f = SpamFilter(self.db)
        m = parse_message(make_raw("a@b.c", "hi", "text", {"X-Spam-Flag": "YES"}))
        self.assertTrue(f.classify(m).is_spam)

    def test_bayes_learns(self):
        f = SpamFilter(self.db)
        self.assertFalse(f.bayes_ready())
        for s in SPAM:
            f.train(self.add(make_raw(*s)), True)
        for h in HAM:
            f.train(self.add(make_raw(*h)), False)
        self.assertTrue(f.bayes_ready())
        new_spam = parse_message(make_raw("Луки <x@casino-luck.biz>", "Срочно: выигрыш!", "Бесплатно заберите приз казино http://z.biz"))
        new_ham = parse_message(make_raw("Иван Петров <ivan@company.ru>", "Отчёт", "Посмотри договор и счёт до пятницы, пожалуйста."))
        self.assertTrue(f.classify(new_spam).is_spam, f.bayes_score(new_spam))
        self.assertFalse(f.classify(new_ham).is_spam, f.bayes_score(new_ham))

    def test_retrain_reverses(self):
        f = SpamFilter(self.db)
        m = self.add(make_raw(*HAM[0]))
        f.train(m, True)
        f.train(m, False)
        self.assertEqual(self.db.get_setting("bayes_spam_docs"), "0")
        self.assertEqual(self.db.get_setting("bayes_ham_docs"), "1")


class FakeIMAP:
    """Минимальная имитация imaplib для теста синхронизации."""
    def __init__(self, messages):
        self.messages = messages  # {uid: raw}

    def select(self, name, readonly=False):
        return "OK", [str(len(self.messages)).encode()]

    def list(self, *args):
        return "OK", [b'() "/" INBOX']

    def response(self, code):
        return code, [b"42"]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            if args[-1] == "ALL":
                return "OK", [" ".join(map(str, sorted(self.messages))).encode()]
            lo = int(args[1].split()[1].split(":")[0])
            found = [u for u in sorted(self.messages) if u >= lo] or [max(self.messages)]
            return "OK", [" ".join(map(str, found)).encode()]
        if cmd == "FETCH":
            from mailclient.threads import header_bytes
            data = []
            for u in (sorted(self.messages) if args[0] == "1:*" else map(int, args[0].split(","))):
                raw = self.messages[u]
                meta = f"{u} (UID {u} FLAGS () RFC822.SIZE {len(raw)}".encode()
                if "BODY.PEEK[HEADER]" in args[1] or "BODY.PEEK[]" in args[1]:
                    section = "HEADER" if "BODY.PEEK[HEADER]" in args[1] else ""
                    payload = header_bytes(raw) if section else raw
                    data.append((meta + f" BODY[{section}] {{{len(payload)}}}".encode(), payload))
                else:
                    data.append(meta)
                data.append(b")")
            return "OK", data

    def logout(self):
        pass


class TestSync(Base):
    def test_sync_and_incremental(self):
        self.db.add_spam_rule("black", "from", "casino-luck.biz")
        msgs = {1: make_raw(*HAM[0]), 2: make_raw(*SPAM[0]), 3: make_raw(*HAM[1])}
        fake = FakeIMAP(msgs)
        acc = self.db.account(self.acc_id)
        with mock.patch.object(protocols, "imap_connect", return_value=fake), \
             mock.patch.object(protocols, "get_password", return_value="x"):
            self.assertEqual(protocols.sync_account(self.db, acc), 3)
            self.assertEqual(len(self.db.list_messages(self.acc_id, "inbox")), 2)
            self.assertEqual(len(self.db.list_messages(self.acc_id, "spam")), 1)
            # повторная синхронизация не дублирует
            self.assertEqual(protocols.sync_account(self.db, acc), 0)
            msgs[4] = make_raw(*HAM[2])
            self.assertEqual(protocols.sync_account(self.db, acc), 1)


if __name__ == "__main__":
    unittest.main()


class TestLogin(unittest.TestCase):
    def test_imap_utf8_password_uses_authenticate(self):
        im = mock.Mock()
        protocols.imap_login(im, "user@x.rs", "пароль")
        im.login.assert_not_called()
        mech, cb = im.authenticate.call_args[0]
        self.assertEqual(mech, "PLAIN")
        self.assertEqual(cb(b""), b"\0user@x.rs\0" + "пароль".encode())

    def test_imap_ascii_password_uses_login(self):
        im = mock.Mock()
        protocols.imap_login(im, "u", "secret")
        im.login.assert_called_once_with("u", "secret")

    def test_smtp_utf8_password(self):
        s = mock.Mock()
        s.has_extn.return_value = True
        s.docmd.return_value = (235, b"ok")
        protocols.smtp_login(s, "u", "пароль")
        s.login.assert_not_called()
        self.assertTrue(s.docmd.call_args[0][1].startswith("PLAIN "))

    def test_friendly_errors(self):
        import socket, imaplib
        self.assertIn("не найден", protocols.friendly_error(socket.gaierror(11001, "x")))
        self.assertIn("пароль", protocols.friendly_error(imaplib.IMAP4.error("AUTHENTICATIONFAILED Invalid credentials")))
        self.assertIn("STARTTLS", protocols.friendly_error(imaplib.IMAP4.error("LOGIN => LOGINDISABLED")))


class TestStorage(Base):
    def test_eml_files(self):
        store = Path(self.tmp.name) / "Почта"
        acc = self.db.account(self.acc_id)
        acc.storage_dir = str(store)
        self.db.save_account(acc)
        raw = make_raw(*HAM[0])
        m = parse_message(raw); m.account_id = self.acc_id; m.uid = 7; m.server_folder = "INBOX"
        mid = self.db.add_message(m)
        files = list(store.rglob("*.eml"))
        self.assertEqual(len(files), 1)
        self.assertIn("Входящие", str(files[0]))
        self.assertEqual(files[0].read_bytes(), raw)
        self.assertEqual(self.db.message(mid).raw, raw)  # читается из файла
        # повтор того же письма не создаёт второй записи
        self.assertIsNone(self.db.add_message(m))
        # перенос папки
        new = Path(self.tmp.name) / "Новая"
        self.assertEqual(self.db.move_storage(self.acc_id, str(store), str(new)), 1)
        self.assertEqual(self.db.message(mid).raw, raw)
        self.assertEqual(len(list(new.rglob("*.eml"))), 1)
        self.assertEqual(len(list(store.rglob("*.eml"))), 0)
        # удаление навсегда удаляет файл
        self.db.delete_message(mid)
        self.assertEqual(len(list(new.rglob("*.eml"))), 0)

    def test_migration_adds_storage_columns(self):
        import sqlite3
        c = sqlite3.connect(self.db.path)
        cols = {r[1] for r in c.execute("PRAGMA table_info(messages)")}
        c.close()
        self.assertIn("raw_path", cols)


class TestViewerHelpers(unittest.TestCase):
    def test_remote_detection_and_inline(self):
        from mailclient.ui.viewer import has_remote_content, inline_parts, text_to_html
        self.assertTrue(has_remote_content('<img src="https://t.example/p.gif">'))
        self.assertTrue(has_remote_content("<td style=\"background:url('http://x/y.png')\">"))
        self.assertFalse(has_remote_content('<img src="cid:logo"> <a href="https://site">link</a>'))
        em = EmailMessage(); em["From"] = "a@b"; em.set_content("t")
        em.add_alternative("<img src='cid:logo'>", subtype="html")
        em.get_payload()[1].add_related(b"PNGDATA", "image", "png", cid="<logo>")
        self.assertEqual(inline_parts(em.as_bytes())["logo"], ("image/png", b"PNGDATA"))
        h = text_to_html("Привет <b>\n> цитата\nhttps://example.com/x")
        self.assertIn("&lt;b&gt;", h); self.assertIn('class="q"', h); self.assertIn('href="https://example.com/x"', h)


class TestBuildMessage(unittest.TestCase):
    def test_html_with_inline_image_and_attachment(self):
        import email as email_mod
        from email import policy as pol
        acc = Account(name="Андрей", email="a@x.rs")
        em = protocols.build_message(acc, "b@y.rs", "Тема", "текст", html="<p><b>текст</b><img src='cid:i1@s'></p>",
                                     inline_images=[("i1@s", "image/png", b"PNG")],
                                     attachments=[("отчёт.pdf", b"%PDF")])
        msg = email_mod.message_from_bytes(em.as_bytes(), policy=pol.default)
        types = [p.get_content_type() for p in msg.walk()]
        self.assertEqual(types, ["multipart/mixed", "multipart/alternative", "text/plain", "multipart/related",
                                 "text/html", "image/png", "application/pdf"])
        self.assertEqual(msg["From"], "Андрей <a@x.rs>")
        self.assertEqual([p.get_filename() for p in msg.iter_attachments()], ["отчёт.pdf"])

    def test_from_without_duplicate_name(self):
        em = protocols.build_message(Account(name="a@x.rs", email="a@x.rs"), "b@y.rs", "s", "t")
        self.assertEqual(em["From"], "a@x.rs")


class TestAttachmentsHelpers(unittest.TestCase):
    def test_unique_path_and_size(self):
        from mailclient.ui.attachments_bar import human_size, unique_path
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.pdf").write_bytes(b"1")
            self.assertEqual(unique_path(Path(d), "a.pdf").name, "a (1).pdf")
            self.assertEqual(unique_path(Path(d), "../evil/b.txt").name, "b.txt")  # без выхода из папки
        self.assertEqual(human_size(1536), "2 КБ")
        self.assertEqual(human_size(3 * 1048576), "3,0 МБ")

    def test_inline_images_not_listed_as_attachments(self):
        from mailclient.mailparse import attachments
        em = EmailMessage(); em["From"] = "a@b"; em.set_content("t")
        em.add_alternative("<img src='cid:i'>", subtype="html")
        em.get_payload()[1].add_related(b"PNG", "image", "png", cid="<i>", filename="logo.png", disposition="inline")
        em.add_attachment(b"PDF", maintype="application", subtype="pdf", filename="doc.pdf")
        self.assertEqual([n for n, _ in attachments(em.as_bytes())], ["doc.pdf"])


class TestOutlookInline(unittest.TestCase):
    def test_referenced_cid_images_hidden_even_if_attachment_disposition(self):
        from mailclient.mailparse import attachments
        em = EmailMessage(); em["From"] = "a@b"; em.set_content("t")
        em.add_alternative("<p>Pozdrav</p><img src=\"cid:image001.png@01DB1234\">", subtype="html")
        em.get_payload()[1].add_related(b"PNG", "image", "png", cid="<image001.png@01DB1234>",
                                        filename="image001.png", disposition="attachment")
        em.add_attachment(b"JPG", maintype="image", subtype="jpeg", filename="web.JPG")
        self.assertEqual([n for n, _ in attachments(em.as_bytes())], ["web.JPG"])


class TestAccountOrder(unittest.TestCase):
    def test_order_saved_and_new_account_goes_last(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "m.db")
            try:
                ids = [db.save_account(Account(name=n, email=n, username=n, imap_host="h", smtp_host="h"))
                       for n in ("a@x", "b@x", "c@x")]
                db.set_account_order([ids[2], ids[0], ids[1]])
                self.assertEqual([a.email for a in db.accounts()], ["c@x", "a@x", "b@x"])
                acc = db.account(ids[0]); acc.name = "A"; db.save_account(acc)
                db.save_account(Account(name="d@x", email="d@x", username="d", imap_host="h", smtp_host="h"))
                self.assertEqual([a.email for a in db.accounts()], ["c@x", "a@x", "b@x", "d@x"])
            finally:
                db.close()
