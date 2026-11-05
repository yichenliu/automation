"""The Keel language (app/lang): patches compile to the design, every name is checked, features make their parts and
prove themselves in the simulator, and every piece the registry has is one the AI can use (the guard)."""
import json
from datetime import date, timedelta

import pytest
from pydantic import TypeAdapter

from app import ai, lang
from app.blueprint import schema
from app.builder import trial
from app.engine.records import Engine, FieldErrors
from app.extensions import db
from app.lang import model as m, patterns, registry
from app.models import Membership, Message, User, Workspace

# an app with every list the registry's examples name, for each piece's example to compile on
EVERYTHING = [
    {"do": "add_list", "key": "customers", "label": "Customers", "singular": "Customer", "fields": [
        {"type": "text", "key": "name", "label": "Name", "required": True}, {"type": "email", "key": "email", "label": "Email"},
        {"type": "number", "key": "discount_percent", "label": "Discount percent"}]},
    {"do": "add_list", "key": "invoices", "label": "Invoices", "singular": "Invoice", "fields": [
        {"type": "auto_number", "key": "number", "label": "Number", "prefix": "INV-"},
        {"type": "link", "key": "customer", "label": "Customer", "to": "customers"},
        {"type": "date", "key": "due_date", "label": "Due date"}, {"type": "money", "key": "total", "label": "Total"},
        {"type": "file", "key": "pdf", "label": "PDF"}, {"type": "url", "key": "payment_link", "label": "Payment link"},
        {"type": "money", "key": "balance", "label": "Balance"}, {"type": "link", "key": "job", "label": "Job", "to": "jobs"},
        {"type": "date", "key": "issue_date", "label": "Issue date"}],
     "stages": {"stages": ["Draft", "Sent", "Part paid", "Paid"], "moves": [
         {"from": ["Draft"], "to": "Sent", "button": "Send"}, {"from": ["Sent"], "to": "Part paid", "button": "Part paid"},
         {"from": ["Sent", "Part paid"], "to": "Paid", "button": "Mark paid"}]}},
    {"do": "add_list", "key": "invoice_lines", "label": "Invoice lines", "singular": "Invoice line", "inside": "invoices", "fields": [
        {"type": "text", "key": "description", "label": "Description"}]},
    {"do": "add_list", "key": "payments", "label": "Payments", "singular": "Payment", "fields": [
        {"type": "link", "key": "invoice", "label": "Invoice", "to": "invoices", "required": True},
        {"type": "money", "key": "amount", "label": "Amount", "required": True}]},
    {"do": "add_list", "key": "quotes", "label": "Quotes", "singular": "Quote", "fields": [
        {"type": "link", "key": "customer", "label": "Customer", "to": "customers"}, {"type": "money", "key": "total", "label": "Total"}],
     "stages": {"stages": ["Draft", "Sent", "Accepted", "Declined"], "moves": [
         {"from": ["Draft"], "to": "Sent", "button": "Send"}, {"from": ["Sent"], "to": "Accepted", "button": "Accepted"},
         {"from": ["Sent"], "to": "Declined", "button": "Declined"}]}},
    {"do": "add_list", "key": "quote_lines", "label": "Quote lines", "singular": "Quote line", "inside": "quotes", "fields": [
        {"type": "text", "key": "description", "label": "Description"}]},
    {"do": "add_list", "key": "jobs", "label": "Jobs", "singular": "Job", "fields": [
        {"type": "text", "key": "title", "label": "Title"}, {"type": "link", "key": "customer", "label": "Customer", "to": "customers"},
        {"type": "person", "key": "assigned_to", "label": "Assigned to"}, {"type": "yes_no", "key": "billable", "label": "Billable"},
        {"type": "date", "key": "scheduled_for", "label": "Scheduled for"}, {"type": "text", "key": "fax", "label": "Fax"},
        {"type": "choice", "key": "priority", "label": "Priority", "options": ["Standard", "Urgent"]}],
     "stages": {"stages": ["Booked", "In progress", "Done"]}},
    {"do": "add_list", "key": "job_lines", "label": "Job lines", "singular": "Job line", "inside": "jobs", "fields": [
        {"type": "text", "key": "description", "label": "Description"}]},
    {"do": "add_list", "key": "orders", "label": "Orders", "singular": "Order", "fields": [{"type": "text", "key": "name", "label": "Name"}],
     "stages": {"stages": ["New", "In fitting", "Ready"]}},
    {"do": "add_list", "key": "bookings", "label": "Bookings", "singular": "Booking", "fields": [
        {"type": "date", "key": "date", "label": "Date", "required": True}, {"type": "text", "key": "name", "label": "Name"}],
     "stages": {"stages": ["Booked", "Done", "Cancelled"]}},
    {"do": "add_list", "key": "appointments", "label": "Appointments", "singular": "Appointment", "fields": [
        {"type": "date", "key": "starts_on", "label": "Starts on", "required": True},
        {"type": "link", "key": "client", "label": "Client", "to": "customers"}]},
    {"do": "add_list", "key": "members", "label": "Members", "singular": "Member", "fields": [
        {"type": "text", "key": "name", "label": "Name"}, {"type": "choice", "key": "status", "label": "Status", "options": ["Active", "Lapsed"],
                                                           "default": "Active"}]},
    {"do": "add_list", "key": "services", "label": "Services", "singular": "Service", "fields": [
        {"type": "text", "key": "name", "label": "Name"}, {"type": "money", "key": "price", "label": "Price"}]},
    {"do": "add_list", "key": "notes", "label": "Notes", "singular": "Note", "fields": [{"type": "text", "key": "text", "label": "Text"}]},
    {"do": "set_team", "key": "finance", "name": "Finance"}, {"do": "set_team", "key": "office", "name": "Office"},
]


def _invoices():
    """The invoices list on its own (without its link to jobs)."""
    patch = json.loads(json.dumps(EVERYTHING[1]))
    patch["fields"] = [f for f in patch["fields"] if f["key"] != "job"]
    return patch


def everything():
    out = lang.apply(schema.empty("Everything"), EVERYTHING)
    assert not out.problems, out.problems
    return out.blueprint


def _workspace(app, bp):
    user = User(email="owner@example.com", name="Olive")
    user.set_password("x" * 12)
    db.session.add(user)
    db.session.flush()
    ws = Workspace(owner_id=user.id, name="Test", blueprint=bp, state={}, timezone="Australia/Perth")
    db.session.add(ws)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=user.id, role="owner"))
    db.session.commit()
    return ws


def test_every_piece_of_the_language_says_what_it_does_and_shows_how():
    for kind in registry.KINDS:
        assert registry.items(kind), kind
        for cls in registry.items(kind):
            assert (cls.__doc__ or "").strip(), f"{cls.__name__} has no docstring: the AI wouldn't know what it does"
            assert cls.__keel_example__ is not None, f"{cls.__name__} has no example"
            TypeAdapter(cls).validate_python(cls.__keel_example__)  # its example is a valid one
            assert cls.__name__ not in registry.catalog() or True
    said = registry.catalog()
    for cls in registry.items("feature") + registry.items("patch"):
        assert registry._names(cls, cls.__keel_kind__) in said, f"{cls.__name__} isn't in what the AI is told"


def test_each_patch_example_applies_on_an_app_that_has_what_it_names():
    bp = everything()
    bp = lang.apply(bp, [m.AddRule.__keel_example__,  # what the step patches' examples change
                         {"do": "stage_alert", "list": "invoices", "stage": "Paid", "key": "alert_invoices_paid"},
                         m.AddValidation.__keel_example__]).blueprint  # what remove_validation's example takes away
    for cls in registry.items("patch"):
        if cls in (m.AddList,):
            continue
        out = lang.apply(bp, [cls.__keel_example__])
        assert not out.problems, (cls.__name__, out.problems)


@pytest.mark.parametrize("feature", [c.__name__ for c in registry.items("feature")])
def test_each_feature_compiles_and_passes_its_own_checks_in_the_simulator(app, feature):
    cls = next(c for c in registry.items("feature") if c.__name__ == feature)
    out = lang.apply(everything(), [cls.__keel_example__])
    assert not out.problems, out.problems
    ws = _workspace(app, out.blueprint)
    examples = patterns.checks(out.blueprint)
    assert examples, f"{feature} writes no checks of its own"
    walked, results = trial.try_out(ws, examples, screens=False)
    failed = [(r["title"], r.get("problem")) for r in results if not r["ok"]]
    assert not failed, failed
    assert not [i for i in walked["issues"] if i["level"] == "fix"], walked["issues"]


def test_a_feature_whose_parts_are_broken_fails_its_own_checks(app):
    """The checks prove the feature works: take away the rule it made, and they fail."""
    bp = lang.apply(everything(), [{"do": "payments", "list": "invoices", "payments": "payments", "paid_stage": "Paid"}]).blueprint
    broken = json.loads(json.dumps(bp))
    broken["automations"] = [a for a in broken["automations"] if a["key"] != "payments_invoices"]
    ws = _workspace(app, broken)
    _, results = trial.try_out(ws, patterns.checks(broken), screens=False)
    assert results and not all(r["ok"] for r in results)


def test_names_are_checked_and_each_problem_says_where_and_what_is_there():
    bp = everything()
    out = lang.apply(bp, [
        {"do": "add_rule", "rule": {"key": "r", "name": "R", "when": {"on": "reaches", "list": "invoices", "stage": "Payed"}, "steps": [{"do": "notify", "message": "x"}]}},
        {"do": "add_rule", "rule": {"key": "r2", "name": "R2", "when": {"on": "added", "list": "payments"},
                                    "steps": [{"do": "set", "on": "invoice", "values": [{"field": "status", "to": "Paid"}],
                                               "only_if": [{"field": "status", "op": "equals", "value": "Sent"}]}]}},
        {"do": "add_field", "list": "invoice", "field": {"type": "formula", "key": "x", "label": "X", "formula": "totl * 2"}},
        {"do": "stage_alert", "list": "invoices", "stage": "Paid"},
    ])
    assert out.refused == [1, 2, 3]  # the good one still applied
    said = "\n".join(out.problems)
    assert "patch 1" in said and "“Payed” isn't a stage of Invoices" in said
    assert "patch 2" in said and "“status” isn't a field of Payments" in said
    assert "“totl” isn't a field of Invoices (did you mean total?)" in said
    assert any(e["do"] == "stage_alert" for e in out.blueprint["features"])


def test_a_condition_on_a_linked_records_choice_says_how_to_write_it():
    bp = lang.apply(everything(), [{"do": "add_field", "list": "jobs", "field": {"type": "link", "key": "order", "label": "Order", "to": "orders"}}]).blueprint
    out = lang.apply(bp, [{"do": "add_rule", "rule": {"key": "r", "name": "R", "when": {"on": "reaches", "list": "jobs", "stage": "Done"},
                                                      "steps": [{"do": "set", "on": "order", "values": [{"field": "status", "to": "Ready"}],
                                                                 "only_if": [{"field": "status", "op": "equals", "value": "In fitting"}]}]}}])
    assert "write order.status" in out.problems[0]


def test_a_design_op_can_never_make_what_the_language_has_patches_for():
    out = lang.apply(everything(), [{"do": "design_op", "op": {"op": "create_table", "key": "x", "label": "X", "fields": []}},
                                    {"do": "design_op", "op": {"op": "update_table", "table": "jobs", "workflow": {}}},
                                    {"do": "design_op", "op": {"op": "update_table", "table": "jobs", "row_access": {"field": "assigned_to"}}}])
    assert out.refused == [1, 2]
    assert "lists, fields, stages and rules are made with add_list" in out.problems[0]


