"""Передача уведомлений IDLE из фоновых потоков в поток окна Qt."""
from PySide6.QtCore import QObject, Signal

from ..idle import MailWatcher


class MailWatch(QObject):
    changed = Signal(int)
    unavailable = Signal(int)

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.watchers = []
        self._generation = 0

    def restart(self):
        self.stop()
        generation = self._generation

        def emit_if_current(signal, account_id):
            if generation == self._generation:
                signal.emit(account_id)

        for acc in self.db.accounts(only_enabled=True):
            watcher = MailWatcher(
                self.db, acc,
                lambda aid: emit_if_current(self.changed, aid),
                lambda aid: emit_if_current(self.unavailable, aid),
            )
            self.watchers.append(watcher)
            watcher.start()

    def stop(self):
        self._generation += 1
        for watcher in self.watchers:
            watcher.stop()
        self.watchers.clear()
