"""Metadata reads must preserve MIME behavior without materializing file payloads."""
import email.message
from dataclasses import asdict
from email.message import EmailMessage
import unittest
from unittest.mock import patch

from mailclient.contacts import raw_addresses
from mailclient.db import Message
from mailclient.mailparse import (attachment_names, attachments, header, parse_headers,
                                  parse_message, message_attachment_names)


class HeaderMetadataTests(unittest.TestCase):
    def test_outer_headers_only_and_large_body_is_not_retained(self):
        raw = (b'To: Person <person@example.test>\r\nX-Spam-Flag: NO\r\n'
               b'Content-Type: multipart/mixed; boundary="body"\r\n\r\n'
               b'--body\r\nX-Spam-Flag: YES\r\nTo: wrong@example.test\r\n\r\n'
               + b'x' * 1024 * 1024 + b'\r\n--body--\r\n')
        parsed = parse_headers(raw)
        self.assertEqual(parsed['X-Spam-Flag'], 'NO')
        self.assertFalse(parsed.is_multipart())
        self.assertLess(len(parsed.as_bytes()), 200)
        self.assertEqual(header(raw, 'X-Spam-Flag'), 'NO')
        self.assertEqual(raw_addresses(raw), [('Person', 'person@example.test')])

    def test_all_supported_line_endings_and_folded_repeated_headers(self):
        for newline in (b'\r\n', b'\n', b'\r'):
            with self.subTest(newline=newline):
                raw = newline.join((b'To: First <first@example.test>,',
                                    b' Second <second@example.test>',
                                    b'To: Third <third@example.test>',
                                    b'X-Spam-Flag: NO', b'', b'To: wrong@example.test'))
                self.assertEqual(raw_addresses(raw), [('First', 'first@example.test'),
                                                       ('Second', 'second@example.test'),
                                                       ('Third', 'third@example.test')])
                self.assertEqual(header(raw, 'X-Spam-Flag'), 'NO')

    def test_mixed_line_endings_and_header_without_body(self):
        raw = b'X-Spam-Flag: YES\r\nTo: User <user@example.test>\n\r\nX-Spam-Flag: NO'
        self.assertEqual(header(raw, 'X-Spam-Flag'), 'YES')
        self.assertEqual(raw_addresses(raw), [('User', 'user@example.test')])
        self.assertEqual(header(b'X-Spam-Flag: NO', 'X-Spam-Flag'), 'NO')
        self.assertEqual(raw_addresses(b''), [])


class AttachmentMetadataTests(unittest.TestCase):
    @staticmethod
    def fixture():
        msg = EmailMessage()
        msg['Subject'] = 'Metadata test'
        msg.set_content('Plain body')
        msg.add_alternative('<p>HTML body<img src="cid:logo"></p>', subtype='html')
        html = msg.get_payload()[1]
        html.add_related(b'pixels', maintype='image', subtype='png', cid='<logo>',
                         filename='logo.png', disposition='inline')
        msg.add_attachment(b'pdf bytes', maintype='application', subtype='pdf', filename='счёт.pdf')
        msg.add_attachment(b'other image', maintype='image', subtype='png', cid='<other>', filename='photo.png')
        msg.add_attachment(b'unnamed', maintype='application', subtype='octet-stream')
        inner = EmailMessage()
        inner['From'] = 'inner@example.test'
        inner.set_content('Nested email')
        inner.add_attachment(b'inner file', maintype='application', subtype='octet-stream', filename='inside.bin')
        msg.add_attachment(inner, filename='forwarded.eml')
        return msg.as_bytes()

    def test_names_match_saved_files_and_inline_and_nested_rules(self):
        raw = self.fixture()
        files = attachments(raw)
        self.assertEqual(attachment_names(raw), [name for name, _ in files])
        self.assertEqual(attachment_names(raw), ['счёт.pdf', 'photo.png', 'attachment-3.bin', 'forwarded.eml'])
        self.assertEqual(files[0][1], b'pdf bytes')
        self.assertIn(b'Nested email', files[-1][1])

    def test_names_do_not_decode_or_serialize_attachment_payloads(self):
        raw = self.fixture()
        original_payload = email.message.Message.get_payload

        def payload(part, i=None, decode=False):
            if decode and part.get_content_disposition() == 'attachment':
                raise AssertionError('Attachment content was decoded for a filename')
            return original_payload(part, i=i, decode=decode)

        with patch.object(email.message.Message, 'get_payload', payload), \
                patch.object(email.message.Message, 'as_bytes', side_effect=AssertionError('Nested email serialized')):
            self.assertEqual(attachment_names(raw), ['счёт.pdf', 'photo.png', 'attachment-3.bin', 'forwarded.eml'])

    def test_empty_plain_message_has_no_attachments(self):
        self.assertEqual(attachment_names(b'Subject: Plain\n\nHello'), [])
        self.assertEqual(attachment_names(b''), [])

    def test_parsed_message_reuses_names_without_retaining_mime_tree(self):
        raw = self.fixture()
        message = parse_message(raw)
        with patch('mailclient.mailparse.email.message_from_bytes', side_effect=AssertionError('MIME parsed twice')):
            self.assertEqual(message_attachment_names(message), attachment_names_expected())
        self.assertNotIn('_attachment_names', asdict(message))
        self.assertIs(message._attachment_names[0], raw)
        self.assertEqual(message._attachment_names[1], tuple(attachment_names_expected()))
        self.assertTrue(all(isinstance(value, str) for value in message._attachment_names[1]))

    def test_raw_replacement_invalidates_names_and_returns_an_independent_list(self):
        message = parse_message(self.fixture())
        first = message_attachment_names(message)
        first.clear()
        self.assertEqual(message_attachment_names(message), attachment_names_expected())
        message.raw = b'Subject: Replacement\n\nNo attachment'
        self.assertEqual(message_attachment_names(message), [])
        with patch('mailclient.mailparse.email.message_from_bytes', side_effect=AssertionError('MIME parsed twice')):
            self.assertEqual(message_attachment_names(message), [])
        manual = Message(raw=self.fixture())
        self.assertEqual(message_attachment_names(manual), attachment_names_expected())


def attachment_names_expected():
    return ['счёт.pdf', 'photo.png', 'attachment-3.bin', 'forwarded.eml']


if __name__ == '__main__':
    unittest.main()
