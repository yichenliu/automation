"""Reading files: every sample is built here at test time, so the tests need nothing but the installed libraries."""
import base64
import io
from datetime import date, datetime, timedelta
from email.message import EmailMessage

import pytest
from PIL import Image, ImageDraw

from app.files import readers
from app.files.readers import kind_of, read, sheet_rows

SALES_HEADERS = ["Date", "Customer", "Amount", "amount (2)", "Column 5", "Notes", "Created"]


def picture(b64):
    return Image.open(io.BytesIO(base64.b64decode(b64)))


@pytest.fixture
def sales_xlsx(tmp_path):
    """A title above the header, a fully empty column, a duplicate and a blank header, a gap, 120 data rows, and a
    hidden sheet that comes first in the workbook."""
    import openpyxl

    wb = openpyxl.Workbook()
    lookup = wb.active
    lookup.title = "Lookup"
    lookup.append(["Code", "Meaning"])
    lookup.append(["A", "Active"])
    lookup.sheet_state = "hidden"
    ws = wb.create_sheet("Sales")
    wb.active = 1
    ws["A1"] = "Sales report 2024"
    for col, label in enumerate(["Date", "Customer", None, "Amount", "amount", None, " Notes\n", "Created"], 1):
        ws.cell(3, col, label)
    for i in range(120):
        r = 4 + i + (i >= 60)  # leave row 64 empty
        values = [date(2024, 1, 1) + timedelta(days=i), f"Client {i}", None, 10.5 * i, i, "x" if i % 2 else None,
                  "$1,200.50", datetime(2024, 1, 1, 9, 30) + timedelta(days=i), "   "]
        for col, value in enumerate(values, 1):
            ws.cell(r, col, value)
    path = tmp_path / "sales.xlsx"
    wb.save(path)
    return path


def text_pdf(path, pages, **options):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    pdf = canvas.Canvas(str(path), pagesize=A4, **options)
    for n in range(1, pages + 1):
        for line in range(6):
            pdf.drawString(72, 780 - 20 * line, f"Invoice INV-{n:03d} line {line}: 3 x Widgets at $12.50 each, $37.50")
        pdf.showPage()
    pdf.save()
    return path


def scanned_pdf(tmp_path, pages):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    scan = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(scan)
    for k in range(12):
        draw.text((100, 100 + 40 * k), f"RECEIPT line {k}   Coffee x2   $9.00", fill="black")
    scan.save(tmp_path / "scan.png")
    path = tmp_path / "scan.pdf"
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for _ in range(pages):
        pdf.drawImage(str(tmp_path / "scan.png"), 0, 0, *A4)
        pdf.showPage()
    pdf.save()
    return path


# Spreadsheets -----------------------------------------------------------------------------------------------------

def test_xlsx_finds_the_header_below_a_title_and_cleans_rows(sales_xlsx):
    content = read(sales_xlsx, max_rows=10)
    assert content.kind == "spreadsheet" and content.error == ""
    sales, lookup = content.sheets
    assert sales.name == "Sales" and sales.headers == SALES_HEADERS
    assert sales.row_count == 120 and len(sales.rows) == 10
    assert sales.rows[0] == ["2024-01-01", "Client 0", 0, 0, None, "$1,200.50", "2024-01-01 09:30"]
    assert sales.rows[1] == ["2024-01-02", "Client 1", 10.5, 1, "x", "$1,200.50", "2024-01-02 09:30"]
    assert sales.rows[2][2] == 21 and isinstance(sales.rows[2][2], int)
    assert (lookup.name, lookup.headers, lookup.rows) == ("Lookup", ["Code", "Meaning"], [["A", "Active"]])
    assert content.meta == {"hidden_sheets": ["Lookup"]}
    assert content.text.startswith(
        "## Sheet: Sales (120 rows)\n| Date | Customer | Amount | amount (2) | Column 5 | Notes | Created |\n"
        "| 2024-01-01 | Client 0 | 0 | 0 |  | $1,200.50 | 2024-01-01 09:30 |\n")
    assert "… 110 more rows" in content.text and "Sales report 2024" not in content.text
    assert "## Sheet: Lookup (1 row)" in content.text


def test_rows_are_capped_at_max_rows_but_all_are_counted(sales_xlsx):
    sales = read(sales_xlsx).sheets[0]
    assert len(sales.rows) == 50 and sales.row_count == 120
    assert read(sales_xlsx, max_rows=0).sheets[0].rows == []


