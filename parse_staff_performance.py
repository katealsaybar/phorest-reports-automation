r"""
Parse "Staff Performance Overview" PDFs (downloaded by bulk_download.py) into
rows matching the `phorest_staff_daily` Supabase table.

This is a line-for-line Python port of the site's own parser
(tararosesalon-dashboard-all-main/upload/phorest-pdf.js + phorest-staff.js),
built because the Upload Portal only runs in a browser and no headless path
existed for this report (unlike Utilisation, which already has
parse_utilisation.py). Output-only for now — prints/writes rows, does not
touch Supabase, so a human can spot-check before anything is pushed.

Usage:
    py parse_staff_performance.py downloads\al-quoz-staff-performance-2026-09-08.pdf
    py parse_staff_performance.py downloads\*.pdf --out staff_perf.json

Note on PDF extraction: the site's JS code (phorest-pdf.js) works around an
axis-swap quirk in pdf.js's raw getTextContent() coordinates on this report's
rotated page. pdfplumber does not have that quirk — page.rotation is applied
before chars are handed back, so extract_text() already reads top-to-bottom,
left-to-right in natural order (confirmed against a real Sept 2026 sample).
No coordinate-swap workaround is needed here.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import pdfplumber

SP_BRANCH_SLUGS = {
    'saadiyat': 'SAA',
    'khalifa-city': 'KCA',
    'motor-city': 'MC',
    'al-quoz': 'AQ',
}

IS_INT = re.compile(r'^\d+$')
IS_MONEY = re.compile(r'^-?[\d,]+\.\d{2}$')
DATE_LINE = re.compile(r'(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})')


def norm_line(s):
    return re.sub(r'\s+', ' ', s.strip().lower())


def to_num(t):
    return float(t.replace(',', ''))


def strip_archived(name):
    return re.sub(r'\s*\(A\)$', '', name).strip()


def branch_from_filename(filename):
    lower = filename.lower()
    for slug, code in SP_BRANCH_SLUGS.items():
        if lower.startswith(slug + '-'):
            return code
    return None


def date_from_filename(filename):
    m = re.search(r'(\d{4}-\d{2}-\d{2})\.pdf$', filename, re.I)
    return m.group(1) if m else None


def to_iso_date(ddmmyy):
    d, mo, y = ddmmyy.split('/')
    return f"20{y}-{mo}-{d}"


def extract_lines(pdf_path):
    lines = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ''
            lines.extend(l for l in text.split('\n') if l.strip())
    return lines


def strip_pdf_header_lines(lines):
    out = []
    for l in lines:
        n = norm_line(l)
        if n.startswith('employee '):
            continue
        if n.startswith('# new rqs'):
            continue
        out.append(l)
    return out


SP_HEADER_LINES = {
    'employee client staff visits services courses (perf) products total',
    '# new rqs ex vat total ex vat total ex vat total ex vat total ex vat total',
    'avg. spend per client',
    'rat',
}


def parse_one_block(lines, branch_code):
    if not lines:
        raise ValueError('Empty report block.')

    salon_line = next((l for l in lines if 'salon' in l.lower()), None)
    date_line = next((l for l in lines if DATE_LINE.search(l)), None)
    if not date_line:
        raise ValueError('Could not find the date line (expected "DD/MM/YY - DD/MM/YY").')

    m = DATE_LINE.search(date_line)
    date_from, date_to = m.group(1), m.group(2)
    if date_from != date_to:
        raise ValueError(f'Multi-day range ({date_from} - {date_to}) — expected a single-day report.')

    body_lines = []
    for l in lines:
        n = norm_line(l)
        if l == salon_line:
            continue
        if l == date_line:
            continue
        if n == 'staff performance overview':
            continue
        if n.startswith('visits are calculated'):
            continue
        if n in SP_HEADER_LINES:
            continue
        if re.match(r'^page\s*\d+$', n, re.I):
            continue
        body_lines.append(l)

    tokens = ' '.join(body_lines).split()

    employees = []
    totals = None
    i = 0
    while i < len(tokens):
        name_parts = []
        while i < len(tokens) and not IS_INT.match(tokens[i]) and not IS_MONEY.match(tokens[i]):
            name_parts.append(tokens[i])
            i += 1
        if not name_parts:
            i += 1
            continue
        name = strip_archived(' '.join(name_parts))

        if name.lower() == 'total':
            need = 8
            if i + need > len(tokens):
                break
            v = [to_num(t) for t in tokens[i:i + need]]
            totals = {
                'servicesExVat': v[0], 'servicesTotal': v[1],
                'coursesExVat': v[2], 'coursesTotal': v[3],
                'productsExVat': v[4], 'productsTotal': v[5],
                'totalExVat': v[6], 'totalTotal': v[7],
            }
            i += need
            break

        need = 14
        if i + need > len(tokens):
            break
        f = tokens[i:i + need]
        employees.append({
            'name': name,
            'visits': int(f[0]), 'newClients': int(f[1]), 'rqs': int(f[2]), 'rating': f[3],
            'servicesExVat': to_num(f[4]), 'servicesTotal': to_num(f[5]),
            'coursesExVat': to_num(f[6]), 'coursesTotal': to_num(f[7]),
            'productsExVat': to_num(f[8]), 'productsTotal': to_num(f[9]),
            'totalExVat': to_num(f[10]), 'totalTotal': to_num(f[11]),
            'avgSpendExVat': to_num(f[12]), 'avgSpendTotal': to_num(f[13]),
        })
        i += need

    if not employees and not totals:
        raise ValueError('No employee rows recognised — the report format may have changed.')

    closed = (not employees) and totals is not None and all(
        not totals[k] for k in ('servicesExVat', 'servicesTotal', 'coursesExVat', 'coursesTotal',
                                  'productsExVat', 'productsTotal', 'totalExVat', 'totalTotal'))

    return {'branch': branch_code, 'date': date_from, 'employees': employees, 'totals': totals, 'closed': closed}


def build_rows(rec):
    iso_date = to_iso_date(rec['date'])
    rows = []
    for e in rec['employees']:
        rows.append({
            'branch': rec['branch'], 'date': iso_date, 'employee_name': e['name'], 'is_total': False,
            'visits': e['visits'], 'new_clients': e['newClients'], 'rqs': e['rqs'], 'rating': e['rating'],
            'services_ex_vat': e['servicesExVat'], 'services_total': e['servicesTotal'],
            'courses_ex_vat': e['coursesExVat'], 'courses_total': e['coursesTotal'],
            'products_ex_vat': e['productsExVat'], 'products_total': e['productsTotal'],
            'total_ex_vat': e['totalExVat'], 'total_total': e['totalTotal'],
            'avg_spend_ex_vat': e['avgSpendExVat'], 'avg_spend_total': e['avgSpendTotal'],
        })
    if rec['totals']:
        t = rec['totals']
        rows.append({
            'branch': rec['branch'], 'date': iso_date, 'employee_name': 'TOTAL', 'is_total': True,
            'visits': None, 'new_clients': None, 'rqs': None, 'rating': None,
            'services_ex_vat': t['servicesExVat'], 'services_total': t['servicesTotal'],
            'courses_ex_vat': t['coursesExVat'], 'courses_total': t['coursesTotal'],
            'products_ex_vat': t['productsExVat'], 'products_total': t['productsTotal'],
            'total_ex_vat': t['totalExVat'], 'total_total': t['totalTotal'],
            'avg_spend_ex_vat': None, 'avg_spend_total': None,
        })
    return rows


def parse_pdf(pdf_path):
    pdf_path = Path(pdf_path)
    branch = branch_from_filename(pdf_path.name)
    if not branch:
        raise ValueError(f"Could not match filename '{pdf_path.name}' to a branch.")
    filename_date = date_from_filename(pdf_path.name)

    raw_lines = extract_lines(pdf_path)
    lines = strip_pdf_header_lines(raw_lines)
    rec = parse_one_block(lines, branch)
    iso_date = to_iso_date(rec['date'])
    if filename_date and filename_date != iso_date:
        raise ValueError(f"Filename says {filename_date} but report content says {iso_date}.")

    rows = build_rows(rec)
    return {'file': pdf_path.name, 'branch': branch, 'date': iso_date, 'closed': rec['closed'], 'rows': rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pdfs', nargs='+', help='PDF file(s), globs expanded by the shell')
    ap.add_argument('--out', help='write all rows as JSON to this path')
    args = ap.parse_args()

    results = []
    for p in args.pdfs:
        try:
            res = parse_pdf(p)
            results.append(res)
            n_emp = len([r for r in res['rows'] if not r['is_total']])
            status = 'CLOSED (no trading)' if res['closed'] else f"{n_emp} employee rows + TOTAL"
            print(f"[OK]   {res['file']} -> {res['branch']} {res['date']} — {status}")
        except Exception as e:
            print(f"[FAIL] {p} -> {e}", file=sys.stderr)

    if args.out:
        all_rows = [r for res in results for r in res['rows']]
        Path(args.out).write_text(json.dumps(all_rows, indent=2), encoding='utf-8')
        print(f"\nWrote {len(all_rows)} rows to {args.out}")


if __name__ == '__main__':
    main()
