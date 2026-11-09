"""Быстрый запуск: заставка до загрузки Qt, браузер писем после первой отрисовки окна."""
import os
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from mailclient import passwords, splash
from mailclient.db import Message
from mailclient.ui import viewer


class TestSplashSwitch(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.multiple(splash, _splash=None, _used=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def show(self, argv, *, nt=True, env=None):
        created = []

        class FakeSplash:
            def __init__(self, version):
                created.append(version)

            def close(self):
                created.append("closed")

        with mock.patch.object(splash, "_Splash", FakeSplash), \
                mock.patch.object(splash.os, "name", "nt" if nt else "posix"), \
                mock.patch.dict(os.environ, env or {}, clear=False):
            splash.show(argv)
            splash.close()
        return created

    def test_shown_on_normal_windows_start_and_closed(self):
        from mailclient import __version__
        self.assertEqual(self.show(["SimpleMail.exe"]), [__version__, "closed"])

    def test_not_shown_for_tray_start_installer_restore_or_other_systems(self):
        self.assertEqual(self.show(["SimpleMail.exe", "--minimized"]), [])
        self.assertEqual(self.show(["SimpleMail.exe", "--install-restore", "x"]), [])
        self.assertEqual(self.show(["main.py"], nt=False), [])
        self.assertEqual(self.show(["main.py"], env={"SIMPLEMAIL_NO_SPLASH": "1"}), [])

    def test_shown_once_and_close_is_idempotent(self):
        created = self.show(["SimpleMail.exe"])
        splash.show(["SimpleMail.exe"])
        splash.close()
        splash.close()
        self.assertEqual(created.count("closed"), 1)

    def test_texts_follow_installer_language(self):
        title, hint = splash._text()
        self.assertTrue(title and hint)

    def test_uptime_failure_never_breaks_startup(self):
        with mock.patch.object(splash.os, "name", "nt"), \
                mock.patch("ctypes.WinDLL", side_effect=TypeError("broken"), create=True):
            self.assertIsNone(splash.seconds_since_start())


@unittest.skipUnless(os.name == "nt", "Windows-only splash window")
class TestSplashWindow(unittest.TestCase):
    def test_native_window_appears_and_disappears(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        window = splash._Splash("9.9.9")
        try:
            self.assertTrue(window.hwnd, "splash window was not created")
            self.assertEqual(user32.FindWindowW("SimpleMailSplash", None), window.hwnd)
        finally:
            window.close()
        self.assertFalse(window._thread.is_alive())
        self.assertFalse(user32.FindWindowW("SimpleMailSplash", None))

    def test_process_uptime_for_log(self):
        uptime = splash.seconds_since_start()
        self.assertIsNotNone(uptime)
        self.assertGreater(uptime, 0)
        self.assertLess(uptime, 24 * 3600)


class TestDeferredBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_view(self):
        view = viewer.MessageView(defer_web=True)
        self.addCleanup(view.shutdown)
        return view

    def test_browser_is_not_created_with_the_window(self):
        if not viewer.HAS_WEBENGINE:
            self.skipTest("Qt WebEngine is unavailable")
        with mock.patch.object(viewer.MessageView, "_create_web") as create:
            view = viewer.MessageView(defer_web=True)
        create.assert_not_called()
        self.assertIsNone(view.web)
        self.assertTrue(view._web_available())
        view.shutdown()
        self.assertFalse(view._web_available())

    def test_message_shown_before_browser_is_rendered_once_it_exists(self):
        if not viewer.HAS_WEBENGINE:
            self.skipTest("Qt WebEngine is unavailable")
        view = self.make_view()
        message = Message(id=5, body_html='<p>Hello <img src="https://example.com/a.png"></p>')
        view.show_message(message, allow_remote=True)
        self.assertIsNotNone(view._deferred_render)
        page_html, remote, _ = view._deferred_render
        self.assertIn("Hello", page_html)
        self.assertTrue(remote, "remote images stay allowed while the browser is being created")
        view._create_web()
        self.assertIsNotNone(view.web)
        self.assertIsNone(view._deferred_render)
        self.assertFalse(view._web_pending)
        self.assertTrue(view.interceptor.allow_remote)
        self.assertIs(view.stack.currentWidget(), view.web)
        self.assertEqual(view._msg.id, 5)

    def test_first_paint_starts_browser_creation(self):
        if not viewer.HAS_WEBENGINE:
            self.skipTest("Qt WebEngine is unavailable")
        with mock.patch.object(viewer.MessageView, "_create_web") as create:
            view = viewer.MessageView(defer_web=True)
            view.resize(400, 300)
            view.show()
            deadline = time.monotonic() + 3
            while not create.called and time.monotonic() < deadline:
                self.app.processEvents()
            view.hide()
        create.assert_called()

    def test_shutdown_before_creation_never_starts_browser(self):
        if not viewer.HAS_WEBENGINE:
            self.skipTest("Qt WebEngine is unavailable")
        view = viewer.MessageView(defer_web=True)
        view.shutdown()
        view._create_web()
        self.assertIsNone(view.web)


class TestKeyringInBackground(unittest.TestCase):
    def test_background_initialization_does_not_block_and_holds_the_lock(self):
        started, release = threading.Event(), threading.Event()

        def slow_backend():
            started.set()
            release.wait(5)

        fake = mock.Mock()
        fake.get_keyring.side_effect = slow_backend
        with mock.patch.object(passwords, "keyring", fake):
            begin = time.monotonic()
            passwords.initialize_keyring(background=True)
            self.assertLess(time.monotonic() - begin, 0.5)
            self.assertTrue(started.wait(5))
            # Пароли ждут загрузки хранилища, а не обращаются к нему параллельно.
            self.assertFalse(passwords._keyring_lock.acquire(timeout=0.05))
            release.set()
            self.assertTrue(passwords._keyring_lock.acquire(timeout=5))
            passwords._keyring_lock.release()


if __name__ == "__main__":
    unittest.main()


class TestActivationWhileStarting(unittest.TestCase):
    def test_second_launch_waits_for_copy_busy_creating_browser(self):
        """Повторный запуск сразу после появления окна: первая копия пару секунд занята
        созданием браузера писем и отвечает не сразу — это не «программа не отвечает»."""
        import subprocess
        import sys
        import tempfile
        root = str(__import__("pathlib").Path(__file__).resolve().parents[1])
        server_code = (
            "import sys, time; from PySide6.QtCore import QCoreApplication, QTimer\n"
            "from mailclient.instance import start_activation_server\n"
            "app = QCoreApplication([]); QTimer.singleShot(30000, app.quit)\n"
            "s = start_activation_server(sys.argv[1], lambda: (print('shown', flush=True), app.quit()))\n"
            "print('ready', flush=True); time.sleep(3); app.exec()\n")
        client_code = ("import sys; from PySide6.QtCore import QCoreApplication\n"
                       "from mailclient.instance import notify_running\n"
                       "app = QCoreApplication([])\n"
                       "print(notify_running(sys.argv[1]))\n")
        env = dict(os.environ, PYTHONPATH=root)
        with tempfile.TemporaryDirectory() as data:
            server = subprocess.Popen([sys.executable, "-c", server_code, data], env=env, cwd=root,
                                      stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(server.stdout.readline().strip(), "ready")
                client = subprocess.run([sys.executable, "-c", client_code, data], env=env, cwd=root,
                                        capture_output=True, text=True, timeout=30)
                output, _ = server.communicate(timeout=10)
            finally:
                if server.poll() is None:
                    server.kill()
                    server.communicate()
        self.assertEqual(client.stdout.strip(), "True", client.stderr)
        self.assertIn("shown", output)
