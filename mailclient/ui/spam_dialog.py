"""Настройки спам-фильтра: правила и порог."""
from __future__ import annotations

from ..i18n import tr

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..db import Database
from ..spam import MIN_DOCS

KINDS = {"white": "Белый список (не спам)", "black": "Чёрный список (спам)"}
FIELDS = {"from": "Отправитель", "subject": "Тема", "body": "Текст письма"}


class SpamDialog(QDialog):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle(tr("Спам-фильтр"))
        self.resize(620, 440)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([tr("Список"), tr("Поле"), tr("Содержит")])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)

        self.kind = QComboBox()
        for k, v in KINDS.items():
            self.kind.addItem(tr(v), k)
        self.field = QComboBox()
        for k, v in FIELDS.items():
            self.field.addItem(tr(v), k)
        self.pattern = QLineEdit(); self.pattern.setPlaceholderText(tr("например: @partner.ru или «выигрыш»"))
        add = QPushButton(tr("Добавить")); add.clicked.connect(self._add)
        delete = QPushButton(tr("Удалить выбранное")); delete.clicked.connect(self._delete)
        add_row = QHBoxLayout()
        for w in (self.kind, self.field, self.pattern):
            add_row.addWidget(w)
        add_row.addWidget(add)

        self.threshold = QDoubleSpinBox(); self.threshold.setRange(0.5, 0.999); self.threshold.setSingleStep(0.01)
        self.threshold.setDecimals(3)
        self.threshold.setValue(float(db.get_setting("spam_threshold", "0.9")))
        self.threshold.valueChanged.connect(lambda v: db.set_setting("spam_threshold", f"{v:.3f}"))
        s, h = db.get_setting("bayes_spam_docs"), db.get_setting("bayes_ham_docs")
        stats = QLabel(tr("Байесовский фильтр обучен: спам — {0}, не спам — {1} писем. Включается после {2} писем каждого вида.\nОбучение: кнопки «Спам» и «Не спам» в главном окне.", s, h, MIN_DOCS))
        stats.setWordWrap(True)
        form = QFormLayout()
        form.addRow(tr("Порог спама (байес):"), self.threshold)

        close = QPushButton(tr("Закрыть")); close.clicked.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("Правила проверяются первыми. Белый список важнее чёрного.")))
        lay.addWidget(self.table, 1)
        lay.addLayout(add_row)
        lay.addWidget(delete)
        lay.addLayout(form)
        lay.addWidget(stats)
        lay.addWidget(close)
        self._reload()

    def _reload(self):
        rules = self.db.spam_rules()
        self.table.setRowCount(len(rules))
        for i, r in enumerate(rules):
            for j, text in enumerate((tr(KINDS[r["kind"]]), tr(FIELDS[r["field"]]), r["pattern"])):
                it = QTableWidgetItem(text)
                it.setData(256, r["id"])
                self.table.setItem(i, j, it)

    def _add(self):
        p = self.pattern.text().strip()
        if p:
            self.db.add_spam_rule(self.kind.currentData(), self.field.currentData(), p)
            self.pattern.clear()
            self._reload()

    def _delete(self):
        for row in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.db.delete_spam_rule(self.table.item(row, 0).data(256))
        self._reload()
