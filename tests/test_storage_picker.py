"""Storage selection preserves explicit paths and proposes mailbox subfolders."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from mailclient.db import Database
from mailclient.ui.storage_picker import StoragePicker, STORAGE_PARENT_SETTING


class TestStoragePicker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "mail.db")
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
        self.db.close()
        self.temp.cleanup()

    def picker(self, email="first@gmail.com", storage_dir=""):
        widget = StoragePicker(self.db, email, storage_dir)
        self.widgets.append(widget)
        return widget

    def test_parent_selection_appends_email_and_tracks_corrections(self):
        widget = self.picker()
        with mock.patch("mailclient.ui.storage_picker.QFileDialog.getExistingDirectory",
                        return_value=str(self.root)):
            widget.browse()
        self.assertEqual(widget.path(), str(self.root / "first@gmail.com"))
        widget.set_email("corrected@gmail.com")
        self.assertEqual(widget.path(), str(self.root / "corrected@gmail.com"))
        self.assertFalse((self.root / "corrected@gmail.com").exists())

    def test_manual_parent_path_and_blank_email(self):
        widget = self.picker("")
        base = self.root / "New mail folder"
        widget.base_edit.setText(str(base))
        self.assertEqual(widget.path(), str(base))
        widget.set_email("first@gmail.com")
        self.assertEqual(widget.path(), str(base / "first@gmail.com"))
        widget.base_edit.clear()
        self.assertEqual(widget.path(), "")

    def test_selecting_existing_named_folder_does_not_nest_it(self):
        widget = self.picker()
        target = self.root / "first@gmail.com"
        target.mkdir()
        with mock.patch("mailclient.ui.storage_picker.QFileDialog.getExistingDirectory",
                        return_value=str(target)):
            widget.browse()
        self.assertEqual(widget.path(), str(target))
        self.assertEqual(widget.base_edit.text(), str(self.root))
        widget.set_email("second@gmail.com")
        self.assertEqual(widget.path(), str(self.root / "second@gmail.com"))

    def test_direct_final_path_edit_disables_automatic_changes(self):
        widget = self.picker()
        final = str(self.root / "My exact folder")
        widget.path_edit.setText(final)
        widget.set_email("second@gmail.com")
        self.assertEqual(widget.path(), final)
        self.assertFalse(widget.mailbox_subfolder.isChecked())

    def test_typed_already_named_parent_tracks_email_without_nesting(self):
        widget = self.picker()
        widget.base_edit.setText(str(self.root / "first@gmail.com"))
        self.assertEqual(widget.path(), str(self.root / "first@gmail.com"))
        widget.set_email("second@gmail.com")
        self.assertEqual(widget.path(), str(self.root / "second@gmail.com"))

    def test_incomplete_home_path_can_be_typed_without_exception(self):
        widget = self.picker()
        with mock.patch.object(Path, "expanduser", side_effect=RuntimeError("Unknown home")):
            widget.base_edit.setText("~unknown-user")
        self.assertEqual(widget.path(), str(Path("~unknown-user") / "first@gmail.com"))

    def test_existing_custom_and_automatic_paths_are_preserved(self):
        for name in ("My archive", "first@gmail.com"):
            with self.subTest(name=name):
                final = str(self.root / name)
                widget = self.picker(storage_dir=final)
                self.assertEqual(widget.path(), final)
                widget.set_email("second@gmail.com")
                self.assertEqual(widget.path(), final)
                widget.base_edit.setText(str(self.root / "Moved"))
                expected = self.root / "Moved"
                if name == "first@gmail.com":
                    expected /= "second@gmail.com"
                self.assertEqual(widget.path(), str(expected))

    def test_exact_folder_can_be_selected_without_subfolder(self):
        widget = self.picker()
        widget.mailbox_subfolder.setChecked(False)
        with mock.patch("mailclient.ui.storage_picker.QFileDialog.getExistingDirectory",
                        return_value=str(self.root)):
            widget.browse()
        self.assertEqual(widget.path(), str(self.root))

    def test_parent_preference_changes_only_on_successful_save(self):
        widget = self.picker()
        widget.base_edit.setText(str(self.root))
        self.assertFalse(self.db.get_setting(STORAGE_PARENT_SETTING))
        widget.remember_parent()
        self.assertEqual(self.db.get_setting(STORAGE_PARENT_SETTING), str(self.root))
        other = self.picker("second@gmail.com")
        self.assertEqual(other.path(), str(self.root / "second@gmail.com"))

    def test_manual_full_path_also_remembers_parent(self):
        widget = self.picker()
        widget.path_edit.setText(str(self.root / "Manual"))
        widget.remember_parent()
        other = self.picker("second@gmail.com")
        self.assertEqual(other.path(), str(self.root / "second@gmail.com"))

    def test_browse_starts_at_existing_ancestor_and_cancel_keeps_path(self):
        widget = self.picker()
        widget.base_edit.setText(str(self.root / "does" / "not" / "exist"))
        before = widget.path()
        with mock.patch("mailclient.ui.storage_picker.QFileDialog.getExistingDirectory",
                        return_value="") as choose:
            widget.browse()
        self.assertEqual(choose.call_args.args[2], str(self.root))
        self.assertEqual(widget.path(), before)

    def test_drive_root_and_unc_paths(self):
        if os.name != "nt":
            self.skipTest("Windows paths")
        widget = self.picker()
        for base in ("D:\\", "\\\\server\\share"):
            with self.subTest(base=base):
                widget.base_edit.setText(base)
                self.assertEqual(widget.path(), str(Path(base) / "first@gmail.com"))


if __name__ == "__main__":
    unittest.main()
