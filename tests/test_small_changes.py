"""Small changes in the Keel language: one setting of a field, one stage, one move, one step of a rule. The AI, the App
map and outside agents change an app the same way, one piece at a time, and nothing else about it moves."""
from app import lang
from app.blueprint import schema
from app.lang import model as m

from .test_lang import everything


def _apply(bp, *patches):
    out = lang.apply(bp, list(patches))
    assert not out.problems, out.problems
    return out.blueprint


def _field(bp, table, key):
    return schema.field(schema.table(bp, table), key)


def test_a_fields_settings_change_one_at_a_time_and_null_takes_one_away():
    bp = _apply(everything(), {"do": "set_field", "list": "customers", "field": "name", "settings": {"help": "As on their ABN", "placeholder": "Acme Pty Ltd"}})
    f = _field(bp, "customers", "name")
    assert f["help"] == "As on their ABN" and f["placeholder"] == "Acme Pty Ltd" and f["required"]
    bp = _apply(bp, {"do": "set_field", "list": "customers", "field": "name", "settings": {"required": False, "help": None}})
    f = _field(bp, "customers", "name")
    assert not f.get("required") and not f.get("help") and f["placeholder"] == "Acme Pty Ltd"
    bad = lang.apply(bp, [{"do": "set_field", "list": "customers", "field": "name", "settings": {"colour": "red"}}])
    assert bad.problems and "isn't a setting" in bad.problems[0]


def test_stages_reordered_with_a_start_and_one_taken_out_closes_the_chain():
    bp = _apply(everything(), {"do": "order_stages", "list": "jobs", "stages": ["Booked", "In progress", "Done"], "start": "In progress"})
    assert schema.table(bp, "jobs")["workflow"]["start"] == "In progress"
    bp = _apply(everything(), {"do": "remove_stage", "list": "jobs", "stage": "In progress"})
    flow = schema.table(bp, "jobs")["workflow"]
    assert _field(bp, "jobs", "status")["options"] == ["Booked", "Done"]
    assert any(mv["from"] == ["Booked"] and mv["to"] == "Done" for mv in flow["transitions"]), flow


def test_a_move_is_added_changed_and_taken_away_by_its_button():
    bp = _apply(everything(), {"do": "add_move", "list": "quotes", "move": {"from": ["Sent"], "to": "Draft", "button": "Back to draft",
                                                                          "only_if": ["total < 1000"], "not_by": "creator"}})
    back = next(mv for mv in schema.table(bp, "quotes")["workflow"]["transitions"] if mv["label"] == "Back to draft")
    assert back["only_if"] == [{"formula": "total < 1000"}] and back["not_by"] == ["creator"]
    bp = _apply(bp, {"do": "change_move", "list": "quotes", "button": "Send", "move": {"from": ["Draft"], "to": "Sent", "button": "Send it", "who": ["owner"]}})
    labels = [mv["label"] for mv in schema.table(bp, "quotes")["workflow"]["transitions"]]
    assert "Send it" in labels and "Send" not in labels
    bp = _apply(bp, {"do": "remove_move", "list": "quotes", "button": "Back to draft"})
    assert "Back to draft" not in [mv["label"] for mv in schema.table(bp, "quotes")["workflow"]["transitions"]]


def test_a_lists_stages_come_off():
    bp = _apply(everything(), {"do": "remove_stages", "list": "orders"})
    assert not schema.table(bp, "orders").get("workflow")


def test_one_step_of_a_rule_is_added_changed_moved_and_taken_away_and_a_rule_switched_off():
    bp = _apply(everything(), m.AddRule.__keel_example__)
    rule = lambda b: next(a for a in b["automations"] if a["key"] == "job_done_invoice")  # noqa: E731
    bp = _apply(bp, {"do": "add_step", "rule": "job_done_invoice", "step": {"do": "wait", "days": 2}, "at": 1})
    assert [s["type"] for s in rule(bp)["actions"]] == ["wait", "create_record", "notify"]
    bp = _apply(bp, {"do": "move_step", "rule": "job_done_invoice", "at": 1, "to": 3})
    assert [s["type"] for s in rule(bp)["actions"]] == ["create_record", "notify", "wait"]
    bp = _apply(bp, {"do": "change_step", "rule": "job_done_invoice", "at": 3, "step": {"do": "wait", "days": 5}})
    assert rule(bp)["actions"][2]["days"] == 5
    bp = _apply(bp, {"do": "remove_step", "rule": "job_done_invoice", "at": 3})
    assert [s["type"] for s in rule(bp)["actions"]] == ["create_record", "notify"]
    bp = _apply(bp, {"do": "change_rule", "rule": "job_done_invoice", "enabled": False})
    assert rule(bp)["enabled"] is False