def test_sheet_rows_streams_every_row(sales_xlsx):
    headers, rows = sheet_rows(sales_xlsx)
    rows = list(rows)
    assert headers == SALES_HEADERS
    assert len(rows) == 120 and all(len(row) == len(headers) for row in rows)
    assert rows[0] == read(sales_xlsx).sheets[0].rows[0]
    assert rows[-1][:2] == [(date(2024, 1, 1) + timedelta(days=119)).isoformat(), "Client 119"]
    headers, rows = sheet_rows(sales_xlsx, sheet="lookup")
    assert (headers, list(rows)) == (["Code", "Meaning"], [["A", "Active"]])
    with pytest.raises(readers.Unreadable, match="no sheet called"):
        sheet_rows(sales_xlsx, sheet="Nope")


def test_upload_stored_without_an_extension(sales_xlsx, tmp_path):
    stored = tmp_path / "upload-7f3a"
    stored.write_bytes(sales_xlsx.read_bytes())
    content = read(stored, name="Sales.xlsx")
    assert content.name == "Sales.xlsx" and content.error == "" and content.sheets[0].row_count == 120
    headers, rows = sheet_rows(stored, name="Sales.xlsx")
    assert headers == SALES_HEADERS and sum(1 for _ in rows) == 120


def test_semicolon_csv_in_cp1252(tmp_path):
    lines = ["Sales export;;", "Name;Amount;Date"] + [f"Café {i};{i},50 €;0{i % 9 + 1}/02/2024" for i in range(12)]
    path = tmp_path / "export.csv"
    path.write_bytes("\r\n".join(lines).encode("cp1252"))
    content = read(path)
    sheet = content.sheets[0]
    assert content.error == "" and sheet.name == "export"
    assert sheet.headers == ["Name", "Amount", "Date"] and sheet.row_count == 12
    assert sheet.rows[0] == ["Café 0", "0,50 €", "01/02/2024"]  # numbers stay text; the importer reads them
    headers, rows = sheet_rows(path)
    assert headers == sheet.headers and len(list(rows)) == 12


def test_tab_separated_txt_reads_as_a_table(tmp_path):
    path = tmp_path / "clients.txt"  # Excel's "Unicode text" export: UTF-16 with tabs
    path.write_text("Client\tCity\tBalance\nAcme\tPerth\t1200\nBeta\tAlbany\t300\nGamma\tBroome\t50\n", "utf-16")
    content = read(path)
    assert content.kind == "spreadsheet"
    assert content.sheets[0].headers == ["Client", "City", "Balance"]
    assert content.sheets[0].rows[0] == ["Acme", "Perth", "1200"]
    note = tmp_path / "note.txt"
    note.write_text("Hello, this is a note.\nIt has commas, here and there, and sometimes more, too.\nThe end.\n")
    assert read(note).kind == "text"


def test_xls(tmp_path):
    xlwt = pytest.importorskip("xlwt")
    book = xlwt.Workbook()
    sheet = book.add_sheet("Stock")
    as_date = xlwt.easyxf(num_format_str="YYYY-MM-DD")
    for r, row in enumerate([["SKU", "Count", "Received"], ["W-1", 4, date(2024, 3, 1)], ["W-2", 7.5, None]]):
        for c, value in enumerate(row):
            if value is not None:
                sheet.write(r, c, value, as_date if isinstance(value, date) else xlwt.Style.default_style)
    path = tmp_path / "stock.xls"
    book.save(str(path))
    content = read(path)
    assert content.error == "" and content.sheets[0].headers == ["SKU", "Count", "Received"]
    assert content.sheets[0].rows == [["W-1", 4, "2024-03-01"], ["W-2", 7.5, None]]
    assert list(sheet_rows(path)[1]) == content.sheets[0].rows


