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


# --- round 40 ------------------------------------------------------------------------------------------------------

def test_a_formula_that_works_out_a_date_is_a_date_whatever_it_was_left_showing():
    """confirmed_at + 2h left at "shows": "number" (the concrete pumper): a date, so the overdue feature can count from
    it and the stages can still change."""
    bp = _apply(everything(), {"do": "add_field", "list": "jobs", "field": {"type": "datetime", "key": "confirmed_at", "label": "Confirmed at"}},
                {"do": "add_field", "list": "jobs", "field": {"type": "formula", "key": "confirm_by", "label": "Confirm by", "formula": "confirmed_at + 2h"}})
    assert schema.field(schema.table(bp, "jobs"), "confirm_by")["format"] == "date"
    old = {"key": "due_by", "label": "Due by", "type": "formula", "expression": "confirmed_at + 2h", "format": "number"}  # saved before
    schema.table(bp, "jobs")["fields"].append(old)
    from app.lang.design import Design
    assert Design(bp).kind(old) == "date"
    assert lang.apply(bp, [{"do": "chase", "list": "jobs", "stage": schema.field(schema.table(bp, "jobs"), "status")["options"][0],
                            "days": 1, "since": "due_by"}]).problems == []


def test_a_rule_about_clients_reads_client_as_the_record_and_owner_as_the_owner():
    bp = _apply(everything(), {"do": "add_field", "list": "jobs", "field": {"type": "person", "key": "approved_by", "label": "Approved by"}},
                {"do": "add_rule", "rule": {"key": "hello", "name": "Say hello", "when": {"on": "added", "list": "customers"},
                                            "steps": [{"do": "notify", "who": "owner", "message": "New: {{customer.name}}"}]}},
                {"do": "add_rule", "rule": {"key": "ok", "name": "Mark approved", "when": {"on": "added", "list": "jobs"},
                                            "steps": [{"do": "set", "values": [{"field": "approved_by", "to": "owner"}]}]}})
    assert _rule(bp, "ok")["actions"][0]["values"]["approved_by"] == "{{owner}}"


def test_the_singular_and_owner_work_out_in_the_engine(app, owner):
    from app.engine.records import Engine, RecordContext
    _, ws = owner
    ws.blueprint = everything()
    db.session.commit()
    e = Engine(ws, by="Ethan")
    c = e.create("customers", {"name": "Acme", "email": "a@example.com"})
    ctx = RecordContext(e, e.get(c.id))
    assert expr.evaluate("customer.name", ctx) == "Acme" and expr.evaluate("owner", ctx) == ws.owner_id


def test_a_gate_waits_on_a_worked_out_field_coming_out_right():
    bp = _apply(everything(), {"do": "add_field", "list": "jobs", "field": {"type": "formula", "key": "paid_up", "label": "Paid up",
                                                                           "formula": "count(invoices where status != 'Paid') == 0", "shows": "yes_no"}})
    stages = schema.field(schema.table(bp, "jobs"), "status")["options"]
    out = lang.apply(bp, [{"do": "gate", "list": "jobs", "stage": stages[-1], "needs": ["paid_up"]}])
    assert not out.problems, out.problems


def test_a_feature_taken_away_leaves_the_fields_a_rule_made_from_it_still_reads():
    bp = _apply(everything(), {"do": "chase", "list": "invoices", "stage": "Sent", "days": 14, "email_to": "customer.email",
                               "message": "A friendly reminder: invoice {{number}} is still unpaid."})
    made = next(a for a in bp["automations"] if a.get("feature") and not a["key"].endswith("_stamp"))
    bp = _apply(bp, {"do": "change_rule", "rule": made["key"], "name": "Chase after ten days"})  # now the builder's own
    bp = _apply(bp, {"do": "remove_feature", "feature": made["feature"]})
    assert _rule(bp, made["key"])["name"] == "Chase after ten days"
    assert schema.field(schema.table(bp, "invoices"), "chased_sent") is not None  # it reads it: kept


def test_a_validation_is_taken_away_by_a_part_of_its_check():
    bp = _apply(everything(), {"do": "add_validation", "list": "payments", "check": "amount <= invoice.total", "message": "Too much"})
    bp = _apply(bp, {"do": "remove_validation", "list": "payments", "check": "invoice.total"})
    assert not [r for r in schema.table(bp, "payments").get("rules") or [] if "invoice.total" in r.get("check", "")]


def test_a_list_with_no_stages_is_reminded_some_days_after_a_date():
    bp = _apply(everything(), {"do": "add_field", "list": "customers", "field": {"type": "date", "key": "last_visit", "label": "Last visit"}},
                {"do": "chase", "list": "customers", "since": "last_visit", "days": 182, "message": "Time for a check-up"})
    rule = next(a for a in bp["automations"] if a.get("feature"))
    assert rule["trigger"]["table"] == "customers" and rule["conditions"][0] == {"field": "last_visit", "op": "more_than_days_ago", "value": 181}
    assert "join(" in lang.catalog() and expr.evaluate("join(x, ' / ')", expr.Context({"x": ["a", None, "b"]})) == "a / b"


