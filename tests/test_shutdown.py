"""Shutdown covers both pools and cannot crash in Windows Qt DLL detach."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from mailclient import shutdown
from mailclient.ui import worker


ROOT = Path(__file__).resolve().parents[1]


class WorkerShutdownTests(unittest.TestCase):
    def test_local_and_network_workers_share_one_deadline(self):
        local, network = Mock(), Mock()
        local.waitForDone.return_value = True
        network.waitForDone.return_value = False
        with patch.object(worker, '_local_pool', local), \
                patch('PySide6.QtCore.QThreadPool.globalInstance', return_value=network), \
                patch.object(shutdown.time, 'monotonic', side_effect=[10.0, 10.5, 12.5]):
            self.assertFalse(shutdown.wait_for_workers(3000))
        local.waitForDone.assert_called_once_with(2500)
        network.waitForDone.assert_called_once_with(500)

    def test_unfinished_local_task_prevents_normal_shutdown(self):
        local, network = Mock(), Mock()
        local.waitForDone.return_value = False
        network.waitForDone.return_value = True
        with patch.object(worker, '_local_pool', local), \
                patch('PySide6.QtCore.QThreadPool.globalInstance', return_value=network):
            self.assertFalse(shutdown.wait_for_workers(0))
        local.waitForDone.assert_called_once_with(0)
        network.waitForDone.assert_called_once_with(0)

    def test_unused_local_pool_is_not_created(self):
        network = Mock()
        network.waitForDone.return_value = True
        with patch.object(worker, '_local_pool', None), \
                patch.object(worker, 'local_pool', side_effect=AssertionError('Created a new pool')), \
                patch('PySide6.QtCore.QThreadPool.globalInstance', return_value=network):
            self.assertTrue(shutdown.wait_for_workers(0))

    def test_actual_qt_process_exits_with_hanging_worker_and_preserves_saved_state(self):
        script = r'''
import os, sys, threading
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QRunnable, QThreadPool
from mailclient.shutdown import force_exit, wait_for_workers
from mailclient.ui.worker import local_pool

app = QCoreApplication([])
started = threading.Event()
gate = threading.Event()
class Hanging(QRunnable):
    def run(self):
        started.set()
        gate.wait(30)

task = Hanging()
pool = local_pool() if sys.argv[2] == 'local' else QThreadPool.globalInstance()
pool.start(task)
if not started.wait(5):
    force_exit(4)
if wait_for_workers(100):
    force_exit(5)
with Path(sys.argv[1]).open('w', encoding='utf-8') as marker:
    marker.write('state saved before forced exit')
    marker.flush()
    os.fsync(marker.fileno())
force_exit(int(sys.argv[3]))
'''
        with tempfile.TemporaryDirectory(prefix='simplemail-shutdown-test-') as temporary:
            marker = Path(temporary) / 'saved.txt'
            for pool, exit_code in [('network', 0), ('local', 7)]:
                with self.subTest(pool=pool):
                    result = subprocess.run(
                        [sys.executable, '-c', script, str(marker), pool, str(exit_code)],
                        cwd=ROOT, env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
                        capture_output=True, text=True, timeout=10,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                    self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
                    self.assertEqual(marker.read_text(encoding='utf-8'), 'state saved before forced exit')
                    marker.unlink()


if __name__ == '__main__':
    unittest.main()