@pytest.mark.parametrize("damage", ["not a zip", "truncated"])
def test_a_damaged_xlsx_is_an_error_not_an_exception(sales_xlsx, tmp_path, damage):
    path = tmp_path / "broken.xlsx"
    data = sales_xlsx.read_bytes()
    path.write_bytes(b"this is not a spreadsheet" if damage == "not a zip" else data[: len(data) // 2])
    content = read(path)
    assert content.kind == "spreadsheet" and content.sheets == []
    assert content.error == readers.FAILED["spreadsheet"]
    with pytest.raises(readers.Unreadable):
        sheet_rows(path)


# PDFs -------------------------------------------------------------------------------------------------------------

def test_text_pdf(tmp_path):
    content = read(text_pdf(tmp_path / "invoice.pdf", 2))
    assert content.kind == "pdf" and content.error == "" and content.pages == 2
    assert content.text.startswith("--- page 1 ---\nInvoice INV-001 line 0")
    assert "--- page 2 ---\nInvoice INV-002" in content.text
    assert content.images == []


def test_scanned_pdf_is_rendered_for_the_vision_model(tmp_path):
    content = read(scanned_pdf(tmp_path, pages=3), max_images=2)
    assert content.error == "" and content.pages == 3 and len(content.images) == 2
    page = picture(content.images[0])
    assert page.format == "PNG" and 1500 < max(page.size) <= 1600 and page.height > page.width


def test_password_protected_pdf(tmp_path):
    from reportlab.lib.pdfencrypt import StandardEncryption

    locked = text_pdf(tmp_path / "locked.pdf", 1, encrypt=StandardEncryption("secret", ownerPassword="owner"))
    assert read(locked).error == "This PDF is password protected."
    # No password needed to open, only to edit: reads normally.
    restricted = text_pdf(tmp_path / "restricted.pdf", 1, encrypt=StandardEncryption("", ownerPassword="owner"))
    assert "INV-001" in read(restricted).text


def test_damaged_pdf(tmp_path):
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"%PDF-1.4\nthis is not really a pdf")
    content = read(path)
    assert content.kind == "pdf" and content.error == readers.FAILED["pdf"]


# Images -----------------------------------------------------------------------------------------------------------

def test_jpeg_is_turned_upright_from_its_exif_orientation(tmp_path):
    photo = Image.new("RGB", (200, 100), (0, 0, 255))
    photo.paste((255, 0, 0), (0, 0, 100, 100))  # left half red, as stored
    exif = Image.Exif()
    exif[0x0112] = 6  # "rotate 90° clockwise to display"
    path = tmp_path / "receipt.jpg"
    photo.save(path, "JPEG", exif=exif.tobytes())
    content = read(path)
    assert content.kind == "image" and content.error == "" and len(content.images) == 1
    upright = picture(content.images[0]).convert("RGB")
    assert upright.size == (100, 200)
    top, bottom = upright.getpixel((50, 40)), upright.getpixel((50, 160))
    assert top[0] > 200 and top[2] < 60 and bottom[2] > 200 and bottom[0] < 60


def test_big_transparent_image_is_shrunk_onto_white(tmp_path):
    image = Image.new("RGBA", (3200, 800), (0, 0, 0, 0))
    image.paste((0, 0, 0, 255), (0, 0, 400, 800))
    path = tmp_path / "logo.png"
    image.save(path)
    out = picture(read(path).images[0])
    assert out.size == (1600, 400) and out.mode == "RGB"
    assert out.getpixel((1000, 200)) == (255, 255, 255) and out.getpixel((50, 200)) == (0, 0, 0)


def test_big_photo_is_shrunk(tmp_path):
    Image.new("RGB", (4000, 3000), (200, 30, 30)).save(tmp_path / "big.jpg", quality=80)
    assert picture(read(tmp_path / "big.jpg").images[0]).size == (1600, 1200)


def test_heic_is_refused_politely(tmp_path):
    heic = b"\x00\x00\x00\x18ftypheic" + bytes(64)
    for name in ("photo.heic", "photo.jpg"):  # also when it's been renamed
        (tmp_path / name).write_bytes(heic)
        assert read(tmp_path / name).error == "HEIC photos aren't supported yet; export as JPG."


# Documents, text and email ----------------------------------------------------------------------------------------

def test_docx_with_a_table(tmp_path):
    import docx

    document = docx.Document()
    document.add_heading("Quote Q-1042", 1)
    document.add_paragraph("Prepared for Acme Pty Ltd")
    table = document.add_table(rows=4, cols=3)
    for r, row in enumerate([["Item", "Qty", "Price"], ["Widget", "3", "$12.50"], ["Gadget", "1", "$99.00"]]):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    table.cell(3, 0).merge(table.cell(3, 1)).text = "Total"
    table.cell(3, 2).text = "$136.50"
    document.add_paragraph("Valid for 30 days")
    document.sections[0].footer.paragraphs[0].text = "ABN 12 345 678 901"
    path = tmp_path / "quote.docx"
    document.save(path)
    content = read(path)
    assert content.kind == "document" and content.error == ""
    assert "Item | Qty | Price\nWidget | 3 | $12.50\nGadget | 1 | $99.00\nTotal | $136.50" in content.text
    assert content.text.index("Prepared for") < content.text.index("Item | Qty") < content.text.index("Valid for")
    assert content.text.endswith("ABN 12 345 678 901")


def test_old_word_doc_is_refused(tmp_path):
    (tmp_path / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(100))
    assert read(tmp_path / "old.doc").error == "Old Word .doc files can't be read; save it as .docx."


def email_message():
    message = EmailMessage()
    message["From"] = "Supplier Co <accounts@supplier.example>"
    message["To"] = "ethan@example.com"
    message["Subject"] = "Invoice INV-77 for October"
    message["Date"] = "Fri, 31 Oct 2026 09:15:00 +0800"
    return message


def test_email_with_an_attachment(tmp_path):
    message = email_message()
    message.set_content("Hi Ethan,\n\nPlease find invoice INV-77 attached. Total $1,240.00 due 14 Nov.\n")
    message.add_alternative("<p>Hi Ethan,</p><p>Please find invoice <b>INV-77</b> attached.</p>", subtype="html")
    message.add_attachment(b"%PDF-1.4 not opened", maintype="application", subtype="pdf", filename="INV-77.pdf")
    path = tmp_path / "invoice.eml"
    path.write_bytes(bytes(message))
    content = read(path)
    assert content.kind == "email" and content.error == ""
    assert content.meta == {"from": "Supplier Co <accounts@supplier.example>", "to": "ethan@example.com",
                            "subject": "Invoice INV-77 for October", "date": "2026-10-31T09:15+08:00",
                            "attachments": ["INV-77.pdf"]}
    assert content.text == "Hi Ethan,\n\nPlease find invoice INV-77 attached. Total $1,240.00 due 14 Nov."


def test_html_only_email_and_web_page_are_stripped_to_text(tmp_path):
    message = email_message()
    message.set_content("<html><head><style>p {color: red}</style></head><body><p>Please pay <b>INV-77</b>.</p>"
                        "<table><tr><td>Qty</td><td>3</td></tr><tr><td>Total</td><td>$37.50</td></tr></table>"
                        "<script>alert('x')</script></body></html>", subtype="html")
    (tmp_path / "html.eml").write_bytes(bytes(message))
    (tmp_path / "page.html").write_text(message.get_content())
    for name in ("html.eml", "page.html"):
        assert read(tmp_path / name).text == "Please pay INV-77.\n\nQty | 3\nTotal | $37.50"


def test_text_is_truncated_at_max_chars(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("word " * 1000)
    text = read(path, max_chars=100).text
    assert text.endswith("\n[…truncated]") and len(text) <= 100 + len("\n[…truncated]")
    assert read(path).text == ("word " * 1000).strip()


# Anything else ----------------------------------------------------------------------------------------------------

def test_unknown_missing_and_empty_files(tmp_path):
    (tmp_path / "thing.xyz").write_bytes(b"\x01\x02\x03")
    unknown = read(tmp_path / "thing.xyz")
    assert (unknown.kind, unknown.error) == ("unknown", "This kind of file can't be read.")
    assert read(tmp_path / "missing.pdf").error == "This file couldn't be found."
    (tmp_path / "empty.csv").write_bytes(b"")
    assert read(tmp_path / "empty.csv").error == "This file is empty."
    with pytest.raises(readers.Unreadable):
        sheet_rows(tmp_path / "thing.xyz")


def test_kind_of():
    assert [kind_of(n) for n in ("A.XLSX", "b.csv", "c.pdf", "d.jpeg", "e.docx", "f.md", "g.eml", "h", "i.zip")] == [
        "spreadsheet", "spreadsheet", "pdf", "image", "document", "text", "email", "unknown", "unknown"]


def test_a_spreadsheets_formulas_are_read_in_its_own_headings(tmp_path):
    """The sheet's logic comes across, not just its numbers: a line's total, a price looked up on another sheet, a fixed
    rate cell, a sum of another sheet's amounts."""
    import openpyxl

    wb = openpyxl.Workbook()
    orders = wb.active
    orders.title = "Orders"
    orders.append(["GST rate", 0.1])
    orders.append([])
    orders.append(["Customer", "Product", "Qty", "Unit price", "Line total", "GST"])
    orders.append(["Cafe Uno", "Flour", 2, "=VLOOKUP(B4,Products!A:C,3,FALSE)", "=C4*D4", "=E4*$B$1"])
    products = wb.create_sheet("Products")
    products.append(["Code", "Name", "Price"])
    products.append(["Flour", "Flour 25kg", 30])
    customers = wb.create_sheet("Customer totals")
    customers.append(["Name", "Total ordered", "Note"])
    customers.append(["Cafe Uno", "=SUMIF(Orders!A:A,A2,Orders!E:E)", '="Total for "&A2'])
    path = tmp_path / "book.xlsx"
    wb.save(path)
    content = readers.read(path)
    sheets = {s.name: s for s in content.sheets}
    assert sheets["Orders"].formulas == {"Unit price": "VLOOKUP([Product], “Products” [Code]…[Price], 3, FALSE)",
                                         "Line total": "[Qty] * [Unit price]", "GST": "[Line total] * GST rate (0.1, cell B1)"}
    assert sheets["Customer totals"].formulas["Total ordered"] == "SUMIF(“Orders” [Customer], [Name], “Orders” [Line total])"
    assert sheets["Customer totals"].formulas["Note"] == '"Total for "&[Name]'  # text in quotes is left as it is
    assert sheets["Products"].formulas == {}
    assert "Worked out by formulas: Unit price = VLOOKUP" in content.text
