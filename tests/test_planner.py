"""Queries in SQL (engine/planner.py) give exactly what reading the whole table into Python gives, on PostgreSQL and on
the simulator's SQLite alike, for tidy records and for the untidy ones a design change leaves behind (numbers kept as
text, dates written another way, lists where one value belongs, nothing at all)."""
import random
from datetime import timedelta

import pytest

from app import create_app, sim
from app.blueprint import ops, schema
from app.engine.planner import Plan
from app.engine.records import Engine, FieldErrors
from app.extensions import db
from app.models import Membership, Record, User, Workspace

DESIGN = [
    {"op": "create_table", "table": "customers", "label": "Customers", "fields": [
        {"key": "name", "type": "text", "required": True},
        {"key": "region", "type": "select", "options": ["North", "South", "Café"]},
        {"key": "email", "type": "email"},
        {"key": "vip", "type": "checkbox"}]},
    {"op": "create_table", "table": "orders", "label": "Orders", "fields": [
        {"key": "reference", "type": "text", "unique": True},
        {"key": "customer", "type": "link", "table": "customers"},
        {"key": "status", "type": "select", "options": ["Draft", "Sent", "Paid"]},
        {"key": "amount", "type": "money"},
        {"key": "discount", "type": "percent", "default": 0.1},
        {"key": "hours", "type": "duration"},
        {"key": "due", "type": "date"},
        {"key": "when", "type": "datetime"},
        {"key": "rush", "type": "checkbox"},
        {"key": "labels", "type": "multi_select", "options": ["A", "B", "Ünder"]},
        {"key": "owner", "type": "user"},
        {"key": "note", "type": "text"},
        {"key": "address", "type": "address"},
        {"key": "doubled", "type": "formula", "expression": "amount * 2", "format": "money"},
        {"key": "customer_email", "type": "lookup", "link": "customer", "field": "email"}]},
    {"op": "add_field", "table": "customers", "field": {"key": "order_count", "type": "rollup", "table": "orders", "via": "customer", "aggregate": "count"}},
    {"op": "add_field", "table": "customers", "field": {"key": "ordered", "type": "rollup", "table": "orders", "via": "customer", "aggregate": "sum", "field": "amount"}},
]

# What a design change or an outside write can leave in a record: kept as they are, read by Python as they always were.
UNTIDY = [
    {"amount": "120", "status": "paid", "due": "30/11/2026", "rush": "yes", "note": 12},
    {"amount": "abc", "status": " Paid ", "due": "2026-11-05T10:00", "rush": "no", "note": True, "owner": "1"},
    {"amount": True, "status": "PAID", "due": "2026-02-30", "rush": 0, "note": ["x"], "owner": 1.0},
    {"amount": None, "status": 5, "due": 20261105, "rush": 1, "note": {"x": 1}, "owner": 5.5, "customer": "1"},
    {"amount": [], "status": None, "due": "", "rush": "", "note": "  hello  ", "customer": 1.0, "when": "2026-11-05"},
    {"amount": [1], "status": ["Paid"], "rush": None, "note": "ÉCOLE", "customer": [1], "when": "2026-11-05T24:00"},
    {"amount": {"a": 1}, "status": "Café", "rush": "Checked ", "labels": "A", "when": "2026-11-05 09:30", "hours": "1.5"},
    {"amount": 12.0, "status": "ÉCOLE", "labels": ["a", "B"], "owner": [1, 2], "discount": "", "hours": 2},
    {"amount": "", "labels": [], "owner": True, "discount": None, "address": "1 Main St\nPerth", "customer": True},
    {"amount": "  ", "labels": ["Ünder"], "reference": None, "note": "", "due": "2026-11-05"},
    {},
]


def _people_and_design(ws_bp):
    result = ops.apply(ws_bp, DESIGN)
    assert not result.problems, result.problems
    bp = result.blueprint
    orders = next(t for t in bp["tables"] if t["key"] == "orders")
    orders["row_access"] = {"field": "owner", "roles": ["editor"]}
    return bp


