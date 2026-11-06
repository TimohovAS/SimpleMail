"""Background thread bodies update WebEngine without starting a navigation."""
import os
import json
import subprocess
import sys
import textwrap
import unittest
from dataclasses import replace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from mailclient.db import Message
from mailclient.ui import viewer


class TestThreadBodyRefresh(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with mock.patch.object(viewer, "HAS_WEBENGINE", False):
            self.view = viewer.MessageView()
        self.first = Message(id=1, body_text="The text being read")
        self.second = Message(id=2, body_downloaded=False)
        self.view.show_thread(self.first, [self.first, self.second], set())
        # Exercise WebEngine routing without creating a Chromium process. The
        # application normally executes this script in its isolated world.
        self.view._uses_webengine = True
        self.view.web = mock.Mock()
        self.view.interceptor = mock.Mock()
        self.page = self.view.web.page.return_value
        self.page.runJavaScript.side_effect = lambda script, world, callback=None: callback(True) if callback else None
        self.view._counter = 7
        self.view._loading = False

    def tearDown(self):
        self.view.web = None
        self.view.close()

    def test_same_thread_body_arrival_patches_cards_without_navigation(self):
        complete = replace(self.second, body_downloaded=True, body_text='Body with "quotes" < & >')
        page = viewer.build_thread_page([self.first, complete], 1, set(), False, viewer.thread_style())
        self.view.show_thread(self.first, [self.first, complete], set(), page=page, keep_view=True)
        self.view.web.setUrl.assert_not_called()
        self.assertEqual(self.view._counter, 7)
        self.assertEqual(self.view._msg.id, 1)
        self.assertEqual(self.view.thread_state(), {(1, True), (2, True)})
        self.assertEqual(self.view._page_html, page.html)
        self.page.runJavaScript.assert_called_once()
        script, world, _ = self.page.runJavaScript.call_args.args
        self.assertEqual(world, viewer.QWebEngineScript.ScriptWorldId.ApplicationWorld)
        self.assertIn('"/message/7"', script)
        self.assertIn("if(left.isEqualNode(right))return", script)
        self.assertIn("next.open=old.open", script)
        self.assertIn("window.scrollTo", script)

    def test_loading_page_defers_and_coalesces_to_latest_body(self):
        self.view._loading = True
        first_update = replace(self.second, body_downloaded=True, body_text="Old update")
        latest = replace(first_update, body_text="Latest update")
        self.view.show_thread(self.first, [self.first, first_update], set(), keep_view=True)
        self.view.show_thread(self.first, [self.first, latest], set(), keep_view=True)
        self.page.runJavaScript.assert_not_called()
        self.view.web.setUrl.assert_not_called()
        self.assertEqual(self.view.thread_state(), {(1, True), (2, False)})
        self.view._load_finished(True)
        self.page.runJavaScript.assert_called_once()
        self.view.web.setUrl.assert_not_called()
        self.assertIn("Latest update", self.view._page_html)
        self.assertNotIn("Old update", self.view._page_html)
        self.assertIsNone(self.view._pending_thread_refresh)

    def test_other_message_discards_deferred_thread_refresh(self):
        self.view._loading = True
        complete = replace(self.second, body_downloaded=True, body_text="Arrived body")
        self.view.show_thread(self.first, [self.first, complete], set(), keep_view=True)
        self.view.show_message(Message(id=3, body_text="Different message"))
        self.view.web.setUrl.reset_mock()
        self.view._load_finished(True)
        self.view.web.setUrl.assert_not_called()
        self.page.runJavaScript.assert_not_called()
        self.assertEqual(self.view._msg.id, 3)
        self.assertFalse(self.view.thread_shown)

    def test_late_patch_failure_cannot_navigate_away_from_new_message(self):
        self.page.runJavaScript.side_effect = None
        complete = replace(self.second, body_downloaded=True, body_text="Arrived body")
        self.view.show_thread(self.first, [self.first, complete], set(), keep_view=True)
        callback = self.page.runJavaScript.call_args.args[2]
        self.view.show_message(Message(id=3, body_text="Different message"))
        self.view.web.setUrl.reset_mock()
        callback(False)
        self.view.web.setUrl.assert_not_called()
        self.assertEqual(self.view._msg.id, 3)


class TestActualThreadDOM(unittest.TestCase):
    @unittest.skipUnless(viewer.HAS_WEBENGINE, "Qt WebEngine is unavailable")
    def test_expanded_quote_and_text_selection_survive_other_body_arrival(self):
        # A subprocess registers the URL scheme before QApplication; it is
        # isolated from existing offscreen/fallback Qt fixtures in this suite.
        script = textwrap.dedent('''
            import os,json
            from PySide6.QtCore import Qt,QEventLoop,QTimer
            from PySide6.QtWidgets import QApplication
            from mailclient.ui import viewer
            from mailclient.db import Message
            viewer.register_schemes()
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
            app=QApplication([])
            view=viewer.MessageView();view.resize(700,500);view.show()
            first=Message(id=1,body_html='<p>Message</p><blockquote>Selected quote text</blockquote>')
            second=Message(id=2,body_downloaded=False)
            def wait_until(predicate):
                loop=QEventLoop();poll=QTimer();poll.setInterval(20)
                poll.timeout.connect(lambda:loop.quit() if predicate() else None)
                poll.start();QTimer.singleShot(12000,loop.quit);loop.exec();poll.stop()
                assert predicate(),'Timed out'
            def js(script):
                result=[]
                view.web.page().runJavaScript(script,viewer.QWebEngineScript.ScriptWorldId.ApplicationWorld,
                                              lambda value:result.append(value))
                wait_until(lambda:bool(result));return result[0]
            view.show_thread(first,[first,second],set())
            wait_until(lambda:not view._loading)
            js("window.savedCard=document.getElementById('m1');window.savedQuote=savedCard.querySelector('details.quote');savedQuote.open=true;var r=document.createRange();r.selectNodeContents(savedQuote);var s=window.getSelection();s.removeAllRanges();s.addRange(r);window.savedSelection=s.toString();document.getElementById('m2').open=false;true;")
            before=view._counter
            second=Message(id=2,body_text='Background body')
            view.show_thread(first,[first,second],set(),keep_view=True)
            result=json.loads(js("JSON.stringify({sameCard:document.getElementById('m1')===window.savedCard,sameQuote:document.querySelector('#m1 details.quote')===window.savedQuote,quoteOpen:document.querySelector('#m1 details.quote').open,sameSelection:window.getSelection().toString()===window.savedSelection,secondBody:document.getElementById('m2').querySelector('.mb').textContent,secondOpen:document.getElementById('m2').open})"))
            result['noNavigation']=view._counter==before
            print(json.dumps(result),flush=True)
            view.shutdown();app.processEvents()
            os._exit(0)
        ''')
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                timeout=40, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(state, {"sameCard": True, "sameQuote": True, "quoteOpen": True,
                                 "sameSelection": True, "secondBody": "Background body",
                                 "secondOpen": False, "noNavigation": True})


if __name__ == "__main__":
    unittest.main()
