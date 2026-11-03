"""Ready-made parts to start an app from: Inventory & warehouse, Orders & fulfilment, Procurement, Production. Each is the
proven way of doing it (stock as movements, a delivery received by counting it in, an order picked by scanning), made of
the same pieces the builder uses, so the builder carries on from it like from anything it made.

They fit together: a list two parts both need (Products) is made once with the fields of both, and what joins two parts
(an order shipped takes its stock off; a purchase order received puts it on) comes only when both are chosen.
`ops_for(keys, bp)` is the design changes for the chosen parts, leaving out what the app has already.
"""
STOCK_IN = {"type": "create_for_each", "table": "stock_movements"}

PRODUCTS = {"op": "create_table", "table": "products", "label": "Products", "singular": "Product", "icon": "package", "fields": [
    {"key": "name", "label": "Name", "type": "text", "required": True},
    {"key": "sku", "label": "SKU", "type": "barcode", "unique": True},
    {"key": "price", "label": "Price", "type": "money"},
    {"key": "status", "label": "Status", "type": "select", "options": ["Active", "Discontinued"], "default": "Active"}]}

MODULES = {
    "inventory": {
        "label": "Inventory & warehouse", "icon": "boxes",
        "about": "Products and their stock in each place, every movement kept (received, sold, moved, counted, damaged), "
                 "batches and use-by dates, what's running low, stock counts on a phone, moves between places, and labels to scan.",
        "products": [{"key": "cost", "label": "Cost", "type": "money", "access": "admin"},
                     {"key": "reorder_point", "label": "Reorder when down to", "type": "number"},
                     {"key": "reorder_qty", "label": "Reorder how many", "type": "number"},
                     {"key": "on_hand", "label": "On hand", "type": "rollup", "table": "stock_movements", "via": "product",
                      "aggregate": "sum", "field": "change"},
                     {"key": "low", "label": "Running low", "type": "formula", "expression": "reorder_point > 0 and on_hand <= reorder_point",
                      "format": "checkbox"}],
        "ops": [
            {"op": "create_table", "table": "locations", "label": "Locations", "singular": "Location", "icon": "map-pin", "fields": [
                {"key": "name", "label": "Name", "type": "text", "required": True},
                {"key": "kind", "label": "Kind", "type": "select", "options": ["Warehouse", "Shelf or bin", "Van", "Quarantine"], "default": "Warehouse"},
                {"key": "code", "label": "Code", "type": "barcode", "unique": True}],
             "documents": [{"label": "Location label", "format": "label", "fields": ["kind"], "code_field": "code"}]},
            {"op": "create_table", "table": "stock_movements", "label": "Stock movements", "singular": "Stock movement", "icon": "arrow-up-down",
             "fields": [
                 {"key": "product", "label": "Product", "type": "link", "table": "products", "required": True},
                 {"key": "location", "label": "Location", "type": "link", "table": "locations"},
                 {"key": "direction", "label": "In or out", "type": "select", "options": ["In", "Out"], "default": "In", "required": True},
                 {"key": "quantity", "label": "Quantity", "type": "number", "required": True, "min": 0},
                 {"key": "change", "label": "Change", "type": "formula", "expression": "iif(direction == 'Out', -quantity, quantity)"},
                 {"key": "reason", "label": "Why", "type": "select", "default": "Received",
                  "options": ["Received", "Sold", "Moved", "Counted", "Damaged", "Returned", "Used in production", "Made"]},
                 {"key": "batch", "label": "Batch or lot", "type": "text"},
                 {"key": "use_by", "label": "Use by", "type": "date"},
                 {"key": "reference", "label": "Reference", "type": "text"},
                 {"key": "date", "label": "Date", "type": "date", "default": "today"}]},
            {"op": "create_table", "table": "stock_counts", "label": "Stock counts", "singular": "Stock count", "icon": "clipboard-list", "fields": [
                {"key": "location", "label": "Where", "type": "link", "table": "locations", "required": True},
                {"key": "date", "label": "Date", "type": "date", "default": "today"},
                {"key": "status", "label": "Status", "type": "select", "options": ["Counting", "Done"], "default": "Counting"}]},
            {"op": "create_table", "table": "count_lines", "label": "Counted", "singular": "Counted item", "embedded": True, "fields": [
                {"key": "stock_count", "label": "Stock count", "type": "link", "table": "stock_counts", "required": True},
                {"key": "product", "label": "Product", "type": "link", "table": "products", "required": True},
                {"key": "counted", "label": "Counted", "type": "number", "default": 1},
                {"key": "expected", "label": "Expected", "type": "lookup", "link": "product", "field": "on_hand"},
                {"key": "difference", "label": "Difference", "type": "formula", "expression": "counted - expected"}]},
            {"op": "create_table", "table": "transfers", "label": "Stock moves", "singular": "Stock move", "icon": "truck", "fields": [
                {"key": "product", "label": "Product", "type": "link", "table": "products", "required": True},
                {"key": "from_location", "label": "From", "type": "link", "table": "locations", "required": True},
                {"key": "to_location", "label": "To", "type": "link", "table": "locations", "required": True},
                {"key": "quantity", "label": "Quantity", "type": "number", "required": True, "min": 0},
                {"key": "status", "label": "Status", "type": "select", "options": ["Requested", "Moved"], "default": "Requested"}]},
            {"op": "add_view", "table": "products", "view": {"key": "running_low", "label": "Running low", "type": "list",
                                                            "filters": [{"field": "low", "op": "equals", "value": True}]}},
            {"op": "add_document", "table": "products", "document": {"label": "Shelf label", "format": "label", "fields": ["price"], "code_field": "sku"}},
            {"op": "add_automation", "automation": {
                "key": "count_done", "name": "A finished count corrects the stock", "trigger": {"type": "record_created_or_updated", "table": "stock_counts"},
                "conditions": [{"field": "status", "op": "changed_to", "value": "Done"}],
                "actions": [{"type": "create_for_each", "table": "stock_movements", "source": {"table": "count_lines", "via": "stock_count"},
                             "where": [{"field": "difference", "op": "not_equals", "value": 0}],
                             "values": {"product": "{{item.product}}", "location": "{{record.location}}",
                                        "direction": "{{iif(item.difference < 0, 'Out', 'In')}}", "quantity": "{{abs(item.difference)}}",
                                        "reason": "Counted", "reference": "Stock count {{record.date}}"}}]}},
            {"op": "add_automation", "automation": {
                "key": "move_stock", "name": "A stock move takes it from one place to the other", "trigger": {"type": "record_updated", "table": "transfers"},
                "conditions": [{"field": "status", "op": "changed_to", "value": "Moved"}],
                "actions": [{"type": "create_record", "table": "stock_movements", "values": {
                                "product": "{{record.product}}", "location": "{{record.from_location}}", "direction": "Out",
                                "quantity": "{{record.quantity}}", "reason": "Moved"}},
                            {"type": "create_record", "table": "stock_movements", "values": {
                                "product": "{{record.product}}", "location": "{{record.to_location}}", "direction": "In",
                                "quantity": "{{record.quantity}}", "reason": "Moved"}}]}},
            {"op": "add_flow", "flow": {
                "key": "stock_count", "label": "Count stock", "table": "stock_counts", "start": "new", "again": True,
                "description": "Scan everything on a shelf; the differences correct the stock.",
                "steps": [{"kind": "form", "label": "Where are you counting?", "fields": ["location"]},
                          {"kind": "scan", "label": "Scan each item", "into": {"table": "count_lines", "item": "product", "quantity": "counted"}},
                          {"kind": "decide", "label": "Finished counting?", "field": "status",
                           "options": [{"label": "Yes: correct the stock", "value": "Done"}, {"label": "Not yet: keep it open", "value": "Counting"}]}]}},
            {"op": "add_page", "page": {"label": "Stock", "icon": "boxes", "widgets": [
                {"type": "metric", "label": "Products", "table": "products", "aggregate": "count"},
                {"type": "metric", "label": "Running low", "table": "products", "aggregate": "count", "filters": [{"field": "low", "op": "equals", "value": True}]},
                {"type": "list", "label": "Running low", "table": "products", "filters": [{"field": "low", "op": "equals", "value": True}],
                 "columns": ["name", "sku", "on_hand", "reorder_point"]}]}},
        ],
    },
    "orders": {
        "label": "Orders & fulfilment", "icon": "shopping-cart",
        "about": "Customers and their orders with lines, picked by scanning each item against the order, packed, shipped with "
                 "the carrier and tracking number, packing slips and address labels, and returns.",
        "products": [],
        "ops": [
            {"op": "create_table", "table": "customers", "label": "Customers", "singular": "Customer", "icon": "users", "fields": [
                {"key": "name", "label": "Name", "type": "text", "required": True}, {"key": "email", "label": "Email", "type": "email"},
                {"key": "phone", "label": "Phone", "type": "phone"}, {"key": "address", "label": "Address", "type": "address"}]},
            {"op": "create_table", "table": "sales_orders", "label": "Orders", "singular": "Order", "icon": "shopping-cart", "fields": [
                {"key": "number", "label": "Number", "type": "autonumber", "prefix": "SO-"},
                {"key": "customer", "label": "Customer", "type": "link", "table": "customers", "required": True},
                {"key": "date", "label": "Date", "type": "date", "default": "today"},
                {"key": "ship_to", "label": "Ship to", "type": "address"},
                {"key": "carrier", "label": "Carrier", "type": "text"}, {"key": "tracking", "label": "Tracking number", "type": "text"},
                {"key": "total", "label": "Total", "type": "rollup", "table": "order_lines", "via": "order", "aggregate": "sum", "field": "amount"},
                {"key": "status", "label": "Status", "type": "select", "options": ["New", "Picking", "Packed", "Shipped", "Cancelled"], "default": "New"}],
             "workflow": {"field": "status", "transitions": [
                 {"from": ["New"], "to": "Picking", "label": "Start picking"}, {"from": ["Picking"], "to": "Packed", "label": "Packed"},
                 {"from": ["Packed"], "to": "Shipped", "label": "Shipped", "requires": ["carrier"]},
                 {"from": ["New", "Picking", "Packed"], "to": "Cancelled", "label": "Cancel"}]},
             "documents": [{"label": "Packing slip", "title": "Packing slip", "number_field": "number", "fields": ["date"],
                            "to": {"field": "customer", "details": ["address"]}, "lines": {"table": "order_lines", "via": "order", "columns": ["product", "quantity"]}},
                           {"label": "Address label", "format": "label", "size": "100x150", "fields": ["ship_to", "carrier"]}]},
            {"op": "create_table", "table": "order_lines", "label": "Order lines", "singular": "Order line", "embedded": True, "fields": [
                {"key": "order", "label": "Order", "type": "link", "table": "sales_orders", "required": True},
                {"key": "product", "label": "Product", "type": "link", "table": "products", "required": True},
                {"key": "quantity", "label": "Quantity", "type": "number", "required": True, "default": 1, "min": 0},
                {"key": "unit_price", "label": "Unit price", "type": "money"},
                {"key": "amount", "label": "Amount", "type": "formula", "expression": "quantity * unit_price", "format": "money"},
                {"key": "picked", "label": "Picked", "type": "number"}]},
            {"op": "create_table", "table": "returns", "label": "Returns", "singular": "Return", "icon": "undo-2", "fields": [
                {"key": "order", "label": "Order", "type": "link", "table": "sales_orders", "required": True},
                {"key": "product", "label": "Product", "type": "link", "table": "products", "required": True},
                {"key": "quantity", "label": "Quantity", "type": "number", "required": True, "default": 1, "min": 0},
                {"key": "reason", "label": "Why", "type": "select", "options": ["Damaged", "Wrong item", "Not wanted", "Other"]},
                {"key": "status", "label": "Status", "type": "select", "options": ["Asked for", "Received", "Refunded"], "default": "Asked for"}]},
            {"op": "add_view", "table": "sales_orders", "view": {"key": "to_pick", "label": "To pick", "type": "inbox",
                                                                "filters": [{"field": "status", "op": "equals", "value": "Picking"}]}},
            {"op": "add_flow", "flow": {
                "key": "pick_order", "label": "Pick and pack", "table": "sales_orders", "start": "record",
                "when": [{"field": "status", "op": "equals", "value": "Picking"}],
                "steps": [{"kind": "pick", "label": "Scan each item", "lines": {"table": "order_lines", "via": "order"}, "target": "quantity",
                           "count_into": "picked", "code": "product.sku"},
                          {"kind": "decide", "label": "All packed?", "field": "status",
                           "options": [{"label": "Packed", "value": "Packed"}, {"label": "Not yet", "value": "Picking"}]},
                          {"kind": "print", "label": "Address label", "document": "address_label", "of": "record"}]}},
        ],
        "with": {"inventory": [
            {"op": "add_automation", "automation": {
                "key": "shipped_takes_stock", "name": "A shipped order takes its stock off", "trigger": {"type": "record_updated", "table": "sales_orders"},
                "conditions": [{"field": "status", "op": "changed_to", "value": "Shipped"}],
                "actions": [{"type": "create_for_each", "table": "stock_movements", "source": {"table": "order_lines", "via": "order"},
                             "values": {"product": "{{item.product}}", "direction": "Out", "quantity": "{{item.quantity}}", "reason": "Sold",
                                        "reference": "{{record.number}}"}}]}},
            {"op": "add_automation", "automation": {
                "key": "return_received", "name": "A returned item goes back on the stock", "trigger": {"type": "record_updated", "table": "returns"},
                "conditions": [{"field": "status", "op": "changed_to", "value": "Received"}, {"field": "reason", "op": "not_equals", "value": "Damaged"}],
                "actions": [{"type": "create_record", "table": "stock_movements", "values": {
                    "product": "{{record.product}}", "direction": "In", "quantity": "{{record.quantity}}", "reason": "Returned"}}]}}]},
    },
    "procurement": {
        "label": "Procurement", "icon": "factory",
        "about": "Suppliers and purchase orders with lines, approval for big ones, the order sent as a PDF, and the delivery "
                 "received by counting it in on a phone (short deliveries asked why).",
        "products": [],
        "ops": [
            {"op": "create_table", "table": "suppliers", "label": "Suppliers", "singular": "Supplier", "icon": "factory", "fields": [
                {"key": "name", "label": "Name", "type": "text", "required": True}, {"key": "email", "label": "Email", "type": "email"},
                {"key": "phone", "label": "Phone", "type": "phone"}, {"key": "lead_time", "label": "Days to deliver", "type": "number"}]},
            {"op": "create_table", "table": "purchase_orders", "label": "Purchase orders", "singular": "Purchase order", "icon": "receipt", "fields": [
                {"key": "number", "label": "Number", "type": "autonumber", "prefix": "PO-"},
                {"key": "supplier", "label": "Supplier", "type": "link", "table": "suppliers", "required": True},
                {"key": "date", "label": "Date", "type": "date", "default": "today"}, {"key": "expected", "label": "Expected", "type": "date"},
                {"key": "total", "label": "Total", "type": "rollup", "table": "po_lines", "via": "purchase_order", "aggregate": "sum", "field": "amount"},
                {"key": "short_note", "label": "Why it came short", "type": "text"},
                {"key": "status", "label": "Status", "type": "select", "options": ["Draft", "Waiting for approval", "Approved", "Sent", "Received", "Cancelled"],
                 "default": "Draft"}],
             "workflow": {"field": "status", "transitions": [
                 {"from": ["Draft"], "to": "Waiting for approval", "label": "Ask for approval", "when": [{"field": "total", "op": "greater_than", "value": 5000}]},
                 {"from": ["Draft", "Approved"], "to": "Sent", "label": "Send to supplier", "when": [{"field": "total", "op": "at_most", "value": 5000}]},
                 {"from": ["Approved"], "to": "Sent", "label": "Send to supplier"},
                 {"from": ["Draft", "Waiting for approval", "Approved", "Sent"], "to": "Cancelled", "label": "Cancel"}]},
             "documents": [{"label": "Purchase order", "title": "Purchase order", "number_field": "number", "fields": ["date", "expected"],
                            "to": {"field": "supplier", "details": ["email", "phone"]},
                            "lines": {"table": "po_lines", "via": "purchase_order", "columns": ["product", "quantity", "unit_cost", "amount"]},
                            "totals": ["total"]}]},
            {"op": "create_table", "table": "po_lines", "label": "Purchase order lines", "singular": "Purchase order line", "embedded": True, "fields": [
                {"key": "purchase_order", "label": "Purchase order", "type": "link", "table": "purchase_orders", "required": True},
                {"key": "product", "label": "Product", "type": "link", "table": "products", "required": True},
                {"key": "quantity", "label": "Quantity", "type": "number", "required": True, "default": 1, "min": 0},
                {"key": "unit_cost", "label": "Unit cost", "type": "money"},
                {"key": "amount", "label": "Amount", "type": "formula", "expression": "quantity * unit_cost", "format": "money"},
                {"key": "received", "label": "Received", "type": "number"}]},
            {"op": "add_automation", "automation": {
                "key": "approve_po", "name": "A big purchase order is approved first", "trigger": {"type": "record_updated", "table": "purchase_orders"},
                "conditions": [{"field": "status", "op": "changed_to", "value": "Waiting for approval"}],
                "actions": [{"type": "request_approval", "approver": "admin", "message": "Please approve this purchase order.", "not_by": ["creator"],
                             "if_rejected": [{"type": "update_record", "values": {"status": "Draft"}}]},
                            {"type": "update_record", "values": {"status": "Approved"}}]}},
            {"op": "add_flow", "flow": {
                "key": "receive_po", "label": "Receive the delivery", "table": "purchase_orders", "start": "record",
                "when": [{"field": "status", "op": "equals", "value": "Sent"}],
                "steps": [{"kind": "info", "label": "Check the delivery", "text": "Delivery for **{{record.number}}**: count each item in."},
                          {"kind": "pick", "label": "Count it in", "lines": {"table": "po_lines", "via": "purchase_order"}, "target": "quantity",
                           "count_into": "received", "code": "product.sku"},
                          {"kind": "decide", "label": "Done?", "field": "status",
                           "options": [{"label": "Received", "value": "Received"}, {"label": "Not yet", "value": "Sent"}]}],
                "then": [{"type": "update_record", "values": {"short_note": "{{flow.count_it_in.reason}}"}}]}},
        ],
        "with": {"inventory": [
            {"op": "add_automation", "automation": {
                "key": "received_adds_stock", "name": "A received delivery puts its stock on", "trigger": {"type": "record_updated", "table": "purchase_orders"},
                "conditions": [{"field": "status", "op": "changed_to", "value": "Received"}],
                "actions": [{"type": "create_for_each", "table": "stock_movements", "source": {"table": "po_lines", "via": "purchase_order"},
                             "where": [{"field": "received", "op": "greater_than", "value": 0}],
                             "values": {"product": "{{item.product}}", "direction": "In", "quantity": "{{item.received}}", "reason": "Received",
                                        "reference": "{{record.number}}"}}]}}]},
    },
    "production": {
        "label": "Production", "icon": "hammer",
        "about": "What each product is made of, work orders with their materials, and finishing one puts the made product on "
                 "the stock and takes its materials off.",
        "products": [],
        "ops": [
            {"op": "create_table", "table": "bom_lines", "label": "Made of", "singular": "Material", "icon": "layers", "fields": [
                {"key": "product", "label": "Product", "type": "link", "table": "products", "required": True},
                {"key": "material", "label": "Material", "type": "link", "table": "products", "required": True},
                {"key": "quantity", "label": "Per one made", "type": "number", "required": True, "min": 0}]},
            {"op": "create_table", "table": "work_orders", "label": "Work orders", "singular": "Work order", "icon": "hammer", "fields": [
                {"key": "number", "label": "Number", "type": "autonumber", "prefix": "WO-"},
                {"key": "product", "label": "Product", "type": "link", "table": "products", "required": True},
                {"key": "quantity", "label": "How many", "type": "number", "required": True, "default": 1, "min": 0},
                {"key": "due", "label": "Due", "type": "date"},
                {"key": "status", "label": "Status", "type": "select", "options": ["Planned", "In progress", "Done", "Cancelled"], "default": "Planned"}],
             "workflow": {"field": "status", "transitions": [
                 {"from": ["Planned"], "to": "In progress", "label": "Start"}, {"from": ["In progress"], "to": "Done", "label": "Finish"},
                 {"from": ["Planned", "In progress"], "to": "Cancelled", "label": "Cancel"}]}},
            {"op": "create_table", "table": "wo_materials", "label": "Materials used", "singular": "Material used", "embedded": True, "fields": [
                {"key": "work_order", "label": "Work order", "type": "link", "table": "work_orders", "required": True},
                {"key": "material", "label": "Material", "type": "link", "table": "products", "required": True},
                {"key": "quantity", "label": "Quantity", "type": "number", "required": True, "min": 0}]},
            {"op": "add_view", "table": "work_orders", "view": {"key": "board", "label": "Board", "type": "board", "group_by": "status"}},
        ],
        "with": {"inventory": [
            {"op": "add_automation", "automation": {
                "key": "finished_moves_stock", "name": "A finished work order puts the product on and takes its materials off",
                "trigger": {"type": "record_updated", "table": "work_orders"},
                "conditions": [{"field": "status", "op": "changed_to", "value": "Done"}],
                "actions": [{"type": "create_record", "table": "stock_movements", "values": {
                                "product": "{{record.product}}", "direction": "In", "quantity": "{{record.quantity}}", "reason": "Made",
                                "reference": "{{record.number}}"}},
                            {"type": "create_for_each", "table": "stock_movements", "source": {"table": "wo_materials", "via": "work_order"},
                             "values": {"product": "{{item.material}}", "direction": "Out", "quantity": "{{item.quantity}}",
                                        "reason": "Used in production", "reference": "{{record.number}}"}}]}}]},
    },
}


