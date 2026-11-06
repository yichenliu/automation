"""A source-to-pay procurement system (the kind Wawasu is) assembled from the kit: teams, approvers found from a
delegation-of-authority table, separation of duties, confirming sign-in for the moves that matter, cover while away,
a supplier portal where suppliers bid, sign and keep their details up to date, bank details kept encrypted with changes
that wait for Finance after a cooling-off period, sealed bids, side-by-side comparison, invoices that can't be entered
twice, a request's journey to payment, a bank file, working days and the financial year."""
from datetime import timedelta

from app import ai
from app.blueprint import ops, schema
from app.builder import chat, prompts
from app.engine import automations
from app.engine.records import Engine, FieldErrors
from app.extensions import db
from app.models import Approval, Membership, RecordChange, User, utcnow

import pytest

from .conftest import ScriptedAI, login


def _person(ws, email, name, role, teams=None):
    user = User(email=email, name=name)
    user.set_password("password1234")
    db.session.add(user)
    db.session.flush()
    member = Membership(workspace_id=ws.id, user_id=user.id, role=role, teams=teams)
    db.session.add(member)
    db.session.commit()
    return user, member


def _as(ws, user):
    return Engine(ws, by=user.name, member=Membership.query.filter_by(workspace_id=ws.id, user_id=user.id).first())


def _login(client, email):
    client.post("/logout")
    return client.post("/login", data={"email": email, "password": "password1234"})


