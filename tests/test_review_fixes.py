"""Исправления по ревью 0.9: кодировки заголовков, безопасные ссылки, блокировки, синхронизация."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mailclient.mailparse import attachments, decode_mime_header, parse_message


class TestEightBitHeaders(unittest.TestCase):
    def raw(self, encoding):
        return ("Subject: Привет мир\r\nFrom: Иван Петров <ivan@example.com>\r\n"
                "To: Пётр <petr@example.com>, x@example.com\r\nContent-Type: text/plain\r\n\r\nтекст").encode(encoding)

    def test_unencoded_headers_in_common_charsets(self):
        for encoding in ("utf-8", "cp1251", "koi8-r"):
            with self.subTest(encoding=encoding):
                m = parse_message(self.raw(encoding))
                self.assertEqual(m.subject, "Привет мир")
                self.assertEqual(m.sender, "Иван Петров <ivan@example.com>")
                self.assertEqual(m.recipients, "Пётр <petr@example.com>, x@example.com")

    def test_rfc2047_spacing_unchanged(self):
        cases = {
            b"=?utf-8?B?0J/RgNC40LLQtdGC?= =?utf-8?B?INC80LjRgA==?= tail": "Привет мир tail",
            b"Re: [list] =?utf-8?Q?=D0=BF=D1=80=D0=B8?= done": "Re: [list] при done",
            b"plain ascii": "plain ascii",
            b"=?x-unknown?Q?abc?=": "abc",
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(parse_message(b"Subject: " + header + b"\r\n\r\nx").subject, expected)

    def test_unencoded_attachment_name(self):
        for encoding in ("utf-8", "cp1251"):
            raw = ("Content-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\nContent-Type: text/plain\r\n\r\nhi\r\n"
                   "--b\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=\"Счёт 1.pdf\"\r\n\r\n"
                   "PDF\r\n--b--\r\n").encode(encoding)
            with self.subTest(encoding=encoding):
                self.assertEqual([name for name, _ in attachments(raw)], ["Счёт 1.pdf"])

    def test_plain_string_passthrough(self):
        self.assertEqual(decode_mime_header("Обычный текст"), "Обычный текст")
        self.assertEqual(decode_mime_header(None), "")


class TestLinkSafety(unittest.TestCase):
    def test_only_web_links_open_externally(self):
        from PySide6.QtCore import QUrl
        from mailclient.ui.viewer import link_action
        expected = {
            "https://example.com/x": "open", "HTTP://example.com": "open",
            "mailto:a@example.com": "mailto",
            "file:///C:/Windows/System32/calc.exe": "block", "ms-msdt:/id PCWDiagnostic": "block",
            "search-ms:query=x": "block", "javascript:alert(1)": "block", "http:///nohost": "block",
            "\\\\server\\share\\x.exe": "block",
        }
        for url, action in expected.items():
            with self.subTest(url=url):
                self.assertEqual(link_action(QUrl(url)), action)

    def test_mailto_parameters(self):
        from PySide6.QtCore import QUrl
        from mailclient.ui.viewer import parse_mailto
        url = QUrl("mailto:a@example.com?subject=%D0%9F%D1%80%D0%B8&cc=c@example.com&body=hi%20there")
        self.assertEqual(parse_mailto(url), {"to": "a@example.com", "cc": "c@example.com",
                                             "subject": "При", "body": "hi there"})


if __name__ == "__main__":
    unittest.main()
