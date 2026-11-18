"""Цитата в ответах из SimpleMail сворачивается в «•••», как в ответах из Gmail."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from mailclient.db import MARK_ANSWERED, MARK_FORWARDED, Account, Database
from mailclient.ui.compose_dialog import ComposeDialog
from mailclient.ui.editor import wrap_quotes
from mailclient.ui.viewer import _MailHTML

HEADER = "18.11.2026 12:55, Petar <p@example.com> написал(а):"


def fold(source):
    p = _MailHTML(fragment=True, fold_quotes=True)
    p.feed(source)
    return p.finish()


class ReplyQuoteCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.db.close)
        self.acc = self.db.account(self.db.save_account(Account(
            name="T", email="me@example.com", username="me", imap_host="i", smtp_host="s")))

    def compose(self, mark=MARK_ANSWERED, **kw):
        kw.setdefault("quote_body_html", "<p>Poštovani,</p><p>u prilogu predračun.</p>"
                                         "<table><tr><td>cell</td></tr></table>")
        dialog = ComposeDialog(self.db, account_id=self.acc.id, to="p@example.com", subject="Re: x",
                               quote_header=HEADER, quote_text="Poštovani,\nu prilogu predračun.",
                               mark_source=[1, mark], **kw)
        self.addCleanup(dialog.close)
        return dialog

    @staticmethod
    def parts(message):
        html = text = ""
        for part in message.walk():
            if part.get_content_type() == "text/html":
                html = part.get_content()
            elif part.get_content_type() == "text/plain":
                text = part.get_content()
        return html, text

    def test_sent_reply_has_standard_quote_that_folds(self):
        dialog = self.compose()
        dialog.editor.edit.textCursor().insertText("Hvala, stiže sutra.")
        state, message = dialog._composition()
        html, text = self.parts(message)
        self.assertIn('<div class="gmail_quote">', html)
        self.assertIn('<blockquote class="gmail_quote"', html)
        self.assertLess(html.index("gmail_quote"), html.index("написал(а)"))
        self.assertLess(html.index("<blockquote"), html.index("Poštovani"))
        self.assertLess(html.index("cell"), html.index("</blockquote></div>"))
        self.assertLess(html.index("Hvala"), html.index("gmail_quote"))
        self.assertEqual(html.count("<table"), html.count("</table>"))
        # В переписке цитата (со строкой «написал(а):») свёрнута, новый текст — нет.
        body = fold(html)
        self.assertEqual(body.count("<details"), 1)
        self.assertLess(body.index("Hvala"), body.index("<details"))
        self.assertLess(body.index("<details"), body.index("написал(а)"))
        self.assertLess(body.index("cell"), body.index("</details>"))
        # Обычный текст: строки цитаты с «> ».
        self.assertIn("\n> Poštovani,\n> u prilogu predračun.", text)
        self.assertIn(HEADER + "\n", text)
        self.assertNotIn("> " + HEADER, text)
        self.assertIn("Hvala, stiže sutra.", text.split(HEADER)[0])
        # В черновике хранится HTML редактора: пометка цитаты переживает повторное открытие.
        self.assertNotIn("gmail_quote", state["html"])
        dialog._dirty = True
        dialog._save_draft()
        reopened = ComposeDialog(self.db, draft_id=dialog._draft_id)
        self.addCleanup(reopened.close)
        html2, _ = self.parts(reopened._composition()[1])
        self.assertIn('<blockquote class="gmail_quote"', html2)

    def test_text_typed_below_the_quote_is_not_quoted(self):
        dialog = self.compose()
        cursor = dialog.editor.edit.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertBlock()
        cursor.insertText("Odgovor ispod citata")
        html, text = self.parts(dialog._composition()[1])
        self.assertGreater(html.index("Odgovor ispod"), html.index("</blockquote></div>"))
        self.assertIn("\nOdgovor ispod citata", text)

    def test_forward_is_not_folded(self):
        dialog = self.compose(mark=MARK_FORWARDED)
        html, text = self.parts(dialog._composition()[1])
        self.assertNotIn("gmail_quote", html)
        self.assertNotIn("\n> ", text)

    def test_plain_mode_quotes_with_marks(self):
        dialog = self.compose()
        dialog.mode.setCurrentIndex(dialog.mode.findData("plain"))
        self.assertIn("> Poštovani,", dialog.editor.to_plain())
        html, text = self.parts(dialog._composition()[1])
        self.assertEqual(html, "")
        self.assertIn("> u prilogu predračun.", text)

    def test_wrap_quotes_without_marks_is_unchanged(self):
        source = "<html><body>\n<p>a</p>\n<p>b</p></body></html>"
        self.assertEqual(wrap_quotes(source), source)


class OldSimpleMailReplyCase(unittest.TestCase):
    """Ответы, отправленные из SimpleMail раньше: цитата — абзацы Qt с полем справа."""
    QT = ('<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0//EN" "http://www.w3.org/TR/REC-html40/strict.dtd">'
          '<html><head><meta name="qrichtext" content="1" /><meta charset="utf-8" /></head><body style="">\n'
          '<p style=" margin-top:12px; margin-left:0px; margin-right:0px;">Druže uplaćeno je</p>\n'
          '<p style=" margin-top:12px; margin-left:0px; margin-right:0px;">-- <br />Andrej</p>\n'
          '<p style=" margin-top:12px; margin-left:0px; margin-right:0px;">{header}</p>\n'
          '<p style=" margin-top:12px; margin-left:6px; margin-right:40px;">Poštovani,<br />u prilogu</p>\n'
          '<table style=" margin-left:6px; margin-right:40px;"><tr><td><p style=" margin-right:0px;">c</p></td></tr></table>\n'
          '<p style=" margin-top:12px; margin-left:46px; margin-right:80px;">On Wed wrote:</p></body></html>')

    def test_old_reply_quote_folds_from_the_attribution(self):
        body = fold(self.QT.format(header="18.11.2026 12:55, Netiks Petar &lt;p@x.rs&gt; piše:"))
        self.assertEqual(body.count("<details"), 1)
        self.assertLess(body.index("Andrej"), body.index("<details"))
        self.assertLess(body.index("<details"), body.index("piše:"))
        self.assertTrue(body.endswith("On Wed wrote:</p></details>"))
        self.assertEqual(body.count("<p"), body.count("</p>"))

    def test_old_forward_and_other_mail_are_not_folded(self):
        forward = "-------- Пересылаемое сообщение --------<br />От: a<br />Тема: b"
        self.assertNotIn("<details", fold(self.QT.format(header=forward)))
        self.assertNotIn("<details", fold(self.QT.format(header="Obična rečenica.")))
        other = self.QT.replace('<meta name="qrichtext" content="1" />', "").format(header="X piše:")
        self.assertNotIn("<details", fold(other))


if __name__ == "__main__":
    unittest.main()