WAWASU = [
    {"op": "set_team", "team": {"key": "finance", "name": "Finance", "about": "Pays suppliers"}},
    {"op": "set_team", "team": {"key": "procurement", "name": "Procurement"}},
    {"op": "create_table", "table": "suppliers", "label": "Suppliers", "fields": [
        {"key": "name", "type": "text", "required": True}, {"key": "abn", "label": "ABN", "type": "text"},
        {"key": "email", "type": "email"}, {"key": "phone", "type": "phone"},
        {"key": "status", "type": "select", "options": ["Applied", "Verified", "Suspended"]},
        {"key": "bank_account", "type": "text", "sensitive": True, "pattern": "\\d{6,10}", "pattern_message": "An account number is six to ten digits (nine digits here)"},
        {"key": "bsb", "label": "BSB", "type": "text"},
        {"key": "rating", "type": "number", "access": "procurement"}],
     "workflow": {"field": "status", "transitions": [{"from": ["Applied"], "to": "Verified", "label": "Verify", "who": ["procurement"],
                                                      "not_by": ["creator"]}]},
     "approve_changes": {"fields": ["bank_account", "bsb"], "approver": "finance", "wait_days": 7, "tell": "email"}},
    {"op": "set_portal", "portal": {"table": "suppliers", "email_field": "email", "title": "Supplier portal",
                                    "profile": ["phone", "bank_account", "bsb"], "sign_up": {"fields": ["abn", "phone"]}}},
    {"op": "create_table", "table": "approval_limits", "label": "Approval limits", "fields": [
        {"key": "person", "type": "user"}, {"key": "category", "type": "select", "options": ["IT", "Goods"]}, {"key": "limit", "type": "money"}]},
    {"op": "create_table", "table": "requests", "label": "Requests", "fields": [
        {"key": "item", "type": "text", "required": True}, {"key": "category", "type": "select", "options": ["IT", "Goods"]},
        {"key": "amount", "type": "money"},
        {"key": "status", "type": "select", "options": ["Draft", "Submitted", "Approved", "Rejected", "Ordered"]},
        {"key": "approver", "type": "lookup", "table": "approval_limits", "field": "person", "sort": "limit",
         "where": [{"field": "category", "op": "equals", "value": "{{record.category}}"},
                   {"field": "limit", "op": "at_least", "value": "{{record.amount}}"}]},
        {"key": "same_item", "label": "Same item asked for this month", "type": "rollup", "table": "requests", "aggregate": "count",
         "where": [{"field": "item", "op": "equals", "value": "{{record.item}}"}, {"field": "created_at", "op": "this_month"}]},
        {"key": "due", "type": "formula", "expression": "workday(created_at, 5)", "format": "date"}],
     "workflow": {"field": "status", "transitions": [
         {"from": ["Draft"], "to": "Submitted", "label": "Submit", "who": ["creator"]},
         {"from": ["Submitted"], "to": "Approved", "label": "Approve", "who": ["approver"], "not_by": ["creator"]},
         {"from": ["Submitted"], "to": "Rejected", "label": "Reject", "who": ["approver"]}]},
     "journey": [{"label": "Submitted", "when": [{"field": "status", "op": "not_equals", "value": "Draft"}]},
                 {"label": "Approved", "when": [{"field": "status", "op": "one_of", "value": ["Approved", "Ordered"]}]},
                 {"label": "Ordered", "table": "purchase_orders", "via": "request"},
                 {"label": "Paid", "table": "invoices", "via": "po.request", "when": [{"field": "status", "op": "equals", "value": "Paid"}]}]},
    {"op": "create_table", "table": "tenders", "label": "Tenders", "fields": [
        {"key": "title", "type": "text", "required": True}, {"key": "closes_at", "type": "datetime"},
        {"key": "invited", "type": "link", "table": "suppliers", "multiple": True},
        {"key": "status", "type": "select", "options": ["Open", "Closed", "Recommended", "Awarded"]}],
     "workflow": {"field": "status", "transitions": [
         {"from": ["Open"], "to": "Closed", "label": "Close"},
         {"from": ["Closed"], "to": "Recommended", "label": "Recommend", "who": ["procurement"]},
         {"from": ["Recommended"], "to": "Awarded", "label": "Approve the award", "not_by": ["Recommended"], "step_up": True}]}},
    {"op": "create_table", "table": "invitations", "label": "Invitations", "fields": [
        {"key": "tender", "type": "link", "table": "tenders"}, {"key": "supplier", "type": "link", "table": "suppliers"}]},
    {"op": "create_table", "table": "bids", "label": "Bids", "fields": [
        {"key": "tender", "type": "link", "table": "tenders", "required": True}, {"key": "supplier", "type": "link", "table": "suppliers"},
        {"key": "notes", "type": "long_text"},
        {"key": "total", "type": "rollup", "table": "bid_lines", "via": "bid", "aggregate": "sum", "field": "amount"},
        {"key": "score", "type": "number", "access": "procurement"}],
     "sealed": {"until": "tender.closes_at", "keep_open": ["score"]},
     "portal": {"via": "supplier", "can_add": True, "edit": ["notes"]}},
    {"op": "create_table", "table": "bid_lines", "label": "Bid lines", "embedded": True, "fields": [
        {"key": "bid", "type": "link", "table": "bids"}, {"key": "item", "type": "text"}, {"key": "quantity", "type": "number"},
        {"key": "unit_price", "type": "money"}, {"key": "amount", "type": "formula", "expression": "quantity * unit_price", "format": "money"}]},
    {"op": "create_table", "table": "clarifications", "label": "Clarifications", "fields": [
        {"key": "tender", "type": "link", "table": "tenders"}, {"key": "question", "type": "text"}, {"key": "answer", "type": "long_text"},
        {"key": "published", "type": "checkbox"}],
     "portal": {"via": "tender.invited", "show_if": [{"field": "published", "op": "equals", "value": True}]}},
    {"op": "create_table", "table": "purchase_orders", "label": "Purchase orders", "fields": [
        {"key": "number", "type": "autonumber", "prefix": "PO-"}, {"key": "request", "type": "link", "table": "requests"},
        {"key": "supplier", "type": "link", "table": "suppliers"}, {"key": "total", "type": "money"},
        {"key": "status", "type": "select", "options": ["Issued", "Acknowledged", "Superseded"]},
        {"key": "previous", "label": "Previous version", "type": "link", "table": "purchase_orders"}],
     "workflow": {"field": "status", "transitions": [{"from": ["Issued"], "to": "Acknowledged", "label": "Acknowledge", "who": ["portal"]}]},
     "portal": {"via": "supplier", "fields": ["number", "total", "status"]}},
    {"op": "create_table", "table": "po_lines", "label": "PO lines", "embedded": True, "fields": [
        {"key": "po", "type": "link", "table": "purchase_orders"}, {"key": "item", "type": "text"}, {"key": "price", "type": "money"}]},
    {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [
        {"key": "supplier", "type": "link", "table": "suppliers"}, {"key": "invoice_number", "type": "text"},
        {"key": "po", "label": "PO", "type": "link", "table": "purchase_orders"}, {"key": "amount", "type": "money"},
        {"key": "status", "type": "select", "options": ["Received", "Approved", "Paid"]}],
     "unique": [["supplier", "invoice_number"]],
     "workflow": {"field": "status", "transitions": [{"from": ["Received"], "to": "Approved", "label": "Approve", "not_by": ["creator"]},
                                                     {"from": ["Approved"], "to": "Paid", "label": "Mark paid"}]}},
    {"op": "create_table", "table": "payment_runs", "label": "Payment runs", "fields": [
        {"key": "name", "type": "text"}, {"key": "status", "type": "select", "options": ["Draft", "Approved"]},
        {"key": "total", "type": "rollup", "table": "payment_lines", "via": "run", "aggregate": "sum", "field": "amount"}],
     "workflow": {"field": "status", "transitions": [{"from": ["Draft"], "to": "Approved", "label": "Approve the run", "who": ["finance"],
                                                      "not_by": ["creator"], "step_up": True}]},
     "access": {"teams": ["finance"]},
     "documents": [{"key": "bank_file", "label": "Bank file", "format": "text", "extension": "aba",
                    "header": "0{{pad(record.name, 20)}}",
                    "line": "1{{lpad(digits(line.supplier.bank_account), 9)}}{{lpad(cents(line.amount), 10)}}{{pad(line.supplier.name, 12)}}",
                    "lines": {"table": "payment_lines", "via": "run"}, "only_if": [{"field": "status", "op": "equals", "value": "Approved"}],
                    "footer": "7{{lpad(count, 6)}}{{lpad(cents(record.total), 12)}}"}]},
    {"op": "create_table", "table": "payment_lines", "label": "Payment lines", "embedded": True, "fields": [
        {"key": "run", "type": "link", "table": "payment_runs"}, {"key": "supplier", "type": "link", "table": "suppliers"},
        {"key": "amount", "type": "money"}]},
    {"op": "add_automation", "automation": {
        "key": "request_approval", "name": "Approve requests", "trigger": {"type": "record_updated", "table": "requests"},
        "conditions": [{"field": "status", "op": "changed_to", "value": "Submitted"}],
        "actions": [{"type": "request_approval", "approver": "approver", "not_by": ["creator"], "message": "Approve {{record.item}}"},
                    {"type": "update_record", "values": {"status": "Approved"}}]}},
    {"op": "add_automation", "automation": {
        "key": "invite", "name": "Invite the suppliers", "trigger": {"type": "button", "table": "tenders", "label": "Invite"},
        "actions": [{"type": "create_for_each", "source": {"field": "invited"}, "table": "invitations",
                     "values": {"tender": "{{record}}", "supplier": "{{item}}"}}]}},
    {"op": "add_automation", "automation": {
        "key": "revise_po", "name": "Revise the order", "trigger": {"type": "button", "table": "purchase_orders", "label": "Revise"},
        "actions": [{"type": "copy_record", "values": {"status": "Issued"}, "link_back": "previous"},
                    {"type": "update_record", "values": {"status": "Superseded"}}]}},
    {"op": "add_page", "page": {"key": "home", "label": "My work", "home_for": ["finance"], "widgets": [{"type": "my_work", "label": "Waiting for you"}]}},
]


