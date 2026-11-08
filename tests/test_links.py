"""Ссылки в письме: target=_blank, адрес под мышью, своё меню по правой кнопке."""
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QElapsedTimer, QPoint, Qt, QUrl
from PySide6.QtWidgets import QApplication, QMenu

from mailclient.ui import viewer


def _recording_menu(menus):
    class Menu(QMenu):
        def exec(self, *args):  # не показывать меню, только запомнить пункты
            menus.append(self)
    return Menu


def _wait(app, condition, ms=15000):
    timer = QElapsedTimer(); timer.start()
    while not condition() and timer.elapsed() < ms:
        app.processEvents()
    return condition()


@unittest.skipUnless(viewer.HAS_WEBENGINE, "нужен Qt WebEngine")
class WebLinksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = viewer.MessageView()
        self.view.resize(600, 300)
        self.view.show()
        self.opened = []
        patcher = mock.patch.object(viewer.QDesktopServices, "openUrl",
                                    side_effect=lambda url: self.opened.append(url.toString()) or True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.view.shutdown)

    def _show(self, body):
        loaded = []
        self.view.web.loadFinished.connect(lambda ok: loaded.append(ok))
        self.view._render(viewer.wrap_html(f"<html><body style='margin:0'>{body}</body></html>"), remote=False)
        self.assertTrue(_wait(self.app, lambda: loaded))

    def test_target_blank_link_opens_in_default_browser(self):
        # кнопки в рассылках (AliExpress и т.п.) — ссылки target="_blank": раньше щелчок пропадал
        self._show('<a href="https://example.com/track" target="_blank" '
                   'style="display:block;width:400px;height:100px">Track packages</a>')
        target = self.view.web.focusProxy()
        from PySide6.QtTest import QTest
        QTest.mouseClick(target, Qt.LeftButton, Qt.NoModifier, QPoint(50, 50))
        self.assertTrue(_wait(self.app, lambda: self.opened))
        _wait(self.app, lambda: False, 300)
        self.assertEqual(self.opened, ["https://example.com/track"])

    def test_new_window_page_opens_only_first_address_through_link_rules(self):
        page = self.view.web.page()
        catcher = page.createWindow(viewer.QWebEnginePage.WebWindowType.WebBrowserTab)
        clicked = viewer.QWebEnginePage.NavigationType.NavigationTypeLinkClicked
        self.assertFalse(catcher.acceptNavigationRequest(QUrl("about:blank"), clicked, True))
        self.assertFalse(catcher.acceptNavigationRequest(QUrl("https://example.com/a"), clicked, True))
        self.assertFalse(catcher.acceptNavigationRequest(QUrl("https://example.com/b"), clicked, True))
        blocked = []
        self.view.link_blocked.connect(blocked.append)
        other = page.createWindow(viewer.QWebEnginePage.WebWindowType.WebBrowserWindow)
        other.acceptNavigationRequest(QUrl("file:///C:/Windows/System32/calc.exe"), clicked, True)
        _wait(self.app, lambda: blocked)
        self.assertEqual(self.opened, ["https://example.com/a"])
        self.assertEqual(len(blocked), 1)

    def test_hovered_link_address_is_reported(self):
        shown = []
        self.view.link_hovered.connect(shown.append)
        self.view._link_hovered("https://example.com/x?a=1")
        self.view._link_hovered("simplemail://page/cmd/order")
        self.view._link_hovered("")
        self.view._link_hovered(QUrl("mailto:a@example.com"))
        self.assertEqual(shown, ["https://example.com/x?a=1", "", "", "mailto:a@example.com"])

    def test_context_menu_has_working_items_only(self):
        request = mock.Mock()
        request.linkUrl.return_value = QUrl("https://example.com/track")
        menus = []
        with mock.patch.object(self.view.web, "lastContextMenuRequest", return_value=request), \
                mock.patch.object(viewer, "QMenu", _recording_menu(menus)):
            self.view._context_menu(QPoint(5, 5))
        texts = [a.text() for a in menus[0].actions() if not a.isSeparator()]
        self.assertEqual(texts, ["Открыть ссылку в браузере", "Копировать адрес ссылки", "Копировать", "Выделить всё"])
        actions = {a.text(): a for a in menus[0].actions()}
        actions["Открыть ссылку в браузере"].trigger()
        actions["Копировать адрес ссылки"].trigger()
        self.assertEqual(self.opened, ["https://example.com/track"])
        self.assertEqual(QApplication.clipboard().text(), "https://example.com/track")

    def test_context_menu_for_mailto_and_plain_text(self):
        request = mock.Mock()
        request.linkUrl.return_value = QUrl("mailto:shop@example.com?subject=Hi")
        menus = []
        with mock.patch.object(self.view.web, "lastContextMenuRequest", return_value=request), \
                mock.patch.object(viewer, "QMenu", _recording_menu(menus)):
            self.view._context_menu(QPoint(5, 5))
            request.linkUrl.return_value = QUrl()
            self.view._context_menu(QPoint(5, 5))
        actions = {a.text(): a for a in menus[0].actions()}
        self.assertIn("Написать письмо", actions)
        actions["Копировать адрес"].trigger()
        self.assertEqual(QApplication.clipboard().text(), "shop@example.com")
        self.assertEqual([a.text() for a in menus[1].actions()], ["Копировать", "Выделить всё"])


class StatusLinkTest(unittest.TestCase):
    """Адрес ссылки под мышью — в строке состояния под областью письма."""
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import tempfile
        from pathlib import Path
        from mailclient.db import Account, Database
        from mailclient.ui import main_window
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.db.save_account(Account(name="A", email="a@example.com", enabled=False, storage_dir=self.tmp.name))
        with mock.patch.object(main_window, "run_task"), mock.patch("mailclient.ui.mail_watch.MailWatcher"), \
                mock.patch.object(viewer, "HAS_WEBENGINE", False):
            self.window = main_window.MainWindow(self.db)
        self.window.resize(1400, 800)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.db.close()
        self.tmp.cleanup()

    def test_hovered_link_shown_under_message_and_hidden_after(self):
        w = self.window
        w.status.showMessage("Проверка завершена. Новых писем: 0")
        long_url = "https://example.com/track?" + "x" * 500
        w.viewer.link_hovered.emit(long_url)
        label = w.link_target
        self.assertTrue(label.isVisible())
        viewer_left = w.status.mapFromGlobal(w.viewer.mapToGlobal(w.viewer.rect().topLeft())).x()
        self.assertEqual(label.x(), viewer_left)
        self.assertLessEqual(label.geometry().right(), w.counts_label.x())
        self.assertTrue(label.text().startswith("https://example.com/"))
        self.assertLess(len(label.text()), len(long_url))  # длинный адрес сокращён, а не растягивает окно
        self.assertEqual(w.status.currentMessage(), "Проверка завершена. Новых писем: 0")
        w.viewer.link_hovered.emit("")
        self.assertFalse(label.isVisible())


if __name__ == "__main__":
    unittest.main()
