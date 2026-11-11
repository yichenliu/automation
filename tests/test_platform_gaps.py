"""What replicating a whole marketplace (wawasu) needed that the platform couldn't say, each on a small app of its own:
text tests in formulas, a call out held to its allowed hosts (an address from a record too), a spreadsheet brought
into a list by a rule, the AI's confidence kept and judged, a held change approved only once its notice went out,
everyone made to sign in with two steps, record access through several links, a step-by-step form whose step is the
record's lines, a file's fingerprint in formulas, and records kept only as long as the list says."""
import re
from datetime import timedelta

import pytest

from app.blueprint import expr, schema
from app.engine import automations, retention
from app.engine.records import Engine, FieldErrors
from app.extensions import db
from app.lang import apply
from app.models import Membership, Record, User, Workspace


def _ok(patches, bp=None):
    out = apply(bp or {}, patches)
    assert not out.problems, out.problems
    return out.blueprint


def _ws(app, bp, settings=None):
    owner = User(email="owner@example.com", name="Olive")
    owner.set_password("x" * 12)
    db.session.add(owner)
    db.session.flush()
    ws = Workspace(owner_id=owner.id, name="Market", blueprint=bp, state={}, settings=settings or {})
    db.session.add(ws)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=owner.id, role="owner"))
    db.session.commit()
    return ws


class Ctx(expr.Context):
    functions = {}

    def __init__(self, names):
        self.names = names

    def resolve(self, name):
        return self.names.get(name)


def test_formulas_test_text():
    c = Ctx({"name": "Acme Fittings Pty Ltd", "email": "ann@acme.example", "abn": "51 824 753 556"})
    assert expr.evaluate('contains(name, "pty")', c) is True and expr.evaluate('contains(name, "")', c) is False
    assert expr.evaluate('starts_with(name, "acme") and ends_with(email, "@ACME.example")', c) is True
    assert expr.evaluate('replace(abn, " ", "")', c) == "51824753556" and expr.evaluate('left(name, 4)', c) == "Acme"
    assert expr.evaluate('right(name, 3)', c) == "Ltd" and expr.evaluate('trim("  a   b ")', c) == "a b"
    assert expr.evaluate('matches(replace(abn, " ", ""), "^\\\\d{11}$")', c) is True and expr.evaluate('matches(name, "(")', c) is False
    for name in ("contains", "starts_with", "ends_with", "replace", "trim", "left", "right", "matches"):
        assert name in expr.KNOWN and name in expr.SAID


def test_a_call_out_stays_within_its_hosts(app):
    """Each buyer's ERP has its own address, from the record: the call is held to the hosts the record's integration
    allows, and the app's own list; the key kept secret goes only there."""
    bp = _ok([{"do": "add_list", "key": "links", "label": "Links", "singular": "Link", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "url", "key": "endpoint", "label": "Endpoint"},
                  {"type": "long_text", "key": "hosts", "label": "Hosts"}, {"type": "text", "key": "secret", "label": "Secret", "sensitive": True},
                  {"type": "text", "key": "status_back", "label": "Status back"}]},
              {"do": "add_rule", "rule": {"key": "send", "name": "Send", "when": {"on": "button", "list": "links", "label": "Send"}, "steps": [
                  {"do": "webhook", "url": "{{endpoint}}/orders", "hosts": "{{hosts}}", "headers": {"Authorization": "Bearer {{secret}}"},
                   "save": {"status_back": "status"}}]}}])
    step = next(a for a in bp["automations"] if a["key"] == "send")["actions"][0]
    assert step["hosts"] == "{{hosts}}" and step["headers"] == {"Authorization": "Bearer {{secret}}"} and step["save"] == {"status_back": "status"}
    out = apply(bp, [{"do": "add_rule", "rule": {"key": "loose", "name": "Loose", "when": {"on": "button", "list": "links", "label": "Loose"},
                                                  "steps": [{"do": "webhook", "url": "{{endpoint}}"}]}}])
    assert out.problems and "hosts" in out.problems[0]  # an address from a record needs the hosts it may reach
    ws = _ws(app, bp, settings={"outbound_hosts": ["*.example.com"]})
    e = Engine(ws)
    r = e.create("links", {"name": "ERP", "endpoint": "https://erp.example.com/api", "hosts": "erp.example.com", "secret": "s3cret"})
    run = type("Run", (), {"context": {}, "id": None})()
    ctx = automations._context(e, run, e.get(r.id))
    automations._reachable(e, step, ctx, "https://erp.example.com/api/orders")
    with pytest.raises(ValueError):
        automations._reachable(e, step, ctx, "https://evil.example.net/orders")
    with pytest.raises(ValueError):  # the step's hosts allow it, the app's don't
        e.update(e.get(r.id), {"hosts": "erp.other.org"})
        automations._reachable(e, step, automations._context(e, run, e.get(r.id)), "https://erp.other.org/orders")
    assert automations._headers(e, step, ctx) == {"Authorization": "Bearer s3cret"}


def test_a_rule_brings_a_spreadsheet_into_a_list(app, tmp_path):
    """A seller's price list: each row a catalogue item of that seller's, a row with a code already theirs updates it, a
    row that can't be one says why; another seller's item with the same code is never touched."""
    bp = _ok([{"do": "add_list", "key": "sellers", "label": "Sellers", "singular": "Seller", "fields": [{"type": "text", "key": "name", "label": "Name"}]},
              {"do": "add_list", "key": "uploads", "label": "Uploads", "singular": "Upload", "fields": [
                  {"type": "link", "key": "seller", "label": "Seller", "to": "sellers"}, {"type": "file", "key": "sheet", "label": "Sheet"},
                  {"type": "long_text", "key": "outcome", "label": "Outcome"}]},
              {"do": "add_list", "key": "items", "label": "Items", "singular": "Item", "fields": [
                  {"type": "link", "key": "seller", "label": "Seller", "to": "sellers"}, {"type": "text", "key": "code", "label": "Code", "required": True},
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "money", "key": "price", "label": "Price"}]},
              {"do": "add_rule", "rule": {"key": "bring_in", "name": "Bring it in", "when": {"on": "button", "list": "uploads", "label": "Bring in"},
                                          "steps": [{"do": "import", "file": "sheet", "list": "items", "values": [{"field": "seller", "formula": "record.seller"}],
                                                     "match_on": "code", "result": "outcome"}]}}])
    ws = _ws(app, bp)
    e = Engine(ws)
    acme, bolt = e.create("sellers", {"name": "Acme"}), e.create("sellers", {"name": "Bolt"})
    theirs = e.create("items", {"seller": bolt.id, "code": "E100", "name": "Bolt's elbow", "price": 9})
    mine = e.create("items", {"seller": acme.id, "code": "E100", "name": "Elbow", "price": 10})
    upload = e.create("uploads", {"seller": acme.id})
    from app.storage import save
    save(ws.id, b"Code,Name,Price\nE100,Elbow 100mm,11\nT200,Tee,7\n,No code,3\n", "prices.csv", record_id=upload.id, field="sheet")
    rule = next(a for a in ws.blueprint["automations"] if a["key"] == "bring_in")
    automations.press(e, rule, e.get(upload.id), "Olive")
    fresh = Engine(ws)
    assert fresh.get(mine.id).data["price"] in (11, "11", 11.0) and fresh.get(theirs.id).data["price"] in (9, "9", 9.0)
    tees = [r for r in fresh.rows("items") if r.data.get("code") == "T200"]
    assert len(tees) == 1 and tees[0].data["seller"] == acme.id
    outcome = fresh.get(upload.id).data["outcome"]
    assert "Brought 1 new item and updated 1" in outcome and "Row 4: not brought in" in outcome


def test_the_ais_confidence_is_kept_and_judged():
    action = {"type": "ai_fill", "field": "category", "confidence": "category_confidence", "sure_above": 80}
    assert automations._sureness(action, {"sure": True, "confidence": 92}, {"category": 4}) == {"values": {"category": 4, "category_confidence": 92}, "sure": True}
    assert automations._sureness(action, {"sure": True, "confidence": 61}, {"category": 4})["sure"] is False
    assert automations._sureness(action, {"sure": True, "confidence": None}, {"category": 4})["sure"] is False
    bp = _ok([{"do": "add_list", "key": "lines", "label": "Lines", "singular": "Line", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "text", "key": "category", "label": "Category"},
                  {"type": "number", "key": "sure", "label": "Sure"}]},
              {"do": "add_rule", "rule": {"key": "sort", "name": "Sort", "when": {"on": "added", "list": "lines"}, "steps": [
                  {"do": "ai_fill", "field": "category", "instructions": "The category", "review": "when_unsure", "confidence": "sure", "sure_above": 80}]}}])
    step = next(a for a in bp["automations"] if a["key"] == "sort")["actions"][0]
    assert step["confidence"] == "sure" and step["sure_above"] == 80