def ops_for(keys, bp=None):
    """The design changes for the chosen parts (in the order they need), leaving out lists the app already has."""
    keys = [k for k in MODULES if k in keys]
    have = {t["key"] for t in (bp or {}).get("tables", [])}
    out = []
    plain = [f for k in keys for f in MODULES[k]["products"] if f["type"] not in ("rollup", "formula")]
    if "products" not in have:
        out.append({**PRODUCTS, "fields": PRODUCTS["fields"] + plain})
    else:  # the app has its products: what these parts keep about each is added to them
        mine = {f["key"] for t in bp.get("tables", []) if t["key"] == "products" for f in t["fields"]}
        out += [{"op": "add_field", "table": "products", "field": f} for f in PRODUCTS["fields"] + plain if f["key"] not in mine]
    made = have | {"products"}
    later = []  # products' totals and flags: once the tables they read are there
    for k in keys:
        for op in MODULES[k]["ops"]:
            if op["op"] == "create_table" and op["table"] in made:
                continue
            if op["op"] == "create_table":
                made.add(op["table"])
            out.append(op)
        later += [{"op": "add_field", "table": "products", "field": f} for f in MODULES[k]["products"] if f["type"] in ("rollup", "formula")
                  and f["key"] not in {x["key"] for t in (bp or {}).get("tables", []) if t["key"] == "products" for x in t["fields"]}]
    out += later
    present = _present(bp)
    for k in keys + [p for p in present if p not in keys]:  # what joins a part to one chosen or already there, either way round
        for other, joins in MODULES[k].get("with", {}).items():
            if (k in keys and (other in keys or other in present)) or (k in present and other in keys):
                out += joins
    return out


def _present(bp):
    """The parts an app has already (by their main list): Inventory if it keeps stock movements, and so on."""
    have = {t["key"] for t in (bp or {}).get("tables", [])}
    return {k for k, main in (("inventory", "stock_movements"), ("orders", "sales_orders"), ("procurement", "purchase_orders"),
                              ("production", "work_orders")) if main in have}
