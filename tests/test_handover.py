"""What stopped apps being right when handed over (evals/analyst.py's headline), each kept fixed. Most of it was the
builder's own worked examples failing for reasons that weren't the app's: a person they named who wasn't in the copy, an
"approve" where the record's own button does it, an email the copy couldn't send. The rest was the platform's: an
approval named for the one person kept from it, shift hours between two times, amounts in another currency with no
rates. Every fix is the platform's, for any app."""
import json

from app.blueprint import ops
from app.blueprint.expr import _hours_between
from app.builder import checks, trial
from app.engine import automations
from app.engine.records import Engine, parse_duration
from app.extensions import db
from app.models import Approval, Notification

from .conftest import login
from .test_wave2 import PURCHASES, _as, _built, _person

REQUESTS = [
    {"op": "create_table", "table": "purchase_requests", "label": "Purchase requests", "fields": [
        {"key": "title", "type": "text", "required": True},
        {"key": "team_lead", "label": "Team lead", "type": "user"},
        {"key": "total", "type": "money"},
        {"key": "rejected_reason", "label": "Why it was rejected", "type": "text"},
        {"key": "status", "type": "select", "options": ["Draft", "Waiting for approval", "Approved", "Rejected"], "default": "Draft"}],
     "workflow": {"field": "status", "transitions": [{"from": ["Draft", "Rejected"], "to": "Waiting for approval", "label": "Send for approval"}]}},
    {"op": "add_automation", "automation": {
        "key": "route", "name": "The team lead approves", "trigger": {"type": "record_updated", "table": "purchase_requests"},
        "conditions": [{"field": "status", "op": "changed_to", "value": "Waiting for approval"}],
        "actions": [{"type": "request_approval", "approver": "team_lead", "message": "Please approve this.", "not_by": ["creator"],
                     "if_rejected": [{"type": "update_record", "values": {"status": "Rejected", "rejected_reason": "{{reason}}"}}]},
                    {"type": "update_record", "values": {"status": "Approved"}}]}},
]


def test_an_approval_for_whoever_raised_it_goes_to_an_admin_and_a_no_says_why(app, owner):
    user, ws = owner
    _built(ws, REQUESTS)
    ava = _person(ws, "ava@example.com", "Ava", "admin")
    mine = Engine(ws, by="Ethan", member=None)
    me = _as(ws, user)
    request = me.create("purchase_requests", {"title": "Laptop", "team_lead": user.id, "total": 1500})
    me.move(me.get(request.id), 0)
    approval = Approval.query.one()  # not left with Ethan, who may not answer it: it would wait for ever
    assert approval.assignee_id is None and approval.role == "admin"
    assert "Ethan can't answer it: they raised this purchase request" in approval.history[0]["what"]
    assert automations.may_decide(approval, _as(ws, ava).member, ava.id, mine.get(request.id), _as(ws, ava))
    automations.decide(_as(ws, ava).system(), approval, False, "Ava", comment="The quote is over 30 days old", by_id=ava.id)
    assert mine.get(request.id).data["status"] == "Rejected"
    assert mine.get(request.id).data["rejected_reason"] == "The quote is over 30 days old"  # the rule's {{reason}}


