"""Ready-made parts to start an app from: Inventory & warehouse, Orders & fulfilment, Procurement, Production. Each is the
proven way of doing it (stock as movements, a delivery received by counting it in, an order picked by scanning), written
in the Keel language like anything the builder makes, so the builder carries on from it like from anything it made.

They fit together: a list two parts both need (Products) is made once with the fields of both, and what joins two parts
(an order shipped takes its stock off; a purchase order received puts it on) comes only when both are chosen.
`ops_for(keys, bp)` is the Keel patches for the chosen parts, leaving out what the app has already; `unsaid_for(keys, bp)`
the design ops for the little the language can't say yet (who may not approve a purchase order, and what happens when
it's turned down).
"""
from . import schema

WORKED_OUT = ("total", "formula", "lookup")
NAMING = ("text", "auto_number", "email")  # the kinds of field that name a record, as the design picks one


def _list(key, label, singular, fields, icon=None, stages=None, inside=None, settings=None, documents=(), later=()):
    """A list a part makes, as the patches that make it: the list (its fields and stages), its icon, the field that names
    each record and the columns of its list of everything (a stock move is named by its product and shows it first, as the
    fields are given), the settings a field has beyond its kind (a quantity never below 0), its documents, and fields that
    read what the part adds to another list (a count line's expected stock is the product's on hand). Left out whole when
    the app has the list already."""
    patch = {"do": "add_list", "key": key, "label": label, "singular": singular, "fields": fields}
    if stages:
        patch["stages"] = stages
    if inside:
        patch["inside"] = inside
    title = next((f["key"] for f in fields if f["type"] in NAMING), fields[0]["key"])
    out = [patch, {"do": "change_list", "list": key, "title_field": title, **({"icon": icon} if icon else {})}]
    if not inside and any(f["type"] in ("link",) + WORKED_OUT for f in fields):
        out.append({"do": "design_op", "op": {"op": "update_view", "table": key, "view": {
            "key": "all", "label": f"All {schema.lc(label)}", "type": "list",
            "columns": [f["key"] for f in fields if f["type"] != "long_text"][:7]}}})
    out += [{"do": "set_field", "list": key, "field": field, "settings": said} for field, said in (settings or {}).items()]
    out += [{"do": "design_op", "op": {"op": "add_document", "table": key, "document": doc}} for doc in documents]
    out += [{"do": "add_field", "list": key, "field": f} for f in later]
    return out


def _rule(key, name, when, steps, only_if=None):
    rule = {"key": key, "name": name, "when": when, "steps": steps}
    if only_if:
        rule["only_if"] = only_if
    return {"do": "add_rule", "rule": rule}


def _changed(table, to):
    """When a record of the list has its status changed to this."""
    return {"on": "changed", "list": table, "field": "status", "to": to}


def _f(key, label, kind="text", **more):
    return {"type": kind, "key": key, "label": label, **more}


def _is(field, value, op="equals"):
    return {"field": field, "op": op, "value": value}


def _set(field, formula=None, to=None):
    return {"field": field, "formula": formula} if formula is not None else {"field": field, "to": to}


NAME = _f("name", "Name", required=True)
NOT_BELOW_0 = {"quantity": {"min": 0}}

PRODUCTS = {"key": "products", "label": "Products", "singular": "Product", "icon": "package", "fields": [
    NAME, _f("sku", "SKU", "barcode", unique=True), _f("price", "Price", "money"),
    _f("status", "Status", "choice", options=["Active", "Discontinued"], default="Active")]}

