"""Перевод интерфейса, параметры сообщений и сохранение выбранного языка."""
import ast
import os
import re
import string
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QLocale
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QMessageBox

from mailclient import i18n, storage
from mailclient.db import Account, Database, Message
from mailclient.errors import explain
from mailclient.ui import main_window, viewer
from mailclient.ui.account_dialog import AccountEditDialog
from mailclient.ui.compose_dialog import ComposeDialog
from mailclient.ui.header import human_date
from mailclient.ui.signatures_dialog import SignaturesDialog
from mailclient.ui.spam_dialog import SpamDialog


class TestTranslationCatalog(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("ru")

    def test_all_marked_messages_are_translated_and_placeholders_match(self):
        catalog = i18n.catalog("sr_Latn")
        formatter = string.Formatter()
        root = Path(i18n.__file__).parent
        for path in root.rglob("*.py"):
            if "locales" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "tr" and node.args
                        and isinstance(node.args[0], ast.Constant)):
                    text = node.args[0].value
                    if not re.search("[А-Яа-яЁё]", text):
                        continue
                    with self.subTest(path=path.name, text=text):
                        self.assertIn(text, catalog)
        for source, translation in catalog.items():
            with self.subTest(source=source):
                self.assertTrue(translation)
                self.assertIsNone(re.search(r"[\u0400-\u04ff]", translation))
                fields = lambda text: sorted((name, spec, conv) for _, name, spec, conv
                                             in formatter.parse(text) if name is not None)
                self.assertEqual(fields(source), fields(translation))

    def test_language_fallback_and_safe_formatting(self):
        self.assertEqual(i18n.set_language("invalid"), "ru")
        self.assertEqual(i18n.tr("Сохранить"), "Сохранить")
        i18n.set_language("sr_Latn")
        self.assertEqual(i18n.tr("Сохранить"), "Sačuvaj")
        self.assertEqual(i18n.tr("Письмо сохранено: {0}", "{моё письмо}.eml"),
                         "Poruka je sačuvana: {моё письмо}.eml")
        self.assertEqual(i18n.tr("user text {braces}"), "user text {braces}")

    def test_stored_reasons_are_translated_without_changing_rule_text(self):
        i18n.set_language("sr_Latn")
        self.assertEqual(i18n.spam_reason("фильтр ещё не обучен"), "filter još nije obučen")
        self.assertEqual(i18n.spam_reason("байес: 0.95 ≥ 0.90"), "Bajes: 0.95 ≥ 0.90")
        self.assertEqual(i18n.spam_reason("белый список: subject содержит «Проект»"),
                         "lista dozvoljenih: naslov sadrži „Проект“")
        i18n.set_language("ru")
        self.assertEqual(i18n.spam_reason("вручную: спам"), "вручную: спам")

    def test_localized_errors_and_unchanged_storage_names(self):
        i18n.set_language("sr_Latn")
        error = explain(ConnectionRefusedError("test"))
        self.assertEqual(error.summary, "Server je odbio vezu")
        self.assertEqual(error.hint, "Proverite port i tip šifrovanja.")
        self.assertEqual(storage.FOLDER_NAMES["inbox"], "Входящие")
        self.assertEqual(storage.ATTACHMENTS_DIR, "Вложения")

    def test_dates_follow_selected_language(self):
        timestamp = int(datetime(2020, 1, 2, 10, 30).timestamp())
        i18n.set_language("sr_Latn")
        self.assertEqual(human_date(timestamp, full=True), "2 jan 2020, 10:30")
        i18n.set_language("ru")
        self.assertEqual(human_date(timestamp, full=True), "2 янв 2020, 10:30")