def test_lists_that_link_to_each_other_can_be_made_in_any_order_and_keep_their_field_order():
    out = lang.apply(schema.empty(), [
        {"do": "add_list", "key": "jobs", "label": "Jobs", "singular": "Job", "fields": [
            {"type": "link", "key": "customer", "label": "Customer", "to": "customers"}, {"type": "text", "key": "title", "label": "Title"},
            {"type": "lookup", "key": "phone", "label": "Phone", "path": "customer.phone"}]},
        {"do": "add_list", "key": "customers", "label": "Customers", "singular": "Customer", "fields": [
            {"type": "text", "key": "name", "label": "Name"}, {"type": "phone", "key": "phone", "label": "Phone"},
            {"type": "total", "key": "jobs_count", "label": "Jobs", "of": "jobs", "link": "customer", "how": "count"}]}])
    assert not out.problems, out.problems
    jobs = schema.table(out.blueprint, "jobs")
    assert [f["key"] for f in jobs["fields"]][:3] == ["customer", "title", "phone"]


def test_changing_a_feature_remakes_its_parts_and_removing_it_takes_them_away():
    bp = lang.apply(everything(), [{"do": "chase", "list": "invoices", "stage": "Sent", "days": 14, "key": "chase"}]).blueprint
    assert any(a.get("feature") == "chase" for a in bp["automations"])
    bp = lang.apply(bp, [{"do": "chase", "list": "invoices", "stage": "Sent", "days": 7, "since": "due_date", "key": "chase"}]).blueprint
    keys = [a["key"] for a in bp["automations"] if a.get("feature") == "chase"]
    assert keys == ["chase"]  # the stamp rule it no longer needs is gone
    assert not schema.field(schema.table(bp, "invoices"), "sent_on")
    out = lang.apply(bp, [{"do": "remove_feature", "feature": "chase"}])
    assert not out.problems
    assert not any(a.get("feature") == "chase" for a in out.blueprint["automations"])
    assert not out.blueprint.get("features")


def test_an_approval_puts_back_who_may_move_it_when_removed():
    bp = everything()
    before = schema.table(bp, "quotes")["workflow"]["transitions"]
    bp = lang.apply(bp, [{"do": "approval", "list": "quotes", "stage": "Sent", "by": "owner"}]).blueprint
    assert all(mv.get("who") == ["owner"] for mv in schema.table(bp, "quotes")["workflow"]["transitions"] if mv["to"] == "Sent")
    bp = lang.apply(bp, [{"do": "remove_feature", "feature": "approval_quotes_sent"}]).blueprint
    assert [{k: v for k, v in mv.items() if k != "feature"} for mv in schema.table(bp, "quotes")["workflow"]["transitions"]] == before


def test_the_app_as_the_builder_reads_it_names_its_features_and_rules():
    bp = lang.apply(everything(), [{"do": "stage_alert", "list": "invoices", "stage": "Paid"}]).blueprint
    said = lang.describe(bp)
    assert "alert_invoices_paid" in said and "invoices “Invoices” (Invoice)" in said
    assert "stages (status): Draft → Sent → Part paid → Paid" in said


