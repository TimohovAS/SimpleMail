"""Headers arrive before bodies; deferred payloads keep identity and survive retries."""
import imaplib
import threading
import time
from unittest import mock

from mailclient import protocols, sync
from mailclient.errors import MailOperationError
from test_complete_features import Fixture, FolderIMAP, raw_mail
from test_development import CondstoreIMAP


class RecordingIMAP(FolderIMAP):
    def __init__(self, boxes):
        super().__init__(boxes)
        self.commands = []
        self.before_body = None
        self.omit_body = False
        self.truncate_body = False

    def uid(self, command, *args):
        self.commands.append((self.selected, command, args))
        if command == "SEARCH" and str(args[-1]).startswith("UID "):
            uid = int(str(args[-1]).split()[1])
            return "OK", [str(uid).encode() if uid in self.boxes[self.selected] else b""]
        if command == "FETCH" and "BODY.PEEK[]" in args[1]:
            if self.before_body:
                self.before_body(self.selected, int(str(args[0]).split(",")[0]))
            if self.omit_body:
                return "OK", []
            status, data = super().uid(command, *args)
            if self.truncate_body:
                data = [(meta, raw[:-1]) if isinstance(item, tuple) else item
                        for item in data for meta, raw in ([item] if isinstance(item, tuple) else [(None, None)])]
            return status, data
        return super().uid(command, *args)


