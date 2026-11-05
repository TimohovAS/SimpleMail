# -*- mode: python ; coding: utf-8 -*-
"""Windows onedir bundle, включая Qt WebEngine, переводы и Windows keyring."""
from pathlib import Path
import re
from PyInstaller.utils.hooks import copy_metadata, get_package_paths

root = Path(SPECPATH)
data_files = [
    (str(root / "mailclient" / "icons"), "mailclient/icons"),
    (str(root / "mailclient" / "locales"), "mailclient/locales"),
] + copy_metadata("keyring")

analysis = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=[],
    datas=data_files,
    hiddenimports=["keyring.backends.Windows"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
# Python и Qt могут поставлять разные версии VC Runtime. На верхнем уровне
# используем комплект Qt: Windows загружает эти DLL раньше вложенных копий.
qt_dir = Path(get_package_paths("PySide6")[1])
runtime_files = list(qt_dir.glob("msvcp140*.dll")) + list(qt_dir.glob("vcruntime140*.dll"))
runtime_names = {path.name.lower() for path in runtime_files}
analysis.binaries = [entry for entry in analysis.binaries
                     if not (Path(entry[0]).parent == Path(".") and Path(entry[0]).name.lower() in runtime_names)]
analysis.binaries += [(path.name, str(path), "BINARY") for path in runtime_files]
# Qt для Windows использует системный ICU с невeрсионированными именами API.
# ICU из Poppler/Git, случайно найденный через PATH, с ним несовместим.
analysis.binaries = [entry for entry in analysis.binaries
                     if not re.fullmatch(r"icu(?:uc|in|dt\d+)\.dll", Path(entry[0]).name, re.IGNORECASE)]
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, [],
    exclude_binaries=True,
    name="SimpleMail",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(root / "mailclient" / "icons" / "simplemail.ico"),
    version=str(root / "tools" / "windows-version.txt"),
    contents_directory="_internal",
)
collection = COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name="SimpleMail")