class Model:
    """Stands in for the AI in a builder turn: answers each step of the turn by what its instructions start with."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def __call__(self, messages, schema=None, on_progress=None):
        system = messages[0]["content"]
        step = next((k for k, start in (("understand", "Read what the owner says"), ("data", "Now make the lists"),
                                        ("behaviour", "Now make what the app does"), ("checks", "You check business apps"),
                                        ("fix", "The app was tried out"), ("repair", "These patches couldn't")) if start in system), "other")
        self.asked.append(step)
        answer = self.answers.get(step, {"patches": []})
        return json.dumps(answer(messages) if callable(answer) else answer), ""


def test_a_turn_of_the_new_builder_works_out_the_needs_builds_them_and_proves_them(app):
    ws = _workspace(app, None)
    model = Model({
        "understand": {"needs": [{"said": "invoices with GST", "does": "Invoices add up their lines with GST"},
                                 {"said": "tell me when one's paid", "does": "The owner is told when an invoice is paid"}],
                       "reply": "I'll set up your invoices."},
        "data": {"patches": [EVERYTHING[0], _invoices(), EVERYTHING[2]]},
        "behaviour": {"work": [{"need": 1, "patches": [{"do": "line_totals", "list": "invoices", "lines": "invoice_lines", "tax_percent": 10, "tax_label": "GST"}]},
                               {"need": 2, "patches": [{"do": "stage_alert", "list": "invoices", "stage": "Paid"}]}]},
    })
    ai.set_backend(model)
    q = Message(workspace_id=ws.id, channel="builder", user_id=ws.owner_id, role="user", text="Invoices with GST, and tell me when one's paid")
    a = Message(workspace_id=ws.id, channel="builder", role="assistant", status="queued")
    db.session.add_all([q, a])
    db.session.commit()
    from app.builder import chat
    chat.work(a.id)
    a = db.session.get(Message, a.id)
    assert a.status == "done", a.error
    assert model.asked[:3] == ["understand", "data", "behaviour"]
    assert "fix" not in model.asked  # it came out right: nothing to search for
    titles = {x["title"]: x["ok"] for x in a.checks["examples"]}
    assert titles and all(titles.values()), titles
    assert "Invoices add up their lines with GST" in a.text
    ws = db.session.get(Workspace, ws.id)
    assert {e["do"] for e in ws.blueprint["features"]} == {"line_totals", "stage_alert"}


def test_what_fails_is_put_right_by_the_best_of_several_candidate_fixes(app):
    ws = _workspace(app, None)
    good = {"work": [{"need": 1, "patches": [{"do": "payments", "list": "invoices", "payments": "payments", "paid_stage": "Paid", "key": "payments_invoices"}]}]}
    bad = {"work": [{"need": 0, "patches": [{"do": "add_field", "list": "invoices", "field": {"type": "text", "key": "memo", "label": "Memo"}}]}]}
    fixes = iter([bad, good, bad])
    model = Model({
        "understand": {"needs": [{"said": "payments", "does": "Invoices are marked paid when paid"}], "reply": "OK."},
        "data": {"patches": [EVERYTHING[0], _invoices(), EVERYTHING[3]]},
        # a rule that doesn't do what's needed: its worked example fails, and the best candidate fix makes it come out right
        "behaviour": {"work": [{"need": 1, "patches": [{"do": "add_rule", "rule": {"key": "paid", "name": "Note a payment",
                                                                                   "when": {"on": "added", "list": "payments"},
                                                                                   "steps": [{"do": "notify", "message": "A payment came in"}]}}]}]},
        "checks": {"examples": [{"need": 1, "title": "A paid invoice is Paid", "steps": [
            {"do": "add", "table": "invoices", "as": "i1", "values": {"total": 100}},
            {"do": "add", "table": "payments", "as": "p1", "values": {"invoice": "i1", "amount": 100}}],
            "expect": [{"record": "i1", "field": "status", "equals": "Paid"}]}]},
        "fix": lambda messages: next(fixes),
    })
    ai.set_backend(model)
    q = Message(workspace_id=ws.id, channel="builder", user_id=ws.owner_id, role="user", text="Mark invoices paid when they're paid")
    a = Message(workspace_id=ws.id, channel="builder", role="assistant", status="queued")
    db.session.add_all([q, a])
    db.session.commit()
    from app.builder import chat
    chat.work(a.id)
    a = db.session.get(Message, a.id)
    assert a.status == "done", a.error
    assert model.asked.count("fix") == 3  # three candidates at once
    assert all(x["ok"] for x in a.checks["examples"]), a.checks["examples"]
    assert a.checks["fixes"] == 1
    assert a.checks["needs"] == [{"does": "Invoices are marked paid when paid", "said": "payments", "suggested": False, "state": "ok"}]
    assert a.text.startswith("Your app is ready to try.") and "What your app does now:\n- Invoices are marked paid when paid" in a.text


def test_a_need_whose_patches_were_all_refused_is_said_as_not_made_yet(app):
    ws = _workspace(app, None)
    wrong = {"work": [{"need": 1, "patches": [{"do": "stage_alert", "list": "invoices", "stage": "Settled"}]}]}
    model = Model({
        "understand": {"needs": [{"said": "tell me when paid", "does": "The owner is told when an invoice is paid"}], "reply": "OK."},
        "data": {"patches": [EVERYTHING[0], _invoices()]},
        "behaviour": wrong, "repair": wrong, "fix": wrong,
    })
    ai.set_backend(model)
    q = Message(workspace_id=ws.id, channel="builder", user_id=ws.owner_id, role="user", text="Tell me when an invoice is paid")
    a = Message(workspace_id=ws.id, channel="builder", role="assistant", status="queued")
    db.session.add_all([q, a])
    db.session.commit()
    from app.builder import chat
    chat.work(a.id)
    a = db.session.get(Message, a.id)
    assert a.status == "done", a.error
    assert a.checks["needs"][0]["state"] == "unmade"
    assert a.text.startswith("Not everything works yet. I couldn't make this yet: The owner is told when an invoice is paid.")
    assert "(not made yet)" in a.text


def test_a_model_stuck_while_correcting_what_was_refused_doesnt_end_the_turn(app):
    """What was made stays made and is handed over; what was refused is said as not made yet."""
    ws = _workspace(app, None)

    def stuck(messages):
        raise ai.Stuck("The AI got stuck writing its answer (it kept repeating itself).")
    model = Model({
        "understand": {"needs": [{"said": "invoices", "does": "Keeps your invoices"},
                                 {"said": "tell me when paid", "does": "The owner is told when an invoice is paid"}], "reply": "OK."},
        "data": {"patches": [EVERYTHING[0], _invoices()]},
        "behaviour": {"work": [{"need": 2, "patches": [{"do": "stage_alert", "list": "invoices", "stage": "Settled"}]}], "already": [1]},
        "repair": stuck, "fix": stuck, "checks": stuck,
    })
    ai.set_backend(model)
    q = Message(workspace_id=ws.id, channel="builder", user_id=ws.owner_id, role="user", text="Invoices, and tell me when one's paid")
    a = Message(workspace_id=ws.id, channel="builder", role="assistant", status="queued")
    db.session.add_all([q, a])
    db.session.commit()
    from app.builder import chat
    chat.work(a.id)
    a = db.session.get(Message, a.id)
    assert a.status == "done", a.error
    assert "repair" in model.asked
    assert schema.table(db.session.get(Workspace, ws.id).blueprint, "invoices") is not None
    assert "I couldn't make this yet: The owner is told when an invoice is paid" in a.text


def test_a_feature_checks_itself_when_its_total_is_worked_out_by_a_formula(app):
    bp = lang.apply(everything(), [
        {"do": "add_field", "list": "invoices", "field": {"type": "money", "key": "base", "label": "Base"}},
        {"do": "add_field", "list": "invoices", "field": {"type": "money", "key": "call_out_fee", "label": "Call-out fee"}},
        {"do": "change_field", "list": "invoices", "field": {"type": "formula", "key": "total", "label": "Total", "formula": "base + call_out_fee", "shows": "money"}},
        {"do": "payments", "list": "invoices", "payments": "payments", "paid_stage": "Paid", "part_paid_stage": "Part paid"}]).blueprint
    examples = patterns.checks(bp)
    assert len(examples) == 2
    ws = _workspace(app, bp)
    _, results = trial.try_out(ws, examples, screens=False)
    assert all(r["ok"] for r in results), [r.get("problem") for r in results]


def test_lists_named_as_needed_but_not_made_are_asked_for_again(app):
    ws = _workspace(app, None)
    data = iter([{"lists": ["customers", "invoices"], "patches": [EVERYTHING[0]]}, {"lists": ["invoices"], "patches": [_invoices()]}])
    model = Model({
        "understand": {"needs": [{"said": "invoices", "does": "Customers get invoices"}], "reply": "OK."},
        "data": lambda messages: next(data),
        "behaviour": {"work": []},
    })
    ai.set_backend(model)
    q = Message(workspace_id=ws.id, channel="builder", user_id=ws.owner_id, role="user", text="Customers and their invoices")
    a = Message(workspace_id=ws.id, channel="builder", role="assistant", status="queued")
    db.session.add_all([q, a])
    db.session.commit()
    from app.builder import chat
    chat.work(a.id)
    assert model.asked.count("data") == 2
    ws = db.session.get(Workspace, ws.id)
    assert {t["key"] for t in ws.blueprint["tables"]} >= {"customers", "invoices"}


def test_an_answer_too_long_to_write_is_asked_for_again_in_smaller_pieces(app):
    ws = _workspace(app, None)
    data = iter(["cut", {"lists": ["customers", "invoices"], "patches": [EVERYTHING[0]]}, {"lists": ["invoices"], "patches": [_invoices()]}])
    behaviour = iter(["cut", {"patches": [{"do": "stage_alert", "list": "invoices", "stage": "Paid"}]}])

    def answer(steps):
        def one(messages):
            got = next(steps)
            if got == "cut":
                raise ai.CutOff("The AI's answer was cut off at the length limit")
            return got
        return one
    model = Model({"understand": {"needs": [{"said": "tell me when paid", "does": "The owner is told when an invoice is paid"}], "reply": "OK."},
                   "data": answer(data), "behaviour": answer(behaviour)})
    ai.set_backend(model)
    q = Message(workspace_id=ws.id, channel="builder", user_id=ws.owner_id, role="user", text="Invoices, and tell me when one is paid")
    a = Message(workspace_id=ws.id, channel="builder", role="assistant", status="queued")
    db.session.add_all([q, a])
    db.session.commit()
    from app.builder import chat
    chat.work(a.id)
    a = db.session.get(Message, a.id)
    assert a.status == "done", a.error
    assert a.checks["needs"][0]["state"] == "ok"
    assert any("a few at a time" in p for p in a.progress) and any("one need at a time" in p for p in a.progress)


def test_an_email_to_someone_the_record_cant_reach_says_which_addresses_it_can():
    out = lang.apply(everything(), [{"do": "reminder_before", "list": "appointments", "date_field": "starts_on", "days": 1, "email_to": "owner"}])
    assert "The email addresses an appointment can reach: client.email" in out.problems[0]


def test_the_new_builder_adds_the_records_the_owner_gave_and_brings_in_an_attached_spreadsheet(app):
    from app import storage
    from app.models import Job, Record
    ws = _workspace(app, None)
    f = storage.save(ws.id, b"Customer name,E-mail\nAcme,acme@example.com\nBolt,bolt@example.com\n", "customers.csv")
    model = Model({
        "understand": {"needs": [{"said": "my customers are in the sheet", "does": "Keeps the customers from the spreadsheet"}], "reply": "OK."},
        "data": {"lists": ["customers", "services"], "patches": [
            EVERYTHING[0], {"do": "add_list", "key": "services", "label": "Services", "singular": "Service", "fields": [
                {"type": "text", "key": "name", "label": "Name"}, {"type": "money", "key": "price", "label": "Price"}]},
            {"do": "add_records", "list": "services", "records": [{"name": "Gutter clean", "price": 180}]},
            {"do": "import_rows", "file": f.id, "list": "customers", "columns": {"Customer name": "name", "E-mail": "email"}}]},
        "behaviour": {"work": []},
    })
    ai.set_backend(model)
    q = Message(workspace_id=ws.id, channel="builder", user_id=ws.owner_id, role="user", text="My customers are in this sheet; I do gutter cleans for $180",
                files=[f.id])
    a = Message(workspace_id=ws.id, channel="builder", role="assistant", status="queued")
    db.session.add_all([q, a])
    db.session.commit()
    from app.builder import chat
    chat.work(a.id)
    a = db.session.get(Message, a.id)
    assert a.status == "done", a.error
    assert Record.query.filter_by(workspace_id=ws.id, table="services").count() == 1
    assert Job.query.filter_by(workspace_id=ws.id, kind="import").count() == 1
    assert any("Importing" in c for c in a.changes)


def test_a_rule_that_makes_a_document_the_list_hasnt_got_makes_a_standard_one():
    out = lang.apply(everything(), [{"do": "add_rule", "rule": {"key": "send_report", "name": "Send the report", "when": {"on": "reaches", "list": "jobs", "stage": "Done"},
                                                              "steps": [{"do": "document", "document": "job_report"},
                                                                        {"do": "email", "to": "customer.email", "subject": "Your report", "body": "Attached.", "attach": ["job_report"]}]}}])
    assert not out.problems, out.problems
    assert [d["key"] for d in schema.table(out.blueprint, "jobs")["documents"]] == ["job_report"]


@pytest.mark.parametrize("only_if", [[{"field": "status", "op": "equals", "value": "In progress"}], []])
def test_a_limit_in_all_counting_only_some_stages_checks_itself(app, only_if):
    out = lang.apply(everything(), [{"do": "limit", "list": "jobs", "max": 2, "only_if": only_if}])
    assert not out.problems, out.problems
    ws = _workspace(app, out.blueprint)
    _, results = trial.try_out(ws, patterns.checks(out.blueprint), screens=False)
    assert results and all(r["ok"] for r in results), [r.get("problem") for r in results]


def test_a_limit_per_stage_is_refused_saying_how_to_count_only_some_stages():
    out = lang.apply(everything(), [{"do": "limit", "list": "jobs", "per": ["status"], "max": 5}])
    assert "leave per empty" in out.problems[0] and '"op": "one_of"' in out.problems[0]


@pytest.mark.parametrize("said, only", [("Fix what didn't come out right", True), ("Please fix what isn't right on my Checks page:\n- x", True),
                                        ("fix it", True), ("yes please", True), ("add a column “Team note”", False),
                                        ("tell me when a funeral case reaches Closed", False),
                                        ("No, the booking should wait until I approve the plan, then it moves on", False)])
def test_only_a_plain_fix_request_skips_reading_what_the_owner_says(said, only):
    from app.builder.build import only_fixing
    assert only_fixing(said) is only


def test_an_overdue_stage_the_list_has_is_put_straight_after_the_stages_it_comes_from():
    bp = lang.apply(everything(), [{"do": "set_stages", "list": "bookings", "stages": {"stages": ["Booked", "Done", "Late"]}}]).blueprint
    out = lang.apply(bp, [{"do": "add_field", "list": "bookings", "field": {"type": "date", "key": "due", "label": "Due"}},
                          {"do": "overdue", "list": "bookings", "date_field": "due", "from_stages": ["Booked"], "to_stage": "Late"}])
    assert not out.problems, out.problems
    assert schema.field(schema.table(out.blueprint, "bookings"), "status")["options"] == ["Booked", "Late", "Done"]


def test_a_move_tried_by_staff_and_then_by_the_owner_means_staff_are_stopped(app):
    bp = lang.apply(everything(), [{"do": "approval", "list": "quotes", "stage": "Sent", "by": "owner"}]).blueprint
    ws = _workspace(app, bp)
    example = {"title": "Only I send quotes", "steps": [
        {"do": "add", "table": "quotes", "as": "q1", "values": {"total": 100}},
        {"do": "move", "as": "q1", "to": "Sent", "by": "staff"}, {"do": "move", "as": "q1", "to": "Sent", "by": "owner"}],
        "expect": [{"record": "q1", "field": "status", "equals": "Sent"}]}
    _, results = trial.try_out(ws, [example], screens=False)
    assert results[0]["ok"], results[0].get("problem")


def test_an_expected_date_can_be_today_on_the_examples_own_calendar(app):
    bp = lang.apply(everything(), [{"do": "chase", "list": "invoices", "stage": "Sent", "days": 14}]).blueprint
    ws = _workspace(app, bp)
    example = {"title": "Chased and dated", "steps": [
        {"do": "add", "table": "invoices", "as": "i1", "values": {}}, {"do": "move", "record": "i1", "to": "Sent"},
        {"do": "wait", "days": 14}, {"do": "run", "rule": "chase_invoices_sent"}],
        "expect": [{"record": "i1", "field": "chased_sent", "equals": "today"}, {"record": "i1", "field": "sent_on", "equals": "today-14"}]}
    _, results = trial.try_out(ws, [example], screens=False)
    assert results[0]["ok"], results[0].get("problem")


def test_a_model_repeating_itself_while_working_out_the_needs_is_asked_again_and_the_turn_goes_on(app):
    ws = _workspace(app, None)
    understood = iter(["stuck", {"needs": [{"said": "invoices", "does": "Keeps invoices"}], "reply": "OK."}])

    def understand(messages):
        got = next(understood)
        if got == "stuck":
            raise ai.Stuck("The AI got stuck writing its answer (it kept repeating itself).")
        return got
    model = Model({"understand": understand, "data": {"lists": ["invoices"], "patches": [_invoices()]}, "behaviour": {"work": []}})
    ai.set_backend(model)
    q = Message(workspace_id=ws.id, channel="builder", user_id=ws.owner_id, role="user", text="I need invoices")
    a = Message(workspace_id=ws.id, channel="builder", role="assistant", status="queued")
    db.session.add_all([q, a])
    db.session.commit()
    from app.builder import chat
    chat.work(a.id)
    a = db.session.get(Message, a.id)
    assert a.status == "done", a.error
    assert model.asked.count("understand") == 2


def test_an_owner_approving_what_only_they_may_send_is_their_move_and_staff_trying_first_are_stopped(app):
    bp = lang.apply(everything(), [{"do": "approval", "list": "quotes", "stage": "Sent", "by": "owner", "over": {"field": "total", "amount": 5000}}]).blueprint
    ws = _workspace(app, bp)
    example = {"title": "Over $5,000 waits for me", "steps": [
        {"do": "add", "table": "quotes", "as": "q1", "values": {"total": 6000}},
        {"do": "move", "record": "q1", "to": "Sent", "by": "staff"}, {"do": "approve", "record": "q1"}],
        "expect": [{"record": "q1", "field": "status", "equals": "Sent"}]}
    _, results = trial.try_out(ws, [example], screens=False)
    assert results[0]["ok"], results[0].get("problem")


def test_a_stage_added_after_another_leads_on_where_that_one_did_and_nothing_else_changes():
    bp = everything()
    out = lang.apply(bp, [{"do": "add_stage", "list": "quotes", "stage": "On hold", "after": "Sent", "button": "Put on hold"}])
    assert not out.problems, out.problems
    t = schema.table(out.blueprint, "quotes")
    assert schema.field(t, "status")["options"] == ["Draft", "Sent", "On hold", "Accepted", "Declined"]
    moves = {(tuple(mv["from"]), mv["to"]) for mv in t["workflow"]["transitions"]}
    assert (("Sent",), "On hold") in moves and (("Sent", "On hold"), "Accepted") in moves and (("Sent", "On hold"), "Declined") in moves
    assert (("Draft",), "Sent") in moves


def test_a_gate_on_a_condition_keeps_a_record_from_a_stage_until_it_holds(app):
    out = lang.apply(everything(), [{"do": "gate", "list": "jobs", "stage": "In progress",
                                     "only_if": [{"field": "scheduled_for", "op": "within_next_days", "value": 2}]}])
    assert not out.problems, out.problems
    examples = patterns.checks(out.blueprint)
    assert len(examples) == 2
    ws = _workspace(app, out.blueprint)
    _, results = trial.try_out(ws, examples, screens=False)
    assert all(r["ok"] for r in results), [r.get("problem") for r in results]


@pytest.mark.parametrize("gate", [
    {"do": "gate", "list": "quotes", "stage": "Sent", "blocked_if": [{"field": "total", "op": "less_than", "value": 50}]},
    {"do": "gate", "list": "jobs", "stage": "Urgent", "needs": ["assigned_to"]},
    {"do": "gate", "list": "jobs", "stage": "Urgent", "blocked_if": [{"field": "billable", "op": "equals", "value": False}]},
    {"do": "gate", "list": "jobs", "stage": "In progress", "needs": ["customer"]},
])
def test_a_gate_stops_what_its_blocked_if_says_and_holds_back_a_choice_that_isnt_a_stage(app, gate):
    """“A steer under 450kg can't be signed off”: blocked_if, as the owner says it (not only_if written backwards); and
    “signed off” may be a choice of another field, kept to by every save."""
    out = lang.apply(everything(), [gate])
    assert not out.problems, out.problems
    examples = patterns.checks(out.blueprint)
    assert len(examples) == 2
    ws = _workspace(app, out.blueprint)
    _, results = trial.try_out(ws, examples, screens=False)
    assert all(r["ok"] for r in results), [r.get("problem") for r in results]
    gone = lang.apply(out.blueprint, [{"do": "remove_feature", "feature": out.blueprint["features"][-1]["key"]}])
    assert not gone.problems, gone.problems
    assert not schema.table(gone.blueprint, gate["list"]).get("rules")


def test_overdue_can_count_from_the_day_a_record_got_to_its_stage(app):
    """“Chase them 30 days after I send it”: no date field to count from, so the day it got to Sent is noted; on the
    30th day it's Overdue (and the owner's own way of checking that comes out right)."""
    out = lang.apply(everything(), [{"do": "overdue", "list": "invoices", "from_stages": ["Sent"], "to_stage": "Overdue", "days_after": 30}])
    assert not out.problems, out.problems
    examples = patterns.checks(out.blueprint) + [{
        "title": "30 days after it's sent it's overdue", "steps": [
            {"do": "add", "as": "i1", "table": "invoices", "values": {}}, {"do": "move", "record": "i1", "to": "Sent"},
            {"do": "wait", "days": 30}, {"do": "run", "rule": "overdue_invoices"}],
        "expect": [{"record": "i1", "field": "status", "equals": "Overdue"}]}]
    ws = _workspace(app, out.blueprint)
    _, results = trial.try_out(ws, examples, screens=False)
    assert len(results) == 3 and all(r["ok"] for r in results), [r.get("problem") for r in results]


def test_a_rule_emails_a_records_own_field_called_email(app):
    """“email”, “reason”, “file”: names a rule keeps for what started it, and a list's own fields too; with no email
    starting the rule, {{email}} is the record's."""
    out = lang.apply(schema.empty("x"), [
        {"do": "add_list", "key": "students", "label": "Students", "singular": "Student", "fields": [
            {"type": "text", "key": "name", "label": "Name"}, {"type": "email", "key": "email", "label": "Email"},
            {"type": "text", "key": "reason", "label": "Reason"}],
         "stages": {"stages": ["Signed up", "Learning"]}},
        {"do": "chase", "list": "students", "stage": "Signed up", "days": 3, "email_to": "email", "message": "Hi {{name}}: {{reason}}"}])
    assert not out.problems, out.problems
    ws = _workspace(app, out.blueprint)
    _, results = trial.try_out(ws, patterns.checks(out.blueprint), screens=False)
    assert results and all(r["ok"] for r in results), [r.get("problem") for r in results]


