"""Reading files: spreadsheets, PDFs, photos, Word documents, emails and plain text.

`read` turns any file into a `Content` the AI can look at: compact text, page images for scans and photos, and the first
rows of each sheet. `sheet_rows` streams every row of one sheet for importing. `read` never raises for a bad file; it
comes back with `error`, a plain sentence saying what's wrong. Everything is read in bounded amounts, so a huge or
hostile file can't exhaust memory.
"""
import base64
import codecs
import csv
import email
import io
import logging
import math
import re
import threading
import warnings
import zipfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from email import policy
from functools import partial
from html.parser import HTMLParser
from itertools import chain, islice
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

# openpyxl warns about every Excel feature it skips (data validation, slicers…); none of it matters for reading values.
warnings.filterwarnings("ignore", category=UserWarning, module=r"openpyxl\.")

MAX_SIDE = 1600                 # longest side, in pixels, of an image given to the vision model
PREVIEW_ROWS = 15               # rows of each sheet shown in `text`
HEADER_SEARCH = 10              # non-empty rows searched for the header row
TRUNCATED = "\n[…truncated]"
HTML_BYTES = 10_000_000         # most of a web page is markup, so read more of it than of plain text
EMAIL_BYTES = 50_000_000
XML_PART_BYTES = 100_000_000    # an Office file's XML that gets loaded whole (a "zip bomb" expands far past this)
SAMPLE_BYTES = 1_000_000        # read to guess a text file's encoding and delimiter

PASSWORD_PDF = "This PDF is password protected."
PASSWORD_SHEET = "This spreadsheet is password protected."
FAILED = {
    "spreadsheet": "This spreadsheet couldn't be opened; it may be damaged or saved in another format.",
    "pdf": "This PDF couldn't be opened; it may be damaged.",
    "image": "This image couldn't be opened; it may be damaged or in a format that isn't supported.",
    "document": "This document couldn't be opened; it may be damaged.",
    "text": "This file couldn't be read as text.",
    "email": "This email couldn't be read; it may be damaged.",
    "unknown": "This kind of file can't be read.",
}