def test_examples_name_people_press_the_records_own_button_and_say_when_no_ok_was_needed(app, owner):
    _, ws = owner
    _built(ws, REQUESTS + PURCHASES + [
        {"op": "create_table", "table": "variations", "label": "Variations", "fields": [
            {"key": "description", "type": "text", "required": True},
            {"key": "status", "type": "select", "options": ["Proposed", "Client signed off", "Rejected"], "default": "Proposed"}],
         "workflow": {"field": "status", "transitions": [
             {"from": ["Proposed"], "to": "Client signed off", "label": "Client signs off"},
             {"from": ["Proposed"], "to": "Rejected", "label": "Reject"}]}},
        {"op": "create_table", "table": "notes", "label": "Notes", "fields": [{"key": "text", "type": "text", "required": True}]}])
    named = {"title": "Priya, the team lead, approves a laptop Ethan asked for", "steps": [
        {"do": "add", "as": "r", "table": "purchase_requests", "values": {"title": "Laptop", "team_lead": "Priya", "total": 1500}},
        {"do": "move", "as": "", "record": "r", "to": "Waiting for approval"},
        {"do": "approve", "as": "", "record": "r"}],
        "expect": [{"record": "r", "field": "status", "equals": "Approved"}]}
    rejected = {"title": "Priya says no, and why", "steps": [
        {"do": "add", "as": "r", "table": "purchase_requests", "values": {"title": "Chairs", "team_lead": "Priya", "total": 900}},
        {"do": "move", "as": "", "record": "r", "to": "Waiting for approval"},
        {"do": "reject", "as": "", "record": "r", "comment": "Too dear"}],
        "expect": [{"record": "r", "field": "rejected_reason", "equals": "Too dear"}]}
    signed = {"title": "The client signs off a variation", "steps": [
        {"do": "add", "as": "v", "table": "variations", "values": {"description": "Quartz benchtop"}},
        {"do": "approve", "as": "", "record": "v"}],
        "expect": [{"record": "v", "field": "status", "equals": "Client signed off"}]}
    small = {"title": "A $300 purchase order", "steps": [
        {"do": "add", "as": "po", "table": "purchase_orders", "values": {"supplier": "Bunnings"}},
        {"do": "add", "as": "l", "table": "po_lines", "values": {"po": "po", "item": "Screws", "quantity": 3, "unit_cost": 100}},
        {"do": "move", "as": "", "record": "po", "to": "Waiting for approval"},
        {"do": "approve", "as": "", "record": "po"}],
        "expect": [{"record": "po", "field": "status", "equals": "Approved"}]}
    note = {"title": "Approving a note", "steps": [
        {"do": "add", "as": "n", "table": "notes", "values": {"text": "Hello"}},
        {"do": "approve", "as": "", "record": "n"}],
        "expect": [{"record": "n", "field": "text", "equals": "Hello"}]}
    _, (by_priya, why, button, under, nothing) = trial.try_out(ws, [named, rejected, signed, small, note], screens=False)
    assert by_priya["ok"] and why["ok"] and button["ok"], (by_priya, why, button)
    assert under["kind"] == "example" and "didn't need anyone's OK this time" in under["problem"] and "Under" in under["problem"]
    assert nothing["kind"] == "example" and "nothing asks for someone's OK on a note" in nothing["problem"]


def test_shift_hours_long_durations_and_emails_in_a_try_out(app, owner):
    _, ws = owner
    assert _hours_between("09:00", "12:00") == 3 and _hours_between("22:00", "06:00") == 8 and _hours_between("2:30 pm", "5pm") == 2.5
    assert _hours_between("2026-11-03T09:00", "2026-11-03T17:30") == 8.5
    assert parse_duration("01:30:00") == 1.5 and parse_duration("1:30") == 1.5
    _built(ws, [
        {"op": "create_table", "table": "donations", "label": "Donations", "fields": [
            {"key": "email", "type": "email", "required": True}, {"key": "receipt_sent", "label": "Receipt sent", "type": "checkbox"}]},
        {"op": "add_automation", "automation": {
            "key": "receipt", "name": "Send a receipt", "trigger": {"type": "record_created", "table": "donations"},
            "actions": [{"type": "send_email", "to": "{{record.email}}", "subject": "Thank you", "body": "Your receipt."},
                        {"type": "update_record", "values": {"receipt_sent": True}}]}}])
    receipt = {"title": "A donation gets its receipt", "steps": [{"do": "add", "as": "d", "table": "donations", "values": {"email": "jo@example.com"}}],
               "expect": [{"record": "d", "field": "receipt_sent", "equals": True}]}
    _, (sent,) = trial.try_out(ws, [receipt], screens=False)
    assert sent["ok"], sent  # the copy acts as if the mailbox were connected, and sends nothing


