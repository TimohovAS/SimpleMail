"""Редактор письма с форматированием (HTML).

Умеет: шрифт и размер, жирный/курсив/подчёркнутый/зачёркнутый, цвет текста и фона,
выравнивание, списки, отступы, верхний/нижний индекс, ссылки, горизонтальная линия,
таблицы, картинки (из файла, перетаскиванием, вставкой из буфера обмена).

Картинки хранятся в письме как встроенные (cid:), поэтому получатель видит их без
загрузки из интернета.
"""
from __future__ import annotations

from ..i18n import tr

import html as html_lib
import mimetypes
import re
import uuid
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, QUrl, Signal
from PySide6.QtGui import (
    QAction, QColor, QFont, QImage, QKeySequence, QTextBlockFormat, QTextCharFormat, QTextCursor,
    QTextDocument, QTextImageFormat, QTextListFormat, QTextTableFormat, QTextLength,
)
from PySide6.QtWidgets import (
    QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFontComboBox, QFormLayout,
    QInputDialog, QLineEdit, QMessageBox, QSpinBox, QTextEdit, QToolBar, QVBoxLayout, QWidget,
)

from .icons import icon
from .image_resources import raster_image

FONT_SIZES = ["8", "9", "10", "11", "12", "14", "16", "18", "20", "24", "28", "36", "48"]
IMAGE_FILTER = "Картинки (*.png *.jpg *.jpeg *.gif *.bmp *.webp)"
MAX_IMAGE_WIDTH = 600  # ширина картинки в тексте по умолчанию (оригинал отправляется целиком)
DEFAULT_FONT = QFont("Segoe UI", 10)
CID_RE = re.compile(r"cid:([^\"'\s>)]+)")


class MailTextEdit(QTextEdit):
    """Поле текста: вставка картинок из буфера обмена и перетаскиванием файлов."""
    image_dropped = Signal(bytes, str, str)  # данные, mime, имя

    def loadResource(self, kind, url):
        if kind == QTextDocument.ImageResource and url.scheme() == "cid":
            provider = getattr(self, "image_provider", lambda: {})
            mime, data = provider().get(url.path(), ("", b""))
            return raster_image(mime, data)
        return None

    def canInsertFromMimeData(self, source):
        return source.hasImage() or source.hasUrls() or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source):
        if source.hasImage():
            img = source.imageData()
            if isinstance(img, QImage) and not img.isNull():
                buf = QBuffer(); buf.open(QIODevice.WriteOnly)
                img.save(buf, "PNG")
                self.image_dropped.emit(bytes(buf.data()), "image/png", "image.png")
                return
        if source.hasUrls():
            handled = False
            for url in source.urls():
                path = url.toLocalFile()
                mime = mimetypes.guess_type(path)[0] or ""
                if path and mime.startswith("image/"):
                    self.image_dropped.emit(Path(path).read_bytes(), mime, Path(path).name)
                    handled = True
            if handled:
                return
        super().insertFromMimeData(source)


