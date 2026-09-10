r"""
Bulk-download Phorest "Additional reports" PDFs, one per day, for a given
branch/account and date range — the one downloader for every report type
this pipeline uses (staff-performance, financial-totals, sales-transactions,
staff-utilisation). Report requests run concurrently across a pool of pages
sharing one logged-in browser session, instead of one date at a time. This
is the main speedup over looping sequentially.

Requires phorest_session.json to already exist (run save_session.py once
first to log in manually and capture the session). The same session works
across branches AND across every report type below, as long as the logged-in
user has access to that account — one login for all of it.

Usage:
    py bulk_download.py --account 52308 --prefix al-quoz --start 2026-01-01 --end 2026-01-31
    py bulk_download.py --account 52308 --prefix al-quoz --start 2026-01-01 --end 2026-01-31 --report financial-totals
    py bulk_download.py --account 52308 --prefix al-quoz --start 2026-01-01 --end 2026-01-31 --concurrency 8
    py bulk_download.py --retry-failed --report staff-utilisation

Flow per date (run concurrently, --concurrency dates in flight at once):
    1. Build the jasper-reports URL with FROM_DATE/TO_DATE set to that date
       (plus whatever other filters that report needs — see REPORTS below).
    2. Navigate there (loads the given account, pre-fills the date pickers).
    3. Click "Generate now".
    4. Wait for the SPA to route to /report-viewer?reportId=...&reportUrl=...
       — either on the same page, or (staff-utilisation only) a popup tab.
    5. Pull the reportUrl (a presigned S3 link straight to the PDF) out of
       that URL and fetch it directly with a plain HTTP GET.
    6. Save it under that report's own folder (see REPORTS below) — either a
       flat downloads/ mirrored into a branch/year/month tree, or (for
       staff-utilisation) straight into the branch/year/month tree with no
       separate flat copy, matching how collect_today.py already expects to
       find it.

Dates whose PDF already exists are skipped, so re-running a range is cheap
and safe. Pass --force to re-download them anyway.

Each date gets --attempts tries (default 3) before it's given up on.
Failures are logged to failed_dates.txt (inside that report's own folder)
and skipped, so the run continues rather than crashing. Re-run everything in
that log with --retry-failed.

Phorest's access token only lives 30 minutes; the browser silently refreshes
it from the (30-day) refresh token on page load. This script writes the
refreshed session back to phorest_session.json at the end of every run, so
the saved session keeps working without re-logging in. If the refresh token
itself has expired you'll see "session expired": run save_session.py again.

See "../HOW-DOWNLOADS-WORK.md" for the full picture (branches, report specs,
folder layout, how to add a new report type).
"""

import argparse
import asyncio
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

SCRIPT_DIR = Path(__file__).parent
SESSION_FILE = SCRIPT_DIR / "phorest_session.json"
DEFAULT_CONCURRENCY = 5
REPORTS_BASE = SCRIPT_DIR.parent

# --prefix -> the branch-folder name actually used on disk, where it differs
# from the prefix itself. Add new branches here as needed.
BRANCH_FOLDERS = {
    "al-quoz": "al quoz",
    "khalifa-city": "khalifa",
    "motor-city": "motor city",
    "saadiyat": "saadiyat",
}

ACCOUNT_IDS = {
    "al-quoz": "52308",
    "khalifa-city": "36539",
    "motor-city": "46729",
    "saadiyat": "36629",
    "fratelli": "59766",
}

DEFAULT_ATTEMPTS = 3
DEFAULT_REPORT_TIMEOUT = 120  # seconds to wait for one report to generate
RECLICK_INTERVAL = 12  # seconds; re-press "Generate now" if nothing happened