def test_amounts_in_another_currency_need_rates_and_getting_started_says_so(app, owner, client):
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "purchase_orders", "label": "Purchase orders", "fields": [
        {"key": "supplier", "type": "text", "required": True}, {"key": "currency", "type": "select", "options": ["AUD", "USD"]},
        {"key": "subtotal", "type": "money"},
        {"key": "total", "type": "formula", "format": "money", "expression": "convert(subtotal, currency)"}]}])
    said = [i["plain"] for i in checks.needs_rates(Engine(ws))]
    assert said == ["Amounts in other currencies stay empty until you add your exchange rates in Settings › General"]
    login(client)
    assert "Add your exchange rates" in client.get(f"/w/{ws.id}/app/").get_data(as_text=True)
    ws.settings = {**ws.settings, "money": {"base": "AUD", "rates": {"USD": 1.5}}}
    db.session.commit()
    assert not checks.needs_rates(Engine(ws))


# A childcare centre's application, approved by its button: a child is added for it, and the absence it stands for
# records who approved it. An invoice issued on the 10th is due on the 24th, however many days the example then waits.
CENTRE = [
    {"op": "create_table", "table": "children", "label": "Children", "fields": [
        {"key": "first_name", "label": "First name", "type": "text", "required": True},
        {"key": "last_name", "label": "Last name", "type": "text", "required": True}]},
    {"op": "create_table", "table": "applications", "label": "Applications", "fields": [
        {"key": "child_name", "label": "Child's name", "type": "text", "required": True},
        {"key": "approved_by", "label": "Approved by", "type": "user"},
        {"key": "approved_on", "label": "Approved on", "type": "date"},
        {"key": "status", "type": "select", "options": ["Pending", "Approved", "Rejected"], "default": "Pending"}],
     "workflow": {"field": "status", "transitions": [{"from": ["Pending"], "to": "Approved", "label": "Approve"},
                                                     {"from": ["Pending"], "to": "Rejected", "label": "Reject"}]}},
    {"op": "add_automation", "automation": {
        "key": "enrol", "name": "Approved applications become children", "trigger": {"type": "record_updated", "table": "applications"},
        "conditions": [{"field": "status", "op": "changed_to", "value": "Approved"}],
        "actions": [{"type": "create_record", "table": "children", "values": {"first_name": "{{record.child_name}}"}}]}},
    {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
        {"key": "issued", "type": "date", "required": True},
        {"key": "due", "type": "formula", "format": "date", "expression": "add_days(issued, 14)"},
        {"key": "status", "type": "select", "options": ["Sent", "Overdue"], "default": "Sent"}]},
    {"op": "add_automation", "automation": {
        "key": "chase", "name": "Overdue invoices", "trigger": {"type": "schedule", "table": "invoices", "every": "day", "at": "08:00"},
        "conditions": [{"field": "status", "op": "equals", "value": "Sent"}, {"field": "due", "op": "before_today"}],
        "actions": [{"type": "update_record", "values": {"status": "Overdue"}}]}},
]


