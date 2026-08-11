"""
Retry every (branch, date) pair currently listed in downloads/failed_dates.txt.

Reuses bulk_download.py's account routing, folder auto-routing, download
logic, and concurrent worker pool so behavior stays identical to a normal
run — just against a much shorter job list.

Usage:
    py retry_failed.py
    py retry_failed.py --concurrency 8

After running, failed_dates.txt is rewritten to contain only whatever still
failed (deduped) — successes are removed.
"""

import argparse
import asyncio
import sys
from datetime import date as date_cls
from pathlib import Path

from bulk_download import (
    DEFAULT_BASE_DIR,
    DEFAULT_CONCURRENCY,
    SESSION_FILE,
    resolve_download_dir,
    run_jobs,
)

ACCOUNT_IDS = {
    "al-quoz": "52308",
    "khalifa-city": "36539",
    "motor-city": "46729",
    "saadiyat": "36629",
    "fratelli": "59766",
}


def load_failed(failed_log: Path):
    if not failed_log.exists():
        return []
    pairs = []
    seen = set()
    for line in failed_log.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        prefix, date_str = line.split()
        if (prefix, date_str) not in seen:
            seen.add((prefix, date_str))
            pairs.append((prefix, date_str))
    return pairs


async def main_async(args):
    base_dir = DEFAULT_BASE_DIR
    failed_log = base_dir / "failed_dates.txt"

    pairs = load_failed(failed_log)
    if not pairs:
        print("No failed dates to retry.")
        return

    unknown = sorted({p for p, _ in pairs if p not in ACCOUNT_IDS})
    if unknown:
        print(f"ERROR: no account ID mapped for prefix(es): {unknown}")
        sys.exit(1)

    if not SESSION_FILE.exists():
        print(f"ERROR: session file not found at {SESSION_FILE}. Run save_session.py first.")
        sys.exit(1)

    print(f"Retrying {len(pairs)} failed date(s)...")

    jobs = []
    for prefix, date_str in pairs:
        account_id = ACCOUNT_IDS[prefix]
        d = date_cls.fromisoformat(date_str)
        download_dir = resolve_download_dir(base_dir, prefix, d)
        jobs.append((account_id, prefix, date_str, download_dir))

    results = await run_jobs(jobs, concurrency=args.concurrency, headless=not args.show)

    if results["failed"]:
        with open(failed_log, "w") as f:
            for prefix, date_str in results["failed"]:
                f.write(f"{prefix} {date_str}\n")
        print(f"\n{len(results['ok'])} succeeded, {len(results['failed'])} still failed. See {failed_log}")
    else:
        failed_log.unlink(missing_ok=True)
        print(f"\nAll {len(results['ok'])} retried date(s) succeeded. failed_dates.txt cleared.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument(
        "--show",
        action="store_true",
        help="Run with a visible browser window instead of headless (slower, useful for debugging).",
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
