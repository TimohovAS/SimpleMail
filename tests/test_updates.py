"""Trust boundaries, damaged/canceled downloads, and update shutdown behavior."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request

from mailclient import __version__, updates

PAYLOAD = b"MZ-test-installer" * 20
_major, _minor, _patch = updates.version_tuple(__version__)
NEXT_VERSION = f"{_major}.{_minor + 1}.0"


def release_data(version=None, payload=PAYLOAD):
    version = version or NEXT_VERSION
    name = f"SimpleMail-Setup-{version}-x64.exe"
    return {"tag_name": "v" + version, "draft": False, "prerelease": False, "body": "Release notes",
            "assets": [{"name": name, "state": "uploaded", "size": len(payload),
                        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                        "browser_download_url": f"{updates.RELEASES_URL}/download/v{version}/{name}"}]}


def response(payload, length=None):
    stream = io.BytesIO(payload)
    stream.headers = {} if length is None else {"Content-Length": str(length)}
    return stream


class TestRelease(unittest.TestCase):
    def test_semver_and_stable_releases_only(self):
        self.assertGreater(updates.version_tuple("0.10.0"), updates.version_tuple("0.9.9"))
        for flag in ("draft", "prerelease"):
            data = release_data()
            data[flag] = True
            self.assertIsNone(updates.parse_release(data))
        self.assertIsNone(updates.parse_release(release_data(__version__)))
        self.assertIsNone(updates.parse_release(release_data("0.0.0")))
        self.assertEqual(updates.parse_release(release_data()).version, NEXT_VERSION)

    def test_refuses_invalid_or_untrusted_metadata(self):
        for field, value in (("browser_download_url", "http://github.com/file.exe"),
                             ("browser_download_url", "https://evil.example/file.exe"),
                             ("digest", None), ("digest", "sha256:bad"), ("size", 0),
                             ("size", updates.MAX_INSTALLER_SIZE + 1), ("name", "another.exe")):
            with self.subTest(field=field, value=value):
                data = release_data()
                data["assets"][0][field] = value
                with self.assertRaises(updates.UpdateError):
                    updates.parse_release(data)
        for tag in ("v../0.11.0", "v0.11.0-beta.1", "v0.11", "v00.11.0"):
            data = release_data()
            data["tag_name"] = tag
            with self.assertRaises(updates.UpdateError):
                updates.parse_release(data)

    def test_404_means_no_published_release_but_other_errors_are_reported(self):
        with mock.patch.object(updates, "_open", side_effect=HTTPError(updates.API_URL, 404, "Missing", {}, None)):
            self.assertIsNone(updates.check_update())
        with mock.patch.object(updates, "_open", side_effect=HTTPError(updates.API_URL, 403, "Rate limited", {}, None)):
            with self.assertRaises(HTTPError):
                updates.check_update()

    def test_metadata_limits_and_https_redirects(self):
        with mock.patch.object(updates, "_open", return_value=response(json.dumps(release_data()).encode())):
            self.assertEqual(updates.check_update().version, NEXT_VERSION)
        with mock.patch.object(updates, "_open", return_value=response(b" " * (updates.MAX_METADATA_SIZE + 1))):
            with self.assertRaises(updates.UpdateError):
                updates.check_update()
        with self.assertRaises(updates.UpdateError):
            updates._HTTPSRedirects().redirect_request(Request(updates.API_URL), None, 302, "", {}, "http://cdn.example/file")


class TestDownload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.release = updates.parse_release(release_data())

    def test_verified_download_and_cache_reuse(self):
        progress = []
        with mock.patch.object(updates, "_open", return_value=response(PAYLOAD, len(PAYLOAD))) as request:
            path = updates.download_update(self.release, self.root, progress=progress.append)
            self.assertEqual(path.read_bytes(), PAYLOAD)
            self.assertEqual(progress[-1], "100")
            self.assertEqual(updates.download_update(self.release, self.root), path)
            request.assert_called_once_with(self.release.url)
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_damaged_truncated_oversize_and_wrong_length_never_leave_an_installer(self):
        for payload, length in ((b"X" * len(PAYLOAD), None), (PAYLOAD[:-1], None),
                                (PAYLOAD + b"x", None), (PAYLOAD, 1)):
            with self.subTest(length=length, size=len(payload)):
                with mock.patch.object(updates, "_open", return_value=response(payload, length)):
                    with self.assertRaises(updates.UpdateError):
                        updates.download_update(self.release, self.root)
                self.assertEqual(list(self.root.iterdir()), [])

    def test_network_failure_and_cancellation_remove_partial_files(self):
        with mock.patch.object(updates, "_open", side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError):
                updates.download_update(self.release, self.root)
        cancel = threading.Event()
        with mock.patch.object(updates, "_open", return_value=response(PAYLOAD)):
            with self.assertRaises(updates.DownloadCancelled):
                updates.download_update(self.release, self.root, cancel=cancel, progress=lambda _: cancel.set())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_corrupt_cache_is_replaced_and_tampering_is_detected_before_install(self):
        path = self.root / self.release.filename
        path.write_bytes(b"damaged")
        with mock.patch.object(updates, "_open", return_value=response(PAYLOAD)):
            self.assertEqual(updates.download_update(self.release, self.root), path)
        path.write_bytes(b"X" * len(PAYLOAD))
        with self.assertRaises(updates.UpdateError):
            updates.verify_installer(path, self.release)

    def test_launch_preserves_paths_and_language_without_shell_interpolation(self):
        path = self.root / self.release.filename
        path.write_bytes(PAYLOAD)
        data_dir = self.root / "Mail with spaces & symbols"
        with mock.patch.object(updates, "installation_supported", return_value=True), \
                mock.patch.object(updates, "cache_dir", return_value=self.root / "helper cache"), \
                mock.patch.object(updates, "_start_update_helper") as start:
            updates.launch_installer(path, self.release, data_dir, "sr_Latn")
        command = start.call_args.args[0]
        self.assertEqual(Path(command[0]).name, "powershell.exe")
        self.assertIn("Hidden", command)
        request = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        args = request["arguments"]
        self.assertEqual(request["installer"], str(path.resolve()))
        self.assertEqual(request["parent_pid"], os.getpid())
        self.assertEqual(request["sha256"], self.release.sha256)
        self.assertEqual(request["size"], self.release.size)
        self.assertTrue(Path(command[-1]).with_suffix(".ps1").is_file())
        self.assertIn("/MAILDATA=" + str(data_dir.resolve()), args)
        self.assertIn("/LANG=serbianlatin", args)
        self.assertNotIn("shell", start.call_args.kwargs)
        self.assertNotIn("/VERYSILENT", args)

    def test_failed_helper_launch_removes_queued_request(self):
        path = self.root / self.release.filename
        path.write_bytes(PAYLOAD)
        directory = self.root / "helper"
        with mock.patch.object(updates, "installation_supported", return_value=True), \
                mock.patch.object(updates, "cache_dir", return_value=directory), \
                mock.patch.object(updates, "_start_update_helper", side_effect=OSError("blocked")):
            with self.assertRaises(OSError):
                updates.launch_installer(path, self.release, self.root)
        self.assertEqual(list(directory.iterdir()), [])


@unittest.skipUnless(os.name == "nt", "Windows update helper")
class TestUpdateHelper(unittest.TestCase):
    def test_tampered_installer_is_rejected_after_waiting_for_parent_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer = root / "setup with spaces.exe"
            installer.write_bytes(PAYLOAD)
            parent = subprocess.Popen([sys.executable, "-c",
                                       "import sys,time; from pathlib import Path; time.sleep(1); "
                                       "p=Path(sys.argv[1]); p.write_bytes(b'X'*p.stat().st_size)",
                                       str(installer)], creationflags=subprocess.CREATE_NO_WINDOW)
            self.addCleanup(parent.wait)
            request = root / "install request.json"
            helper = request.with_suffix(".ps1")
            shutil.copyfile(Path(updates.__file__).with_name("update_helper.ps1"), helper)
            request.write_text(json.dumps({"parent_pid": parent.pid, "installer": str(installer),
                                           "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
                                           "size": len(PAYLOAD), "arguments": []}), encoding="utf-8")
            powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                     "-File", str(helper), "-RequestPath", str(request), "-QuietErrors"],
                                    capture_output=True, text=True, timeout=20,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            parent.wait()
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("verification failed", request.with_suffix(".error.log").read_text(encoding="utf-8-sig"))
            self.assertFalse(request.exists())
            self.assertFalse(helper.exists())


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
from mailclient.db import Account, Database
from mailclient.ui import main_window, updates_dialog, viewer


class TestUpdatesUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.parent = QMainWindow()
        self.parent._closing = False
        self.install = mock.Mock(return_value=True)
        self.dialog = updates_dialog.UpdatesDialog(self.parent, self.install)
        self.worker_patch = mock.patch.object(updates_dialog, "run_task")
        self.worker = self.worker_patch.start()
        self.addCleanup(self.worker_patch.stop)
        self.addCleanup(self.parent.close)
        self.addCleanup(self.dialog.close)

    def test_check_stays_hidden_without_release_and_shows_new_release_as_plain_text(self):
        self.dialog.check(automatic=True)
        self.worker.call_args.kwargs["on_done"](None)
        self.assertFalse(self.dialog.isVisible())
        self.dialog.check(automatic=True)
        self.worker.call_args.kwargs["on_done"](updates.parse_release(release_data()))
        self.assertTrue(self.dialog.isVisible())
        self.assertEqual(self.dialog.notes.toPlainText(), "Release notes")
        self.assertTrue(self.dialog.download_button.isEnabled())
        self.assertFalse(self.dialog.install_button.isEnabled())

    def test_no_install_before_verification_and_failed_verification_disables_install(self):
        self.dialog.release = updates.parse_release(release_data())
        self.dialog.installer = Path("fake.exe")
        with mock.patch.object(updates, "installation_supported", return_value=True):
            self.dialog.install()
            self.install.assert_not_called()
            from mailclient.errors import explain
            self.worker.call_args.kwargs["on_error"](explain(updates.UpdateError("damaged")))
            self.assertFalse(self.dialog.install_button.isEnabled())
            self.assertTrue(self.dialog.download_button.isEnabled())
        self.install.assert_not_called()

    def test_closing_cancels_download_and_late_automatic_check_does_not_reopen_app(self):
        self.dialog.release = updates.parse_release(release_data())
        self.dialog.download()
        self.dialog.cancel_button.click()
        self.assertTrue(self.dialog.cancel.is_set())
        self.dialog.reject()
        self.assertTrue(self.dialog.cancel.is_set())
        self.worker.call_args.kwargs["on_done"](None)
        self.assertTrue(self.dialog.download_button.isEnabled())
        self.dialog.check(automatic=True)
        self.parent._closing = True
        self.worker.call_args.kwargs["on_done"](updates.parse_release(release_data()))
        self.assertFalse(self.dialog.isVisible())

    def test_dismissed_automatic_check_does_not_reopen_dialog_or_replace_new_check(self):
        self.dialog.check(automatic=True)
        old_done = self.worker.call_args.kwargs["on_done"]
        self.dialog.show()
        self.dialog.reject()
        old_done(updates.parse_release(release_data()))
        self.assertFalse(self.dialog.isVisible())
        self.assertIsNone(self.dialog.release)
        self.dialog.check()
        old_done(updates.parse_release(release_data()))
        self.assertTrue(self.dialog.busy)
        self.worker.call_args.kwargs["on_done"](None)
        self.assertFalse(self.dialog.busy)

    def test_dismissed_verification_never_calls_install(self):
        self.dialog.release = updates.parse_release(release_data())
        self.dialog.installer = Path("fake.exe")
        with mock.patch.object(updates, "installation_supported", return_value=True):
            self.dialog.install()
            finished = self.worker.call_args.kwargs["on_done"]
            self.dialog.reject()
            finished(Path("fake.exe"))
        self.install.assert_not_called()

    def test_new_download_does_not_reuse_cancellation_or_receive_old_progress(self):
        self.dialog.release = updates.parse_release(release_data())
        self.dialog.download()
        old_worker = self.worker.call_args
        old_cancel = self.dialog.cancel
        self.dialog.reject()
        self.dialog.download()
        self.assertTrue(old_cancel.is_set())
        self.assertFalse(self.dialog.cancel.is_set())
        self.dialog.cancel_button.click()
        self.assertTrue(self.dialog.cancel.is_set())
        old_worker.kwargs["on_progress"]("80")
        old_worker.kwargs["on_done"](Path("obsolete.exe"))
        self.assertEqual(self.dialog.progress.value(), 0)
        self.assertIsNone(self.dialog.installer)
        self.assertTrue(self.dialog.busy)

    def test_real_background_worker_checks_downloads_and_verifies_before_handoff(self):
        from mailclient.ui.worker import run_task

        def finish():
            deadline = time.monotonic() + 5
            while self.dialog.busy and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.005)
            self.assertFalse(self.dialog.busy, "Background operation did not finish")

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(updates_dialog, "run_task", run_task), \
                mock.patch.object(updates, "cache_dir", return_value=Path(tmp)), \
                mock.patch.object(updates, "installation_supported", return_value=True), \
                mock.patch.object(updates, "_open", side_effect=[
                    response(json.dumps(release_data()).encode()), response(PAYLOAD)]):
            self.dialog.check()
            finish()
            self.dialog.download()
            self.assertFalse(self.dialog.install_button.isEnabled())
            finish()
            self.assertTrue(self.dialog.install_button.isEnabled())
            self.dialog.install()
            self.install.assert_not_called()
            finish()
            self.install.assert_called_once()
            self.assertEqual(self.install.call_args.args[0].read_bytes(), PAYLOAD)


class TestUpdateShutdown(unittest.TestCase):
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
                        mock.patch("mailclient.ui.worker._active", set())]
        for patch in self.patches:
            patch.start()
        self.window = main_window.MainWindow(self.db)
        self.app.processEvents()
        self.app._pending_update = None
        self.release = updates.parse_release(release_data())

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.app._pending_update = None
        for patch in reversed(self.patches):
            patch.stop()
        self.db.close()
        self.tmp.cleanup()

    def test_busy_mail_and_declined_confirmation_leave_application_running(self):
        self.window.syncing = 1
        with mock.patch.object(QMessageBox, "information"):
            self.assertFalse(self.window._install_update(Path("setup.exe"), self.release))
        self.window.syncing = 0
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            self.assertFalse(self.window._install_update(Path("setup.exe"), self.release))
        self.assertIsNone(self.app._pending_update)
        self.assertFalse(self.window._closing)

    def test_failed_draft_save_aborts_update_and_success_preserves_custom_data_directory(self):
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                mock.patch.object(self.window, "_save_quick_draft", return_value=False):
            self.assertFalse(self.window._install_update(Path("setup.exe"), self.release))
        self.assertIsNone(self.app._pending_update)
        self.assertFalse(self.window._closing)
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.assertTrue(self.window._install_update(Path("setup.exe"), self.release))
        self.assertEqual(self.app._pending_update[2], Path(self.tmp.name).resolve())
        self.assertTrue(self.window._closing)

    def test_install_exits_instead_of_hiding_to_tray(self):
        self.window.tray.icon = mock.Mock()
        self.window.acts["close_to_tray"].setChecked(True)
        self.window.show()
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.assertTrue(self.window._install_update(Path("setup.exe"), self.release))
        self.assertTrue(self.window._closing)
        self.assertTrue(self.window._quitting)
        self.assertIsNotNone(self.app._pending_update)


if __name__ == "__main__":
    unittest.main()