def _fill(seed=7):
    """The same people, design and records, in the same order (so with the same ids), in whichever database is open."""
    owner = User(email="owner@example.com", name="Olive Owner")
    rep = User(email="rep@example.com", name="Rex Rep")
    for u in (owner, rep):
        u.password_hash = "!"
        db.session.add(u)
    db.session.flush()
    ws = Workspace(owner_id=owner.id, name="Shop", blueprint=schema.empty("Shop"), version=0, state={}, settings={},
                   timezone="Australia/Perth")
    db.session.add(ws)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=owner.id, role="owner"))
    db.session.add(Membership(workspace_id=ws.id, user_id=rep.id, role="editor"))
    ws.blueprint = _people_and_design(ws.blueprint)
    db.session.commit()
    e = Engine(ws, by="test")
    rnd = random.Random(seed)
    names = ["Acme Pty Ltd", "Bolt & Co", "café crème", "Delta", "acme supplies", "Échelle"]
    customers = [e.create("customers", {"name": n, "region": rnd.choice(["North", "South", "Café", None]), "email": f"c{i}@example.com",
                                        "vip": rnd.random() < 0.5}) for i, n in enumerate(names)]
    today = e.today
    for i in range(70):
        e.create("orders", {"reference": f"R-{i:03d}", "customer": rnd.choice(customers).id if rnd.random() < 0.9 else None,
                            "status": rnd.choice(["Draft", "Sent", "Paid", None]), "amount": rnd.choice([None, 0, 15, 99.5, 120, 250.25, 1000]),
                            "discount": rnd.choice([None, 0.05, 0.2]), "hours": rnd.choice([None, 1, 2.5]),
                            "due": (today + timedelta(days=rnd.randrange(-40, 40))).isoformat() if rnd.random() < 0.85 else None,
                            "when": f"{(today + timedelta(days=rnd.randrange(-5, 5))).isoformat()} {rnd.randrange(24):02d}:{rnd.choice(['00', '30'])}" if rnd.random() < 0.7 else None,
                            "rush": rnd.random() < 0.3, "labels": rnd.sample(["A", "B", "Ünder"], rnd.randrange(3)),
                            "owner": rnd.choice([owner.id, rep.id, None]), "note": rnd.choice(["", "call first", "Late payer", "ÉCOLE", None]),
                            "address": rnd.choice([None, "2 High St\nFremantle", "PO Box 5"])})
    for i, data in enumerate(UNTIDY):  # written as they are, not through the engine's cleaning
        db.session.add(Record(workspace_id=ws.id, table="orders", data={"reference": f"U-{i:02d}", **data}, created_by="outside"))
    db.session.commit()
    return ws.id, owner.id, rep.id


def _reference(e, table, filters=None, q=None, sort=None):
    """What Engine.query gave before SQL: the whole table read, then matched, searched and sorted in Python."""
    rows = e.visible_rows(table)
    rows = [r for r in rows if e.matches(r, filters)] if filters else list(rows)
    return e.sort(e.search(rows, q), sort)