def test_a_held_change_waits_for_its_notice_to_go_out(app):
    bp = _ok([{"do": "add_list", "key": "firms", "label": "Firms", "singular": "Firm", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "email", "key": "email", "label": "Email"},
                  {"type": "text", "key": "account", "label": "Account"}]},
              {"do": "design_op", "op": {"op": "update_table", "table": "firms", "approve_changes": {
                  "fields": ["account"], "approver": "admin", "tell": "email", "told_first": True}}}])
    assert schema.table(bp, "firms")["approve_changes"]["told_first"] is True
    ws = _ws(app, bp)
    staff = User(email="sam@example.com", name="Sam")
    staff.set_password("x" * 12)
    db.session.add(staff)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=staff.id, role="editor"))
    db.session.commit()
    firm = Engine(ws).create("firms", {"name": "Acme", "email": "ann@acme.example"})
    sam = Engine(ws, by="Sam", member=Membership.query.filter_by(workspace_id=ws.id, user_id=staff.id).one())
    sam.update(sam.get(firm.id), {"account": "123"})
    from app.models import Approval
    change = Approval.query.filter_by(workspace_id=ws.id, kind="change", status="pending").one()
    assert not change.proposal.get("told_at")  # no mailbox connected, nobody in the portal: not told
    from app.engine.records import FieldErrors
    with pytest.raises(FieldErrors):
        automations.decide_change(Engine(ws), change, True, "Olive")


def test_an_app_can_ask_everyone_for_two_step_sign_in(app):
    from app.security import two_step_needed
    ws = _ws(app, {}, settings={"require_two_step": True})
    user = User(email="sam@example.com", name="Sam")
    assert two_step_needed(ws, user)
    user.totp_secret = "set"
    assert not two_step_needed(ws, user)
    assert not two_step_needed(_ws_plain(app), User(email="x@example.com", name="X"))


def _ws_plain(app):
    owner = User(email="plain@example.com", name="P")
    owner.set_password("x" * 12)
    db.session.add(owner)
    db.session.flush()
    ws = Workspace(owner_id=owner.id, name="Plain", blueprint={}, state={}, settings={})
    db.session.add(ws)
    db.session.commit()
    return ws


def test_record_access_follows_several_links(app):
    """An offer is its round's buyer's: its members see it, others don't."""
    bp = _ok([{"do": "add_list", "key": "orgs", "label": "Orgs", "singular": "Org", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "person", "key": "members", "label": "Members", "many": True}]},
              {"do": "add_list", "key": "rounds", "label": "Rounds", "singular": "Round", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "link", "key": "buyer", "label": "Buyer", "to": "orgs"}]},
              {"do": "add_list", "key": "offers", "label": "Offers", "singular": "Offer", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "link", "key": "round", "label": "Round", "to": "rounds"}]},
              {"do": "design_op", "op": {"op": "update_table", "table": "offers",
                                         "row_access": {"field": "round.buyer.members", "roles": ["viewer", "editor"]}}}])
    assert schema.check(bp) == []
    ws = _ws(app, bp)
    people = {}
    for name in ("ann", "bob"):
        people[name] = User(email=f"{name}@example.com", name=name.title())
        people[name].set_password("x" * 12)
        db.session.add(people[name])
        db.session.flush()
        db.session.add(Membership(workspace_id=ws.id, user_id=people[name].id, role="editor"))
    db.session.commit()
    e = Engine(ws)
    org = e.create("orgs", {"name": "Pilbara", "members": [people["ann"].id]})
    offer = e.create("offers", {"name": "O1", "round": e.create("rounds", {"name": "R1", "buyer": org.id}).id})

    def sees(name):
        engine = Engine(ws, by=name, member=Membership.query.filter_by(workspace_id=ws.id, user_id=people[name].id).one())
        return [r.id for r in engine.visible_rows("offers")]
    assert sees("ann") == [offer.id] and sees("bob") == []


def test_a_step_of_a_form_can_be_its_lines():
    bp = _ok([{"do": "add_list", "key": "tenders", "label": "Tenders", "singular": "Tender", "fields": [
                  {"type": "text", "key": "title", "label": "Title"}, {"type": "date", "key": "closes", "label": "Closes"}]},
              {"do": "add_list", "key": "tender_lines", "label": "Tender lines", "singular": "Tender line", "inside": "tenders", "fields": [
                  {"type": "link", "key": "tender", "label": "Tender", "to": "tenders"}, {"type": "text", "key": "what", "label": "What"}]},
              {"do": "design_op", "op": {"op": "update_table", "table": "tenders", "wizard": True, "sections": [
                  {"label": "1 Basics", "fields": ["title"]}, {"label": "2 What to price", "lines": "tender_lines"},
                  {"label": "3 Dates", "fields": ["closes"]}]}}])
    assert schema.table(bp, "tenders")["sections"][1] == {"label": "2 What to price", "fields": [], "lines": "tender_lines"}
    assert schema.check(bp) == []
    from app.web.erp_records import _sections
    shown = _sections(schema.table(bp, "tenders"), lines=True)
    assert [s["label"] for s in shown] == ["1 Basics", "2 What to price", "3 Dates"] and shown[1]["lines"] == "tender_lines"
    assert [s["label"] for s in _sections(schema.table(bp, "tenders"))] == ["1 Basics", "3 Dates"]


def test_a_files_fingerprint_reads_in_formulas(app):
    bp = _ok([{"do": "add_list", "key": "contracts", "label": "Contracts", "singular": "Contract", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "file", "key": "signed", "label": "Signed copy"},
                  {"type": "text", "key": "print", "label": "Fingerprint"}]},
              {"do": "add_rule", "rule": {"key": "keep", "name": "Keep its fingerprint", "when": {"on": "button", "list": "contracts", "label": "Keep"},
                                          "steps": [{"do": "set", "values": [{"field": "print", "formula": "record.signed.fingerprint"}]}]}}])
    ws = _ws(app, bp)
    e = Engine(ws)
    c = e.create("contracts", {"name": "C1"})
    from app.storage import save
    stored = save(ws.id, b"%PDF-1.4 signed", "signed.pdf", record_id=c.id, field="signed")
    automations.press(e, next(a for a in ws.blueprint["automations"] if a["key"] == "keep"), e.get(c.id), "Olive")
    assert Engine(ws).get(c.id).data["print"] == stored.sha256


def test_records_are_kept_only_as_long_as_their_list_says(app):
    """Seven years from the invoice date, unless a legal hold covers it; once deleted a while, erased for good."""
    bp = _ok([{"do": "add_list", "key": "holds", "label": "Holds", "singular": "Hold", "fields": [{"type": "text", "key": "name", "label": "Name"}],
               "stages": {"stages": ["In force", "Lifted"]}},
              {"do": "add_list", "key": "invoices", "label": "Invoices", "singular": "Invoice", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "date", "key": "invoice_date", "label": "Invoice date"}]},
              {"do": "design_op", "op": {"op": "update_table", "table": "invoices", "keep_for": {
                  "years": 7, "from": "invoice_date", "unless": ['count(holds where status == "In force") > 0']}}}])
    assert schema.table(bp, "invoices")["keep_for"]["from"] == "invoice_date" and schema.check(bp) == []
    out = apply(bp, [{"do": "design_op", "op": {"op": "update_table", "table": "invoices", "keep_for": {"years": 7, "from": "name"}}}])
    assert out.problems  # counted from a date
    ws = _ws(app, bp)
    e = Engine(ws)
    old = e.create("invoices", {"name": "Old", "invoice_date": (e.today - timedelta(days=365 * 8)).isoformat()})
    new = e.create("invoices", {"name": "New", "invoice_date": e.today.isoformat()})
    hold = e.create("holds", {"name": "Dispute", "status": "In force"})
    assert retention.run(ws) == (0, 0)  # held
    e.update(e.get(hold.id), {"status": "Lifted"})
    assert retention.run(ws) == (1, 0)
    assert Engine(ws).get(old.id) is None and Engine(ws).get(new.id) is not None
    gone = db.session.get(Record, old.id)
    gone.deleted_at = gone.deleted_at - timedelta(days=31)
    db.session.commit()
    assert retention.run(ws) == (0, 1)
    erased = db.session.get(Record, old.id)
    assert erased.table == retention.ERASED and "name" not in erased.data and retention.run(ws) == (0, 0)


