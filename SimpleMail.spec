# -*- mode: python ; coding: utf-8 -*-
"""Windows onedir bundle, включая Qt WebEngine, переводы и Windows keyring."""
from pathlib import Path
import re
from PyInstaller.utils.hooks import copy_metadata, get_package_paths

root = Path(SPECPATH)
data_files = [
    (str(root / "mailclient" / "icons"), "mailclient/icons"),
    (str(root / "mailclient" / "locales"), "mailclient/locales"),
    (str(root / "mailclient" / "update_helper.ps1"), "mailclient"),
] + copy_metadata("keyring") + copy_metadata("msal") + copy_metadata("msal-extensions")
# Client ID Google/Microsoft для входа без настройки (файл не хранится в git, см. README).
if (root / "mailclient" / "oauth_clients.json").is_file():
    data_files.append((str(root / "mailclient" / "oauth_clients.json"), "mailclient"))

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

# --- Размер установщика -------------------------------------------------------
# PyInstaller забирает из PySide6 всё, что связано с QtWebEngine: QML-модули
# (а через них Quick3D, Charts, Graphs, Quick Controls), инструменты разработчика
# Chromium, отладочные ресурсы и переводы на все языки. Программе это не нужно.
QT_PLUGINS = {"platforms/qwindows.dll", "styles", "imageformats", "iconengines", "tls", "networkinformation"}
QT_PLUGINS_DROPPED = {"imageformats/qpdf.dll"}  # тянет Qt6Pdf (PDF в письмах не показываем)
WEBENGINE_LOCALES = {"en-US.pak", "ru.pak", "sr.pak"}
QT_TRANSLATIONS = {"qtbase_ru.qm"}  # i18n.install_qt_translations


def _qt_path(dest):
    """Путь внутри PySide6 ('plugins/styles/x.dll') или None для остальных файлов."""
    parts = Path(dest).parts
    return "/".join(parts[1:]).lower() if len(parts) > 1 and parts[0] == "PySide6" else None


def _qt_needed(dest):
    rel = _qt_path(dest)
    if rel is None:
        return True
    name = rel.rsplit("/", 1)[-1]
    if rel.startswith("qml/"):
        return False  # QWebEngineView в виджетах QML не использует
    if rel.startswith("plugins/"):
        plugin = rel[len("plugins/"):]
        return plugin not in QT_PLUGINS_DROPPED and any(
            plugin == keep or plugin.startswith(keep + "/") for keep in QT_PLUGINS)
    if rel.startswith("translations/qtwebengine_locales/"):
        return name in {n.lower() for n in WEBENGINE_LOCALES}
    if rel.startswith("translations/"):
        return name in QT_TRANSLATIONS
    if rel.startswith("resources/"):
        # Отладочные копии и DevTools (инспектор страниц) не используются.
        return ".debug." not in name and not name.startswith("qtwebengine_devtools_resources")
    return True


def _pe_imports(path):
    import pefile
    pe = pefile.PE(path, fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                                           pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
    names = {entry.dll.decode().lower() for entry in
             getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) + getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", [])}
    pe.close()
    return names


analysis.datas = [entry for entry in analysis.datas if _qt_needed(entry[0])]
analysis.binaries = [entry for entry in analysis.binaries if _qt_needed(entry[0])]
# Библиотеки Qt6*.dll оставляем, только если их загружает что-то оставшееся
# (модули .pyd, плагины, QtWebEngineProcess.exe и сами библиотеки Qt),
# иначе их приносили только выброшенные QML-модули.
qt_libraries = {Path(entry[0]).name.lower(): entry for entry in analysis.binaries
                if _qt_path(entry[0]) is not None and re.fullmatch(r"qt6\w+\.dll", Path(entry[0]).name, re.IGNORECASE)}
pending = [entry[1] for entry in analysis.binaries if Path(entry[0]).name.lower() not in qt_libraries]
used = set()
while pending:
    for name in _pe_imports(pending.pop()):
        if name in qt_libraries and name not in used:
            used.add(name)
            pending.append(qt_libraries[name][1])
analysis.binaries = [entry for entry in analysis.binaries
                     if Path(entry[0]).name.lower() not in qt_libraries or Path(entry[0]).name.lower() in used]
for required in ("qt6webenginecore.dll", "qt6webenginewidgets.dll", "qt6svg.dll", "qt6network.dll"):
    if required not in used:
        raise SystemExit(f"Qt library {required} was dropped from the bundle")
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