class TestStartupLanguage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.addCleanup(self.db.close)
        self.addCleanup(i18n.set_language, "ru")
        self.installed_language = Path(self.tmp.name) / "installer-language.txt"
        for patcher in (
            mock.patch.object(i18n.sys, "frozen", True, create=True),
            mock.patch.object(i18n.sys, "executable", str(Path(self.tmp.name) / "SimpleMail.exe")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_serbian_first_launch_is_persisted_across_installer_language_changes(self):
        self.installed_language.write_text("sr_Latn", encoding="utf-8")
        self.assertEqual(i18n.startup_language(self.db), "sr_Latn")
        self.assertEqual(self.db.get_setting("language"), "sr_Latn")
        self.installed_language.write_text("ru", encoding="utf-8")
        self.assertEqual(i18n.startup_language(self.db), "sr_Latn")

    def test_saved_russian_choice_takes_precedence_over_serbian_setup(self):
        self.db.set_setting("language", "ru")
        self.installed_language.write_text("sr_Latn", encoding="utf-8")
        self.assertEqual(i18n.startup_language(self.db), "ru")
        self.assertEqual(self.db.get_setting("language"), "ru")

    def test_russian_setup_initializes_russian_interface(self):
        self.installed_language.write_text("ru", encoding="utf-8")
        self.assertEqual(i18n.startup_language(self.db), "ru")
        self.assertEqual(self.db.get_setting("language"), "ru")

    def test_missing_unknown_or_unreadable_installer_language_uses_russian(self):
        for content in (None, b"unsupported", b"\xff\xfe"):
            with self.subTest(content=content):
                self.db.set_setting("language", "")
                if content is not None:
                    self.installed_language.write_bytes(content)
                self.assertEqual(i18n.startup_language(self.db), "ru")

    def test_source_launch_does_not_use_an_installer_default(self):
        self.installed_language.write_text("sr_Latn", encoding="utf-8")
        with mock.patch.object(i18n.sys, "frozen", False):
            self.assertEqual(i18n.startup_language(self.db), "ru")

    def test_early_windows_use_installer_language_without_database(self):
        self.installed_language.write_text("sr_Latn", encoding="utf-8")
        missing = Path(self.tmp.name) / "none" / "mail.db"
        self.assertEqual(i18n.early_language(missing), "sr_Latn")
        self.assertFalse(missing.exists())

    def test_early_windows_use_saved_choice_from_any_schema(self):
        self.installed_language.write_text("sr_Latn", encoding="utf-8")
        self.db.set_setting("language", "ru")
        with self.db.conn() as c:
            c.execute("PRAGMA user_version = 0")
        self.assertEqual(i18n.early_language(self.db.path), "ru")
        self.db.set_setting("language", "")
        self.assertEqual(i18n.early_language(self.db.path), "sr_Latn")

    def test_early_windows_survive_unreadable_database(self):
        self.installed_language.write_text("sr_Latn", encoding="utf-8")
        broken = Path(self.tmp.name) / "broken.db"
        broken.write_bytes(b"not a database")
        self.assertEqual(i18n.early_language(broken), "sr_Latn")
        self.assertEqual(broken.read_bytes(), b"not a database")


class TestSerbianUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        i18n.set_language("sr_Latn")
        i18n.install_qt_translations(self.app)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.aid = self.db.save_account(Account(name="Moj nalog", email="test@example.com",
                                                enabled=False, storage_dir=self.tmp.name))
        self.windows = []

    def tearDown(self):
        for window in reversed(self.windows):
            window.close()
        self.app.processEvents()
        self.db.close()
        self.tmp.cleanup()
        i18n.set_language("ru")
        i18n.install_qt_translations(self.app)

    def main_window(self):
        with mock.patch.object(main_window, "run_task"), mock.patch("mailclient.ui.mail_watch.MailWatcher"), mock.patch.object(viewer, "HAS_WEBENGINE", False):
            window = main_window.MainWindow(self.db)
            self.windows.append(window)
            self.app.processEvents()
        return window

    def test_main_window_menus_headers_folders_and_account_labels(self):
        window = self.main_window()
        self.assertEqual(window.acts["check"].text(), "Proveri poštu")
        self.assertEqual(window.language_menu.title(), "Jezik interfejsa")
        self.assertEqual([a.text() for a in window.language_group.actions()], ["Русский", "Srpski (latinica)"])
        self.assertEqual(window.table.horizontalHeaderItem(3).text(), "Naslov")
        self.assertEqual(window.tree.topLevelItem(0).child(0).text(0), "Primljeno")
        self.assertEqual(window._folder_item(self.aid, "sent").text(0), "Poslato")
        dialog = AccountEditDialog(self.db)
        self.windows.append(dialog)
        labels = [label.text() for label in dialog.findChildren(QLabel)]
        self.assertIn("Korisničko ime:", labels)
        self.assertIn("Lozinka:", labels)
        self.assertNotIn("Potpis:", labels)
        self.assertEqual(dialog.imap_sec.itemText(2), "Bez šifrovanja")
        self.assertEqual(dialog.windowTitle(), "Nalog e-pošte")

    def test_compose_spam_and_signature_dialogs(self):
        compose = ComposeDialog(self.db, subject="Моя тема", body="Текст моего письма")
        spam = SpamDialog(self.db)
        signatures = SignaturesDialog(self.db)
        self.windows.extend([compose, spam, signatures])
        self.assertEqual(compose.windowTitle(), "Моя тема")
        self.assertIn("Текст моего письма", compose.editor.to_plain())
        self.assertEqual(compose.send_act.text(), "Pošalji")
        self.assertEqual(compose.sig.itemText(0), "Bez potpisa")
        self.assertEqual(spam.field.itemText(0), "Pošiljalac")
        self.assertEqual(spam.table.horizontalHeaderItem(2).text(), "Uslov")
        self.assertEqual(signatures.windowTitle(), "Potpisi")
        self.assertEqual(signatures.name.placeholderText(), "Naziv potpisa, na primer „Poslovni“")

    def test_language_choice_persists_and_is_restored(self):
        i18n.set_language("ru")
        i18n.install_qt_translations(self.app)
        window = self.main_window()
        with mock.patch.object(QMessageBox, "information") as message:
            window.language_actions["sr_Latn"].trigger()
            message.assert_called_once()
        self.assertEqual(self.db.get_setting("language"), "sr_Latn")
        second = Database(self.db.path)
        self.assertEqual(i18n.set_language(second.get_setting("language")), "sr_Latn")
        second.close()
        with mock.patch.object(QMessageBox, "information") as message:
            window.language_actions["ru"].trigger()
            message.assert_called_once()
        self.assertEqual(self.db.get_setting("language"), "ru")

    def test_qt_standard_buttons_use_serbian_latin(self):
        self.assertEqual(QLocale().script(), QLocale.LatinScript)
        self.assertEqual(QCoreApplication.translate("QPlatformTheme", "Cancel"), "Otkaži")
        box = QMessageBox()
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
        for button in box.buttons():
            self.assertIsNone(re.search(r"[\u0400-\u04ff]", button.text()))
        self.assertIn("Otkaži", [b.text().replace("&", "") for b in box.buttons()])

    def test_startup_loads_saved_language_before_window_creation(self):
        import main as entrypoint
        self.db.set_setting("language", "sr_Latn")
        i18n.set_language("ru")
        seen = []
        def create_window(db):
            initialize_keyring.assert_called_once_with()
            seen.append(i18n.language())
            return mock.Mock()

        with mock.patch.object(entrypoint, "Database", return_value=self.db), \
                mock.patch("mailclient.passwords.initialize_keyring") as initialize_keyring, \
                mock.patch("mailclient.logs.setup_logging"), \
                mock.patch("mailclient.instance.start_activation_server"), \
                mock.patch.object(entrypoint, "QApplication", return_value=self.app), \
                mock.patch.object(main_window, "MainWindow", side_effect=create_window), \
                mock.patch.object(viewer, "register_schemes"), \
                mock.patch.object(self.app, "exec", return_value=0), \
                mock.patch("sys.argv", ["main.py", "--data", self.tmp.name, "--isolated-profile"]):
            with self.assertRaises(SystemExit) as exited:
                entrypoint.main()
        self.assertEqual(exited.exception.code, 0)
        self.assertEqual(seen, ["sr_Latn"])


if __name__ == "__main__":
    unittest.main()