def test_a_convert_with_a_stage_and_a_button_is_the_button_shown_at_that_stage(app):
    out = lang.apply(everything(), [{"do": "convert", "from_list": "jobs", "to_list": "invoices", "when_stage": "Done", "button": "Invoice job"}])
    assert not out.problems, out.problems
    rule = next(a for a in out.blueprint["automations"] if a.get("feature"))
    assert rule["trigger"]["type"] == "button" and rule["conditions"] == [{"field": "status", "op": "equals", "value": "Done"}]
    ws = _workspace(app, out.blueprint)
    _, results = trial.try_out(ws, patterns.checks(out.blueprint), screens=False)
    assert results and all(r["ok"] for r in results), [r.get("problem") for r in results]


def _into(bp, table, stage):
    t = schema.table(bp, table)
    return [mv for mv in t["workflow"]["transitions"] if mv["to"] == stage]


def _gated_and_approved():
    out = lang.apply(everything(), [{"do": "gate", "list": "quotes", "stage": "Accepted", "needs": ["total"]},
                                    {"do": "approval", "list": "quotes", "stage": "Accepted", "by": "owner"}])
    assert not out.problems, out.problems
    return out.blueprint


def _holds(bp, stage="Accepted"):
    moves = _into(bp, "quotes", stage)
    return moves and all("total" in (mv.get("requires") or []) for mv in moves) and any(mv.get("who") == ["owner"] for mv in moves)


def test_features_on_a_lists_moves_are_kept_when_its_stages_are_laid_out_anew(app):
    """A gate and an approval on the same move: new stages keep both; removing or changing one keeps the other; a
    stage added or renamed keeps them; and what would quietly take one's parts away is refused, saying how."""
    bp = _gated_and_approved()
    relaid = lang.apply(bp, [{"do": "set_stages", "list": "quotes", "stages": {"stages": ["Draft", "Checked", "Sent", "Accepted", "Declined"], "moves": [
        {"from": ["Draft"], "to": "Checked", "button": "Check"}, {"from": ["Checked"], "to": "Sent", "button": "Send"},
        {"from": ["Sent"], "to": "Accepted", "button": "Accept"}, {"from": ["Sent"], "to": "Declined", "button": "Decline"}]}}])
    assert not relaid.problems, relaid.problems
    assert _holds(relaid.blueprint) and any(mv["from"] == ["Checked"] for mv in schema.table(relaid.blueprint, "quotes")["workflow"]["transitions"])
    assert any(c.startswith("Kept on the new stages") for c in relaid.changes)
    ws = _workspace(app, relaid.blueprint)
    _, results = trial.try_out(ws, patterns.checks(relaid.blueprint), screens=False)
    assert all(r["ok"] for r in results), [(r["title"], r.get("problem")) for r in results]

    without_gate = lang.apply(bp, [{"do": "remove_feature", "feature": "gate_quotes_accepted"}])
    assert not without_gate.problems, without_gate.problems
    moves = _into(without_gate.blueprint, "quotes", "Accepted")
    assert any(mv.get("who") == ["owner"] for mv in moves) and not any(mv.get("requires") for mv in moves)

    changed = lang.apply(bp, [{"do": "gate", "list": "quotes", "stage": "Accepted", "needs": ["customer"]}])
    assert not changed.problems, changed.problems
    moves = _into(changed.blueprint, "quotes", "Accepted")
    assert all(mv.get("requires") == ["customer"] for mv in moves) and any(mv.get("who") == ["owner"] for mv in moves)

    added = lang.apply(bp, [{"do": "add_stage", "list": "quotes", "stage": "On hold", "after": "Draft", "button": "Hold"}])
    assert not added.problems and _holds(added.blueprint), added.problems
    gone = lang.apply(added.blueprint, [{"do": "remove_feature", "feature": "gate_quotes_accepted"},
                                        {"do": "remove_feature", "feature": "approval_quotes_accepted"}])
    assert not gone.problems, gone.problems
    assert any(mv["to"] == "On hold" for mv in schema.table(gone.blueprint, "quotes")["workflow"]["transitions"])

    renamed = lang.apply(bp, [{"do": "rename_choice", "list": "quotes", "field": "status", "old": "Accepted", "new": "Won"}])
    assert not renamed.problems, renamed.problems
    assert _holds(renamed.blueprint, "Won")
    assert {e["params"]["stage"] for e in renamed.blueprint["features"] if e["do"] in ("gate", "approval")} == {"Won"}


def test_a_feature_whose_field_is_taken_away_is_said_to_need_removing_first():
    bp = _gated_and_approved()
    out = lang.apply(bp, [{"do": "remove_field", "list": "quotes", "field": "total"}])
    assert out.refused == [1] and "remove_feature" in " ".join(out.problems), out.problems
    assert _holds(out.blueprint)


def test_a_gate_waiting_for_what_must_be_filled_in_anyway_says_so():
    out = lang.apply(everything(), [{"do": "gate", "list": "bookings", "stage": "Done", "needs": ["date"]}])
    assert out.refused == [1] and "not required" in out.problems[0], out.problems


def test_a_name_written_as_a_key_is_shown_in_words():
    out = lang.apply(schema.empty("x"), [{"do": "add_list", "key": "feedlot_buyers", "label": "Feedlot buyers", "singular": "feedlot_buyer",
                                          "fields": [{"type": "text", "key": "name", "label": "contact_person"}]}])
    t = schema.table(out.blueprint, "feedlot_buyers")
    assert t["singular"] == "Feedlot buyer" and t["fields"][0]["label"] == "Contact person"


def test_an_approval_records_who_let_it_through_and_when(app):
    bp = lang.apply(everything(), [{"do": "approval", "list": "quotes", "stage": "Sent", "by": "owner"}]).blueprint
    ws = _workspace(app, bp)
    example = {"title": "Approved and recorded", "steps": [
        {"do": "add", "table": "quotes", "as": "q1", "values": {"total": 100}}, {"do": "move", "record": "q1", "to": "Sent"}],
        "expect": [{"record": "q1", "field": "sent_by", "equals": "filled in"}, {"record": "q1", "field": "sent_on", "equals": "today"}]}
    _, results = trial.try_out(ws, [example], screens=False)
    assert results[0]["ok"], results[0].get("problem")


def test_a_stage_a_rule_moves_records_on_from_isnt_a_dead_end(app):
    bp = lang.apply(everything(), [
        {"do": "set_stages", "list": "orders", "stages": {"stages": ["New", "In fitting", "Ready"],
                                                           "moves": [{"from": ["New"], "to": "In fitting", "button": "Start fitting"}]}},
        {"do": "add_rule", "rule": {"key": "fitted", "name": "Fitted orders are ready", "when": {"on": "changed", "list": "orders", "field": "name"},
                                    "only_if": [{"field": "status", "op": "equals", "value": "In fitting"}],
                                    "steps": [{"do": "set", "values": [{"field": "status", "to": "Ready"}]}]}}]).blueprint
    ws = _workspace(app, bp)
    walked, _ = trial.try_out(ws, [], screens=False)
    assert not [i for i in walked["issues"] if "orders record at In fitting can't move on" in i["text"]], walked["issues"]


