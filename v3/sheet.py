"""v3 sheet parser. No header is assumed: row 1 is data.

Columns A-D are read, in worksheet order. A row with an empty
column A is skipped and counted. Values are returned as strings
(stripped; empty = "").
"""

def _cell(value):
    if value is None:
        return ""
    return str(value).strip()


def parse_xlsx_detailed(path):
    """Return (rows, sheet_names, skipped): rows are (a, b, c, d)
    tuples; skipped counts rows dropped for empty column A."""
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    rows = []
    sheets = []
    skipped = 0
    for ws in wb.worksheets:
        sheets.append(ws.title)
        for values in ws.iter_rows(values_only=True):
            if values is None:
                continue
            cols = [_cell(v) for v in (list(values) + ["", "", "", ""])[:4]]
            if cols[0] == "":
                skipped += 1
                continue
            rows.append(tuple(cols))
    return rows, sheets, skipped


def parse_xlsx(path):
    rows, _sheets, _skipped = parse_xlsx_detailed(path)
    return rows


def detect_mode(rows):
    """'2fa' if any row has column B, else 'cookies' (display only)."""
    for row in rows:
        if len(row) > 1 and row[1]:
            return "2fa"
    return "cookies"
