"""Автозапуск тестируется без записи в настоящий реестр Windows."""
import ctypes
import os
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox
from mailclient import windows_integration as windows
from mailclient.db import Database, Account
from mailclient.ui import main_window, viewer
from mailclient.ui.icons import app_icon, ICON_DIR

REAL_STARTUP_SUPPORTED = windows.startup_supported


class TestWindowsStartup(unittest.TestCase):
    """Ярлык в «Автозагрузке» вместо записи в реестр; реестр — только старые записи."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.shortcut = Path(self.tmp.name) / "Startup" / "SimpleMail.lnk"
        self.registry = mock.MagicMock()
        self.registry.REG_SZ = 1
        self.registry.REG_EXPAND_SZ = 2
        self.registry.KEY_SET_VALUE = 2
        self.registry.KEY_QUERY_VALUE = 1
        self.registry.HKEY_CURRENT_USER = "HKCU"
        self.registry.QueryValueEx.side_effect = FileNotFoundError()  # старой записи нет
        self.written = []

        def write(path, target, arguments):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(target + "\n" + arguments, encoding="utf-8")
            self.written.append((path, target, arguments))

        def read(path):
            target, arguments = path.read_text(encoding="utf-8").split("\n", 1)
            return target, arguments

        for patch in (mock.patch.object(windows, "winreg", self.registry),
                      mock.patch.object(windows, "startup_shortcut", return_value=self.shortcut),
                      mock.patch.object(windows, "startup_supported", return_value=True),
                      mock.patch.object(windows, "_write_shortcut", side_effect=write),
                      mock.patch.object(windows, "_read_shortcut", side_effect=read)):
            patch.start()
            self.addCleanup(patch.stop)

    def _frozen(self, executable):
        return mock.patch.multiple(windows.sys, executable=str(executable), frozen=True, create=True)

    def test_enable_creates_shortcut_and_never_writes_registry(self):
        root = Path(self.tmp.name)
        python = root / "Python Files" / "python.exe"
        python.parent.mkdir()
        windowed = python.with_name("pythonw.exe")
        windowed.touch()
        project = root / "Project Files"
        data = root / "User Mail"
        with mock.patch.object(windows.sys, "executable", str(python)), \
                mock.patch.object(windows.sys, "frozen", False, create=True), \
                mock.patch.object(windows, "PROJECT_DIR", project):
            windows.set_startup_enabled(True, data)
        self.assertEqual(self.written, [(self.shortcut, str(windowed.resolve()),
                                         f'"{project / "main.py"}" --profile-fallback "{data.resolve()}" --minimized')])
        self.registry.SetValueEx.assert_not_called()
        self.registry.CreateKeyEx.assert_not_called()
        self.registry.DeleteValue.assert_not_called()  # старой записи нет — реестр не трогаем
        self.assertTrue(windows.startup_enabled())

    def test_frozen_build_starts_exe_without_python_or_main_script(self):
        executable = Path(self.tmp.name) / "App Files" / "SimpleMail.exe"
        data = Path(self.tmp.name) / "Mail Data"
        with self._frozen(executable):
            target, arguments = windows.startup_launch(data)
        self.assertEqual(target, str(executable.resolve()))
        self.assertEqual(arguments, f'--profile-fallback "{data.resolve()}" --minimized')
        self.assertNotIn("main.py", arguments)

    def test_enable_and_disable_remove_registry_entry_of_previous_versions(self):
        self.registry.QueryValueEx.side_effect = None
        self.registry.QueryValueEx.return_value = ('"C:\\Program Files\\SimpleMail\\SimpleMail.exe" --minimized',
                                                   self.registry.REG_SZ)
        self.assertTrue(windows.startup_enabled())  # включён прежней версией
        windows.set_startup_enabled(True, Path.cwd())
        self.registry.DeleteValue.assert_called_once_with(
            self.registry.OpenKey.return_value.__enter__.return_value, "SimpleMail")
        self.registry.reset_mock()
        windows.set_startup_enabled(False, Path.cwd())
        self.assertFalse(self.shortcut.exists())
        self.registry.DeleteValue.assert_called_once()
        self.registry.SetValueEx.assert_not_called()

    def test_disable_ignores_missing_shortcut_and_entry(self):
        windows.set_startup_enabled(False, Path.cwd())
        self.registry.DeleteValue.assert_not_called()
        self.assertFalse(windows.startup_enabled())

    def test_unavailable_platform(self):
        with mock.patch.object(windows, "startup_supported", return_value=False):
            self.assertFalse(windows.startup_enabled())
            with self.assertRaises(OSError):
                windows.set_startup_enabled(True, Path.cwd())
            with mock.patch.object(windows.sys, "frozen", True, create=True):
                self.assertFalse(windows.refresh_startup_profile(Path.cwd()))
        self.assertEqual(self.written, [])

    def test_supported_only_on_windows(self):
        with mock.patch.object(windows, "os", SimpleNamespace(name="posix")):
            self.assertFalse(REAL_STARTUP_SUPPORTED())
        with mock.patch.object(windows, "os", SimpleNamespace(name="nt")):
            self.assertTrue(REAL_STARTUP_SUPPORTED())

    def test_shortcut_errors_are_reported_to_caller(self):
        with mock.patch.object(windows, "_write_shortcut", side_effect=PermissionError("test")):
            with self.assertRaises(PermissionError):
                windows.set_startup_enabled(True, Path.cwd())
        with mock.patch.object(windows, "startup_shortcut", side_effect=OSError("no folder")):
            self.assertFalse(windows.startup_enabled())
            with self.assertRaises(OSError):
                windows.set_startup_enabled(True, Path.cwd())

    def test_refresh_points_shortcut_to_this_exe_and_active_profile(self):
        executable = Path(self.tmp.name) / "New Install" / "SimpleMail.exe"
        data = Path(self.tmp.name) / "Active Mail"
        for target, arguments in [("D:\\Old Install\\SimpleMail.exe", '--profile-fallback "D:\\Old Mail" --minimized'),
                                  ("D:\\Portable\\SIMPLEMAIL.EXE", "--profile-fallback D:\\OldMail")]:
            with self.subTest(target=target):
                self.shortcut.parent.mkdir(exist_ok=True)
                self.shortcut.write_text(target + "\n" + arguments, encoding="utf-8")
                self.written.clear()
                with self._frozen(executable):
                    self.assertTrue(windows.refresh_startup_profile(data))
                self.assertEqual(self.written, [(self.shortcut, str(executable.resolve()),
                                                 f'--profile-fallback "{data.resolve()}" --minimized')])
        self.registry.SetValueEx.assert_not_called()

    def test_refresh_leaves_current_foreign_and_missing_shortcuts_alone(self):
        executable = Path(self.tmp.name) / "App" / "SimpleMail.exe"
        with self._frozen(executable):
            self.assertFalse(windows.refresh_startup_profile(Path.cwd()))  # автозапуск выключен
            self.shortcut.parent.mkdir()
            for target, arguments in [windows.startup_launch(Path.cwd()),
                                      ("C:\\Other App\\Mail.exe", "--data D:\\Mail"),
                                      ("C:\\Python\\pythonw.exe", '"D:\\SimpleMail\\main.py"')]:
                with self.subTest(target=target):
                    self.shortcut.write_text(target + "\n" + arguments, encoding="utf-8")
                    self.assertFalse(windows.refresh_startup_profile(Path.cwd()))
        with mock.patch.object(windows.sys, "frozen", False, create=True):
            self.shortcut.write_text("D:\\Old\\SimpleMail.exe\n", encoding="utf-8")
            self.assertFalse(windows.refresh_startup_profile(Path.cwd()))  # запуск из исходников
        self.assertEqual(self.written, [])
        self.registry.SetValueEx.assert_not_called()
        self.registry.QueryValueEx.assert_not_called()  # реестр при запуске не читается и не пишется

    def test_refresh_propagates_read_and_write_errors(self):
        self.shortcut.parent.mkdir()
        self.shortcut.write_text("D:\\Old\\SimpleMail.exe\n--data D:\\Old", encoding="utf-8")
        with self._frozen(Path(self.tmp.name) / "SimpleMail.exe"):
            with mock.patch.object(windows, "_read_shortcut", side_effect=PermissionError("read denied")), \
                    self.assertRaisesRegex(PermissionError, "read denied"):
                windows.refresh_startup_profile(Path.cwd())
            with mock.patch.object(windows, "_write_shortcut", side_effect=PermissionError("write denied")), \
                    self.assertRaisesRegex(PermissionError, "write denied"):
                windows.refresh_startup_profile(Path.cwd())

    def test_taskbar_has_its_own_identity(self):
        shell = mock.Mock()
        fake_ctypes = SimpleNamespace(windll=SimpleNamespace(shell32=shell),
                                      c_wchar_p=ctypes.c_wchar_p, c_long=ctypes.c_long)
        with mock.patch.object(windows, "os", SimpleNamespace(name="nt")), \
                mock.patch.object(windows, "ctypes", fake_ctypes):
            windows.configure_taskbar_identity()
        shell.SetCurrentProcessExplicitAppUserModelID.assert_called_once_with("TimohovAS.SimpleMail")


@unittest.skipUnless(os.name == "nt", "ярлыки Windows создаются только в Windows")
class TestRealShortcut(unittest.TestCase):
    """Настоящий IShellLinkW: ярлык создаётся, читается и открывается оболочкой Windows."""

    def test_write_and_read_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "App Files" / "SimpleMail.exe"
            target.parent.mkdir()
            target.write_bytes(b"MZ")
            shortcut = Path(tmp) / "Автозагрузка" / "SimpleMail.lnk"
            arguments = '--profile-fallback "D:\\Почта и вложения" --minimized'
            windows._write_shortcut(shortcut, str(target), arguments)
            self.assertTrue(shortcut.is_file())
            self.assertEqual(windows._read_shortcut(shortcut), (str(target), arguments))
            windows._write_shortcut(shortcut, str(target), "--minimized")  # перезапись
            self.assertEqual(windows._read_shortcut(shortcut), (str(target), "--minimized"))

    def test_startup_folder_is_users_startup(self):
        path = windows.startup_shortcut()
        self.assertEqual(path.name, "SimpleMail.lnk")
        self.assertEqual(path.parent.name.casefold(), "startup")


class TestIconAndStartupUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.db.save_account(Account(name="Test", enabled=False, storage_dir=self.tmp.name))
        self.patches = [mock.patch.object(main_window, "run_task"),
                        mock.patch("mailclient.ui.mail_watch.MailWatcher"),
                        mock.patch.object(viewer, "HAS_WEBENGINE", False),
                        mock.patch.object(main_window, "startup_supported", return_value=True),
                        mock.patch.object(main_window, "startup_enabled", return_value=False),
                        mock.patch.object(main_window, "set_startup_enabled")]
        mocks = [patch.start() for patch in self.patches]
        self.set_startup = mocks[-1]
        self.window = main_window.MainWindow(self.db)
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        for patch in reversed(self.patches):
            patch.stop()
        self.db.close()
        self.tmp.cleanup()

    def test_autostart_option_is_off_until_user_checks_it(self):
        action = self.window.acts["autostart"]
        self.assertTrue(action.isCheckable())
        self.assertFalse(action.isChecked())
        self.set_startup.assert_not_called()
        action.trigger()
        self.set_startup.assert_called_once_with(True, Path(self.tmp.name).resolve())
        self.assertTrue(action.isChecked())
        action.trigger()
        self.assertEqual(self.set_startup.call_args.args, (False, Path(self.tmp.name).resolve()))

    def test_hidden_start_needs_tray_and_configured_mailbox(self):
        with mock.patch.object(type(self.window.tray), "available", new_callable=mock.PropertyMock,
                               return_value=True):
            self.assertTrue(self.window.can_start_hidden())
            with mock.patch.object(self.db, "accounts", return_value=[]):
                self.assertFalse(self.window.can_start_hidden())  # первый запуск: нужно окно ящика
        with mock.patch.object(type(self.window.tray), "available", new_callable=mock.PropertyMock,
                               return_value=False):
            self.assertFalse(self.window.can_start_hidden())  # без трея программу было бы не открыть

    def test_window_never_shown_keeps_saved_layout(self):
        self.db.set_setting("geometry", "saved")
        self.window._save_ui()
        self.assertEqual(self.db.get_setting("geometry"), "saved")
        self.window.show()
        self.app.processEvents()
        self.window._save_ui()
        self.assertNotEqual(self.db.get_setting("geometry"), "saved")

    def test_failed_registration_restores_checkbox_and_shows_error(self):
        self.set_startup.side_effect = PermissionError("test")
        with mock.patch.object(QMessageBox, "warning") as warning:
            self.window.acts["autostart"].trigger()
            warning.assert_called_once()
        self.assertFalse(self.window.acts["autostart"].isChecked())

    def test_ico_contains_all_windows_sizes_and_window_uses_it(self):
        from mailclient import __version__
        self.assertEqual(self.window.windowTitle(), f"SimpleMail {__version__}")
        data = (ICON_DIR / "simplemail.ico").read_bytes()
        self.assertEqual(struct.unpack_from("<HHH", data), (0, 1, 7))
        sizes = []
        for index in range(7):
            width, height, _, _, _, depth, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * index)
            sizes.append(width or 256)
            self.assertEqual(width, height)
            self.assertEqual(depth, 32)
            self.assertEqual(data[offset:offset + 8], b"\x89PNG\r\n\x1a\n")
            self.assertLessEqual(offset + length, len(data))
        self.assertEqual(sizes, [16, 24, 32, 48, 64, 128, 256])
        self.assertFalse(self.window.windowIcon().isNull())
        for size in sizes:
            self.assertFalse(app_icon().pixmap(size, size).isNull())


class TestMinimizedStart(unittest.TestCase):
    """main.py --minimized: окно не открывается, если программу можно открыть из трея."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def run_main(self, *extra, hidden_ok=True, isolated=True, registry_error=False):
        import main as entrypoint
        window = mock.Mock()
        window.can_start_hidden.return_value = hidden_ok
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "mail.db")
            try:
                with mock.patch.object(entrypoint, "Database", return_value=db), \
                        mock.patch.object(entrypoint, "_instance_lease", None), \
                        mock.patch("mailclient.instance.acquire_instance", return_value=object()), \
                        mock.patch("mailclient.updates.hold_installation_mutex"), \
                        mock.patch("mailclient.passwords.initialize_keyring"), \
                        mock.patch("mailclient.logs.setup_logging"), \
                        mock.patch("mailclient.instance.start_activation_server"), \
                        mock.patch.object(entrypoint, "QApplication", return_value=self.app), \
                        mock.patch.object(main_window, "MainWindow", return_value=window), \
                        mock.patch.object(viewer, "register_schemes"), \
                        mock.patch.object(entrypoint, "remember_profile", return_value=True) as remember, \
                        mock.patch.object(windows, "refresh_startup_profile",
                                          side_effect=PermissionError("test") if registry_error else None) as refresh, \
                        mock.patch.dict(os.environ, {"SIMPLEMAIL_PROFILE_FILE": ""}), \
                        mock.patch.object(self.app, "exec", return_value=0), \
                        mock.patch("sys.argv", ["main.py", "--data", tmp,
                                                *(["--isolated-profile"] if isolated else []), *extra]):
                    with self.assertRaises(SystemExit):
                        entrypoint.main()
                if isolated:
                    remember.assert_not_called()
                    refresh.assert_not_called()
                else:
                    remember.assert_called_once_with(db)
                    refresh.assert_called_once_with(Path(tmp).resolve())
            finally:
                db.close()
        return window

    def test_autostart_launch_stays_in_tray(self):
        self.run_main("--minimized").show.assert_not_called()

    def test_normal_launch_and_impossible_hidden_start_show_window(self):
        self.run_main().show.assert_called_once_with()
        self.run_main("--minimized", hidden_ok=False).show.assert_called_once_with()

    def test_registry_error_does_not_stop_startup(self):
        with self.assertLogs("simplemail", level="WARNING"):
            self.run_main(isolated=False, registry_error=True).show.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
