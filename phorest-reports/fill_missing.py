"""
Fill in only the missing dates for a branch/date-range — scans the
auto-routed folder structure for PDFs that already exist and skips them,
so re-running this is always safe/cheap.

Usage:
    py fill_missing.py --account 46729 --prefix motor-city --start 2026-02-01 --end 2026-07-31
"""

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

from bulk_download import DEFAULT_CONCURRENCY, DEFAULT_BASE_DIR, SESSION_FILE, resolve_download_dir, run_jobs


def daterange(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


async def main_async(args):
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    base_dir = Path(args.base_outdir)
    failed_log = base_dir / "failed_dates.txt"

    if not SESSION_FILE.exists():
        print(f"ERROR: session file not found at {SESSION_FILE}. Run save_session.py first.")
        sys.exit(1)

    jobs = []
    skipped = 0
    for d in daterange(start, end):
        download_dir = resolve_download_dir(base_dir, args.prefix, d)
        dest = download_dir / f"{args.prefix}-staff-performance-{d.isoformat()}.pdf"
        if dest.exists():
            skipped += 1
            continue
        jobs.append((args.account, args.prefix, d.isoformat(), download_dir))

    print(f"{args.prefix}: {skipped} already present, {len(jobs)} missing -> fetching now")
    if not jobs:
        return

    results = await run_jobs(jobs, concurrency=args.concurrency, headless=not args.show)

    if results["failed"]:
        with open(failed_log, "a") as f:
            for prefix, date_str in results["failed"]:
                f.write(f"{prefix} {date_str}\n")
        print(f"\n{args.prefix}: {len(results['ok'])} succeeded, {len(results['failed'])} failed. See {failed_log}")
    else:
        print(f"\n{args.prefix}: all {len(results['ok'])} missing date(s) filled successfully.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--base-outdir", default=str(DEFAULT_BASE_DIR))
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
