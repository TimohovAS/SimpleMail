"""Background WAL writes remain visible with mapped database reads and file growth."""
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from mailclient.db import Account, Database


class MappedReadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'mail.db'
        self.writer = Database(self.path)
        self.addCleanup(self.writer.close)
        account = Account(name='Test', email='test@example.test')
        self.account_id = self.writer.save_account(account)
        self.reader = Database(self.path)
        self.addCleanup(self.reader.close)

    def add_rows(self, count):
        with self.writer.conn() as connection:
            connection.executemany(
                'INSERT INTO messages(account_id,folder,subject,body_text,seen) VALUES(?,?,?,?,?)',
                [(self.account_id, 'inbox', 'Performance fixture', 'body ' * 13000, 0)
                 for _ in range(count)])

    def test_commits_and_checkpoint_growth_are_visible_without_reopening(self):
        self.add_rows(2)
        self.writer.conn().execute('PRAGMA wal_checkpoint(TRUNCATE)')
        initial_size = self.path.stat().st_size
        self.assertEqual(len(self.reader.search_messages(account_id=self.account_id)), 2)

        # The reader's existing mapped connection must pick up pages appended
        # first to WAL, then checkpointed into the growing main database file.
        self.add_rows(64)
        with self.writer.conn() as connection:
            connection.execute('UPDATE messages SET seen=1 WHERE id % 2=0')
        before_checkpoint = self.reader.search_messages(account_id=self.account_id)
        self.assertEqual(len(before_checkpoint), 66)
        self.assertEqual(sum(row['seen'] for row in before_checkpoint), 33)
        self.writer.conn().execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.assertGreater(self.path.stat().st_size, initial_size)
        after_checkpoint = self.reader.search_messages(account_id=self.account_id)
        self.assertEqual([dict(row) for row in before_checkpoint], [dict(row) for row in after_checkpoint])
        self.assertEqual(self.reader.conn().execute('PRAGMA mmap_size').fetchone()[0], 0)

        # A platform/filesystem with mapping disabled must return the same data.
        ordinary = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        self.addCleanup(ordinary.close)
        ordinary.execute('PRAGMA mmap_size=0')
        expected = ordinary.execute('SELECT id,seen FROM messages ORDER BY id DESC').fetchall()
        self.assertEqual([(row['id'], row['seen']) for row in after_checkpoint], expected)

    def test_active_reader_snapshot_survives_concurrent_commit(self):
        self.add_rows(2)
        connection = self.reader.conn()
        connection.execute('BEGIN')
        self.assertEqual(connection.execute('SELECT count(*) FROM messages').fetchone()[0], 2)
        self.add_rows(3)
        self.assertEqual(connection.execute('SELECT count(*) FROM messages').fetchone()[0], 2)
        connection.rollback()
        self.assertEqual(connection.execute('SELECT count(*) FROM messages').fetchone()[0], 5)

    def test_cancelled_query_releases_mapping_and_progress_handler(self):
        with self.writer.conn() as connection:
            connection.executemany(
                'INSERT INTO messages(account_id,folder,subject,seen) VALUES(?,?,?,?)',
                [(self.account_id, 'inbox', 'Fixture', 0) for _ in range(2000)])
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(self.reader.search_messages(account_id=self.account_id, cancel=cancel), [])
        self.assertEqual(self.reader.conn().execute('PRAGMA mmap_size').fetchone()[0], 0)
        self.assertEqual(len(self.reader.search_messages(account_id=self.account_id)), 2000)
        self.assertEqual(self.reader.conn().execute('PRAGMA mmap_size').fetchone()[0], 0)

    def test_compaction_waits_for_mapped_query_then_physically_shrinks_file(self):
        connection = self.writer.conn()
        connection.execute('PRAGMA wal_autocheckpoint=0')
        with connection:
            connection.executemany(
                'INSERT INTO messages(account_id,folder,subject,raw,seen) VALUES(?,?,?,zeroblob(1048576),0)',
                [(self.account_id, 'inbox', 'Fixture') for _ in range(16)])
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
        initial_size = self.path.stat().st_size
        with connection:
            connection.execute('DELETE FROM messages WHERE id>1')
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()

        entered, release, read_finished, close_reader = (threading.Event() for _ in range(4))
        compact_attempted, compacted = threading.Event(), threading.Event()
        errors, mappings = [], []

        def slow_casefold(value):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('Test query was not released')
            return str(value or '').casefold()

        def read():
            try:
                reader_connection = self.reader.conn()
                reader_connection.create_function('casefold', 1, slow_casefold, deterministic=True)
                self.reader.search_messages('absent', account_id=self.account_id)
                mappings.append(reader_connection.execute('PRAGMA mmap_size').fetchone()[0])
                read_finished.set()
                close_reader.wait(10)  # Keep the idle connection open during VACUUM.
            except BaseException as error:
                errors.append(error)
            finally:
                self.reader.close()

        def compact():
            try:
                compact_attempted.set()
                self.writer.compact()
                compacted.set()
            except BaseException as error:
                errors.append(error)
            finally:
                self.writer.close()

        reader_thread = threading.Thread(target=read)
        compact_thread = threading.Thread(target=compact)
        reader_thread.start()
        try:
            self.assertTrue(entered.wait(10))
            compact_thread.start()
            self.assertTrue(compact_attempted.wait(10))
            self.assertFalse(compacted.wait(0.1))
            release.set()
            self.assertTrue(read_finished.wait(10))
            self.assertTrue(compacted.wait(10))
            self.assertEqual(mappings, [0])
            connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
            self.assertLess(self.path.stat().st_size, initial_size // 2)
            self.assertEqual(connection.execute('SELECT count(*),sum(length(raw)) FROM messages').fetchone()[:],
                             (1, 1048576))
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            release.set()
            close_reader.set()
            reader_thread.join(10)
            if compact_thread.ident is not None:
                compact_thread.join(10)
        self.assertFalse(reader_thread.is_alive())
        self.assertFalse(compact_thread.is_alive())
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
