"""What building a whole multi-party app in one go with the Keel language found missing in the platform (a marketplace
where buyers and sellers share records), each shown on a small app of its own: teams and settings named by the lists
made in the same batch, people fields holding several, separation of duties kept on a list's own moves, the portal
reaching a record's record's people, a colleague of the outside person approving their change, who added a record read
by rules, whole lists read in a rule's values, and the record a rule made kept through its lines."""
from app.blueprint import schema
from app.engine import automations
from app.engine.records import Engine
from app.extensions import db
from app.lang import apply
from app.models import Approval, Membership, User, Workspace


def _ws(app, bp, email="owner@example.com"):
    owner = User(email=email, name="Olive")
    owner.set_password("x" * 12)
    db.session.add(owner)
    db.session.flush()
    ws = Workspace(owner_id=owner.id, name="Market", blueprint=bp, state={}, settings={})
    db.session.add(ws)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=owner.id, role="owner"))
    db.session.commit()
    return ws


def _outside(ws, email, name, contact_id):
    user = User(email=email, name=name)
    user.set_password("x" * 12)
    db.session.add(user)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=user.id, role="portal", contact_id=contact_id))
    db.session.commit()
    return user


def _ok(patches, bp=None):
    out = apply(bp or {}, patches)
    assert not out.problems, out.problems
    return out.blueprint


def test_teams_and_settings_made_with_the_lists_can_be_named_by_their_moves():
    """A team made in the same batch, and a portal set on a list made in it, used to come after the lists' moves were
    laid down, so a move for the team or for the outside person was refused."""
    bp = _ok([{"do": "set_team", "key": "buyers", "name": "Buyers"},
              {"do": "add_list", "key": "firms", "label": "Firms", "singular": "Firm", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "email", "key": "email", "label": "Email"}]},
              {"do": "add_list", "key": "orders", "label": "Orders", "singular": "Order", "fields": [
                  {"type": "link", "key": "firm", "label": "Firm", "to": "firms"}],
               "stages": {"stages": ["Draft", "Sent", "Acknowledged"], "moves": [
                   {"from": ["Draft"], "to": "Sent", "button": "Send", "who": ["buyers"]},
                   {"from": ["Sent"], "to": "Acknowledged", "button": "Acknowledge", "who": ["portal"]}]}},
              {"do": "design_op", "op": {"op": "set_portal", "portal": {"table": "firms", "email_field": "email"}}},
              {"do": "design_op", "op": {"op": "update_table", "table": "orders", "portal": {"via": "firm"}}}])
    moves = schema.table(bp, "orders")["workflow"]["transitions"]
    assert [m["who"] for m in moves] == [["buyers"], ["portal"]]


def test_a_person_field_can_hold_several_people():
    bp = _ok([{"do": "add_list", "key": "groups", "label": "Groups", "singular": "Group", "fields": [
        {"type": "text", "key": "name", "label": "Name"}, {"type": "person", "key": "members", "label": "Members", "many": True}]}])
    assert schema.field(schema.table(bp, "groups"), "members")["multiple"] is True
    out = apply({}, [{"do": "add_list", "key": "x", "label": "X", "singular": "X", "fields": [{"type": "date", "key": "d", "label": "D", "many": True}]}])
    assert out.problems and "only a person field" in out.problems[0]


def test_one_feature_on_each_of_two_lists_keeps_its_rule_on_its_own_list():
    """Two reminders before two lists' expiry dates of the same name: the second's rule was placed before its own field
    and moved to the first list, which had one."""
    lists = [{"do": "add_list", "key": k, "label": k.title(), "singular": k[:-1].title(), "fields": [
        {"type": "text", "key": "name", "label": "Name"}, {"type": "date", "key": "expires_on", "label": "Expires"}]} for k in ("licences", "policies")]
    bp = _ok(lists + [{"do": "reminder_before", "key": f"{k}_expiring", "list": k, "date_field": "expires_on", "days": 30} for k in ("licences", "policies")])
    assert {a["key"]: a["trigger"]["table"] for a in bp["automations"]} == {"licences_expiring": "licences", "policies_expiring": "policies"}