class TestHeaderSync(Fixture):
    def setUp(self):
        super().setUp()
        self.im = RecordingIMAP({"INBOX": {uid: (raw_mail(f"Mail {uid}", f"searchablebody{uid}"), "")
                                            for uid in range(1, 5)},
                                 "Sent Items": {1: (raw_mail("Sent body"), "\\Seen")}})
        # Fixture mocks captured the former object; replace their return value.
        protocols.imap_connect.return_value = self.im

    def headers(self, full_history=False, **kwargs):
        folder = sync.ServerFolder("INBOX", "inbox", "INBOX", set())
        return sync.sync_mailbox(self.db, self.acc, self.im, folder, lambda _: None,
                                 headers_only=True, full_history=full_history, **kwargs)

    def pending(self):
        return self.db.conn().execute("SELECT id,uid FROM messages WHERE body_downloaded=0 ORDER BY uid").fetchall()

    def test_all_folder_headers_exist_before_first_body_and_real_size_is_kept(self):
        observations = []

        def before_first(folder, uid):
            if observations:
                return
            rows = self.db.conn().execute("SELECT * FROM messages").fetchall()
            observations.append(len(rows))
            self.assertEqual(len(rows), 5)
            self.assertTrue(all(not row["body_downloaded"] and not row["raw"] and not row["raw_path"] for row in rows))
            self.assertTrue(all(not row["body_text"] for row in rows))
            self.assertTrue(all(row["size"] == len(self.im.boxes[row["server_folder"]][row["uid"]][0]) for row in rows))

        self.im.before_body = before_first
        changes = []
        self.assertEqual(sync.synchronize(self.db, self.acc, lambda text: changes.append(text)), 5)
        self.assertEqual(observations, [5])
        self.assertTrue(any(getattr(text, "messages_changed", False) for text in changes))
        self.assertFalse(self.pending())
        headers = [args[0] for _, cmd, args in self.im.commands if cmd == "FETCH" and "BODY.PEEK[HEADER]" in args[1]]
        self.assertEqual(headers[0], "4,3,2,1")

    def test_small_local_window_fetches_flags_only_for_known_uids(self):
        raw = raw_mail("Known")
        self.im.boxes["INBOX"] = {5001: (raw, ""), 5003: (raw_mail("Known 2"), "")}
        self.headers()
        self.im.boxes["INBOX"].update({uid: (raw, "") for uid in range(1, 5001)})
        self.im.commands.clear()
        self.headers()
        flags = [args for _, cmd, args in self.im.commands if cmd == "FETCH" and args[1] == "(UID FLAGS)"]
        self.assertEqual(flags, [("5001,5003", "(UID FLAGS)")])

    def test_unchanged_full_flags_do_not_rewrite_messages_or_their_fts_rows(self):
        sync.synchronize(self.db, self.acc)
        connection = self.db.conn()
        # Isolate message/FTS writes from independent folder/settings bookkeeping.
        with mock.patch.object(self.db, "set_setting"), \
                mock.patch.object(self.db, "set_server_count"), \
                mock.patch.object(self.db, "set_folder_state"), \
                mock.patch.object(self.db, "set_folder_marks"):
            before = connection.total_changes
            self.headers()
            self.assertEqual(connection.total_changes, before)
            raw, _ = self.im.boxes["INBOX"][2]
            self.im.boxes["INBOX"][2] = (raw, "\\Seen \\Flagged \\Answered")
            self.headers()
            self.assertGreater(connection.total_changes, before)
        changed = connection.execute("SELECT seen,flagged,marks FROM messages WHERE server_folder='INBOX' AND uid=2").fetchone()
        self.assertEqual(tuple(changed), (1, 1, sync.server_marks((b"\\Answered",))))
        self.assertTrue(self.db.search_messages("searchablebody2"))

    def test_flags_response_does_not_overwrite_local_changes_made_during_fetch(self):
        sync.synchronize(self.db, self.acc)
        mid = self.db.conn().execute("SELECT id FROM messages WHERE server_folder='INBOX' AND uid=2").fetchone()[0]
        original_uid = self.im.uid

        def local_change(command, *args):
            result = original_uid(command, *args)
            if command == "FETCH" and args[1] == "(UID FLAGS)":
                self.db.update_message(mid, seen=1, pending_seen=1, flagged=1, pending_flagged=1, marks=2, pending_marks=2)
            return result

        with mock.patch.object(self.im, "uid", side_effect=local_change):
            self.headers()
        changed = self.db.message(mid, with_raw=False)
        self.assertEqual((changed.seen, changed.pending_seen, changed.flagged, changed.pending_flagged), (1, 1, 1, 1))
        self.assertEqual((changed.marks, changed.pending_marks), (2, 2))

    def test_large_local_history_uses_one_bounded_flags_command(self):
        raw = raw_mail("History")
        self.im.boxes["INBOX"] = {uid: (raw, "") for uid in range(1, 502)}
        self.headers(full_history=True)
        self.im.commands.clear()
        self.headers(full_history=True)
        flags = [args for _, cmd, args in self.im.commands if cmd == "FETCH" and args[1] == "(UID FLAGS)"]
        self.assertEqual(flags, [("1:*", "(UID FLAGS)")])

    def test_progress_is_visible_before_connect_and_folder_discovery(self):
        stages = []
        original_list = self.im.list

        def connect(*args):
            self.assertTrue(stages)
            self.assertIn("подключение", stages[-1])
            return self.im

        def listing(*args):
            self.assertIn("списка папок", stages[-1])
            return original_list(*args)

        with mock.patch.object(protocols, "imap_connect", side_effect=connect), \
                mock.patch.object(self.im, "list", side_effect=listing):
            sync.synchronize(self.db, self.acc, stages.append)

    def test_header_placeholder_not_exported_as_eml_and_not_indexed_as_complete_history(self):
        self.headers()
        rows = self.pending()
        self.assertEqual(len(rows), 4)
        self.assertFalse(list((self.root / "store").rglob("*.eml")))
        message = self.db.message(rows[0]["id"])
        self.assertFalse(message.body_downloaded)
        self.assertEqual(message.raw, b"")
        self.assertEqual(message.body_text, "")
        self.assertFalse(self.db.search_messages("searchablebody1"))
        self.assertFalse(self.db.search_messages()[0]["body_downloaded"])

    def test_opening_message_keeps_id_local_flags_and_caches_attachments_and_search(self):
        self.im.boxes["INBOX"][4] = (raw_mail("Attachment", "uniquefullbody", [("report.txt", b"document")]), "")
        self.headers()
        mid = self.db.conn().execute("SELECT id FROM messages WHERE uid=4").fetchone()[0]
        self.db.update_message(mid, seen=1, pending_seen=1, flagged=1, pending_flagged=1, marks=3, pending_marks=3)
        loaded = sync.fetch_message_body(self.db, mid)
        self.assertEqual(loaded.id, mid)
        self.assertTrue(loaded.body_downloaded)
        self.assertEqual((loaded.seen, loaded.pending_seen, loaded.flagged, loaded.pending_flagged), (True, 1, True, 1))
        self.assertEqual((loaded.marks, loaded.pending_marks), (3, 3))
        self.assertEqual(self.db.attachment_names(mid), ["report.txt"])
        self.assertEqual(self.db.search_messages("uniquefullbody")[0]["id"], mid)
        self.assertTrue(loaded.raw_path)
        count = len(self.im.commands)
        self.assertTrue(sync.fetch_message_body(self.db, mid).body_downloaded)
        self.assertEqual(len(self.im.commands), count)

    def test_incomplete_and_truncated_fetch_retry_without_empty_complete_message(self):
        self.headers()
        mid = self.pending()[0]["id"]
        for field in ("omit_body", "truncate_body"):
            setattr(self.im, field, True)
            with self.assertRaises(imaplib.IMAP4.error):
                sync.fetch_message_body(self.db, mid)
            self.assertFalse(self.db.message(mid).body_downloaded)
            self.assertTrue(self.db.message(mid).server_folder)
            setattr(self.im, field, False)
        self.assertTrue(sync.fetch_message_body(self.db, mid).body_downloaded)

    def test_deleted_uid_and_changed_epoch_preserve_unavailable_header(self):
        self.headers()
        rows = self.pending()
        deleted, recycled = rows[0], rows[1]
        del self.im.boxes["INBOX"][deleted["uid"]]
        with self.assertRaises(MailOperationError):
            sync.fetch_message_body(self.db, deleted["id"])
        self.im.validity["INBOX"] += 1
        with self.assertRaises(MailOperationError):
            sync.fetch_message_body(self.db, recycled["id"])
        for row in (deleted, recycled):
            message = self.db.message(row["id"])
            self.assertEqual(message.folder, "archive")
            self.assertIsNone(message.server_folder)
            self.assertFalse(message.body_downloaded)

    def test_self_consistent_literal_cannot_override_full_rfc822_size(self):
        self.headers()
        row = self.pending()[0]
        full = self.im.boxes["INBOX"][row["uid"]][0]
        short = full[:-10]
        meta = f"1 (UID {row['uid']} RFC822.SIZE {len(full)} BODY[] {{{len(short)}}}".encode()
        with mock.patch.object(self.im, "uid", return_value=("OK", [(meta, short)])):
            with self.assertRaises(imaplib.IMAP4.error):
                sync.fetch_message_body(self.db, row["id"])
        self.assertFalse(self.db.message(row["id"]).body_downloaded)

    def test_cancel_after_headers_resume_without_skipping_lower_uids_or_expanding_limit(self):
        self.db.set_setting("initial_fetch_limit", "3")
        cancel = threading.Event()

        def progress(text):
            if getattr(text, "messages_changed", False):
                cancel.set()

        with mock.patch.object(sync, "HEADER_BATCH", 1):
            self.assertEqual(sync.synchronize(self.db, self.acc, progress, cancel=cancel), 1)
        first = self.pending()[0]
        self.assertEqual(first["uid"], 4)
        self.assertEqual(sync.synchronize(self.db, self.acc), 3)  # remaining 2 inbox + 1 sent
        inbox_uids = {row[0] for row in self.db.conn().execute("SELECT uid FROM messages WHERE folder='inbox'")}
        self.assertEqual(inbox_uids, {2, 3, 4})
        self.assertTrue(self.db.message(first["id"]).body_downloaded)

    def test_condstore_does_not_hide_pending_body_after_restart(self):
        fake = CondstoreIMAP({1: raw_mail("Pending")}, {})
        protocols.imap_connect.return_value = fake
        folder = sync.ServerFolder("INBOX", "inbox", "INBOX", set())
        sync.sync_mailbox(self.db, self.acc, fake, folder, lambda _: None, headers_only=True, condstore=True)
        mid = self.pending()[0]["id"]
        fake.commands.clear()
        self.assertEqual(sync.synchronize(self.db, self.acc), 0)
        self.assertTrue(self.db.message(mid).body_downloaded)
        self.assertTrue(any(cmd[0] == "FETCH" and "BODY.PEEK[]" in cmd[-1] for cmd in fake.commands))

    def test_pause_history_stops_bodies_and_next_normal_sync_keeps_old_history_paused(self):
        self.db.set_setting("initial_fetch_limit", "2")
        self.db.set_setting(f"history:{self.acc.id}", "running")
        pause = threading.Event()

        def progress(text):
            if self.db.conn().execute("SELECT count(*) FROM messages WHERE body_downloaded=1").fetchone()[0] >= 1:
                self.db.set_setting(f"history:{self.acc.id}", "paused")
                pause.set()

        sync.synchronize(self.db, self.acc, progress, full_history=True, history_cancel=pause)
        self.assertEqual(self.db.get_setting(f"history:{self.acc.id}"), "paused")
        sync.synchronize(self.db, self.acc)
        oldest = self.db.conn().execute("SELECT id,body_downloaded FROM messages WHERE server_folder='INBOX' AND uid=1").fetchone()
        self.assertFalse(oldest["body_downloaded"])
        self.assertTrue(sync.fetch_message_body(self.db, oldest["id"]).body_downloaded)

    def test_long_body_download_still_discovers_new_inbox_headers(self):
        introduced = False

        def incoming(folder, uid):
            nonlocal introduced
            if not introduced:
                self.im.boxes["INBOX"][5] = (raw_mail("Fresh arrival"), "")
                introduced = True

        self.im.before_body = incoming
        with mock.patch.object(sync, "HEADER_REFRESH_SECONDS", 0):
            self.assertEqual(sync.synchronize(self.db, self.acc), 6)
        row = self.db.conn().execute("SELECT id,body_downloaded FROM messages WHERE server_folder='INBOX' AND uid=5").fetchone()
        self.assertTrue(row["body_downloaded"])

    def test_completion_does_not_overwrite_manual_spam_choice(self):
        self.headers()
        mid = self.pending()[0]["id"]
        self.db.update_message(mid, is_spam=1, trained_as=1, spam_reason="manual")
        self.assertTrue(sync.fetch_message_body(self.db, mid).is_spam)
        self.assertEqual(self.db.message(mid).spam_reason, "manual")

    def test_cancelled_open_never_marks_body_complete(self):
        self.headers()
        mid = self.pending()[0]["id"]
        cancel = threading.Event()
        self.im.before_body = lambda *_: cancel.set()
        self.assertIsNone(sync.fetch_message_body(self.db, mid, cancel))
        self.assertFalse(self.db.message(mid).body_downloaded)

    def test_open_waiter_preempts_next_background_batch(self):
        # The requested old message is outside the first bounded background batch.
        self.im.boxes["INBOX"] = {uid: (raw_mail(f"Mail {uid}"), "") for uid in range(1, 46)}
        entered, release, foreground_done = threading.Event(), threading.Event(), threading.Event()
        body_order, failures = [], []
        first = True

        def body(folder, uid):
            nonlocal first
            body_order.append((folder, uid))
            if first:
                first = False
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test did not release download")

        self.im.before_body = body

        def background():
            try:
                sync.synchronize(self.db, self.acc)
            except Exception as error:
                failures.append(error)
            finally:
                self.db.close()

        def foreground(mid):
            try:
                sync.fetch_message_body(self.db, mid)
                foreground_done.set()
            except Exception as error:
                failures.append(error)
            finally:
                self.db.close()

        background_thread = threading.Thread(target=background)
        background_thread.start()
        self.assertTrue(entered.wait(5))
        target = self.db.conn().execute("SELECT id FROM messages WHERE server_folder='INBOX' AND uid=1").fetchone()[0]
        foreground_thread = threading.Thread(target=foreground, args=(target,))
        foreground_thread.start()
        deadline = time.monotonic() + 5
        while not self.db.account_lock(self.acc.id).contended() and time.monotonic() < deadline:
            time.sleep(.01)
        release.set()
        background_thread.join(10)
        foreground_thread.join(10)
        self.assertFalse(background_thread.is_alive())
        self.assertFalse(foreground_thread.is_alive())
        self.assertFalse(failures)
        self.assertTrue(foreground_done.is_set())
        self.assertEqual(body_order[1], ("INBOX", 1))

    def test_small_messages_use_bounded_batches_without_extra_size_requests(self):
        self.im.boxes["INBOX"] = {uid: (raw_mail(f"Mail {uid}"), "") for uid in range(1, 46)}
        self.assertEqual(sync.synchronize(self.db, self.acc), 46)
        requests = [args for folder, command, args in self.im.commands
                    if folder == "INBOX" and command == "FETCH" and "BODY.PEEK[]" in args[1]]
        self.assertEqual([len(args[0].split(",")) for args in requests], [20, 20, 5])
        self.assertEqual({int(uid) for args in requests for uid in args[0].split(",")}, set(range(1, 46)))
        self.assertFalse(any(command == "FETCH" and args[1] == "(UID RFC822.SIZE)"
                             for _, command, args in self.im.commands))
        self.assertFalse(self.pending())

    def test_body_batches_respect_cached_byte_limit_and_fetch_large_messages_alone(self):
        self.im.boxes["INBOX"] = {
            1: (raw_mail("Small one", "a"), ""),
            2: (raw_mail("Large", "b", [("large.bin", b"x" * 9000)]), ""),
            3: (raw_mail("Small three", "c"), ""),
            4: (raw_mail("Small four", "d"), ""),
        }
        with mock.patch.object(sync, "BATCH_BYTES", 1500):
            sync.synchronize(self.db, self.acc)
        for folder, command, args in self.im.commands:
            if command != "FETCH" or "BODY.PEEK[]" not in args[1]:
                continue
            uids = list(map(int, args[0].split(",")))
            total = sum(len(self.im.boxes[folder][uid][0]) for uid in uids)
            self.assertTrue(len(uids) == 1 or total <= 1500)
            if folder == "INBOX" and 2 in uids:
                self.assertEqual(uids, [2])
        self.assertFalse(self.pending())

    def test_missing_body_in_batch_keeps_only_that_message_pending_for_retry(self):
        self.headers()
        self.im.omit_uid = 2
        with self.assertRaises(MailOperationError):
            sync.synchronize(self.db, self.acc)
        self.assertEqual([row["uid"] for row in self.pending()], [2])
        self.im.omit_uid = None
        sync.synchronize(self.db, self.acc)
        self.assertFalse(self.pending())

    def test_cancel_inside_received_batch_keeps_remaining_messages_resumable(self):
        cancel = threading.Event()

        def progress(text):
            if getattr(text, "change_kind", None) == "body":
                cancel.set()

        sync.synchronize(self.db, self.acc, progress, cancel=cancel)
        self.assertEqual(len(self.pending()), 4)
        self.assertEqual(self.db.conn().execute("SELECT count(*) FROM messages WHERE body_downloaded=1").fetchone()[0], 1)
        self.im.commands.clear()
        sync.synchronize(self.db, self.acc)
        fetched = [uid for _, command, args in self.im.commands
                   if command == "FETCH" and "BODY.PEEK[]" in args[1] for uid in args[0].split(",")]
        self.assertEqual(len(fetched), 4)
        self.assertFalse(self.pending())

    def test_periodic_inbox_refresh_retains_condstore_without_full_flag_scan(self):
        fake = CondstoreIMAP({uid: raw_mail(f"Mail {uid}") for uid in range(1, 46)}, {})
        protocols.imap_connect.return_value = fake
        with mock.patch.object(sync, "HEADER_REFRESH_SECONDS", 0):
            sync.synchronize(self.db, self.acc)
        self.assertEqual(len([command for command in fake.commands if command[0] == "SEARCH"]), 1)
        self.assertFalse([command for command in fake.commands if command[0] == "FETCH" and command[-1] == "(UID FLAGS)"])
        self.assertEqual(len([command for command in fake.commands if command[0] == "FETCH" and "BODY.PEEK[]" in command[-1]]), 3)

    def test_downloaded_payload_is_not_read_back_from_disk(self):
        with mock.patch.object(sync.storage, "read_eml", side_effect=AssertionError("unexpected .eml reread")):
            sync.synchronize(self.db, self.acc)
        self.assertFalse(self.pending())

    def test_foreground_payload_reuses_response_and_retains_complete_raw(self):
        self.headers()
        row = self.pending()[0]
        with mock.patch.object(sync.storage, "read_eml", side_effect=AssertionError("unexpected .eml reread")):
            message = sync.fetch_message_body(self.db, row["id"])
        self.assertEqual(message.raw, self.im.boxes["INBOX"][row["uid"]][0])
        self.assertTrue(message.raw_path)

    def test_batch_response_order_does_not_mix_message_bodies(self):
        original_uid = self.im.uid

        def reordered(command, *args):
            status, data = original_uid(command, *args)
            if command == "FETCH" and "BODY.PEEK[]" in args[1]:
                data.reverse()
                data.insert(0, (b"1 (UID 99999 BODY[] {5}", b"bogus"))
            return status, data

        with mock.patch.object(self.im, "uid", side_effect=reordered):
            sync.synchronize(self.db, self.acc)
        for row in self.db.conn().execute("SELECT id,server_folder,uid FROM messages"):
            self.assertEqual(self.db.message(row["id"]).raw, self.im.boxes[row["server_folder"]][row["uid"]][0])
