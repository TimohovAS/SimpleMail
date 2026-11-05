"""Сербская латиница (windows-1250) в заголовках и тексте писем без верной кодировки."""
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mailclient.db import Database
from mailclient.mailparse import attachments, decode_mime_header, parse_message


def fiscal_receipt() -> bytes:
    # Так приходит «Fiskalni račun» от кассовой программы: тема 8-битная без MIME,
    # текст объявлен iso-8859-1, а на деле windows-1250.
    return ("From: \"Maloprodaja - Minor d.o.o.\" <maloprodaja@minor.co.rs>\r\n"
            "Subject: Fiskalni račun\r\n"
            "Mime-Version: 1.0\r\n"
            "Content-Type: multipart/mixed; boundary=b\r\n\r\n"
            "--b\r\nContent-Type: text/plain; charset=iso-8859-1\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
            "Poštovani,\r\n\r\nU prilogu je fiskalni račun.\r\n\r\ns poštovanjem,\r\n"
            "--b\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=\"račun 1.pdf\"\r\n\r\n"
            "PDF\r\n--b--\r\n").encode("cp1250")


class TestSerbianLatin(unittest.TestCase):
    def test_fiscal_receipt(self):
        m = parse_message(fiscal_receipt())
        self.assertEqual(m.subject, "Fiskalni račun")
        self.assertEqual(m.body_text.split(), "Poštovani, U prilogu je fiskalni račun. s poštovanjem,".split())
        self.assertEqual([name for name, _ in attachments(fiscal_receipt())], ["račun 1.pdf"])

    def test_unencoded_headers(self):
        for text in ("Fiskalni račun", "RAČUN BR. 5", "Poštovani, šaljem ponudu", "Žena, đak i Ćirić"):
            with self.subTest(text=text):
                raw = b"Subject: " + text.encode("cp1250") + b"\r\n\r\nx"
                self.assertEqual(parse_message(raw).subject, text)

    def test_russian_unchanged(self):
        # Русские темы по-прежнему читаются как cp1251/koi8-r, а не как латиница.
        for text in ("Счет на оплату", "Новости ир", "Re: Привет мир"):
            for encoding in ("cp1251", "koi8-r"):
                with self.subTest(text=text, encoding=encoding):
                    raw = b"Subject: " + text.encode(encoding) + b"\r\n\r\nx"
                    self.assertEqual(parse_message(raw).subject, text)

    def test_declared_latin1(self):
        self.assertEqual(decode_mime_header("=?iso-8859-1?Q?Fiskalni_ra=E8un?="), "Fiskalni račun")
        self.assertEqual(decode_mime_header("=?windows-1250?Q?Fiskalni_ra=E8un?="), "Fiskalni račun")
        # Настоящий западноевропейский текст не трогаем.
        for text in ("très élevé", "Grüße aus München", "Señor"):
            with self.subTest(text=text):
                raw = ("Subject: x\r\nContent-Type: text/plain; charset=iso-8859-1\r\n\r\n" + text).encode("cp1252")
                self.assertEqual(parse_message(raw).body_text, text)


class TestReparseTool(unittest.TestCase):
    def test_fixes_stored_subject(self):
        spec = importlib.util.spec_from_file_location(
            "reparse_messages", Path(__file__).resolve().parents[1] / "tools" / "reparse_messages.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        with tempfile.TemporaryDirectory() as tmp:
            eml = Path(tmp) / "m.eml"
            eml.write_bytes(fiscal_receipt())
            db = Database(Path(tmp) / "mail.db")
            c = db.conn()
            c.execute("INSERT INTO accounts(id, name, email, username, imap_host, smtp_host) VALUES (1, 'a', 'a@example.com', 'a', 'h', 'h')")
            c.execute("INSERT INTO messages(account_id, subject, sender, body_text, raw_path) VALUES "
                      "(1, 'Fiskalni ra\ufffdun', 'x', 'Po\x9atovani', ?)", (str(eml),))
            c.execute("INSERT INTO messages(account_id, subject, sender, body_text) VALUES (1, 'Счет', 'y', 'ok')")
            c.commit()
            db.close()
            with sqlite3.connect(Path(tmp) / "mail.db") as c:
                c.row_factory = sqlite3.Row
                changes = tool.reparse(c, everything=False)
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0][1]["subject"], "Fiskalni račun")
            c.close()


if __name__ == "__main__":
    unittest.main()