TODAY_OPS = ["before_today", "after_today", "today", "this_week", "last_month", "this_year", "this_financial_year"]
QUERIES = [([{"field": "status", "op": op, "value": v}], None, None) for op, v in
           [("equals", "Paid"), ("equals", "paid "), ("not_equals", "Paid"), ("equals", ""), ("equals", None), ("one_of", ["Paid", "Sent"]),
            ("is_empty", None), ("not_empty", None), ("contains", "aid"), ("contains", "é"), ("greater_than", "D"), ("at_most", "Paid")]] + \
    [([{"field": "amount", "op": op, "value": v}], None, None) for op, v in
     [("equals", 120), ("equals", "120"), ("equals", "abc"), ("not_equals", 0), ("greater_than", 99), ("at_least", "100"), ("less_than", 50),
      ("at_most", True), ("is_empty", None), ("not_empty", None), ("one_of", [15, 1000])]] + \
    [([{"field": "discount", "op": op, "value": v}], None, None) for op, v in
     [("equals", 10), ("greater_than", "5%"), ("less_than", 0.15), ("is_empty", None)]] + \
    [([{"field": "hours", "op": op, "value": v}], None, None) for op, v in [("greater_than", 1), ("equals", 2), ("is_empty", None)]] + \
    [([{"field": "due", "op": op, "value": 7}], None, None) for op in TODAY_OPS + ["within_next_days", "within_last_days", "more_than_days_ago"]] + \
    [([{"field": "due", "op": op, "value": v}], None, None) for op, v in
     [("equals", "2026-11-05"), ("greater_than", "2026-11-01"), ("less_than", "05/11/2026"), ("is_empty", None), ("within_next_days", -1),
      ("within_next_days", 2.5), ("more_than_days_ago", -1.5)]] + \
    [([{"field": "when", "op": op, "value": v}], None, None) for op, v in
     [("equals", "2026-11-05"), ("greater_than", "2026-11-05 09:00"), ("today", None), ("within_last_days", 3), ("is_empty", None)]] + \
    [([{"field": "rush", "op": op, "value": v}], None, None) for op, v in
     [("equals", True), ("equals", False), ("equals", "yes"), ("is_empty", None), ("not_empty", None)]] + \
    [([{"field": "labels", "op": op, "value": v}], None, None) for op, v in
     [("equals", "a"), ("equals", "Ünder"), ("not_equals", "B"), ("is_empty", None), ("equals", "")]] + \
    [([{"field": "owner", "op": op, "value": v}], None, None) for op, v in
     [("equals", 1), ("equals", "Rex Rep"), ("equals", "me"), ("not_equals", "olive owner"), ("is_empty", None), ("equals", "")]] + \
    [([{"field": "customer", "op": op, "value": v}], None, None) for op, v in
     [("equals", "Acme Pty Ltd"), ("equals", "1"), ("equals", 2), ("not_equals", "delta"), ("is_empty", None), ("one_of", ["Delta", "Bolt & Co"])]] + \
    [([{"field": "note", "op": op, "value": v}], None, None) for op, v in
     [("equals", "hello"), ("equals", "12"), ("equals", "école"), ("contains", "LATE"), ("contains", ""), ("is_empty", None), ("greater_than", "a")]] + \
    [([{"field": "doubled", "op": "greater_than", "value": 200}], None, None),  # a formula: Python, on what SQL found
     ([{"field": "customer_email", "op": "contains", "value": "c1"}], None, None),
     ([{"field": "amount", "op": "greater_than", "value": "{{record.hours}}"}], None, None),
     ([{"field": "customer.region", "op": "equals", "value": "North"}], None, None),
     ([{"any": [{"field": "status", "op": "equals", "value": "Paid"}, {"field": "rush", "op": "equals", "value": True}]}], None, None),
     ([{"all": [{"field": "amount", "op": "at_least", "value": 50}, {"field": "status", "op": "not_equals", "value": "Draft"}]}], None, None),
     ([{"any": [{"field": "status", "op": "equals", "value": "Paid"}, {"field": "doubled", "op": "less_than", "value": 50}]}], None, None),
     ([{"field": "status", "op": "equals", "value": "Paid"}, {"field": "amount", "op": "greater_than", "value": 50}], None, None)] + \
    [(None, q, None) for q in ["acme", "ACME", "late", "é", "école", "c1@", "rex", "former", "a, b", "perth", "main st", "12", "zzz"]] + \
    [(None, None, {"field": k, "dir": d}) for k in ["status", "amount", "discount", "hours", "due", "when", "rush", "owner", "note",
                                                       "reference", "created_at", "customer", "doubled", "labels"] for d in ("asc", "desc")] + \
    [([{"field": "status", "op": "equals", "value": "Paid"}], "acme", {"field": "amount", "dir": "desc"}),
     ([{"field": "amount", "op": "greater_than", "value": 10}], None, {"field": "due", "dir": "asc"})]