_KINDS = {
    "spreadsheet": (".xlsx", ".xlsm", ".xls", ".csv", ".tsv"),
    "pdf": (".pdf",),
    "image": (".png", ".jpg", ".jpeg", ".jfif", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif"),
    "document": (".docx", ".doc"),
    "text": (".txt", ".md", ".markdown", ".json", ".xml", ".html", ".htm"),
    "email": (".eml",),
}
_KIND_OF = {ext: kind for kind, exts in _KINDS.items() for ext in exts}
_ZIP, _OLE = b"PK\x03\x04", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1")


@dataclass
class Sheet:
    name: str
    headers: list[str]          # unique labels; a blank one becomes "Column 3", a repeat "Amount (2)"
    rows: list[list]            # the first data rows: str, int, float, bool, None; dates as "YYYY-MM-DD" strings
    row_count: int              # every data row in the sheet
    formulas: dict = field(default_factory=dict)  # a column the sheet works out: heading → its formula in headings ("[Qty] * [Price]")


@dataclass
class Content:
    kind: str                   # "spreadsheet" | "pdf" | "image" | "document" | "text" | "email" | "unknown"
    name: str
    text: str = ""
    images: list[str] = field(default_factory=list)     # base64 PNGs, no "data:" prefix
    sheets: list[Sheet] = field(default_factory=list)
    pages: int = 0
    error: str = ""
    meta: dict = field(default_factory=dict)


class Unreadable(ValueError):
    """A file that can't be read; the message is a plain sentence saying why."""


def kind_of(name) -> str:
    return _KIND_OF.get(Path(str(name)).suffix.lower(), "unknown")


def read(path, name=None, max_chars=60_000, max_rows=50, max_images=4) -> Content:
    path = Path(path)
    name = name or path.name
    content = Content(kind=kind_of(name), name=name)
    reader = _READERS.get(content.kind, _read_unknown)
    try:
        if not path.is_file():
            raise Unreadable("This file couldn't be found.")
        if path.stat().st_size == 0:
            raise Unreadable("This file is empty.")
        cut = reader(path, content, max_chars=max_chars, max_rows=max_rows, max_images=max_images)
    except Unreadable as e:
        return Content(kind=content.kind, name=name, error=str(e))
    except Exception:  # a damaged or hostile file can fail in countless ways inside the parsers
        log.info("Couldn't read %s", name, exc_info=True)
        return Content(kind=content.kind, name=name, error=FAILED[content.kind])
    content.text = _truncate(_tidy(content.text), max_chars, cut)
    return content


def sheet_rows(path, sheet=None, name=None) -> tuple[list[str], Iterator[list]]:
    """The headers and every data row of one sheet (the first visible one unless `sheet` names another), cleaned the
    same way as `read`. The sheet is read twice: once now to settle the columns, then lazily as the rows are taken, so
    a big import never holds the whole file. Raises Unreadable."""
    path = Path(path)
    name = name or path.name
    if kind_of(name) != "spreadsheet" and Path(name).suffix.lower() != ".txt":
        raise Unreadable("Only spreadsheets (.xlsx, .xls, .csv) can be imported as rows.")
    try:
        with _open_book(path, name) as book:
            index = _sheet_index(book, sheet)
            layout = _scan(book[index][2](), max_rows=0)
    except Unreadable:
        raise
    except Exception as e:
        raise Unreadable(FAILED["spreadsheet"]) from e
    return layout.headers, _stream_rows(path, name, index, layout)


# Spreadsheets -----------------------------------------------------------------------------------------------------

@dataclass
class _Layout:
    header_at: int              # index of the header among the raw rows, or -1 when there's none
    columns: list[int]          # raw positions of the columns kept
    headers: list[str]
    count: int
    rows: list[list]


def _read_spreadsheet(path, content, max_rows, **_):
    hidden, layouts = [], []
    with _open_book(path, content.name) as book:
        for sheet_name, is_hidden, rows in book:
            layout = _scan(rows(), max_rows)
            layouts.append(layout)
            content.sheets.append(Sheet(sheet_name, layout.headers, layout.rows, layout.count))
            if is_hidden:
                hidden.append(sheet_name)
    try:
        _formulas(path, content.sheets, layouts)
    except Exception:  # noqa: BLE001 - the values are what's read; the formulas only say how some were worked out
        log.info("Couldn't read the formulas of %s", content.name, exc_info=True)
    content.text = "\n\n".join(_render(s) for s in content.sheets)
    if hidden:
        content.meta["hidden_sheets"] = hidden


@contextmanager
def _open_book(path, name):
    """Yields [(sheet name, hidden, rows), …], visible sheets first; `rows()` iterates a sheet's raw rows afresh."""
    ext = Path(name).suffix.lower()
    with open(path, "rb") as f:
        head = f.read(512)
    # Trust the bytes over the name: "report.xls" is often a renamed .xlsx, or a CSV or web page from some export.
    if head.startswith(_ZIP):
        opener = _open_xlsx
    elif head.startswith(_OLE):
        opener = _open_xls
    elif ext in (".xlsx", ".xlsm"):
        raise Unreadable(FAILED["spreadsheet"])
    elif ext == ".xls" and head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
        raise Unreadable("This file is a web page saved with a spreadsheet name; open it in Excel and save it as .xlsx.")
    else:
        opener = _open_delimited
    with opener(path, name) as book:
        yield book


@contextmanager
def _open_xlsx(path, name):
    import openpyxl

    _guard_zip(path, "xl/sharedStrings.xml")  # the one part openpyxl loads whole, even in read-only mode
    # Given a path, openpyxl insists on an Excel extension; uploads are often stored without one.
    with open(path, "rb") as f:
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        try:
            sheets = sorted(wb.worksheets, key=lambda ws: getattr(ws, "sheet_state", "visible") != "visible")
            yield [(ws.title, getattr(ws, "sheet_state", "visible") != "visible", partial(_xlsx_rows, ws))
                   for ws in sheets]
        finally:
            wb.close()


FORMULA_ROWS = 3  # data rows looked at for each column's formula (the first may be a heading's total, or blank)
_REF = re.compile(r"(?<![\w.$])(?:(?P<sheet>'(?:[^']|'')+'|[A-Za-z_][\w.]*)!)?(?P<c1>\$?[A-Z]{1,3})(?P<r1>\$?\d+)?"
                  r"(?::(?P<c2>\$?[A-Z]{1,3})(?P<r2>\$?\d+)?)?(?![\w(!])")


def _formulas(path, sheets, layouts):
    """Each column an Excel sheet works out with a formula (a line's total, a price looked up on another sheet, a sum of
    another sheet's amounts), written with the columns' headings instead of cell addresses: the spreadsheet's own
    logic, for the app to keep as calculations rather than typed-in numbers."""
    with open(path, "rb") as f:
        if f.read(4) != _ZIP:
            return  # CSV has no formulas; old .xls keeps only their results
    import openpyxl

    with open(path, "rb") as f:
        wb = openpyxl.load_workbook(f, read_only=True, data_only=False)
        try:
            known = {sheet.name: (sheet, layout) for sheet, layout in zip(sheets, layouts)}
            for ws in wb.worksheets:
                sheet, layout = known.get(ws.title, (None, None))
                if sheet is None or layout.header_at < 0:
                    continue
                ws.reset_dimensions()
                looked = 0
                for i, raw in enumerate(ws.iter_rows(values_only=True)):
                    if i <= layout.header_at or not any(v is not None for v in raw):
                        continue
                    for at, heading in zip(layout.columns, sheet.headers):
                        value = getattr(raw[at], "text", raw[at]) if at < len(raw) else None  # an array formula's text
                        if isinstance(value, str) and value.startswith("=") and heading not in sheet.formulas:
                            sheet.formulas[heading] = _in_headings(wb, value[1:], ws.title, i + 1, known)
                    looked += 1
                    if looked >= FORMULA_ROWS:
                        break
        finally:
            wb.close()


def _in_headings(wb, formula, here, row, known):
    """=C2*D2 as [Quantity] * [Unit price]; Products!C:C as “Products” [Price]; $F$1 as GST rate (0.1, cell F1)."""
    from openpyxl.utils import column_index_from_string

    def heading(sheet_name, col):
        sheet, layout = known.get(sheet_name, (None, None))
        at = column_index_from_string(col.strip("$")) - 1
        return sheet.headers[layout.columns.index(at)] if sheet is not None and at in layout.columns else None

    def ref(m):
        if not m.group("r1") and not m.group("c2"):
            return m.group(0)  # a name, not a cell
        name = m.group("sheet").strip("'").replace("''", "'") if m.group("sheet") else here
        where = "" if name == here else f"“{name}” "
        first = heading(name, m.group("c1"))
        if m.group("c2"):  # a range: a whole column of another sheet (what a VLOOKUP or SUMIF looks in)
            last = heading(name, m.group("c2"))
            if first and m.group("c1").strip("$") == m.group("c2").strip("$"):
                return f"{where}[{first}]" if where else f"all of [{first}]"
            return f"{where}[{first or m.group('c1')}]…[{last or m.group('c2')}]"
        number = int(m.group("r1").strip("$"))
        if first and number == row and name == here:
            return f"[{first}]"
        if first and name != here and number == row:
            return f"{where}[{first}]"
        return _fixed_cell(wb, name, m.group("c1").strip("$"), number) + (f" on “{name}”" if where else "")

    parts = re.split(r'("(?:[^"]|"")*")', formula)  # leave text in quotes as it is
    out = "".join(part if part.startswith('"') else _REF.sub(ref, part) for part in parts)
    out = re.sub(r"\s*([*/+-])\s*", r" \1 ", out).replace("( ", "(")
    return re.sub(r",(?=\S)", ", ", out).strip()[:300]


def _fixed_cell(wb, sheet_name, col, number):
    """A cell the formula always uses (a tax rate at the top of the sheet): its label beside it, its value, its address."""
    from openpyxl.utils import column_index_from_string

    at = column_index_from_string(col)
    try:
        ws = wb[sheet_name]
        cells = next(ws.iter_rows(min_row=number, max_row=number, min_col=max(1, at - 1), max_col=at, values_only=True), ())
    except (KeyError, StopIteration, ValueError):
        cells = ()
    label = cells[0] if len(cells) == 2 and isinstance(cells[0], str) and not cells[0].startswith("=") else None
    value = cells[-1] if cells else None
    shown = "" if value is None or (isinstance(value, str) and value.startswith("=")) else f"{value}, "
    return f"{label.strip()} ({shown}cell {col}{number})" if label else f"cell {col}{number}" + (f" ({value})" if shown else "")


def _xlsx_rows(ws):
    ws.reset_dimensions()  # go by the cells actually present; some files claim a million empty rows and columns
    return ws.iter_rows(values_only=True)


@contextmanager
def _open_xls(path, name):
    import xlrd

    try:
        book = xlrd.open_workbook(str(path), logfile=io.StringIO())
    except Exception as e:
        # A password-protected .xlsx is wrapped in the old OLE container, so it ends up here.
        if "encrypted" in str(e).lower() or Path(name).suffix.lower() in (".xlsx", ".xlsm"):
            raise Unreadable(PASSWORD_SHEET) from e
        raise
    try:
        sheets = sorted(book.sheets(), key=lambda s: s.visibility != 0)
        yield [(s.name, s.visibility != 0, partial(_xls_rows, book, s)) for s in sheets]
    finally:
        book.release_resources()


def _xls_rows(book, sheet):
    for r in range(sheet.nrows):
        yield [_xls_value(book, cell) for cell in sheet.row(r)]


def _xls_value(book, cell):
    import xlrd

    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return xlrd.error_text_from_code.get(cell.value, "#ERROR")
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            moment = xlrd.xldate.xldate_as_datetime(cell.value, book.datemode)
        except (ValueError, OverflowError, xlrd.xldate.XLDateError):
            return cell.value
        return moment.time() if cell.value < 1 else moment  # a bare time is stored as a fraction of a day
    return cell.value


@contextmanager
def _open_delimited(path, name):
    encoding, delimiter, _ = _sniff(path, name)

    def rows():
        with open(path, encoding=encoding, errors="replace", newline="") as f:
            yield from csv.reader(f, delimiter=delimiter)

    yield [(Path(name).stem, False, rows)]


def _sniff(path, name):
    """The encoding and delimiter of a delimited text file, and whether it looks like a table at all."""
    with open(path, "rb") as f:
        raw = f.read(SAMPLE_BYTES)
    encoding = _encoding(raw)
    if Path(name).suffix.lower() == ".tsv":
        return encoding, "\t", True
    lines = codecs.getincrementaldecoder(encoding)(errors="replace").decode(raw).splitlines()
    if len(raw) == SAMPLE_BYTES:
        lines = lines[:-1]  # the last line was probably cut short
    lines = [line for line in lines if line.strip()][:50]
    try:
        sniffed = csv.Sniffer().sniff("\n".join(lines), delimiters=",;\t|").delimiter
    except csv.Error:
        sniffed = None
    # The Sniffer favours "," even when it's the decimal comma in a ";" file, so check which delimiter splits the
    # lines into the most consistent number of columns.
    scores = {d: _delimiter_score(lines, d, sniffed) for d in ",;\t|"}
    delimiter = max(scores, key=scores.get)
    wide, consistency = scores[delimiter][:2]
    return encoding, delimiter, wide and consistency >= 0.9 and len(lines) >= 3


def _delimiter_score(lines, delimiter, sniffed):
    try:
        widths = [len(row) for row in csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)]
    except csv.Error:
        return (False, 0, 0, False)
    if not widths:
        return (False, 0, 0, False)
    width, n = Counter(widths).most_common(1)[0]
    return (width >= 2, round(n / len(widths), 2), width, delimiter == sniffed)


