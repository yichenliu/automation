"""What the builder writes, read as it means it, where only one meaning is possible (round 39): a formula where a
condition's field goes, a patch sent inside a design_op, a JS-style `? :`, a change to a feature's rule, a rule or a
feature taken away by the other's name, a stage added where it already is. And make before break: a fix that takes
rules away to put another in never leaves the app with neither."""
from app import lang
from app.blueprint import expr, schema
from app.extensions import db
from app.models import Message

from .test_lang import everything


def _apply(bp, *patches):
    out = lang.apply(bp, list(patches))
    assert not out.problems, out.problems
    return out.blueprint


def _rule(bp, key):
    return next(a for a in bp["automations"] if a["key"] == key)


def test_a_formula_where_a_conditions_field_goes_is_that_formula():
    bp = _apply(everything(), {"do": "add_rule", "rule": {"key": "late", "name": "Late invoices", "when": {"on": "every", "list": "invoices"},
                                                         "only_if": [{"field": "days_since(due_date)", "op": "greater_than", "value": 3},
                                                                     {"field": "count(payments)", "op": "equals", "value": 0}],
                                                         "steps": [{"do": "notify", "who": "owner", "message": "Late"}]}})
    assert _rule(bp, "late")["conditions"] == [{"formula": "(days_since(due_date)) > 3"}, {"formula": "(count(payments)) == 0"}]
    assert schema.normalize_condition({"field": "total - paid", "op": ">=", "value": 100}) == {"formula": "(total - paid) >= 100"}
    assert schema.normalize_condition({"field": "due_date", "op": "is_empty"}) == {"field": "due_date", "op": "is_empty"}


def test_a_patch_sent_inside_a_design_op_is_that_patch():
    bp = _apply(everything(), {"do": "design_op", "op": {"op": "add_view", "list": "jobs", "view": {"type": "calendar", "label": "Job calendar",
                                                                                                 "date_field": "scheduled_for"}}})
    assert any(v["label"] == "Job calendar" for v in schema.table(bp, "jobs")["views"])
    refused = lang.apply(everything(), [{"do": "design_op", "op": {"op": "add_list", "nonsense": True}}])
    assert refused.problems and "add_list" in refused.problems[0]


def test_a_question_mark_choice_reads_as_if():
    assert expr.normalise("status == 'Late' ? total * 1.1 : total") == "iif(status == 'Late', total * 1.1, total)"
    assert expr.evaluate("a > 1 ? 'big' : a > 0 ? 'small' : 'none'", expr.Context({"a": 0.5})) == "small"
    assert expr.normalise("note == 'why?'") == "note == 'why?'"


def test_a_features_rule_is_changed_and_is_then_the_writers_own():
    bp = _apply(everything(), {"do": "chase", "list": "invoices", "stage": "Sent", "days": 14, "email_to": "customer.email",
                               "message": "A friendly reminder: invoice {{number}} is still unpaid."})
    made = next(a for a in bp["automations"] if a.get("feature"))
    bp = _apply(bp, {"do": "change_rule", "rule": made["key"], "name": "Chase after ten days"})
    changed = _rule(bp, made["key"])
    assert changed["name"] == "Chase after ten days" and not changed.get("feature")
    entry = next(e for e in bp["features"] if e["key"] == made["feature"])
    assert made["key"] in entry["given_up"]
    again = _apply(bp, {"do": "chase", "list": "invoices", "stage": "Sent", "days": 7, "email_to": "customer.email",
                        "message": "Still unpaid", "key": made["feature"]})
    assert _rule(again, made["key"])["name"] == "Chase after ten days"  # made again, the feature leaves it be


def test_a_one_rule_feature_taken_away_by_its_rules_name_goes_and_names_mixed_up_still_work():
    bp = _apply(everything(), {"do": "chase", "list": "invoices", "stage": "Sent", "days": 14, "email_to": "customer.email",
                               "message": "A friendly reminder: invoice {{number}} is still unpaid."})
    made = next(a for a in bp["automations"] if a.get("feature"))
    gone = _apply(bp, {"do": "remove_rule", "rule": made["key"]})
    assert not gone.get("features") and not any(a["key"] == made["key"] for a in gone["automations"])
    bp = _apply(everything(), {"do": "overdue", "list": "invoices", "date_field": "due_date", "from_stages": ["Sent"], "to_stage": "Overdue"})
    made = next(a for a in bp["automations"] if a.get("feature"))  # a feature that does more (its stage's moves): it stays
    gone = _apply(bp, {"do": "remove_rule", "rule": made["key"]})
    assert not any(a["key"] == made["key"] for a in gone["automations"]) and made["key"] in gone["features"][0]["given_up"]
    bp = _apply(everything(), {"do": "add_rule", "rule": {"key": "nudge", "name": "Nudge", "when": {"on": "added", "list": "jobs"},
                                                         "steps": [{"do": "notify", "who": "owner", "message": "New job"}]}})
    assert not any(a["key"] == "nudge" for a in _apply(bp, {"do": "remove_feature", "feature": "nudge"})["automations"])


def test_a_stage_added_where_it_already_is_is_nothing_to_do():
    stages = schema.field(schema.table(everything(), "jobs"), "status")["options"]
    out = lang.apply(everything(), [{"do": "add_stage", "list": "jobs", "stage": stages[1], "after": stages[0]}])
    assert not out.problems and schema.field(schema.table(out.blueprint, "jobs"), "status")["options"] == stages


def test_a_fix_that_would_replace_rules_keeps_them_until_the_replacement_is_made(app, owner, monkeypatch):
    """The dance studio (round 39): asked to make access blocking automatic, the builder took every rule away and sent
    one new rule the platform refused; the app was left with none, the refund alert too. Now what it takes away waits
    for what replaces it."""
    from app.builder import build
    _, ws = owner
    ws.blueprint = _apply(everything(), {"do": "add_rule", "rule": {"key": "refund_alert", "name": "Tell me about big refunds",
                                                                    "when": {"on": "added", "list": "payments"},
                                                                    "steps": [{"do": "notify", "who": "owner", "message": "A refund"}]}})
    db.session.commit()
    message = Message(workspace_id=ws.id, role="assistant", text="")
    db.session.add(message)
    db.session.commit()
    turn = build.Turn(message, ws, None)
    monkeypatch.setattr(build, "_ask", lambda *a, **kw: {"work": []})  # the correction sends nothing better
    bad = {"do": "add_rule", "rule": {"key": "block", "name": "Block", "when": {"on": "every", "list": "members"},
                                      "only_if": [{"field": "no_such_field", "op": "equals", "value": 1}],
                                      "steps": [{"do": "notify", "who": "owner", "message": "Blocked"}]}}
    turn.apply([{"do": "remove_rule", "rule": "refund_alert"}, bad], "behaviour", "1. Block access", owners={0: 1, 1: 1})
    assert any(a["key"] == "refund_alert" for a in turn.bp["automations"])
    good = {**bad, "rule": {**bad["rule"], "only_if": [{"field": "status", "op": "is_empty"}]}}
    turn.apply([{"do": "remove_rule", "rule": "refund_alert"}, good], "behaviour", "1. Block access", owners={0: 1, 1: 1})
    keys = [a["key"] for a in turn.bp["automations"]]
    assert "block" in keys and "refund_alert" not in keys  # made: then what it replaces goes