def test_a_lists_title_field_and_section_and_lines_only_some():
    bp = _apply(everything(), {"do": "change_list", "list": "jobs", "title_field": "title", "module": "Work"})
    jobs = schema.table(bp, "jobs")
    assert jobs["title_field"] == "title"
    bp = _apply(bp, {"do": "add_field", "list": "job_lines", "field": {"type": "yes_no", "key": "billable", "label": "Billable"}},
                {"do": "add_rule", "rule": {"key": "invoice_lines", "name": "Invoice the billable lines", "when": {"on": "reaches", "list": "jobs", "stage": "Done"},
                                            "steps": [{"do": "add", "list": "invoices", "values": [{"field": "job", "formula": "record"}]},
                                                      {"do": "add_each", "list": "invoice_lines", "lines": "job_lines", "link": "job",
                                                       "values": [{"field": "invoice", "formula": "created"}], "lines_where": [{"field": "billable", "op": "equals", "value": True}]}]}})
    each = next(a for a in bp["automations"] if a["key"] == "invoice_lines")["actions"][1]
    assert each["where"] == [{"field": "billable", "op": "equals", "value": True}]


def test_a_new_list_is_named_and_shown_by_its_fields_links_included():
    """A stock movement (item, location, quantity) is named by its item, not its quantity, and its list shows them all:
    chosen once its links are made too, not before."""
    out = lang.apply(everything(), [
        {"do": "add_list", "key": "locations", "label": "Locations", "singular": "Location", "fields": [{"type": "text", "key": "name", "label": "Name"}]},
        {"do": "add_list", "key": "movements", "label": "Stock movements", "singular": "Stock movement", "fields": [
            {"type": "link", "key": "item", "label": "Item", "to": "services"}, {"type": "link", "key": "location", "label": "Location", "to": "locations"},
            {"type": "number", "key": "change", "label": "Change"}]}])
    assert not out.problems, out.problems
    t = schema.table(out.blueprint, "movements")
    assert t["title_field"] == "item"
    assert next(v for v in t["views"] if v["key"] == "all")["columns"][:3] == ["item", "location", "change"]


def test_how_a_features_field_looks_is_anyones_and_stays_when_the_feature_is_made_again():
    bp = _apply(everything(), m.ChangeRule.__keel_example__ if False else {"do": "line_totals", "list": "invoices", "lines": "invoice_lines", "tax_percent": 10, "tax_label": "GST"})
    total = next(f for f in schema.table(bp, "invoices")["fields"] if f.get("feature") and f["key"] == "total")
    bp = _apply(bp, {"do": "set_field", "list": "invoices", "field": total["key"], "settings": {"label": "Amount due", "help": "With GST"}})
    bp = _apply(bp, {"do": "line_totals", "list": "invoices", "lines": "invoice_lines", "tax_percent": 15, "tax_label": "GST"})
    again = schema.field(schema.table(bp, "invoices"), total["key"])
    assert again["label"] == "Amount due" and again["help"] == "With GST"
    refused = lang.apply(bp, [{"do": "set_field", "list": "invoices", "field": total["key"], "settings": {"required": True}}])
    assert refused.problems and "change the feature" in refused.problems[0]


def test_taking_away_the_only_way_to_a_stage_says_so_rather_than_coming_back():
    out = lang.apply(everything(), [{"do": "remove_move", "list": "quotes", "button": "Accepted"}])
    assert out.problems and "only way to Accepted" in out.problems[0]


def test_a_new_lists_field_shown_by_a_formula_or_a_group_is_made_not_a_crash():
    out = lang.apply(everything(), [{"do": "add_list", "key": "animals", "label": "Animals", "singular": "Animal", "fields": [
        {"type": "text", "key": "tag", "label": "Tag"}, {"type": "yes_no", "key": "sick", "label": "Sick"},
        {"type": "text", "key": "treatment", "label": "Treatment", "show_if": ["sick == True"]},
        {"type": "text", "key": "vet", "label": "Vet", "show_if": [{"any": [{"field": "sick", "op": "equals", "value": True}, "tag != ''"]}]}]}])
    assert not out.problems, out.problems
    fields = {f["key"]: f for f in schema.table(out.blueprint, "animals")["fields"]}
    assert fields["treatment"]["show_if"] == [{"formula": "sick == True"}] and "any" in fields["vet"]["show_if"][0]


def test_a_rule_on_a_schedule_that_checks_records_is_told_to_name_its_list():
    out = lang.apply(everything(), [{"do": "add_rule", "rule": {"key": "due", "name": "Due soon", "when": {"on": "every", "period": "day"},
                                                                "only_if": [{"field": "due_date", "op": "within_next_days", "value": 7}],
                                                                "steps": [{"do": "notify", "who": "owner", "message": "Due soon"}]}}])
    assert out.problems and "give its when the list" in out.problems[0]
