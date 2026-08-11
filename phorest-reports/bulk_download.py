r"""
Bulk-download Staff Performance Overview reports from Phorest, one PDF per
day, for a given branch/account and date range. Report requests run
concurrently across a pool of pages sharing one logged-in browser session,
instead of one date at a time — this is the main speedup over looping
sequentially.

Requires phorest_session.json to already exist (run save_session.py once
first to log in manually and capture the session). The same session works
across branches as long as the logged-in user has access to that account.

Usage:
    py bulk_download.py --account 52308 --prefix al-quoz --start 2026-03-01 --end 2026-03-31
    py bulk_download.py --account 52308 --prefix al-quoz --start 2026-03-01 --end 2026-03-31 --concurrency 8

Files are auto-routed into Kate's existing folder structure based on branch,
year, and month — no --outdir needed for normal use:

    <base>/<branch folder>/<year> <branch folder>/<MM><year> <branch folder>/<prefix>-staff-performance-YYYY-MM-DD.pdf

e.g. --prefix al-quoz, date 2026-03-15 ->
    downloads/al quoz/2026 al quoz/032026 al quoz/al-quoz-staff-performance-2026-03-15.pdf

<base> defaults to the shared downloads/ folder next to this script. Override
with --base-outdir if you ever need a different root. Add a new branch to
BRANCH_FOLDERS below if its folder name doesn't match its --prefix.

A date range spanning multiple months routes each date into its own correct
month folder automatically.

Flow per date (run concurrently, --concurrency dates in flight at once):
    1. Build the jasper-reports URL with FROM_DATE/TO_DATE set to that date.
    2. Navigate there (loads the given account, pre-fills the date pickers).
    3. Click "Generate now".
    4. Wait for the SPA to route to /report-viewer?reportId=...&reportUrl=...
    5. Pull the reportUrl (a presigned S3 link straight to the PDF) out of
       that URL and fetch it directly with a plain HTTP GET.
    6. Save it to the auto-resolved folder above.

Failures on any single date are logged to failed_dates.txt (in the base
folder) and skipped — the run continues rather than crashing.
"""

import argparse
import asyncio
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

SCRIPT_DIR = Path(__file__).parent
SESSION_FILE = SCRIPT_DIR / "phorest_session.json"
DEFAULT_BASE_DIR = SCRIPT_DIR / "downloads"
DEFAULT_CONCURRENCY = 5

# Maps --prefix to the folder name Kate actually uses on disk, where they differ.
# Add new branches here as needed.
BRANCH_FOLDERS = {
    "al-quoz": "al quoz",
    "khalifa-city": "khalifa",
    "motor-city": "motor city",
    "saadiyat": "saadiyat",
}


def resolve_download_dir(base_dir: Path, prefix: str, d: date) -> Path:
    branch_folder = BRANCH_FOLDERS.get(prefix, prefix)
    year_folder = f"{d.year} {branch_folder}"
    month_folder = f"{d.month:02d}{d.year} {branch_folder}"
    return base_dir / branch_folder / year_folder / month_folder


def build_report_url(account_id: str, date_str: str) -> str:
    filters = {
        "FROM_DATE": date_str,
        "TO_DATE": date_str,
        "excludeSelfEmployedStaff": False,
        "top_n": 4000,
    }
    encoded_filters = urllib.parse.quote(json.dumps(filters, separators=(",", ":")))
    base_url = f"https://my.phorest.com/a/{account_id}/jasper-reports"
    return f"{base_url}?category=STAFF&filters={encoded_filters}&report=STAFF_PERFORMANCE_OVERVIEW"