def test_how_long_records_are_kept_can_be_worked_out_for_each(app):
    """Each buyer keeps its invoices as long as its own rule says."""
    bp = _ok([{"do": "add_list", "key": "orgs", "label": "Orgs", "singular": "Org", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "number", "key": "keep_years", "label": "Keep years"}]},
              {"do": "add_list", "key": "invoices", "label": "Invoices", "singular": "Invoice", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "link", "key": "buyer", "label": "Buyer", "to": "orgs"},
                  {"type": "date", "key": "invoice_date", "label": "Invoice date"}]},
              {"do": "design_op", "op": {"op": "update_table", "table": "invoices", "keep_for": {
                  "years": "coalesce(buyer.keep_years, 7)", "from": "invoice_date"}}}])
    assert schema.check(bp) == []
    ws = _ws(app, bp)
    e = Engine(ws)
    short, long_ = e.create("orgs", {"name": "Short", "keep_years": 2}), e.create("orgs", {"name": "Long"})
    three = (e.today - timedelta(days=365 * 3 + 2)).isoformat()
    a = e.create("invoices", {"name": "A", "buyer": short.id, "invoice_date": three})
    b = e.create("invoices", {"name": "B", "buyer": long_.id, "invoice_date": three})
    assert retention.run(ws) == (1, 0) and Engine(ws).get(a.id) is None and Engine(ws).get(b.id) is not None


def test_a_percentage_field_keeps_the_ais_confidence_as_a_fraction():
    action = {"confidence": "match_confidence", "sure_above": 80}
    assert automations._sureness(action, {"sure": True, "confidence": 92}, {"x": 1}, fraction=True)["values"]["match_confidence"] == 0.92


def test_an_example_meets_a_control_that_needs_another_record(app):
    """A contract can't go for signing unless a delegation limit covers it: the simulator adds the limit an administrator
    would, rather than the example stopping there; a step meant to be stopped still is."""
    from app.builder import trial
    bp = _ok([{"do": "add_list", "key": "orgs", "label": "Orgs", "singular": "Org", "fields": [{"type": "text", "key": "name", "label": "Name"}]},
              {"do": "add_list", "key": "limits", "label": "Limits", "singular": "Limit", "fields": [
                  {"type": "link", "key": "buyer", "label": "Buyer", "to": "orgs", "required": True},
                  {"type": "text", "key": "document", "label": "Document", "required": True}, {"type": "money", "key": "up_to", "label": "Up to", "required": True}]},
              {"do": "add_list", "key": "deals", "label": "Deals", "singular": "Deal", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "link", "key": "buyer", "label": "Buyer", "to": "orgs"},
                  {"type": "money", "key": "value", "label": "Value"}],
               "stages": {"stages": ["Draft", "Signing"], "moves": [{"from": ["Draft"], "to": "Signing", "button": "Sign"}]}},
              {"do": "add_validation", "list": "deals", "only_if": [{"field": "status", "op": "equals", "value": "Signing"}],
               "check": 'count(limits where buyer == this.buyer and document == "Deal" and up_to >= this.value) > 0',
               "message": "No limit covers this deal"}])
    ws = _ws(app, bp)
    steps = [{"do": "add", "as": "o", "table": "orgs", "values": {"name": "Pilbara"}},
             {"do": "add", "as": "d", "table": "deals", "values": {"name": "Big", "buyer": "o", "value": 5000}}]
    went = {"title": "Signed", "steps": steps + [{"do": "move", "record": "d", "to": "Signing"}],
            "expect": [{"record": "d", "field": "status", "equals": "Signing"}, {"table": "limits", "count": 1}]}
    stopped = {"title": "Stopped", "steps": steps + [{"do": "move", "record": "d", "to": "Signing", "refused": True}],
               "expect": [{"record": "d", "field": "status", "equals": "Draft"}]}
    _, (one, two) = trial.try_out(ws, [went, stopped], screens=False)
    assert one["ok"], one
    assert two["ok"], two


def test_what_waits_for_someone_includes_their_own_next_moves(app):
    """The original's "Needs you": an order the seller proposed changes to waits for the purchasing officer's Send again,
    though nobody asked them anything; a request turned down waits for whoever raised it to change and send it again."""
    from app.web.erp import ready_for
    bp = _ok([{"do": "set_team", "key": "buyers", "name": "Buyers"},
              {"do": "add_list", "key": "orders", "label": "Orders", "singular": "Order", "fields": [{"type": "text", "key": "name", "label": "Name"}],
               "stages": {"stages": ["Draft", "Changes proposed", "Sent", "Turned down"], "moves": [
                   {"from": ["Draft"], "to": "Sent", "button": "Send"},
                   {"from": ["Changes proposed"], "to": "Sent", "button": "Send again", "who": ["buyers"]},
                   {"from": ["Turned down"], "to": "Draft", "button": "Change and resubmit"}]}}])
    ws = _ws(app, bp)
    people = {}
    for name, teams in (("bea", ["buyers"]), ("sam", [])):
        people[name] = User(email=f"{name}@example.com", name=name.title())
        people[name].set_password("x" * 12)
        db.session.add(people[name])
        db.session.flush()
        db.session.add(Membership(workspace_id=ws.id, user_id=people[name].id, role="editor", teams=teams))
    db.session.commit()

    def engine(name):
        return Engine(ws, by=name, member=Membership.query.filter_by(workspace_id=ws.id, user_id=people[name].id).one())
    e = Engine(ws)
    proposed = e.create("orders", {"name": "Hose", "status": "Changes proposed"})
    e.create("orders", {"name": "Draft one"})
    mine = engine("sam").create("orders", {"name": "Gloves"})
    e.update(e.get(mine.id), {"status": "Turned down"})
    assert [(x["r"].id, x["move"]) for x in ready_for(engine("bea"))] == [(proposed.id, "Send again")]
    assert [(x["r"].id, x["move"]) for x in ready_for(engine("sam"))] == [(mine.id, "Change and resubmit")]


def test_a_line_field_named_like_its_records_field_has_its_own_choices(app):
    """A request's Unit is its business unit; its line's Unit is a unit of measure. The line's picker was offered the
    business units (none), so a line could never be saved."""
    from app.web.erp_records import _options
    bp = _ok([{"do": "add_list", "key": "units", "label": "Business units", "singular": "Business unit", "fields": [{"type": "text", "key": "name", "label": "Name"}]},
              {"do": "add_list", "key": "measures", "label": "Measures", "singular": "Measure", "fields": [{"type": "text", "key": "name", "label": "Name"}]},
              {"do": "add_list", "key": "asks", "label": "Asks", "singular": "Ask", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "link", "key": "unit", "label": "Unit", "to": "units"}]},
              {"do": "add_list", "key": "ask_lines", "label": "Ask lines", "singular": "Ask line", "inside": "asks", "fields": [
                  {"type": "link", "key": "ask", "label": "Ask", "to": "asks"}, {"type": "link", "key": "unit", "label": "Unit", "to": "measures"}]}])
    ws = _ws(app, bp)
    e = Engine(ws)
    each = e.create("measures", {"name": "Each"})
    got = _options(e, schema.table(bp, "asks"), [schema.table(bp, "ask_lines")])
    assert got["unit"] == [] and got["ask_lines.unit"] == [(each.id, "Each")]


def test_whoever_raised_it_hears_it_was_decided_and_who_is_next(app):
    """After the approver says yes, the requester was told nothing, and the request said "Nothing more to do here" while
    purchasing had it to order."""
    from app.models import Approval, Notification
    from app.web.erp import next_up
    bp = _ok([{"do": "set_team", "key": "buyers", "name": "Purchasing"},
              {"do": "add_list", "key": "asks", "label": "Asks", "singular": "Ask", "fields": [{"type": "text", "key": "name", "label": "Name"}],
               "stages": {"stages": ["Draft", "Submitted", "Approved", "Ordered"], "moves": [
                   {"from": ["Draft"], "to": "Submitted", "button": "Submit"},
                   {"from": ["Approved"], "to": "Ordered", "button": "Order it", "who": ["buyers"]}]}},
              {"do": "add_rule", "rule": {"key": "ok", "name": "Approve it", "when": {"on": "reaches", "list": "asks", "stage": "Submitted"},
                                          "steps": [{"do": "approval", "who": "admin", "message": "OK?"},
                                                    {"do": "set", "values": [{"field": "status", "to": "Approved"}]}]}}])
    ws = _ws(app, bp)
    rita = User(email="rita@example.com", name="Rita")
    rita.set_password("x" * 12)
    db.session.add(rita)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=rita.id, role="editor"))
    db.session.commit()
    mine = Engine(ws, by="Rita", member=Membership.query.filter_by(workspace_id=ws.id, user_id=rita.id).one())
    ask = mine.create("asks", {"name": "Gloves"})
    mine.move(mine.get(ask.id), next(i for i, m, _ in mine.moves(mine.get(ask.id)) if m["to"] == "Submitted"))
    approval = Approval.query.filter_by(workspace_id=ws.id, status="pending").one()
    owner = User.query.filter_by(email="owner@example.com").one()
    automations.decide(Engine(ws), approval, True, "Olive", by_id=owner.id)
    told = Notification.query.filter_by(workspace_id=ws.id, user_id=rita.id).all()
    assert any("Olive approved your ask" in n.text for n in told)
    e = Engine(ws)
    assert e.get(ask.id).data["status"] == "Approved"
    assert next_up(e, schema.table(bp, "asks"), e.get(ask.id)) == "Purchasing, to order it"