def _built(ws):
    result = ops.apply(ws.blueprint, WAWASU)
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    return result


def test_the_whole_procurement_design_fits_the_kit_and_says_what_it_does(app, owner):
    _, ws = owner
    result = _built(ws)
    said = " ".join(result.changes)
    assert "Added the team Finance" in said and "put people in it under Settings" in said
    assert "Bank account kept encrypted and shown masked" in said
    assert "changes to Bank account and BSB waiting for finance's approval (never the person who made them), no sooner than 7 days later" in said
    assert "sealed until tender’s closes at" in said
    assert "“Approve the award” never by whoever moved it to Recommended" in said and "after confirming their sign-in" in said
    assert "no two alike in Supplier and Invoice number" in said and "kept to the finance team" in said
    assert "the .aba file Bank file, made in its own layout" in said and "copies it with its lines, linked back by Previous version" in said
    flow = automations.describe(Engine(ws), ws.blueprint["automations"][0])["flow"][0]["text"]
    assert flow == "Ask the request's approver to approve it, and wait for their answer (never the person who asked for it)"
    outline = prompts.outline(ws.blueprint, {})
    assert "Teams: finance \"Finance\", procurement \"Procurement\"" in outline and "person of the approval_limits found by conditions" in outline


def test_the_approver_is_whoever_holds_the_limit_and_never_the_requester(app, owner, client):
    user, ws = owner
    _built(ws)
    sam, _ = _person(ws, "sam@example.com", "Sam", "editor")
    lee, _ = _person(ws, "lee@example.com", "Lee", "editor")
    boss = Engine(ws, by="Ethan")
    boss.create("approval_limits", {"person": lee.id, "category": "IT", "limit": 5000})
    boss.create("approval_limits", {"person": user.id, "category": "IT", "limit": 50000})
    small = _as(ws, sam).create("requests", {"item": "Laptop", "category": "IT", "amount": 2400})
    big = _as(ws, lee).create("requests", {"item": "Server", "category": "IT", "amount": 3000})
    assert Engine(ws).value(small, "approver") == lee.id  # the lowest limit that covers it
    assert Engine(ws).value(_as(ws, sam).create("requests", {"item": "SAN", "category": "IT", "amount": 20000}), "approver") == user.id
    assert Engine(ws).value(small, "same_item") == 0 and Engine(ws).value(_as(ws, sam).create("requests", {"item": "Laptop"}), "same_item") == 1
    _as(ws, sam).move(small, 0)  # Submit
    ask = Approval.query.one()
    assert ask.assignee_id == lee.id and ask.proposal == {"not_by": ["creator"]}
    # Lee raised the second request and holds its limit: separation of duties keeps it from him, the owner included
    _as(ws, lee).move(big, 0)
    second = Approval.query.filter_by(record_id=big.id).one()
    assert not automations.may_decide(second, Membership.query.filter_by(user_id=lee.id).first(), lee.id, big, engine=_as(ws, lee))
    approve = next(i for i, m, _ in _as(ws, user).moves(Engine(ws).get(big.id)) if m["to"] == "Approved")
    with pytest.raises(FieldErrors, match="you raised this"):
        _as(ws, lee).move(Engine(ws).get(big.id), approve)
    _login(client, "lee@example.com")
    base = f"/w/{ws.id}/app"
    page = client.get(base + "/approvals").get_data(as_text=True)
    assert "Approve Laptop" in page and "Approve Server" not in page.split("Waiting for others")[0]
    client.post(base + f"/approvals/{ask.id}", data={"decision": "approve"})
    assert Engine(ws).get(small.id).data["status"] == "Approved"
    record_page = client.get(base + f"/t/requests/{small.id}").get_data(as_text=True)
    assert 'class="journey"' in record_page and "Ordered" in record_page


