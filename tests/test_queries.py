"""Formulas that look at other records: the records linking to one (a client's invoices), or a whole list, counted,
added up or tested one by one. The rules people state about their business are mostly about other records ("no
bookings while the client owes", "no stylist booked twice at once"), so a rule can say them in one line."""
from datetime import date, timedelta

import pytest

from app import lang
from app.blueprint import expr, schema
from app.engine.records import Engine, FieldErrors
from app.extensions import db
from app.models import Membership, User, Workspace

APP = [
    {"do": "add_list", "key": "clients", "label": "Clients", "singular": "Client", "fields": [
        {"type": "text", "key": "name", "label": "Name", "required": True}]},
    {"do": "add_list", "key": "invoices", "label": "Invoices", "singular": "Invoice", "fields": [
        {"type": "link", "key": "client", "label": "Client", "to": "clients", "required": True},
        {"type": "money", "key": "amount", "label": "Amount"}, {"type": "date", "key": "due_date", "label": "Due date"},
        {"type": "choice", "key": "status", "label": "Status", "options": ["Unpaid", "Paid"], "default": "Unpaid"}]},
    {"do": "add_list", "key": "payments", "label": "Payments", "singular": "Payment", "fields": [
        {"type": "link", "key": "invoice", "label": "Invoice", "to": "invoices", "required": True},
        {"type": "money", "key": "amount", "label": "Amount", "required": True}]},
    {"do": "add_list", "key": "bookings", "label": "Bookings", "singular": "Booking", "fields": [
        {"type": "link", "key": "client", "label": "Client", "to": "clients", "required": True},
        {"type": "text", "key": "stylist", "label": "Stylist"},
        {"type": "datetime", "key": "starts_at", "label": "Starts at"}, {"type": "datetime", "key": "ends_at", "label": "Ends at"}]},
]


def _workspace(bp):
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


def _app(extra):
    out = lang.apply(schema.empty("Salon"), APP + extra)
    assert not out.problems, out.problems
    return out.blueprint


def test_where_reads_as_a_filter_whatever_the_case_or_quotes():
    assert expr.normalise('count(client.invoices where status = "Unpaid")') == 'count(where(client.invoices, status == "Unpaid"))'
    assert expr.normalise('sum(job.payments WHERE kind = "Deposit", amount)') == 'sum(where(job.payments, kind == "Deposit"), amount)'
    assert expr.normalise('note == "where to?"') == 'note == "where to?"'


def test_no_booking_while_the_client_owes(app):
    bp = _app([{"do": "add_validation", "list": "bookings", "message": "This client owes on an invoice over 30 days old",
                "check": 'count(client.invoices where status = "Unpaid" and days_since(due_date) > 30) == 0'}])
    ws = _workspace(bp)
    e = Engine(ws, by="Olive")
    sam = e.create("clients", {"name": "Sam"})
    old = e.create("invoices", {"client": sam.id, "amount": 100, "due_date": (date.today() - timedelta(days=45)).isoformat()})
    e.create("invoices", {"client": sam.id, "amount": 50, "due_date": date.today().isoformat()})  # not overdue: no matter
    with pytest.raises(FieldErrors) as refused:
        e.create("bookings", {"client": sam.id, "stylist": "Ann"})
    assert "owes" in str(refused.value.errors)
    e.update(e.get(old.id), {"status": "Paid"})
    assert e.create("bookings", {"client": sam.id, "stylist": "Ann"}).id


def test_no_stylist_booked_twice_at_once(app):
    bp = _app([{"do": "add_validation", "list": "bookings", "message": "That stylist is booked then",
                "check": "not any(bookings where stylist == this.stylist and id != this.id and overlaps(starts_at, ends_at, this.starts_at, this.ends_at))"}])
    ws = _workspace(bp)
    e = Engine(ws, by="Olive")
    sam = e.create("clients", {"name": "Sam"})
    e.create("bookings", {"client": sam.id, "stylist": "Ann", "starts_at": "2026-12-01 09:00", "ends_at": "2026-12-01 10:00"})
    with pytest.raises(FieldErrors):
        e.create("bookings", {"client": sam.id, "stylist": "Ann", "starts_at": "2026-12-01 09:30", "ends_at": "2026-12-01 10:30"})
    assert e.create("bookings", {"client": sam.id, "stylist": "Bo", "starts_at": "2026-12-01 09:30", "ends_at": "2026-12-01 10:30"}).id
    assert e.create("bookings", {"client": sam.id, "stylist": "Ann", "starts_at": "2026-12-01 10:00", "ends_at": "2026-12-01 11:00"}).id