def _scan(rows, max_rows):
    """One pass over a sheet's raw rows: find the header, the columns in use, count the data rows, keep the first."""
    rows = enumerate(rows)
    head = list(islice(((i, row) for i, row in rows if not _blank(row)), HEADER_SEARCH))
    at = _find_header([row for _, row in head])
    header_at, header = head[at] if at is not None else (-1, ())
    used = {j for j, v in enumerate(header) if _clean(v) is not None}
    count, kept = 0, []
    for _, raw in chain(head[at + 1 if at is not None else 0:], rows):
        row = [_clean(v) for v in raw]
        filled = [j for j, v in enumerate(row) if v is not None]
        if not filled:
            continue
        used.update(filled)
        count += 1
        if count <= max_rows:
            kept.append(row)
    columns = sorted(used)
    headers = _labels([header[j] if j < len(header) else None for j in columns])
    return _Layout(header_at, columns, headers, count, [_project(row, columns) for row in kept])


def _find_header(rows):
    """The first row with two or more filled cells, at least half of them words rather than numbers or dates. A row
    no more than half as wide as the table (a "Printed: | 3 May" line above it) is passed over."""
    sizes = [sum(_clean(v) is not None for v in row) for row in rows]
    widest = max(sizes, default=0)
    for k, row in enumerate(rows):
        labels = sum(_is_label(v) for v in row)
        if sizes[k] >= 2 and labels * 2 >= sizes[k] and sizes[k] * 2 > widest:
            return k
    return None


