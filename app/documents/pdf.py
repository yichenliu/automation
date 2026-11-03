"""Turns a business document (tax invoice, quote, purchase order, statement, delivery note...) given as a plain dict of
display-ready strings into a polished PDF. `render(doc)` returns the PDF bytes. Every string is printed literally:
nothing in it is read as markup."""
import functools
import io
import os
import re
from collections import namedtuple
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (BaseDocTemplate, Frame, Image, KeepTogether, PageTemplate, Paragraph, Spacer, Table,
                                TableStyle)
from reportlab.platypus.flowables import HRFlowable

DEFAULT_ACCENT = "#5b5bd6"
INK = colors.HexColor("#1f2328")
SOFT_INK = colors.HexColor("#3f4650")
MUTED_HEX = "#6b7280"
MUTED = colors.HexColor(MUTED_HEX)
HAIRLINE = colors.HexColor("#e3e5e8")

SIDE_MARGIN = 18 * mm
TOP_MARGIN = 18 * mm
BOTTOM_MARGIN = 24 * mm
LOGO_BOX = (48 * mm, 18 * mm)

_SUPPLEMENTAL = "/System/Library/Fonts/Supplemental/"
_DEJAVU = "/usr/share/fonts/truetype/dejavu/"
# (regular, bold) in order of preference. Arial Unicode has no bold cut, so it borrows Arial Bold, which shares its
# Latin design; bold text that Arial Bold can't draw falls back to the regular face (see _p).
_FONT_CANDIDATES = [
    (_SUPPLEMENTAL + "Arial Unicode.ttf", _SUPPLEMENTAL + "Arial Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", _SUPPLEMENTAL + "Arial Bold.ttf"),
    (_SUPPLEMENTAL + "Arial.ttf", _SUPPLEMENTAL + "Arial Bold.ttf"),
    (_DEJAVU + "DejaVuSans.ttf", _DEJAVU + "DejaVuSans-Bold.ttf"),
]

Fonts = namedtuple("Fonts", "regular bold has_bold")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def render(doc: dict) -> bytes:
    """The PDF for one business document."""
    business = doc.get("business") or {}
    theme = doc.get("theme") or {}
    title, number = _text(doc.get("title")) or "Document", _text(doc.get("number"))
    heading, name = f"{title} {number}".strip(), _text(business.get("name"))
    page_w, page_h = LETTER if _text(doc.get("page_size")).lower() == "letter" else A4
    width, height = page_w - 2 * SIDE_MARGIN, page_h - TOP_MARGIN - BOTTOM_MARGIN
    st = _styles(_colour(theme.get("accent")))

    story = (_header(doc, business, theme, title, number, st, width) + _parties(doc, st, width) + _lines(doc, st, width)
             + _sections(doc, st, width) + _closing(doc, business, st, width, height) + _notes(doc, st))

    def decorate(c, template):
        _draw_footer(c, page_w, doc.get("footer"), st)
        if template.page > 1:
            _draw_running_header(c, page_w, page_h, name, heading, st)

    buffer = io.BytesIO()
    template = BaseDocTemplate(buffer, pagesize=(page_w, page_h), title=heading, author=name, subject=title,
                               creator=name, invariant=1)  # the same document, the same bytes (made twice, kept once)
    frame = Frame(SIDE_MARGIN, BOTTOM_MARGIN, width, height, leftPadding=0, rightPadding=0, topPadding=0,
                  bottomPadding=0)
    template.addPageTemplates([PageTemplate(frames=[frame], onPage=decorate)])
    def stamp(c, page, total):
        _draw_page_number(c, page_w, page, total, st)

    template.build(story, canvasmaker=_numbered_canvas(stamp))
    return buffer.getvalue()


# --- text and fonts ---------------------------------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _fonts():
    """Registers the best TrueType font found on this machine, once per process."""
    for regular, bold in _FONT_CANDIDATES:
        if not os.path.isfile(regular):
            continue
        try:
            pdfmetrics.registerFont(TTFont("DocSans", regular))
        except Exception:
            continue
        try:
            pdfmetrics.registerFont(TTFont("DocSans-Bold", bold))
            return Fonts("DocSans", "DocSans-Bold", True)
        except Exception:
            return Fonts("DocSans", "DocSans", False)
    return Fonts("Helvetica", "Helvetica-Bold", True)