def daterange(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def download_pdf(url: str, dest: Path):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    if not data.startswith(b"%PDF"):
        raise ValueError("response was not a valid PDF (session may have expired)")
    dest.write_bytes(data)


async def process_date(page, account_id: str, prefix: str, date_str: str, download_dir: Path):
    url = build_report_url(account_id, date_str)
    report_viewer_pattern = re.compile(rf"https://my\.phorest\.com/a/{account_id}/report-viewer\?reportId=")
    # Track popups spawned by THIS worker's page only. Never scan
    # context.pages: with concurrent workers every tab in the context is
    # visible to every worker, and whoever polls first steals whichever
    # viewer appears — that's exactly how the 10 Aug 2026 range downloads
    # ended up with filenames pointing at the wrong dates. A popup event on
    # this page can only belong to this worker's own Generate click.
    popups = []
    on_popup = lambda p: popups.append(p)
    page.on("popup", on_popup)
    try:
        await page.goto(url, wait_until="load", timeout=45000)

        generate_btn = page.get_by_role("button", name="Generate now")
        await generate_btn.wait_for(state="visible", timeout=30000)
        await generate_btn.click()

        # As of 10 Aug 2026 Phorest may open the viewer in a new tab instead
        # of replacing this one, and generation can outlive a 60s wait — so
        # poll this page's URL plus its OWN popups, re-click once early in
        # case the first click landed before the page was interactive, and
        # give slow generation a much longer total budget.
        viewer_page = None
        reclicked = False
        for elapsed in range(180):
            for p in [page] + popups:
                if not p.is_closed() and report_viewer_pattern.search(p.url):
                    viewer_page = p
                    break
            if viewer_page:
                break
            if elapsed == 15 and not reclicked:
                reclicked = True
                try:
                    await generate_btn.click()
                except Exception:
                    pass
            await asyncio.sleep(1)
        if not viewer_page:
            raise TimeoutError("timed out waiting for report")

        query = urllib.parse.urlparse(viewer_page.url).query
        params = urllib.parse.parse_qs(query)
        report_url = params.get("reportUrl", [None])[0]
        if not report_url:
            raise ValueError("reportUrl param missing from report-viewer URL")

        download_dir.mkdir(parents=True, exist_ok=True)
        dest = download_dir / f"{prefix}-staff-performance-{date_str}.pdf"
        await asyncio.to_thread(download_pdf, report_url, dest)
        if viewer_page is not page:
            # Viewer opened as a separate tab — close it so the worker's own
            # page can be reused without stray tabs piling up.
            try:
                await viewer_page.close()
            except Exception:
                pass
        return dest
    except Exception:
        try:
            download_dir.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(download_dir / f"_debug_failure_{prefix}_{date_str}.png"))
        except Exception:
            pass
        raise
    finally:
        try:
            page.remove_listener("popup", on_popup)
        except Exception:
            pass
        # Close any leftover popups this job spawned but didn't use.
        for p in popups:
            if not p.is_closed():
                try:
                    await p.close()
                except Exception:
                    pass


async def _worker(context, queue: asyncio.Queue, results: dict, stagger: float):
    # Small stagger so concurrent workers don't all hit "Generate now" in the
    # same instant on startup.
    await asyncio.sleep(stagger)
    page = await context.new_page()
    try:
        while True:
            try:
                job = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            account_id, prefix, date_str, download_dir = job
            try:
                dest = await process_date(page, account_id, prefix, date_str, download_dir)
                print(f"[OK]   {prefix} {date_str} -> {dest}")
                results["ok"].append((prefix, date_str))
            except PlaywrightTimeoutError:
                print(f"[FAIL] {prefix} {date_str} -> timed out waiting for report")
                results["failed"].append((prefix, date_str))
            except Exception as e:
                print(f"[FAIL] {prefix} {date_str} -> {e}")
                results["failed"].append((prefix, date_str))
    finally:
        await page.close()


async def run_jobs(jobs: list, concurrency: int = DEFAULT_CONCURRENCY, headless: bool = True) -> dict:
    """Run (account_id, prefix, date_str, download_dir) jobs across a pool of
    concurrent pages sharing one logged-in browser context."""
    queue: asyncio.Queue = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)

    results = {"ok": [], "failed": []}
    n_workers = max(1, min(concurrency, len(jobs)))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(storage_state=str(SESSION_FILE))
        workers = [
            asyncio.create_task(_worker(context, queue, results, stagger=i * 0.4))
            for i in range(n_workers)
        ]
        await asyncio.gather(*workers)
        await browser.close()

    return results


async def main_async(args):
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    base_dir = Path(args.base_outdir)
    failed_log = base_dir / "failed_dates.txt"

    if not SESSION_FILE.exists():
        print(f"ERROR: session file not found at {SESSION_FILE}. Run save_session.py first.")
        sys.exit(1)

    base_dir.mkdir(parents=True, exist_ok=True)

    jobs = [
        (args.account, args.prefix, d.isoformat(), resolve_download_dir(base_dir, args.prefix, d))
        for d in daterange(start, end)
    ]

    results = await run_jobs(jobs, concurrency=args.concurrency, headless=not args.show)

    if results["failed"]:
        with open(failed_log, "a") as f:
            for prefix, date_str in results["failed"]:
                f.write(f"{prefix} {date_str}\n")
        print(f"\n{len(results['ok'])} succeeded, {len(results['failed'])} failed. See {failed_log}")
    else:
        print(f"\nAll {len(results['ok'])} date(s) processed successfully.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True, help="Phorest account/business ID, e.g. 52308")
    parser.add_argument("--prefix", required=True, help="Filename prefix, e.g. al-quoz")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--base-outdir",
        default=str(DEFAULT_BASE_DIR),
        help="Root folder under which branch/year/month subfolders are auto-created. "
        "Defaults to ./downloads next to this script.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Number of report requests to run in parallel (default {DEFAULT_CONCURRENCY}). "
        "Higher is faster but hits Phorest harder — dial back if you see failures.",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Run with a visible browser window instead of headless (slower, useful for debugging).",
    )
    parser.add_argument(
        "--no-collect",
        action="store_true",
        help="Skip the _UPLOAD-ME staging folder that is normally built after the run.",
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))

    if not args.no_collect:
        import collect_today

        collect_today.collect()


if __name__ == "__main__":
    main()
