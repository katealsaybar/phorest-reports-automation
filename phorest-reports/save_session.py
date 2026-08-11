"""
One-time (or occasional re-run) script to log into Phorest manually and save
the browser session (cookies/local storage) to disk so later automation
scripts can reuse it without logging in again.

Usage:
    py save_session.py

What happens:
    1. A visible Chromium window opens at https://my.phorest.com
    2. Log in manually in that window (enter credentials, 2FA, etc.)
    3. Once you're on a logged-in page (e.g. the dashboard), come back to
       the Playwright Inspector window that pops up alongside the browser
       and click the "Resume" (play) button, or press F8 / the resume
       shortcut shown in the Inspector.
    4. The script then saves session state to phorest_session.json and exits.
"""

from pathlib import Path
from playwright.sync_api import sync_playwright

SESSION_FILE = Path(__file__).parent / "phorest_session.json"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        page.goto("https://my.phorest.com")

        print("\n=== ACTION NEEDED ===")
        print("Log in manually in the browser window that just opened.")
        print("When you're logged in and see the Phorest dashboard,")
        print("go to the Playwright Inspector window and click Resume (▶).")
        print("======================\n")

        # This opens the Playwright Inspector and blocks here until the
        # user clicks "Resume" in that Inspector window.
        page.pause()

        context.storage_state(path=str(SESSION_FILE))
        print(f"\nSession saved to {SESSION_FILE}")

        browser.close()


if __name__ == "__main__":
    main()