def test_teams_cover_while_away_and_confirming_sign_in(app, owner, client):
    user, ws = owner
    _built(ws)
    ann, _ = _person(ws, "ann@example.com", "Ann", "editor", teams=["finance"])
    bob, bob_member = _person(ws, "bob@example.com", "Bob", "editor", teams=["finance"])
    cat, _ = _person(ws, "cat@example.com", "Cat", "editor")
    assert not _as(ws, cat).can("payment_runs") and _as(ws, ann).can("payment_runs", "edit") and Engine(ws, member=None).can("payment_runs")
    run = _as(ws, ann).create("payment_runs", {"name": "Weekly run", "status": "Draft"})
    approve = 0
    with pytest.raises(FieldErrors, match="you raised this"):  # Ann prepared it: someone else approves it
        _as(ws, ann).move(run, approve)
    with pytest.raises(FieldErrors, match="Confirm it's you"):
        _as(ws, bob).move(Engine(ws).get(run.id), approve)
    _login(client, "bob@example.com")
    base = f"/w/{ws.id}/app"
    page = client.get(base + f"/t/payment_runs/{run.id}").get_data(as_text=True)
    assert 'name="step_up_code"' in page
    client.post(base + f"/t/payment_runs/{run.id}/move/0", data={"step_up_code": "wrong"})
    assert Engine(ws).get(run.id).data["status"] == "Draft"
    client.post(base + f"/t/payment_runs/{run.id}/move/0", data={"step_up_code": "password1234"})
    assert Engine(ws).get(run.id).data["status"] == "Approved"
    assert 'name="step_up_code"' not in client.get(base + f"/t/payment_runs/{run.id}").get_data(as_text=True)  # good for ten minutes
    # Bob is away: what's asked of him goes to Cat meanwhile
    bob_member.away_from, bob_member.away_until, bob_member.cover_id = Engine(ws).today, Engine(ws).today + timedelta(days=3), cat.id
    db.session.commit()
    boss = Engine(ws, by="Ethan")
    boss.create("approval_limits", {"person": bob.id, "category": "Goods", "limit": 900})
    request = boss.create("requests", {"item": "Chairs", "category": "Goods", "amount": 800})
    boss.move(request, 0)
    ask = Approval.query.filter_by(record_id=request.id).one()
    assert ask.assignee_id == cat.id and ask.history[0]["for"] == "Bob"
    # a team's approval: anyone in it answers
    team_ask = automations._assignees(boss, "finance", request)
    assert team_ask == [(None, "team:finance")]