def test_a_lists_own_moves_keep_who_mustnt_make_them_and_a_fresh_sign_in():
    """Written with the list's stages, not_by and step_up were dropped without a word: whoever recommended an award could
    approve it. They're kept, several of them, a stage's mover and a person through links among them."""
    bp = _ok([{"do": "add_list", "key": "requests", "label": "Requests", "singular": "Request", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "person", "key": "requester", "label": "Requester"}]},
              {"do": "add_list", "key": "awards", "label": "Awards", "singular": "Award", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "link", "key": "request", "label": "Request", "to": "requests"}],
               "stages": {"stages": ["Draft", "Recommended", "Approved"], "moves": [
                   {"from": ["Draft"], "to": "Recommended", "button": "Recommend"},
                   {"from": ["Recommended"], "to": "Approved", "button": "Approve", "step_up": True,
                    "not_by": ["creator", "Recommended", "request.requester"]}]}}])
    approve = schema.table(bp, "awards")["workflow"]["transitions"][1]
    assert approve["not_by"] == ["creator", "Recommended", "request.requester"] and approve["step_up"] is True


def _three_hops():
    return {"name": "Market", "tables": [], "automations": [], "pages": [], "modules": [],
            **_ok([{"do": "add_list", "key": "contacts", "label": "Contacts", "singular": "Contact", "fields": [
                       {"type": "text", "key": "name", "label": "Name"}, {"type": "email", "key": "email", "label": "Email"}]},
                   {"do": "add_list", "key": "companies", "label": "Companies", "singular": "Company", "fields": [
                       {"type": "text", "key": "name", "label": "Name"}, {"type": "email", "key": "email", "label": "Email"},
                       {"type": "text", "key": "account", "label": "Account", "sensitive": True},
                       {"type": "link", "key": "people", "label": "People", "to": "contacts", "many": True}]},
                   {"do": "add_list", "key": "cases", "label": "Cases", "singular": "Case", "fields": [
                       {"type": "text", "key": "name", "label": "Name"}, {"type": "link", "key": "company", "label": "Company", "to": "companies"}]},
                   {"do": "add_list", "key": "papers", "label": "Papers", "singular": "Paper", "fields": [
                       {"type": "text", "key": "name", "label": "Name"}, {"type": "link", "key": "case", "label": "Case", "to": "cases"}]},
                   {"do": "design_op", "op": {"op": "set_portal", "portal": {"table": "contacts", "email_field": "email"}}},
                   {"do": "design_op", "op": {"op": "update_table", "table": "companies", "portal": {"via": "people", "edit": ["account"]},
                                              "approve_changes": {"fields": ["account"], "approver": "portal", "wait_days": 7,
                                                                  "tell": "people.email"}}},
                   {"do": "design_op", "op": {"op": "update_table", "table": "papers", "portal": {"via": "case.company", "can_add": True}}}])}


def test_the_portal_reaches_a_records_records_people(app):
    """A case's paper reaches the people of the case's company: one link more than the portal followed."""
    bp = _three_hops()
    assert schema.portal_shows({t["key"]: t for t in bp["tables"]}, schema.table(bp, "papers"), "contacts")
    ws = _ws(app, bp)
    e = Engine(ws)
    ann, bob = e.create("contacts", {"name": "Ann", "email": "ann@firm.example"}), e.create("contacts", {"name": "Bob", "email": "bob@other.example"})
    firm = e.create("companies", {"name": "Firm", "email": "a@firm.example", "people": [ann.id]})
    paper = e.create("papers", {"name": "Licence", "case": e.create("cases", {"name": "Check", "company": firm.id}).id})
    assert e.portal_reaches(paper, ann.id) and not e.portal_reaches(paper, bob.id)


def test_a_change_waits_for_another_of_the_records_outside_people(app):
    """New bank details from one of a company's people wait for another of its people (never the one who made it),
    after the cooling-off; the contact told of it is reached through a link."""
    bp = _three_hops()
    ws = _ws(app, bp)
    e = Engine(ws)
    ann = e.create("contacts", {"name": "Ann", "email": "ann@firm.example"})
    cal = e.create("contacts", {"name": "Cal", "email": "cal@firm.example"})
    firm = e.create("companies", {"name": "Firm", "email": "a@firm.example", "people": [ann.id, cal.id]})
    ann_user, cal_user = _outside(ws, "ann@firm.example", "Ann", ann.id), _outside(ws, "cal@firm.example", "Cal", cal.id)
    by_ann = Engine(ws, by="Ann", member=Membership.query.filter_by(workspace_id=ws.id, user_id=ann_user.id).first())
    by_ann.portal_contact = ann.id
    automations.propose_change(by_ann, firm, {"account": "123456789"}, "Ann")
    asked = Approval.query.filter_by(workspace_id=ws.id, kind="change", status="pending").all()
    assert [a.assignee_id for a in asked] == [cal_user.id]  # Cal, not Ann who made it
    assert asked[0].proposal["token"]  # the contact was told, with a link to cancel it


