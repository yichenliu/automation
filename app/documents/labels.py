"""Labels for things people stick a code on: a product's barcode, a bin's location, an asset tag, a work order's job
card, a parcel. Each label shows the record's name, a few of its details, and a code a scanner reads back: the record's
own barcode where it has one, otherwise a QR code that opens the record in the app.

Every list has a plain label without anything being designed; a table's "label" documents choose what's on it, its size
and how many copies (one per box received). `render` lays them out for a label printer (one label per page, at the
label's size) or on a sheet of labels for an ordinary printer.
"""
import io

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from . import pdf

# key: (name people choose from, label width, height in mm, sheet (columns, rows, page, margins) or None for a roll)
SIZES = {
    "62x29": ("Small label 62 × 29 mm (Brother, Dymo)", 62, 29, None),
    "50x25": ("Small label 50 × 25 mm (2 × 1 in, Zebra)", 50, 25, None),
    "62x100": ("Large label 62 × 100 mm (Brother)", 62, 100, None),
    "100x150": ("Shipping label 100 × 150 mm (4 × 6 in)", 100, 150, None),
    "a4_21": ("A4 sheet of 21 (63.5 × 38.1 mm, Avery L7160)", 63.5, 38.1, (3, 7, A4, 7.2, 15.1, 2.5, 0)),
    "letter_30": ("Letter sheet of 30 (2.63 × 1 in, Avery 5160)", 66.7, 25.4, (3, 10, LETTER, 4.8, 12.7, 3.2, 0)),
}
DEFAULT_SIZE = "62x29"
MAX_LABELS = 2000  # one print run


SIZE_WORDS = [("100x150", r"100\s*x\s*150|4\s*x\s*6|shipping|parcel|courier"), ("62x100", r"62\s*x\s*100|large"),
              ("50x25", r"50\s*x\s*25|2\s*x\s*1\b|zebra"), ("a4_21", r"\ba4|l7160|sheet"), ("letter_30", r"letter|5160"),
              ("62x29", r"62\s*x\s*29|small|brother|dymo")]


def size_of(key):
    """A size by its key, or as people say it ("4x6", "A4 sheet", "small"); the small roll label otherwise."""
    import re
    text = str(key or "").strip().lower()
    if text in SIZES:
        return text
    return next((k for k, words in SIZE_WORDS if re.search(words, text)), DEFAULT_SIZE)


def render(labels, size=DEFAULT_SIZE):
    """labels: [{"title", "lines": [str], "code", "kind": "barcode" | "qr", "caption"}] → PDF bytes."""
    _, w, h, sheet = SIZES[size_of(size)]
    w, h = w * mm, h * mm
    out = io.BytesIO()
    fonts = pdf._fonts()
    if sheet is None:
        c = canvas.Canvas(out, pagesize=(w, h))
        for label in labels[:MAX_LABELS] or [{"title": "", "lines": []}]:
            _draw(c, label, 0, 0, w, h, fonts)
            c.showPage()
    else:
        cols, rows, page, left, top, gap_x, gap_y = sheet
        c = canvas.Canvas(out, pagesize=page)
        per_page = cols * rows
        for n, label in enumerate(labels[:MAX_LABELS]):
            if n and n % per_page == 0:
                c.showPage()
            col, row = n % per_page % cols, n % per_page // cols
            x = left * mm + col * (w + gap_x * mm)
            y = page[1] - top * mm - (row + 1) * h - row * gap_y * mm
            _draw(c, label, x, y, w, h, fonts)
        c.showPage()
    c.save()
    return out.getvalue()