def test_an_owner_deletes_their_app_and_everything_in_it(app, client):
    from app.models import Record, RecordChange, Version
    owner = User(email="olive@example.com", name="Olive")
    owner.set_password("password1234")
    db.session.add(owner)
    db.session.flush()
    ws = Workspace(owner_id=owner.id, name="Old shop", blueprint=_ok([{"do": "add_list", "key": "jobs", "label": "Jobs", "singular": "Job",
                                                                      "fields": [{"type": "text", "key": "name", "label": "Name"}]}]),
                   state={}, settings={}, version=1)
    db.session.add(ws)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=owner.id, role="owner"))
    db.session.add(Version(workspace_id=ws.id, number=1, blueprint=ws.blueprint, changes=[], source="start", by="Olive"))
    db.session.commit()
    Engine(ws).create("jobs", {"name": "Fence"})
    wid = ws.id
    client.post("/login", data={"email": "olive@example.com", "password": "password1234"})
    refused = client.post(f"/w/{wid}/settings/delete", data={"name": "Wrong"})
    assert refused.status_code == 400 and db.session.get(Workspace, wid) is not None
    done = client.post(f"/w/{wid}/settings/delete", data={"name": "Old shop"})
    assert done.status_code in (302, 303) and db.session.get(Workspace, wid) is None
    assert not Record.query.filter_by(workspace_id=wid).count() and not RecordChange.query.filter_by(workspace_id=wid).count()
    assert not Membership.query.filter_by(workspace_id=wid).count() and not Version.query.filter_by(workspace_id=wid).count()


def test_outside_people_see_what_belongs_to_the_company_their_own_record_names(app):
    """A seller's people each name their seller; the seller's profile and its certificates reach them through that, with
    no list of people kept on the seller, and another seller's never do."""
    from app.engine import outside
    bp = _ok([
        {"do": "add_list", "key": "sellers", "label": "Sellers", "singular": "Seller", "fields": [{"type": "text", "key": "name", "label": "Name"}]},
        {"do": "add_list", "key": "seller_people", "label": "Seller people", "singular": "Seller person", "fields": [
            {"type": "text", "key": "name", "label": "Name"}, {"type": "email", "key": "email", "label": "Email"},
            {"type": "link", "key": "seller", "label": "Seller", "to": "sellers"}]},
        {"do": "add_list", "key": "certificates", "label": "Certificates", "singular": "Certificate", "fields": [
            {"type": "text", "key": "kind", "label": "Kind"}, {"type": "link", "key": "seller", "label": "Seller", "to": "sellers", "required": True}]},
        {"do": "design_op", "op": {"op": "set_portal", "table": "seller_people", "email_field": "email", "title": "Seller portal"}},
        {"do": "design_op", "op": {"op": "update_table", "table": "sellers", "portal": {"via": "seller", "edit": ["name"]}}},
        {"do": "design_op", "op": {"op": "update_table", "table": "certificates", "portal": {"via": "seller", "can_add": True}}},
    ])
    assert not schema.check(bp)
    ws = _ws(app, bp)
    e = Engine(ws)
    acme, bolt = e.create("sellers", {"name": "Acme"}), e.create("sellers", {"name": "Bolt"})
    ann = e.create("seller_people", {"name": "Ann", "email": "ann@acme.example", "seller": acme.id})
    e.create("seller_people", {"name": "Bo", "email": "bo@bolt.example", "seller": bolt.id})
    mine, theirs = e.create("certificates", {"kind": "ISO 9001", "seller": acme.id}), e.create("certificates", {"kind": "ISO 9001", "seller": bolt.id})
    assert e.portal_reaches(acme, ann.id) and not e.portal_reaches(bolt, ann.id)
    assert e.portal_reaches(mine, ann.id) and not e.portal_reaches(theirs, ann.id)
    form = outside.addable(e, schema.portal_of(bp, "seller_people"), ann, "certificates")
    assert [i for i, _ in form["options"]["seller"]] == [acme.id]  # only their own seller to add it for
    with pytest.raises(FieldErrors) as refused:  # never for another seller
        outside.add(e, schema.portal_of(bp, "seller_people"), ann, "certificates", {"kind": "ISO 14001", "seller": bolt.id})
    assert "open to you" in refused.value.errors["seller"]


def test_two_reminders_on_the_same_date_are_both_sent(app):
    """A certificate reminded 30 days before it expires and again on the day: each reminder keeps its own mark, so the
    first never stops the second."""
    from app.builder import trial
    from app.lang import checks
    bp = _ok([{"do": "add_list", "key": "certs", "label": "Certs", "singular": "Cert", "fields": [
        {"type": "text", "key": "name", "label": "Name"}, {"type": "email", "key": "email", "label": "Email"},
        {"type": "date", "key": "expires_on", "label": "Expiry date", "required": True}]},
        {"do": "reminder_before", "key": "ahead", "list": "certs", "date_field": "expires_on", "days": 30, "email_to": "email"},
        {"do": "reminder_before", "key": "on_day", "list": "certs", "date_field": "expires_on", "days": 0, "email_to": "email"}])
    marks = [f["key"] for f in schema.table(bp, "certs")["fields"] if f["key"].startswith("reminded_")]
    assert len(set(marks)) == 2
    ws = _ws(app, bp)
    _, results = trial.try_out(ws, checks(bp), screens=False, blueprint=bp)
    assert all(r["ok"] for r in results), [(r["title"], r.get("plain") or r.get("problem")) for r in results if not r["ok"]]


def test_outside_people_send_documents_through_the_portal(app, client):
    """A seller's person adds a certificate with the certificate itself attached, can't add one without it when it's
    required, and adds another file to one of theirs while they may change it."""
    from app.engine import outside
    from app.models import File
    bp = _ok([
        {"do": "add_list", "key": "sellers", "label": "Sellers", "singular": "Seller", "fields": [{"type": "text", "key": "name", "label": "Name"}]},
        {"do": "add_list", "key": "seller_people", "label": "Seller people", "singular": "Seller person", "fields": [
            {"type": "text", "key": "name", "label": "Name"}, {"type": "email", "key": "email", "label": "Email"},
            {"type": "link", "key": "seller", "label": "Seller", "to": "sellers"}]},
        {"do": "add_list", "key": "certificates", "label": "Certificates", "singular": "Certificate", "fields": [
            {"type": "text", "key": "kind", "label": "Kind"}, {"type": "link", "key": "seller", "label": "Seller", "to": "sellers", "required": True},
            {"type": "file", "key": "document", "label": "Certificate", "required": True}]},
        {"do": "design_op", "op": {"op": "set_portal", "table": "seller_people", "email_field": "email", "title": "Seller portal"}},
        {"do": "design_op", "op": {"op": "update_table", "table": "certificates", "portal": {"via": "seller", "can_add": True, "edit": ["kind", "document"]}}},
    ])
    ws = _ws(app, bp)
    e = Engine(ws)
    acme = e.create("sellers", {"name": "Acme"})
    ann = e.create("seller_people", {"name": "Ann", "email": "ann@acme.example", "seller": acme.id})
    portal = schema.portal_of(bp, "seller_people")
    assert "document" in [f["key"] for f in outside.addable(e, portal, ann, "certificates")["fields"]]
    with pytest.raises(FieldErrors) as refused:
        outside.add(e, portal, ann, "certificates", {"kind": "ISO 9001", "seller": acme.id})
    assert "Certificate is required" in refused.value.errors["document"]
    cert = outside.add(e, portal, ann, "certificates", {"kind": "ISO 9001", "seller": acme.id, "document": [("iso9001.pdf", b"%PDF-1.4 cert")]})
    assert [f.name for f in File.query.filter_by(record_id=cert.id, field="document", deleted_at=None)] == ["iso9001.pdf"]
    outside.change(e, ann, schema.table(bp, "certificates"), e.get(cert.id), {"document": [("surveillance-audit.pdf", b"%PDF-1.4 audit")]})
    assert File.query.filter_by(record_id=cert.id, field="document", deleted_at=None).count() == 2
    import io  # and on the portal's own pages: the form sends the file with the record
    person = User(email="ann@acme.example", name="Ann")
    person.set_password("password1234")
    db.session.add(person)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=person.id, role="portal", contact_id=ann.id))
    db.session.commit()
    client.post("/login", data={"email": "ann@acme.example", "password": "password1234"})
    assert 'type="file" name="document"' in client.get(f"/portal/{ws.id}/t/certificates/new").get_data(as_text=True)
    sent = client.post(f"/portal/{ws.id}/t/certificates/new", data={"kind": "ISO 45001", "seller": acme.id,
                                                                    "document": (io.BytesIO(b"%PDF-1.4 safety"), "iso45001.pdf")})
    assert sent.status_code in (302, 303), sent.get_data(as_text=True)[:500]
    made = next(r for r in Engine(ws).rows("certificates") if r.data.get("kind") == "ISO 45001")
    assert [f.name for f in File.query.filter_by(record_id=made.id, field="document")] == ["iso45001.pdf"]
    assert "iso45001.pdf" in client.get(f"/portal/{ws.id}/t/certificates/{made.id}").get_data(as_text=True)


