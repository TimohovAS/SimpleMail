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
    if os.name == "nt":
        # The Windows CRT's _exit still runs DLL detach hooks. Qt's hooks can
        # call retained Python fixtures during that phase, after Python has
        # released their native owners. Terminate only this completed test
        # process, with the actual unittest result and all workers drained.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL
        if not kernel.TerminateProcess(kernel.GetCurrentProcess(), code):
            raise ctypes.WinError(ctypes.get_last_error())
    os._exit(code)