def _covers(font_name, text):
    cmap = getattr(getattr(pdfmetrics.getFont(font_name), "face", None), "charToGlyph", None)
    return cmap is None or all(ord(ch) in cmap for ch in text if not ch.isspace())


def _text(value):
    """A clean display string: None becomes empty, line endings are normalised and control characters dropped."""
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    return _CONTROL_CHARS.sub("", text).strip()


def _markup(text):
    """Escaped text with its line breaks and runs of spaces kept."""
    text = re.sub(r" {2,}", lambda run: " " + "&nbsp;" * (len(run.group()) - 1), escape(text))
    return text.replace("\n", "<br/>")


def _p(text, style, markup=None):
    """A Paragraph of literal text. Bold text the bold face can't draw is set in the regular face instead."""
    text = _text(text)
    if style.fontName != _fonts().regular and not _covers(style.fontName, text):
        style = ParagraphStyle(style.name + "-fallback", parent=style, fontName=_fonts().regular)
    return Paragraph(markup if markup is not None else _markup(text), style)


def _colour(value):
    value = _text(value).lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{3}", value):
        value = "".join(ch * 2 for ch in value)
    return colors.HexColor("#" + (value if re.fullmatch(r"[0-9a-fA-F]{6}", value) else DEFAULT_ACCENT[1:]))


def _tint(colour, amount):
    """`colour` mixed with white; amount 0 is white, 1 is the colour itself."""
    return colors.Color(*(1 - amount + channel * amount for channel in (colour.red, colour.green, colour.blue)))


def _styles(accent):
    fonts = _fonts()
    boost = 1 if fonts.has_bold else 1.15  # without a bold face, hierarchy comes from size alone

    def style(name, size, leading, bold=False, colour=INK, align=TA_LEFT, **extra):
        return ParagraphStyle(name, fontName=fonts.bold if bold else fonts.regular, fontSize=size, leading=leading,
                              textColor=colour, alignment=align, **extra)

    aligns = (TA_LEFT, TA_CENTER, TA_RIGHT)
    return {
        "accent": accent,
        "tint": _tint(accent, 0.07),
        "business": style("business", 11.5 * boost, 15 * boost, bold=True),
        "small": style("small", 8.5, 12, colour=MUTED),
        "title": style("title", 22 * boost, 26 * boost, bold=True, align=TA_RIGHT),
        "number": style("number", 10.5, 14, colour=MUTED, align=TA_RIGHT),
        "label": style("label", 8 * boost, 11 * boost, bold=True, colour=accent),
        "party": style("party", 10 * boost, 14 * boost, bold=True),
        "body": style("body", 9, 13.5),
        "meta_label": style("meta_label", 8.5, 13, colour=MUTED),
        "meta_value": style("meta_value", 8.5, 13),
        "meta_value_right": style("meta_value_right", 8.5, 13, align=TA_RIGHT),
        "head": {a: style(f"head-{a}", 8 * boost, 11, bold=True, colour=SOFT_INK, align=a) for a in aligns},
        "cell": {a: style(f"cell-{a}", 9, 12.5, align=a) for a in aligns},
        "total_label": style("total_label", 9, 13, colour=SOFT_INK),
        "total_value": style("total_value", 9, 13, align=TA_RIGHT),
        "strong_label": style("strong_label", 11 * boost, 15 * boost, bold=True),
        "strong_value": style("strong_value", 11 * boost, 15 * boost, bold=True, align=TA_RIGHT),
        "notes": style("notes", 9, 13.5, colour=SOFT_INK, spaceAfter=5),
        "footer": style("footer", 8, 11, colour=MUTED),
    }


# --- sections ---------------------------------------------------------------------------------------------------------