def test_a_formula_adds_up_the_records_linking_to_it(app):
    bp = _app([{"do": "add_field", "list": "invoices", "field": {"type": "formula", "key": "paid", "label": "Paid", "formula": "sum(payments, amount)", "shows": "money"}},
               {"do": "add_field", "list": "invoices", "field": {"type": "formula", "key": "balance", "label": "Balance", "formula": "amount - sum(payments.amount)", "shows": "money"}}])
    ws = _workspace(bp)
    e = Engine(ws, by="Olive")
    sam = e.create("clients", {"name": "Sam"})
    inv = e.create("invoices", {"client": sam.id, "amount": 300})
    e.create("payments", {"invoice": inv.id, "amount": 100})
    e.create("payments", {"invoice": inv.id, "amount": 50})
    inv = e.get(inv.id)
    assert e.value(inv, "paid") == 150 and e.value(inv, "balance") == 150


def test_a_name_that_isnt_there_says_what_is(app):
    out = lang.apply(schema.empty("Salon"), APP + [{"do": "add_validation", "list": "bookings", "message": "x",
                                                    "check": 'count(client.invoices where state = "Unpaid") == 0'}])
    assert out.problems and "state" in out.problems[0] and "status" in out.problems[0]
    out = lang.apply(schema.empty("Salon"), APP + [{"do": "add_validation", "list": "bookings", "message": "x",
                                                    "check": "count(client.bills) == 0"}])
    assert out.problems and "invoices" in out.problems[0]  # what links to a client is named


JOBS = [{"do": "add_list", "key": "jobs", "label": "Jobs", "singular": "Job", "fields": [
    {"type": "link", "key": "client", "label": "Client", "to": "clients", "required": True},
    {"type": "yes_no", "key": "deposit_paid", "label": "Deposit paid"}, {"type": "text", "key": "notes", "label": "Notes"}],
    "stages": {"stages": ["Booked", "In progress", "Done"]}}]


def _owner_engine(ws):
    member = Membership.query.filter_by(workspace_id=ws.id).first()
    return Engine(ws, by="Olive", member=member)


def test_a_gate_holds_for_the_owner_and_for_the_apps_own_rules(app):
    """A gate used to be only the buttons staff press: the owner, the board and the app's rules went straight past
    it. Now it's a rule every save keeps."""
    bp = _app(JOBS + [{"do": "gate", "list": "jobs", "stage": "In progress", "needs": ["deposit_paid"]}])
    ws = _workspace(bp)
    owner, platform = _owner_engine(ws), Engine(ws)
    sam = owner.create("clients", {"name": "Sam"})
    job = owner.create("jobs", {"client": sam.id})
    for e in (owner, platform):
        with pytest.raises(FieldErrors) as refused:
            e.update(e.get(job.id), {"status": "In progress"})
        assert "Deposit paid must be ticked" in str(refused.value.errors)
    owner.update(owner.get(job.id), {"deposit_paid": True})
    owner.update(owner.get(job.id), {"status": "In progress"})
    assert owner.get(job.id).data["status"] == "In progress"