def test_bank_details_change_waits_for_finance_and_the_supplier_can_cancel(app, owner, client):
    user, ws = owner
    _built(ws)
    ann, _ = _person(ws, "ann@example.com", "Ann", "editor", teams=["finance"])
    boss = Engine(ws, by="Ethan")
    supplier = boss.create("suppliers", {"name": "Acme Pumps", "email": "accounts@acme.example", "bank_account": "123456789", "bsb": "062000"})
    with pytest.raises(FieldErrors, match="nine digits"):  # a format rule checks what was typed, not what's stored
        boss.create("suppliers", {"name": "Bad", "bank_account": "12-34"})
    stored = supplier.data["bank_account"]
    assert stored["masked"] == "••••6789" and "123456789" not in str(stored)  # encrypted, never plain
    assert Engine(ws).value(supplier, "bank_account") == "••••6789"
    portal_user, _ = _person(ws, "acme@example.com", "Acme", "portal")
    Membership.query.filter_by(user_id=portal_user.id).first().contact_id = supplier.id
    db.session.commit()
    _login(client, "acme@example.com")
    page = client.get(f"/portal/{ws.id}/me").get_data(as_text=True)
    assert "••••6789" in page and "123456789" not in page
    client.post(f"/portal/{ws.id}/me", data={"phone": "0400 000 000", "bank_account": "987654321", "bsb": "062000", "_version": supplier.version})
    supplier = Engine(ws).get(supplier.id)
    assert supplier.data["phone"] == "0400 000 000" and supplier.data["bank_account"]["masked"] == "••••6789"  # not used yet
    change = Approval.query.filter_by(kind="change").one()
    assert change.role == "team:finance" and change.proposal["shown"] == {"Bank account": ["••••6789", "••••4321"]}
    assert "987654321" not in str(change.proposal["shown"]) and change.proposal["token"]
    home = client.get(f"/portal/{ws.id}/").get_data(as_text=True)
    assert "Was this you?" in home and "No, cancel it" in home
    # Finance can't approve it before the cooling-off ends
    _login(client, "ann@example.com")
    base = f"/w/{ws.id}/app"
    assert "You can approve it from" in client.get(base + "/approvals").get_data(as_text=True)
    client.post(base + f"/approvals/{change.id}", data={"decision": "approve"})
    assert db.session.get(Approval, change.id).status == "pending"
    change.proposal = {**change.proposal, "ready_at": (utcnow() - timedelta(minutes=1)).isoformat()}
    db.session.commit()
    client.post(base + f"/approvals/{change.id}", data={"decision": "approve"})
    supplier = Engine(ws).get(supplier.id)
    assert db.session.get(Approval, change.id).status == "approved" and supplier.data["bank_account"]["masked"] == "••••4321"
    shown = client.post(base + f"/t/suppliers/{supplier.id}/reveal/bank_account").get_json()
    assert shown == {"value": "987654321"}
    history = RecordChange.query.filter_by(record_id=supplier.id).all()
    assert any(h.kind == "viewed" and (h.changes or {}).get("_shown") == [None, "Bank account"] for h in history)
    assert "987654321" not in str([h.changes for h in history])  # the history keeps only masks
    # a second change, cancelled through the emailed link
    _as(ws, ann).update(Engine(ws).get(supplier.id), {"bank_account": "555555555"})
    second = Approval.query.filter_by(kind="change", status="pending").one()
    assert not automations.may_decide(second, Membership.query.filter_by(user_id=ann.id).first(), ann.id)  # her own change
    client.post("/logout")
    assert "Cancel this change?" in client.get(f"/c/{second.proposal['token']}").get_data(as_text=True)
    assert "Cancelled" in client.post(f"/c/{second.proposal['token']}").get_data(as_text=True)
    assert db.session.get(Approval, second.id).status == "cancelled" and Engine(ws).get(supplier.id).data["bank_account"]["masked"] == "••••4321"