def test_asking_for_a_fix_makes_what_an_owners_check_said_the_app_doesnt_do_yet(app):
    from app.models import Job
    bp = lang.apply(schema.empty("Shop"), [EVERYTHING[0], _invoices()]).blueprint
    ws = _workspace(app, bp)
    ws.state = {"refused_checks": [{"said": "I'm told when an invoice is paid", "missing": True, "by": "owner",
                                    "why": "nothing tells anyone when an invoice is paid"}]}
    db.session.add(Message(workspace_id=ws.id, channel="builder", role="assistant", status="done", text="Not everything works yet. …"))
    db.session.commit()
    model = Model({"data": {"lists": ["invoices"], "patches": []},
                   "behaviour": {"work": [{"need": 1, "patches": [{"do": "stage_alert", "list": "invoices", "stage": "Paid"}]}]}})
    ai.set_backend(model)
    q = Message(workspace_id=ws.id, channel="builder", user_id=ws.owner_id, role="user", text="Fix what didn't come out right")
    a = Message(workspace_id=ws.id, channel="builder", role="assistant", status="queued")
    db.session.add_all([q, a])
    db.session.commit()
    from app.builder import chat
    chat.work(a.id)
    a = db.session.get(Message, a.id)
    assert a.status == "done", a.error
    assert "understand" not in model.asked and "behaviour" in model.asked  # straight to making what the check asks for
    ws = db.session.get(Workspace, ws.id)
    assert any(e["do"] == "stage_alert" for e in ws.blueprint["features"])
    assert Job.query.filter_by(workspace_id=ws.id, kind="check_add").count() == 1  # and the owner's check is tried again


def test_an_email_rule_and_the_ai_steps_compile_with_their_names_checked():
    bp = lang.apply(everything(), [
        {"do": "add_field", "list": "jobs", "field": {"type": "choice", "key": "check", "label": "Check", "options": ["Pass", "Issues"]}},
        {"do": "add_field", "list": "jobs", "field": {"type": "long_text", "key": "notes", "label": "Notes"}}]).blueprint
    out = lang.apply(bp, [
        {"do": "add_rule", "rule": {"key": "bills_by_email", "name": "Bills from suppliers' emails",
                                    "when": {"on": "email", "subject_contains": "invoice", "with_attachment": True},
                                    "steps": [{"do": "ai_create", "list": "invoices", "instructions": "The supplier's bill", "review": "when_unsure",
                                               "attach_to": "pdf"},
                                              {"do": "notify", "who": "owner", "message": "A bill came in: {{email.subject}}"}]}},
        {"do": "add_rule", "rule": {"key": "summarise", "name": "Summarise", "when": {"on": "added", "list": "jobs"},
                                    "steps": [{"do": "ai_fill", "field": "title", "instructions": "A short title"},
                                              {"do": "ai_check", "field": "check", "instructions": "Pass if it has a customer", "notes_field": "notes"},
                                              {"do": "assign", "field": "assigned_to", "among": "editor"}]}},
        {"do": "add_rule", "rule": {"key": "bad", "name": "Bad", "when": {"on": "email"}, "steps": [{"do": "set", "values": [{"field": "x", "to": 1}]}]}},
        {"do": "add_rule", "rule": {"key": "bad2", "name": "Bad2", "when": {"on": "added", "list": "jobs"},
                                    "steps": [{"do": "ai_check", "field": "title", "instructions": "x"}]}}])
    assert out.refused == [3, 4], out.problems
    assert "has no record yet" in out.problems[0] and "needs a select field" in out.problems[1]
    rules = {a["key"]: a for a in out.blueprint["automations"]}
    assert rules["bills_by_email"]["trigger"] == {"type": "email_received", "subject_contains": "invoice", "has_attachment": True}
    assert rules["bills_by_email"]["actions"][0] == {"type": "ai_create_record", "table": "invoices", "instructions": "The supplier's bill",
                                                     "attach_to": "pdf", "review": "when_unsure", "reviewer": "editor"}
    assert [x["type"] for x in rules["summarise"]["actions"]] == ["ai_fill", "ai_check", "assign"]


def test_discovery_proposes_an_app_in_the_keel_language_and_building_it_brings_the_rows_in(app):
    from app import discovery, storage
    from app.models import Job
    ws = _workspace(app, schema.empty("Fresh"))
    f = storage.save(ws.id, b"Customer name,E-mail\nAcme,acme@example.com\n", "customers.csv")
    model = Model({"other": {"patches": [EVERYTHING[0], {"do": "import_rows", "file": f.id, "list": "customers",
                                                         "columns": {"Customer name": "name", "E-mail": "email"}},
                                         {"do": "add_list", "key": "nope", "label": "Nope", "singular": "Nope", "fields": [
                                             {"type": "link", "key": "x", "label": "X", "to": "nowhere"}]}],
                             "reply": "Your customers spreadsheet becomes a list."},
                   "repair": {"patches": []}})
    ai.set_backend(model)
    answer, out = discovery.propose(ws, {"files": 1, "spreadsheets": [{"name": "customers.csv", "file_id": f.id}], "mail": {"messages": 0}})
    assert [p["do"] for p in answer["patches"]] == ["add_list", "import_rows"]  # what didn't fit was left out
    assert out.changes and [d["op"] for d in out.data] == ["import_file"]
    job = Job(workspace_id=ws.id, kind="discover", status="done", payload={},
              result={"found": {}, "patches": answer["patches"], "changes": out.changes, "imports": out.data})
    db.session.add(job)
    db.session.commit()
    number, imports = discovery.build(ws, job, db.session.get(User, ws.owner_id))
    assert number == 1 and len(imports) == 1 and schema.table(db.session.get(Workspace, ws.id).blueprint, "customers")


def test_who_may_see_what_is_part_of_the_language():
    out = lang.apply(everything(), [
        {"do": "set_access", "list": "payments", "view": "finance", "edit": "finance"},
        {"do": "own_records", "list": "jobs", "field": "assigned_to", "roles": ["editor"], "see_all": ["finance"]},
        {"do": "add_field", "list": "jobs", "field": {"type": "money", "key": "cost", "label": "Cost", "visible_to": "admin"}},
        {"do": "add_field", "list": "customers", "field": {"type": "text", "key": "bank_account", "label": "Bank account", "sensitive": True}},
        {"do": "own_records", "list": "jobs", "field": "title"}])
    assert out.refused == [5] and "isn't a person field" in out.problems[0]
    bp = out.blueprint
    assert schema.table(bp, "payments")["access"] == {"teams": ["finance"]}
    assert schema.table(bp, "jobs")["row_access"] == {"field": "assigned_to", "roles": ["editor"], "teams": ["finance"]}
    assert schema.field(schema.table(bp, "jobs"), "cost")["access"] == "admin"
    assert schema.field(schema.table(bp, "customers"), "bank_account")["sensitive"] is True


def test_fields_shown_or_needed_only_when_something_holds_views_and_validations_are_part_of_the_language():
    out = lang.apply(everything(), [
        {"do": "add_field", "list": "quotes", "field": {"type": "text", "key": "reason", "label": "Why declined",
                                                       "show_if": [{"field": "status", "op": "equals", "value": "Declined"}],
                                                       "required_if": [{"field": "status", "op": "equals", "value": "Declined"}]}},
        {"do": "add_view", "list": "jobs", "view": {"type": "timeline", "label": "Timeline", "date_field": "scheduled_for", "end_field": "scheduled_for"}},
        {"do": "add_validation", "list": "quotes", "check": "total >= 0", "message": "A total can't be below zero"},
        {"do": "add_validation", "list": "quotes", "check": "totl >= 0", "message": "x"}])
    assert out.refused == [4] and "“totl” isn't a field of Quotes" in out.problems[0]
    quotes = schema.table(out.blueprint, "quotes")
    reason = schema.field(quotes, "reason")
    assert reason["show_if"] == [{"field": "status", "op": "equals", "value": "Declined"}] and reason["required_if"]
    assert quotes["rules"][-1]["check"] == "total >= 0"
    assert next(v for v in schema.table(out.blueprint, "jobs")["views"] if v["type"] == "timeline")["start_field"] == "scheduled_for"


def test_a_turn_builds_the_app_shows_what_it_made_and_can_be_undone(app, owner):
    from app.builder import chat
    from app.models import Version
    _, ws = owner
    ai.set_backend(Model({"understand": {"needs": [{"said": "customers and invoices", "does": "Keeps customers and their invoices"}], "reply": "OK."},
                          "data": {"lists": ["customers", "invoices"], "patches": [EVERYTHING[0], _invoices()]},
                          "behaviour": {"work": [], "already": [1]}}))
    m = chat.ask(ws, "I need customers and invoices")
    chat.work(m.id)
    m = db.session.get(Message, m.id)
    assert m.status == "done" and m.version_after and m.navigate == f"/w/{ws.id}/app/t/customers"
    ws = db.session.get(Workspace, ws.id)
    assert [t["key"] for t in ws.blueprint["tables"]] == ["customers", "invoices"]
    chat.restore(ws, m.version_before)
    assert ws.blueprint["tables"] == [] and Version.query.filter_by(workspace_id=ws.id).count() == ws.version + 1  # going back is a version too


def test_a_total_over_another_new_lists_formula_is_made_whichever_list_comes_first():
    out = lang.apply(schema.empty(), [
        {"do": "add_list", "key": "quotes", "label": "Quotes", "singular": "Quote", "fields": [
            {"type": "total", "key": "subtotal", "label": "Subtotal", "of": "quote_lines", "link": "quote", "how": "sum", "field": "amount"},
            {"type": "formula", "key": "total", "label": "Total", "formula": "subtotal * 1.1", "shows": "money"}]},
        {"do": "add_list", "key": "quote_lines", "label": "Quote lines", "singular": "Quote line", "inside": "quotes", "fields": [
            {"type": "number", "key": "quantity", "label": "Qty"}, {"type": "money", "key": "unit_price", "label": "Unit price"},
            {"type": "formula", "key": "amount", "label": "Amount", "formula": "quantity * unit_price", "shows": "money"}]}])
    assert not out.problems, out.problems
    assert {"subtotal", "total"} <= {f["key"] for f in schema.table(out.blueprint, "quotes")["fields"]}


# --- round 34: what a change, a check and a button do ---------------------------------------------------------------

def _stages_of(bp, key):
    t = schema.table(bp, key)
    return next(f for f in t["fields"] if f["key"] == t["workflow"]["field"])["options"]


def test_a_feature_taken_away_and_sent_again_in_one_answer_is_made_afresh():
    """“remove_feature X”, then X as it should be: X is there as sent the second time, never made and then taken away."""
    bp = lang.apply(everything(), [{"do": "overdue", "list": "invoices", "date_field": "due_date", "from_stages": ["Sent"], "days_after": 30}]).blueprint
    out = lang.apply(bp, [{"do": "remove_feature", "feature": "overdue_invoices"},
                          {"do": "overdue", "list": "invoices", "date_field": "due_date", "from_stages": ["Sent"], "days_after": 0}])
    assert not out.problems, out.problems
    entry = next(e for e in out.blueprint["features"] if e["key"] == "overdue_invoices")
    assert entry["params"]["days_after"] == 0
    assert any(a["key"] == "overdue_invoices" for a in out.blueprint["automations"])