def test_a_wait_of_no_days_runs_todays_rules_and_moves_no_dates(app, owner):
    """{"do": "wait", "days": 0} was acted out as a day (0 or 1): the hearing clinic's check-up date came out a day
    behind what the owner expected."""
    from app.builder import trial
    _, ws = owner
    ws.blueprint = _apply(everything(), {"do": "add_field", "list": "customers", "field": {"type": "date", "key": "next_visit", "label": "Next visit"}})
    db.session.commit()
    example = {"title": "The next visit stays today", "steps": [
        {"do": "add", "table": "customers", "as": "c1", "values": {"name": "Ann", "email": "a@example.com", "next_visit": "today"}},
        {"do": "wait", "days": 0}], "expect": [{"record": "c1", "field": "next_visit", "equals": "today"}]}
    with trial.copy_of(ws) as copy:
        r = trial.play(copy, example)
    assert r["ok"], r


# --- round 41 ------------------------------------------------------------------------------------------------------

def test_two_features_on_one_stage_keep_each_others_fields_when_the_stages_change():
    """The gutter cleaner: an approval into Done made “Done on”, a chase of Done read it; adding a stage made the
    approval again, it took its own field for someone else's and dropped it, and every stage change after was refused."""
    stages = schema.field(schema.table(everything(), "jobs"), "status")["options"]
    bp = _apply(everything(), {"do": "approval", "list": "jobs", "stage": stages[-1], "by": "owner"},
                {"do": "chase", "list": "jobs", "stage": stages[-1], "days": 7, "email_to": "customer.email"})
    bp = _apply(bp, {"do": "add_stage", "list": "jobs", "stage": "On hold", "after": stages[0]})
    assert schema.field(schema.table(bp, "jobs"), "done_on") is not None


def test_a_percentage_written_out_of_a_hundred_is_kept_as_a_fraction_and_a_repeated_formula_is_refused():
    bp = _apply(everything(), {"do": "add_field", "list": "jobs", "field": {"type": "number", "key": "have", "label": "Have"}},
                {"do": "add_field", "list": "jobs", "field": {"type": "number", "key": "par", "label": "Par"}},
                {"do": "add_field", "list": "jobs", "field": {"type": "formula", "key": "pct", "label": "Stock %",
                                                              "formula": "if(par > 0, have / par * 100, 100)", "shows": "percent"}})
    assert schema.field(schema.table(bp, "jobs"), "pct")["expression"] == "iif(par > 0, have / par, 1)"
    again = lang.apply(bp, [{"do": "add_field", "list": "jobs", "field": {"type": "formula", "key": "pct_2", "label": "Stock % 2",
                                                                          "formula": "iif(par > 0, have / par, 1)", "shows": "percent"}}])
    assert again.problems and "works out exactly this already" in again.problems[0]


def test_the_day_a_record_got_to_a_stage_is_labelled_so_nobody_reads_it_as_still():
    from app.lang.patterns import stamp_label
    assert [stamp_label(s) for s in ("Sent", "Unpaid", "Overdue", "Quoted", "In progress")] == \
        ["Sent on", "Unpaid since", "Overdue since", "Quoted on", "In progress since"]


def test_the_behaviour_step_is_told_it_can_make_a_list_a_need_requires():
    from app.builder import build
    assert "add_list" in build.catalog("behaviour") and "add_list, first in that need's patches" in build.BEHAVIOUR


# --- round 42 ------------------------------------------------------------------------------------------------------

def test_a_date_worked_out_from_an_empty_date_is_left_alone_not_the_rule_stopped(app, owner):
    """The used car dealer: “due back = sent to mechanic + 3” ran before the car was sent; empty and 3 made 3, and the
    rule stopped on “3 isn't a date”, failing four of the app's own checks."""
    from app.engine.records import Engine
    _, ws = owner
    ws.blueprint = _apply(everything(), {"do": "add_field", "list": "jobs", "field": {"type": "date", "key": "sent_on", "label": "Sent on"}},
                          {"do": "add_field", "list": "jobs", "field": {"type": "date", "key": "due_back", "label": "Due back"}},
                          {"do": "add_rule", "rule": {"key": "due", "name": "Set the due back date", "when": {"on": "added_or_changed", "list": "jobs"},
                                                      "steps": [{"do": "set", "values": [{"field": "due_back", "formula": "sent_on + 3"}]}]}})
    db.session.commit()
    e = Engine(ws, by="Ethan")
    c = e.create("customers", {"name": "Acme", "email": "a@example.com"})
    job = e.create("jobs", {"title": "Brakes", "customer": c.id})
    assert e.get(job.id).data.get("due_back") in (None, "")
    e.update(e.get(job.id), {"sent_on": "2026-11-02"})
    assert e.get(job.id).data["due_back"] == "2026-11-05"


def test_a_field_named_in_a_rule_about_no_list_is_said_not_a_crash():
    out = lang.apply(everything(), [{"do": "add_rule", "rule": {"key": "weekly", "name": "Weekly", "when": {"on": "every", "period": "week"},
                                                                "steps": [{"do": "email", "to": "customer.email", "subject": "Hi", "body": "x"}]}}])
    assert out.problems and "about no list" in out.problems[0]


def test_which_builder_steps_think_is_set_step_by_step(app):
    from app.builder import build
    app.config["AI_THINK_STEPS"], app.config["AI_THINK_BUILDER"] = "", False
    assert not build.thinks("understand")
    app.config["AI_THINK_STEPS"] = "understand, fix"
    assert build.thinks("understand") and build.thinks("fix") and not build.thinks("behaviour")
    app.config["AI_THINK_STEPS"] = "all"
    assert build.thinks("behaviour") and build.thinks("owner_check")
    app.config["AI_THINK_STEPS"], app.config["AI_THINK_BUILDER"] = "", True
    assert build.thinks("data")