def _draw(c, label, x, y, w, h, fonts):
    pad = min(w, h) * 0.07
    inner_w, inner_h = w - 2 * pad, h - 2 * pad
    code, kind = str(label.get("code") or ""), label.get("kind") or "qr"
    tall = h > w * 1.2  # a portrait label: the code under the words, not beside them
    words_w, floor = inner_w, y + pad  # the words go from the top down to the floor
    if code and kind == "qr":
        side = min(inner_h * (0.5 if tall else 1), inner_w * (0.8 if tall else 0.4))
        drawing = createBarcodeDrawing("QR", value=code, width=side, height=side, barBorder=0)
        renderPDF.draw(drawing, c, x + pad + ((inner_w - side) / 2 if tall else inner_w - side), y + pad)
        if tall:
            floor = y + pad + side + 4
        else:
            words_w = inner_w - side - pad
    elif code:
        bar_h = min(inner_h * (0.25 if tall else 0.36), 30 * mm)
        text_size = max(5, min(9, bar_h / 3))
        try:  # the bars as wide as the label allows (wider bars scan from further away), up to a scanner's comfort
            modules = createBarcodeDrawing("Code128", value=code, barWidth=1, barHeight=bar_h, quiet=False).width
            drawing = createBarcodeDrawing("Code128", value=code, barWidth=min(inner_w * 0.95 / max(modules, 1), 2.4), barHeight=bar_h,
                                           humanReadable=True, fontName=fonts.regular, fontSize=text_size, quiet=False)
        except Exception:  # noqa: BLE001 - a value Code 128 can't hold: a QR code holds anything
            return _draw(c, {**label, "kind": "qr"}, x, y, w, h, fonts)
        renderPDF.draw(drawing, c, x + pad + (inner_w - drawing.width) / 2, y + pad + text_size + 1)
        floor = y + pad + text_size + 1 + bar_h + 3
    room = y + h - pad - floor
    title = str(label.get("title") or "")
    base = max(6.0, min(28.0 if tall else 15.0, room / (5 if tall else 3.4)))
    size = base
    while size > max(6.0, base * 0.7) and c.stringWidth(title, fonts.bold, size) > words_w:
        size *= 0.92  # a long name a little smaller, then cut short
    top = y + h - pad
    for text, font, points in [(title, fonts.bold, size)] + [(t, fonts.regular, max(5.5, size * 0.66)) for t in label.get("lines") or []]:
        if top - points < floor:
            break
        top -= points * 1.15
        c.setFont(font, points)
        c.drawString(x + pad, top + points * 0.15, _fit(c, str(text), font, points, words_w))


def _fit(c, text, font, points, width):
    if c.stringWidth(text, font, points) <= width:
        return text
    while text and c.stringWidth(text + "…", font, points) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def for_records(engine, records, doc=None, link=None):
    """The labels for some records: as a "label" document says, or the plain one (title, three details, a code)."""
    if not records:
        return []
    t = engine.tables[records[0].table]
    code_field = engine.field(t["key"], doc["code_field"]) if doc and doc.get("code_field") else \
        next((f for f in t["fields"] if f["type"] == "barcode"), None)
    shown = [engine.field(t["key"], k) for k in (doc or {}).get("fields") or []] if doc else \
        [f for f in t["fields"] if f["type"] in ("text", "select", "date", "number", "money", "link", "autonumber", "address")
         and f is not code_field and not f.get("sensitive") and f["key"] != t.get("title_field")][:3]
    shown = [f for f in shown if f is not None and engine.sees(f)]
    copies = engine.field(t["key"], doc["copies_field"]) if doc and doc.get("copies_field") else None
    title_field = engine.field(t["key"], doc["title_field"]) if doc and doc.get("title_field") else None
    out = []
    for r in records:
        value = engine.value(r, code_field["key"]) if code_field is not None else None
        kind = (doc or {}).get("code") or ("barcode" if value else "qr")
        code = str(value) if value else (link(r) if link else str(r.id))
        title = engine.display(title_field, engine.value(r, title_field["key"]), r) if title_field else engine.title(r)
        lines = [f"{f['label']}: {engine.display(f, engine.value(r, f['key']), r)}" for f in shown
                 if engine.display(f, engine.value(r, f["key"]), r) not in ("—", "")]
        label = {"title": title, "lines": lines, "code": code, "kind": kind if kind in ("barcode", "qr") else "qr"}
        many = int(engine.value(r, copies["key"]) or 1) if copies is not None else 1
        out += [label] * max(1, min(many, 500))
    return out[:MAX_LABELS]