def test_sealed_bids_come_through_the_portal_and_open_at_the_close(app, owner, client):
    user, ws = owner
    _built(ws)
    boss = Engine(ws, by="Ethan")
    acme = boss.create("suppliers", {"name": "Acme", "email": "a@acme.example"})
    bolt = boss.create("suppliers", {"name": "Bolt", "email": "b@bolt.example"})
    closes = (boss.now + timedelta(days=2)).isoformat(timespec="minutes")
    tender = boss.create("tenders", {"title": "Pumps", "closes_at": closes, "invited": [acme.id, bolt.id], "status": "Open"})
    boss.create("clarifications", {"tender": tender.id, "question": "Delivery?", "answer": "To site", "published": True})
    boss.create("clarifications", {"tender": tender.id, "question": "Internal note", "published": False})
    automations.press(boss, ws.blueprint["automations"][1], boss.get(tender.id), "Ethan")
    assert sorted(r.data["supplier"] for r in Engine(ws).rows("invitations")) == [acme.id, bolt.id]
    bids = []
    for who, contact, price in (("acme@example.com", acme, 100), ("bolt@example.com", bolt, 90)):
        portal_user, _ = _person(ws, who, who.split("@")[0], "portal")
        Membership.query.filter_by(user_id=portal_user.id).first().contact_id = contact.id
        db.session.commit()
        _login(client, who)
        home = client.get(f"/portal/{ws.id}/").get_data(as_text=True)
        assert "Delivery?" in home and "Internal note" not in home  # published answers reach every invited supplier
        client.post(f"/portal/{ws.id}/t/bids/new", data={"tender": tender.id, "notes": "Best price", "lines-bid_lines-0-item": "Pump",
                                                          "lines-bid_lines-0-quantity": "2", "lines-bid_lines-0-unit_price": str(price)})
        bids.append(Engine(ws).rows("bids")[-1])
    acme_bid, bolt_bid = bids
    assert acme_bid.data["supplier"] == acme.id and Engine(ws).children("bid_lines", "bid", acme_bid.id)
    # before the close: nobody at the business sees what's in a bid, the owner included; the AI doesn't either
    owner_engine = _as(ws, user)
    assert owner_engine.value(acme_bid, "notes") is None and owner_engine.value(acme_bid, "total") is None
    assert Engine(ws).value(acme_bid, "total") is None and Engine(ws).value(acme_bid, "supplier") == acme.id
    line = Engine(ws).children("bid_lines", "bid", acme_bid.id)[0]
    assert Engine(ws).value(line, "unit_price") is None
    with pytest.raises(FieldErrors, match="only whoever made it"):
        owner_engine.update(acme_bid, {"notes": "changed"})
    _login(client, "ethan@example.com")
    base = f"/w/{ws.id}/app"
    page = client.get(base + f"/t/bids/{acme_bid.id}").get_data(as_text=True)
    assert "Sealed until" in page and "Best price" not in page
    assert any(h.kind == "viewed" for h in RecordChange.query.filter_by(record_id=acme_bid.id))  # every opening kept
    # its maker sees and changes it until the close
    _login(client, "acme@example.com")
    assert "Best price" in client.get(f"/portal/{ws.id}/t/bids/{acme_bid.id}").get_data(as_text=True)
    client.post(f"/portal/{ws.id}/t/bids/{acme_bid.id}/edit", data={"notes": "Best price, 2 weeks", "_version": acme_bid.version})
    assert Engine(ws).get(acme_bid.id).data["notes"] == "Best price, 2 weeks"
    # the tender closes: they open, and nobody can change what was sent
    t = Engine(ws).get(tender.id)
    t.data = {**t.data, "closes_at": (boss.now - timedelta(hours=1)).isoformat(timespec="minutes")}
    db.session.commit()
    assert _as(ws, user).value(Engine(ws).get(acme_bid.id), "total") == 200
    with pytest.raises(FieldErrors, match="can't be changed now"):
        Engine(ws).update(Engine(ws).get(acme_bid.id), {"notes": "late change"})
    Engine(ws).update(Engine(ws).get(acme_bid.id), {"score": 7})  # the business still scores them
    _login(client, "ethan@example.com")
    compared = client.get(base + f"/t/bids/compare?ids={acme_bid.id}&ids={bolt_bid.id}").get_data(as_text=True)
    assert "side by side" in compared and "Pump" in compared and "lowest" in compared and "$180.00" in compared
    assert "Compare" in client.get(base + f"/t/tenders/{tender.id}").get_data(as_text=True)