# Reports this script can pull. The key is both the --report value and the
# filename segment (<prefix>-<key>-<date>.pdf).
#
#   category / report   the (category, report) pair the jasper-reports URL
#                        takes. category is None for the one report confirmed
#                        (10 Sep 2026, live click) to need no category param —
#                        adding a wrong one doesn't error, it just times out
#                        silently, so only set it when actually confirmed.
#   filters              this report's OWN complete filter payload, merged
#                        with FROM_DATE/TO_DATE. Deliberately NOT a shared
#                        default across reports — staff-utilisation was
#                        merged in from a standalone script (10 Sep 2026)
#                        that never sent excludeSelfEmployedStaff at all, and
#                        changing its filter shape isn't something to risk
#                        guessing at, so each report states its own in full.
#   routing              "flat": one static downloads/ folder, mirrored into
#                        a branch/year/month tree afterwards (mirror_to_month_folder).
#                        "nested": save STRAIGHT into the branch/year/month
#                        tree, no separate flat copy — this is staff-utilisation's
#                        original layout (see collect_today.py, which expects
#                        to find PDFs already sitting in that tree) and is
#                        preserved as-is rather than folded into "flat".
#   opens_popup          True if Phorest may open the report viewer in a new
#                        tab instead of navigating the current page in place
#                        (confirmed true for staff-utilisation, 10 Aug 2026 —
#                        a worker that doesn't track its OWN page's popups
#                        specifically can steal a popup that belongs to a
#                        different concurrent worker and mislabel dates).
#                        False (the default) skips popup-tracking entirely,
#                        since the other three reports have never shown this
#                        behaviour and there's no reason to add the extra
#                        bookkeeping where it's never needed.
REPORTS = {
    "staff-performance": {
        "category": "STAFF",
        "report": "STAFF_PERFORMANCE_OVERVIEW",
        "filters": {"excludeSelfEmployedStaff": False, "top_n": 4000},
        "routing": "flat",
        "download_dir": SCRIPT_DIR / "downloads",
        "month_base": SCRIPT_DIR / "phorest-reports" / "downloads",
        "opens_popup": False,
    },
    "financial-totals": {
        "category": "FINANCIAL",
        "report": "FINANCIAL_TOTALS",
        "filters": {"excludeSelfEmployedStaff": False},
        "routing": "flat",
        "download_dir": REPORTS_BASE / "financial totals" / "downloads",
        "month_base": REPORTS_BASE / "financial totals" / "downloads",
        "opens_popup": False,
    },
    "sales-transactions": {
        # Folder is "staff financial totals" (10 Sep 2026, Kate's naming call
        # — matches the utilisation-reports precedent of folder name != exact
        # report name), but the --report key, filenames, and the underlying
        # Phorest report stay "sales-transactions"/"Sales Transactions" — the
        # raw per-line-item data this pulls could feed other things later,
        # not just the staff_financial_totals table it feeds today.
        "category": None,
        "report": "TRANSACTIONS",
        "filters": {"excludeSelfEmployedStaff": False},
        "routing": "flat",
        "download_dir": REPORTS_BASE / "staff financial totals" / "downloads",
        "month_base": REPORTS_BASE / "staff financial totals" / "downloads",
        "date_subfolder": True,  # .../<month folder>/<YYYY-MM-DD>/<pdf> in the mirror
        "opens_popup": False,
    },
    "staff-utilisation": {
        # Merged 10 Sep 2026 from "utilisation reports/bulk_download.py",
        # which was a full standalone copy of this script with its own
        # session file. Filters, routing and popup-handling below are its
        # ORIGINAL behaviour, ported as-is rather than assumed compatible
        # with the other reports' conventions.
        "category": "STAFF",
        "report": "STAFF_UTILISATION",
        "filters": {"fetch_archived": True},
        "routing": "nested",
        "nested_base": REPORTS_BASE / "utilisation reports" / "downloads",
        "opens_popup": True,
    },
}
DEFAULT_REPORT = "staff-performance"


class SessionExpired(Exception):
    """The saved session can no longer authenticate. Re-run save_session.py."""


def month_folder_path(base: Path, prefix: str, d: date, date_subfolder: bool = False) -> Path:
    """<base>/<branch>/<year> <branch>/<MM><year> <branch>/ — the one nested
    layout every report's month-folder mirror (or, for staff-utilisation, the
    ONLY copy) resolves to. Reports whose spec sets "date_subfolder": True
    (staff-financial-totals — see its own REPORTS entry) get one more level,
    a plain YYYY-MM-DD folder, so the day's PDF sits somewhere browsable on
    its own rather than named-but-flat inside the month folder."""
    branch = BRANCH_FOLDERS.get(prefix, prefix)
    folder = base / branch / f"{d.year} {branch}" / f"{d.month:02d}{d.year} {branch}"
    return folder / d.isoformat() if date_subfolder else folder


def mirror_to_month_folder(dest: Path, prefix: str, date_str: str, report: str):
    """Copy a flat-routed download into its month-folder mirror. No-op for
    nested-routed reports, which already saved straight into that tree."""
    spec = REPORTS[report]
    if spec["routing"] != "flat" or not dest.exists():
        return
    d = date.fromisoformat(date_str)
    folder = month_folder_path(spec["month_base"], prefix, d, spec.get("date_subfolder", False))
    copy = folder / dest.name
    if copy.exists():
        return
    folder.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(dest.read_bytes())


