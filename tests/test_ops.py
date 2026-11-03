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


def test_labels_and_documents_go_straight_to_the_printer_chosen(app, client, owner, monkeypatch):
    from app import printing
    user, ws = owner
    _built(ws, STOCK + [{"op": "add_document", "table": "work_orders", "document": {"label": "Job card", "title": "Job card", "fields": ["title"]}}])
    e = Engine(ws)
    flour = e.create("products", {"name": "Flour 25kg", "sku": "FL25-0001", "boxes": 2})
    job = e.create("work_orders", {"title": "Oak table"})
    sent = []
    monkeypatch.setattr(printing, "mac_printers", lambda: (["Brother_QL_820NWB", "Office_Laser"], "Office_Laser"))
    monkeypatch.setattr(printing, "_send_mac", lambda name, pdf, copies=1, size=None: sent.append(("mac", name, pdf[:4], size)))
    monkeypatch.setattr(printing, "_send_zpl", lambda address, data: sent.append(("zpl", address, data)))
    login(client)
    base, settings = f"/w/{ws.id}/app", f"/w/{ws.id}/settings/printers"
    assert "Brother QL 820NWB" in client.get(settings).get_data(as_text=True)
    record = client.get(f"{base}/t/products/{flour.id}").get_data(as_text=True)
    assert "label/print" not in record  # no printer chosen: labels open as PDFs to print by hand
    client.post(settings, data={"labels": "mac:Brother_QL_820NWB", "size": "62x29", "documents": "mac:Office_Laser"})
    client.post(f"{base}/t/products/{flour.id}/label/print", data={"doc": "shelf_label"})
    assert sent[-1][:3] == ("mac", "Brother_QL_820NWB", b"%PDF") and sent[-1][3] == "62x29"  # the stock loaded wins
    client.post(f"{base}/t/work_orders/{job.id}/document/job_card/print")
    assert sent[-1][:3] == ("mac", "Office_Laser", b"%PDF")
    client.post(settings, data={"labels": "zpl", "zpl_address": "192.168.1.50", "dpi": "203", "size": "50x25"})
    client.post(f"{base}/t/products/bulk", data={"ids": [flour.id], "action": "print_labels"})
    kind, address, zpl = sent[-1]
    assert (kind, address) == ("zpl", "192.168.1.50") and zpl.startswith("^XA") and "^BCN" in zpl and "^FDFL25-0001^FS" in zpl
    assert "Sent 1 label to the label printer at 192.168.1.50" in client.get(f"{base}/t/products").get_data(as_text=True)
    assert "as its IP address" in client.post(settings, data={"labels": "zpl", "zpl_address": "not an address!"}, follow_redirects=True).get_data(as_text=True)


def test_zpl_keeps_its_own_marks_out_of_the_words():
    from app import printing
    out = printing.zpl([{"title": "Bolt ^XZ ~JA", "lines": ["Bin A"], "code": "https://x/1", "kind": "qr"}], "62x29")
    assert out.count("^XA") == 1 and out.count("^XZ") == 1 and "^BQN" in out and "Bolt  XZ -JA" in out


def test_an_inbox_lists_them_beside_the_open_one(app, client, owner):
    user, ws = owner
    _built(ws, STOCK + [{"op": "add_view", "table": "work_orders", "view": {"key": "queue", "label": "To work through", "type": "queue"}}])
    e = Engine(ws)
    first = e.create("work_orders", {"title": "Oak table"})
    second = e.create("work_orders", {"title": "Pine chair"})
    assert schema.table(ws.blueprint, "work_orders")["views"][-1]["type"] == "inbox"
    login(client)
    page = client.get(f"/w/{ws.id}/app/t/work_orders?view=queue").get_data(as_text=True)
    assert "data-inbox-item" in page and "Pine chair" in page and "Oak table" in page
    assert f'<iframe src="/w/{ws.id}/app/t/work_orders/' in page  # the first one open beside the list
    opened = client.get(f"/w/{ws.id}/app/t/work_orders?view=queue&open={second.id}").get_data(as_text=True)
    assert f'src="/w/{ws.id}/app/t/work_orders/{second.id}"' in opened