def test_a_rule_missing_what_it_must_fill_is_found_a_stage_records_who_and_dates_stay_on_the_calendar(app, owner):
    user, ws = owner
    _built(ws, CENTRE)
    said = [i for i in checks.left_unfilled(Engine(ws)) if i["level"] == "fix"]
    assert [i["plain"] for i in said] == ["“Approved applications become children” can't add a child: it doesn't fill in last name, "
                                          "which must be filled in"]
    me = _as(ws, user)
    application = me.create("applications", {"child_name": "Oliver"})
    me.move(me.get(application.id), 0)
    data = me.get(application.id).data
    assert data["approved_by"] == user.id and data["approved_on"] == me.today.isoformat()  # the button records who and when
    today = me.today
    issued = (today.replace(day=1)).isoformat()
    due = (today.replace(day=1) + __import__("datetime").timedelta(days=14)).isoformat()
    late = {"title": "An invoice not paid 30 days after it's due is overdue", "steps": [
        {"do": "add", "as": "i", "table": "invoices", "values": {"issued": issued}},
        {"do": "wait", "as": "", "days": 45},
        {"do": "run", "as": "", "rule": "chase"}],
        "expect": [{"record": "i", "field": "due", "equals": due}, {"record": "i", "field": "status", "equals": "Overdue"}]}
    twice = {"title": "Approving makes a child", "steps": [
        {"do": "add", "as": "a", "table": "applications", "values": {"child_name": "Mia"}},
        {"do": "move", "as": "", "record": "a", "to": "Approved"},
        {"do": "run", "as": "", "rule": "enrol"}],
        "expect": [{"table": "children", "count": 1}]}
    _, (on_time, run_too) = trial.try_out(ws, [late, twice], screens=False)
    assert on_time["ok"], on_time  # still due on the calendar date, though 45 days were let pass
    assert run_too["kind"] == "example" and "“run” is for scheduled automations" in run_too["problem"]


def test_an_ok_from_to_do_moves_the_record_on_when_an_older_rule_doesnt(app, owner):
    user, ws = owner
    older = json.loads(json.dumps(PURCHASES))  # built before the builder added "then Approved" after each approval
    older[0]["workflow"]["transitions"].append({"from": ["Waiting for approval"], "to": "Approved", "label": "Approve", "who": ["owner"]})
    over = older[2]["automation"]["actions"][0]["paths"][0]
    over["steps"] = [over["steps"][0]]
    ws.blueprint = ops.apply(ws.blueprint, older).blueprint
    ws.blueprint["automations"][0]["actions"][0]["paths"][0]["steps"] = [over["steps"][0]]  # as it was saved back then
    db.session.commit()
    buyer = _person(ws, "kim@example.com", "Kim", "editor")
    kim = _as(ws, buyer)
    po = kim.create("purchase_orders", {"supplier": "Bunnings"})
    kim.create("po_lines", {"po": po.id, "item": "Timber", "quantity": 2, "unit_cost": 4000})
    kim.move(kim.get(po.id), 0)
    me = _as(ws, user)
    assert me.get(po.id).data["status"] == "Waiting for approval"
    automations.decide(me.system(), Approval.query.one(), True, "Ethan", by_id=user.id)  # from To do, not the button
    assert me.get(po.id).data["status"] == "Approved"


# A plumber's jobs and quotes, linked both ways; the invoice for a finished job made only when it came from a quote; a
# shop's low-stock count comparing two of a product's own numbers.
JOBS = [
    {"op": "create_table", "table": "jobs", "label": "Jobs", "fields": [
        {"key": "title", "type": "text", "required": True},
        {"key": "status", "type": "select", "options": ["Booked", "Completed"], "default": "Booked"}],
     "workflow": {"field": "status", "transitions": [{"from": ["Booked"], "to": "Completed", "label": "Mark done"}]}},
    {"op": "create_table", "table": "quotes", "label": "Quotes", "fields": [
        {"key": "customer", "type": "text", "required": True}, {"key": "job", "type": "link", "table": "jobs"}]},
    {"op": "add_field", "table": "jobs", "field": {"key": "quote", "type": "link", "table": "quotes"}},
    {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
        {"key": "job", "type": "link", "table": "jobs", "required": True}]},
    {"op": "add_automation", "automation": {
        "key": "invoice", "name": "Draft invoice when job is done", "trigger": {"type": "record_updated", "table": "jobs"},
        "conditions": [{"field": "status", "op": "changed_to", "value": "Completed"}],
        "actions": [{"type": "branch", "paths": [{"label": "Has a quote", "when": [{"field": "quote", "op": "not_empty"}],
                                                   "steps": [{"type": "create_record", "table": "invoices", "values": {"job": "{{record}}"}}]}]}]}},
    {"op": "create_table", "table": "products", "label": "Products", "fields": [
        {"key": "name", "type": "text", "required": True}, {"key": "on_hand", "label": "On hand", "type": "number"},
        {"key": "reorder_level", "label": "Reorder level", "type": "number"}]},
]


