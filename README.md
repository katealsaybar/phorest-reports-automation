# Phorest Staff Performance Report Automation

Bulk-downloads "Staff Performance Overview" PDF reports from [Phorest](https://my.phorest.com),
one PDF per day, for any branch and date range.

## Setup

```bash
pip install playwright
playwright install chromium
```

## 1. Log in and save a session

```bash
python save_session.py
```

This opens a visible Chromium window at `my.phorest.com`. Log in manually
(credentials, 2FA, etc.). Once you're on the dashboard, go to the Playwright
Inspector window that pops up alongside it and click **Resume (▶)**. The
script saves your session to `phorest_session.json` and exits.

Re-run this whenever the session expires (you'll see download failures with
"response was not a valid PDF" as the error).

## 2. Run a batch

```bash
python bulk_download.py --account <ACCOUNT_ID> --prefix <NAME> --start YYYY-MM-DD --end YYYY-MM-DD
```

Example — Al Quoz branch, January 2026:

```bash
python bulk_download.py --account 52308 --prefix al-quoz --start 2026-01-01 --end 2026-01-31
```

| Branch | Account ID |
|---|---|
| Al Quoz | 52308 |
| Khalifa City | 36539 |
| Motor City | 46729 |
| Saadiyat | 36629 |

PDFs are saved to `downloads/<prefix>-staff-performance-<date>.pdf` by default.
Add `--outdir` to save somewhere else, e.g.:

```bash
python bulk_download.py --account 52308 --prefix al-quoz --start 2026-01-01 --end 2026-01-31 --outdir "D:\Reports\AlQuoz"
```

Dates are processed **concurrently** (5 at a time by default) instead of one
at a time. Tune with `--concurrency`:

```bash
python bulk_download.py --account 52308 --prefix al-quoz --start 2026-01-01 --end 2026-01-31 --concurrency 8
```

Higher concurrency finishes faster but hits Phorest harder — dial it back if
you start seeing failures. Add `--show` to watch it work in a visible browser
window (slower — useful for debugging, not for normal runs).

Any date that fails (timeout, session expired, etc.) is skipped and logged
to `failed_dates.txt` inside that same output folder — the run continues
rather than crashing. Re-run the same command with `--start`/`--end` set to
just the failed date(s) to retry.

## Notes

- Runs headless by default for speed. Phorest's report generation is an SPA
  flow (click "Generate now" → redirect to a report-viewer page with a
  presigned S3 link to the PDF) — a real browser drives the click and
  navigation, and the actual PDF is fetched with a direct HTTP request to
  that presigned link.
- Multiple report requests run in parallel, each on its own page but sharing
  one logged-in browser session/context.
- `phorest_session.json` and `downloads/` are gitignored — never commit
  session cookies or client report data.
