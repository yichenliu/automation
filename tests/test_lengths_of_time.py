"""Lengths of time in the formula language, as people and the AI write them: "returned more than 2 hours late",
"not paid 48 hours after booking". A plain number with a date is days, as in a spreadsheet; 2h, 48 hours, 30m, 3 days
are lengths of time. Every function is in the catalog the AI reads, and the design checks catch a date that moves on
every save and a rule that says hours but counts days."""
from datetime import date, datetime, timedelta

import pytest

from app import lang
from app.blueprint import expr, schema
from app.engine.records import Engine
from app.lang import registry

from .test_queries import _workspace

CTX = dict(names={"due": datetime(2026, 11, 6, 10, 0), "back": datetime(2026, 11, 6, 13, 0), "day": date(2026, 11, 6),
                  "booked": datetime(2026, 11, 3, 8, 0), "note": "back in 2h"},
           today=date(2026, 11, 7), now=datetime(2026, 11, 7, 9, 0))


def _x(formula):
    return expr.evaluate(formula, expr.Context(**CTX))


@pytest.mark.parametrize("formula, expected", [
    ("back > due + 2h", True),              # three hours late is more than two
    ("back > due + 4 hours", False),
    ("back > due + 2", False),              # a plain number is days
    ("back - due > 2h", True),              # two times apart, compared with a length
    ("back - due", 0),                      # still whole days, as a date take a date has always been
    ("booked < now() - 48h", True),
    ("booked < now() - 72 hours", True),
    ("booked < now() - 5 days", False),
    ("hours_since(booked) > 48", True),
    ("hours_until(due)", -23.0),
    ("day + 2w", date(2026, 11, 20)),
    ("day + 3h", datetime(2026, 11, 6, 3, 0)),  # hours on a day: from its start
    ("due + 30m", datetime(2026, 11, 6, 10, 30)),
    ("due + hours(1.5)", datetime(2026, 11, 6, 11, 30)),
    ("2 * 1h + due", datetime(2026, 11, 6, 12, 0)),
    ("day - 3 days", date(2026, 11, 3)),
    ("note", "back in 2h"),                 # in quotes it's words
    ("concat('in ', '2h')", "in 2h"),
])
def test_lengths_of_time_read_as_people_write_them(formula, expected):
    assert _x(formula) == expected


def test_a_length_where_a_number_belongs_says_so_in_the_formulas_words():
    with pytest.raises(expr.ExprError) as said:
        _x("2h + 5")
    assert "length of time" in str(said.value)


def test_every_function_is_in_the_catalog_the_ai_reads():
    known = set(expr.FUNCTIONS) | expr.QUERIES | expr.TODAY_FUNCTIONS | expr.NOW_FUNCTIONS | set(expr.RECORD_FUNCTIONS) | {"today", "now"}
    assert not known - set(expr.SAID), known - set(expr.SAID)
    said = registry.catalog()
    assert "FORMULAS" in said and "hours_since(" in said and "48 hours" in said
    for name in known:
        assert name in said, name


def test_a_condition_whose_value_is_worked_out_is_a_formula():
    out = lang.apply(schema.empty("Park"), [
        {"do": "add_list", "key": "bookings", "label": "Bookings", "singular": "Booking", "fields": [
            {"type": "text", "key": "guest", "label": "Guest"}, {"type": "datetime", "key": "booked_at", "label": "Booked at"},
            {"type": "yes_no", "key": "chased", "label": "Chased"}]},
        {"do": "add_rule", "rule": {"key": "chase", "name": "Chase after 48 hours", "when": {"on": "every", "period": "day", "list": "bookings"},
                                    "only_if": [{"field": "booked_at", "op": "less_than", "value": "now() - 48h"}],
                                    "steps": [{"do": "set", "values": [{"field": "chased", "to": True}]}]}}])
    assert not out.problems, out.problems
    rule = next(a for a in out.blueprint["automations"] if a["key"] == "chase")
    assert rule["conditions"] == [{"field": "booked_at", "op": "less_than", "value": "{{now() - 48h}}"}]
    assert schema.normalize_condition({"field": "booked_at", "op": "<", "value": "today() - 2"})["value"] == "{{today() - 2}}"
    assert schema.normalize_condition({"field": "booked_at", "op": "<", "value": "{{today() - 2}}"})["value"] == "{{today() - 2}}"
    refused = lang.apply(out.blueprint, [{"do": "change_rule", "rule": "chase", "only_if": [{"field": "booked_at", "op": "less_than", "value": "now() - 48 parsecs"}]}])
    assert refused.problems