def test_links_both_ways_stay_in_step_a_making_rule_for_only_some_is_found_and_a_field_named_bare_is_that_field(app, owner):
    user, ws = owner
    _built(ws, JOBS)
    me = _as(ws, user)
    job = me.create("jobs", {"title": "Burst pipe"})
    quote = me.create("quotes", {"customer": "Ann", "job": job.id})
    assert me.get(job.id).data["quote"] == quote.id  # the quote names the job, so the job names the quote
    other = me.create("jobs", {"title": "Leaking tap"})
    me.update(me.get(quote.id), {"job": other.id})
    assert me.get(other.id).data.get("quote") == quote.id and me.get(job.id).data["quote"] == quote.id  # filled where empty only
    said = [i["plain"] for i in checks.made_only_sometimes(Engine(ws))]
    assert said == ["“Draft invoice when job is done” makes an invoice only when Quote is filled in: any other job gets none"]
    low = me.create("products", {"name": "Tea", "on_hand": 2, "reorder_level": 5})
    me.create("products", {"name": "Milk", "on_hand": 9, "reorder_level": 5})
    wanted = [{"field": "on_hand", "op": "less_than", "value": "reorder_level"}]
    assert [r.id for r in me.rows("products") if me.matches(r, wanted)] == [low.id]
    assert automations.describe_condition(me, wanted[0], "products") == "On hand is less than its reorder level"


def test_an_email_with_nobody_to_send_it_to_is_left_out_and_said_and_a_per_line_rule_from_something_optional_is_found(app, owner):
    user, ws = owner
    _built(ws, [
        {"op": "create_table", "table": "customers", "label": "Customers", "fields": [
            {"key": "name", "type": "text", "required": True}, {"key": "email", "type": "email"}]},
        {"op": "create_table", "table": "warehouses", "label": "Warehouses", "fields": [{"key": "name", "type": "text", "required": True}]},
        {"op": "create_table", "table": "orders", "label": "Orders", "fields": [
            {"key": "customer", "type": "link", "table": "customers", "required": True},
            {"key": "warehouse", "type": "link", "table": "warehouses"}, {"key": "sent", "type": "checkbox"}]},
        {"op": "create_table", "table": "order_lines", "label": "Order lines", "embedded": True, "fields": [
            {"key": "order", "type": "link", "table": "orders", "required": True}, {"key": "item", "type": "text"},
            {"key": "quantity", "type": "number", "required": True}]},
        {"op": "create_table", "table": "stock_movements", "label": "Stock movements", "fields": [
            {"key": "item", "type": "text"}, {"key": "warehouse", "type": "link", "table": "warehouses", "required": True},
            {"key": "quantity", "type": "number", "required": True}]},
        {"op": "add_automation", "automation": {
            "key": "confirm", "name": "Confirm the order", "trigger": {"type": "record_created", "table": "orders"},
            "actions": [{"type": "send_email", "to": "{{record.customer.email}}", "subject": "Your order", "body": "Thanks."},
                        {"type": "update_record", "values": {"sent": True}}]}},
        {"op": "add_automation", "automation": {
            "key": "stock_out", "name": "Orders take stock out", "trigger": {"type": "record_updated", "table": "orders"},
            "actions": [{"type": "create_for_each", "table": "stock_movements", "source": {"table": "order_lines", "via": "order"},
                         "values": {"item": "{{item.item}}", "quantity": "{{item.quantity}}", "warehouse": "{{record.warehouse}}"}}]}}])
    me = _as(ws, user)
    walk_in = me.create("customers", {"name": "Walk-in"})
    order = me.create("orders", {"customer": walk_in.id})
    assert me.get(order.id).data["sent"] is True  # the rule carried on past the email it couldn't send
    note = Notification.query.filter(Notification.text.like("%an email wasn't sent%")).first()
    assert note is not None and "is empty" in note.text and "Your order" in note.text
    said = [i["plain"] for i in checks.left_unfilled(Engine(ws))]
    assert "“Orders take stock out” stops when an order's warehouse is left empty" in said


