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

    def response(self, code):
        return code, [b"42"]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            lo = int(args[1].split()[1].split(":")[0])
            found = [u for u in sorted(self.messages) if u >= lo] or [max(self.messages)]
            return "OK", [" ".join(map(str, found)).encode()]
        if cmd == "FETCH":
            data = []
            for u in map(int, args[0].split(",")):
                data.append((f"{u} (UID {u} FLAGS () BODY[] {{1}}".encode(), self.messages[u]))
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


class TestMigration(unittest.TestCase):
    def test_old_db_gets_imap_security(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "old.db"
            c = sqlite3.connect(p)
            c.execute("""CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                email TEXT NOT NULL, username TEXT NOT NULL, imap_host TEXT NOT NULL,
                imap_port INTEGER NOT NULL DEFAULT 993, imap_ssl INTEGER NOT NULL DEFAULT 1,
                smtp_host TEXT NOT NULL, smtp_port INTEGER NOT NULL DEFAULT 465,
                smtp_security TEXT NOT NULL DEFAULT 'ssl', enabled INTEGER NOT NULL DEFAULT 1)""")
            c.execute("INSERT INTO accounts(name,email,username,imap_host,imap_ssl,smtp_host) VALUES('a','a','a','h',0,'s')")
            c.commit(); c.close()
            db = Database(p)
            self.assertEqual(db.accounts()[0].imap_security, "starttls")
            db.close()
