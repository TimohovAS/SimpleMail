"""Процесс встроенного браузера, который рисует письмо, завершился (сон компьютера, антивирус,
нехватка памяти): текст письма пропадал, оставались только шапка и вложения. Теперь страница
показывается заново."""
import json
import os
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mailclient.ui import viewer


class TestRestartLimit(unittest.TestCase):
    def make_view(self):
        view = viewer.MessageView.__new__(viewer.MessageView)
        view._shutdown = False
        view.web = object()
        view._loading = True
        view._render_restarts = []
        return view

    def test_page_is_shown_again_but_not_endlessly(self):
        view = self.make_view()
        with mock.patch.object(viewer.QTimer, "singleShot") as later:
            for _ in range(viewer.RENDER_RESTART_LIMIT):
                view._render_process_terminated("CrashedTerminationStatus", 1)
            self.assertFalse(view._loading)
            self.assertEqual([c.args[1] for c in later.call_args_list],
                             [view._reload_page] * viewer.RENDER_RESTART_LIMIT)
            later.reset_mock()
            view._render_process_terminated("CrashedTerminationStatus", 1)
            self.assertEqual([c.args[1] for c in later.call_args_list], [view._show_render_failure])

    def test_old_restarts_are_forgotten(self):
        view = self.make_view()
        view._render_restarts = [0.0] * viewer.RENDER_RESTART_LIMIT
        with mock.patch.object(viewer.QTimer, "singleShot") as later, \
                mock.patch.object(viewer.time, "monotonic", return_value=viewer.RENDER_RESTART_WINDOW_S + 1):
            view._render_process_terminated("CrashedTerminationStatus", 1)
        self.assertEqual(later.call_args.args[1], view._reload_page)

    def test_nothing_after_shutdown(self):
        view = self.make_view()
        view._shutdown = True
        with mock.patch.object(viewer.QTimer, "singleShot") as later:
            view._render_process_terminated("NormalTerminationStatus", 0)
        later.assert_not_called()


class TestRealRendererKilled(unittest.TestCase):
    @unittest.skipUnless(viewer.HAS_WEBENGINE, "Qt WebEngine is unavailable")
    def test_text_comes_back_after_renderer_is_killed(self):
        # Отдельный процесс: схема адресов регистрируется до QApplication.
        script = textwrap.dedent('''
            import json, os, signal
            from PySide6.QtCore import Qt, QEventLoop, QTimer
            from PySide6.QtWidgets import QApplication
            from mailclient.ui import viewer
            from mailclient.db import Message
            viewer.register_schemes()
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
            app = QApplication([])
            view = viewer.MessageView(); view.resize(600, 400); view.show()
            def wait_until(predicate, ms=15000):
                loop = QEventLoop(); poll = QTimer(); poll.setInterval(20)
                poll.timeout.connect(lambda: loop.quit() if predicate() else None)
                poll.start(); QTimer.singleShot(ms, loop.quit); loop.exec(); poll.stop()
                return predicate()
            def text():
                out = []
                view.web.page().toPlainText(out.append)
                wait_until(lambda: bool(out))
                return out[0]
            view.show_message(Message(id=1, body_text="Текст письма"))
            assert wait_until(lambda: not view._loading), "page not loaded"
            result = {"before": text()}
            page = view.web.page()
            pid = page.renderProcessPid()
            os.kill(pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
            result["restarted"] = wait_until(lambda: page.renderProcessPid() not in (0, pid) and not view._loading)
            result["after"] = text()
            print(json.dumps(result), flush=True)
            view.shutdown(); app.processEvents()
            os._exit(0)
        ''')
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                timeout=60, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(state, {"before": "Текст письма", "restarted": True, "after": "Текст письма"})


if __name__ == "__main__":
    unittest.main()
