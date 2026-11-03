"""The business document PDF: content lands in the file, long tables paginate cleanly, odd input can't break it."""
import io

from PIL import Image
from pypdf import PdfReader

from app.documents import pdf as pdf_module
from app.documents.pdf import render

COLUMNS = [{"label": "Description", "align": "left", "width": 4}, {"label": "Qty", "align": "right", "width": 1},
           {"label": "Unit price", "align": "right", "width": 1.5}, {"label": "Amount", "align": "right", "width": 1.5}]


def invoice(**overrides):
    doc = {
        "business": {"name": "Banksia Data Co.", "legal_name": "Banksia Data Pty Ltd", "abn": "12345678901",
                     "address": "Level 4, 140 St Georges Tce\nPerth WA 6000", "email": "accounts@banksia.example",
                     "bank": "BSB 086-006\nAccount 5521 4478"},
        "theme": {"accent": "#0f766e"},
        "title": "Tax invoice",
        "number": "INV-0001",
        "fields": [{"label": "Issue date", "value": "1 Nov 2026"}, {"label": "Due date", "value": "15 Nov 2026"}],
        "to": {"label": "Bill to", "lines": ["Acme Pty Ltd", "12 Hay St", "Perth WA 6000"]},
        "details": [{"label": "Project", "value": "Pit survey"}],
        "lines": {"columns": COLUMNS,
                  "rows": [["Analysis work\nsecond line of description", "10", "$150.00", "$1,500.00"]]},
        "totals": [{"label": "Subtotal", "value": "$1,500.00"}, {"label": "GST", "value": "$150.00"},
                   {"label": "Total", "value": "$1,650.00", "strong": True}],
        "notes": "Payment within 14 days.\n\nThanks for choosing us.",
        "footer": "Thank you for your business.",
    }
    doc.update(overrides)
    return doc


def pages(data):
    return [page.extract_text() for page in PdfReader(io.BytesIO(data)).pages]


def test_invoice_content_and_metadata():
    data = render(invoice())
    assert data.startswith(b"%PDF")
    text = "\n".join(pages(data))
    for expected in ("Tax invoice", "INV-0001", "Banksia Data Co.", "ABN 12 345 678 901", "Analysis work",
                     "second line of description", "Acme Pty Ltd", "Pit survey", "$1,500.00", "$150.00", "$1,650.00",
                     "Payment details", "BSB 086-006", "Thank you for your business.", "Page 1 of 1"):
        assert expected in text, expected
    metadata = PdfReader(io.BytesIO(data)).metadata
    assert metadata.title == "Tax invoice INV-0001"
    assert metadata.author == "Banksia Data Co."


def test_long_line_tables_repeat_their_header_on_every_page():
    rows = [[f"Survey item {i}", "1", "$10.00", "$10.00"] for i in range(120)]
    text = pages(render(invoice(lines={"columns": COLUMNS, "rows": rows})))
    assert len(text) >= 3
    for number, page in enumerate(text, start=1):
        assert "Description" in page
        assert f"Page {number} of {len(text)}" in page
    assert "Survey item 119" in text[-1]


def test_only_the_required_keys():
    text = "\n".join(pages(render({"business": {"name": "Solo Trader"}, "title": "Quote"})))
    assert "Solo Trader" in text and "Quote" in text and "Page 1 of 1" in text


def test_markup_in_user_text_is_printed_literally():
    tricky = "<b>&</b> <script>"
    doc = invoice(lines={"columns": COLUMNS, "rows": [[tricky, "1", "$1.00", "$1.00"]]}, notes=f"See {tricky} &amp;")
    doc["business"]["name"] = "Smith & <Sons>"
    text = "\n".join(pages(render(doc)))
    assert tricky in text
    assert "See <b>&</b> <script> &amp;" in text
    assert "Smith & <Sons>" in text


def test_missing_or_unreadable_logo_is_skipped(tmp_path):
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not really a png")
    for path in ("/no/such/logo.png", str(broken)):
        data = render(invoice(theme={"accent": "not-a-colour", "logo_path": path}))
        assert "Banksia Data Co." in pages(data)[0]
        assert not PdfReader(io.BytesIO(data)).pages[0].images


def test_logo_is_drawn(tmp_path):
    logo = tmp_path / "logo.png"
    Image.new("RGB", (400, 120), (91, 91, 214)).save(logo)
    data = render(invoice(theme={"logo_path": str(logo)}))
    assert len(PdfReader(io.BytesIO(data)).pages[0].images) == 1


def test_letter_size_and_unicode_text():
    doc = invoice(page_size="Letter", to={"label": "Bill to", "lines": ["株式会社 Ōtaki Café", "Zürich"]})
    data = render(doc)
    page = PdfReader(io.BytesIO(data)).pages[0]
    assert round(float(page.mediabox.width)) == 612
    if pdf_module._fonts().regular != "Helvetica":  # a Unicode TrueType font is installed
        assert "株式会社 Ōtaki Café" in page.extract_text()