def test_who_added_a_record_is_read_by_formulas_and_rules(app):
    bp = _ok([{"do": "add_list", "key": "links", "label": "Links", "singular": "Link", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "person", "key": "added_by", "label": "Added by"}]},
              {"do": "add_rule", "rule": {"key": "who", "name": "Note who added it", "when": {"on": "added", "list": "links"},
                                          "steps": [{"do": "set", "values": [{"field": "added_by", "formula": "record.created_by"}]}]}}])
    ws = _ws(app, bp)
    owner = User.query.filter_by(email="owner@example.com").one()
    e = Engine(ws, by=owner.name, member=Membership.query.filter_by(workspace_id=ws.id, user_id=owner.id).first())
    r = e.create("links", {"name": "A"})
    assert Engine(ws).get(r.id).data["added_by"] in (owner.id, str(owner.id))


def test_a_rules_values_can_look_at_a_whole_list_and_created_stays_the_record_it_made():
    """first(people where …) in a rule's value was refused (formulas can name whole lists; a rule's values couldn't), and
    after its lines were added, "created" was the last line, not the order the step before made."""
    bp = _ok([{"do": "add_list", "key": "staff", "label": "Staff", "singular": "Staff member", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "person", "key": "login", "label": "Login"},
                  {"type": "text", "key": "site", "label": "Site"}]},
              {"do": "add_list", "key": "asks", "label": "Asks", "singular": "Ask", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "text", "key": "site", "label": "Site"}]},
              {"do": "add_list", "key": "ask_lines", "label": "Ask lines", "singular": "Ask line", "inside": "asks", "fields": [
                  {"type": "link", "key": "ask", "label": "Ask", "to": "asks"}, {"type": "text", "key": "what", "label": "What"}]},
              {"do": "add_list", "key": "orders", "label": "Orders", "singular": "Order", "fields": [
                  {"type": "link", "key": "ask", "label": "Ask", "to": "asks"}], "stages": {"stages": ["Draft", "Sent"]}},
              {"do": "add_list", "key": "order_lines", "label": "Order lines", "singular": "Order line", "inside": "orders", "fields": [
                  {"type": "link", "key": "order", "label": "Order", "to": "orders"}, {"type": "text", "key": "what", "label": "What"}]},
              {"do": "add_rule", "rule": {"key": "order_it", "name": "Order it", "when": {"on": "added", "list": "asks"}, "steps": [
                  {"do": "set", "values": [{"field": "site", "formula": "first(staff where login == record.created_by).site"}]},
                  {"do": "add", "list": "orders", "copy_same": False, "values": [{"field": "ask", "formula": "record"}]},
                  {"do": "add_each", "list": "order_lines", "lines": "ask_lines", "link": "ask", "values": [
                      {"field": "order", "formula": "created"}, {"field": "what", "formula": "item.what"}]},
                  {"do": "set", "on": "created", "values": [{"field": "status", "to": "Sent"}]}]}}])
    steps = next(a for a in bp["automations"] if a["key"] == "order_it")["actions"]
    assert steps[-1]["target"] == "created" and steps[-1]["values"] == {"status": "Sent"}


def test_a_lists_form_can_be_set_out_in_sections():
    bp = _ok([{"do": "add_list", "key": "asks", "label": "Asks", "singular": "Ask", "fields": [
                  {"type": "text", "key": "what", "label": "What"}, {"type": "date", "key": "by", "label": "By"}]},
              {"do": "design_op", "op": {"op": "update_table", "table": "asks", "wizard": True, "sections": [
                  {"label": "1 Describe", "fields": ["what"]}, {"label": "2 Details", "fields": ["by"]}]}}])
    t = schema.table(bp, "asks")
    assert t["wizard"] is True and [s["label"] for s in t["sections"]] == ["1 Describe", "2 Details"]