def test_invoices_once_a_journey_revisions_and_the_bank_file(app, owner, client):
    user, ws = owner
    _built(ws)
    boss = Engine(ws, by="Ethan")
    acme = boss.create("suppliers", {"name": "Acme", "email": "a@acme.example", "bank_account": "123456789"})
    request = boss.create("requests", {"item": "Pumps", "category": "Goods", "amount": 400, "status": "Approved"})
    po = boss.create("purchase_orders", {"request": request.id, "supplier": acme.id, "total": 400, "status": "Issued"})
    boss.create("po_lines", {"po": po.id, "item": "Pump", "price": 200})
    boss.create("invoices", {"supplier": acme.id, "invoice_number": "INV-1", "po": po.id, "amount": 400, "status": "Received"})
    with pytest.raises(FieldErrors, match="already has this supplier and invoice number"):
        boss.create("invoices", {"supplier": acme.id, "invoice_number": "inv-1", "amount": 400})
    from app.web.erp_records import journey as path
    journey = {s["label"]: s["done"] for s in path(boss, boss.tables["requests"], boss.get(request.id))}  # its links need no request
    assert journey == {"Submitted": True, "Approved": True, "Ordered": True, "Paid": False}
    # a revision: the order and its lines copied, linked back, the old one superseded
    automations.press(boss, ws.blueprint["automations"][2], boss.get(po.id), "Ethan")
    orders = Engine(ws).rows("purchase_orders")
    new = orders[-1]
    assert new.data["previous"] == po.id and new.data["status"] == "Issued" and Engine(ws).get(po.id).data["status"] == "Superseded"
    assert [x.data["item"] for x in Engine(ws).children("po_lines", "po", new.id)] == ["Pump"] and new.data["number"] != po.data["number"]
    # the supplier acknowledges the new order in the portal
    portal_user, _ = _person(ws, "acme@example.com", "Acme", "portal")
    Membership.query.filter_by(user_id=portal_user.id).first().contact_id = acme.id
    db.session.commit()
    _login(client, "acme@example.com")
    assert "Acknowledge" in client.get(f"/portal/{ws.id}/").get_data(as_text=True)
    client.post(f"/portal/{ws.id}/t/purchase_orders/{new.id}/move/0")
    assert Engine(ws).get(new.id).data["status"] == "Acknowledged"
    # the bank file: account numbers in full in the file, never kept, the making of it logged with its fingerprint
    run = boss.create("payment_runs", {"name": "Run 1", "status": "Draft"})
    boss.create("payment_lines", {"run": run.id, "supplier": acme.id, "amount": 400})
    from app.documents import produce, text_file
    name, content, sha, sensitive = text_file(boss, boss.get(run.id), "bank_file")
    assert sensitive and name.endswith(".aba")
    from app.blueprint import expr
    assert expr.render("{{a}},{{b}}", expr.Context({"a": 1, "b": "x"})) == "1,x"  # two of them, not one starting and ending with braces
    assert content.decode().splitlines() == ["0Run 1               ", "11234567890000040000" + "Acme        ", "7000001000000040000"]
    with pytest.raises(ValueError, match="never kept"):
        produce(boss, boss.get(run.id), "bank_file")
    _login(client, "ethan@example.com")
    early = client.post(f"/w/{ws.id}/app/t/payment_runs/{run.id}/document/bank_file", follow_redirects=True)
    assert "isn&#39;t ready yet" in early.get_data(as_text=True)  # no file for the bank before the run is approved
    boss.update(boss.get(run.id), {"status": "Approved"})
    response = client.post(f"/w/{ws.id}/app/t/payment_runs/{run.id}/document/bank_file")
    assert response.data == content and response.headers["X-Content-SHA256"] == sha
    assert any("fingerprint" in str(h.changes) for h in RecordChange.query.filter_by(record_id=run.id))


