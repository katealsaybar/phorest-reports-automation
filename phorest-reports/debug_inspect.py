import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SCRIPT_DIR = Path(__file__).parent
SESSION_FILE = SCRIPT_DIR / "phorest_session.json"

account_id = sys.argv[1] if len(sys.argv) > 1 else "52308"
attempts = int(sys.argv[2]) if len(sys.argv) > 2 else 3

filters = {
    "FROM_DATE": "2026-01-01",
    "TO_DATE": "2026-01-01",
    "excludeSelfEmployedStaff": False,
    "top_n": 4000,
}
encoded = urllib.parse.quote(json.dumps(filters, separators=(",", ":")))
url = f"https://my.phorest.com/a/{account_id}/jasper-reports?category=STAFF&filters={encoded}&report=STAFF_PERFORMANCE_OVERVIEW"
pattern = re.compile(rf"https://my\.phorest\.com/a/{account_id}/report-viewer\?reportId=")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(storage_state=str(SESSION_FILE))

    for i in range(attempts):
        page = context.new_page()
        t0 = time.time()
        try:
            page.goto(url, wait_until="load", timeout=45000)
            btn = page.get_by_role("button", name="Generate now")
            btn.wait_for(state="visible", timeout=30000)
            t_visible = time.time()
            btn.click()
            t_click = time.time()
            page.wait_for_url(pattern, timeout=60000)
            t_nav = time.time()
            print(f"[attempt {i+1}] OK. visible@{t_visible-t0:.1f}s click@{t_click-t0:.1f}s nav@{t_nav-t0:.1f}s")
        except PlaywrightTimeoutError as e:
            print(f"[attempt {i+1}] TIMEOUT after {time.time()-t0:.1f}s. Current URL: {page.url}")
            page.screenshot(path=str(SCRIPT_DIR / f"debug_{account_id}_attempt{i+1}_timeout.png"))
        page.close()

    browser.close()