def test_a_gate_on_what_other_records_say_and_a_change_beside_the_point(app):
    """“No job starts while the client owes on an invoice over 30 days”, in one line; a job already started when the
    client fell behind can still have its notes changed, but not be moved back into the stage."""
    bp = _app(JOBS + [{"do": "gate", "list": "jobs", "stage": "In progress",
                       "blocked_if": ['count(client.invoices where status = "Unpaid" and days_since(due_date) > 30) > 0']}])
    ws = _workspace(bp)
    e = _owner_engine(ws)
    sam = e.create("clients", {"name": "Sam"})
    job = e.create("jobs", {"client": sam.id})
    e.update(e.get(job.id), {"status": "In progress"})  # nothing owed yet
    e.create("invoices", {"client": sam.id, "amount": 80, "due_date": (date.today() - timedelta(days=40)).isoformat()})
    e.update(e.get(job.id), {"notes": "Bring the ladder"})  # beside the point: kept
    e.update(e.get(job.id), {"status": "Booked"})
    with pytest.raises(FieldErrors) as refused:
        e.update(e.get(job.id), {"status": "In progress"})
    assert "can't move to In progress while" in str(refused.value.errors)
    other = e.create("jobs", {"client": sam.id})
    with pytest.raises(FieldErrors):
        e.update(e.get(other.id), {"status": "In progress"})


def test_only_if_that_cant_be_turned_round_still_holds(app):
    bp = _app(JOBS + [{"do": "add_field", "list": "jobs", "field": {"type": "date", "key": "starts_on", "label": "Starts on"}},
                      {"do": "gate", "list": "jobs", "stage": "In progress", "only_if": [{"field": "starts_on", "op": "within_next_days", "value": 7}]}])
    ws = _workspace(bp)
    e = _owner_engine(ws)
    sam = e.create("clients", {"name": "Sam"})
    job = e.create("jobs", {"client": sam.id, "starts_on": (date.today() + timedelta(days=30)).isoformat()})
    with pytest.raises(FieldErrors):
        e.update(e.get(job.id), {"status": "In progress"})
    e.update(e.get(job.id), {"starts_on": (date.today() + timedelta(days=2)).isoformat()})
    e.update(e.get(job.id), {"status": "In progress"})


def test_an_app_made_with_the_old_gate_has_it_made_again_as_a_rule(app):
    from app.lang import patterns
    bp = _app(JOBS + [{"do": "gate", "list": "jobs", "stage": "In progress", "needs": ["deposit_paid"]}])
    jobs = schema.table(bp, "jobs")
    jobs["rules"] = []  # as it was made before: only the buttons held back
    for entry in bp["features"]:
        entry.pop("made", None)
    again, said = patterns.refresh(bp)
    assert said and any("deposit" in r["message"].lower() for r in schema.table(again, "jobs")["rules"])
    assert patterns.refresh(again)[1] == []  # made as the platform makes it now: nothing more to do


def test_stages_nothing_reaches_or_leaves_get_their_buttons(app):
    bp = _app([{"do": "add_list", "key": "orders", "label": "Orders", "singular": "Order", "fields": [{"type": "text", "key": "name", "label": "Name"}],
                "stages": {"stages": ["New", "Approved", "Packed", "Delivered", "Cancelled"],
                           "moves": [{"from": ["New"], "to": "Approved", "button": "Approve"}]}}])
    moves = schema.table(bp, "orders")["workflow"]["transitions"]
    to = {m["to"] for m in moves}
    assert {"Approved", "Packed", "Delivered", "Cancelled"} <= to, moves
    assert any("Approved" in m["from"] and m["to"] == "Packed" for m in moves)


def test_waiting_days_runs_the_daily_rules_with_no_run_step(app):
    """Time passes in a try-out as it does in the app: each day of a wait the rules that run on a schedule run, so an
    owner's check needn't know which rule to run."""
    from app.builder import trial
    bp = _app([{"do": "add_list", "key": "invoices2", "label": "Bills", "singular": "Bill", "fields": [
        {"type": "text", "key": "name", "label": "Name"}, {"type": "date", "key": "due_date", "label": "Due date"}],
        "stages": {"stages": ["Sent", "Overdue", "Paid"]}},
        {"do": "overdue", "list": "invoices2", "date_field": "due_date", "from_stages": ["Sent"], "to_stage": "Overdue"}])
    ws = _workspace(bp)
    example = {"title": "A bill not paid 5 days after it's due is overdue", "steps": [
        {"do": "add", "table": "invoices2", "as": "b1", "values": {"name": "B1", "due_date": "today+2"}},
        {"do": "wait", "days": 7}], "expect": [{"record": "b1", "field": "status", "equals": "Overdue"}]}
    _, results = trial.try_out(ws, [example], screens=False)
    assert results[0]["ok"], results[0]