def test_an_email_links_to_the_record_in_the_portal_and_to_signing_up(app, monkeypatch):
    """An invitation to bid takes the seller straight to the tender in their portal; an invitation to join takes a
    newcomer to the portal's sign-up page; staff are sent to the record in the app."""
    from app import outbox
    from app.connectors import microsoft
    from app.models import Outbox
    monkeypatch.setattr(outbox, "at_once", lambda: False)
    monkeypatch.setattr(microsoft, "status", lambda ws_: {"connected": True})
    bp = _ok([
        {"do": "add_list", "key": "sellers", "label": "Sellers", "singular": "Seller", "fields": [
            {"type": "text", "key": "name", "label": "Name"}, {"type": "email", "key": "email", "label": "Email"}]},
        {"do": "add_list", "key": "seller_people", "label": "Seller people", "singular": "Seller person", "fields": [
            {"type": "text", "key": "name", "label": "Name"}, {"type": "email", "key": "email", "label": "Email"},
            {"type": "link", "key": "seller", "label": "Seller", "to": "sellers"}]},
        {"do": "add_list", "key": "invitations", "label": "Invitations", "singular": "Invitation", "fields": [
            {"type": "text", "key": "tender", "label": "Tender"}, {"type": "link", "key": "seller", "label": "Seller", "to": "sellers", "required": True}]},
        {"do": "design_op", "op": {"op": "set_portal", "table": "seller_people", "email_field": "email", "title": "Seller portal",
                                   "sign_up": {"fields": ["name"]}}},
        {"do": "design_op", "op": {"op": "update_table", "table": "invitations", "portal": {"via": "seller"}}},
        {"do": "add_rule", "rule": {"key": "invite", "name": "Invite them", "when": {"on": "added", "list": "invitations"}, "steps": [
            {"do": "email", "to": "seller.email", "subject": "Invited to bid", "body": "Open it: {{record.portal_link}} New here? {{join_link}} (staff: {{app_link}})"}]}},
    ])
    ws = _ws(app, bp)
    e = Engine(ws)
    acme = e.create("sellers", {"name": "Acme", "email": "bids@acme.example"})
    invite = e.create("invitations", {"tender": "Haul road resurfacing", "seller": acme.id})
    body = Outbox.query.filter_by(workspace_id=ws.id).one().body
    assert f"/portal/{ws.id}/t/invitations/{invite.id}" in body
    assert f"/join/{ws.id}/seller_people" in body
    assert f"/w/{ws.id}/app/t/invitations/{invite.id}" in body


def test_a_sensitive_field_shows_as_many_of_its_last_digits_as_it_says(app):
    """A seller's account number shows ••••789 (three digits, as the owner asked), and a staff-restricted field the
    portal names outright is the seller's to keep up to date."""
    from app.engine import outside
    from app.engine.records import mask
    bp = _ok([
        {"do": "add_list", "key": "sellers", "label": "Sellers", "singular": "Seller", "fields": [
            {"type": "text", "key": "name", "label": "Name"},
            {"type": "text", "key": "account_number", "label": "Account number", "sensitive": True, "shows_last": 3, "visible_to": "admin"}]},
        {"do": "add_list", "key": "seller_people", "label": "Seller people", "singular": "Seller person", "fields": [
            {"type": "text", "key": "name", "label": "Name"}, {"type": "email", "key": "email", "label": "Email"},
            {"type": "link", "key": "seller", "label": "Seller", "to": "sellers"}]},
        {"do": "design_op", "op": {"op": "set_portal", "table": "seller_people", "email_field": "email", "title": "Seller portal"}},
        {"do": "design_op", "op": {"op": "update_table", "table": "sellers", "portal": {"via": "seller", "edit": ["name", "account_number"]}}},
    ])
    assert schema.field(schema.table(bp, "sellers"), "account_number")["shows_last"] == 3
    ws = _ws(app, bp)
    e = Engine(ws)
    acme = e.create("sellers", {"name": "Acme"})
    ann = e.create("seller_people", {"name": "Ann", "email": "ann@acme.example", "seller": acme.id})
    t = schema.table(bp, "sellers")
    assert "account_number" in [f["key"] for f in outside.editable(e, t, e.get(acme.id))]
    outside.change(e, ann, t, e.get(acme.id), {"account_number": "123456789"})
    assert Engine(ws).value(Engine(ws).get(acme.id), "account_number") == "••••789" == mask("123456789", 3)


def test_a_link_defaults_to_a_record_named_by_its_code(app, client):
    """An item's unit is EA unless said otherwise: a new one is linked to that unit, and its form starts with it picked."""
    bp = _ok([
        {"do": "add_list", "key": "uom", "label": "Units", "singular": "Unit", "fields": [{"type": "text", "key": "code", "label": "Code"}]},
        {"do": "add_list", "key": "items", "label": "Items", "singular": "Item", "fields": [
            {"type": "text", "key": "name", "label": "Name"}, {"type": "link", "key": "unit", "label": "Unit", "to": "uom", "default": "EA"}]}])
    ws = _ws(app, bp)
    e = Engine(ws)
    e.create("uom", {"code": "KG"})
    each = e.create("uom", {"code": "EA"})
    assert e.create("items", {"name": "Gloves"}).data["unit"] == each.id
    assert e.starting_value(schema.field(schema.table(bp, "items"), "unit")) == each.id
    client.post("/login", data={"email": "owner@example.com", "password": "x" * 12})
    page = client.get(f"/w/{ws.id}/app/t/items/new").get_data(as_text=True)
    unit = page[page.index('name="unit"'):][:1500]
    assert re.search(rf'value="{each.id}"[^>]*selected', unit), unit


def test_a_check_names_the_apps_starting_data(app):
    """Units of measure the builder added as the app's starting data are in the simulator's copy too, so a check can
    name one (a pair of gloves, PR), and an item left without a unit gets its default (EA)."""
    from app.lang import tools
    owner = User(email="o2@example.com", name="Olive")
    owner.set_password("x" * 12)
    db.session.add(owner)
    db.session.flush()
    ws = Workspace(owner_id=owner.id, name="Catalogue", blueprint={}, state={}, settings={}, version=0)
    db.session.add(ws)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=owner.id, role="owner"))
    db.session.commit()
    done = tools.apply(ws, [
        {"do": "add_list", "key": "uom", "label": "Units", "singular": "Unit", "fields": [{"type": "text", "key": "code", "label": "Code"}]},
        {"do": "add_records", "list": "uom", "records": [{"code": "EA"}, {"code": "PR"}]},
        {"do": "add_list", "key": "items", "label": "Items", "singular": "Item", "fields": [
            {"type": "text", "key": "name", "label": "Name"}, {"type": "link", "key": "unit", "label": "Unit", "to": "uom", "default": "EA"}]}], 0)
    assert done["saved"], done
    kept = tools.add_check(ws, "Gloves come in pairs, bolts each", [
        {"do": "add", "table": "items", "as": "i1", "values": {"name": "Gloves", "unit": "PR"}},
        {"do": "add", "table": "items", "as": "i2", "values": {"name": "Bolt"}}],
        [{"record": "i1", "field": "unit", "equals": "PR"}, {"record": "i2", "field": "unit", "equals": "EA"}])
    assert kept["kept"] and kept["ok"], kept


def test_a_new_record_starts_from_the_defaults_of_whoever_adds_it(app, client):
    """A request is requested by whoever adds it, and starts with their own cost centre, read from their record among
    the business's people: when it's saved, and on its form before then."""
    bp = _ok([
        {"do": "add_list", "key": "cost_centres", "label": "Cost centres", "singular": "Cost centre", "fields": [{"type": "text", "key": "name", "label": "Name"}]},
        {"do": "add_list", "key": "people", "label": "People", "singular": "Person", "fields": [
            {"type": "text", "key": "name", "label": "Name"}, {"type": "person", "key": "login", "label": "Signs in as"},
            {"type": "link", "key": "default_cost_centre", "label": "Default cost centre", "to": "cost_centres"}]},
        {"do": "add_list", "key": "requests", "label": "Requests", "singular": "Request", "fields": [
            {"type": "text", "key": "title", "label": "Title"}, {"type": "person", "key": "requested_by", "label": "Requested by", "default": "me"},
            {"type": "link", "key": "cost_centre", "label": "Cost centre", "to": "cost_centres",
             "default": "{{first(people where login == this.requested_by).default_cost_centre}}"}]}])
    ws = _ws(app, bp)
    e = Engine(ws)
    e.create("cost_centres", {"name": "Admin"})
    plant = e.create("cost_centres", {"name": "Processing plant"})
    owner = User.query.filter_by(email="owner@example.com").one()
    e.create("people", {"name": "Olive", "login": owner.id, "default_cost_centre": plant.id})
    mine = Engine(ws, member=Membership.query.filter_by(workspace_id=ws.id, user_id=owner.id).one())
    made = mine.create("requests", {"title": "Gloves"})
    assert made.data["requested_by"] == owner.id and made.data["cost_centre"] == plant.id
    client.post("/login", data={"email": "owner@example.com", "password": "x" * 12})
    page = client.get(f"/w/{ws.id}/app/t/requests/new").get_data(as_text=True)
    picker = page[page.index('name="cost_centre"'):][:1500]
    assert re.search(rf'value="{plant.id}"[^>]*selected', picker), picker


