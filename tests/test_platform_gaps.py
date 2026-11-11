"""What replicating a whole marketplace (wawasu) needed that the platform couldn't say, each on a small app of its own:
text tests in formulas, a call out held to its allowed hosts (an address from a record too), a spreadsheet brought
into a list by a rule, the AI's confidence kept and judged, a held change approved only once its notice went out,
everyone made to sign in with two steps, record access through several links, a step-by-step form whose step is the
record's lines, a file's fingerprint in formulas, and records kept only as long as the list says."""
from datetime import timedelta

import pytest

from app.blueprint import expr, schema
from app.engine import automations, retention
from app.engine.records import Engine
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
