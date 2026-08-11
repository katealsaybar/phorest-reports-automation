"""Collect today's downloaded PDFs into one _UPLOAD-ME folder inside downloads/.

Copies, never moves — the branch folders keep their originals. The folder is
named from the REPORT dates in the filenames (same as "_UPLOAD-ME aug 06-09"),
not from the day you happened to download them.

bulk_download.py runs this automatically when it finishes, so normally you
never call it by hand. Run it directly only to catch up on a past day:

    python collect_today.py            # PDFs downloaded today
    python collect_today.py 2026-08-10 # PDFs downloaded on a past date
"""

import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec"]
DATE_IN_NAME = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def find_downloads(root: Path) -> Path:
    """downloads/ sits either beside this script or one folder deeper."""
    direct = root / "downloads"
    if direct.is_dir():
        return direct
    nested = sorted(root.glob("*/downloads"))
    if nested:
        return nested[0]
    sys.exit(f"No downloads/ folder found under {root}")


def report_date(pdf: Path):
    m = DATE_IN_NAME.search(pdf.name)
    if not m:
        return None
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def folder_label(dates) -> str:
    """'aug 10' for one date, 'aug 06-09' for a range, 'aug 30-sep 02' across months."""
    lo, hi = min(dates), max(dates)
    if lo == hi:
        return f"{MONTHS[lo.month - 1]} {lo.day:02d}"
    if lo.month == hi.month:
        return f"{MONTHS[lo.month - 1]} {lo.day:02d}-{hi.day:02d}"
    return (f"{MONTHS[lo.month - 1]} {lo.day:02d}-"
            f"{MONTHS[hi.month - 1]} {hi.day:02d}")


def collect(when=None):
    """Stage the PDFs downloaded on `when` (default today). Safe to re-run."""
    when = when or date.today()

    root = Path(__file__).resolve().parent
    downloads = find_downloads(root)

    found = []
    for pdf in downloads.rglob("*.pdf"):
        # Skip copies already sitting in a previous batch folder.
        if any(p.startswith("_UPLOAD-ME") for p in pdf.parts):
            continue
        if date.fromtimestamp(pdf.stat().st_mtime) == when:
            found.append(pdf)

    if not found:
        print(f"No PDFs downloaded on {when} under {downloads}")
        return

    dates = [d for d in (report_date(p) for p in found) if d]
    label = folder_label(dates) if dates else when.strftime("%b %d").lower()

    target = downloads / f"_UPLOAD-ME {label}"
    target.mkdir(exist_ok=True)

    copied = skipped = 0
    for pdf in sorted(found):
        dest = target / pdf.name
        if dest.exists():
            skipped += 1
            continue
        shutil.copy2(pdf, dest)
        copied += 1

    print(f"{target}")
    print(f"  {copied} copied, {skipped} already there "
          f"({len(found)} downloaded on {when})")


def main():
    when = (datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
            if len(sys.argv) > 1 else None)
    collect(when)


if __name__ == "__main__":
    main()
