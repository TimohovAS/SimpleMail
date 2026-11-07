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


class TestWindowsStartup(unittest.TestCase):
    def setUp(self):
        self.registry = mock.MagicMock()
        self.registry.REG_SZ = 1
        self.registry.REG_EXPAND_SZ = 2
        self.registry.KEY_SET_VALUE = 2
        self.registry.KEY_QUERY_VALUE = 1
        self.registry.HKEY_CURRENT_USER = "HKCU"
        self.patch = mock.patch.object(windows, "winreg", self.registry)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_enable_preserves_windowed_launcher_and_data_directory_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / "Python Files" / "python.exe"
            python.parent.mkdir()
            windowed = python.with_name("pythonw.exe")
            windowed.touch()
            project = root / "Project Files"
            data = root / "User Mail"
            with mock.patch.object(windows.sys, "executable", str(python)), \
                    mock.patch.object(windows, "PROJECT_DIR", project), \
                    mock.patch.object(windows, "startup_supported", return_value=True):
                windows.set_startup_enabled(True, data)
            expected = f'"{windowed.resolve()}" "{project / "main.py"}" --profile-fallback "{data.resolve()}"'
            self.registry.SetValueEx.assert_called_once_with(
                self.registry.CreateKeyEx.return_value.__enter__.return_value,
                "SimpleMail", 0, self.registry.REG_SZ, expected)
            self.registry.CreateKeyEx.assert_called_once_with(
                "HKCU", windows.RUN_KEY, 0, self.registry.KEY_SET_VALUE)

    def test_disable_only_removes_simplemail_and_ignores_missing_value(self):
        with mock.patch.object(windows, "startup_supported", return_value=True):
            windows.set_startup_enabled(False, Path.cwd())
            self.registry.DeleteValue.assert_called_once_with(
                self.registry.OpenKey.return_value.__enter__.return_value, "SimpleMail")
            self.registry.DeleteKey.assert_not_called()
            self.registry.DeleteValue.side_effect = FileNotFoundError()
            windows.set_startup_enabled(False, Path.cwd())

    def test_frozen_build_autostart_uses_exe_without_python_or_main_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "App Files" / "SimpleMail.exe"
            data = Path(tmp) / "Mail Data"
            with mock.patch.object(windows.sys, "executable", str(executable)), \
                    mock.patch.object(windows.sys, "frozen", True, create=True):
                command = windows.startup_command(data)
            self.assertEqual(command, f'"{executable.resolve()}" --profile-fallback "{data.resolve()}"')
            self.assertNotIn("python", command.lower())
            self.assertNotIn("main.py", command)

    def test_read_startup_state_and_unavailable_platform(self):
        with mock.patch.object(windows, "startup_supported", return_value=True):
            self.registry.QueryValueEx.return_value = ("pythonw.exe main.py", self.registry.REG_SZ)
            self.assertTrue(windows.startup_enabled())
            self.registry.QueryValueEx.side_effect = FileNotFoundError()
            self.assertFalse(windows.startup_enabled())
        with mock.patch.object(windows, "os", SimpleNamespace(name="posix")):
            self.assertFalse(windows.startup_supported())
            self.assertFalse(windows.startup_enabled())
            with self.assertRaises(OSError):
                windows.set_startup_enabled(True, Path.cwd())

    def test_permission_errors_are_reported_to_caller(self):
        self.registry.CreateKeyEx.side_effect = PermissionError("test")
        with mock.patch.object(windows, "startup_supported", return_value=True):
            with self.assertRaises(PermissionError):
                windows.set_startup_enabled(True, Path.cwd())

    def test_refresh_replaces_legacy_executable_and_explicit_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "New Install" / "SimpleMail.exe"
            data = Path(tmp) / "Active Mail"
            commands = [
                ('"D:\\Old Install\\SimpleMail.exe" --data "D:\\Old Mail"', self.registry.REG_SZ),
                ('D:\\Portable\\SIMPLEMAIL.EXE --data D:\\OldMail', self.registry.REG_SZ),
                ('"%LOCALAPPDATA%\\Programs\\SimpleMail\\SimpleMail.exe" --data "D:\\Old Mail"',
                 self.registry.REG_EXPAND_SZ),
            ]
            with mock.patch.object(windows.sys, "executable", str(executable)), \
                    mock.patch.object(windows.sys, "frozen", True, create=True), \
                    mock.patch.object(windows, "startup_supported", return_value=True):
                for command, kind in commands:
                    with self.subTest(command=command):
                        self.registry.reset_mock()
                        self.registry.QueryValueEx.return_value = (command, kind)
                        self.assertTrue(windows.refresh_startup_profile(data))
                        expected = f'"{executable.resolve()}" --profile-fallback "{data.resolve()}"'
                        self.registry.SetValueEx.assert_called_once_with(
                            self.registry.OpenKey.return_value.__enter__.return_value,
                            "SimpleMail", 0, self.registry.REG_SZ, expected)
                        self.registry.CreateKeyEx.assert_not_called()

    def test_refresh_leaves_disabled_unrelated_or_malformed_commands_unchanged(self):
        with mock.patch.object(windows.sys, "frozen", True, create=True), \
                mock.patch.object(windows, "startup_supported", return_value=True):
            for command, kind in [("", self.registry.REG_SZ),
                                  ('"C:\\Other App\\Mail.exe" --data D:\\Mail', self.registry.REG_SZ),
                                  ('pythonw.exe "D:\\SimpleMail\\main.py"', self.registry.REG_SZ),
                                  ('"C:\\Broken\\SimpleMail.exe --data D:\\Mail', self.registry.REG_SZ),
                                  ('SimpleMail.exe', 3)]:
                with self.subTest(command=command, kind=kind):
                    self.registry.QueryValueEx.return_value = (command, kind)
                    self.assertFalse(windows.refresh_startup_profile(Path.cwd()))
            self.registry.QueryValueEx.side_effect = FileNotFoundError()
            self.assertFalse(windows.refresh_startup_profile(Path.cwd()))
        self.registry.SetValueEx.assert_not_called()
        self.registry.CreateKeyEx.assert_not_called()

    def test_refresh_skips_source_runs_and_unsupported_platforms(self):
        with mock.patch.object(windows.sys, "frozen", False, create=True), \
                mock.patch.object(windows, "startup_supported", return_value=True):
            self.assertFalse(windows.refresh_startup_profile(Path.cwd()))
        with mock.patch.object(windows.sys, "frozen", True, create=True), \
                mock.patch.object(windows, "startup_supported", return_value=False):
            self.assertFalse(windows.refresh_startup_profile(Path.cwd()))
        self.registry.OpenKey.assert_not_called()

    def test_refresh_does_not_rewrite_current_command(self):
        with mock.patch.object(windows.sys, "frozen", True, create=True), \
                mock.patch.object(windows, "startup_supported", return_value=True):
            self.registry.QueryValueEx.return_value = (windows.startup_command(Path.cwd()), self.registry.REG_SZ)
            self.assertFalse(windows.refresh_startup_profile(Path.cwd()))
        self.registry.SetValueEx.assert_not_called()

    def test_refresh_propagates_read_and_write_errors(self):
        with mock.patch.object(windows.sys, "frozen", True, create=True), \
                mock.patch.object(windows, "startup_supported", return_value=True):
            self.registry.QueryValueEx.side_effect = PermissionError("read denied")
            with self.assertRaisesRegex(PermissionError, "read denied"):
                windows.refresh_startup_profile(Path.cwd())
            self.registry.QueryValueEx.side_effect = None
            self.registry.QueryValueEx.return_value = ('D:\\Old\\SimpleMail.exe --data D:\\Old', self.registry.REG_SZ)
            self.registry.SetValueEx.side_effect = PermissionError("write denied")
            with self.assertRaisesRegex(PermissionError, "write denied"):
                windows.refresh_startup_profile(Path.cwd())

    def test_taskbar_has_its_own_identity(self):
        shell = mock.Mock()
        fake_ctypes = SimpleNamespace(windll=SimpleNamespace(shell32=shell),
                                      c_wchar_p=ctypes.c_wchar_p, c_long=ctypes.c_long)
        with mock.patch.object(windows, "os", SimpleNamespace(name="nt")), \
                mock.patch.object(windows, "ctypes", fake_ctypes):
            windows.configure_taskbar_identity()
        shell.SetCurrentProcessExplicitAppUserModelID.assert_called_once_with("TimohovAS.SimpleMail")


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


if __name__ == "__main__":
    unittest.main()
