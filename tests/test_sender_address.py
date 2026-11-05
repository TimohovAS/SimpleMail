"""Шапка письма показывает адрес ящика отправителя, а не только имя."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from mailclient.db import Message
from mailclient.ui.header import MessageHeader, sender_html


class TestSenderAddress(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def show(self, sender):
        h = MessageHeader()
        h.show_message(Message(account_id=1, subject="s", sender=sender, recipients="me@example.com"), [])
        return h

    def test_name_and_address_are_both_visible(self):
        h = self.show("sales <sales@objexlabs.com>")
        text = h.sender.text()
        self.assertIn("<b>sales</b>", text)
        self.assertIn("&lt;sales@objexlabs.com&gt;", text)

    def test_address_only_or_same_as_name_is_shown_once(self):
        h = self.show("info@prospel.rs")
        self.assertEqual(h.sender.text(), "<b>info@prospel.rs</b>")
        h = self.show('"info@prospel.rs" <INFO@prospel.rs>')
        self.assertEqual(h.sender.text(), "<b>INFO@prospel.rs</b>")

    def test_fake_address_in_name_is_exposed(self):
        text = sender_html("support@bank.rs", "x@evil.example", "#777")
        self.assertIn("support@bank.rs", text)
        self.assertIn("&lt;x@evil.example&gt;", text)

    def test_html_in_name_is_escaped(self):
        text = sender_html("<script>", "a@b.rs", "#777")
        self.assertNotIn("<script>", text)

    def test_context_menu_copies_address(self):
        h = self.show("Иван Петров <ivan@example.com>")
        sent = []
        h.compose_to.connect(sent.append)
        actions = {a.text(): a for a in h.sender_menu().actions() if a.text()}
        actions["Копировать адрес"].trigger()
        self.assertEqual(QGuiApplication.clipboard().text(), "ivan@example.com")
        actions["Копировать имя и адрес"].trigger()
        self.assertIn("ivan@example.com", QGuiApplication.clipboard().text())
        actions["Написать письмо"].trigger()
        self.assertTrue(sent and "ivan@example.com" in sent[0])

    def test_empty_sender(self):
        h = self.show("")
        self.assertIn("(без отправителя)", h.sender.text())
        self.assertTrue(h.sender_menu().isEmpty())


if __name__ == "__main__":
    unittest.main()