def test_an_ai_step_asked_to_do_sums_is_found(app, owner):
    _, ws = owner
    _built(ws, [
        {"op": "create_table", "table": "work_orders", "label": "Work orders", "fields": [
            {"key": "title", "type": "text", "required": True},
            {"key": "shortage", "label": "Material shortage", "type": "select", "options": ["None", "Short"]},
            {"key": "summary", "type": "long_text"}]},
        {"op": "add_automation", "automation": {
            "key": "short", "name": "Check shortages", "trigger": {"type": "record_created", "table": "work_orders"},
            "actions": [{"type": "ai_fill", "field": "shortage", "instructions": "Compare what's needed with what's on hand."},
                        {"type": "ai_fill", "field": "summary", "instructions": "Write a friendly summary for the floor."}]}}])
    said = [i["plain"] for i in checks.ai_doing_sums(Engine(ws))]
    assert said == ["“Check shortages” has the AI work out material shortage, a sum the app should do itself"]  # words are the AI's


def test_a_value_an_example_typed_that_isnt_one_is_the_examples_to_correct(app, owner):
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "suppliers", "label": "Suppliers", "fields": [
        {"key": "name", "type": "text", "required": True},
        {"key": "abn", "label": "ABN", "type": "text", "pattern": "^[0-9]{11}$", "pattern_message": "An ABN is 11 digits"},
        {"key": "since", "type": "date"}]}])
    short = {"title": "A supplier", "steps": [{"do": "add", "as": "s", "table": "suppliers", "values": {"name": "Acme", "abn": "1234"}}],
             "expect": [{"record": "s", "field": "name", "equals": "Acme"}]}
    when = {"title": "Another", "steps": [{"do": "add", "as": "s", "table": "suppliers", "values": {"name": "Bo", "since": "last Tuesday"}}],
            "expect": [{"record": "s", "field": "name", "equals": "Bo"}]}
    _, (abn, date) = trial.try_out(ws, [short, when], screens=False)
    assert abn["kind"] == "example" and "correct the example's values" in abn["problem"]
    assert date["kind"] == "example" and "isn't a date" in date["problem"]


def test_a_reply_says_lists_and_rules_unless_the_owner_or_the_app_uses_the_word():
    from app.builder.chat import plain
    bp = {"tables": [{"key": "products", "label": "Products", "fields": [{"key": "kind", "label": "Kind", "options": ["Table", "Chair"]}]}]}
    assert plain("I set up the fields and two automations in your ERP. Workflows run by themselves.", bp, "we sell online") \
        == "I set up the details and two rules in your app. Processes run by themselves."
    assert plain("Each table and chair has its fields.", bp, "We make tables") == "Each table and chair has its details."
    assert plain("An overdue invoice triggers a reminder.", bp, "") == "An overdue invoice sets off a reminder."


