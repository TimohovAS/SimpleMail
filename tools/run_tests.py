"""Run tests, drain workers and exit without tearing down retained Qt fixtures."""
import argparse
import os
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
    # UI fixtures can retain Python callbacks after their native owners close.
    # Destroying all of them a second time (or during interpreter finalization)
    # is unsafe. The CLI exits the isolated test process after workers finish;
    # real application shutdown is exercised by the installer smoke test.
    widgets = sys.modules.get("PySide6.QtWidgets")
    if widgets is not None and (app := widgets.QApplication.instance()) is not None:
        from PySide6.QtCore import QThreadPool
        if not QThreadPool.globalInstance().waitForDone(5000):
            raise RuntimeError("Tests left unfinished background tasks")
    return int(not result.wasSuccessful())


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
