"""Fail a build when source, Windows resource, or release tag versions disagree."""
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from mailclient import __version__


def check():
    if not re.fullmatch(r"\d+\.\d+\.\d+", __version__):
        raise ValueError("Expected a stable SemVer version")
    resource = (root / "tools" / "windows-version.txt").read_text(encoding="utf-8")
    numeric = tuple(map(int, __version__.split("."))) + (0,)
    for field in ("filevers", "prodvers"):
        value = re.search(rf"{field}=\(([^)]+)\)", resource)
        if value is None or tuple(map(int, value[1].split(","))) != numeric:
            raise ValueError(f"{field} does not match {__version__}")
    for field in ("FileVersion", "ProductVersion"):
        if f"StringStruct('{field}', '{__version__}')" not in resource:
            raise ValueError(f"{field} does not match {__version__}")
    if len(sys.argv) > 1 and sys.argv[1] != "v" + __version__:
        raise ValueError("Release tag does not match the application version")
    print(f"Version OK: {__version__}")


if __name__ == "__main__":
    check()