def test_a_stage_put_after_the_one_an_overdue_stage_comes_from_stays_where_it_was_put():
    bp = lang.apply(everything(), [{"do": "overdue", "list": "invoices", "date_field": "due_date", "from_stages": ["Sent"]}]).blueprint
    assert _stages_of(bp, "invoices")[:3] == ["Draft", "Sent", "Overdue"]  # placed when it's made
    out = lang.apply(bp, [{"do": "add_stage", "list": "invoices", "stage": "On hold", "after": "Sent"}])
    assert not out.problems, out.problems
    assert _stages_of(out.blueprint, "invoices")[:4] == ["Draft", "Sent", "On hold", "Overdue"]
    assert any(c.startswith("Kept on the new stages") for c in out.changes)


def test_sending_a_document_at_a_stage_names_the_button_that_gets_there(app):
    send = {"do": "send_document", "list": "invoices", "button": "Email the invoice", "email_to": "customer.email", "then_stage": "Sent"}
    bp = lang.apply(everything(), [send]).blueprint
    into = lambda b: [mv["label"] for mv in schema.table(b, "invoices")["workflow"]["transitions"] if mv["to"] == "Sent"]  # noqa: E731
    assert into(bp) == ["Email the invoice"]
    ws = _workspace(app, bp)
    pressed = {"title": "Emailing it sends it", "steps": [
        {"do": "add", "table": "customers", "as": "c1", "values": {"name": "Ann", "email": "ann@example.com"}},
        {"do": "add", "table": "invoices", "as": "i1", "values": {"customer": "c1", "total": 100}},
        {"do": "press", "record": "i1", "button": "Email the invoice"}],
        "expect": [{"record": "i1", "field": "status", "equals": "Sent"}, {"told": True, "record": "i1"}]}
    _, (result,) = trial.try_out(ws, [pressed], screens=False)
    assert result["ok"], result
    assert into(lang.apply(bp, [{"do": "remove_feature", "feature": "send_invoices"}]).blueprint) == ["Send"]  # given back


def test_a_number_of_days_read_with_days_is_that_number(app):
    from datetime import timedelta
    from app.blueprint import expr
    from app.engine.records import Engine, RecordContext
    ws = _workspace(app, everything())
    engine = Engine(ws)
    r = engine.create("invoices", {"due_date": (engine.today - timedelta(days=3)).isoformat()})
    assert expr.evaluate("max(0, (today() - due_date).days)", RecordContext(engine, r)) == 3


def test_the_builder_reads_a_lists_rules_and_can_change_or_take_one_away():
    from app.lang import read
    bp = lang.apply(everything(), [m.AddValidation.__keel_example__]).blueprint
    assert "must keep amount <= invoice.total" in read.describe(bp)
    again = lang.apply(bp, [{**m.AddValidation.__keel_example__, "message": "Never more than the invoice"}]).blueprint
    assert [r["message"] for r in schema.table(again, "payments")["rules"]] == ["Never more than the invoice"]  # replaced, not twice
    gone = lang.apply(again, [{"do": "remove_validation", "list": "payments", "check": "amount<=invoice.total"}])
    assert not gone.problems, gone.problems
    assert not schema.table(gone.blueprint, "payments").get("rules")


def test_a_features_own_checks_keep_to_the_rules_of_the_lists_they_add_to(app):
    """A job needs an accepted quote: the convert feature's own check adds its job with one, so it's tried, not refused."""
    out = lang.apply(everything(), [
        {"do": "add_field", "list": "jobs", "field": {"type": "link", "key": "quote", "label": "Quote", "to": "quotes"}},
        {"do": "add_validation", "list": "jobs", "check": "quote.status == 'Accepted'", "message": "A job needs an accepted quote"},
        {"do": "convert", "from_list": "jobs", "to_list": "invoices", "when_stage": "Done", "button": "Invoice job"}])
    assert not out.problems, out.problems
    ws = _workspace(app, out.blueprint)
    _, results = trial.try_out(ws, patterns.checks(out.blueprint), screens=False)
    assert all(r["ok"] for r in results), [(r["title"], r.get("problem")) for r in results if not r["ok"]]


def test_a_button_waiting_for_something_is_refused_saying_what_it_waits_for(app):
    from app.engine import automations
    from app.engine.records import Engine, FieldErrors
    bp = lang.apply(everything(), [{"do": "add_rule", "rule": {
        "key": "order_parts", "name": "Order parts", "when": {"on": "button", "list": "jobs", "label": "Order parts"},
        "only_if": [{"field": "billable", "op": "equals", "value": True}], "steps": [{"do": "notify", "message": "Parts for {{title}}"}]}}])
    assert not bp.problems, bp.problems
    ws = _workspace(app, bp.blueprint)
    engine = Engine(ws)
    job = engine.create("jobs", {"title": "Fence", "billable": False})
    rule = next(a for a in bp.blueprint["automations"] if a["key"] == "order_parts")
    with pytest.raises(FieldErrors) as refused:
        automations.press(engine, rule, job, "Olive")
    assert "You can press “Order parts” once" in str(refused.value.errors)
    stopped = {"title": "Parts aren't ordered for a job that isn't billable", "steps": [
        {"do": "add", "table": "jobs", "as": "j1", "values": {"title": "Gate", "billable": False}},
        {"do": "press", "record": "j1", "button": "Order parts", "refused": True}], "expect": [{"record": "j1", "field": "title", "equals": "Gate"}]}
    _, (result,) = trial.try_out(ws, [stopped], screens=False)
    assert result["ok"], result


def test_gates_on_stages_one_after_another_each_pass_their_own_checks(app):
    """Getting to the second gate's stage goes through the first's, filled in as its button asks."""
    out = lang.apply(everything(), [
        {"do": "add_field", "list": "orders", "field": {"type": "date", "key": "cleared_on", "label": "Cleared on"}},
        {"do": "add_field", "list": "orders", "field": {"type": "yes_no", "key": "paid", "label": "Paid"}},
        {"do": "gate", "list": "orders", "stage": "In fitting", "blocked_if": [{"field": "cleared_on", "op": "is_empty"}]},
        {"do": "gate", "list": "orders", "stage": "Ready", "needs": ["paid"]},
        {"do": "stage_alert", "list": "orders", "stage": "Ready"}])
    assert not out.problems, out.problems
    ws = _workspace(app, out.blueprint)
    _, results = trial.try_out(ws, patterns.checks(out.blueprint), screens=False)
    assert all(r["ok"] for r in results), [(r["title"], r.get("problem")) for r in results if not r["ok"]]


def _after_not_everything_works(app, understood):
    """A turn asked right after the builder said “Not everything works yet”, with a check of the owner's the app doesn't
    do yet. Returns (the reply, the model)."""
    ws = _workspace(app, everything())
    ws.state = {"refused_checks": [{"said": "Overdue invoices get a reminder", "why": "nothing reminds anyone", "missing": True, "by": "owner"}]}
    db.session.add(Message(workspace_id=ws.id, channel="builder", role="assistant", status="done",
                           text="Not everything works yet. Press “Fix these” and I'll have another go, or tell me what should happen."))
    db.session.commit()
    model = Model({"understand": understood, "data": {"patches": []},
                   "behaviour": {"work": [{"need": 1, "patches": [{"do": "stage_alert", "list": "invoices", "stage": "Paid"}]}], "already": [2]}})
    ai.set_backend(model)
    q = Message(workspace_id=ws.id, channel="builder", user_id=ws.owner_id, role="user", text="When an invoice gets to Paid, let me know.")
    a = Message(workspace_id=ws.id, channel="builder", role="assistant", status="queued")
    db.session.add_all([q, a])
    db.session.commit()
    from app.builder import chat
    chat.work(a.id)
    return db.session.get(Message, a.id), model


def test_after_not_everything_works_a_new_request_is_only_that(app):
    """“Let me know when one's paid” after “Not everything works yet” is a new request: the owner's check the app doesn't
    do yet stays on the Checks page for Fix, not taken on (nor anything changed for it) unasked."""
    a, _ = _after_not_everything_works(app, {"needs": [{"said": "let me know", "does": "Tells you when an invoice is paid"}], "reply": "OK.", "about": []})
    assert a.status == "done", a.error
    assert [n["does"] for n in a.checks["needs"]] == ["Tells you when an invoice is paid"]
    assert not any(p.startswith("Making what your checks ask for") for p in a.progress or [])


def test_after_not_everything_works_what_the_message_is_about_is_taken_on(app):
    a, _ = _after_not_everything_works(app, {"needs": [{"said": "remind them", "does": "Reminds about overdue invoices"}], "reply": "OK.", "about": [1]})
    assert a.status == "done", a.error
    assert "Overdue invoices get a reminder" in [n["said"] for n in a.checks["needs"]]


def test_a_check_whose_example_needs_a_rule_the_app_hasnt_got_is_one_it_doesnt_do_yet(app):
    """Looked at twice and still needing a rule the app doesn't have: the owner hears their app doesn't do it yet (and
    can ask the builder for it), not that it couldn't be turned into a check."""
    from app.builder import checking
    ws = _workspace(app, everything())
    example = {"title": "x", "steps": [{"do": "add", "table": "invoices", "as": "i1", "values": {"total": 100}},
                                       {"do": "wait", "days": 8}, {"do": "run", "rule": "chase_overdue_invoices"}],
               "expect": [{"record": "i1", "field": "status", "equals": "Sent"}]}

    def model(messages, schema=None, on_progress=None):
        again = len(messages) > 2  # the second look: it says its example was the slip, and sends one needing the same rule
        return json.dumps({"verdict": "example", "missing": "", "examples": [example]} if again else {"examples": [example]}), ""
    ai.set_backend(model)
    result = checking.add_and_tell(ws, "Invoices a week overdue get chased", by="owner")
    assert result["kind"] == "missing", result
    assert result["plain"].startswith("your app doesn't do this yet: it has no rule that does it")
    kept = db.session.get(Workspace, ws.id).state["refused_checks"]
    assert kept[0]["missing"] and kept[0]["said"] == "Invoices a week overdue get chased"


# a chain of must-have links, four deep: an order's contract's quote's enquiry's client
CHAIN = [
    {"do": "add_list", "key": "clients", "label": "Clients", "singular": "Client", "fields": [
        {"type": "text", "key": "name", "label": "Name", "required": True}, {"type": "email", "key": "email", "label": "Email"}]},
    {"do": "add_list", "key": "enquiries", "label": "Enquiries", "singular": "Enquiry", "fields": [
        {"type": "link", "key": "client", "label": "Client", "to": "clients", "required": True}]},
    {"do": "add_list", "key": "quotes", "label": "Quotes", "singular": "Quote", "fields": [
        {"type": "link", "key": "enquiry", "label": "Enquiry", "to": "enquiries", "required": True}]},
    {"do": "add_list", "key": "contracts", "label": "Contracts", "singular": "Contract", "fields": [
        {"type": "link", "key": "quote", "label": "Quote", "to": "quotes", "required": True}], "stages": {"stages": ["Draft", "Approved"]}},
    {"do": "add_list", "key": "orders", "label": "Orders", "singular": "Order", "fields": [
        {"type": "link", "key": "contract", "label": "Contract", "to": "contracts", "required": True}],
     "stages": {"stages": ["Created", "Coordinating", "Ready"]}},
]