def _header(doc, business, theme, title, number, st, width):
    """Logo and business details on the left; title, number and the meta fields on the right; then the accent rule."""
    left_w, right_w = _split(width)
    left = []
    logo = _logo(theme.get("logo_path"))
    if logo:
        left += [logo, Spacer(1, 4 * mm)]
    left.append(_p(business.get("name"), st["business"]))
    left += [_p(line, st["small"]) for line in _business_lines(business)]

    right = [_p(title, st["title"])]
    if number:
        right += [Spacer(1, 1 * mm), _p(number, st["number"])]
    fields = _pairs(doc.get("fields"))
    if fields:
        right += [Spacer(1, 6 * mm), _pairs_table(fields, st, right_w, st["meta_value_right"])]

    return [_band(left, right, [left_w, right_w]), Spacer(1, 5 * mm),
            HRFlowable(width="100%", thickness=0.9, color=st["accent"], spaceBefore=0, spaceAfter=0),
            Spacer(1, 6 * mm)]


def _business_lines(business):
    lines = []
    legal_name = _text(business.get("legal_name"))
    if legal_name and legal_name != _text(business.get("name")):
        lines.append(legal_name)
    if _text(business.get("abn")):
        lines.append(_abn(business["abn"]))
    lines += [line for line in _text(business.get("address")).split("\n") if line.strip()]
    lines += [_text(business.get(key)) for key in ("email", "phone", "website") if _text(business.get(key))]
    return lines


def _abn(value):
    text = re.sub(r"^ABN[:\s]*", "", _text(value), flags=re.IGNORECASE)
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11:
        text = f"{digits[:2]} {digits[2:5]} {digits[5:8]} {digits[8:]}"
    return "ABN " + text


def _parties(doc, st, width):
    """Who the document is for on the left, extra details on the right."""
    left_w, right_w = _split(width)
    to = doc.get("to") or {}
    lines = [_text(line) for line in to.get("lines") or [] if _text(line)]
    left = []
    if lines:
        if _text(to.get("label")):
            left += [_p(to["label"], st["label"]), Spacer(1, 1 * mm)]
        left += [_p(lines[0], st["party"])] + [_p(line, st["body"]) for line in lines[1:]]
    details = _pairs(doc.get("details"))
    right = [_pairs_table(details, st, right_w, st["meta_value"])] if details else []
    if not left and not right:
        return []
    return [_band(left, right, [left_w, right_w]), Spacer(1, 7 * mm)]


def _lines(doc, st, width):
    """The line-item table; its header row repeats on every page it spans."""
    spec = doc.get("lines") or {}
    rows = [list(row) for row in spec.get("rows") or [] if isinstance(row, (list, tuple))]
    if not rows:
        return []
    columns = [c if isinstance(c, dict) else {} for c in spec.get("columns") or []]
    count = max([len(columns)] + [len(row) for row in rows])
    columns += [{}] * (count - len(columns))
    weights = [_weight(c.get("width")) for c in columns]
    widths = [width * w / sum(weights) for w in weights]
    aligns = [{"right": TA_RIGHT, "center": TA_CENTER, "centre": TA_CENTER}.get(_text(c.get("align")).lower(), TA_LEFT)
              for c in columns]

    has_header = any(_text(c.get("label")) for c in columns)
    data = [[_p(c.get("label"), st["head"][a]) for c, a in zip(columns, aligns)]] if has_header else []
    data += [[_cell(value, st["cell"][a]) for value, a in zip(row + [""] * (count - len(row)), aligns)] for row in rows]

    first_body = 1 if has_header else 0
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (0, -1), 0), ("RIGHTPADDING", (-1, 0), (-1, -1), 0),  # flush with the page margins
        ("TOPPADDING", (0, first_body), (-1, -1), 6), ("BOTTOMPADDING", (0, first_body), (-1, -1), 6),
        ("LINEBELOW", (0, first_body), (-1, -1), 0.5, HAIRLINE),
    ]
    if has_header:
        commands += [("TOPPADDING", (0, 0), (-1, 0), 0), ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                     ("LINEBELOW", (0, 0), (-1, 0), 0.9, INK)]
    table = Table(data, colWidths=widths, repeatRows=first_body, splitInRow=1, hAlign="LEFT")
    table.setStyle(TableStyle(commands))
    return [table]


