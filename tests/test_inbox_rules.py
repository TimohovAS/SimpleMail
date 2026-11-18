"""Правила для входящих писем: условия, применение при проверке почты и «Применить сейчас»."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mailclient import protocols, rules, sync
from mailclient.db import Account, Message
from mailclient.rules import Rule
from test_complete_features import Fixture, FolderIMAP, raw_mail


def mail(subject, sender="sender@example.com", to="me@example.com", body="Текст"):
    return protocols.build_message(Account(email=sender), to, subject, body).as_bytes()


class MoveIMAP(FolderIMAP):
    """Сервер с MOVE/UIDPLUS и любыми флагами в STORE."""

    def __init__(self, boxes=None):
        super().__init__(boxes)
        self.moves = []

    def capability(self):
        return "OK", [b"IMAP4rev1 MOVE UIDPLUS"]

    def uid(self, cmd, *args):
        box = self.boxes[self.selected]
        if cmd == "MOVE":
            uid, target = int(args[0]), args[1]
            target = (target.decode() if isinstance(target, bytes) else target).strip('"')
            new_uid = max(self.boxes[target], default=0) + 1
            self.boxes[target][new_uid] = box.pop(uid)
            self.moves.append((self.selected, uid, target))
            self.appenduid = f"{self.validity[target]} {uid} {new_uid}".encode()
            return "OK", [b"moved"]
        if cmd == "STORE":
            flag = args[2].strip("()")
            for uid in map(int, args[0].split(",")):
                raw, flags = box[uid]
                flag_set = set(flags.split())
                (flag_set.add if args[1].startswith("+") else flag_set.discard)(flag)
                box[uid] = (raw, " ".join(sorted(flag_set)))
            return "OK", [b"stored"]
        if cmd == "SEARCH" and str(args[-1]).startswith("UID "):
            uid = int(str(args[-1]).split()[1])
            return "OK", [str(uid).encode() if uid in box else b""]
        return super().uid(cmd, *args)


class TestRuleMatching(Fixture):
    def message(self, **fields):
        return Message(**{"sender": "Иван <ivan@partner.rs>", "recipients": "me@example.com",
                          "subject": "Счёт №5", **fields})

    def test_conditions_all_any_and_case(self):
        rule = Rule(conditions=[["from", "PARTNER.RS"], ["subject", "счёт"]], mark_read=True)
        self.assertTrue(rule.matches(self.message()))
        self.assertFalse(rule.matches(self.message(subject="Привет")))
        rule.match = rules.MATCH_ANY
        self.assertTrue(rule.matches(self.message(subject="Привет")))
        self.assertFalse(Rule(conditions=[["to", "  "]], mark_read=True).matches(self.message()))

    def test_actions_combine_and_stop(self):
        first = Rule(account_id=1, conditions=[["from", "partner"]], move_to="Projects", stop=False)
        second = Rule(account_id=1, conditions=[["subject", "Счёт"]], move_to="Other", mark_flagged=True, stop=True)
        third = Rule(conditions=[["subject", "Счёт"]], mark_read=True)
        actions = rules.actions_for([first, second, third], self.message())
        self.assertEqual((actions.move_to, actions.mark_flagged, actions.mark_read), ("Projects", True, False))

    def test_storage_roundtrip_and_broken_setting(self):
        saved = [Rule(name="Счета", account_id=self.acc.id, conditions=[["from", "a@b"]], move_to="Projects"),
                 Rule(conditions=[["subject", "x"]], move_to="Projects", mark_read=True, enabled=False)]
        rules.save_rules(self.db, saved)
        loaded = rules.load_rules(self.db)
        self.assertEqual(loaded[0], saved[0])
        self.assertIsNone(loaded[1].move_to)  # у правила для всех ящиков папки нет
        self.assertEqual(rules.active_rules(self.db, self.acc.id), [saved[0]])
        self.db.set_setting(rules.SETTING, "{испорчено")
        self.assertEqual(rules.load_rules(self.db), [])


class TestRulesOnSync(Fixture):
    def setUp(self):
        super().setUp()
        self.im = MoveIMAP({"INBOX": {1: (mail("Старое от партнёра", "boss@partner.rs"), "")},
                            "Sent Items": {}, "Trash": {}, "Projects": {}})
        protocols.imap_connect.return_value = self.im
        rules.save_rules(self.db, [
            Rule(account_id=self.acc.id, conditions=[["from", "partner.rs"]], move_to="Projects"),
            Rule(conditions=[["subject", "рассылка"]], mark_read=True, mark_flagged=True),
        ])

    def by_subject(self, subject):
        row = self.db.conn().execute("SELECT id FROM messages WHERE subject=?", (subject,)).fetchone()
        return self.db.message(row["id"])

    def test_first_download_does_not_sort_old_mail(self):
        self.assertEqual(sync.synchronize(self.db, self.acc), 1)
        self.assertEqual(self.by_subject("Старое от партнёра").folder, "inbox")
        self.assertFalse(self.im.moves)

    def test_new_mail_is_moved_and_marked(self):
        sync.synchronize(self.db, self.acc)
        self.im.boxes["INBOX"].update({2: (mail("Отчёт", "boss@partner.rs"), ""),
                                       3: (mail("Еженедельная рассылка", "news@shop.rs"), ""),
                                       4: (mail("Просто письмо", "friend@example.com"), "")})
        # Перемещённое и прочитанное правилом — не «новые письма».
        self.assertEqual(sync.synchronize(self.db, self.acc), 1)
        moved = self.by_subject("Отчёт")
        self.assertEqual((moved.folder, moved.server_folder), ("remote:Projects", "Projects"))
        self.assertEqual(self.im.moves, [("INBOX", 2, "Projects")])
        self.assertIn(moved.uid, self.im.boxes["Projects"])
        self.assertTrue(moved.body_downloaded)
        news = self.by_subject("Еженедельная рассылка")
        self.assertEqual((news.folder, news.seen, news.flagged), ("inbox", True, True))
        self.assertIsNone(news.pending_seen)
        self.assertIsNone(news.pending_flagged)
        self.assertEqual(set(self.im.boxes["INBOX"][3][1].split()), {"\\Seen", "\\Flagged"})
        self.assertEqual(self.by_subject("Просто письмо").folder, "inbox")
        self.assertEqual(self.by_subject("Старое от партнёра").folder, "inbox")
        # Следующая проверка ничего не переносит повторно.
        self.im.moves.clear()
        self.assertEqual(sync.synchronize(self.db, self.acc), 0)
        self.assertFalse(self.im.moves)
        self.assertEqual(self.db.message(moved.id).folder, "remote:Projects")

    def test_spam_is_left_to_spam_filter(self):
        sync.synchronize(self.db, self.acc)
        self.db.add_spam_rule("black", "subject", "Выигрыш")
        self.im.boxes["INBOX"][2] = (mail("Выигрыш", "promo@partner.rs"), "")
        sync.synchronize(self.db, self.acc)
        self.assertNotIn(("INBOX", 2, "Projects"), self.im.moves)
        self.assertTrue(self.by_subject("Выигрыш").is_spam)

    def test_rules_of_other_account_and_disabled_rules_are_ignored(self):
        sync.synchronize(self.db, self.acc)
        rules.save_rules(self.db, [
            Rule(account_id=self.acc.id + 100, conditions=[["from", "partner.rs"]], move_to="Projects"),
            Rule(account_id=self.acc.id, conditions=[["from", "partner.rs"]], move_to="Projects", enabled=False),
        ])
        self.im.boxes["INBOX"][2] = (mail("Отчёт", "boss@partner.rs"), "")
        self.assertEqual(sync.synchronize(self.db, self.acc), 1)
        self.assertEqual(self.by_subject("Отчёт").folder, "inbox")
        self.assertFalse(self.im.moves)

    def test_missing_folder_is_reported_but_sync_completes(self):
        sync.synchronize(self.db, self.acc)
        rules.save_rules(self.db, [Rule(account_id=self.acc.id, conditions=[["from", "partner.rs"]], move_to="Нет такой")])
        self.im.boxes["INBOX"][2] = (mail("Отчёт", "boss@partner.rs"), "")
        with self.assertRaisesRegex(Exception, "правила"):
            sync.synchronize(self.db, self.acc)
        self.assertEqual(self.by_subject("Отчёт").folder, "inbox")

    def test_apply_now_sorts_existing_inbox(self):
        sync.synchronize(self.db, self.acc)
        matched, errors = rules.apply_to_inbox(self.db)
        self.assertEqual((matched, errors), (1, []))
        self.assertEqual(self.by_subject("Старое от партнёра").folder, "remote:Projects")
        self.assertEqual(self.im.moves, [("INBOX", 1, "Projects")])


class TestRulesDialog(Fixture):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_editor_builds_rule_and_folder_only_for_one_account(self):
        from mailclient.ui.rules_dialog import RuleEditDialog, RulesDialog, describe
        with self.db.conn() as c:
            c.execute("INSERT INTO folders(account_id,name,view,display_name) VALUES(?,?,?,?)",
                      (self.acc.id, "Projects", "remote:Projects", "Проекты"))
            c.execute("INSERT INTO folders(account_id,name,view,display_name) VALUES(?,?,?,?)",
                      (self.acc.id, "Trash", "trash", "Корзина"))
        dialog = RuleEditDialog(self.db, Rule(account_id=self.acc.id, conditions=[["from", "a@partner.rs"]]))
        self.assertEqual([dialog.folder.itemText(i) for i in range(dialog.folder.count())], ["Проекты"])
        dialog.move.setChecked(True)
        dialog.read.setChecked(True)
        rule = dialog.rule()
        self.assertEqual((rule.move_to, rule.mark_read, rule.conditions), ("Projects", True, [["from", "a@partner.rs"]]))
        self.assertIn("«Проекты»", describe(self.db, rule))
        dialog.account.setCurrentIndex(dialog.account.findData(None))
        self.assertFalse(dialog.move.isEnabled())
        self.assertIsNone(dialog.rule().move_to)

        rules.save_rules(self.db, [rule, Rule(conditions=[["subject", "x"]], mark_read=True)])
        lst = RulesDialog(self.db)
        self.assertEqual(lst.list.count(), 2)
        lst.list.setCurrentRow(1)
        lst._move(-1)
        self.assertEqual(json.loads(self.db.get_setting(rules.SETTING))[0]["conditions"], [["subject", "x"]])
        from PySide6.QtCore import Qt
        lst.list.item(0).setCheckState(Qt.Unchecked)
        self.assertFalse(rules.load_rules(self.db)[0].enabled)