def test_nobody_approves_above_their_own_limit(app):
    """A move held to whoever presses it: their delegation limit covers the amount exactly at the limit, and not one cent
    above it; someone with no limit can't approve at all."""
    bp = _ok([
        {"do": "add_list", "key": "limits", "label": "Delegation limits", "singular": "Delegation limit", "fields": [
            {"type": "person", "key": "person", "label": "Person"}, {"type": "money", "key": "limit", "label": "Limit"}]},
        {"do": "add_list", "key": "asks", "label": "Asks", "singular": "Ask", "fields": [{"type": "money", "key": "amount", "label": "Amount"}],
         "stages": {"stages": ["Pending", "Approved"], "moves": [{"from": ["Pending"], "to": "Approved", "button": "Approve",
                                                                  "only_if": ["count(limits where person == me and limit >= this.amount) > 0"]}]}}])
    ws = _ws(app, bp)
    bea = User(email="bea@example.com", name="Bea")
    bea.set_password("x" * 12)
    cy = User(email="cy@example.com", name="Cy")
    cy.set_password("x" * 12)
    db.session.add_all([bea, cy])
    db.session.flush()
    db.session.add_all([Membership(workspace_id=ws.id, user_id=bea.id, role="editor"), Membership(workspace_id=ws.id, user_id=cy.id, role="editor")])
    db.session.commit()
    Engine(ws).create("limits", {"person": bea.id, "limit": 5000})
    as_bea = lambda: Engine(ws, member=Membership.query.filter_by(user_id=bea.id).one())  # noqa: E731
    as_cy = Engine(ws, member=Membership.query.filter_by(user_id=cy.id).one())
    at_limit, over = Engine(ws).create("asks", {"amount": 5000}), Engine(ws).create("asks", {"amount": 5000.01})

    def approve(engine, record):
        i, _, problem = next(x for x in engine.moves(engine.get(record.id)) if x[1]["to"] == "Approved")
        if problem:
            raise FieldErrors({"_": problem})
        engine.move(engine.get(record.id), i)
    with pytest.raises(FieldErrors):
        approve(as_bea(), over)
    with pytest.raises(FieldErrors):  # and pressed all the same, the engine refuses it
        bea_now = as_bea()
        bea_now.move(bea_now.get(over.id), next(i for i, m, _ in bea_now.moves(bea_now.get(over.id)) if m["to"] == "Approved"))
    with pytest.raises(FieldErrors):
        approve(as_cy, at_limit)
    approve(as_bea(), at_limit)
    assert Engine(ws).get(at_limit.id).data["status"] == "Approved" and Engine(ws).get(over.id).data["status"] == "Pending"


def test_a_new_lists_settings_can_name_fields_added_after_it_in_the_same_batch(app):
    """An order made in one batch with its total added by a patch of its own, its portal showing the total, and the
    seller's Acknowledge through that portal: none of it is refused for coming in the wrong order."""
    out = apply({}, [
        {"do": "add_list", "key": "sellers", "label": "Sellers", "singular": "Seller", "fields": [{"type": "text", "key": "name", "label": "Name"}]},
        {"do": "add_list", "key": "seller_people", "label": "Seller people", "singular": "Seller person", "fields": [
            {"type": "text", "key": "name", "label": "Name"}, {"type": "email", "key": "email", "label": "Email"},
            {"type": "link", "key": "seller", "label": "Seller", "to": "sellers"}]},
        {"do": "design_op", "op": {"op": "set_portal", "table": "seller_people", "email_field": "email", "title": "Seller portal"}},
        {"do": "add_list", "key": "orders", "label": "Orders", "singular": "Order", "fields": [
            {"type": "link", "key": "seller", "label": "Seller", "to": "sellers", "required": True}],
         "stages": {"stages": ["Sent", "Acknowledged"], "moves": [{"from": ["Sent"], "to": "Acknowledged", "button": "Acknowledge", "who": ["portal"]}]}},
        {"do": "add_list", "key": "order_lines", "label": "Order lines", "singular": "Order line", "inside": "orders", "fields": [
            {"type": "link", "key": "order", "label": "Order", "to": "orders", "required": True}, {"type": "money", "key": "amount", "label": "Amount"}]},
        {"do": "add_field", "list": "orders", "field": {"type": "total", "key": "total", "label": "Total", "of": "order_lines", "link": "order", "field": "amount"}},
        {"do": "design_op", "op": {"op": "update_table", "table": "orders", "portal": {"via": "seller", "fields": ["seller", "status", "total"]}}}])
    assert not out.problems, out.problems
    t = schema.table(out.blueprint, "orders")
    assert t["portal"]["fields"] == ["seller", "status", "total"] and t["workflow"]["transitions"][0]["who"] == ["portal"]


def test_an_outside_person_moves_a_locked_record_by_its_buttons(app):
    """A sent order is locked against changes, and the seller still acknowledges it in the portal, as staff move a
    locked record by its buttons; changing it there is still refused."""
    from app.engine import outside
    bp = _ok([
        {"do": "add_list", "key": "sellers", "label": "Sellers", "singular": "Seller", "fields": [{"type": "text", "key": "name", "label": "Name"}]},
        {"do": "add_list", "key": "seller_people", "label": "Seller people", "singular": "Seller person", "fields": [
            {"type": "text", "key": "name", "label": "Name"}, {"type": "email", "key": "email", "label": "Email"},
            {"type": "link", "key": "seller", "label": "Seller", "to": "sellers"}]},
        {"do": "design_op", "op": {"op": "set_portal", "table": "seller_people", "email_field": "email", "title": "Seller portal"}},
        {"do": "add_list", "key": "orders", "label": "Orders", "singular": "Order", "fields": [
            {"type": "link", "key": "seller", "label": "Seller", "to": "sellers", "required": True}, {"type": "text", "key": "note", "label": "Note"}],
         "stages": {"stages": ["Sent", "Acknowledged"], "moves": [{"from": ["Sent"], "to": "Acknowledged", "button": "Acknowledge", "who": ["portal"]}]}},
        {"do": "design_op", "op": {"op": "update_table", "table": "orders", "portal": {"via": "seller", "edit": ["note"]}}},
        {"do": "lock", "list": "orders", "stages": ["Sent", "Acknowledged"]}])
    ws = _ws(app, bp)
    e = Engine(ws)
    acme = e.create("sellers", {"name": "Acme"})
    ann = e.create("seller_people", {"name": "Ann", "email": "ann@acme.example", "seller": acme.id})
    order = e.create("orders", {"seller": acme.id})
    t = schema.table(bp, "orders")
    assert outside.editable(e, t, e.get(order.id)) == []  # locked: nothing to change
    i = next(i for i, m, _ in e.moves(e.get(order.id)) if m["to"] == "Acknowledged")
    outside.move(e, ann, t, e.get(order.id), i, by="Ann (through the portal)")
    assert Engine(ws).get(order.id).data["status"] == "Acknowledged"