def resolve_dest_dir(report: str, prefix: str, date_str: str, override: Path = None) -> Path:
    """Where THIS date's PDF for THIS report actually lands. Flat-routed
    reports use one static folder (override replaces it, e.g. --outdir);
    nested-routed ones resolve a branch/year/month(/date) path per date
    (override replaces the tree's root instead, matching the old --base-outdir)."""
    spec = REPORTS[report]
    if spec["routing"] == "flat":
        return override or spec["download_dir"]
    base = override or spec["nested_base"]
    return month_folder_path(base, prefix, date.fromisoformat(date_str), spec.get("date_subfolder", False))


def build_report_url(account_id: str, date_str: str, report: str = DEFAULT_REPORT) -> str:
    spec = REPORTS[report]
    filters = {"FROM_DATE": date_str, "TO_DATE": date_str, **spec["filters"]}
    encoded_filters = urllib.parse.quote(json.dumps(filters, separators=(",", ":")))
    base_url = f"https://my.phorest.com/a/{account_id}/jasper-reports"
    # Most reports need a &category= param, but some (confirmed live off a real
    # click, e.g. Sales Transactions) route fine with just filters+report and no
    # category at all — adding an empty/wrong category to those breaks nothing
    # silently, it just times out, so only add it when the report spec has one.
    category_part = f"category={spec['category']}&" if spec.get("category") else ""
    return f"{base_url}?{category_part}filters={encoded_filters}&report={spec['report']}"


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
        raise SessionExpired("response was not a valid PDF")
    dest.write_bytes(data)


async def generate_and_wait(page, popups: list, viewer_pattern, timeout: float):
    """Press "Generate now" and wait for the SPA to route to the report
    viewer, on this page OR (staff-utilisation) a popup it opened. Returns
    whichever page actually has the viewer URL.

    Phorest sometimes renders the button before its click handler is bound
    (most often on the first page load of a run, while the app is still
    refreshing its access token), so a single click can silently do nothing
    and the page just sits on the report form. Re-press it every
    RECLICK_INTERVAL seconds for as long as it's still on screen.
    """
    button = page.get_by_role("button", name="Generate now")
    await button.wait_for(state="visible", timeout=30000)

    deadline = time.monotonic() + timeout
    last_click = 0.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PlaywrightTimeoutError(f"report did not generate within {timeout:.0f}s")
        if time.monotonic() - last_click >= RECLICK_INTERVAL:
            try:
                if await button.is_visible():
                    await button.click()
                    last_click = time.monotonic()
            except Exception:
                pass  # page navigated mid-check; the checks below settle it
        for candidate in [page] + popups:
            if not candidate.is_closed() and viewer_pattern.search(candidate.url):
                return candidate
        await asyncio.sleep(min(1.0, max(0.05, remaining)))


async def process_date(
    page,
    account_id: str,
    prefix: str,
    date_str: str,
    download_dir: Path,
    attempts: int = DEFAULT_ATTEMPTS,
    report_timeout: float = DEFAULT_REPORT_TIMEOUT,
    report: str = DEFAULT_REPORT,
):
    url = build_report_url(account_id, date_str, report)
    viewer_pattern = re.compile(
        rf"https://my\.phorest\.com/a/{account_id}/report-viewer\?reportId="
    )
    dest = download_dir / f"{prefix}-{report}-{date_str}.pdf"

    # Track popups spawned by THIS worker's page only. Never scan
    # context.pages: with concurrent workers every tab in the context is
    # visible to every worker, and whoever polls first steals whichever
    # viewer appears — that's how staff-utilisation range downloads once
    # ended up with filenames pointing at the wrong dates (10 Aug 2026,
    # before this was its own script). A popup event on this page can only
    # belong to this worker's own Generate click.
    popups = []
    on_popup = lambda p: popups.append(p)
    page.on("popup", on_popup)

    try:
        for attempt in range(1, attempts + 1):
            try:
                await page.goto(url, wait_until="load", timeout=45000)
                viewer_page = await generate_and_wait(page, popups, viewer_pattern, report_timeout)

                query = urllib.parse.urlparse(viewer_page.url).query
                report_url = urllib.parse.parse_qs(query).get("reportUrl", [None])[0]
                if not report_url:
                    raise ValueError("reportUrl param missing from report-viewer URL")

                download_dir.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(download_pdf, report_url, dest)
                if viewer_page is not page:
                    try:
                        await viewer_page.close()
                    except Exception:
                        pass
                mirror_to_month_folder(dest, prefix, date_str, report)
                return dest
            except SessionExpired:
                raise  # retrying with a dead session is pointless
            except Exception as e:
                if attempt == attempts:
                    try:
                        await page.screenshot(
                            path=str(download_dir / f"_debug_failure_{prefix}_{date_str}.png")
                        )
                    except Exception:
                        pass
                    raise
                print(f"[warn] {prefix} {date_str} attempt {attempt}/{attempts} failed ({e}), retrying")
                await asyncio.sleep(3 * attempt)
    finally:
        try:
            page.remove_listener("popup", on_popup)
        except Exception:
            pass
        for p in popups:  # close anything spawned but never used as the viewer
            if not p.is_closed():
                try:
                    await p.close()
                except Exception:
                    pass


