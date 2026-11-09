"""Run each test file in isolation, then safely terminate its retained Qt fixtures."""
import argparse
import fnmatch
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


TESTS = ROOT / "tests"


def test_files(pattern):
    """Match unittest discovery without importing every Qt test into the parent."""
    files = []
    for directory, subdirs, names in os.walk(TESTS):
        # unittest only descends into importable packages.
        subdirs[:] = sorted(name for name in subdirs if (Path(directory) / name / "__init__.py").is_file())
        files.extend(Path(directory) / name for name in sorted(names)
                     if name.endswith(".py") and fnmatch.fnmatch(name, pattern))
    return files


def run_file(path, report=None):
    suite = unittest.defaultTestLoader.discover(str(path.parent), pattern=path.name, top_level_dir=str(TESTS))
    # Имя каждого теста: вывод файла печатается только при ошибке, и по нему видно, где упало.
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    # UI fixtures can retain Python callbacks after their native owners close.
    # Destroying all of them a second time (or during interpreter finalization)
    # is unsafe. The CLI exits the isolated test process after workers finish;
    # real application shutdown is exercised by the installer smoke test.
    worker_error = ""
    widgets = sys.modules.get("PySide6.QtWidgets")
    if widgets is not None and widgets.QApplication.instance() is not None:
        from PySide6.QtCore import QThreadPool
        if not QThreadPool.globalInstance().waitForDone(5000):
            worker_error = "Tests left unfinished background tasks"
            print(worker_error, file=sys.stderr)
    if report:
        report.write_text(json.dumps({"tests": result.testsRun, "failures": len(result.failures),
                                      "errors": len(result.errors), "skipped": len(result.skipped),
                                      "unexpected_successes": len(result.unexpectedSuccesses),
                                      "worker_error": worker_error}), encoding="utf-8")
    return int(not result.wasSuccessful() or bool(worker_error))


def run_isolated(files, jobs, timeout):
    running = set()
    guard = threading.Lock()
    stopped = threading.Event()
    totals = dict(tests=0, failures=0, errors=0, skipped=0, unexpected_successes=0)
    failed_files = []
    started = time.monotonic()
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.setdefault("QT_QPA_PLATFORM", "offscreen")

    def stop_process(process):
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass  # it exited between poll and kill

    def run_one(path, report):
        if stopped.is_set():
            return path, 130, "Cancelled", None
        command = [sys.executable, str(Path(__file__).resolve()), "--single-file", str(path),
                   "--report", str(report)]
        # The lock prevents Ctrl+C from missing a worker while it is starting.
        with guard:
            if stopped.is_set():
                return path, 130, "Cancelled", None
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            running.add(process)
        try:
            try:
                output, _ = process.communicate(timeout=timeout)
                code = process.returncode
            except subprocess.TimeoutExpired:
                stop_process(process)
                output, _ = process.communicate()
                output += f"\nTest file exceeded {timeout:g}s timeout.\n"
                code = 124
        finally:
            with guard:
                running.discard(process)
        try:
            counts = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            counts = None
        return path, code, output, counts

    with tempfile.TemporaryDirectory(prefix="simplemail-tests-") as temporary:
        executor = ThreadPoolExecutor(max_workers=jobs)
        futures = {executor.submit(run_one, path, Path(temporary) / f"{index}.json"): path
                   for index, path in enumerate(files)}
        interrupted = False
        try:
            for future in as_completed(futures):
                path = futures[future]
                try:
                    _, code, output, counts = future.result()
                except Exception as error:
                    code, output, counts = 1, f"Test worker failed: {error}\n", None
                if counts is not None:
                    for key in totals:
                        totals[key] += counts.get(key, 0)
                success = (code == 0 and counts is not None and not counts.get("worker_error")
                           and not any(counts.get(key) for key in ("failures", "errors", "unexpected_successes")))
                name = path.relative_to(TESTS).as_posix()
                if success:
                    skipped = f", {counts['skipped']} skipped" if counts["skipped"] else ""
                    print(f"PASS {name}: {counts['tests']} tests{skipped}", flush=True)
                else:
                    failed_files.append(name)
                    exit_label = f"{code} (0x{code & 0xFFFFFFFF:08X})" if code not in (0, 1, 124, 130) else str(code)
                    print(f"FAIL {name}: exit {exit_label}", flush=True)
                    if counts is None:
                        print("Worker did not produce a complete test report.", flush=True)
                    print(output.rstrip(), flush=True)
        except KeyboardInterrupt:
            interrupted = True
            stopped.set()
            with guard:
                for process in running:
                    stop_process(process)
            print("Interrupted; stopping test workers.", file=sys.stderr, flush=True)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        if interrupted:
            return 130
    print(f"\nRan {totals['tests']} tests in {len(files)} isolated files in {time.monotonic() - started:.1f}s; "
          f"{totals['failures']} failures, {totals['errors']} errors, {totals['skipped']} skipped, "
          f"{totals['unexpected_successes']} unexpected successes, {len(failed_files)} failed files.", flush=True)
    if failed_files:
        print("FAILED files: " + ", ".join(failed_files), flush=True)
        return 1
    print("OK", flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--jobs", type=int, default=3, help="parallel test files (default: 3)")
    parser.add_argument("--timeout", type=float, default=300, help="timeout per isolated file in seconds")
    parser.add_argument("--single-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.jobs < 1 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--jobs and --timeout must be positive finite numbers")
    if args.single_file:
        # Аварийное завершение процесса (например, 0x80000003 из WebEngine) печатает
        # стек Python всех потоков — видно, на каком тесте и где упало.
        import faulthandler
        faulthandler.enable(all_threads=True)
        path = args.single_file.resolve()
        if not path.is_relative_to(TESTS) or not path.is_file():
            parser.error("test file must be inside tests/")
        return run_file(path, args.report)
    files = test_files(args.pattern)
    if not files:
        parser.error(f"no test files match {args.pattern!r}")
    if len(files) == 1:
        return run_file(files[0])
    return run_isolated(files, args.jobs, args.timeout)


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