def test_a_list_can_keep_several_rules_for_who_sees_which_records(app):
    """Each organisation sees only its own records, and of those each marker only their own marks: two rules, each of which
    must hold. One rule per list was all there was, and setting the second took the first away."""
    bp = _ok([{"do": "add_list", "key": "orgs", "label": "Orgs", "singular": "Org", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "person", "key": "members", "label": "Members", "many": True}]},
              {"do": "add_list", "key": "marks", "label": "Marks", "singular": "Mark", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "link", "key": "org", "label": "Org", "to": "orgs"},
                  {"type": "person", "key": "marker", "label": "Marker"}]},
              {"do": "design_op", "op": {"op": "update_table", "table": "marks", "row_access": [
                  {"field": "org.members", "roles": ["editor"]}, {"field": "marker", "roles": ["editor"]}]}}])
    t = schema.table(bp, "marks")
    assert t["row_access"]["field"] == "org.members" and t["row_access_also"][0]["field"] == "marker"
    ws = _ws(app, bp)
    people = {}
    for name in ("ann", "bob", "cy"):
        user = User(email=f"{name}@example.com", name=name.title())
        user.set_password("x" * 12)
        db.session.add(user)
        db.session.flush()
        db.session.add(Membership(workspace_id=ws.id, user_id=user.id, role="editor"))
        people[name] = user
    db.session.commit()
    e = Engine(ws)
    ours, theirs = e.create("orgs", {"name": "Ours", "members": [people["ann"].id, people["bob"].id]}), e.create("orgs", {"name": "Theirs", "members": [people["cy"].id]})
    anns = e.create("marks", {"name": "Ann's", "org": ours.id, "marker": people["ann"].id})
    bobs = e.create("marks", {"name": "Bob's", "org": ours.id, "marker": people["bob"].id})
    cys_in_ours = e.create("marks", {"name": "Cy's, in ours", "org": ours.id, "marker": people["cy"].id})

    def sees(name):
        member = Membership.query.filter_by(workspace_id=ws.id, user_id=people[name].id).first()
        return {r.data["name"] for r in Engine(ws, by=name, member=member).visible_rows("marks")}
    assert sees("ann") == {"Ann's"} and sees("bob") == {"Bob's"}
    assert sees("cy") == set()  # their own mark, but in another organisation's records
    del anns, bobs, cys_in_ours, theirs


def test_a_record_naming_no_organisation_can_be_everyones(app):
    """The platform's standard clauses (no organisation) are every buyer's, beside each buyer's own (seen by it alone)."""
    bp = _ok([{"do": "add_list", "key": "orgs", "label": "Orgs", "singular": "Org", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "person", "key": "members", "label": "Members", "many": True}]},
              {"do": "add_list", "key": "clauses", "label": "Clauses", "singular": "Clause", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "link", "key": "org", "label": "Org", "to": "orgs"}]},
              {"do": "design_op", "op": {"op": "update_table", "table": "clauses", "row_access": {
                  "field": "org.members", "roles": ["editor"], "shared_when_empty": True}}}])
    ws = _ws(app, bp)
    users = {}
    for name in ("ann", "cy"):
        users[name] = User(email=f"{name}@example.com", name=name.title())
        users[name].set_password("x" * 12)
        db.session.add(users[name])
        db.session.flush()
        db.session.add(Membership(workspace_id=ws.id, user_id=users[name].id, role="editor"))
    db.session.commit()
    e = Engine(ws)
    ours = e.create("orgs", {"name": "Ours", "members": [users["ann"].id]})
    e.create("orgs", {"name": "Theirs", "members": [users["cy"].id]})
    e.create("clauses", {"name": "Standard liability"})
    e.create("clauses", {"name": "Our liability", "org": ours.id})

    def sees(name):
        member = Membership.query.filter_by(workspace_id=ws.id, user_id=users[name].id).first()
        return {r.data["name"] for r in Engine(ws, by=name, member=member).visible_rows("clauses")}
    assert sees("ann") == {"Standard liability", "Our liability"} and sees("cy") == {"Standard liability"}