async def _worker(context, queue: asyncio.Queue, results: dict, stagger: float, opts: dict):
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
            if results.get("session_dead"):
                results["failed"].append((prefix, date_str))
                continue
            try:
                dest = await process_date(
                    page, account_id, prefix, date_str, download_dir,
                    attempts=opts["attempts"], report_timeout=opts["report_timeout"],
                    report=opts["report"],
                )
                print(f"[OK]   {prefix} {date_str} -> {dest.name}")
                results["ok"].append((prefix, date_str))
            except SessionExpired as e:
                print(f"[FAIL] {prefix} {date_str} -> session expired ({e})")
                results["session_dead"] = True
                results["failed"].append((prefix, date_str))
            except PlaywrightTimeoutError:
                print(f"[FAIL] {prefix} {date_str} -> timed out waiting for report")
                results["failed"].append((prefix, date_str))
            except Exception as e:
                print(f"[FAIL] {prefix} {date_str} -> {e}")
                results["failed"].append((prefix, date_str))
    finally:
        await page.close()


async def run_jobs(
    jobs: list,
    concurrency: int = DEFAULT_CONCURRENCY,
    headless: bool = True,
    attempts: int = DEFAULT_ATTEMPTS,
    report_timeout: float = DEFAULT_REPORT_TIMEOUT,
    report: str = DEFAULT_REPORT,
) -> dict:
    """Run (account_id, prefix, date_str, download_dir) jobs across a pool of
    concurrent pages sharing one logged-in browser context."""
    queue: asyncio.Queue = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)

    results = {"ok": [], "failed": [], "session_dead": False}
    n_workers = max(1, min(concurrency, len(jobs)))
    opts = {"attempts": attempts, "report_timeout": report_timeout, "report": report}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(storage_state=str(SESSION_FILE))
        workers = [
            asyncio.create_task(_worker(context, queue, results, i * 0.4, opts))
            for i in range(n_workers)
        ]
        await asyncio.gather(*workers)
        # Phorest's access token expires after 30 minutes; the app refreshes it
        # in the browser, so save that refreshed state back over the stale one.
        if not results["session_dead"]:
            try:
                await context.storage_state(path=str(SESSION_FILE))
            except Exception as e:
                print(f"[warn] could not save refreshed session: {e}")
        await browser.close()

    return results


def read_failed_log(failed_log: Path):
    """Return the (prefix, date_str) pairs in failed_dates.txt, deduped."""
    if not failed_log.exists():
        return []
    pairs, seen = [], set()
    for line in failed_log.read_text().splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        pair = (parts[0], parts[1])
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return pairs


def update_failed_log(failed_log: Path, results: dict):
    """Drop anything that succeeded, keep/add what failed, deduped."""
    pairs = [p for p in read_failed_log(failed_log) if p not in set(results["ok"])]
    for pair in results["failed"]:
        if pair not in pairs:
            pairs.append(pair)
    if pairs:
        failed_log.write_text("".join(f"{p} {d}\n" for p, d in pairs))
    else:
        failed_log.unlink(missing_ok=True)


def build_jobs(pairs, report: str, force: bool, outdir_override: Path = None):
    """Turn (prefix, date_str) pairs into jobs, skipping PDFs already on disk.
    Each job resolves its own destination folder (flat: one static folder;
    nested: a branch/year/month path per date) via resolve_dest_dir."""
    jobs, skipped = [], 0
    for prefix, date_str in pairs:
        account_id = ACCOUNT_IDS.get(prefix)
        if account_id is None:
            print(f"[skip] {prefix} {date_str} -> no account ID mapped for '{prefix}'")
            continue
        download_dir = resolve_dest_dir(report, prefix, date_str, outdir_override)
        dest = download_dir / f"{prefix}-{report}-{date_str}.pdf"
        if dest.exists() and not force:
            # Heal the month-folder mirror even when the flat download is
            # skipped (no-op for nested-routed reports, see mirror_to_month_folder).
            mirror_to_month_folder(dest, prefix, date_str, report)
            skipped += 1
            continue
        jobs.append((account_id, prefix, date_str, download_dir))
    return jobs, skipped


