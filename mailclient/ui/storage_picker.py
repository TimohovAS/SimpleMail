"""Выбор общей папки и отдельной папки почтового ящика."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QWidget,
)

from ..i18n import tr
from ..storage import default_storage_dir, safe_name


STORAGE_PARENT_SETTING = "storage_parent"


def existing_directory(path: str) -> str:
    """Start the chooser at an existing ancestor of a not-yet-created path."""
    try:
        candidate = Path(path).expanduser() if path.strip() else Path.home()
        while not candidate.is_dir():
            parent = candidate.parent
            if parent == candidate:
                return str(Path.home())
            candidate = parent
    except (OSError, ValueError, RuntimeError):
        return str(Path.home())
    return str(candidate)


class StoragePicker(QWidget):
    """A parent location with an optional automatically named mailbox subfolder.

    The final path remains editable. Editing it explicitly turns off automatic
    naming, so subsequent address edits cannot undo the user's chosen path.
    Merely opening an existing account never changes its storage location.
    """

    def __init__(self, db, email: str, storage_dir: str = "", parent=None):
        super().__init__(parent)
        self.db = db
        self._email = email.strip()
        self._updating = False
        self._preserve_existing = bool(storage_dir)

        self.base_edit = QLineEdit()
        self.base_edit.setAccessibleName(tr("Разместить в папке"))
        self.base_edit.setToolTip(tr("Выберите диск или общую папку. Подпапка для этого ящика появится автоматически."))
        self.browse_button = QPushButton(tr("Обзор…"))
        self.browse_button.clicked.connect(self.browse)
        self.mailbox_subfolder = QCheckBox(tr("Создать подпапку с адресом ящика"))
        self.path_edit = QLineEdit()
        self.path_edit.setAccessibleName(tr("Полный путь к папке ящика"))
        self.path_edit.setToolTip(tr("Можно изменить полный путь вручную. Он сохранится без изменений."))

        default_parent = str(Path(default_storage_dir(self._email)).parent)
        base = db.get_setting(STORAGE_PARENT_SETTING, "") or default_parent
        auto = True
        if storage_dir:
            final = Path(storage_dir)
            auto = bool(self._email) and final.name.casefold() == safe_name(self._email).casefold()
            base = str(final.parent) if auto else storage_dir
        self.base_edit.setText(base)
        self.mailbox_subfolder.setChecked(auto)
        self.path_edit.setText(storage_dir or self._generated_path())

        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self.base_edit, 1)
        row.addWidget(self.browse_button)
        form.addRow(tr("Разместить в:"), row)
        form.addRow("", self.mailbox_subfolder)
        form.addRow(tr("Папка ящика:"), self.path_edit)

        self.base_edit.textChanged.connect(self._location_changed)
        self.mailbox_subfolder.toggled.connect(self._location_changed)
        self.path_edit.textChanged.connect(self._manual_path)

    def path(self) -> str:
        return self.path_edit.text().strip()

    def set_email(self, email: str) -> None:
        # Typing the already-named mailbox folder into the parent field should
        # behave the same as picking it in the chooser.
        base = Path(self.base_edit.text().strip())
        if (self.mailbox_subfolder.isChecked() and not self._preserve_existing
                and self._email and base.name.casefold() == safe_name(self._email).casefold()):
            with QSignalBlocker(self.base_edit):
                self.base_edit.setText(str(base.parent))
        self._email = email.strip()
        if self.mailbox_subfolder.isChecked() and not self._preserve_existing:
            self._refresh_path()

    def _generated_path(self) -> str:
        base = self.base_edit.text().strip()
        if not base:
            return ""
        path = Path(base)
        try:
            path = path.expanduser()
        except RuntimeError:
            # Incomplete paths are allowed while typing; saving validates them.
            pass
        if self.mailbox_subfolder.isChecked() and self._email:
            name = safe_name(self._email)
            # A user can choose an existing mailbox folder itself, too.
            if path.name.casefold() != name.casefold():
                path /= name
        return str(path)

    def _refresh_path(self) -> None:
        self._updating = True
        try:
            self.path_edit.setText(self._generated_path())
        finally:
            self._updating = False

    def _location_changed(self, *_) -> None:
        if self._updating:
            return
        self._preserve_existing = False
        self._refresh_path()

    def _manual_path(self, value: str) -> None:
        if self._updating:
            return
        self._preserve_existing = False
        self._updating = True
        try:
            self.mailbox_subfolder.setChecked(False)
            self.base_edit.setText(value)
        finally:
            self._updating = False

    def browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, tr("Выберите диск или общую папку для почты"),
            existing_directory(self.base_edit.text()),
        )
        if not folder:
            return
        selected = Path(folder)
        if (self.mailbox_subfolder.isChecked() and self._email
                and selected.name.casefold() == safe_name(self._email).casefold()):
            selected = selected.parent
        # A selection of the same path is still an explicit user choice.
        with QSignalBlocker(self.base_edit):
            self.base_edit.setText(str(selected))
        self._location_changed()

    def remember_parent(self) -> None:
        """Call only after the account was successfully saved."""
        path = self.path()
        if path:
            # Always remember the final parent, including manually entered paths.
            self.db.set_setting(STORAGE_PARENT_SETTING, str(Path(path).parent))