def test_working_days_the_financial_year_suggestions_and_my_work(app, owner, client):
    user, ws = owner
    _built(ws)
    ws.settings = {**ws.settings, "calendar": {"workdays": [0, 1, 2, 3, 4], "holidays": ["2026-12-25 Christmas Day"], "fy_start": 7}}
    db.session.commit()
    e = Engine(ws)
    assert str(e.workday("2026-12-23", 2)) == "2026-12-28" and e.workdays_between("2026-12-21", "2026-12-28") == 4
    from datetime import date
    assert schema.period("this_financial_year", date(2026, 11, 2), 7) == (date(2026, 7, 1), date(2027, 6, 30))
    assert schema.period("last_financial_year", date(2026, 3, 2), 7) == (date(2024, 7, 1), date(2025, 6, 30))
    # the AI suggests a field while the form is filled in; nothing is saved
    ops_ = [{"op": "update_field", "table": "requests", "key": "category", "changes": {"ai": "Which category is it?"}}]
    result = ops.apply(ws.blueprint, ops_)
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    ai.set_backend(ScriptedAI({"value": "IT", "sure": True, "why": "A laptop is IT."}))
    login(client)
    base = f"/w/{ws.id}/app"
    form = client.get(base + "/t/requests/new").get_data(as_text=True)
    assert "data-suggest-field=\"category\"" in form
    answer = client.post(base + "/t/requests/suggest/category", data={"item": "Laptop"}).get_json()
    assert answer["ok"] and answer["value"] == "IT" and Engine(ws).rows("requests") == []
    # the owner's home page shows what's waiting for them
    Engine(ws, by="Ethan").create("approval_limits", {"person": user.id, "category": "IT", "limit": 9000})
    sam, _ = _person(ws, "sam@example.com", "Sam", "editor")
    req = _as(ws, sam).create("requests", {"item": "Laptop", "category": "IT", "amount": 100})
    _as(ws, sam).move(req, 0)
    page = client.get(base + "/p/home").get_data(as_text=True)
    assert "Waiting for you" in page and "Approve Laptop" in page


def test_suppliers_can_join_by_themselves(app, owner, client):
    _, ws = owner
    _built(ws)
    page = client.get(f"/join/{ws.id}/suppliers").get_data(as_text=True)
    assert "Supplier portal" in page and 'name="abn"' in page
    client.post(f"/join/{ws.id}/suppliers", data={"name": "Crane Co", "abn": "51824753556", "phone": "0400", "email": "ops@crane.example",
                                                  "password": "a-long-password"})
    supplier = Engine(ws).rows("suppliers")[-1]
    assert supplier.data["name"] == "Crane Co" and supplier.data["status"] == "Applied" and supplier.data["email"] == "ops@crane.example"
    member = Membership.query.filter_by(workspace_id=ws.id, role="portal").one()
    assert member.contact_id == supplier.id
    assert "Hello, Crane Co" in client.get(f"/portal/{ws.id}/").get_data(as_text=True)
    theirs = Engine(ws, by="Crane Co (portal)")
    theirs.portal_contact = supplier.id
    with pytest.raises(FieldErrors, match="can't go from Applied to Verified"):  # verifying them is the business's move, not theirs
        theirs.update(theirs.get(supplier.id), {"status": "Verified"})
