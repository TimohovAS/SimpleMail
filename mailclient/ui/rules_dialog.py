"""Окно «Правила для входящих»: список правил и редактор одного правила."""
from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from ..db import Database
from ..i18n import tr
from ..rules import FIELDS, MATCH_ALL, MATCH_ANY, Rule, apply_to_inbox, load_rules, save_rules
from .icons import icon
from .worker import run_task

HIDDEN_TARGETS = ("inbox", "drafts", "outbox", "trash", "spam")


def target_folders(db: Database, account_id: int) -> list[tuple[str, str]]:
    """Папки ящика, куда правило может переносить письма: [(серверное имя, название)]."""
    rows = db.server_folders(account_id)
    return [(row["name"], row["display_name"] or row["name"]) for row in rows
            if row["view"] and row["view"] not in HIDDEN_TARGETS]


def describe(db: Database, rule: Rule) -> str:
    """Строка правила в списке: условия → действия."""
    joiner = tr(" или ") if rule.match == MATCH_ANY else tr(" и ")
    conditions = joiner.join(tr("{0} содержит «{1}»", tr(FIELDS[f]).lower(), text)
                             for f, text in rule.conditions if text.strip())
    actions = []
    if rule.move_to:
        folders = dict(target_folders(db, rule.account_id)) if rule.account_id is not None else {}
        actions.append(tr("перенести в «{0}»", folders.get(rule.move_to, rule.move_to)))
    if rule.mark_flagged:
        actions.append(tr("отметить важным"))
    if rule.mark_read:
        actions.append(tr("отметить прочитанным"))
    text = tr("Если {0}: {1}", conditions, ", ".join(actions))
    return f"{rule.name} — {text}" if rule.name else text


class ConditionRow(QWidget):
    def __init__(self, field="from", text="", parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)
        self.field = QComboBox()
        for key, title in FIELDS.items():
            self.field.addItem(tr(title), key)
        self.field.setCurrentIndex(max(0, self.field.findData(field)))
        self.text = QLineEdit(text)
        self.text.setPlaceholderText(tr("например: @partner.rs или «Счёт»"))
        self.remove = QToolButton(); self.remove.setIcon(icon("x")); self.remove.setAutoRaise(True)
        self.remove.setToolTip(tr("Убрать условие"))
        lay.addWidget(self.field)
        lay.addWidget(QLabel(tr("содержит")))
        lay.addWidget(self.text, 1)
        lay.addWidget(self.remove)

    def value(self) -> list[str]:
        return [self.field.currentData(), self.text.text().strip()]


