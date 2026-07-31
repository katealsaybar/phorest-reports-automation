r"""
Bulk-download Staff Performance Overview reports from Phorest, one PDF per
day, for a given branch/account and date range.

Requires phorest_session.json to already exist (run save_session.py once
first to log in manually and capture the session). The same session works
across branches as long as the logged-in user has access to that account.

Usage:
    py bulk_download.py --account 52308 --prefix al-quoz --start 2026-01-01 --end 2026-01-31
    py bulk_download.py --account 52308 --prefix al-quoz --start 2026-01-01 --end 2026-01-31 --outdir "D:\Reports\AlQuoz"

Flow per date:
    1. Build the jasper-reports URL with FROM_DATE/TO_DATE set to that date.
    2. Navigate there (loads the given account, pre-fills the date pickers).
    3. Click "Generate now".
    4. Wait for the SPA to route to /report-viewer?reportId=...&reportUrl=...
    5. Pull the reportUrl (a presigned S3 link straight to the PDF) out of
       that URL and fetch it directly with a plain HTTP GET.
    6. Save it as <outdir>/<prefix>-staff-performance-YYYY-MM-DD.pdf
       (--outdir defaults to ./downloads next to this script)

Failures on any single date are logged to failed_dates.txt and skipped —
the run continues rather than crashing.
"""

import argparse
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SCRIPT_DIR = Path(__file__).parent
SESSION_FILE = SCRIPT_DIR / "phorest_session.json"
DEFAULT_DOWNLOAD_DIR = SCRIPT_DIR / "downloads"


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


def process_date(page, account_id: str, prefix: str, date_str: str, download_dir: Path):
    url = build_report_url(account_id, date_str)
    report_viewer_pattern = re.compile(rf"https://my\.phorest\.com/a/{account_id}/report-viewer\?reportId=")
    try:
        page.goto(url, wait_until="load", timeout=45000)

        generate_btn = page.get_by_role("button", name="Generate now")
        generate_btn.wait_for(state="visible", timeout=30000)
        generate_btn.click()

        page.wait_for_url(report_viewer_pattern, timeout=60000)

        query = urllib.parse.urlparse(page.url).query
        params = urllib.parse.parse_qs(query)
        report_url = params.get("reportUrl", [None])[0]
        if not report_url:
            raise ValueError("reportUrl param missing from report-viewer URL")

        dest = download_dir / f"{prefix}-staff-performance-{date_str}.pdf"
        download_pdf(report_url, dest)
        return dest
    except Exception:
        try:
            page.screenshot(path=str(download_dir / f"_debug_failure_{prefix}_{date_str}.png"))
        except Exception:
            pass
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True, help="Phorest account/business ID, e.g. 52308")
    parser.add_argument("--prefix", required=True, help="Filename prefix, e.g. al-quoz")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--outdir",
        default=str(DEFAULT_DOWNLOAD_DIR),
        help="Folder to save PDFs into (created if missing). Defaults to ./downloads next to this script.",
    )
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    download_dir = Path(args.outdir)
    failed_log = download_dir / "failed_dates.txt"

    if not SESSION_FILE.exists():
        print(f"ERROR: session file not found at {SESSION_FILE}. Run save_session.py first.")
        sys.exit(1)

    download_dir.mkdir(parents=True, exist_ok=True)

    failed_dates = []
    ok_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(storage_state=str(SESSION_FILE))
        page = context.new_page()

        for d in daterange(start, end):
            date_str = d.isoformat()
            try:
                dest = process_date(page, args.account, args.prefix, date_str, download_dir)
                print(f"[OK]   {date_str} -> {dest.name}")
                ok_count += 1
            except PlaywrightTimeoutError:
                print(f"[FAIL] {date_str} -> timed out waiting for report")
                failed_dates.append(date_str)
            except Exception as e:
                print(f"[FAIL] {date_str} -> {e}")
                failed_dates.append(date_str)

            time.sleep(random.uniform(2, 3))

        browser.close()

    if failed_dates:
        with open(failed_log, "a") as f:
            for d in failed_dates:
                f.write(f"{args.prefix} {d}\n")
        print(f"\n{ok_count} succeeded, {len(failed_dates)} failed. See {failed_log}")
    else:
        print(f"\nAll {ok_count} date(s) processed successfully.")


if __name__ == "__main__":
    main()
