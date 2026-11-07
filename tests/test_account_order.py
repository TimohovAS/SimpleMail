"""Порядок ящиков сохраняется и меняется кнопками и перетаскиванием."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from tests.ui_tasks import run_local

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QModelIndex, QPoint, QPointF, Qt
from PySide6.QtWidgets import QApplication, QAbstractItemView

from mailclient import i18n
from mailclient.db import Account, Database, Message
from mailclient.ui import main_window, viewer
from mailclient.ui.account_dialog import AccountsDialog


class DatabaseFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.ids = [self.db.save_account(Account(name=f"Account {i}", email=f"a{i}@example.com",
                                                enabled=(i != 1), storage_dir=self.tmp.name))
                    for i in range(3)]

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def order(self, db=None, only_enabled=False):
        return [a.id for a in (db or self.db).accounts(only_enabled=only_enabled)]


class TestAccountOrder(DatabaseFixture):
    def test_old_database_keeps_insertion_order_and_saved_order_survives_restart(self):
        self.assertEqual(self.order(), self.ids)
        wanted = self.ids[::-1]
        self.db.set_account_order(wanted)
        other = Database(self.db.path)
        try:
            self.assertEqual(self.order(other), wanted)
            self.assertEqual(self.order(other, only_enabled=True), [self.ids[2], self.ids[0]])
        finally:
            other.close()

    def test_edit_add_and_delete_keep_order_and_account_links(self):
        message_id = self.db.add_message(Message(account_id=self.ids[0], subject="Test"))
        self.db.set_setting(f"pw:{self.ids[0]}", "test-placeholder")
        self.db.set_account_order(self.ids[::-1])
        account = self.db.account(self.ids[0])
        account.name = "Renamed"
        self.db.save_account(account)
        new_id = self.db.save_account(Account(name="New", storage_dir=self.tmp.name))
        self.assertEqual(self.order(), [self.ids[2], self.ids[1], self.ids[0], new_id])
        self.db.delete_account(self.ids[1])
        self.assertEqual(self.order(), [self.ids[2], self.ids[0], new_id])
        self.assertEqual(self.db.message(message_id).account_id, self.ids[0])
        self.assertEqual(self.db.get_setting(f"pw:{self.ids[0]}"), "test-placeholder")

    def test_invalid_order_does_not_replace_saved_order(self):
        self.db.set_account_order(self.ids[::-1])
        for invalid in (self.ids[:2], [self.ids[0]] * 3, self.ids[:2] + [999]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.db.set_account_order(invalid)
            self.assertEqual(self.order(), self.ids[::-1])

    def test_corrupt_saved_order_falls_back_without_hiding_accounts(self):
        for value in ("not JSON", "{}", '["bad"]', "null"):
            self.db.set_setting("account_order", value)
            self.assertEqual(self.order(), self.ids)


class TestAccountsDialogOrder(DatabaseFixture):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.dialog = AccountsDialog(self.db)

    def tearDown(self):
        self.dialog.close()
        self.app.processEvents()
        i18n.set_language("ru")
        super().tearDown()

    def test_buttons_move_selected_account_and_update_boundaries(self):
        self.assertEqual(self.dialog.up_btn.shortcut().toString(), "Ctrl+Up")
        self.assertEqual(self.dialog.down_btn.shortcut().toString(), "Ctrl+Down")
        self.assertFalse(self.dialog.up_btn.isEnabled())
        self.assertTrue(self.dialog.down_btn.isEnabled())
        self.dialog.down_btn.click()
        self.assertEqual(self.order(), [self.ids[1], self.ids[0], self.ids[2]])
        self.assertEqual(self.dialog._current_id(), self.ids[0])
        self.dialog.down_btn.click()
        self.assertFalse(self.dialog.down_btn.isEnabled())
        self.dialog.up_btn.click()
        self.assertEqual(self.order(), [self.ids[1], self.ids[0], self.ids[2]])
        self.dialog._reload()
        self.assertEqual(self.dialog._current_id(), self.ids[0])

    def test_drag_model_move_persists_and_new_dialog_restores_order(self):
        self.assertEqual(self.dialog.list.dragDropMode(), QAbstractItemView.InternalMove)
        model = self.dialog.list.model()
        self.assertTrue(model.moveRows(QModelIndex(), 2, 1, QModelIndex(), 0))
        self.assertEqual(self.order(), [self.ids[2], self.ids[0], self.ids[1]])
        other = AccountsDialog(self.db)
        try:
            restored = [other.list.item(i).data(Qt.UserRole) for i in range(other.list.count())]
            self.assertEqual(restored, self.order())
        finally:
            other.close()

    def test_empty_list_and_one_account_disable_movement(self):
        for aid in self.ids[:2]:
            self.db.delete_account(aid)
        self.dialog._reload()
        self.assertFalse(self.dialog.up_btn.isEnabled())
        self.assertFalse(self.dialog.down_btn.isEnabled())
        self.db.delete_account(self.ids[2])
        self.dialog._reload()
        self.dialog._move(-1)
        self.assertFalse(self.dialog.up_btn.isEnabled())
        self.assertFalse(self.dialog.down_btn.isEnabled())

    def test_serbian_labels(self):
        i18n.set_language("sr_Latn")
        other = AccountsDialog(self.db)
        try:
            self.assertEqual(other.up_btn.text(), "↑ Gore")
            self.assertEqual(other.down_btn.text(), "↓ Dole")
            self.assertEqual(other.up_btn.toolTip(), "Pomeri nalog gore (Ctrl+↑)")
            self.assertEqual(other.down_btn.toolTip(), "Pomeri nalog dole (Ctrl+↓)")
            self.assertEqual(other.list.toolTip(), "Prevucite naloge mišem da promenite redosled")
        finally:
            other.close()

    def test_main_tree_updates_when_dialog_order_changes(self):
        with mock.patch.object(main_window, "run_task", side_effect=run_local), \
                mock.patch("mailclient.ui.mail_watch.MailWatcher"), \
                mock.patch.object(viewer, "HAS_WEBENGINE", False):
            window = main_window.MainWindow(self.db)
            self.app.processEvents()
            def reorder(dialog):
                dialog.down_btn.click()
                tree_ids = [window.tree.topLevelItem(i).data(0, Qt.UserRole)[0]
                            for i in range(window.tree.topLevelItemCount())]
                self.assertEqual(tree_ids, [self.ids[1], self.ids[0], self.ids[2]])
                return 0
            try:
                with mock.patch.object(AccountsDialog, "exec", new=reorder):
                    window._manage_accounts()
            finally:
                window.close()
                self.app.processEvents()


class TestMainAccountTree(DatabaseFixture):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.patches = [mock.patch.object(main_window, "run_task", side_effect=run_local),
                        mock.patch("mailclient.ui.mail_watch.MailWatcher"),
                        mock.patch.object(viewer, "HAS_WEBENGINE", False)]
        for patch in self.patches:
            patch.start()
        self.window = main_window.MainWindow(self.db)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        for patch in reversed(self.patches):
            patch.stop()
        i18n.set_language("ru")
        super().tearDown()

    def menu_for(self, item):
        menus = []
        class CapturedMenu(main_window.QMenu):
            def exec(self, pos):
                menus.append(self)
        with mock.patch.object(main_window, "QMenu", CapturedMenu):
            self.window._account_menu(self.window.tree.visualItemRect(item).center())
        self.assertEqual(len(menus), 1)
        return menus[0]

    def action(self, menu, text):
        return next(action for action in menu.actions() if action.text() == i18n.tr(text))

    def test_move_targets_right_clicked_account_and_saves_order(self):
        selected_folder = self.window.current
        menu = self.menu_for(self.window.tree.topLevelItem(1))
        self.action(menu, "Вниз").trigger()
        self.assertEqual(self.order(), [self.ids[0], self.ids[2], self.ids[1]])
        tree_ids = [self.window.tree.topLevelItem(i).data(0, Qt.UserRole)[0] for i in range(3)]
        self.assertEqual(tree_ids, self.order())
        self.assertEqual(self.window.current, selected_folder)

    def test_boundaries_and_folders(self):
        first = self.menu_for(self.window.tree.topLevelItem(0))
        last = self.menu_for(self.window.tree.topLevelItem(2))
        self.assertFalse(self.action(first, "Вверх").isEnabled())
        self.assertFalse(self.action(last, "Вниз").isEnabled())
        folder = self.window.tree.topLevelItem(0).child(0)
        with mock.patch.object(main_window, "QMenu") as popup:
            self.window._account_menu(self.window.tree.visualItemRect(folder).center())
            popup.assert_not_called()

    def test_quick_actions_and_properties_use_clicked_account(self):
        menu = self.menu_for(self.window.tree.topLevelItem(2))
        self.window.syncing = 0
        with mock.patch.object(self.window, "check_mail") as check:
            self.action(menu, "Проверить почту").trigger()
            check.assert_called_once_with(manual=True, account_ids={self.ids[2]})
        with mock.patch.object(self.window, "_open_compose") as compose:
            self.action(menu, "Написать письмо").trigger()
            compose.assert_called_once_with(account_id=self.ids[2])
        with mock.patch.object(main_window, "AccountEditDialog") as edit:
            edit.return_value.exec.return_value = 0
            self.action(menu, "Настройки ящика…").trigger()
            self.assertEqual(edit.call_args.args[1].id, self.ids[2])
            edit.return_value.exec.return_value = 1
            with mock.patch.object(self.window.mail_watch, "restart") as restart:
                self.action(menu, "Настройки ящика…").trigger()
                restart.assert_called_once()
            self.assertIn(self.ids[2], self.window._pending_mail)

    def test_enable_checking_and_settings_shortcuts(self):
        menu = self.menu_for(self.window.tree.topLevelItem(1))
        self.assertFalse(self.action(menu, "Проверить почту").isEnabled())
        enabled = self.action(menu, "Проверять этот ящик")
        self.assertFalse(enabled.isChecked())
        with mock.patch.object(self.window.mail_watch, "restart") as restart:
            enabled.trigger()
            restart.assert_called_once()
        self.assertTrue(self.db.account(self.ids[1]).enabled)
        self.assertIn(self.ids[1], self.window._pending_mail)
        settings = self.action(menu, "&Настройки").menu()
        for key in ("accounts", "signatures", "spam_settings", "interval"):
            self.assertIn(self.window.acts[key], settings.actions())
        self.assertIn(self.window.language_menu.menuAction(), settings.actions())
        self.assertEqual(self.order(), self.ids)
        with mock.patch.object(self.window.mail_watch, "restart"):
            enabled.trigger()
        self.assertFalse(self.db.account(self.ids[1]).enabled)
        self.assertNotIn(self.ids[1], self.window._pending_mail)

    def test_check_during_sync_is_queued_and_serbian_menu_is_translated(self):
        self.window._check_account(self.ids[2])
        self.assertIn(self.ids[2], self.window._pending_mail)
        i18n.set_language("sr_Latn")
        menu = self.menu_for(self.window.tree.topLevelItem(0))
        self.assertEqual(self.action(menu, "Настройки ящика…").text(), "Podešavanja naloga…")
        self.assertEqual(self.action(menu, "Вниз").text(), "Dole")

    def drop(self, source, position, external=False):
        tree = self.window.tree
        tree._drag_item = source
        event = mock.Mock()
        event.source.return_value = object() if external else tree
        event.position.return_value = QPointF(position)
        tree.dropEvent(event)
        return event

    def test_drop_before_first_account_is_deferred_and_keeps_selection(self):
        tree = self.window.tree
        selected = self.window.current
        position = tree.visualItemRect(tree.topLevelItem(0)).topLeft() + QPoint(1, 1)
        event = self.drop(tree.topLevelItem(2), position)
        self.assertEqual(self.order(), self.ids)
        event.accept.assert_called_once()
        self.app.processEvents()
        self.assertEqual(self.order(), [self.ids[2], self.ids[0], self.ids[1]])
        self.assertEqual(self.window.current, selected)
        self.assertEqual([tree.topLevelItem(i).data(0, Qt.UserRole)[0] for i in range(3)],
                         self.order())

    def test_drop_on_folder_moves_whole_account_without_nesting(self):
        tree = self.window.tree
        first = tree.topLevelItem(0)
        folder = tree.topLevelItem(2).child(0)
        self.drop(first, tree.visualItemRect(folder).center())
        self.app.processEvents()
        self.assertEqual(self.order(), [self.ids[1], self.ids[2], self.ids[0]])
        for index in range(3):
            account = tree.topLevelItem(index)
            self.assertIsNone(account.parent())
            self.assertGreater(account.childCount(), 0)
            self.assertTrue(all(account.child(i).data(0, Qt.UserRole)[0] == self.order()[index]
                                for i in range(account.childCount())))

    def test_no_op_and_external_drop_do_not_change_order(self):
        tree = self.window.tree
        first = tree.topLevelItem(0)
        position = tree.visualItemRect(first.child(0)).center()
        with mock.patch.object(self.window, "_reorder_accounts") as reorder:
            self.drop(first, position)
            event = self.drop(first, position, external=True)
            self.app.processEvents()
            reorder.assert_not_called()
            event.ignore.assert_called_once()
        self.assertEqual(self.order(), self.ids)

    def test_folder_cannot_start_account_drag_and_reload_cancels_drag(self):
        tree = self.window.tree
        tree._press_item = tree.topLevelItem(0).child(0)
        with mock.patch("mailclient.ui.account_tree.QDrag") as drag:
            tree.startDrag(Qt.MoveAction)
            drag.assert_not_called()
        tree._drag_item = tree.topLevelItem(0)
        self.window.reload_tree()
        self.assertIsNone(tree._press_item)
        self.assertIsNone(tree._drag_item)
        event = self.drop(None, QPoint(1, 1))
        event.ignore.assert_called_once()

    def test_deferred_drop_ignores_outdated_account_list(self):
        tree = self.window.tree
        position = tree.visualItemRect(tree.topLevelItem(2).child(0)).center()
        self.drop(tree.topLevelItem(0), position)
        new_id = self.db.save_account(Account(name="New", storage_dir=self.tmp.name))
        self.app.processEvents()
        self.assertEqual(self.order(), self.ids + [new_id])


if __name__ == "__main__":
    unittest.main()