def test_a_formula_comparing_a_choice_with_words_that_arent_one_is_sent_back(app):
    """count(client.invoices where status = "Overdue") would never count anything when Overdue isn't a status."""
    out = lang.apply(schema.empty("Salon"), APP + [{"do": "add_validation", "list": "bookings", "message": "x",
                                                    "check": 'count(client.invoices where status = "Overdue") == 0'}])
    assert out.problems and "Overdue" in out.problems[0] and "Unpaid, Paid" in out.problems[0]
    ok = lang.apply(schema.empty("Salon"), APP + [{"do": "add_validation", "list": "bookings", "message": "x",
                                                   "check": "count(client.invoices where status in ['unpaid']) == 0"}])
    assert not ok.problems, ok.problems


def test_a_gate_needing_a_formula_holds_to_it(app):
    bp = _app(JOBS + [{"do": "add_list", "key": "deposits", "label": "Deposits", "singular": "Deposit", "fields": [
        {"type": "link", "key": "job", "label": "Job", "to": "jobs", "required": True}, {"type": "money", "key": "amount", "label": "Amount"}]},
        {"do": "gate", "list": "jobs", "stage": "In progress", "needs": ["count(deposits) > 0"]}])
    ws = _workspace(bp)
    e = _owner_engine(ws)
    sam = e.create("clients", {"name": "Sam"})
    job = e.create("jobs", {"client": sam.id})
    with pytest.raises(FieldErrors):
        e.update(e.get(job.id), {"status": "In progress"})
    e.create("deposits", {"job": job.id, "amount": 50})
    e.update(e.get(job.id), {"status": "In progress"})


def test_a_check_that_comes_out_wrong_says_why_in_the_apps_own_terms(app):
    """The builder is told what in the app was about it: the rules on the list and what each found, or the rule that
    sets the field and what it was waiting for, so a fix changes what's really wrong."""
    from app.builder import trial
    bp = _app(JOBS + [{"do": "add_validation", "list": "jobs", "message": "Not for Sam", "check": "notes != 'sam'"},
                      {"do": "add_rule", "rule": {"key": "start_when_paid", "name": "Start a job once its deposit is paid",
                                                  "when": {"on": "changed", "list": "jobs", "field": "deposit_paid"},
                                                  "only_if": [{"field": "deposit_paid", "op": "equals", "value": True}, {"field": "notes", "op": "equals", "value": "go"}],
                                                  "steps": [{"do": "set", "values": [{"field": "status", "to": "In progress"}]}]}}])
    ws = _workspace(bp)
    allowed = {"title": "A job can't start before its deposit is paid", "steps": [
        {"do": "add", "table": "clients", "as": "c1", "values": {"name": "Ann"}},
        {"do": "add", "table": "jobs", "as": "j1", "values": {"client": "c1"}},
        {"do": "move", "record": "j1", "to": "In progress", "refused": True}], "expect": []}
    stays = {"title": "Paying the deposit starts the job", "steps": [
        {"do": "add", "table": "clients", "as": "c1", "values": {"name": "Ann"}},
        {"do": "add", "table": "jobs", "as": "j1", "values": {"client": "c1"}},
        {"do": "change", "record": "j1", "values": {"deposit_paid": True}}],
        "expect": [{"record": "j1", "field": "status", "equals": "In progress"}]}
    _, (first, second) = trial.try_out(ws, [allowed, stays], screens=False)
    assert not first["ok"] and any("Not for Sam" in w and "held" in w for w in first["why"]), first
    assert not second["ok"] and any("start_when_paid" in w and "didn't run" in w and "notes" in w for w in second["why"]), second
