"""Published Windows releases; HTTPS download and mandatory SHA-256 verification."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__

REPOSITORY = "TimohovAS/SimpleMail"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_INSTALLER_SIZE = 1024 * 1024 * 1024
MAX_METADATA_SIZE = 2 * 1024 * 1024
RUNNING_MUTEX = r"Local\TimohovAS.SimpleMail.Running"


class UpdateError(Exception):
    pass


class DownloadCancelled(Exception):
    pass


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value):
        raise UpdateError("Invalid stable release version")
    return tuple(int(part) for part in value.split("."))


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    sha256: str
    size: int
    notes: str = ""

    @property
    def filename(self):
        return f"SimpleMail-Setup-{self.version}-x64.exe"

    def validate(self):
        version_tuple(self.version)
        urls = {f"{RELEASES_URL}/download/{tag}/{self.filename}"
                for tag in (self.version, "v" + self.version)}
        if self.url not in urls:
            raise UpdateError("Installer must belong to the official SimpleMail release")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise UpdateError("Release has no valid SHA-256 digest")
        if type(self.size) is not int or not 0 < self.size <= MAX_INSTALLER_SIZE:
            raise UpdateError("Invalid installer size")


class _HTTPSRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise UpdateError("Refusing an unencrypted redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url, accept="application/octet-stream"):
    request = Request(url, headers={"User-Agent": f"SimpleMail/{__version__}", "Accept": accept})
    return build_opener(_HTTPSRedirects()).open(request, timeout=30)


def parse_release(data, current=__version__) -> Release | None:
    if not isinstance(data, dict):
        raise UpdateError("Invalid release metadata")
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name", "")
    if not isinstance(tag, str):
        raise UpdateError("Invalid release tag")
    version = tag.removeprefix("v")
    if version_tuple(version) <= version_tuple(current):
        return None
    filename = f"SimpleMail-Setup-{version}-x64.exe"
    for asset in data.get("assets", []):
        if asset.get("name") != filename or asset.get("state") != "uploaded":
            continue
        digest = asset.get("digest") or ""
        if not digest.startswith("sha256:"):
            raise UpdateError("GitHub has not provided a SHA-256 digest for this installer")
        release = Release(version, asset.get("browser_download_url", ""),
                          digest[len("sha256:"):], asset.get("size"), (data.get("body") or "")[:20000])
        release.validate()
        return release
    raise UpdateError("The new release has no Windows x64 installer")


def check_update(current=__version__) -> Release | None:
    try:
        with _open(API_URL, "application/vnd.github+json") as response:
            raw = response.read(MAX_METADATA_SIZE + 1)
    except HTTPError as error:
        if error.code == 404:  # repository has not published its first release yet
            return None
        raise
    if len(raw) > MAX_METADATA_SIZE:
        raise UpdateError("Release metadata is too large")
    return parse_release(json.loads(raw), current)


def cache_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) / "SimpleMail" / "Updates"


def verify_installer(path: Path, release: Release) -> Path:
    release.validate()
    path = Path(path)
    if path.stat().st_size != release.size:
        raise UpdateError("Downloaded installer size does not match the release")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != release.sha256:
        raise UpdateError("Downloaded installer SHA-256 does not match the release")
    return path


def download_update(release: Release, directory=None, cancel=None, progress=None) -> Path:
    release.validate()
    cancel = cancel if cancel is not None else threading.Event()
    directory = Path(directory) if directory is not None else cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / release.filename
    if cancel.is_set():
        raise DownloadCancelled()
    if target.exists():
        try:
            return verify_installer(target, release)
        except UpdateError:
            pass  # replace a corrupt cache only after the new download is verified
    fd, name = tempfile.mkstemp(prefix="SimpleMail-", suffix=".part", dir=directory)
    partial = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream, _open(release.url) as response:
            length = response.headers.get("Content-Length")
            if length is not None and int(length) != release.size:
                raise UpdateError("Server returned an unexpected installer size")
            received = 0
            digest = hashlib.sha256()
            while True:
                if cancel.is_set():
                    raise DownloadCancelled()
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > release.size:
                    raise UpdateError("Download exceeds the expected installer size")
                stream.write(chunk)
                digest.update(chunk)
                if progress:
                    progress(str(received * 100 // release.size))
        if cancel.is_set():
            raise DownloadCancelled()
        if received != release.size or digest.hexdigest() != release.sha256:
            raise UpdateError("Incomplete or damaged download; SHA-256 verification failed")
        partial.replace(target)
        # Only obsolete installers in our dedicated cache; never mail or settings.
        for old in directory.glob("SimpleMail-Setup-*-x64.exe"):
            if old != target and re.fullmatch(r"SimpleMail-Setup-\d+\.\d+\.\d+-x64\.exe", old.name):
                try:
                    old.unlink()
                except OSError:
                    pass  # a previous installer may still be running
        return target
    finally:
        partial.unlink(missing_ok=True)


def installation_supported():
    return os.name == "nt" and bool(getattr(sys, "frozen", False))


def hold_installation_mutex():
    """Installer must never replace a running packaged application."""
    if not installation_supported():
        return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel.CreateMutexW(None, False, RUNNING_MUTEX)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle  # held by main until process exit, including shutdown of workers


def _installer_command(path, data_dir, ui_language):
    return [str(path), "/SP-", "/NORESTART", f"/DIR={Path(sys.executable).resolve().parent}",
               f"/MAILDATA={(Path(data_dir) / 'mail.db').resolve().parent}",
               "/LANG=" + ("serbianlatin" if ui_language == "sr_Latn" else "russian")]


def launch_installer(path, release, data_dir, ui_language="ru"):
    """Queue a standalone helper that waits for this process to fully terminate."""
    if not installation_supported():
        raise UpdateError("Installation requires the packaged Windows application")
    path = verify_installer(Path(path), release).resolve()
    command = _installer_command(path, data_dir, ui_language)
    directory = cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="install-", suffix=".json", dir=directory)
    request = Path(name)
    helper = request.with_suffix(".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"parent_pid": os.getpid(), "installer": str(path),
                       "sha256": release.sha256, "size": release.size,
                       "arguments": command[1:]}, stream)
        shutil.copyfile(Path(__file__).with_name("update_helper.ps1"), helper)
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        helper_command = [str(powershell), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                          "-ExecutionPolicy", "Bypass", "-File", str(helper), "-RequestPath", str(request)]
        return _start_update_helper(helper_command, directory)
    except Exception:
        helper.unlink(missing_ok=True)
        request.unlink(missing_ok=True)
        raise


def _start_update_helper(command, directory):
    # Do not let PyInstaller's DLL search directory affect Inno Setup.
    ctypes.windll.kernel32.SetDllDirectoryW(None)
    environment = os.environ.copy()
    bundled = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    environment["PATH"] = os.pathsep.join(
        entry for entry in environment.get("PATH", "").split(os.pathsep)
        if entry and not Path(entry).resolve().is_relative_to(bundled))
    return subprocess.Popen(command, env=environment, cwd=directory, close_fds=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