async def main_async(args):
    spec = REPORTS[args.report]
    outdir_override = Path(args.outdir) if args.outdir else None
    # failed_dates.txt lives at the report's own root: the flat downloads/
    # folder, or (nested routing) the top of the branch/year/month tree.
    failed_log_base = outdir_override or (
        spec["download_dir"] if spec["routing"] == "flat" else spec["nested_base"]
    )
    failed_log = failed_log_base / "failed_dates.txt"

    if not SESSION_FILE.exists():
        print(f"ERROR: session file not found at {SESSION_FILE}. Run save_session.py first.")
        sys.exit(1)

    failed_log_base.mkdir(parents=True, exist_ok=True)

    if args.retry_failed:
        pairs = read_failed_log(failed_log)
        if not pairs:
            print(f"Nothing listed in {failed_log}, nothing to retry.")
            return
    else:
        if not all([args.account, args.prefix, args.start, args.end]):
            print("ERROR: --account, --prefix, --start and --end are all required "
                  "(or use --retry-failed on its own).")
            sys.exit(1)
        ACCOUNT_IDS[args.prefix] = args.account  # honour whatever account was passed
        pairs = [
            (args.prefix, d.isoformat())
            for d in daterange(date.fromisoformat(args.start), date.fromisoformat(args.end))
        ]

    jobs, skipped = build_jobs(pairs, args.report, args.force, outdir_override)
    if skipped:
        print(f"{skipped} date(s) already downloaded, skipping (use --force to redo them).")
    if not jobs:
        update_failed_log(failed_log, {"ok": pairs, "failed": []})
        print("Nothing left to fetch.")
        return

    print(f"Fetching {len(jobs)} report(s)...")
    results = await run_jobs(
        jobs,
        concurrency=args.concurrency,
        headless=not args.show,
        attempts=args.attempts,
        report_timeout=args.report_timeout,
        report=args.report,
    )
    # Dates skipped as already-present count as done for log-cleanup purposes.
    attempted = set(results["ok"]) | set(results["failed"])
    results["ok"].extend(p for p in pairs if p not in attempted)
    update_failed_log(failed_log, results)

    if results["session_dead"]:
        print("\nSession expired. Run save_session.py to log in again, then re-run "
              "with --retry-failed.")
    elif results["failed"]:
        print(f"\n{len(results['failed'])} still failed. See {failed_log}. Retry with "
              f"--retry-failed.")
    else:
        print("\nAll reports downloaded. failed_dates.txt is clear.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", help="Phorest account/business ID, e.g. 52308")
    parser.add_argument(
        "--report",
        choices=sorted(REPORTS),
        default=DEFAULT_REPORT,
        help=f"Which Phorest report to pull (default {DEFAULT_REPORT}). Also the "
        "filename segment, so two reports can share one --outdir.",
    )
    parser.add_argument("--prefix", help="Filename prefix, e.g. al-quoz")
    parser.add_argument("--start", help="YYYY-MM-DD")
    parser.add_argument("--end", help="YYYY-MM-DD")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Ignore --account/--prefix/--start/--end and re-run everything still "
        "listed in failed_dates.txt (branch accounts are looked up automatically).",
    )
    parser.add_argument(
        "--outdir",
        default=None,
        help="Where PDFs land (created if missing). Defaults to the chosen "
        "report's own folder. For a 'flat'-routed report (staff-performance, "
        "financial-totals, sales-transactions) this replaces that flat folder; "
        "for staff-utilisation ('nested' routing) this replaces the root of "
        "its branch/year/month tree instead (same meaning as the old "
        "--base-outdir).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Number of report requests to run in parallel (default {DEFAULT_CONCURRENCY}). "
        "Higher is faster but hits Phorest harder, so dial back if you see failures.",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=DEFAULT_ATTEMPTS,
        help=f"Tries per date before giving up (default {DEFAULT_ATTEMPTS}).",
    )
    parser.add_argument(
        "--report-timeout",
        type=float,
        default=DEFAULT_REPORT_TIMEOUT,
        help=f"Seconds to wait for one report to generate (default {DEFAULT_REPORT_TIMEOUT}). "
        "staff-utilisation's own launcher raises this — it was seen needing "
        "longer than the other three reports.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download dates whose PDF is already on disk (skipped by default).",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Run with a visible browser window instead of headless (slower, useful for debugging).",
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