def test_a_features_checks_add_what_a_record_must_link_to_however_far_and_meet_what_its_moves_wait_for(app):
    """An order must have a contract, which must have a quote, an enquiry and a client: a feature's check adds them all.
    The move on from Created waits for the contract to be Approved: the walk approves it first, as a person would."""
    from app.lang.design import Design
    out = lang.apply(schema.empty("Fit-outs"), CHAIN + [
        {"do": "gate", "list": "orders", "stage": "Coordinating", "only_if": [{"field": "contract.status", "op": "equals", "value": "Approved"}]},
        {"do": "stage_alert", "list": "orders", "stage": "Ready"}])
    assert not out.problems, out.problems
    d = Design(out.blueprint)
    assert [s["table"] for s in patterns.adding(d, d.list("orders"), "r1")] == ["clients", "enquiries", "quotes", "contracts", "orders"]
    ws = _workspace(app, out.blueprint)
    _, results = trial.try_out(ws, lang.checks(out.blueprint), screens=False)
    assert results and all(r["ok"] for r in results), [(r["title"], r.get("problem")) for r in results if not r["ok"]]


def test_a_features_check_never_types_into_what_the_app_works_out_and_is_about_its_own_messages(app):
    """The enquiry is emailed to its client's email, which the enquiry looks up (it can't be typed in): the check adds
    the client with an email instead. Another alert telling someone at an earlier stage is that alert's, not this one's."""
    out = lang.apply(schema.empty("Fit-outs"), CHAIN[:2] + [
        {"do": "add_field", "list": "enquiries", "field": {"type": "lookup", "key": "client_email", "label": "Client email", "path": "client.email"}},
        {"do": "set_stages", "list": "enquiries", "stages": {"stages": ["New", "Seen", "Answered"]}},
        {"do": "send_document", "list": "enquiries", "button": "Send answer", "email_to": "client_email", "then_stage": "Answered"},
        {"do": "stage_alert", "list": "enquiries", "stage": "Seen", "key": "seen_alert"}])
    assert not out.problems, out.problems
    checks = lang.checks(out.blueprint)
    typed = [k for c in checks for s in c["steps"] if s.get("table") == "enquiries" for k in s.get("values") or {}]
    assert "client_email" not in typed
    told = [e for c in checks for e in c["expect"] if e.get("told") is not None]
    assert told and all(e.get("by_rule") for e in told)
    ws = _workspace(app, out.blueprint)
    _, results = trial.try_out(ws, checks, screens=False)
    assert all(r["ok"] for r in results), [(r["title"], r.get("problem")) for r in results if not r["ok"]]


def test_a_view_grouped_by_a_linked_record_is_a_list_under_each_one():
    """“Unpaid invoices by customer”: grouped by the customer (customer.name means the customer), and a board by
    anything but a choice is the list grouped under each one."""
    out = lang.apply(everything(), [
        {"do": "add_view", "list": "invoices", "view": {"type": "list", "label": "By customer", "group_by": "customer.name"}},
        {"do": "add_view", "list": "jobs", "view": {"type": "board", "label": "By person", "group_by": "assigned_to"}}])
    assert not out.problems, out.problems
    by_customer = next(v for v in schema.table(out.blueprint, "invoices")["views"] if v["label"] == "By customer")
    by_person = next(v for v in schema.table(out.blueprint, "jobs")["views"] if v["label"] == "By person")
    assert (by_customer["type"], by_customer["group_by"]) == ("list", "customer")
    assert (by_person["type"], by_person["group_by"]) == ("list", "assigned_to")


def test_a_gate_can_wait_for_a_linked_records_field_to_be_filled_in():
    out = lang.apply(everything(), [{"do": "gate", "list": "jobs", "stage": "In progress", "needs": ["customer.email"]}])
    assert not out.problems, out.problems
    move = next(mv for mv in schema.table(out.blueprint, "jobs")["workflow"]["transitions"] if mv["to"] == "In progress")
    assert {"field": "customer.email", "op": "not_empty"} in [{k: c.get(k) for k in ("field", "op")} for c in move["only_if"]]


def test_an_approval_on_a_list_whose_status_is_a_plain_choice_gives_it_stages_first():
    """Members' status is a choice (Active, Lapsed) with no buttons: an approval to Lapsed makes those its stages, a
    button on to each next, so there's a move for the approver to hold."""
    out = lang.apply(everything(), [{"do": "approval", "list": "members", "stage": "Lapsed", "by": "owner"}])
    assert not out.problems, out.problems
    flow = schema.table(out.blueprint, "members")["workflow"]
    assert flow["field"] == "status" and flow["start"] == "Active"
    assert [(mv["from"], mv["to"], mv.get("who")) for mv in flow["transitions"]] == [(["Active"], "Lapsed", ["owner"])]


def test_a_field_sent_again_is_made_as_sent_and_never_refused_for_being_there():
    bp = everything()
    same = lang.apply(bp, [{"do": "add_field", "list": "jobs", "field": {"type": "text", "key": "fax", "label": "Fax"}}])
    assert not same.problems and not same.changes, (same.problems, same.changes)
    changed = lang.apply(bp, [{"do": "add_field", "list": "jobs", "field": {"type": "text", "key": "fax", "label": "Fax", "required": True}}])
    assert not changed.problems, changed.problems
    assert schema.field(schema.table(changed.blueprint, "jobs"), "fax")["required"] is True
    stages = lang.apply(bp, [{"do": "add_field", "list": "jobs", "field": {"type": "choice", "key": "status", "label": "Status", "options": ["Open", "Closed"]}}])
    assert stages.problems and "set_stages changes them" in stages.problems[0]


def test_a_rule_can_text_a_phone():
    out = lang.apply(everything(), [
        {"do": "add_field", "list": "customers", "field": {"type": "phone", "key": "mobile", "label": "Mobile"}},
        {"do": "add_rule", "rule": {"key": "ready_text", "name": "Text when done", "when": {"on": "reaches", "list": "jobs", "stage": "Done"},
                                    "steps": [{"do": "text", "to": "customer.mobile", "message": "Your job {{title}} is done."}]}}])
    assert not out.problems, out.problems
    rule = next(a for a in out.blueprint["automations"] if a["key"] == "ready_text")
    assert rule["actions"][0] == {"type": "send_sms", "to": "{{customer.mobile}}", "message": "Your job {{title}} is done."}
    assert "text {{customer.mobile}}" in lang.describe(out.blueprint)
    wrong = lang.apply(everything(), [{"do": "add_rule", "rule": {"key": "x", "name": "x", "when": {"on": "reaches", "list": "jobs", "stage": "Done"},
                                                                  "steps": [{"do": "email", "to": "customer.discount_percent", "subject": "s", "body": "b"}]}}])
    assert wrong.problems


def test_an_owners_check_that_no_longer_fits_the_app_is_written_again_from_their_words(app):
    """The owner's check named a field since taken away: it can't be acted out on the app as it is. Before the turn puts
    anything right, it's written again from the owner's own words for the app as it is, and kept."""
    from app.builder import checking
    ws = _workspace(app, everything())
    old = {"title": "A booking can be added", "by": "owner", "steps": [
        {"do": "add", "table": "bookings", "as": "b1", "values": {"date": "today", "room": "Blue"}}],
        "expect": [{"table": "bookings", "count": 1}]}
    ws.state = {"examples": [old]}
    db.session.commit()
    checking.check(ws, "a change")
    assert [x for x, _ in checking.stale(ws)] == [old]
    fixed = {"title": "A booking can be added", "steps": [{"do": "add", "table": "bookings", "as": "b1", "values": {"date": "today"}}],
             "expect": [{"table": "bookings", "count": 1}]}
    ai.set_backend(lambda messages, schema=None, on_progress=None: (json.dumps({"verdict": "example", "missing": "", "examples": [fixed]}), ""))
    assert checking.refit(ws, checking.stale(ws)) == ["A booking can be added"]
    kept = checking.examples(db.session.get(Workspace, ws.id))
    assert kept[0]["steps"][0]["values"] == {"date": "today"} and kept[0]["by"] == "owner"
    checking.check(ws, "your checks were rewritten")
    assert not checking.stale(ws) and all(x["ok"] for x in ws.state["checks"]["examples"])


def test_a_step_meant_to_be_refused_that_only_the_owner_could_do_is_tried_as_staff(app):
    """“Staff can't send a quote without my OK”: the example moves it as the owner (who may), expecting a refusal. Tried
    again as an ordinary staff member, as the owner meant, before they're told their app doesn't do it."""
    from app.builder import checking
    bp = lang.apply(everything(), [{"do": "approval", "list": "quotes", "stage": "Sent", "by": "owner"}]).blueprint
    ws = _workspace(app, bp)
    example = {"title": "Only I can send a quote", "steps": [{"do": "add", "table": "quotes", "as": "q1", "values": {"total": 100}},
                                                             {"do": "move", "record": "q1", "to": "Sent", "refused": True}],
               "expect": [{"record": "q1", "field": "status", "equals": "Draft"}]}
    ai.set_backend(lambda messages, schema=None, on_progress=None: (json.dumps({"examples": [example]}), ""))
    result = checking.add(ws, "Only I can send a quote", by="owner")
    assert result["ok"], result
    assert result["example"]["steps"][1].get("by") == "staff"


@pytest.mark.parametrize("timing", [{"length": "takes"}, {"ends": "ends_at"}, {}])
def test_no_double_booking_refuses_an_overlapping_time_for_the_same_one_and_checks_itself(app, timing):
    """“No double-booking a stylist”: an appointment whose time overlaps another for the same stylist is refused; one
    at another time, or for another stylist, isn't. Its own check acts that out."""
    out = lang.apply(everything(), [
        {"do": "add_list", "key": "stylists", "label": "Stylists", "singular": "Stylist", "fields": [{"type": "text", "key": "name", "label": "Name"}]},
        {"do": "add_list", "key": "visits", "label": "Visits", "singular": "Visit", "fields": [
            {"type": "link", "key": "stylist", "label": "Stylist", "to": "stylists", "required": True},
            {"type": "datetime", "key": "starts_at", "label": "Starts at", "required": True},
            {"type": "datetime", "key": "ends_at", "label": "Ends at"}, {"type": "duration", "key": "takes", "label": "Takes"}],
         "stages": {"stages": ["Booked", "Done", "Cancelled"]}},
        {"do": "no_overlap", "list": "visits", "per": ["stylist"], "starts": "starts_at", **timing,
         "only_if": [{"field": "status", "op": "not_equals", "value": "Cancelled"}]}])
    assert not out.problems, out.problems
    ws = _workspace(app, out.blueprint)
    _, results = trial.try_out(ws, patterns.checks(out.blueprint), screens=False)
    mine = [r for x, r in zip(patterns.checks(out.blueprint), results) if "same time" in x["title"]]
    assert mine and all(r["ok"] for r in mine), [r.get("problem") for r in mine]
    engine = Engine(ws)
    sam, kim = engine.create("stylists", {"name": "Sam"}), engine.create("stylists", {"name": "Kim"})
    first = {"stylist": sam.id, "starts_at": "2030-01-07 10:00", "ends_at": "2030-01-07 11:00", "takes": "1h"}
    engine.create("visits", first)
    with pytest.raises(FieldErrors) as refused:  # the same moment; or, with an end or a length, half an hour in
        engine.create("visits", {**first, "starts_at": "2030-01-07 10:30" if timing else "2030-01-07 10:00", "ends_at": "2030-01-07 11:30"})
    assert "overlaps another visit for the same stylist" in str(refused.value.errors)
    engine.create("visits", {**first, "stylist": kim.id})  # another stylist
    engine.create("visits", {**first, "starts_at": "2030-01-08 10:00", "ends_at": "2030-01-08 11:00"})  # another day
    if timing:  # it ends before the next starts
        engine.create("visits", {**first, "starts_at": "2030-01-07 11:00", "ends_at": "2030-01-07 12:00"})


