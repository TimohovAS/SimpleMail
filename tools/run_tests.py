"""Run tests and release native Qt resources before Python finalization."""
import argparse
import gc
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pattern", default="test*.py")
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=args.pattern)
    result = unittest.TextTestRunner().run(suite)
    # Some tests deliberately retain windows/signals in class fixtures. Destroy
    # them while Python callbacks are still usable, before Qt's atexit cleanup.
    widgets = sys.modules.get("PySide6.QtWidgets")
    if widgets is not None and (app := widgets.QApplication.instance()) is not None:
        from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool
        if not QThreadPool.globalInstance().waitForDone(5000):
            raise RuntimeError("Tests left unfinished background tasks")
        app.processEvents()
        for window in app.topLevelWidgets():
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.clipboard().clear()
        app.processEvents()
        gc.collect()
        app.shutdown()
    return int(not result.wasSuccessful())


if __name__ == "__main__":
    raise SystemExit(main())