def _cell(value, style):
    """A table cell; lines after the first are set smaller and muted, like a description's detail."""
    text = _text(value)
    first, _, rest = text.partition("\n")
    markup = escape(first)
    if rest:
        markup += f'<br/><font size="8" color="{MUTED_HEX}">{_markup(rest)}</font>'
    return _p(text, style, markup)


def _closing(doc, business, st, width, frame_height):
    """Payment details on the left and totals on the right, side by side under the lines."""
    totals_w = width * 0.46
    totals = _totals(doc, st, totals_w)
    box = _payment_box(business, st, min(width - totals_w - 12 * mm, 90 * mm))
    if not totals and not box:
        return []
    band = _band([box] if box else [], [totals] if totals else [], [width - totals_w, totals_w])
    if band.wrap(width, frame_height)[1] > frame_height / 2:  # unusually long bank details: let them run on instead
        box = _payment_box(business, st, min(width, 90 * mm), splittable=True)
        return [Spacer(1, 6 * mm)] + ([KeepTogether([totals])] if totals else []) + [Spacer(1, 8 * mm), box]
    return [Spacer(1, 6 * mm), band]


def _totals(doc, st, width):
    """Subtotal, tax and total; values sit flush right so they line up with the last column of the lines."""
    rows = [t for t in doc.get("totals") or []
            if isinstance(t, dict) and (_text(t.get("label")) or _text(t.get("value")))]
    if not rows:
        return None
    value_w = min(36 * mm, width / 2)
    data, commands = [], [
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for i, total in enumerate(rows):
        strong = bool(total.get("strong"))
        data.append([_p(total.get("label"), st["strong_label" if strong else "total_label"]),
                     _p(total.get("value"), st["strong_value" if strong else "total_value"])])
        if strong:
            commands += [("LINEABOVE", (0, i), (-1, i), 0.9, INK), ("TOPPADDING", (0, i), (-1, i), 8)]
    table = Table(data, colWidths=[width - value_w, value_w], hAlign="RIGHT")
    table.setStyle(TableStyle(commands))
    return table


def _payment_box(business, st, width, splittable=False):
    """The bank details in a tinted box with an accent edge."""
    bank = _text(business.get("bank"))
    if not bank:
        return None
    edge = 2
    content = [_p("Payment details", st["label"]), Spacer(1, 2 * mm), _p(bank, st["body"])]
    box = Table([["", content]], colWidths=[edge, width - edge], hAlign="LEFT", splitInRow=int(splittable))
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), st["accent"]), ("BACKGROUND", (1, 0), (1, -1), st["tint"]),
        ("LEFTPADDING", (0, 0), (0, -1), 0), ("RIGHTPADDING", (0, 0), (0, -1), 0),
        ("LEFTPADDING", (1, 0), (1, -1), 12), ("RIGHTPADDING", (1, 0), (1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
    ]))
    return box


def _sections(doc, st, width):
    """More tables after the lines, each under its own heading (a report's breakdowns, a statement's open invoices):
    doc["sections"] = [{"heading", "columns": [...], "rows": [[...]], "note"}]."""
    out = []
    for section in doc.get("sections") or []:
        table = _lines({"lines": section}, st, width)
        if not table and not _text(section.get("note")):
            continue
        out += [Spacer(1, 7 * mm), _p(section.get("heading"), st["label"]), Spacer(1, 2.5 * mm)]
        out += table or [_p(section.get("note"), st["body"])]
        if table and _text(section.get("note")):
            out += [Spacer(1, 1.5 * mm), _p(section.get("note"), st["small"])]
    return out


def _notes(doc, st):
    text = _text(doc.get("notes"))
    if not text:
        return []
    paragraphs = [_p(chunk, st["notes"]) for chunk in re.split(r"\n\s*\n", text) if chunk.strip()]
    return [Spacer(1, 8 * mm), KeepTogether([_p("Notes", st["label"]), Spacer(1, 2 * mm), paragraphs[0]]),
            *paragraphs[1:]]


# --- building blocks --------------------------------------------------------------------------------------------------

