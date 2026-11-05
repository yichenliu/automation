"""The Keel language (app/lang): patches compile to the design, every name is checked, features make their parts and
prove themselves in the simulator, and every piece the registry has is one the AI can use (the guard)."""
import json

import pytest
from pydantic import TypeAdapter

from app import ai, lang
from app.blueprint import schema
from app.builder import trial
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
    bp = lang.apply(bp, [{"do": "add_rule", "rule": {"key": "job_done_invoice", "name": "Invoice", "when": {"on": "reaches", "list": "jobs", "stage": "Done"},
                                                     "steps": [{"do": "notify", "message": "Done"}]}},
                         {"do": "stage_alert", "list": "invoices", "stage": "Paid", "key": "alert_invoices_paid"}]).blueprint
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
    app.config["BUILDER"] = "v2"
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
    app.config["BUILDER"] = "v2"
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
    app.config["BUILDER"] = "v2"
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
    app.config["BUILDER"] = "v2"
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
    app.config["BUILDER"] = "v2"
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
    app.config["BUILDER"] = "v2"
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
    app.config["BUILDER"] = "v2"
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


def test_an_approval_records_who_let_it_through_and_when(app):
    bp = lang.apply(everything(), [{"do": "approval", "list": "quotes", "stage": "Sent", "by": "owner"}]).blueprint
    ws = _workspace(app, bp)
    example = {"title": "Approved and recorded", "steps": [
        {"do": "add", "table": "quotes", "as": "q1", "values": {"total": 100}}, {"do": "move", "record": "q1", "to": "Sent"}],
        "expect": [{"record": "q1", "field": "sent_by", "equals": "filled in"}, {"record": "q1", "field": "sent_on", "equals": "today"}]}
    _, results = trial.try_out(ws, [example], screens=False)
    assert results[0]["ok"], results[0].get("problem")