def _stream_rows(path, name, index, layout):
    try:
        with _open_book(path, name) as book:
            for i, raw in enumerate(book[index][2]()):
                if i > layout.header_at:
                    row = [_clean(v) for v in raw]
                    if any(v is not None for v in row):
                        yield _project(row, layout.columns)
    except Unreadable:
        raise
    except Exception as e:
        raise Unreadable(FAILED["spreadsheet"]) from e


def _sheet_index(book, sheet):
    if not book:
        raise Unreadable("This spreadsheet has no sheets.")
    if sheet is None:
        return 0
    if isinstance(sheet, int) and 0 <= sheet < len(book):
        return sheet
    names = [n for n, _, _ in book]
    for wanted in (str(sheet), str(sheet).strip().casefold()):
        for k, n in enumerate(names):
            if n == wanted or n.strip().casefold() == wanted:
                return k
    raise Unreadable(f"There's no sheet called “{sheet}”.")


_NUMBER = re.compile(r"[-+(]?\s*[$€£¥]?\s*[-+]?\d[\d,.\s]*%?\)?")
_DATE = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?")


def _is_label(value):
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(text) and not _NUMBER.fullmatch(text) and not _DATE.fullmatch(text)


def _clean(value):
    """A cell as a plain Python value: trimmed text (None when blank), numbers, bools, and dates as ISO text."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return int(value) if value.is_integer() and abs(value) < 2 ** 53 else value
    if isinstance(value, datetime):
        if value.time() == time(0):
            return value.date().isoformat()
        return value.replace(tzinfo=None).isoformat(" ", "minutes")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat("minutes")
    if isinstance(value, timedelta):
        hours, minutes = divmod(round(value.total_seconds() / 60), 60)
        return f"{hours}:{minutes:02d}"
    return str(value).strip() or None


def _blank(row):
    return all(_clean(v) is None for v in row)


def _project(row, columns):
    return [row[j] if j < len(row) else None for j in columns]


def _labels(values):
    labels, seen = [], set()
    for n, value in enumerate(values, 1):
        cleaned = _clean(value)
        base = " ".join(str(cleaned).split()) if cleaned is not None else f"Column {n}"
        label, k = base, 1
        while label.casefold() in seen:
            k += 1
            label = f"{base} ({k})"
        seen.add(label.casefold())
        labels.append(label)
    return labels


def _render(sheet):
    lines = [f"## Sheet: {sheet.name} ({_count(sheet.row_count, 'row')})"]
    if sheet.headers:
        shown = sheet.rows[:PREVIEW_ROWS]
        lines += [_markdown_row(sheet.headers)] + [_markdown_row(row) for row in shown]
        if sheet.row_count > len(shown):
            lines.append(f"… {_count(sheet.row_count - len(shown), 'more row')}")
    if sheet.formulas:
        lines.append("Worked out by formulas: " + "; ".join(f"{k} = {v}" for k, v in sheet.formulas.items()))
    return "\n".join(lines)


def _count(n, noun):
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _markdown_row(values):
    return "| " + " | ".join(_cell_text(v) for v in values) + " |"


def _cell_text(value):
    if value is None:
        return ""
    text = format(value, ".15g") if isinstance(value, float) else str(value)
    text = " ".join(text.split()).replace("|", "\\|")
    return text if len(text) <= 200 else text[:199] + "…"


# PDFs -------------------------------------------------------------------------------------------------------------

_pdfium_lock = threading.Lock()  # PDFium isn't thread-safe, and the web app and the worker may both render at once


def _read_pdf(path, content, max_chars, max_images, **_):
    from pypdf import PdfReader

    texts, size, failed = [], 0, False
    try:
        with open(path, "rb") as f:  # a file, not a path: given a path, pypdf reads the whole file into memory
            reader = PdfReader(f)
            if reader.is_encrypted and not reader.decrypt(""):
                raise Unreadable(PASSWORD_PDF)
            content.pages = len(reader.pages)
            for page in reader.pages:
                try:
                    texts.append((page.extract_text() or "").strip())
                except Exception:
                    texts.append("")
                    failed = True
                size += len(texts[-1])
                if size > max_chars:
                    break
    except Unreadable:
        raise
    except Exception:
        log.info("pypdf couldn't read %s; rendering it instead", content.name, exc_info=True)
        texts, size, failed = [], 0, True
    if size:
        content.text = "\n\n".join(f"--- page {n} ---\n{text}" for n, text in enumerate(texts, 1))
    if failed or not texts or size / len(texts) < 100:  # little or no text: a scan, so let the vision model look
        content.images, pages = _render_pdf(path, max_images)
        content.pages = content.pages or pages
    return size > max_chars


def _render_pdf(path, max_images):
    import pypdfium2 as pdfium

    with _pdfium_lock:
        try:
            pdf = pdfium.PdfDocument(str(path))
        except pdfium.PdfiumError as e:
            if getattr(e, "err_code", None) == pdfium.raw.FPDF_ERR_PASSWORD:
                raise Unreadable(PASSWORD_PDF) from e
            raise
        try:
            images = []
            for i in range(min(len(pdf), max_images)):
                page = pdf[i]
                try:
                    width, height = page.get_size()
                    bitmap = page.render(scale=min(MAX_SIDE / max(width, height, 1), 4))
                    try:
                        image = bitmap.to_pil().convert("RGB")  # a copy, so the bitmap can be freed
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                images.append(_png(image))
            return images, len(pdf)
        finally:
            pdf.close()


# Images -----------------------------------------------------------------------------------------------------------

def _read_image(path, content, max_images, **_):
    from PIL import Image, UnidentifiedImageError

    with open(path, "rb") as f:
        head = f.read(16)
    if Path(content.name).suffix.lower() in (".heic", ".heif") or (head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS):
        raise Unreadable("HEIC photos aren't supported yet; export as JPG.")
    try:
        with Image.open(path) as image:
            frames = getattr(image, "n_frames", 1) if image.format == "TIFF" else 1  # faxes and scans: one per page
            content.pages = frames
            for k in range(min(frames, max_images)):
                image.seek(k)
                content.images.append(_png(_upright(image)))
    except Image.DecompressionBombError as e:
        raise Unreadable("This image is too large to read.") from e
    except UnidentifiedImageError as e:
        raise Unreadable(FAILED["image"]) from e


def _upright(image):
    """The image turned the way the camera held it, in RGB on white, decoded no bigger than needed."""
    from PIL import Image, ImageOps

    if image.format in ("JPEG", "MPO"):  # MPO: a phone's JPEG with extra frames
        ratio = MAX_SIDE / max(image.size)
        if ratio < 1:  # let the JPEG decoder shrink it by 2, 4 or 8 rather than decode every pixel of a big photo
            image.draft(image.mode, (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))))
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        image = Image.new("RGB", rgba.size, "white")
        image.paste(rgba, mask=rgba.getchannel("A"))  # transparent areas would otherwise turn black
    return image.convert("RGB")


def _png(image):
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


# Word documents ---------------------------------------------------------------------------------------------------

def _read_document(path, content, max_chars, **_):
    if Path(content.name).suffix.lower() == ".doc":
        raise Unreadable("Old Word .doc files can't be read; save it as .docx.")
    import docx

    _guard_zip(path)
    document = docx.Document(str(path))
    lines, size = [], 0
    for line in _docx_lines(document):
        lines.append(line)
        size += len(line) + 1
        if size > max_chars:
            break
    content.text = "\n".join(lines)
    return size > max_chars


def _docx_lines(document):
    """Headers, the body (paragraphs and tables in order) and footers, as lines of text."""
    seen = set()
    for which in ("header", None, "footer"):
        if which is None:
            parts = [document]
        else:
            parts = [getattr(s, which) for s in document.sections]
            parts = [p for p in parts if not p.is_linked_to_previous]
        for part in parts:
            for line in _block_lines(part):
                if which is None:
                    yield line
                elif line not in seen:  # sections usually repeat the same header and footer
                    seen.add(line)
                    yield line


def _block_lines(container):
    from docx.table import Table

    for block in container.iter_inner_content():
        if isinstance(block, Table):
            yield from _table_lines(block)
        else:
            yield block.text


def _table_lines(table):
    for row in table.rows:
        cells, last = [], None
        for cell in row.cells:
            if cell._tc is not last:  # a merged cell comes back once for every column it spans
                cells.append(" ".join(cell.text.split()))
                last = cell._tc
        if any(cells):
            yield " | ".join(cells)


# Text, web pages and emails ---------------------------------------------------------------------------------------

def _read_text(path, content, max_chars, max_rows, **_):
    ext = Path(content.name).suffix.lower()
    if ext == ".txt" and _sniff(path, content.name)[2]:
        content.kind = "spreadsheet"
        return _read_spreadsheet(path, content, max_rows=max_rows)
    is_html = ext in (".html", ".htm")
    limit = HTML_BYTES if is_html else max_chars * 4  # 4 bytes is the most a character can take
    with open(path, "rb") as f:
        raw = f.read(limit + 1)
    cut = len(raw) > limit
    raw = raw[:limit]
    text = codecs.getincrementaldecoder(_encoding(raw))(errors="replace").decode(raw)
    content.text = _html_text(text) if is_html else text
    return cut


def _read_email(path, content, max_chars, **_):
    if path.stat().st_size > EMAIL_BYTES:
        raise Unreadable("This email is too large to read.")
    with open(path, "rb") as f:
        message = email.message_from_binary_file(f, policy=policy.default)
    content.meta = {
        "from": _header(message, "from"),
        "to": _header(message, "to"),
        "subject": _header(message, "subject"),
        "date": _email_date(message),
        "attachments": [part.get_filename() or f"Untitled ({part.get_content_type()})"
                        for part in message.iter_attachments()],
    }
    body = message.get_body(preferencelist=("plain", "html"))
    if body is not None:
        text = _part_text(body)
        content.text = _html_text(text) if body.get_content_type() == "text/html" else text


def _header(message, key):
    try:
        return " ".join(str(message.get(key, "")).split())
    except Exception:  # the email package can trip over a badly formed header
        return ""


def _email_date(message):
    try:
        moment = message["date"].datetime
    except Exception:
        return _header(message, "date")
    return moment.isoformat(timespec="minutes") if moment else _header(message, "date")


def _part_text(part):
    try:
        return part.get_content()
    except Exception:  # an unknown or wrong charset
        raw = part.get_payload(decode=True) or b""
        return raw.decode(_encoding(raw), errors="replace")


class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg"}
    LINES = {"br", "tr", "li", "dt", "dd"}  # start a new line
    BLOCKS = {"p", "div", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
              "footer", "blockquote", "pre", "hr", "title", "address", "form"}  # stand apart, with a blank line

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skipping = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCKS or tag in self.LINES:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def _html_text(markup):
    parser = _HTMLText()
    parser.feed(markup)
    parser.close()
    lines = (" ".join(line.split()).strip(" |") for line in "".join(parser.parts).splitlines())
    return "\n".join(lines)


def _read_unknown(path, content, **_):
    raise Unreadable(FAILED["unknown"])


_READERS = {
    "spreadsheet": _read_spreadsheet,
    "pdf": _read_pdf,
    "image": _read_image,
    "document": _read_document,
    "text": _read_text,
    "email": _read_email,
}


# Helpers ----------------------------------------------------------------------------------------------------------

def _encoding(raw):
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):  # Excel's "Unicode text" export
        return "utf-16"
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            codecs.getincrementaldecoder(encoding)().decode(raw)  # incremental: the sample may end mid-character
            return encoding
        except UnicodeDecodeError:
            pass
    return "latin-1"


def _guard_zip(path, *names):
    """Refuse an Office file whose XML (just `names`, or all of it) would expand past what's safe to load."""
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.filename.endswith(".xml") and (not names or info.filename in names):
                if info.file_size > XML_PART_BYTES:
                    raise Unreadable("This file is too large to read.")


def _tidy(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _truncate(text, max_chars, cut=False):
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + TRUNCATED
    return text + TRUNCATED if cut and text else text