def test_one_record_for_each_person_chosen_and_a_list_of_people_that_grows(app):
    """A tender's panel: a declaration for each member (item is that person, item.email theirs), and each one cleared
    is added to the tender's cleared panel, the people already there kept."""
    bp = _ok([
        {"do": "add_list", "key": "tenders", "label": "Tenders", "singular": "Tender", "fields": [
            {"type": "text", "key": "title", "label": "Title"}, {"type": "person", "key": "panel", "label": "Panel", "many": True},
            {"type": "person", "key": "cleared", "label": "Cleared", "many": True}]},
        {"do": "add_list", "key": "seats", "label": "Seats", "singular": "Seat", "fields": [
            {"type": "link", "key": "tender", "label": "Tender", "to": "tenders"}, {"type": "person", "key": "member", "label": "Member"},
            {"type": "email", "key": "email", "label": "Email"}], "stages": {"stages": ["To declare", "Cleared"]}},
        {"do": "add_rule", "rule": {"key": "seats", "name": "Seats", "when": {"on": "added", "list": "tenders"}, "steps": [
            {"do": "add_each", "list": "seats", "for_each": "panel", "copy_same": False, "values": [
                {"field": "tender", "formula": "record"}, {"field": "member", "formula": "item"}, {"field": "email", "formula": "item.email"}]}]}},
        {"do": "add_rule", "rule": {"key": "cleared", "name": "Cleared", "when": {"on": "reaches", "list": "seats", "stage": "Cleared"}, "steps": [
            {"do": "set", "on": "tender", "values": [{"field": "cleared", "formula": "coalesce(tender.cleared, []) + [record.member]"}]}]}}])
    ws = _ws(app, bp)
    people = []
    for name in ("Pat", "Quinn"):
        u = User(email=f"{name.lower()}@example.com", name=name)
        u.set_password("x" * 12)
        db.session.add(u)
        db.session.flush()
        db.session.add(Membership(workspace_id=ws.id, user_id=u.id, role="editor"))
        people.append(u)
    db.session.commit()
    e = Engine(ws)
    tender = e.create("tenders", {"title": "Pumps", "panel": [u.id for u in people]})
    seats = sorted(Engine(ws).rows("seats"), key=lambda r: r.id)
    assert [(s.data["member"], s.data["email"]) for s in seats] == [(u.id, u.email) for u in people]
    for seat in seats:
        e2 = Engine(ws)
        e2.update(e2.get(seat.id), {"status": "Cleared"})
    assert sorted(Engine(ws).get(tender.id).data["cleared"]) == sorted(u.id for u in people)


def test_a_lock_on_a_four_eyes_stage_is_checked_by_someone_else_moving_it(app):
    """A clause version is approved by a second person and then fixed: the lock's own check moves it as someone of the
    approving team (not whoever wrote it), and finds it fixed."""
    from app.builder import trial
    from app.lang import checks
    bp = _ok([{"do": "set_team", "key": "legal", "name": "Legal"},
              {"do": "add_list", "key": "versions", "label": "Versions", "singular": "Version", "fields": [
                  {"type": "long_text", "key": "wording", "label": "Wording"}, {"type": "person", "key": "written_by", "label": "Written by", "default": "me"}],
               "stages": {"stages": ["Draft", "Approved"], "moves": [{"from": ["Draft"], "to": "Approved", "button": "Approve", "who": ["legal"],
                                                                      "not_by": ["written_by", "creator"]}]}},
              {"do": "lock", "list": "versions", "stages": ["Approved"]}])
    ws = _ws(app, bp)
    _, results = trial.try_out(ws, checks(bp), screens=False, blueprint=bp)
    assert all(r["ok"] for r in results), [(r["title"], r.get("plain") or r.get("problem")) for r in results if not r["ok"]]


def test_a_portal_reaches_outside_people_through_several_links(app):
    """A clause change belongs to a negotiation round of a contract with a seller: it reaches that seller's people along
    all three links, and never another seller's."""
    bp = _ok([
        {"do": "add_list", "key": "sellers", "label": "Sellers", "singular": "Seller", "fields": [{"type": "text", "key": "name", "label": "Name"}]},
        {"do": "add_list", "key": "seller_people", "label": "Seller people", "singular": "Seller person", "fields": [
            {"type": "text", "key": "name", "label": "Name"}, {"type": "email", "key": "email", "label": "Email"},
            {"type": "link", "key": "seller", "label": "Seller", "to": "sellers"}]},
        {"do": "add_list", "key": "contracts", "label": "Contracts", "singular": "Contract", "fields": [
            {"type": "text", "key": "title", "label": "Title"}, {"type": "link", "key": "seller", "label": "Seller", "to": "sellers"}]},
        {"do": "add_list", "key": "rounds", "label": "Rounds", "singular": "Round", "fields": [
            {"type": "number", "key": "no", "label": "Round"}, {"type": "link", "key": "contract", "label": "Contract", "to": "contracts"}]},
        {"do": "add_list", "key": "changes", "label": "Changes", "singular": "Change", "fields": [
            {"type": "text", "key": "wording", "label": "Wording"}, {"type": "link", "key": "round", "label": "Round", "to": "rounds"}]},
        {"do": "design_op", "op": {"op": "set_portal", "table": "seller_people", "email_field": "email", "title": "Seller portal"}},
        {"do": "design_op", "op": {"op": "update_table", "table": "changes", "portal": {"via": "round.contract.seller", "can_add": True}}}])
    assert schema.table(bp, "changes")["portal"]["via"] == "round.contract.seller"
    ws = _ws(app, bp)
    e = Engine(ws)
    acme, bolt = e.create("sellers", {"name": "Acme"}), e.create("sellers", {"name": "Bolt"})
    ann = e.create("seller_people", {"name": "Ann", "email": "ann@acme.example", "seller": acme.id})
    bo = e.create("seller_people", {"name": "Bo", "email": "bo@bolt.example", "seller": bolt.id})
    rnd = e.create("rounds", {"no": 1, "contract": e.create("contracts", {"title": "Pumps", "seller": acme.id}).id})
    change = e.create("changes", {"wording": "Cap at 2x", "round": rnd.id})
    assert e.portal_reaches(change, ann.id) and not e.portal_reaches(change, bo.id)
    from app.engine import outside
    form = outside.addable(e, schema.portal_of(bp, "seller_people"), ann, "changes")
    assert [i for i, _ in form["options"]["round"]] == [rnd.id]


def test_a_move_held_back_says_why_in_the_owners_words(app):
    """Approving over the limit is refused with what the owner wrote ("Your delegation limit doesn't cover this"), not the
    formula read out in words."""
    bp = _ok([{"do": "add_list", "key": "asks", "label": "Asks", "singular": "Ask", "fields": [{"type": "money", "key": "amount", "label": "Amount"}],
               "stages": {"stages": ["Pending", "Approved"], "moves": [{"from": ["Pending"], "to": "Approved", "button": "Approve",
                                                                      "only_if": ["amount < 100"], "message": "Your delegation limit doesn't cover this"}]}}])
    ws = _ws(app, bp)
    e = Engine(ws)
    ask = e.create("asks", {"amount": 250})
    assert [p for _, m, p in e.moves(e.get(ask.id)) if m["to"] == "Approved"] == ["Your delegation limit doesn't cover this"]


def test_a_changed_move_is_exactly_as_sent(app):
    """change_move sends a move whole: a separation of duties left out of it is taken away, not kept from before."""
    bp = _ok([{"do": "add_list", "key": "asks", "label": "Asks", "singular": "Ask", "fields": [{"type": "money", "key": "amount", "label": "Amount"}],
               "stages": {"stages": ["Pending", "Approved"], "moves": [{"from": ["Pending"], "to": "Approved", "button": "Approve", "not_by": ["creator"], "step_up": True}]}}])
    bp = _ok([{"do": "change_move", "list": "asks", "button": "Approve", "move": {"from": ["Pending"], "to": "Approved", "button": "Approve"}}], bp)
    move = schema.table(bp, "asks")["workflow"]["transitions"][0]
    assert "not_by" not in move and "step_up" not in move


def test_a_person_field_that_starts_as_me_is_whoever_adds_the_record(app):
    """A contract's manager is whoever drafts it, whatever the field is called; the app's own rules are nobody."""
    bp = _ok([{"do": "add_list", "key": "contracts", "label": "Contracts", "singular": "Contract", "fields": [
        {"type": "text", "key": "title", "label": "Title"}, {"type": "person", "key": "contract_manager", "label": "Contract manager", "default": "me"}]}])
    ws = _ws(app, bp)
    owner = User.query.filter_by(email="owner@example.com").one()
    mine = Engine(ws, member=Membership.query.filter_by(workspace_id=ws.id, user_id=owner.id).one())
    assert mine.create("contracts", {"title": "Pumps"}).data["contract_manager"] == owner.id
    assert Engine(ws).system().create("contracts", {"title": "By a rule"}).data.get("contract_manager") is None


def test_a_person_field_of_the_record_named_like_creator_stays_that_field_in_not_by(app):
    """A claim a rule raises names who raised it in its own raised_by: approving it is never by that person, who isn't
    the record's creator (the rule is)."""
    bp = _ok([{"do": "add_list", "key": "claims", "label": "Claims", "singular": "Claim", "fields": [
        {"type": "money", "key": "amount", "label": "Amount"}, {"type": "person", "key": "raised_by", "label": "Raised by"}],
        "stages": {"stages": ["Raised", "Approved"], "moves": [{"from": ["Raised"], "to": "Approved", "button": "Approve", "not_by": ["raised_by", "creator"]}]}}])
    assert schema.table(bp, "claims")["workflow"]["transitions"][0]["not_by"] == ["raised_by", "creator"]