def _split(width):
    return width * 0.56, width * 0.44


def _band(left, right, widths):
    """Two top-aligned columns with no padding of their own."""
    table = Table([[left or "", right or ""]], colWidths=widths, hAlign="LEFT", splitInRow=1)
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return table


def _pairs(items):
    pairs = []
    for item in items or []:
        if isinstance(item, dict) and (_text(item.get("label")) or _text(item.get("value"))):
            pairs.append((_text(item.get("label")), _text(item.get("value"))))
    return pairs


def _pairs_table(pairs, st, width, value_style):
    """Label/value rows; the label column is as wide as its longest label."""
    label_style = st["meta_label"]
    longest = max(pdfmetrics.stringWidth(label, label_style.fontName, label_style.fontSize) for label, _ in pairs)
    label_w = min(longest + 6 * mm, width * 0.5)
    table = Table([[_p(label, label_style), _p(value, value_style)] for label, value in pairs],
                  colWidths=[label_w, width - label_w], hAlign="LEFT")
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
    return table


def _weight(value):
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return 1.0
    return weight if weight > 0 else 1.0


def _logo(path):
    """The logo scaled to fit LOGO_BOX, or None when there is no readable image at `path`."""
    if not path:
        return None
    try:
        reader = ImageReader(str(path))
        pixel_w, pixel_h = reader.getSize()
        reader.getRGBData()  # decode now so a corrupt file is skipped rather than failing the build
    except Exception:
        return None
    if pixel_w <= 0 or pixel_h <= 0:
        return None
    scale = min(LOGO_BOX[0] / pixel_w, LOGO_BOX[1] / pixel_h)
    return Image(str(path), width=pixel_w * scale, height=pixel_h * scale, hAlign="LEFT")


# --- page furniture ---------------------------------------------------------------------------------------------------

FOOTER_RULE_Y = BOTTOM_MARGIN - 9 * mm
FOOTER_BASELINE_Y = FOOTER_RULE_Y - 5 * mm


def _draw_footer(c, page_w, footer, st):
    c.saveState()
    c.setStrokeColor(HAIRLINE)
    c.setLineWidth(0.5)
    c.line(SIDE_MARGIN, FOOTER_RULE_Y, page_w - SIDE_MARGIN, FOOTER_RULE_Y)
    if _text(footer):
        para = _p(footer, st["footer"])
        _, height = para.wrapOn(c, page_w - 2 * SIDE_MARGIN - 35 * mm, FOOTER_RULE_Y)
        # the first line shares the page number's baseline; any further lines run below it
        para.drawOn(c, SIDE_MARGIN, FOOTER_BASELINE_Y + st["footer"].fontSize - height)
    c.restoreState()


def _draw_running_header(c, page_w, page_h, left, right, st):
    """Who and what, at the top of every page after the first."""
    baseline = page_h - TOP_MARGIN + 8 * mm
    c.saveState()
    c.setFont(st["footer"].fontName, 8)
    c.setFillColor(MUTED)
    c.drawString(SIDE_MARGIN, baseline, left)
    c.drawRightString(page_w - SIDE_MARGIN, baseline, right)
    c.setStrokeColor(HAIRLINE)
    c.setLineWidth(0.5)
    c.line(SIDE_MARGIN, baseline - 3 * mm, page_w - SIDE_MARGIN, baseline - 3 * mm)
    c.restoreState()


def _draw_page_number(c, page_w, page, total, st):
    c.saveState()
    c.setFont(st["footer"].fontName, 8)
    c.setFillColor(MUTED)
    c.drawRightString(page_w - SIDE_MARGIN, FOOTER_BASELINE_Y, f"Page {page} of {total}")
    c.restoreState()


def _numbered_canvas(draw_page_number):
    """A canvas class that holds pages back until the end, so each can be stamped "Page n of total"."""
    class NumberedCanvas(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._held_pages = []

        def showPage(self):
            self._held_pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._held_pages)
            for state in self._held_pages:
                self.__dict__.update(state)
                draw_page_number(self, self._pageNumber, total)
                super().showPage()
            super().save()

    return NumberedCanvas