def test_a_team_sees_its_own_menu_and_can_see_everything(app, client, owner):
    from .test_wave2 import _person
    user, ws = owner
    _built(ws, STOCK + [
        {"op": "set_team", "team": {"key": "warehouse", "name": "Warehouse"}},
        {"op": "add_view", "table": "products", "view": {"key": "low", "label": "Running low", "type": "list",
                                                         "filters": [{"field": "name", "op": "contains", "value": "flour"}]}},
        {"op": "set_space", "space": {"label": "Warehouse", "for": ["warehouse"], "groups": [
            {"label": "Stock", "items": [{"table": "products", "view": "low"}]}],
            "links": [{"label": "Carrier bookings", "url": "https://carrier.example.com"}, {"label": "Bad", "url": "javascript:alert(1)"}]}}])
    space = schema.spaces(ws.blueprint)[0]
    assert space["links"] == [{"label": "Carrier bookings", "url": "https://carrier.example.com"}]  # only web addresses
    _person(ws, "wes@example.com", "Wes", "editor", teams=["warehouse"])
    e = Engine(ws)
    for name in ("Flour 25kg", "Flour 10kg", "Sugar"):
        e.create("products", {"name": name})
    client.post("/login", data={"email": "wes@example.com", "password": "password1234"})
    base = f"/w/{ws.id}/app"
    menu = client.get(f"{base}/").get_data(as_text=True)
    assert "Running low" in menu and "Carrier bookings" in menu and "Work orders" not in menu and "Everything else" in menu
    assert '<span class="count" title="2 in it now">2</span>' in menu  # how many are in it now, like the To do count
    client.post(f"{base}/menu", data={"show": "all"})
    menu = client.get(f"{base}/").get_data(as_text=True)
    assert "Work orders" in menu and "Just my team" in menu
    client.post(f"{base}/menu", data={"show": "mine"})
    assert "Work orders" not in client.get(f"{base}/").get_data(as_text=True)
    client.post("/logout")
    login(client)  # the owner sees the whole menu
    assert "Work orders" in client.get(f"{base}/").get_data(as_text=True)
    wrong = schema.check({**ws.blueprint, "spaces": [schema.normalize_space({"label": "X", "for": ["nobody"], "groups": [{"items": ["nope"]}]})]})
    assert any("isn't a team or a role" in p for p in wrong) and any("“nope”, which isn't a table" in p for p in wrong)


def test_a_link_made_from_a_records_details_and_a_median(app, owner):
    user, ws = owner
    _built(ws, [
        {"op": "create_table", "table": "shipments", "label": "Shipments", "fields": [
            {"key": "ref", "type": "text", "required": True}, {"key": "tracking", "type": "text"},
            {"key": "days", "type": "number"},
            {"key": "track", "label": "Track it", "type": "url", "template": "https://track.example.com/?n={{record.tracking}}"}]},
        {"op": "create_table", "table": "carriers", "label": "Carriers", "fields": [
            {"key": "name", "type": "text", "required": True},
            {"key": "typical", "type": "rollup", "table": "shipments", "via": "carrier", "aggregate": "typical", "field": "days"}]},
        {"op": "add_field", "table": "shipments", "field": {"key": "carrier", "type": "link", "table": "carriers"}}])
    e = Engine(ws)
    fast = e.create("carriers", {"name": "FastFreight"})
    a = e.create("shipments", {"ref": "S1", "tracking": "AB 12/3", "carrier": fast.id, "days": 2})
    b = e.create("shipments", {"ref": "S2", "carrier": fast.id, "days": 3})
    e.create("shipments", {"ref": "S3", "carrier": fast.id, "days": 30})  # one very late
    assert e.value(a, "track") == "https://track.example.com/?n=AB%2012%2F3"  # made safe for an address
    assert e.value(b, "track") is None  # no tracking number yet: no link
    assert e.value(e.get(fast.id), "typical") == 3  # the middle one, not dragged up by the late one
    problems = schema.check({**ws.blueprint, "tables": ws.blueprint["tables"] + [{"key": "x", "label": "X", "singular": "X", "fields": [
        {"key": "go", "label": "Go", "type": "url", "template": "javascript:{{record.nope}}"}]}]})
    assert any("must start with https://" in p for p in problems) and any("nope" in p for p in problems)