def test_a_person_field_named_like_a_role_word_stays_that_field():
    """A contract's "manager" (its contract manager) was read as the word for an admin: "not by the manager" kept every
    admin out instead of the contract's own manager."""
    bp = _ok([{"do": "add_list", "key": "deals", "label": "Deals", "singular": "Deal", "fields": [
        {"type": "text", "key": "name", "label": "Name"}, {"type": "person", "key": "manager", "label": "Manager"}],
        "stages": {"stages": ["Draft", "Approved"], "moves": [{"from": ["Draft"], "to": "Approved", "button": "Approve", "not_by": ["manager"]}]}}])
    assert schema.table(bp, "deals")["workflow"]["transitions"][0]["not_by"] == ["manager"]


def test_a_new_lists_formula_can_read_a_field_added_to_another_list_in_the_same_batch():
    bp = _ok([{"do": "add_list", "key": "lines", "label": "Lines", "singular": "Line", "fields": [
        {"type": "text", "key": "name", "label": "Name"}, {"type": "money", "key": "price", "label": "Price"}]}])
    bp = _ok([{"do": "add_field", "list": "lines", "field": {"type": "percent", "key": "share", "label": "Share"}},
              {"do": "add_list", "key": "runs", "label": "Runs", "singular": "Run", "fields": [
                  {"type": "link", "key": "line", "label": "Line", "to": "lines"},
                  {"type": "formula", "key": "moved", "label": "Moved", "formula": "line.price * line.share / 100"}]}], bp)
    assert schema.field(schema.table(bp, "runs"), "moved")


def test_a_lock_written_as_a_formula_is_kept_and_said():
    """Locking a bid once its tender closes (a formula, not a field's test) crashed while it was said in words."""
    bp = _ok([{"do": "add_list", "key": "rounds", "label": "Rounds", "singular": "Round", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "datetime", "key": "closes_at", "label": "Closes"}]},
              {"do": "add_list", "key": "bids", "label": "Bids", "singular": "Bid", "fields": [
                  {"type": "link", "key": "round", "label": "Round", "to": "rounds"}, {"type": "money", "key": "price", "label": "Price"}]},
              {"do": "design_op", "op": {"op": "update_table", "table": "bids", "lock_if": ["round.closes_at <= now()"],
                                         "sealed": {"until": "round.closes_at"}}}])
    t = schema.table(bp, "bids")
    assert t["lock_if"] == [{"formula": "round.closes_at <= now()"}] and t["sealed"]["until"] == "round.closes_at" and t["audit_views"]


def test_an_ai_steps_reads_are_about_the_rules_record(app):
    """“contracts where buyer == record.buyer”: record is the rule's record (the question being answered), as it is in
    a value. It was read as each contract itself, so the condition always held and the AI read every buyer's contracts."""
    from app import intelligence
    bp = _ok([{"do": "add_list", "key": "orgs", "label": "Orgs", "singular": "Org", "fields": [{"type": "text", "key": "name", "label": "Name"}]},
              {"do": "add_list", "key": "deals", "label": "Deals", "singular": "Deal", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "link", "key": "org", "label": "Org", "to": "orgs"}]},
              {"do": "add_list", "key": "questions", "label": "Questions", "singular": "Question", "fields": [
                  {"type": "text", "key": "question", "label": "Question"}, {"type": "long_text", "key": "answer", "label": "Answer"},
                  {"type": "link", "key": "org", "label": "Org", "to": "orgs"}]},
              {"do": "add_rule", "rule": {"key": "answer", "name": "Answer", "when": {"on": "added", "list": "questions"}, "steps": [
                  {"do": "ai_fill", "field": "answer", "instructions": "Answer from the deals.",
                   "reads": [{"list": "deals", "where": ["org == record.org"]}]}]}}])
    ws = _ws(app, bp)
    e = Engine(ws)
    ours, theirs = e.create("orgs", {"name": "Ours"}), e.create("orgs", {"name": "Theirs"})
    e.create("deals", {"name": "Our deal", "org": ours.id})
    e.create("deals", {"name": "Their deal", "org": theirs.id})
    asked = e.create("questions", {"question": "Which deals?", "org": ours.id}, automations=False)
    step = next(a for a in bp["automations"] if a["key"] == "answer")["actions"][0]
    read = intelligence.references(e, asked, step)
    assert [r["Name"] for r in read["Deals"]] == ["Our deal"]