MODULES = {
    "inventory": {
        "label": "Inventory & warehouse", "icon": "boxes",
        "about": "Products and their stock in each place, every movement kept (received, sold, moved, counted, damaged), "
                 "batches and use-by dates, what's running low, stock counts on a phone, moves between places, and labels to scan.",
        "products": [_f("cost", "Cost", "money", visible_to="admin"), _f("reorder_point", "Reorder when down to", "number"),
                     _f("reorder_qty", "Reorder how many", "number"),
                     _f("on_hand", "On hand", "total", of="stock_movements", link="product", how="sum", field="change"),
                     _f("low", "Running low", "formula", formula="reorder_point > 0 and on_hand <= reorder_point", shows="yes_no")],
        "patches": [
            _list("locations", "Locations", "Location", icon="map-pin", fields=[
                NAME, _f("kind", "Kind", "choice", options=["Warehouse", "Shelf or bin", "Van", "Quarantine"], default="Warehouse"),
                _f("code", "Code", "barcode", unique=True)],
                documents=[{"label": "Location label", "format": "label", "fields": ["kind"], "code_field": "code"}]),
            _list("stock_movements", "Stock movements", "Stock movement", icon="arrow-up-down", settings=NOT_BELOW_0, fields=[
                _f("product", "Product", "link", to="products", required=True),
                _f("location", "Location", "link", to="locations"),
                _f("direction", "In or out", "choice", options=["In", "Out"], default="In", required=True),
                _f("quantity", "Quantity", "number", required=True),
                _f("change", "Change", "formula", formula="iif(direction == 'Out', -quantity, quantity)"),
                _f("reason", "Why", "choice", default="Received",
                   options=["Received", "Sold", "Moved", "Counted", "Damaged", "Returned", "Used in production", "Made"]),
                _f("batch", "Batch or lot"), _f("use_by", "Use by", "date"), _f("reference", "Reference"),
                _f("date", "Date", "date", default="today")]),
            _list("stock_counts", "Stock counts", "Stock count", icon="clipboard-list", fields=[
                _f("location", "Where", "link", to="locations", required=True), _f("date", "Date", "date", default="today"),
                _f("status", "Status", "choice", options=["Counting", "Done"], default="Counting")]),
            _list("count_lines", "Counted", "Counted item", inside="stock_counts", fields=[
                _f("stock_count", "Stock count", "link", to="stock_counts", required=True),
                _f("product", "Product", "link", to="products", required=True), _f("counted", "Counted", "number", default=1)],
                later=[_f("expected", "Expected", "lookup", path="product.on_hand"),
                       _f("difference", "Difference", "formula", formula="counted - expected")]),
            _list("transfers", "Stock moves", "Stock move", icon="truck", settings=NOT_BELOW_0, fields=[
                _f("product", "Product", "link", to="products", required=True),
                _f("from_location", "From", "link", to="locations", required=True),
                _f("to_location", "To", "link", to="locations", required=True),
                _f("quantity", "Quantity", "number", required=True),
                _f("status", "Status", "choice", options=["Requested", "Moved"], default="Requested")]),
            {"do": "add_view", "list": "products", "view": {"type": "list", "label": "Running low", "filters": [_is("low", True)]}},
            {"do": "design_op", "op": {"op": "add_document", "table": "products", "document": {
                "label": "Shelf label", "format": "label", "fields": ["price"], "code_field": "sku"}}},
            _rule("count_done", "A finished count corrects the stock", {"on": "reaches", "list": "stock_counts", "stage": "Done"}, [
                {"do": "add_each", "list": "stock_movements", "lines": "count_lines", "link": "stock_count",
                 "lines_where": [_is("difference", 0, "not_equals")],
                 "values": [_set("product", "item.product"), _set("location", "record.location"),
                            _set("direction", "iif(item.difference < 0, 'Out', 'In')"), _set("quantity", "abs(item.difference)"),
                            _set("reason", to="Counted"), _set("reference", to="Stock count {{record.date}}")]}]),
            _rule("move_stock", "A stock move takes it from one place to the other", _changed("transfers", "Moved"), [
                {"do": "add", "list": "stock_movements", "values": [
                    _set("product", "record.product"), _set("location", "record.from_location"), _set("direction", to="Out"),
                    _set("quantity", "record.quantity"), _set("reason", to="Moved")]},
                {"do": "add", "list": "stock_movements", "values": [
                    _set("product", "record.product"), _set("location", "record.to_location"), _set("direction", to="In"),
                    _set("quantity", "record.quantity"), _set("reason", to="Moved")]}]),
            {"do": "design_op", "op": {"op": "add_flow", "flow": {
                "key": "stock_count", "label": "Count stock", "table": "stock_counts", "start": "new", "again": True,
                "description": "Scan everything on a shelf; the differences correct the stock.",
                "steps": [{"kind": "form", "label": "Where are you counting?", "fields": ["location"]},
                          {"kind": "scan", "label": "Scan each item", "into": {"table": "count_lines", "item": "product", "quantity": "counted"}},
                          {"kind": "decide", "label": "Finished counting?", "field": "status",
                           "options": [{"label": "Yes: correct the stock", "value": "Done"}, {"label": "Not yet: keep it open", "value": "Counting"}]}]}}},
            {"do": "design_op", "op": {"op": "add_page", "page": {"label": "Stock", "icon": "boxes", "widgets": [
                {"type": "metric", "label": "Products", "table": "products", "aggregate": "count"},
                {"type": "metric", "label": "Running low", "table": "products", "aggregate": "count", "filters": [_is("low", True)]},
                {"type": "list", "label": "Running low", "table": "products", "filters": [_is("low", True)],
                 "columns": ["name", "sku", "on_hand", "reorder_point"]}]}}},
        ],
    },
    "orders": {
        "label": "Orders & fulfilment", "icon": "shopping-cart",
        "about": "Customers and their orders with lines, picked by scanning each item against the order, packed, shipped with "
                 "the carrier and tracking number, packing slips and address labels, and returns.",
        "products": [],
        "patches": [
            _list("customers", "Customers", "Customer", icon="users", fields=[
                NAME, _f("email", "Email", "email"), _f("phone", "Phone", "phone"), _f("address", "Address", "address")]),
            _list("sales_orders", "Orders", "Order", icon="shopping-cart", fields=[
                _f("number", "Number", "auto_number", prefix="SO-"), _f("customer", "Customer", "link", to="customers", required=True),
                _f("date", "Date", "date", default="today"), _f("ship_to", "Ship to", "address"), _f("carrier", "Carrier"),
                _f("tracking", "Tracking number"),
                _f("total", "Total", "total", of="order_lines", link="order", how="sum", field="amount"),
                _f("status", "Status", "choice", options=["New", "Picking", "Packed", "Shipped", "Cancelled"], default="New")],
                stages={"field": "status", "stages": ["New", "Picking", "Packed", "Shipped", "Cancelled"], "start": "New", "moves": [
                    {"from": ["New"], "to": "Picking", "button": "Start picking"}, {"from": ["Picking"], "to": "Packed", "button": "Packed"},
                    {"from": ["Packed"], "to": "Shipped", "button": "Shipped", "needs": ["carrier"]},
                    {"from": ["New", "Picking", "Packed"], "to": "Cancelled", "button": "Cancel"}]},
                documents=[{"label": "Packing slip", "title": "Packing slip", "number_field": "number", "fields": ["date"],
                            "to": {"field": "customer", "details": ["address"]},
                            "lines": {"table": "order_lines", "via": "order", "columns": ["product", "quantity"]}},
                           {"label": "Address label", "format": "label", "size": "100x150", "fields": ["ship_to", "carrier"]}]),
            _list("order_lines", "Order lines", "Order line", inside="sales_orders", settings=NOT_BELOW_0, fields=[
                _f("order", "Order", "link", to="sales_orders", required=True), _f("product", "Product", "link", to="products", required=True),
                _f("quantity", "Quantity", "number", required=True, default=1), _f("unit_price", "Unit price", "money"),
                _f("amount", "Amount", "formula", formula="quantity * unit_price", shows="money"), _f("picked", "Picked", "number")]),
            _list("returns", "Returns", "Return", icon="undo-2", settings=NOT_BELOW_0, fields=[
                _f("order", "Order", "link", to="sales_orders", required=True), _f("product", "Product", "link", to="products", required=True),
                _f("quantity", "Quantity", "number", required=True, default=1),
                _f("reason", "Why", "choice", options=["Damaged", "Wrong item", "Not wanted", "Other"]),
                _f("status", "Status", "choice", options=["Asked for", "Received", "Refunded"], default="Asked for")]),
            {"do": "add_view", "list": "sales_orders", "view": {"type": "inbox", "label": "To pick", "filters": [_is("status", "Picking")]}},
            {"do": "design_op", "op": {"op": "add_flow", "flow": {
                "key": "pick_order", "label": "Pick and pack", "table": "sales_orders", "start": "record",
                "when": [_is("status", "Picking")],
                "steps": [{"kind": "pick", "label": "Scan each item", "lines": {"table": "order_lines", "via": "order"}, "target": "quantity",
                           "count_into": "picked", "code": "product.sku"},
                          {"kind": "decide", "label": "All packed?", "field": "status",
                           "options": [{"label": "Packed", "value": "Packed"}, {"label": "Not yet", "value": "Picking"}]},
                          {"kind": "print", "label": "Address label", "document": "address_label", "of": "record"}]}}},
        ],
        "with": {"inventory": [
            _rule("shipped_takes_stock", "A shipped order takes its stock off", _changed("sales_orders", "Shipped"), [
                {"do": "add_each", "list": "stock_movements", "lines": "order_lines", "link": "order", "values": [
                    _set("product", "item.product"), _set("direction", to="Out"), _set("quantity", "item.quantity"), _set("reason", to="Sold"),
                    _set("reference", "record.number")]}]),
            _rule("return_received", "A returned item goes back on the stock", _changed("returns", "Received"), [
                {"do": "add", "list": "stock_movements", "values": [
                    _set("product", "record.product"), _set("direction", to="In"), _set("quantity", "record.quantity"),
                    _set("reason", to="Returned")]}],
                only_if=[_is("reason", "Damaged", "not_equals")])]},
    },
    "procurement": {
        "label": "Procurement", "icon": "factory",
        "about": "Suppliers and purchase orders with lines, approval for big ones, the order sent as a PDF, and the delivery "
                 "received by counting it in on a phone (short deliveries asked why).",
        "products": [],
        "patches": [
            _list("suppliers", "Suppliers", "Supplier", icon="factory", fields=[
                NAME, _f("email", "Email", "email"), _f("phone", "Phone", "phone"), _f("lead_time", "Days to deliver", "number")]),
            _list("purchase_orders", "Purchase orders", "Purchase order", icon="receipt", fields=[
                _f("number", "Number", "auto_number", prefix="PO-"), _f("supplier", "Supplier", "link", to="suppliers", required=True),
                _f("date", "Date", "date", default="today"), _f("expected", "Expected", "date"),
                _f("total", "Total", "total", of="po_lines", link="purchase_order", how="sum", field="amount"),
                _f("short_note", "Why it came short"),
                _f("status", "Status", "choice", options=["Draft", "Waiting for approval", "Approved", "Sent", "Received", "Cancelled"],
                   default="Draft")],
                stages={"field": "status", "stages": ["Draft", "Waiting for approval", "Approved", "Sent", "Received", "Cancelled"],
                        "start": "Draft", "moves": [
                            {"from": ["Draft"], "to": "Waiting for approval", "button": "Ask for approval",
                             "only_if": [_is("total", 5000, "greater_than")]},
                            {"from": ["Draft", "Approved"], "to": "Sent", "button": "Send to supplier", "only_if": [_is("total", 5000, "at_most")]},
                            {"from": ["Approved"], "to": "Sent", "button": "Send to supplier"},
                            {"from": ["Draft", "Waiting for approval", "Approved", "Sent"], "to": "Cancelled", "button": "Cancel"}]},
                documents=[{"label": "Purchase order", "title": "Purchase order", "number_field": "number", "fields": ["date", "expected"],
                            "to": {"field": "supplier", "details": ["email", "phone"]},
                            "lines": {"table": "po_lines", "via": "purchase_order", "columns": ["product", "quantity", "unit_cost", "amount"]},
                            "totals": ["total"]}]),
            _list("po_lines", "Purchase order lines", "Purchase order line", inside="purchase_orders", settings=NOT_BELOW_0, fields=[
                _f("purchase_order", "Purchase order", "link", to="purchase_orders", required=True),
                _f("product", "Product", "link", to="products", required=True),
                _f("quantity", "Quantity", "number", required=True, default=1), _f("unit_cost", "Unit cost", "money"),
                _f("amount", "Amount", "formula", formula="quantity * unit_cost", shows="money"), _f("received", "Received", "number")]),
            # the language's approval asks admin and waits; who may not give it, and back to Draft when it's turned down,
            # are said in design ops (unsaid_for)
            _rule("approve_po", "A big purchase order is approved first", _changed("purchase_orders", "Waiting for approval"), [
                {"do": "approval", "who": "admin", "message": "Please approve this purchase order."},
                {"do": "set", "values": [_set("status", to="Approved")]}]),
            {"do": "design_op", "op": {"op": "add_flow", "flow": {
                "key": "receive_po", "label": "Receive the delivery", "table": "purchase_orders", "start": "record",
                "when": [_is("status", "Sent")],
                "steps": [{"kind": "info", "label": "Check the delivery", "text": "Delivery for **{{record.number}}**: count each item in."},
                          {"kind": "pick", "label": "Count it in", "lines": {"table": "po_lines", "via": "purchase_order"}, "target": "quantity",
                           "count_into": "received", "code": "product.sku"},
                          {"kind": "decide", "label": "Done?", "field": "status",
                           "options": [{"label": "Received", "value": "Received"}, {"label": "Not yet", "value": "Sent"}]}],
                "then": [{"type": "update_record", "values": {"short_note": "{{flow.count_it_in.reason}}"}}]}}},
        ],
        "unsaid": [
            {"op": "update_automation", "automation": {
                "key": "approve_po", "name": "A big purchase order is approved first", "trigger": {"type": "record_updated", "table": "purchase_orders"},
                "conditions": [{"field": "status", "op": "changed_to", "value": "Waiting for approval"}],
                "actions": [{"type": "request_approval", "approver": "admin", "message": "Please approve this purchase order.", "not_by": ["creator"],
                             "if_rejected": [{"type": "update_record", "values": {"status": "Draft"}}]},
                            {"type": "update_record", "values": {"status": "Approved"}}]}}],
        "with": {"inventory": [
            _rule("received_adds_stock", "A received delivery puts its stock on", _changed("purchase_orders", "Received"), [
                {"do": "add_each", "list": "stock_movements", "lines": "po_lines", "link": "purchase_order",
                 "lines_where": [_is("received", 0, "greater_than")],
                 "values": [_set("product", "item.product"), _set("direction", to="In"), _set("quantity", "item.received"),
                            _set("reason", to="Received"), _set("reference", "record.number")]}])]},
    },
    "production": {
        "label": "Production", "icon": "hammer",
        "about": "What each product is made of, work orders with their materials, and finishing one puts the made product on "
                 "the stock and takes its materials off.",
        "products": [],
        "patches": [
            _list("bom_lines", "Made of", "Material", icon="layers", settings=NOT_BELOW_0, fields=[
                _f("product", "Product", "link", to="products", required=True), _f("material", "Material", "link", to="products", required=True),
                _f("quantity", "Per one made", "number", required=True)]),
            _list("work_orders", "Work orders", "Work order", icon="hammer", settings=NOT_BELOW_0, fields=[
                _f("number", "Number", "auto_number", prefix="WO-"), _f("product", "Product", "link", to="products", required=True),
                _f("quantity", "How many", "number", required=True, default=1), _f("due", "Due", "date"),
                _f("status", "Status", "choice", options=["Planned", "In progress", "Done", "Cancelled"], default="Planned")],
                stages={"field": "status", "stages": ["Planned", "In progress", "Done", "Cancelled"], "start": "Planned", "moves": [
                    {"from": ["Planned"], "to": "In progress", "button": "Start"}, {"from": ["In progress"], "to": "Done", "button": "Finish"},
                    {"from": ["Planned", "In progress"], "to": "Cancelled", "button": "Cancel"}]}),
            _list("wo_materials", "Materials used", "Material used", inside="work_orders", settings=NOT_BELOW_0, fields=[
                _f("work_order", "Work order", "link", to="work_orders", required=True),
                _f("material", "Material", "link", to="products", required=True), _f("quantity", "Quantity", "number", required=True)]),
            {"do": "add_view", "list": "work_orders", "view": {"type": "board", "label": "Board", "group_by": "status"}},
        ],
        "with": {"inventory": [
            _rule("finished_moves_stock", "A finished work order puts the product on and takes its materials off",
                  _changed("work_orders", "Done"), [
                      {"do": "add", "list": "stock_movements", "values": [
                          _set("product", "record.product"), _set("direction", to="In"), _set("quantity", "record.quantity"),
                          _set("reason", to="Made"), _set("reference", "record.number")]},
                      {"do": "add_each", "list": "stock_movements", "lines": "wo_materials", "link": "work_order", "values": [
                          _set("product", "item.material"), _set("direction", to="Out"), _set("quantity", "item.quantity"),
                          _set("reason", to="Used in production"), _set("reference", "record.number")]}])]},
    },
}