def _check_all(member=None):
    """Every query, through the planner and the old way, in the database that's open; returns what the planner found."""
    ws = db.session.get(Workspace, 1)
    who = Membership.query.filter_by(workspace_id=ws.id, user_id=member).first() if member else None
    found = []
    for filters, q, sort in QUERIES:
        expected = [r.id for r in _reference(Engine(ws, by="x", member=who), "orders", filters, q, sort)]
        got = [r.id for r in Engine(ws, by="x", member=who).query("orders", filters, q=q, sort=sort)]
        assert got == expected, (filters, q, sort)
        rows, total = Engine(ws, by="x", member=who).page("orders", filters, q=q, sort=sort, offset=3, limit=7)
        assert ([r.id for r in rows], total) == (expected[3:10], len(expected)), ("page", filters, q, sort)
        assert Engine(ws, by="x", member=who).count("orders", filters, q=q) == len(expected), ("count", filters, q)
        if sort is None:
            for aggregate in ("count", "sum", "avg", "min", "max", "median"):
                e = Engine(ws, by="x", member=who)
                want = e.aggregate(_reference(e, "orders", filters, q), aggregate, "amount")
                assert Engine(ws, by="x", member=who).total("orders", aggregate, "amount", filters, q=q) == want, (aggregate, filters, q)
                paged = Engine(ws, by="x", member=who)  # a list page's totals, added up as its records were counted
                paged.page("orders", filters, q=q, limit=5, sums=["amount", "discount"])
                assert paged.total("orders", aggregate, "amount", filters, q=q) == want, ("paged", aggregate, filters, q)
            first, n = Engine(ws, by="x", member=who).find("orders", q or "a", limit=5)
            e = Engine(ws, by="x", member=who)
            everything = e.search(e.visible_rows("orders"), q or "a")
            assert ([r.id for r in first], n) == ([r.id for r in everything[:5]], len(everything))
        found.append(got)
    e = Engine(ws, by="x", member=who)
    order = [r.id for r in e.visible_rows("orders")]
    for rid in order[:3] + order[-3:] + [order[len(order) // 2]]:
        e = Engine(ws, by="x", member=who)
        at = order.index(rid)
        assert e.neighbours("orders", e.get(rid)) == {"prev": order[at - 1] if at > 0 else None,
                                                      "next": order[at + 1] if at < len(order) - 1 else None, "at": at + 1, "of": len(order)}
    whole, shown = Engine(ws, by="x", member=who), Engine(ws, by="x", member=who)
    whole.rows("orders")  # the old way: the line table read whole
    page, _ = shown.page("customers", limit=4)
    for c in page:  # a page's totals: the orders of just the customers shown, fetched together
        assert shown.value(c, "ordered") == whole.value(whole.get(c.id), "ordered")
        assert shown.value(c, "order_count") == whole.value(whole.get(c.id), "order_count")
    return found


def _tidy_is_sql_alone():
    """Records saved through the engine are settled by SQL alone: the page and the count come straight from it. (SQLite
    folds only A-Z, so there, names like “café crème” are left for Python.)"""
    ws = db.session.get(Workspace, 1)
    e = Engine(ws, by="x")
    cases = [([{"field": "vip", "op": "equals", "value": True}], {"field": "email", "dir": "desc"}),
             ([{"field": "email", "op": "contains", "value": "C1"}], {"field": "email", "dir": "asc"}),
             ([{"field": "vip", "op": "is_empty"}, {"field": "email", "op": "greater_than", "value": "c2"}], {"field": "vip", "dir": "desc"})]
    if db.engine.dialect.name == "postgresql":
        cases.append(([{"field": "region", "op": "equals", "value": "café"}], {"field": "name", "dir": "desc"}))
    for filters, sort in cases:
        total, whole, ordered = Plan(e, "customers", filters, sort=sort).exact()
        assert whole and ordered, filters
    assert "customers" not in e._rows and "orders" not in e._rows  # nothing was read whole


def _duplicates_are_refused():
    ws = db.session.get(Workspace, 1)
    e = Engine(ws, by="x")
    with pytest.raises(FieldErrors) as refused:
        e.create("orders", {"reference": "r-005 "})
    assert "already has this Reference" in refused.value.errors["reference"]
    assert e.create("orders", {"reference": "R-999"}).id


def test_the_planner_gives_what_python_gives_on_postgresql_and_the_simulator(app, tmp_path):
    _fill()
    on_postgres = {"owner": _check_all(), "rep": _check_all(member=2)}
    _tidy_is_sql_alone()
    _duplicates_are_refused()
    db.session.remove()

    simulator = create_app({**{k: v for k, v in app.config.items() if k.isupper()}, **sim.overrides(), "DATA_DIR": tmp_path / "sim"})
    with simulator.app_context():
        assert db.engine.dialect.name == "sqlite"
        _fill()
        on_sqlite = {"owner": _check_all(), "rep": _check_all(member=2)}
        _tidy_is_sql_alone()
        _duplicates_are_refused()
        db.session.remove()
    # created_at differs by database (the moment each was written), so its order is compared within each, above
    same = [i for i, (_, _, sort) in enumerate(QUERIES) if not sort or sort["field"] != "created_at"]
    for who in ("owner", "rep"):
        assert [on_postgres[who][i] for i in same] == [on_sqlite[who][i] for i in same]


def test_an_import_commits_in_batches_with_the_same_results(app, owner):
    """Rows go in a lot at a time; a row that can't go in is left out whole (its new customer too), as row by row."""
    from app.engine.records import Engine
    _, ws = owner
    ws.blueprint = _people_and_design(ws.blueprint)
    db.session.commit()
    e = Engine(ws, by="import")
    seen, done = [], []
    with e.batch(size=3, committed=lambda: done.append(Record.query.filter_by(table="orders").count())):
        for i, (ref, customer) in enumerate([("A-1", "New One"), ("A-2", "New One"), ("A-1", "Another"), ("A-3", "Third"), ("A-4", None)]):
            try:
                with e.atomic():
                    data = {"reference": ref}
                    if customer:
                        found = e._find_linked("customers", customer)
                        data["customer"] = found.id if found else e.create("customers", {"name": customer}).id
                    seen.append(e.create("orders", data).id)
            except FieldErrors as exc:
                seen.append(exc.errors)
    assert done == [3]  # the first lot of three saved rows went in together; the last two with the end of the batch
    assert isinstance(seen[2], dict) and "reference" in seen[2]
    names = sorted(r.data["name"] for r in Record.query.filter_by(table="customers", deleted_at=None))
    assert names == ["New One", "Third"]  # "Another" came with the refused row, and went with it
    assert sorted(r.data["reference"] for r in Record.query.filter_by(table="orders")) == ["A-1", "A-2", "A-3", "A-4"]