def test_a_design_saved_with_a_worked_out_value_is_read_so(app):
    """Apps saved before such a value was read as a formula: the engine and the database planner read it so too."""
    out = lang.apply(schema.empty("Park"), [{"do": "add_list", "key": "bookings", "label": "Bookings", "singular": "Booking", "fields": [
        {"type": "text", "key": "guest", "label": "Guest"}, {"type": "datetime", "key": "booked_at", "label": "Booked at"}]}])
    e = Engine(_workspace(out.blueprint), by="Olive")
    old = e.create("bookings", {"guest": "Ann", "booked_at": (datetime.now() - timedelta(hours=60)).isoformat(sep=" ", timespec="minutes")})
    new = e.create("bookings", {"guest": "Bo", "booked_at": (datetime.now() - timedelta(hours=10)).isoformat(sep=" ", timespec="minutes")})
    saved = [{"field": "booked_at", "op": "less_than", "value": "now() - 48h"}]
    assert e.matches(e.get(old.id), saved) and not e.matches(e.get(new.id), saved)


def _checks(extra):
    from app.builder import checks
    from app.models import Workspace
    from .test_queries import _app
    ws = Workspace.query.first()
    if ws is None:
        ws = _workspace(_app(extra))
    else:
        ws.blueprint = _app(extra)
    e = Engine(ws, by="Olive")
    return checks.restamped(e), checks.days_for_hours(e)


def test_a_date_set_from_today_at_every_save_is_found(app):
    restamped, _ = _checks([{"do": "add_rule", "rule": {"key": "due", "name": "Set the due date", "when": {"on": "added_or_changed", "list": "invoices"},
                                                        "steps": [{"do": "set", "values": [{"field": "due_date", "formula": "today() + 7"}]}]}}])
    assert [i["level"] for i in restamped] == ["fix"] and "every time an invoice is saved" in restamped[0]["plain"]
    for meant in ({"on": "added", "list": "invoices"}, {"on": "changed", "list": "invoices", "field": "status", "to": "Paid"}):
        restamped, _ = _checks([{"do": "add_rule", "rule": {"key": "due", "name": "Set the due date", "when": meant,
                                                            "steps": [{"do": "set", "values": [{"field": "due_date", "formula": "today() + 7"}]}]}}])
        assert not restamped, meant
    restamped, _ = _checks([{"do": "add_rule", "rule": {"key": "due", "name": "Set the due date", "when": {"on": "added_or_changed", "list": "invoices"},
                                                        "only_if": [{"field": "due_date", "op": "is_empty"}],
                                                        "steps": [{"do": "set", "values": [{"field": "due_date", "formula": "today() + 7"}]}]}}])
    assert not restamped  # only while it's empty: set once


def test_a_rule_that_says_hours_but_counts_days_is_found(app):
    rule = {"key": "late", "name": "Flag bookings over two hours late", "when": {"on": "added_or_changed", "list": "bookings"},
            "only_if": ["ends_at > starts_at + 2"], "steps": [{"do": "notify", "who": "owner", "message": "Late"}]}
    _, hours = _checks([{"do": "add_rule", "rule": rule}])
    assert len(hours) == 1 and "starts_at + 2h" in hours[0]["text"]
    _, hours = _checks([{"do": "add_rule", "rule": {**rule, "only_if": ["ends_at > starts_at + 2h"]}}])
    assert not hours
