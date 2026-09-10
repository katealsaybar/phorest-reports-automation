# Phorest Report Automation

Bulk-downloads PDF reports from [Phorest](https://my.phorest.com), one PDF per
day, for any branch and date range.

## Which report

`--report` picks one. It defaults to `staff-performance`, so every command that
worked before this flag existed still works unchanged. Full detail on each
(filters, folder routing, how they were added) is in `../HOW-DOWNLOADS-WORK.md`.

| `--report` | Phorest report | Saved to |
|---|---|---|
| `staff-performance` (default) | Staff Performance Overview | `downloads/`, mirrored into `phorest-reports/downloads/` |
| `financial-totals` | Financial Totals | `../financial totals/downloads/` |
| `sales-transactions` | Sales Transactions | `../staff financial totals/downloads/` (folder named for the deliverable, not the report — with an extra date-named subfolder in the mirror) |
| `staff-utilisation` | Staff Utilisation | `../utilisation reports/downloads/<branch>/<year>/<month>/` (nested only, no flat copy — `collect_today.py` there expects this) |

The report name is also the filename segment, so no two collide.

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

Phorest's access token only lives **30 minutes**, but the browser silently
refreshes it from a 30-day refresh token on page load, and `bulk_download.py`
now writes that refreshed session back to `phorest_session.json` at the end of
every run. So you only need to re-run `save_session.py` roughly once a month,
or if you see `session expired` in the output.

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

PDFs are saved to `downloads/<prefix>-<report>-<date>.pdf` by default, in
whichever folder that report owns (see the table above).
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

Each date gets 3 tries before it's given up on (`--attempts`), and each try
waits up to 120s for Phorest to generate the report (`--report-timeout`).
Anything that still fails is skipped and logged to `failed_dates.txt` inside
that same output folder, so the run continues rather than crashing.

To re-run everything still listed in that log, across all branches, in one go:

```bash
python bulk_download.py --retry-failed
```

Branch account IDs are looked up automatically from the table above. Dates
whose PDF is already on disk are **skipped** (and dropped from the log), so
this is always safe and cheap to re-run. Pass `--force` if you actually want
to re-download a PDF you already have.

## Notes

- Runs headless by default for speed. Phorest's report generation is an SPA
  flow (click "Generate now" → redirect to a report-viewer page with a
  presigned S3 link to the PDF), so a real browser drives the click and
  navigation, and the actual PDF is fetched with a direct HTTP request to
  that presigned link.
- Phorest sometimes renders "Generate now" before its click handler is bound,
  which makes the first click do nothing at all and leaves the page sitting on
  the report form until it times out. The script re-presses the button every
  12s until the page actually routes to the report viewer.
- Multiple report requests run in parallel, each on its own page but sharing
  one logged-in browser session/context.
- `phorest_session.json` and `downloads/` are gitignored — never commit
  session cookies or client report data.