class RichEditor(QWidget):
    """Редактор с двумя панелями форматирования."""

    def __init__(self, parent=None, allow_images: bool = True):
        super().__init__(parent)
        self.allow_images = allow_images
        self._images: dict[str, tuple[str, bytes]] = {}  # cid -> (mime, данные)

        self.edit = MailTextEdit()
        self.edit.image_provider = lambda: self._images
        self.edit.setAcceptRichText(True)
        self.edit.document().setDefaultFont(DEFAULT_FONT)
        self.edit.image_dropped.connect(self._insert_image_data)
        self.edit.currentCharFormatChanged.connect(self._sync_format)
        self.edit.cursorPositionChanged.connect(self._sync_alignment)

        self.tb1 = QToolBar(); self.tb2 = QToolBar()
        for tb in (self.tb1, self.tb2):
            tb.setIconSize(tb.iconSize() * 0.75)
            tb.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self._build()

        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(0)
        lay.addWidget(self.tb1); lay.addWidget(self.tb2); lay.addWidget(self.edit, 1)

    # ================= панели =================
    def _act(self, tb, icon_name, tip, slot, shortcut=None, checkable=False):
        a = QAction(icon(icon_name, "#374151"), tip, self)
        a.setCheckable(checkable)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
            a.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            tip = f"{tip} ({a.shortcut().toString(QKeySequence.NativeText)})"
        a.setToolTip(tip)
        a.triggered.connect(slot)
        tb.addAction(a)
        return a

    def _build(self):
        e, t1, t2 = self.edit, self.tb1, self.tb2
        self._act(t1, "undo-2", tr("Отменить"), e.undo, "Ctrl+Z")
        self._act(t1, "redo-2", tr("Повторить"), e.redo, "Ctrl+Y")
        t1.addSeparator()
        self.font_box = QFontComboBox(); self.font_box.setMaximumWidth(190)
        self.font_box.setToolTip(tr("Шрифт"))
        self.font_box.currentFontChanged.connect(lambda f: self._merge(font_family=f.family()))
        t1.addWidget(self.font_box)
        self.size_box = QComboBox(); self.size_box.setEditable(True); self.size_box.addItems(FONT_SIZES)
        self.size_box.setMaximumWidth(60); self.size_box.setToolTip(tr("Размер шрифта"))
        self.size_box.textActivated.connect(self._set_size)
        t1.addWidget(self.size_box)
        t1.addSeparator()
        self.a_bold = self._act(t1, "bold", tr("Жирный"), lambda on: self._merge(bold=on), "Ctrl+B", True)
        self.a_italic = self._act(t1, "italic", tr("Курсив"), lambda on: self._merge(italic=on), "Ctrl+I", True)
        self.a_under = self._act(t1, "underline", tr("Подчёркнутый"), lambda on: self._merge(underline=on), "Ctrl+U", True)
        self.a_strike = self._act(t1, "strikethrough", tr("Зачёркнутый"), lambda on: self._merge(strike=on), None, True)
        t1.addSeparator()
        self._act(t1, "baseline", tr("Цвет текста"), self._text_color)
        self._act(t1, "highlighter", tr("Цвет фона текста"), self._bg_color)
        self._act(t1, "remove-formatting", tr("Убрать форматирование"), self._clear_format, "Ctrl+Space")
        t1.addSeparator()
        self.a_left = self._act(t1, "align-left", tr("По левому краю"), lambda: self._align(Qt.AlignLeft), "Ctrl+L", True)
        self.a_center = self._act(t1, "align-center", tr("По центру"), lambda: self._align(Qt.AlignHCenter), "Ctrl+E", True)
        self.a_right = self._act(t1, "align-right", tr("По правому краю"), lambda: self._align(Qt.AlignRight), "Ctrl+R", True)
        self.a_just = self._act(t1, "align-justify", tr("По ширине"), lambda: self._align(Qt.AlignJustify), "Ctrl+J", True)

        self._act(t2, "list", tr("Маркированный список"), lambda: self._list(QTextListFormat.ListDisc))
        self._act(t2, "list-ordered", tr("Нумерованный список"), lambda: self._list(QTextListFormat.ListDecimal))
        self._act(t2, "indent-decrease", tr("Уменьшить отступ"), lambda: self._indent(-1))
        self._act(t2, "indent-increase", tr("Увеличить отступ"), lambda: self._indent(+1))
        t2.addSeparator()
        self._act(t2, "superscript", tr("Верхний индекс"), lambda: self._valign(QTextCharFormat.AlignSuperScript))
        self._act(t2, "subscript", tr("Нижний индекс"), lambda: self._valign(QTextCharFormat.AlignSubScript))
        t2.addSeparator()
        self._act(t2, "link", tr("Вставить ссылку"), self._insert_link, "Ctrl+K")
        if self.allow_images:
            self._act(t2, "image", tr("Вставить картинку"), self._insert_image_file)
        self._act(t2, "table", tr("Вставить таблицу"), self._insert_table)
        self._act(t2, "minus", tr("Горизонтальная линия"), lambda: self.edit.insertHtml("<hr>"))

    # ================= форматирование =================
    def _merge(self, bold=None, italic=None, underline=None, strike=None, font_family=None, size=None,
               color=None, background=None, valign=None):
        f = QTextCharFormat()
        if bold is not None:
            f.setFontWeight(QFont.Bold if bold else QFont.Normal)
        if italic is not None:
            f.setFontItalic(italic)
        if underline is not None:
            f.setFontUnderline(underline)
        if strike is not None:
            f.setFontStrikeOut(strike)
        if font_family:
            f.setFontFamilies([font_family])
        if size:
            f.setFontPointSize(size)
        if color is not None:
            f.setForeground(color)
        if background is not None:
            f.setBackground(background)
        if valign is not None:
            f.setVerticalAlignment(valign)
        self.edit.mergeCurrentCharFormat(f)  # к выделению или к набираемому дальше тексту
        self.edit.setFocus()

    def _set_size(self, text):
        try:
            size = float(text.replace(",", "."))
        except ValueError:
            return
        if 1 <= size <= 200:
            self._merge(size=size)

    def _text_color(self):
        col = QColorDialog.getColor(self.edit.textColor(), self, tr("Цвет текста"))
        if col.isValid():
            self._merge(color=col)

    def _bg_color(self):
        col = QColorDialog.getColor(QColor("#fff59d"), self, tr("Цвет фона текста"))
        if col.isValid():
            self._merge(background=col)

    def _clear_format(self):
        c = self.edit.textCursor()
        f = QTextCharFormat()
        f.setFont(DEFAULT_FONT)
        c.setCharFormat(f)
        self.edit.setCurrentCharFormat(f)

    def _valign(self, align):
        cur = self.edit.currentCharFormat().verticalAlignment()
        self._merge(valign=QTextCharFormat.AlignNormal if cur == align else align)

    def _align(self, a):
        self.edit.setAlignment(a)
        self._sync_alignment()

    def _list(self, style):
        c = self.edit.textCursor()
        current = c.currentList()
        if current and current.format().style() == style:  # повторное нажатие — убрать из списка эти строки
            start, end = c.selectionStart(), c.selectionEnd()
            block = self.edit.document().findBlock(start)
            while block.isValid() and block.position() <= end:
                if block.textList():
                    block.textList().remove(block)
                    bc = QTextCursor(block); bf = bc.blockFormat(); bf.setIndent(0); bc.setBlockFormat(bf)
                block = block.next()
            return
        fmt = QTextListFormat()
        fmt.setStyle(style)
        c.createList(fmt)

    def _indent(self, delta):
        c = self.edit.textCursor()
        lst = c.currentList()
        if lst:
            fmt = lst.format()
            fmt.setIndent(max(1, fmt.indent() + delta))
            lst.setFormat(fmt)
        else:
            bf = c.blockFormat()
            bf.setIndent(max(0, bf.indent() + delta))
            c.setBlockFormat(bf)

    def _sync_format(self, f: QTextCharFormat):
        self.a_bold.setChecked(f.fontWeight() >= QFont.Bold)
        self.a_italic.setChecked(f.fontItalic())
        self.a_under.setChecked(f.fontUnderline())
        self.a_strike.setChecked(f.fontStrikeOut())
        fams = f.fontFamilies()
        self.font_box.blockSignals(True)
        self.font_box.setCurrentFont(QFont(fams[0] if fams else DEFAULT_FONT.family()))
        self.font_box.blockSignals(False)
        size = f.fontPointSize() or DEFAULT_FONT.pointSizeF()
        self.size_box.setEditText(str(int(size)) if size == int(size) else str(size))

    def _sync_alignment(self):
        a = self.edit.alignment()
        self.a_left.setChecked(bool(a & Qt.AlignLeft) or a == Qt.AlignLeading)
        self.a_center.setChecked(bool(a & Qt.AlignHCenter))
        self.a_right.setChecked(bool(a & Qt.AlignRight) or a == Qt.AlignTrailing)
        self.a_just.setChecked(bool(a & Qt.AlignJustify))

    # ================= вставки =================
    def _insert_link(self):
        c = self.edit.textCursor()
        url, ok = QInputDialog.getText(self, tr("Ссылка"), tr("Адрес (https://… или mailto:…):"), text="https://")
        url = url.strip()
        if not ok or url in ("", "https://"):
            return
        if "://" not in url and not url.startswith("mailto:"):
            url = ("mailto:" if "@" in url else "https://") + url
        text = c.selectedText() or url
        self.edit.insertHtml(f'<a href="{html_lib.escape(url, quote=True)}">{html_lib.escape(text)}</a>&nbsp;')

    def _insert_table(self):
        dlg = QDialog(self); dlg.setWindowTitle(tr("Вставить таблицу"))
        rows = QSpinBox(); rows.setRange(1, 100); rows.setValue(3)
        cols = QSpinBox(); cols.setRange(1, 20); cols.setValue(3)
        form = QFormLayout(dlg); form.addRow(tr("Строк:"), rows); form.addRow(tr("Столбцов:"), cols)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Cancel).setText(tr("Отмена"))
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject); form.addRow(bb)
        if not dlg.exec():
            return
        fmt = QTextTableFormat()
        fmt.setBorder(1)
        fmt.setBorderStyle(QTextTableFormat.BorderStyle_Solid)
        fmt.setBorderCollapse(True)
        fmt.setCellPadding(4)
        fmt.setCellSpacing(0)
        fmt.setWidth(QTextLength(QTextLength.PercentageLength, 100))
        self.edit.textCursor().insertTable(rows.value(), cols.value(), fmt)

    def _insert_image_file(self):
        files, _ = QFileDialog.getOpenFileNames(self, tr("Вставить картинку"), "", tr(IMAGE_FILTER))
        for f in files:
            mime = mimetypes.guess_type(f)[0] or "image/png"
            self._insert_image_data(Path(f).read_bytes(), mime, Path(f).name)

    def _insert_image_data(self, data: bytes, mime: str, name: str):
        img = QImage.fromData(QByteArray(data))
        if img.isNull():
            QMessageBox.warning(self, tr("Картинка"), tr("Не удалось открыть картинку «{0}».", name))
            return
        cid = f"{uuid.uuid4().hex[:12]}@simplemail"
        self._images[cid] = (mime, data)
        url = QUrl("cid:" + cid)
        self.edit.document().addResource(QTextDocument.ImageResource, url, img)
        fmt = QTextImageFormat()
        fmt.setName(url.toString())
        if img.width() > MAX_IMAGE_WIDTH:
            fmt.setWidth(MAX_IMAGE_WIDTH)
            fmt.setHeight(img.height() * MAX_IMAGE_WIDTH / img.width())
        self.edit.textCursor().insertImage(fmt)

    # ================= API =================
    @staticmethod
    def plain_from_html(html: str) -> str:
        d = QTextDocument(); d.setHtml(html)
        return d.toPlainText()

    def set_html(self, html: str):
        self.edit.setHtml(html)

    def to_html(self) -> str:
        return self.edit.toHtml()

    def to_plain(self) -> str:
        return self.edit.toPlainText().replace(" ", "\n").replace("￼", "")

    def inline_images(self) -> list[tuple[str, str, bytes]]:
        """Картинки, которые остались в тексте: [(cid, mime, данные)]."""
        used = set(CID_RE.findall(self.to_html()))
        return [(cid, mime, data) for cid, (mime, data) in self._images.items() if cid in used]

    def is_empty(self) -> bool:
        return not self.to_plain().strip() and not self.inline_images()

    def set_formatting_visible(self, on: bool):
        self.tb1.setVisible(on)
        self.tb2.setVisible(on)
