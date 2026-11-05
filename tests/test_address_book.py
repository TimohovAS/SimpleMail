"""Адресная книга: автозаполнение из писем, ручная правка и подсказки в полях адресов."""
import os
import sqlite3
import tempfile
import unittest
from email import message_from_bytes, policy
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from mailclient import delivery, protocols
from mailclient.contacts import format_address, is_service_address, parse_addresses, raw_addresses, valid_email
from mailclient.db import Account, Contact, Database
from mailclient.mailparse import parse_message
from mailclient.ui.address_book import AddressBookDialog, AddressEdit, _token_bounds

app = QApplication.instance() or QApplication([])


def type_text(widget, text):
    """Набор с клавиатуры (QTest.keyClicks не умеет кириллицу)."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    for ch in text:
        QApplication.sendEvent(widget, QKeyEvent(QEvent.KeyPress, 0, Qt.NoModifier, ch))
        QApplication.sendEvent(widget, QKeyEvent(QEvent.KeyRelease, 0, Qt.NoModifier, ch))


def raw_mail(sender, to, subject="Тема", cc="", bcc="", date="Wed, 04 Nov 2026 10:00:00 +0300"):
    from email.message import EmailMessage
    em = EmailMessage()
    em["From"] = sender
    em["To"] = to
    if cc:
        em["Cc"] = cc
    if bcc:
        em["Bcc"] = bcc
    em["Subject"] = subject
    em["Date"] = date
    em["Message-ID"] = f"<{abs(hash((sender, to, subject, date)))}@test>"
    em.set_content("текст")
    return em.as_bytes()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.acc = self.db.account(self.db.save_account(Account(
            name="Я", email="Me@Example.com", username="me", imap_host="i", smtp_host="s")))
        self.uid = 0

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def add(self, raw, folder="inbox", is_spam=False):
        self.uid += 1
        m = parse_message(raw)
        m.account_id, m.folder, m.is_spam = self.acc.id, folder, is_spam
        m.server_folder, m.uid, m.uidvalidity = folder, self.uid, 1
        return self.db.add_message(m)

    def book(self):
        return {c.email.casefold(): c for c in self.db.contacts()}


class TestAddressParsing(unittest.TestCase):
    def test_parse_and_format(self):
        self.assertEqual(parse_addresses('Иван Петров <ivan@x.ru>; "Петров, Пётр" <p@x.ru>, bad, a@b.c'),
                         [("Иван Петров", "ivan@x.ru"), ("Петров, Пётр", "p@x.ru"), ("", "a@b.c")])
        self.assertEqual(format_address("Петров, Пётр", "p@x.ru"), '"Петров, Пётр" <p@x.ru>')
        self.assertEqual(format_address("Иван", "i@x.ru"), "Иван <i@x.ru>")
        self.assertEqual(format_address("i@x.ru", "i@x.ru"), "i@x.ru")
        # Отформатированный адрес снова разбирается в те же имя и адрес.
        for name in ("Петров, Пётр", 'Ив"ан', "O'Brien (work)"):
            self.assertEqual(parse_addresses(format_address(name, "q@x.ru")), [(name.strip("'\" "), "q@x.ru")])

    def test_valid_and_service(self):
        for good in ("a@b.c", "ivan.petrov+tag@mail.example.rs", "x@localhost"):
            self.assertTrue(valid_email(good), good)
        for bad in ("", "a", "a@", "@b.c", "a b@c.d", "a@b..c", "a@.b", "a@b@c"):
            self.assertFalse(valid_email(bad), bad)
        for service in ("noreply@x.ru", "no-reply@x.ru", "do_not_reply@x.ru", "MAILER-DAEMON@x.ru",
                        "bounces+123@x.ru", "notifications@github.com", "info.noreply@x.ru"):
            self.assertTrue(is_service_address(service), service)
        for person in ("norman@x.ru", "replyguy@x.ru", "ivan@x.ru"):
            self.assertFalse(is_service_address(person), person)

    def test_raw_addresses_decode_names_with_commas(self):
        raw = raw_mail("me@example.com", '"Петров, Иван" <ivan@x.ru>, Анна <anna@x.ru>', cc="c@x.ru", bcc="Скрытый <h@x.ru>")
        self.assertEqual(raw_addresses(raw), [("Петров, Иван", "ivan@x.ru"), ("Анна", "anna@x.ru"),
                                              ("", "c@x.ru"), ("Скрытый", "h@x.ru")])

    def test_token_bounds_respect_quotes(self):
        text = '"Петров, Иван" <i@x.ru>, ан'
        self.assertEqual(text[slice(*_token_bounds(text, len(text)))], " ан")
        self.assertEqual(_token_bounds(text, 3), (0, text.index(">") + 1))


class TestAutomaticCollection(Fixture):
    def test_received_and_sent_mail_fill_the_book(self):
        self.add(raw_mail("Иван Петров <Ivan@X.ru>", "me@example.com"))
        self.add(raw_mail("Иван П. <ivan@x.ru>", "me@example.com", subject="2", date="Thu, 05 Nov 2026 10:00:00 +0300"))
        self.add(raw_mail("Я <me@example.com>", "Анна <anna@x.ru>, me@example.com", cc="boss@x.ru"), folder="sent")
        book = self.book()
        self.assertEqual(set(book), {"ivan@x.ru", "anna@x.ru", "boss@x.ru"})  # свой адрес не попадает
        self.assertEqual(book["ivan@x.ru"].email, "Ivan@X.ru")
        self.assertEqual(book["ivan@x.ru"].name, "Иван П.")  # имя из последнего письма
        self.assertEqual((book["ivan@x.ru"].received_count, book["ivan@x.ru"].sent_count), (2, 0))
        self.assertEqual((book["anna@x.ru"].name, book["anna@x.ru"].sent_count), ("Анна", 1))

    def test_spam_trash_and_service_senders_are_skipped(self):
        self.add(raw_mail("Казино <win@casino.biz>", "me@example.com"), is_spam=True)
        self.add(raw_mail("junk@x.biz", "me@example.com"), folder="spam")
        self.add(raw_mail("old@x.ru", "me@example.com"), folder="trash")
        self.add(raw_mail("GitHub <noreply@github.com>", "me@example.com"))
        self.assertEqual(self.book(), {})
        # Пользователь сам написал на служебный адрес — тогда он нужен в книге.
        self.add(raw_mail("me@example.com", "noreply@github.com"), folder="sent")
        self.assertEqual(set(self.book()), {"noreply@github.com"})

    def test_mail_sent_by_us_from_another_folder_counts_as_sent(self):
        self.add(raw_mail("me@example.com", "friend@x.ru"), folder="remote:Archive")
        self.assertEqual(self.book()["friend@x.ru"].sent_count, 1)

    def test_delivery_records_recipients_including_bcc_and_returns_deleted(self):
        self.add(raw_mail("Гость <guest@x.ru>", "me@example.com"))
        self.db.delete_contacts([self.book()["guest@x.ru"].id])
        self.add(raw_mail("Гость <guest@x.ru>", "me@example.com", subject="ещё"))
        self.assertNotIn("guest@x.ru", self.book())  # удалённый адрес сам не возвращается
        em = protocols.build_message(self.acc, "Гость <guest@x.ru>", "Тема", "текст", cc='"Петров, Иван" <ivan@x.ru>',
                                     bcc="secret@x.ru")
        mid = self.db.save_composition(self.acc.id, em.as_bytes(), {"to": "guest@x.ru"}, queued=True)
        smtp = mock.Mock(); smtp.send_message.return_value = {}
        with mock.patch.object(protocols, "smtp_connect", return_value=smtp), \
             mock.patch.object(protocols, "get_password", return_value="x"), \
             mock.patch.object(delivery, "append_sent"):
            self.assertEqual(delivery.deliver_outbox(self.db, mid), "sent")
        book = self.book()
        self.assertEqual(set(book), {"guest@x.ru", "ivan@x.ru", "secret@x.ru"})
        self.assertEqual(book["ivan@x.ru"].name, "Петров, Иван")
        self.assertEqual(book["guest@x.ru"].sent_count, 1)

    def test_marking_spam_forgets_sender_we_never_wrote_to(self):
        from mailclient import mailops
        stranger = self.add(raw_mail("Акция <promo@shop.ru>", "me@example.com"))
        friend = self.add(raw_mail("Друг <friend@x.ru>", "me@example.com"))
        self.add(raw_mail("me@example.com", "friend@x.ru"), folder="sent")
        self.db.update_message(stranger, server_folder=None, uid=None)
        self.db.update_message(friend, server_folder=None, uid=None)
        mailops.set_spam_state(self.db, stranger, True)
        mailops.set_spam_state(self.db, friend, True)
        self.assertEqual(set(self.book()), {"friend@x.ru"})

    def test_existing_mail_is_collected_once_after_upgrade(self):
        path = Path(self.tmp.name) / "old.db"
        db = Database(path)
        acc = db.save_account(Account(email="me@example.com", username="me", imap_host="i", smtp_host="s"))
        for i, sender in enumerate(("Старый <old@x.ru>", "old@x.ru", "Новый <new@x.ru>")):
            m = parse_message(raw_mail(sender, "me@example.com", subject=str(i)))
            m.account_id, m.date_ts = acc, 1000 + i
            db.add_message(m)
        m = parse_message(raw_mail("me@example.com", "Коллега <col@x.ru>")); m.account_id, m.folder = acc, "sent"
        db.add_message(m)
        db.close()
        # Имитация базы из версии без адресной книги.
        c = sqlite3.connect(path)  # with sqlite3.connect() не закрывает файл: на Windows папку потом не удалить
        try:
            with c:
                c.execute("DROP TABLE contacts")
                c.execute("DELETE FROM settings WHERE key='contacts_upto'")
        finally:
            c.close()
        db = Database(path)
        try:
            self.assertFalse(db.contacts_ready())
            self.assertEqual(db.contacts(), [])
            later = parse_message(raw_mail("Новый <new@x.ru>", "me@example.com", subject="после")); later.account_id = acc
            later.date_ts = 5000
            db.add_message(later)  # письмо, пришедшее во время сборки книги, записывается сразу
            self.assertEqual(db.build_contacts(batch=2), 4)
            self.assertTrue(db.contacts_ready())
            self.assertEqual(db.build_contacts(), 0)  # второй раз ничего не считает
            book = {c.email: c for c in db.contacts()}
            self.assertEqual(set(book), {"old@x.ru", "new@x.ru", "col@x.ru"})
            self.assertEqual((book["old@x.ru"].name, book["old@x.ru"].received_count), ("Старый", 2))
            self.assertEqual(book["new@x.ru"].received_count, 2)
            self.assertEqual(book["new@x.ru"].last_ts, 5000)
            self.assertEqual(book["col@x.ru"].sent_count, 1)
        finally:
            db.close()

    def test_imap_sync_collects_senders(self):
        from test_core import FakeIMAP
        fake = FakeIMAP({1: raw_mail("Сервер <ivan@x.ru>", "me@example.com"), 2: raw_mail("Мария <maria@x.ru>", "me@example.com")})
        with mock.patch.object(protocols, "imap_connect", return_value=fake), \
             mock.patch.object(protocols, "get_password", return_value="x"):
            protocols.sync_account(self.db, self.acc)
        self.assertEqual({c.email: c.name for c in self.db.contacts()}, {"ivan@x.ru": "Сервер", "maria@x.ru": "Мария"})

    def test_new_database_needs_no_rebuild(self):
        self.assertTrue(self.db.contacts_ready())


class TestManualEditing(Fixture):
    def test_add_edit_delete_and_restore(self):
        cid = self.db.save_contact(Contact(email=" Boss@Firm.rs ", name="Шеф"))
        with self.assertRaises(ValueError):
            self.db.save_contact(Contact(email="boss@firm.rs"))
        self.add(raw_mail("Директор <boss@firm.rs>", "me@example.com"))
        boss = self.db.contact_by_email("BOSS@firm.rs")
        self.assertEqual((boss.name, boss.received_count), ("Шеф", 1))  # ручное имя не перезаписывается
        boss.name, boss.email = "Шеф Иванов", "chief@firm.rs"
        self.db.save_contact(boss)
        self.assertEqual([(c.name, c.email) for c in self.db.contacts()], [("Шеф Иванов", "chief@firm.rs")])
        self.db.delete_contacts([cid])
        self.assertEqual(self.db.contacts(), [])
        self.assertEqual(self.db.suggest_contacts("chief"), [])
        restored = self.db.save_contact(Contact(email="chief@firm.rs", name="Шеф"))
        self.assertEqual(restored, cid)
        self.assertEqual(self.db.contact_by_email("chief@firm.rs").received_count, 1)

    def test_edit_onto_deleted_address_replaces_it(self):
        old = self.db.save_contact(Contact(email="a@x.ru"))
        self.db.delete_contacts([old])
        other = self.db.save_contact(Contact(email="b@x.ru"))
        self.db.save_contact(Contact(id=other, email="a@x.ru", name="А"))
        self.assertEqual([(c.id, c.email) for c in self.db.contacts()], [(other, "a@x.ru")])

    def test_search_and_suggestions(self):
        self.db.save_contact(Contact(email="ivan.petrov@x.ru", name="Иван Петров"))
        self.add(raw_mail("Пётр Иванов <petr@x.ru>", "me@example.com"))
        self.add(raw_mail("me@example.com", "Ирина <irina@corp.rs>"), folder="sent")
        self.add(raw_mail("Игорь <igor@x.ru>", "me@example.com", date="Fri, 06 Nov 2026 10:00:00 +0300"))
        self.add(raw_mail("percent <p%_x@x.ru>", "me@example.com"))
        names = lambda found: [c.email for c in found]
        # Сначала совпадение с начала имени или адреса, среди них — кому писали сами и ручные.
        self.assertEqual(names(self.db.suggest_contacts("и")), ["irina@corp.rs", "ivan.petrov@x.ru", "igor@x.ru", "petr@x.ru"])
        self.assertEqual(names(self.db.suggest_contacts("ИВАН")), ["ivan.petrov@x.ru", "petr@x.ru"])  # фамилия тоже
        self.assertEqual(names(self.db.suggest_contacts("corp")), ["irina@corp.rs"])  # часть адреса
        self.assertEqual(names(self.db.suggest_contacts("и", exclude=["IRINA@corp.rs"]))[0], "ivan.petrov@x.ru")
        self.assertEqual(names(self.db.suggest_contacts("%_")), ["p%_x@x.ru"])  # спецсимволы LIKE не мешают
        self.assertEqual(self.db.suggest_contacts("  "), [])
        self.assertEqual(len(self.db.suggest_contacts("x", limit=2)), 2)
        self.assertEqual([c.email for c in self.db.contacts("петров")], ["ivan.petrov@x.ru"])


class TestAddressWidgets(Fixture):
    def setUp(self):
        super().setUp()
        self.db.save_contact(Contact(email="ivan@x.ru", name="Иван Петров"))
        self.db.save_contact(Contact(email="p@x.ru", name="Петров, Пётр"))
        self.db.save_contact(Contact(email="anna@x.ru", name="Анна"))

    def test_typing_shows_suggestions_and_enter_inserts(self):
        edit = AddressEdit(self.db)
        edit.show()
        edit.setFocus()
        type_text(edit, "пет")
        popup = edit.completer_.popup()
        self.assertTrue(popup.isVisible())
        shown = [edit.completer_.completionModel().index(i, 0).data() for i in range(edit.completer_.completionModel().rowCount())]
        self.assertEqual(sorted(shown), sorted(['Иван Петров <ivan@x.ru>', '"Петров, Пётр" <p@x.ru>']))
        QTest.keyClick(edit, Qt.Key_Down)
        chosen = popup.currentIndex().data()
        QTest.keyClick(edit, Qt.Key_Return)
        self.assertEqual(edit.text(), chosen + ", ")
        self.assertFalse(popup.isVisible())
        type_text(edit, "ан")
        QTest.keyClick(edit, Qt.Key_Tab)
        self.assertEqual(edit.text(), chosen + ", Анна <anna@x.ru>, ")
        self.assertEqual(edit.addresses_text(), chosen + ", Анна <anna@x.ru>")
        # Уже указанный адрес второй раз не подсказывается.
        type_text(edit, "анн")
        self.assertFalse(popup.isVisible())
        self.assertEqual([(n, a) for n, a in parse_addresses(edit.text())][-1], ("Анна", "anna@x.ru"))
        edit.close()

    def test_replacing_address_in_the_middle_keeps_the_rest(self):
        edit = AddressEdit(self.db, "a@b.c, ив, z@z.z")
        edit.setCursorPosition(len("a@b.c, ив"))
        edit.insert_address("Иван Петров <ivan@x.ru>")
        self.assertEqual(edit.text(), "a@b.c, Иван Петров <ivan@x.ru>, z@z.z")
        edit.add_addresses(['"Петров, Пётр" <p@x.ru>', "Иван Петров <ivan@x.ru>"])
        self.assertEqual(edit.text(), 'a@b.c, Иван Петров <ivan@x.ru>, z@z.z, "Петров, Пётр" <p@x.ru>')

    def test_compose_uses_address_fields_and_book_picker(self):
        from mailclient.ui.compose_dialog import ComposeDialog
        dlg = ComposeDialog(self.db, account_id=self.acc.id)
        self.assertIsInstance(dlg.to, AddressEdit)
        dlg.to.setText("Анна <anna@x.ru>, ")
        state, em = dlg._composition()
        self.assertEqual(state["to"], "Анна <anna@x.ru>")
        book = AddressBookDialog(self.db, picker=True, parent=dlg)
        fields = {"to": dlg.to, "cc": dlg.cc, "bcc": dlg.bcc}
        book.picked.connect(lambda field, values: fields[field].add_addresses(values))
        for i in range(book.list.topLevelItemCount()):
            item = book.list.topLevelItem(i)
            item.setSelected(item.text(1) in ("p@x.ru", "anna@x.ru"))
        book._pick("cc")
        self.assertEqual(sorted(a for _, a in parse_addresses(dlg.cc.text())), ["anna@x.ru", "p@x.ru"])
        dlg.cc.setText("")
        state, em = dlg._composition()
        msg = message_from_bytes(em.as_bytes(), policy=policy.default)
        self.assertEqual(msg["To"].addresses[0].display_name, "Анна")
        dlg._dirty = False
        dlg._discard()
        book.deleteLater()
        dlg.deleteLater()

    def test_book_dialog_lists_filters_and_deletes(self):
        book = AddressBookDialog(self.db)
        self.assertEqual(book.list.topLevelItemCount(), 3)
        self.assertEqual(book.list.topLevelItem(0).text(0), "Анна")
        book.search.setText("петров")
        self.assertEqual(book.list.topLevelItemCount(), 2)
        book.list.topLevelItem(0).setSelected(True)
        target = book.selected()[0]
        from PySide6.QtWidgets import QMessageBox
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            book._delete()
        self.assertEqual(book.list.topLevelItemCount(), 1)
        self.assertIsNone(self.db.contact_by_email(target.email))
        composed = []
        book.compose_requested.connect(composed.append)
        book.list.topLevelItem(0).setSelected(True)
        book._compose()
        self.assertEqual(len(composed), 1)
        book.deleteLater()


if __name__ == "__main__":
    unittest.main()