def _place(patch):
    """Where a patch goes among the parts' patches: the lists first, then the fields they add to lists already there,
    then the fields worked out from those (the products' on hand, a count line's expected stock), then the rest."""
    if patch["do"] == "add_list":
        return 0
    if patch["do"] == "add_field":
        return 2 if patch["field"]["type"] in WORKED_OUT else 1
    return 3


def ops_for(keys, bp=None):
    """The Keel patches for the chosen parts, leaving out lists the app already has (and what goes with them)."""
    keys = [k for k in MODULES if k in keys]
    tables = (bp or {}).get("tables", [])
    have = {t["key"] for t in tables}
    mine = {f["key"] for t in tables if t["key"] == "products" for f in t["fields"]}
    wanted = [f for k in keys for f in MODULES[k]["products"]]
    plain = [f for f in wanted if f["type"] not in WORKED_OUT]
    out = []
    if "products" not in have:
        out += _list(**{**PRODUCTS, "fields": PRODUCTS["fields"] + plain})
    else:  # the app has its products: what these parts keep about each is added to them
        out += [{"do": "add_field", "list": "products", "field": f} for f in PRODUCTS["fields"] + plain if f["key"] not in mine]
    out += [{"do": "add_field", "list": "products", "field": f} for f in wanted if f["type"] in WORKED_OUT and f["key"] not in mine]
    made = have | {"products"}
    for k in keys:
        for entry in MODULES[k]["patches"]:
            if isinstance(entry, list):  # a list and what goes with it
                if entry[0]["key"] in made:
                    continue
                made.add(entry[0]["key"])
                out += entry
            else:
                out.append(entry)
    present = _present(bp)
    for k in keys + [p for p in present if p not in keys]:  # what joins a part to one chosen or already there, either way round
        for other, joins in MODULES[k].get("with", {}).items():
            if (k in keys and (other in keys or other in present)) or (k in present and other in keys):
                out += joins
    return sorted(out, key=_place)


def unsaid_for(keys, bp=None):
    """The design ops for what the chosen parts need that the Keel language can't say yet, applied after their patches:
    a purchase order is never approved by whoever made it, and goes back to Draft when it's turned down."""
    return [op for k in MODULES if k in keys for op in MODULES[k].get("unsaid", [])]


def _present(bp):
    """The parts an app has already (by their main list): Inventory if it keeps stock movements, and so on."""
    have = {t["key"] for t in (bp or {}).get("tables", [])}
    return {k for k, main in (("inventory", "stock_movements"), ("orders", "sales_orders"), ("procurement", "purchase_orders"),
                              ("production", "work_orders")) if main in have}
