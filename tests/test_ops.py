"""Operations on the floor: labels to stick on things and scanning them back, the way a warehouse, a workshop or a
dispatch bench works. Every piece is the platform's, for any app."""
import pypdf
import io

from app.blueprint import schema
from app.documents import labels
from app.engine.records import Engine

from .conftest import login
from .test_wave2 import _built

STOCK = [
    {"op": "create_table", "table": "products", "label": "Products", "fields": [
        {"key": "name", "type": "text", "required": True}, {"key": "sku", "label": "SKU", "type": "barcode"},
        {"key": "bin", "type": "text"}, {"key": "cost", "type": "money", "access": "admin"}, {"key": "boxes", "type": "number"}],
     "documents": [{"label": "Shelf label", "format": "label", "size": "4x6", "fields": ["bin", "cost"], "copies": "boxes"}]},
    {"op": "create_table", "table": "work_orders", "label": "Work orders", "singular": "Work order", "fields": [
        {"key": "number", "type": "autonumber", "prefix": "WO-"}, {"key": "title", "type": "text", "required": True}]},
]


def _pages(response):
    return pypdf.PdfReader(io.BytesIO(response.data)).pages


def test_a_label_design_is_understood_and_checked():
    doc = schema.normalize_document({"label": "Bin label", "kind": "sticker", "size": "A4 sheet", "barcode": "sku", "copies": "qty",
                                     "code": "QR code", "fields": ["bin"]})
    assert doc == {"key": "bin_label", "label": "Bin label", "format": "label", "size": "a4_21", "fields": ["bin"], "code_field": "sku",
                   "copies_field": "qty", "code": "qr"}
    assert [labels.size_of(x) for x in ("4x6", "Letter", "2x1 in", "62 x 29 mm", "")] == ["100x150", "letter_30", "50x25", "62x29", "62x29"]


def test_labels_print_from_a_record_and_a_list_and_a_scan_opens_the_record(app, client, owner):
    user, ws = owner
    _built(ws, STOCK)
    e = Engine(ws)
    flour = e.create("products", {"name": "Flour 25kg", "sku": "FL25-0001", "bin": "A-03", "cost": 21, "boxes": 3})
    sugar = e.create("products", {"name": "Sugar 10kg", "bin": "B-01"})
    job = e.create("work_orders", {"title": "Oak table"})
    login(client)
    base = f"/w/{ws.id}/app"

    plain = client.get(f"{base}/t/products/{flour.id}/label")  # every list has one, nothing designed
    assert plain.status_code == 200 and plain.mimetype == "application/pdf" and "inline" in plain.headers["Content-Disposition"]
    assert len(_pages(plain)) == 1 and "FL25-0001" in _pages(plain)[0].extract_text()  # its own barcode
    shelf = client.get(f"{base}/t/products/{flour.id}/label?doc=shelf_label")
    assert len(_pages(shelf)) == 3  # one per box
    assert round(float(_pages(shelf)[0].mediabox.width) / 72 * 25.4) == 100  # 4 × 6 in shipping size, as designed
    many = client.post(f"{base}/t/products/bulk", data={"ids": [flour.id, sugar.id], "action": "labels", "size": "a4_21"})
    assert many.mimetype == "application/pdf" and len(_pages(many)) == 1  # both on one sheet
    assert client.get(f"{base}/t/products/{flour.id}/label?doc=nope").status_code == 404

    assert client.get(f"{base}/t/products/{flour.id}").status_code == 200  # the record's own screen offers it
    page = client.get(f"{base}/t/products/{flour.id}").get_data(as_text=True)
    assert "Print shelf label" in page and "Print label" in page

    for code, where in ((f"http://localhost:5050{base}/t/work_orders/{job.id}", f"/t/work_orders/{job.id}"),  # its QR code
                        ("fl25-0001", f"/t/products/{flour.id}"),                                              # a barcode
                        ("WO-0001", f"/t/work_orders/{job.id}")):                                             # its number
        assert client.get(f"{base}/scan?code={code}").headers["Location"].endswith(where), code
    assert "/search" in client.get(f"{base}/scan?code=nothing-like-it").headers["Location"]
    assert client.get(f"{base}/search?q=FL25-0001").headers["Location"].endswith(f"/t/products/{flour.id}")  # a USB scanner types it
    assert client.get(f"{base}/search?q=Flour").status_code == 200  # a name is still a search


def test_a_label_keeps_to_what_its_reader_may_see(app, owner):
    _, ws = owner
    _built(ws, STOCK)
    e = Engine(ws)
    flour = e.create("products", {"name": "Flour 25kg", "sku": "FL25-0001", "bin": "A-03", "cost": 21})
    from .test_wave2 import _as, _person
    sam = _person(ws, "sam@example.com", "Sam", "editor")
    doc = next(d for d in e.tables["products"]["documents"] if d["key"] == "shelf_label")
    assert labels.for_records(e, [flour], doc)[0]["lines"] == ["Bin: A-03", "Cost: $21.00"]
    staff = _as(ws, sam)
    assert labels.for_records(staff, [staff.get(flour.id)], doc)[0]["lines"] == ["Bin: A-03"]  # the cost is kept to admins
    assert labels.for_records(e, [flour])[0]["kind"] == "barcode"