def test_an_ai_step_answers_as_the_example_expects_in_a_try_out_so_what_follows_is_tried(app, owner):
    _, ws = owner
    _built(ws, [
        {"op": "create_table", "table": "quotes", "label": "Quotes", "fields": [
            {"key": "supplier", "type": "text", "required": True},
            {"key": "price_check", "label": "Price check", "type": "select", "options": ["Pass", "Issues"]},
            {"key": "status", "type": "select", "options": ["Checking", "Approved", "Needs review"], "default": "Checking"}]},
        {"op": "add_automation", "automation": {
            "key": "check", "name": "Check the quote's prices", "trigger": {"type": "record_created", "table": "quotes"},
            "actions": [{"type": "ai_fill", "field": "price_check", "instructions": "Compare the quoted prices with the contract."},
                        {"type": "branch", "paths": [
                            {"label": "Fine", "when": [{"field": "price_check", "op": "equals", "value": "Pass"}],
                             "steps": [{"type": "update_record", "values": {"status": "Approved"}}]},
                            {"label": "Not", "when": [{"field": "price_check", "op": "equals", "value": "Issues"}],
                             "steps": [{"type": "update_record", "values": {"status": "Needs review"}}]}]}]}}])
    clean = {"title": "A clean quote is approved", "steps": [{"do": "add", "as": "q", "table": "quotes", "values": {"supplier": "Acme"}}],
             "expect": [{"record": "q", "field": "price_check", "equals": "Pass"}, {"record": "q", "field": "status", "equals": "Approved"}]}
    dear = {"title": "A dear quote goes for review", "steps": [{"do": "add", "as": "q", "table": "quotes", "values": {"supplier": "Bo"}}],
            "expect": [{"record": "q", "field": "price_check", "equals": "Issues"}, {"record": "q", "field": "status", "equals": "Needs review"}]}
    wrong = {"title": "A dear quote is approved anyway", "steps": [{"do": "add", "as": "q", "table": "quotes", "values": {"supplier": "Cy"}}],
             "expect": [{"record": "q", "field": "price_check", "equals": "Issues"}, {"record": "q", "field": "status", "equals": "Approved"}]}
    _, (a, b, c) = trial.try_out(ws, [clean, dear, wrong], screens=False)
    assert a["ok"] and b["ok"], (a, b)  # the rule after the AI step ran, both ways
    assert not c["ok"] and "Status came out Needs review" in c["plain"]  # what the app does with the answer is still checked


def test_a_rule_set_off_by_another_rules_change_runs_once(app, owner):
    user, ws = owner
    _built(ws, [
        {"op": "create_table", "table": "quotes", "label": "Quotes", "fields": [{"key": "supplier", "type": "text", "required": True},
            {"key": "status", "type": "select", "options": ["Received", "Checking", "Approved"], "default": "Received"}],
         "workflow": {"field": "status", "transitions": [{"from": ["Received"], "to": "Checking", "label": "Start check"}]}},
        {"op": "create_table", "table": "purchase_orders", "label": "Purchase orders", "fields": [
            {"key": "quote", "type": "link", "table": "quotes", "required": True}]},
        {"op": "add_automation", "automation": {"key": "check", "name": "Check", "trigger": {"type": "record_updated", "table": "quotes"},
            "conditions": [{"field": "status", "op": "changed_to", "value": "Checking"}],
            "actions": [{"type": "update_record", "values": {"status": "Approved"}}]}},
        {"op": "add_automation", "automation": {"key": "po", "name": "Raise PO", "trigger": {"type": "record_updated", "table": "quotes"},
            "conditions": [{"field": "status", "op": "changed_to", "value": "Approved"}],
            "actions": [{"type": "create_record", "table": "purchase_orders", "values": {"quote": "{{record}}"}}]}}])
    me = _as(ws, user)
    quote = me.create("quotes", {"supplier": "Acme"})
    me.move(me.get(quote.id), 0)
    assert me.get(quote.id).data["status"] == "Approved"
    assert len(me.rows("purchase_orders")) == 1  # not two: the check's approval set it off once, the move didn't again