def test_a_formula_named_like_the_field_it_sums_doesnt_use_itself():
    """A remittance's amount is the sum of its run's items' amounts: the amount inside sum(…) is the items', not its own."""
    _ok([{"do": "add_list", "key": "items", "label": "Items", "singular": "Item", "fields": [
             {"type": "text", "key": "name", "label": "Name"}, {"type": "money", "key": "amount", "label": "Amount"}]},
         {"do": "add_list", "key": "notes", "label": "Notes", "singular": "Note", "fields": [
             {"type": "text", "key": "name", "label": "Name"},
             {"type": "formula", "key": "amount", "label": "Amount", "formula": "sum(items where name == this.name, amount)"}]}])
    out = apply({}, [{"do": "add_list", "key": "x", "label": "X", "singular": "X", "fields": [
        {"type": "formula", "key": "n", "label": "N", "formula": "n + 1"}]}])
    assert out.problems  # one that really reads itself is still refused


def test_a_portal_list_can_be_for_everyone_in_the_portal(app):
    """Figures published to every seller (market demand with five or more buyers behind it): no link to them needed."""
    bp = _ok([{"do": "add_list", "key": "contacts", "label": "Contacts", "singular": "Contact", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "email", "key": "email", "label": "Email"}]},
              {"do": "add_list", "key": "figures", "label": "Figures", "singular": "Figure", "fields": [
                  {"type": "text", "key": "name", "label": "Name"}, {"type": "number", "key": "buyers", "label": "Buyers"}]},
              {"do": "design_op", "op": {"op": "set_portal", "portal": {"table": "contacts", "email_field": "email"}}},
              {"do": "design_op", "op": {"op": "update_table", "table": "figures", "portal": {
                  "everyone": True, "show_if": [{"field": "buyers", "op": "at_least", "value": 5}]}}}])
    ws = _ws(app, bp)
    e = Engine(ws)
    ann = e.create("contacts", {"name": "Ann", "email": "ann@x.example"})
    wide, thin = e.create("figures", {"name": "Fuel", "buyers": 7}), e.create("figures", {"name": "Rare", "buyers": 2})
    assert e.portal_reaches(wide, ann.id) and not e.portal_reaches(thin, ann.id)


def test_a_team_named_like_a_who_word_stays_the_team():
    """A team called Requester was read as the word for whoever raised a record: its menu was refused."""
    bp = _ok([{"do": "set_team", "key": "requester", "name": "Requester"},
              {"do": "add_list", "key": "asks", "label": "Asks", "singular": "Ask", "fields": [{"type": "text", "key": "name", "label": "Name"}]},
              {"do": "design_op", "op": {"op": "set_space", "space": {"key": "asking", "label": "Asking", "for": ["requester"],
                                                                     "groups": [{"label": "Asks", "items": [{"table": "asks"}]}]}}}])
    assert bp["spaces"][0]["for"] == ["requester"]


def test_a_webhook_rule_about_a_list_runs_on_the_record_the_call_names(app):
    """“A payment status from the ERP” for invoice INV-0001: the rule changes that invoice. A webhook rule always ran with no
    record, so it could only make new ones."""
    bp = _ok([{"do": "add_list", "key": "invoices", "label": "Invoices", "singular": "Invoice", "fields": [
                  {"type": "auto_number", "key": "number", "label": "Number", "prefix": "INV-"}, {"type": "text", "key": "paid", "label": "Paid"}]},
              {"do": "add_rule", "rule": {"key": "paid_in", "name": "Payment status in", "when": {"on": "webhook", "list": "invoices"},
                                          "steps": [{"do": "set", "values": [{"field": "paid", "formula": "payload.status"}]}]}}])
    ws = _ws(app, bp)
    e = Engine(ws)
    inv = e.create("invoices", {})
    rule = next(a for a in bp["automations"] if a["key"] == "paid_in")
    found = automations.hook_record(e, rule, {"number": e.get(inv.id).data["number"], "status": "Paid"})
    assert found.id == inv.id
    automations.start_from(e, rule, "payload", {"id": inv.id, "status": "Paid"}, "test", record=found)
    assert Engine(ws).get(inv.id).data["paid"] == "Paid"
    import pytest
    with pytest.raises(LookupError):
        automations.hook_record(e, rule, {"status": "Paid"})