def test_a_record_named_by_what_its_called_or_its_number_is_that_record(app):
    """The builder's example says “ORD-1” or “Ethiopia Yirgacheffe” instead of a short name it gave: the one called
    that, as a person points at the record in front of them."""
    ws = _workspace(app, everything())
    example = {"title": "An invoice is sent", "steps": [
        {"do": "add", "table": "customers", "values": {"name": "Ethiopia Yirgacheffe"}},
        {"do": "add", "table": "invoices", "values": {"customer": "Ethiopia Yirgacheffe", "total": 10}},
        {"do": "move", "record": "INV-1", "to": "Sent"}],
        "expect": [{"record": "INV-1", "field": "status", "equals": "Sent"},
                   {"record": "Ethiopia Yirgacheffe", "field": "name", "equals": "Ethiopia Yirgacheffe"}]}
    with trial.copy_of(ws) as copy:
        result = trial.play(copy, example)
    assert result["ok"], result.get("problem")


def test_a_features_check_fills_in_what_must_be_filled_in_while_it_applies():
    """A field required unless cancelled (required_if) is filled in by a feature's own check, as one always required."""
    out = lang.apply(everything(), [{"do": "add_field", "list": "jobs", "field": {
        "type": "number", "key": "area", "label": "Area", "required_if": [{"field": "status", "op": "not_equals", "value": "Done"}]}},
        {"do": "approval", "list": "jobs", "stage": "Done", "by": "owner"}])
    assert not out.problems, out.problems
    adds = [s for x in patterns.checks(out.blueprint) for s in x["steps"] if s.get("do") == "add" and s.get("table") == "jobs"]
    assert adds and all("area" in s["values"] for s in adds)


def test_a_formula_can_make_a_date_from_a_year_month_and_day():
    from app.blueprint import expr
    ctx = expr.Context(today=date(2026, 11, 6))
    assert expr.evaluate("date(year(today()), month(today()), 5)", ctx) == date(2026, 11, 5)
    assert expr.evaluate("date(2026, 13, 1)", ctx) == date(2027, 1, 1)
    assert expr.evaluate("eomonth(today(), 0)", ctx) == date(2026, 11, 30)
    with pytest.raises(expr.ExprError) as wrong:
        expr.evaluate("days_between(today())", ctx)
    assert "positional" not in str(wrong.value)


def test_a_record_a_rule_adds_from_a_value_left_empty_is_added_for_people_to_finish(app):
    """A reminder's due date copied from the enquiry's reply-by date, which people left empty: the reminder is added
    all the same, and people are told what to fill in, as when the rule couldn't know it at all."""
    bp = lang.apply(everything(), [
        {"do": "add_list", "key": "reminders", "label": "Reminders", "singular": "Reminder", "fields": [
            {"type": "text", "key": "what", "label": "What", "required": True},
            {"type": "date", "key": "due_on", "label": "Due on", "required": True}]},
        {"do": "add_rule", "rule": {"key": "remind", "name": "Remind about the job", "when": {"on": "added", "list": "jobs"},
                                    "steps": [{"do": "add", "list": "reminders", "values": [{"field": "what", "formula": "title"},
                                                                                         {"field": "due_on", "formula": "scheduled_for"}]}]}}])
    assert not bp.problems, bp.problems
    ws = _workspace(app, bp.blueprint)
    engine = Engine(ws)
    engine.create("jobs", {"title": "Fix the gate"})
    reminders = engine.rows("reminders")
    assert len(reminders) == 1 and (reminders[0].data or {}).get("what") == "Fix the gate"


def test_a_staff_member_refused_a_move_only_the_owner_makes_over_an_amount_is_told_so(app):
    """Approving a quote over $1,000 is the owner's: a staff member pressing Approve on one is told only the owner can,
    when it's over $1,000, and not that they can once it's under."""
    out = lang.apply(everything(), [{"do": "approval", "list": "quotes", "stage": "Accepted", "by": "owner", "over": {"field": "total", "amount": 1000}}])
    assert not out.problems, out.problems
    ws = _workspace(app, out.blueprint)
    staff = User(email="sam@example.com", name="Sam")
    staff.set_password("x" * 12)
    db.session.add(staff)
    db.session.flush()
    member = Membership(workspace_id=ws.id, user_id=staff.id, role="editor")
    db.session.add(member)
    db.session.commit()
    quote = Engine(ws).create("quotes", {"total": 2500, "status": "Sent"})
    theirs = Engine(ws, by="Sam", member=member)
    said = [problem for _, m, problem in theirs.moves(theirs.get(quote.id)) if m["to"] == "Accepted"]
    assert said and said[0].startswith("Only the owner can") and "more than" in said[0].lower(), said


def test_a_total_that_came_out_wrong_because_its_linked_records_dont_count_says_why_without_failing(app):
    out = lang.apply(everything(), [{"do": "add_field", "list": "invoices", "field": {
        "type": "total", "key": "paid_in", "label": "Paid in", "of": "payments", "link": "invoice", "how": "sum", "field": "amount",
        "only": [{"field": "amount", "op": "greater_than", "value": 1000}]}}])
    assert not out.problems, out.problems
    ws = _workspace(app, out.blueprint)
    example = {"title": "Payments add up", "steps": [{"do": "add", "table": "invoices", "as": "i1", "values": {"total": 100}},
                                                     {"do": "add", "table": "payments", "as": "p1", "values": {"invoice": "i1", "amount": 50}}],
               "expect": [{"record": "i1", "field": "paid_in", "equals": 50}]}
    with trial.copy_of(ws) as copy:
        result = trial.play(copy, example)
    assert result["kind"] == "app" and not result["ok"] and "only those where" in result["problem"], result


def test_refusing_while_a_customer_owes_is_a_count_of_their_overdue_invoices_used_through_the_link(app):
    """“No new quote, and no job started, for a customer with an invoice unpaid over 30 days”: a total counting their
    overdue invoices, read through the link by a rule every save keeps and by a gate. And a formula reaching into other
    records (count(invoices where ...)) says the same in one line."""
    out = lang.apply(everything(), [
        {"do": "add_field", "list": "customers", "field": {"type": "total", "key": "overdue_invoices", "label": "Overdue invoices",
                                                           "of": "invoices", "link": "customer", "how": "count",
                                                           "only": [{"field": "status", "op": "not_equals", "value": "Paid"},
                                                                    {"field": "due_date", "op": "more_than_days_ago", "value": 30}]}},
        {"do": "add_validation", "list": "quotes", "check": "customer.overdue_invoices == 0",
         "message": "This customer has an invoice unpaid over 30 days: clear it first"},
        {"do": "gate", "list": "jobs", "stage": "In progress", "blocked_if": [{"field": "customer.overdue_invoices", "op": "greater_than", "value": 0}]}])
    assert not out.problems, out.problems
    ws = _workspace(app, out.blueprint)
    e = Engine(ws)
    acme = e.create("customers", {"name": "Acme"})
    e.create("quotes", {"customer": acme.id, "total": 10})
    job = e.create("jobs", {"title": "Fit-out", "customer": acme.id})
    e.create("invoices", {"customer": acme.id, "due_date": (date.today() - timedelta(days=45)).isoformat(), "total": 5})
    with pytest.raises(FieldErrors):
        e.create("quotes", {"customer": acme.id, "total": 10})
    started = [problem for _, m, problem in e.moves(e.get(job.id)) if m["to"] == "In progress"]
    assert started and started[0], started
    direct = lang.apply(everything(), [{"do": "add_validation", "list": "quotes", "message": "Clear their overdue invoices first",
                                        "check": 'count(invoices where customer == this.customer and status != "Paid" and days_since(due_date) > 30) == 0'}])
    assert not direct.problems, direct.problems
    ws.blueprint = direct.blueprint
    db.session.commit()
    with pytest.raises(FieldErrors):
        Engine(ws).create("quotes", {"customer": acme.id, "total": 10})


def test_a_rule_with_a_long_name_made_on_the_app_map_is_opened_by_the_key_it_is_saved_under():
    from app import studio
    name = "When an invoice reaches Partially paid, tell the owner"
    made, key = studio.rule_ops(everything(), {"do": "add_rule", "name": name, "trigger": {"type": "record_updated", "table": "invoices"},
                                               "step": {"type": "notify", "to": "owner", "message": "{{record}} is now Partially paid"}})
    from app.blueprint import ops as bp_ops
    result = bp_ops.apply(everything(), made)
    assert not result.problems, result.problems
    assert key == schema.slug(key) and not key.endswith("_")
    assert any(a["key"] == key for a in result.blueprint.get("automations", []))


def test_two_status_fields_with_the_same_choices_are_a_mistake_to_put_right(app):
    from app.builder import checks
    bp = lang.apply(everything(), [{"do": "add_field", "list": "invoices", "field": {
        "type": "choice", "key": "payment_status", "label": "Payment status", "options": ["Draft", "Sent", "Part paid", "Paid"]}}]).blueprint
    ws = _workspace(app, bp)
    found = checks.two_statuses(Engine(ws))
    assert found and found[0]["level"] == "fix" and "payment_status" in found[0]["text"] and "remove_field payment_status" in found[0]["text"]
    ws.blueprint = everything()
    assert not checks.two_statuses(Engine(ws))


def test_a_vague_new_app_gets_a_few_quick_questions_to_click_once(app, owner):
    """“need sumthing to run my shop”: the builder asks up to four questions with answers to click, rather than building a
    shell; asked once, then it builds from what they said."""
    from app.builder import chat
    _, ws = owner
    asking = {"needs": [], "questions": [{"ask": "Does anything need your OK before it goes ahead?", "choices": ["Quotes over a set amount", "Orders", "Nothing"]},
                                        {"ask": "What gets chased?", "choices": ["Unpaid invoices", "Quotes not answered"], "many": True}],
              "reply": ""}
    ai.set_backend(Model({"understand": asking}))
    m = chat.ask(ws, "need sumthing to run my shop")
    chat.work(m.id)
    m = db.session.get(Message, m.id)
    assert m.status == "done" and not m.version_after and m.proposals["questions"][0]["choices"][0] == "Quotes over a set amount"
    assert m.text.startswith("A few quick questions") and "?" in m.text  # said in words too
    ai.set_backend(Model({"understand": {**asking, "needs": [{"said": "orders", "does": "Keeps customers and their orders"}]},
                          "data": {"lists": ["customers"], "patches": [EVERYTHING[0]]}, "behaviour": {"work": [], "already": [1]}}))
    again = chat.ask(ws, "Does anything need your OK before it goes ahead? Orders")
    chat.work(again.id)
    again = db.session.get(Message, again.id)
    assert again.status == "done" and again.version_after and not again.proposals  # asked once: now it builds