class RuleEditDialog(QDialog):
    def __init__(self, db: Database, rule: Rule | None = None, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle(tr("Правило") if rule else tr("Новое правило"))
        self.resize(560, 0)
        rule = replace(rule) if rule else Rule(conditions=[["from", ""]])
        self.name = QLineEdit(rule.name)
        self.name.setPlaceholderText(tr("необязательно, например «Счета»"))
        self.account = QComboBox()
        self.account.addItem(tr("Все ящики"), None)
        for acc in db.accounts():
            self.account.addItem(acc.email, acc.id)
        self.account.setCurrentIndex(max(0, self.account.findData(rule.account_id)))
        self.match = QComboBox()
        self.match.addItem(tr("все условия"), MATCH_ALL)
        self.match.addItem(tr("любое из условий"), MATCH_ANY)
        self.match.setCurrentIndex(max(0, self.match.findData(rule.match)))
        form = QFormLayout()
        form.addRow(tr("Название:"), self.name)
        form.addRow(tr("Ящик:"), self.account)
        form.addRow(tr("Выполняется:"), self.match)

        conditions = QGroupBox(tr("Условия"))
        self.rows_layout = QVBoxLayout(conditions)
        self.rows: list[ConditionRow] = []
        add_condition = QPushButton(icon("plus"), tr("Добавить условие"))
        add_condition.clicked.connect(lambda: self._add_row().text.setFocus())
        self.rows_layout.addWidget(add_condition, 0, Qt.AlignLeft)
        for field, text in rule.conditions or [["from", ""]]:
            self._add_row(field, text)

        actions = QGroupBox(tr("Действия"))
        al = QVBoxLayout(actions)
        move_row = QHBoxLayout()
        self.move = QCheckBox(tr("Перенести в папку:"))
        self.folder = QComboBox()
        self.folder.setMinimumContentsLength(20)
        move_row.addWidget(self.move); move_row.addWidget(self.folder, 1)
        self.folder_hint = QLabel(tr("Папку можно выбрать, только когда правило для одного ящика."))
        self.folder_hint.setProperty("tone", "muted"); self.folder_hint.setWordWrap(True)
        self.flag = QCheckBox(tr("Отметить важным"))
        self.flag.setChecked(rule.mark_flagged)
        self.read = QCheckBox(tr("Отметить прочитанным"))
        self.read.setChecked(rule.mark_read)
        self.stop = QCheckBox(tr("Не применять следующие правила"))
        self.stop.setChecked(rule.stop)
        al.addLayout(move_row); al.addWidget(self.folder_hint)
        al.addWidget(self.flag); al.addWidget(self.read); al.addWidget(self.stop)

        self.enabled = QCheckBox(tr("Правило включено"))
        self.enabled.setChecked(rule.enabled)
        note = QLabel(tr("Правила применяются к новым письмам во «Входящих» при проверке почты. "
                         "Спам правила не трогают."))
        note.setProperty("tone", "muted"); note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(conditions)
        lay.addWidget(actions)
        lay.addWidget(self.enabled)
        lay.addWidget(note)
        lay.addWidget(buttons)

        self._move_to = rule.move_to
        self.move.setChecked(bool(rule.move_to))
        self.account.currentIndexChanged.connect(self._fill_folders)
        self.move.toggled.connect(self._update_folder)
        self._fill_folders()

    def _add_row(self, field="from", text="") -> ConditionRow:
        row = ConditionRow(field, text)
        row.remove.clicked.connect(lambda: self._remove_row(row))
        self.rows.append(row)
        self.rows_layout.insertWidget(len(self.rows) - 1, row)
        self._update_remove_buttons()
        return row

    def _remove_row(self, row: ConditionRow):
        if len(self.rows) > 1:
            self.rows.remove(row)
            row.deleteLater()
            self._update_remove_buttons()

    def _update_remove_buttons(self):
        for row in self.rows:
            row.remove.setEnabled(len(self.rows) > 1)

    def _fill_folders(self):
        current = self.folder.currentData() or self._move_to
        self.folder.clear()
        account_id = self.account.currentData()
        if account_id is not None:
            for name, title in target_folders(self.db, account_id):
                self.folder.addItem(title, name)
            index = self.folder.findData(current)
            if index >= 0:
                self.folder.setCurrentIndex(index)
        self._update_folder()

    def _update_folder(self):
        single = self.account.currentData() is not None
        has_folders = self.folder.count() > 0
        self.move.setEnabled(single and has_folders)
        if not (single and has_folders):
            self.move.setChecked(False)
        self.folder.setEnabled(self.move.isChecked())
        self.folder_hint.setText(tr("Папку можно выбрать, только когда правило для одного ящика.") if not single else
                                 tr("В этом ящике пока нет своих папок. Они появятся после проверки почты.")
                                 if not has_folders else "")
        self.folder_hint.setVisible(not (single and has_folders))

    def rule(self) -> Rule:
        return Rule(name=self.name.text().strip(), enabled=self.enabled.isChecked(),
                    account_id=self.account.currentData(), match=self.match.currentData(),
                    conditions=[row.value() for row in self.rows if row.value()[1]],
                    move_to=self.folder.currentData() if self.move.isChecked() else None,
                    mark_flagged=self.flag.isChecked(), mark_read=self.read.isChecked(), stop=self.stop.isChecked())

    def accept(self):
        rule = self.rule()
        if not rule.conditions:
            QMessageBox.warning(self, tr("Правило"), tr("Укажите хотя бы одно условие: что должно содержать письмо."))
            (self.rows[0].text if self.rows else self.name).setFocus()
            return
        if not rule.has_action():
            QMessageBox.warning(self, tr("Правило"), tr("Выберите, что делать с письмом: перенести, отметить важным "
                                                        "или отметить прочитанным."))
            return
        super().accept()


class RulesDialog(QDialog):
    applied = Signal()  # «Применить сейчас» изменило письма — главному окну пора обновить список

    def __init__(self, db: Database, parent=None, new_rule: Rule | None = None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle(tr("Правила для входящих"))
        self.resize(720, 420)
        self.rules = load_rules(db)
        self.list = QListWidget()
        self.list.itemChanged.connect(self._item_changed)
        self.list.itemDoubleClicked.connect(lambda _: self._edit())
        self.list.currentRowChanged.connect(lambda _: self._update_buttons())
        add = QPushButton(icon("plus"), tr("Добавить…")); add.clicked.connect(lambda: self._add())
        self.edit_btn = QPushButton(tr("Изменить…")); self.edit_btn.clicked.connect(self._edit)
        self.del_btn = QPushButton(icon("trash-2"), tr("Удалить")); self.del_btn.clicked.connect(self._delete)
        self.up_btn = QPushButton(tr("Выше")); self.up_btn.clicked.connect(lambda: self._move(-1))
        self.down_btn = QPushButton(tr("Ниже")); self.down_btn.clicked.connect(lambda: self._move(1))
        self.apply_btn = QPushButton(tr("Применить сейчас"))
        self.apply_btn.setToolTip(tr("Применить включённые правила ко всем письмам во «Входящих»"))
        self.apply_btn.clicked.connect(self._apply_now)
        side = QVBoxLayout()
        for button in (add, self.edit_btn, self.del_btn, self.up_btn, self.down_btn):
            side.addWidget(button)
        side.addStretch(1)
        side.addWidget(self.apply_btn)
        top = QHBoxLayout(); top.addWidget(self.list, 1); top.addLayout(side)
        hint = QLabel(tr("Правила проверяются сверху вниз при каждой проверке почты и применяются к новым письмам "
                         "во «Входящих». Флажок слева включает или выключает правило."))
        hint.setProperty("tone", "muted"); hint.setWordWrap(True)
        self.status = QLabel(); self.status.setWordWrap(True); self.status.hide()
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(top, 1)
        lay.addWidget(hint)
        lay.addWidget(self.status)
        lay.addWidget(close)
        self._applying = False
        self._reload()
        if new_rule is not None:
            self._add(new_rule)

    def _reload(self, select: int | None = None):
        with_blocked = self.list.blockSignals(True)
        self.list.clear()
        for rule in self.rules:
            item = QListWidgetItem(describe(self.db, rule))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if rule.enabled else Qt.Unchecked)
            item.setToolTip(item.text())
            self.list.addItem(item)
        self.list.blockSignals(with_blocked)
        if self.rules:
            self.list.setCurrentRow(min(select if select is not None else 0, len(self.rules) - 1))
        self._update_buttons()

    def _save(self, select: int | None = None):
        save_rules(self.db, self.rules)
        self._reload(select)

    def _update_buttons(self):
        row = self.list.currentRow()
        has = 0 <= row < len(self.rules)
        self.edit_btn.setEnabled(has)
        self.del_btn.setEnabled(has)
        self.up_btn.setEnabled(has and row > 0)
        self.down_btn.setEnabled(has and row < len(self.rules) - 1)
        self.apply_btn.setEnabled(not self._applying and any(rule.enabled for rule in self.rules))

    def _item_changed(self, item):
        row = self.list.row(item)
        if 0 <= row < len(self.rules):
            self.rules[row].enabled = item.checkState() == Qt.Checked
            save_rules(self.db, self.rules)
            self._update_buttons()

    def _add(self, template: Rule | None = None):
        dialog = RuleEditDialog(self.db, template, self)
        dialog.setWindowTitle(tr("Новое правило"))
        if dialog.exec() == QDialog.Accepted:
            self.rules.append(dialog.rule())
            self._save(len(self.rules) - 1)

    def _edit(self):
        row = self.list.currentRow()
        if not 0 <= row < len(self.rules):
            return
        dialog = RuleEditDialog(self.db, self.rules[row], self)
        if dialog.exec() == QDialog.Accepted:
            self.rules[row] = dialog.rule()
            self._save(row)

    def _delete(self):
        row = self.list.currentRow()
        if not 0 <= row < len(self.rules):
            return
        if QMessageBox.question(self, tr("Правила для входящих"), tr("Удалить правило?")) != QMessageBox.Yes:
            return
        del self.rules[row]
        self._save(row)

    def _move(self, step: int):
        row = self.list.currentRow()
        target = row + step
        if 0 <= row < len(self.rules) and 0 <= target < len(self.rules):
            self.rules[row], self.rules[target] = self.rules[target], self.rules[row]
            self._save(target)

    def _apply_now(self):
        self._applying = True
        self._update_buttons()
        self.status.setText(tr("Применяю правила к письмам во «Входящих»…"))
        self.status.show()

        def done(result):
            matched, errors = result
            self._applying = False
            self._update_buttons()
            text = tr("Правила применены к письмам: {0}.", matched)
            if errors:
                text += " " + tr("Не получилось: {0}", "; ".join(errors))
            self.status.setText(text)
            if matched:
                self.applied.emit()

        def fail(error):
            self._applying = False
            self._update_buttons()
            self.status.setText(tr("Не удалось применить правила: {0}", error.summary))

        run_task(apply_to_inbox, self.db, on_done=done, on_error=fail)

