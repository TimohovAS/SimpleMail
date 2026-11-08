"""Отчёт о размере собранной программы: что занимает место в установщике.

python tools/bundle_report.py dist/installer-build/SimpleMail [dist/installer]
"""
import sys
from collections import Counter
from pathlib import Path


def mb(size):
    return f"{size / 1e6:8.1f} MB"


def main(bundle, installer_dir=None):
    root = Path(bundle)
    files = [p for p in root.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    print(f"Bundle: {len(files)} files, {mb(total)}")
    groups = Counter()
    for p in files:
        parts = p.relative_to(root).parts
        groups["/".join(parts[:3]) if len(parts) > 3 else "/".join(parts[:-1]) or "."] += p.stat().st_size
    print("Largest folders:")
    for name, size in groups.most_common(25):
        print(f"  {mb(size)}  {name}")
    print("Largest files:")
    for p in sorted(files, key=lambda p: -p.stat().st_size)[:40]:
        print(f"  {mb(p.stat().st_size)}  {p.relative_to(root).as_posix()}")
    if installer_dir:
        for p in sorted(Path(installer_dir).glob("*.exe")):
            print(f"Installer: {p.name} {mb(p.stat().st_size)}")


if __name__ == "__main__":
    main(*sys.argv[1:])
