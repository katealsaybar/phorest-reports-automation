#!/usr/bin/env python
"""Read the category/report constants straight out of Phorest, no guessing.

bulk_download.py builds its URL from a (category, report) pair per report type.
Those constants are not documented anywhere: they only appear in the address bar
after you click a report in Additional reports. Guessing one is expensive to get
wrong, because a bad constant means the SPA never routes to the report viewer
and every date in a run burns its full timeout before failing.

This opens the Additional reports page once with the saved session, clicks each
report in the left-hand nav, and prints the URL parameters that appear. Nothing
is generated and nothing is downloaded — it only reads the menu.

    py find_report_ids.py                       # every report it can find
    py find_report_ids.py "Financial Totals"    # just the ones matching this

Add --show to watch it work. The account only decides which menu is read; the
constants are the same for every branch.
"""
import argparse
import re
import sys
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SCRIPT_DIR = Path(__file__).parent
SESSION_FILE = SCRIPT_DIR / "phorest_session.json"
DEFAULT_ACCOUNT = "36629"  # Saadiyat; any branch's menu carries the same constants


def params_of(url: str):
    q = urllib.parse.urlparse(url).query
    p = urllib.parse.parse_qs(q)
    return p.get("category", [None])[0], p.get("report", [None])[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("match", nargs="?", default=None,
                    help="Only report names containing this text (case-insensitive).")
    ap.add_argument("--account", default=DEFAULT_ACCOUNT)
    ap.add_argument("--show", action="store_true", help="Visible browser window.")
    args = ap.parse_args()

    if not SESSION_FILE.exists():
        sys.exit(f"No session at {SESSION_FILE}. Run save_session.py first.")

    base = f"https://my.phorest.com/a/{args.account}/jasper-reports"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.show)
        context = browser.new_context(storage_state=str(SESSION_FILE))
        page = context.new_page()

        try:
            page.goto(base, wait_until="load", timeout=45000)
        except PlaywrightTimeoutError:
            sys.exit("Additional reports page did not load. Session may be expired.")

        # The nav is a plain list of report names. Wait for any of them rather
        # than a specific one, so this keeps working when Phorest reorders it.
        try:
            page.get_by_text("Financial Totals", exact=True).first.wait_for(timeout=30000)
        except PlaywrightTimeoutError:
            sys.exit("Could not see the report list. Session may be expired — re-run save_session.py.")

        names = page.evaluate("""() => {
            const out = new Set();
            document.querySelectorAll('li, a, button, div[role="button"]').forEach(el => {
                const t = (el.textContent || '').trim();
                // Report names are short, single-line and title-cased.
                if (t && t.length < 45 && !t.includes('\\n') && /^[A-Z]/.test(t)) out.add(t);
            });
            return Array.from(out);
        }""")
        if args.match:
            names = [n for n in names if args.match.lower() in n.lower()]

        print(f"Reading {len(names)} menu entries on account {args.account}\n")
        found = {}
        for name in names:
            try:
                page.get_by_text(name, exact=True).first.click(timeout=4000)
                page.wait_for_timeout(400)
                category, report = params_of(page.url)
                if category and report and (category, report) not in found.values():
                    found[name] = (category, report)
                    print(f"  {name}")
                    print(f"      category={category}")
                    print(f"      report={report}")
            except Exception:
                continue  # not a clickable report, or the menu shifted under us

        browser.close()

    if not found:
        print("\nNothing matched. Try without a filter, or with --show to watch.")
        return

    print("\n--- paste-ready for the REPORTS dict in bulk_download.py ---")
    for name, (category, report) in sorted(found.items()):
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        print(f'    "{slug}": {{')
        print(f'        "category": "{category}",')
        print(f'        "report": "{report}",')
        print(f'        "extra_filters": {{}},')
        print(f"    }},")


if __name__ == "__main__":
    main()