def test_a_line_being_changed_is_item_inside_a_where_too(app):
    """Approved new prices go on a contract's lines: each line being changed (item) finds its own new price among the
    adjustment's lines, inside the where."""
    bp = _ok([
        {"do": "add_list", "key": "contracts", "label": "Contracts", "singular": "Contract", "fields": [{"type": "text", "key": "title", "label": "Title"}]},
        {"do": "add_list", "key": "lines", "label": "Lines", "singular": "Line", "fields": [
            {"type": "link", "key": "contract", "label": "Contract", "to": "contracts"}, {"type": "text", "key": "name", "label": "Name"},
            {"type": "money", "key": "price", "label": "Price"}]},
        {"do": "add_list", "key": "adjs", "label": "Adjustments", "singular": "Adjustment", "fields": [
            {"type": "link", "key": "contract", "label": "Contract", "to": "contracts"}], "stages": {"stages": ["Draft", "Approved"]}},
        {"do": "add_list", "key": "alines", "label": "Adjustment lines", "singular": "Adjustment line", "fields": [
            {"type": "link", "key": "adj", "label": "Adjustment", "to": "adjs"}, {"type": "link", "key": "line", "label": "Line", "to": "lines"},
            {"type": "money", "key": "new_price", "label": "New price"}]},
        {"do": "add_rule", "rule": {"key": "apply", "name": "Apply", "when": {"on": "reaches", "list": "adjs", "stage": "Approved"}, "steps": [
            {"do": "update_each", "list": "lines", "link": "contract", "of": "contract", "values": [
                {"field": "price", "formula": "first(alines where adj == record.id and line == item.id).new_price"}]}]}}])
    ws = _ws(app, bp)
    e = Engine(ws)
    c = e.create("contracts", {"title": "Pumps"})
    a, b = e.create("lines", {"contract": c.id, "name": "Pump", "price": 100}), e.create("lines", {"contract": c.id, "name": "Seal", "price": 10})
    adj = e.create("adjs", {"contract": c.id})
    e.create("alines", {"adj": adj.id, "line": a.id, "new_price": 103.85})
    e.create("alines", {"adj": adj.id, "line": b.id, "new_price": 10.39})
    e.update(e.get(adj.id), {"status": "Approved"})
    after = Engine(ws)
    assert (after.get(a.id).data["price"], after.get(b.id).data["price"]) == (103.85, 10.39)


def test_a_documents_passages_know_their_page(app):
    """A contract PDF's passages each carry the page they start on, so an answer cites the document and the page."""
    from app.knowledge import paged
    text = "\n\n".join(f"--- page {n} ---\nClause {n}. " + " ".join(f"term{n}x{i}" for i in range(260)) for n in range(1, 4))
    pages = [page for _, page in paged(text)]
    assert pages[0] == 1 and pages[-1] == 3 and pages == sorted(pages)


def test_a_rule_copying_a_sensitive_field_copies_the_value_not_its_mask(app):
    """A seller's account number copied into a buyer's own copy by a rule is the account itself (sealed again, shown with
    the copy's own number of digits), never the mask it shows as."""
    from app.engine.records import reveal
    bp = _ok([{"do": "add_list", "key": "a", "label": "As", "singular": "A", "fields": [{"type": "text", "key": "acct", "label": "Account", "sensitive": True}]},
              {"do": "add_list", "key": "b", "label": "Bs", "singular": "B", "fields": [
                  {"type": "link", "key": "a", "label": "A", "to": "a"}, {"type": "text", "key": "acct", "label": "Account", "sensitive": True, "shows_last": 3}]},
              {"do": "add_rule", "rule": {"key": "c", "name": "Copy", "when": {"on": "added", "list": "a"}, "steps": [
                  {"do": "add", "list": "b", "copy_same": False, "values": [{"field": "a", "formula": "record"}, {"field": "acct", "formula": "record.acct"}]}]}}])
    ws = _ws(app, bp)
    Engine(ws).create("a", {"acct": "123456789"})
    copied = Engine(ws).rows("b")[0].data["acct"]
    assert reveal(copied) == "123456789" and copied["masked"] == "••••789"


def test_a_payment_file_is_sealed_with_its_fingerprint_and_a_changed_one_is_refused(app, client):
    """An approved run's bank file holds account numbers, so it's never kept: its fingerprint is, and a download is
    made again and handed out only while it still matches (an account changed since is refused)."""
    bp = _ok([
        {"do": "add_list", "key": "runs", "label": "Runs", "singular": "Run", "fields": [
            {"type": "text", "key": "name", "label": "Name"}, {"type": "text", "key": "fingerprint", "label": "Fingerprint"}],
         "stages": {"stages": ["Draft", "Approved"]}},
        {"do": "add_list", "key": "items", "label": "Items", "singular": "Item", "fields": [
            {"type": "link", "key": "run", "label": "Run", "to": "runs"}, {"type": "money", "key": "amount", "label": "Amount"},
            {"type": "text", "key": "account", "label": "Account", "sensitive": True}]},
        {"do": "add_document", "list": "runs", "key": "aba", "label": "Bank file", "format": "text", "extension": "aba", "lines": "items",
         "line": "1{{lpad(digits(line.account), 9, \" \")}}{{lpad(cents(line.amount), 10, \"0\")}}", "sealed_in": "fingerprint"},
        {"do": "add_rule", "rule": {"key": "seal", "name": "Seal", "when": {"on": "reaches", "list": "runs", "stage": "Approved"}, "steps": [
            {"do": "document", "document": "aba"}]}}])
    ws = _ws(app, bp)
    e = Engine(ws)
    run = e.create("runs", {"name": "Friday"})
    item = e.create("items", {"run": run.id, "amount": 120.5, "account": "123456789"})
    e.update(e.get(run.id), {"status": "Approved"})
    sealed = Engine(ws).get(run.id).data["fingerprint"]
    assert sealed and len(sealed) == 64
    from app.models import File
    assert File.query.filter_by(record_id=run.id).count() == 0  # never kept
    client.post("/login", data={"email": "owner@example.com", "password": "x" * 12})
    got = client.post(f"/w/{ws.id}/app/t/runs/{run.id}/document/aba")
    assert got.status_code == 200 and b"123456789" in got.data and got.headers["X-Content-SHA256"] == sealed
    e2 = Engine(ws)
    e2.update(e2.get(item.id), {"account": "999999999"})
    refused = client.post(f"/w/{ws.id}/app/t/runs/{run.id}/document/aba")
    assert refused.status_code in (302, 303) and b"999999999" not in refused.data


def test_count_distinct_tells_how_many_different_ones(app):
    """Demand for a category is published to sellers only when at least five different buyers stand behind it."""
    bp = _ok([{"do": "add_list", "key": "orgs", "label": "Organisations", "singular": "Organisation", "fields": [{"type": "text", "key": "name", "label": "Name"}]},
              {"do": "add_list", "key": "cats", "label": "Categories", "singular": "Category", "fields": [
                  {"type": "text", "key": "name", "label": "Name"},
                  {"type": "formula", "key": "buyers", "label": "Buyers", "formula": "count_distinct(asks where cat == this.id, buyer)", "shows": "number"}]},
              {"do": "add_list", "key": "asks", "label": "Asks", "singular": "Ask", "fields": [
                  {"type": "link", "key": "cat", "label": "Category", "to": "cats"}, {"type": "link", "key": "buyer", "label": "Buyer", "to": "orgs"}]}])
    ws = _ws(app, bp)
    e = Engine(ws)
    gloves = e.create("cats", {"name": "Gloves"})
    a, b = e.create("orgs", {"name": "A"}), e.create("orgs", {"name": "B"})
    for buyer in (a, a, b):
        e.create("asks", {"cat": gloves.id, "buyer": buyer.id})
    assert Engine(ws).value(Engine(ws).get(gloves.id), "buyers") == 2


def test_a_buttons_rule_knows_who_pressed_it(app):
    """A tool run a button makes is run by whoever pressed the button (me), not by nobody."""
    from app.engine import automations
    bp = _ok([{"do": "add_list", "key": "asks", "label": "Asks", "singular": "Ask", "fields": [{"type": "text", "key": "title", "label": "Title"}]},
              {"do": "add_list", "key": "runs", "label": "Runs", "singular": "Run", "fields": [
                  {"type": "link", "key": "ask", "label": "Ask", "to": "asks"}, {"type": "person", "key": "run_by", "label": "Run by"}]},
              {"do": "add_rule", "rule": {"key": "go", "name": "Go", "when": {"on": "button", "list": "asks", "label": "Go"}, "steps": [
                  {"do": "add", "list": "runs", "copy_same": False, "values": [{"field": "ask", "formula": "record"}, {"field": "run_by", "formula": "me"}]}]}}])
    ws = _ws(app, bp)
    owner = User.query.filter_by(email="owner@example.com").one()
    mine = Engine(ws, member=Membership.query.filter_by(workspace_id=ws.id, user_id=owner.id).one())
    ask = mine.create("asks", {"title": "Pumps"})
    automations.press(mine, next(a for a in bp["automations"] if a["key"] == "go"), mine.get(ask.id), "Olive")
    assert Engine(ws).rows("runs")[0].data["run_by"] == owner.id