def test_a_change_waits_out_its_cooling_off_when_days_pass_and_a_private_number_is_compared_as_shown(app, owner):
    _, ws = owner
    _built(ws, [{"op": "create_table", "table": "suppliers", "label": "Suppliers", "fields": [
        {"key": "name", "type": "text", "required": True},
        {"key": "bank_account", "label": "Bank account", "type": "text", "sensitive": True}],
        "approve_changes": {"fields": ["bank_account"], "approver": "admin", "wait_days": 7}}])
    later = {"title": "New bank details are approved after the cooling-off", "steps": [
        {"do": "add", "as": "s", "table": "suppliers", "values": {"name": "Acme", "bank_account": "11112222"}},
        {"do": "change", "as": "", "record": "s", "values": {"bank_account": "33334444"}},
        {"do": "wait", "as": "", "days": 8},
        {"do": "approve", "as": "", "record": "s"}],
        "expect": [{"record": "s", "field": "bank_account", "equals": "33334444"}]}
    _, (done,) = trial.try_out(ws, [later], screens=False)
    assert done["ok"], done  # the 7 days had passed, and ••••4444 is the 33334444 it expected


def test_an_approval_on_a_plain_status_and_a_rule_that_runs_while_true_are_found(app, owner):
    _, ws = owner
    _built(ws, [
        {"op": "create_table", "table": "quotes", "label": "Quotes", "fields": [
            {"key": "customer", "type": "text", "required": True}, {"key": "discount", "type": "number"},
            {"key": "status", "type": "select", "options": ["Draft", "Needs approval", "Sent"], "default": "Draft"}]},
        {"op": "add_automation", "automation": {
            "key": "big_discount", "name": "Approve big discounts", "trigger": {"type": "record_updated", "table": "quotes"},
            "conditions": [{"field": "discount", "op": "greater_than", "value": 10}, {"field": "status", "op": "equals", "value": "Draft"}],
            "actions": [{"type": "request_approval", "approver": "owner", "message": "Please approve this."}]}},
        {"op": "add_automation", "automation": {
            "key": "tidy", "name": "Tidy the name", "trigger": {"type": "record_updated", "table": "quotes"},
            "conditions": [{"field": "status", "op": "equals", "value": "Draft"}],
            "actions": [{"type": "update_record", "values": {"customer": "{{record.customer}}"}}]}}])
    e = Engine(ws)
    assert [i["plain"] for i in checks.approval_without_stages(e)] == [
        "A quote can be set on by hand without the approval “Approve big discounts” asks for"]
    assert [i["plain"] for i in checks.runs_while_true(e)] == [
        "“Approve big discounts” runs again every time a quote like that is saved"]  # updating a detail again is harmless


def test_the_builder_is_told_todays_weekday_week_and_month():
    from datetime import date
    from app.blueprint.schema import today_words
    assert today_words(date(2026, 11, 3)) == ("Tuesday 3 November 2026 (2026-11-03). This week: Monday 2 November to Sunday "
                                              "8 November. This month: November 2026")


def test_a_teams_own_page_opens_for_the_team_and_the_try_out_opens_it_as_them(app, client, owner):
    from .test_wave2 import _person as person
    user, ws = owner
    _built(ws, [
        {"op": "set_team", "team": {"key": "finance", "name": "Finance"}},
        {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [{"key": "number", "type": "text", "required": True}]},
        {"op": "add_page", "page": {"key": "money", "label": "Money", "access": "finance", "widgets": [{"type": "text", "text": "Hello"}]}}])
    person(ws, "fay@example.com", "Fay", "editor", teams=["finance"])
    person(ws, "sam@example.com", "Sam", "editor")
    for email, status in (("fay@example.com", 200), ("sam@example.com", 404)):  # was a crash for Fay: a team taken for a role
        client.post("/login", data={"email": email, "password": "password1234"})
        assert client.get(f"/w/{ws.id}/app/p/money").status_code == status
        client.post("/logout")
    with trial.copy_of(ws) as copy:
        engine = copy.engine()
        assert trial._screens(copy, engine, {}) == []
        from app.models import User
        assert User.query.filter_by(email="in.finance@example.com").one().name == "Someone in Finance"  # opened them too
