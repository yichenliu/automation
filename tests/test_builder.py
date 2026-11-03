"""The builder chat: a turn's changes are checked, fixed by the AI when they don't fit, applied, and can be undone."""
from app import ai
from app.blueprint import schema
from app.builder import chat
from app.engine.records import Engine
from app.extensions import db
from app.models import Message, Version

from .conftest import ScriptedAI
from .designs import CONSULTANCY


def _turn(ws, text, page=None):
    reply = chat.ask(ws, text, page)
    chat.work(reply.id)
    return db.session.get(Message, reply.id)


def test_a_turn_builds_the_erp_and_can_be_undone(app, owner):
    _, ws = owner
    ai.set_backend(ScriptedAI({"ops": CONSULTANCY, "reply": "I've set up clients, projects and invoices."}))
    m = _turn(ws, "I run a consultancy: clients, projects, invoices.")
    assert m.status == "done" and m.text.startswith("I've set up") and m.version_after == 1 and ws.version == 1
    assert any("Created the table Invoices" in c for c in m.changes)
    assert m.navigate == f"/w/{ws.id}/app/t/clients" and m.thinking and m.progress
    assert [t["key"] for t in ws.blueprint["tables"]] == ["clients", "projects", "invoices"]
    chat.restore(ws, m.version_before)
    assert ws.version == 2 and ws.blueprint["tables"] == [] and Version.query.count() == 3


def test_changes_that_dont_fit_go_back_to_the_ai(app, owner):
    _, ws = owner
    bad = {"ops": [{"op": "create_table", "table": "jobs", "fields": [{"key": "customer", "type": "link", "table": "customers"}]}], "reply": "Done"}
    good = {"ops": [{"op": "create_table", "table": "customers", "fields": [{"key": "name", "type": "text"}]},
                    {"op": "create_table", "table": "jobs", "fields": [{"key": "customer", "type": "link", "table": "customers"}]}], "reply": "Added jobs and customers."}
    scripted = ScriptedAI(bad, good)
    ai.set_backend(scripted)
    m = _turn(ws, "Track jobs for customers")
    assert m.status == "done" and [t["key"] for t in ws.blueprint["tables"]] == ["customers", "jobs"]
    assert "customers" in scripted.asked[1][-1]["content"] and "isn't a table" in scripted.asked[1][-1]["content"]

    ai.set_backend(ScriptedAI(*[bad | {"ops": [{"op": "add_field", "table": "nope", "field": {"key": "x", "type": "text"}}]}] * 3))
    m = _turn(ws, "add x to nope")
    assert m.status == "failed" and "nothing was changed" in m.error and ws.version == 1 and not m.text
    assert any("there's no table" in line for line in m.progress)


def test_sample_data_questions_and_waiting_for_the_ai(app, owner):
    _, ws = owner
    ai.set_backend(ScriptedAI({"ops": CONSULTANCY, "reply": "Built."}))
    _turn(ws, "build it")
    ai.set_backend(ScriptedAI({"ops": [
        {"op": "add_records", "table": "clients", "records": [{"name": "Acme", "status": "Active"}, {"name": "Bright", "status": "Lead"}]},
        {"op": "add_records", "table": "projects", "records": [{"name": "Survey", "client": "Acme", "hours": 3, "rate": 100}]}], "reply": "Added samples."}))
    m = _turn(ws, "add some sample data")
    assert m.changes == ["Added 2 clients", "Added 1 project"] and m.version_after is None
    e = Engine(ws)
    assert e.rows("projects")[0].data["client"] == e.rows("clients")[0].id

    ai.set_backend(ScriptedAI({"ops": [{"op": "find", "table": "clients", "filters": [{"field": "status", "op": "equals", "value": "Active"}]}],
                               "reply": "One client is active."}))
    m = _turn(ws, "which clients are active?", page=f"/w/{ws.id}/app/t/clients")
    assert m.results["total"] == 1 and m.results["rows"][0]["cells"][0] == "Acme"
    assert m.text == "Found 1 client where Status is “Active”."

    ai.set_backend(ScriptedAI(ai.AIUnavailable("the AI at http://127.0.0.1:11434 isn't answering (ConnectTimeout)")))
    m = _turn(ws, "add a phone field to clients")
    assert m.status == "waiting_ai" and m.next_try is not None
    ai.set_backend(ScriptedAI({"ops": [{"op": "add_field", "table": "clients", "field": {"key": "phone", "label": "Phone", "type": "phone"}}], "reply": "Added."}))
    chat.work(m.id)
    m = db.session.get(Message, m.id)
    assert m.status == "done" and m.changes == ["Added Phone to Clients"]


def test_a_big_request_is_built_in_parts_in_one_turn_and_undone_as_one(app, owner):
    _, ws = owner
    first = {"ops": CONSULTANCY[:3], "reply": "Clients and projects are set up.", "more": True, "plan": "invoices, then payroll"}
    second = {"ops": [{"op": "create_table", "table": "payroll", "label": "Payroll", "access": {"view": "admin"},
                       "fields": [{"key": "person", "label": "Person", "type": "text"}, {"key": "pay", "label": "Pay", "type": "money"}]}],
              "reply": "Payroll is set up, and only admins can see it.", "more": False}
    closing = {"reply": "Clients, projects, invoices and a private payroll are set up."}
    scripted = ScriptedAI(first, second, closing)
    ai.set_backend(scripted)
    m = _turn(ws, "Set up my whole business: clients, projects, invoices and payroll that only I can see.")
    assert m.status == "done" and m.text == closing["reply"]  # one reply for the whole turn, from what was really built
    assert len(scripted.asked) == 3 and "Carry on with the next part of your plan: invoices, then payroll" in scripted.asked[1][-1]["content"]
    facts = scripted.asked[2][-1]["content"]
    assert "Created the table Payroll" in facts and "Created the table Clients" in facts and "Not built" not in facts
    assert "Table payroll" not in scripted.asked[0][1]["content"] and "Table clients" in scripted.asked[1][1]["content"]
    payroll = next(t for t in ws.blueprint["tables"] if t["key"] == "payroll")
    assert payroll["access"] == {"view": "admin"}
    assert any("Part 1 is ready" in p for p in m.progress)
    chat.restore(ws, m.version_before)  # one undo takes back every part
    assert ws.blueprint["tables"] == []


def test_an_answer_that_still_has_a_problem_after_repairs_keeps_everything_that_fits(app, owner):
    _, ws = owner
    bad_lock = {"op": "create_table", "table": "payroll", "label": "Payroll", "access": {"view": "admin"},
                "lock_if": [{"field": "nope", "op": "equals", "value": "x"}], "fields": [{"key": "pay", "label": "Pay", "type": "money"}]}
    broken = {"op": "add_field", "table": "nowhere", "field": {"key": "x", "label": "X", "type": "text"}}
    answer = {"ops": CONSULTANCY + [bad_lock, broken], "reply": "Built everything.", "more": False}
    ai.set_backend(ScriptedAI(answer, answer, answer))
    m = _turn(ws, "Clients, projects, invoices and payroll.")
    assert m.status == "done" and "Not everything fitted" in m.text
    keys = [t["key"] for t in ws.blueprint["tables"]]
    assert keys == ["clients", "projects", "invoices", "payroll"]  # the payroll table kept, without its broken lock
    assert "lock_if" not in ws.blueprint["tables"][-1]
    assert ws.blueprint["tables"][-1]["access"]["view"] == "admin"  # only the broken lock goes: payroll stays private
    assert "the new fields of Nowhere" in m.text and "change 5" not in m.text  # said in the owner's words
    assert any("when Payroll locks" in line for line in m.progress)  # the details are there for whoever wants them


def test_row_access_can_follow_a_link_to_the_customers_rep(app, owner):
    from app.blueprint import ops
    result = ops.apply({}, [
        {"op": "create_table", "table": "customers", "fields": [{"key": "name", "type": "text"}, {"key": "rep", "type": "user"}]},
        {"op": "create_table", "table": "orders", "fields": [{"key": "customer", "type": "link", "table": "customers"}]},
        {"op": "update_table", "table": "orders", "changes": {"row_access": {"field": "customer.rep", "roles": ["editor"]},
                                                              "fields": [{"key": "total", "label": "Total", "type": "money"}]}}])
    assert not result.problems, result.problems
    orders = result.blueprint["tables"][1]
    assert orders["row_access"]["field"] == "customer.rep" and [f["key"] for f in orders["fields"]] == ["customer", "total"]


def test_an_answer_cut_off_at_the_length_limit_is_asked_for_again_in_parts(app, owner):
    _, ws = owner
    smaller = {"ops": CONSULTANCY, "reply": "Here's the first part.", "more": False}
    scripted = ScriptedAI(ai.CutOff("cut off"), smaller)
    ai.set_backend(scripted)
    m = _turn(ws, "Everything for my business.")
    assert m.status == "done" and [t["key"] for t in ws.blueprint["tables"]] == ["clients", "projects", "invoices"]
    assert "too long and got cut off" in scripted.asked[1][-1]["content"]


def test_left_out_parts_get_a_second_try_and_lines_copy_across_by_themselves(app, owner):
    _, ws = owner
    tables = [
        {"op": "create_table", "table": "jobs", "label": "Jobs", "fields": [{"key": "title", "type": "text"},
                                                                             {"key": "status", "type": "select", "options": ["Booked", "Done"]}]},
        {"op": "create_table", "table": "job_lines", "label": "Job lines", "embedded": True, "fields": [
            {"key": "job", "type": "link", "table": "jobs"}, {"key": "item", "type": "text"}, {"key": "qty", "type": "number"}]},
        {"op": "create_table", "table": "invoices", "label": "Invoices", "fields": [{"key": "job", "type": "link", "table": "jobs"}, {"key": "title", "type": "text"}]},
        {"op": "create_table", "table": "invoice_lines", "label": "Invoice lines", "embedded": True, "fields": [
            {"key": "invoice", "type": "link", "table": "invoices"}, {"key": "item", "type": "text"}, {"key": "qty", "type": "number"}]}]
    page = {"op": "add_page", "page": {"label": "Overview", "widgets": [{"type": "chart", "table": "jobs", "group_by": "created_time", "label": "Jobs by month"}]}}
    broken = {"op": "add_automation", "automation": {"key": "bill", "name": "Bill it", "trigger": {"type": "record_updated", "table": "jobs"},
                                                     "conditions": [{"field": "status", "op": "changed_to", "value": "Done"}],
                                                     "actions": [{"type": "create_record", "table": "invoices", "values": {"job": "{{record}}", "nope": 1}},
                                                                 {"type": "create_for_each", "source": {"table": "job_lines", "via": "job"}, "table": "invoice_lines"}]}}
    fixed = {"op": "add_automation", "automation": {**broken["automation"], "actions": [
        {"type": "create_record", "table": "invoices", "values": {"job": "{{record}}"}},
        {"type": "create_for_each", "source": {"table": "job_lines", "via": "job"}, "table": "invoice_lines"}]}}
    first = {"ops": tables + [page, broken], "reply": "Built jobs and invoices.", "more": False}
    scripted = ScriptedAI(first, first, first, {"ops": [fixed], "reply": "Fixed the billing automation.", "more": False})
    ai.set_backend(scripted)
    m = _turn(ws, "Jobs with lines, invoiced when done.")
    assert m.status == "done" and "Not everything fitted" not in m.text, m.text
    assert "didn't fit and were left out" in scripted.asked[3][-1]["content"] and "nope" in scripted.asked[3][-1]["content"]
    assert ws.blueprint["pages"][0]["widgets"][0]["group_by"] == "created_at"
    engine = Engine(ws, by="Ethan")
    job = engine.create("jobs", {"title": "Fit-out", "status": "Booked"})
    engine.create("job_lines", {"job": job.id, "item": "Tap", "qty": 2})
    engine.update(engine.get(job.id), {"status": "Done"})
    invoice = Engine(ws).rows("invoices")[0]
    lines = Engine(ws).rows("invoice_lines")
    assert invoice.data["title"] == "Fit-out" and invoice.data["job"] == job.id
    assert [(l.data["item"], l.data["qty"], l.data["invoice"]) for l in lines] == [("Tap", 2, invoice.id)]


def test_the_ais_shorthand_is_understood_where_only_one_reading_fits(app, owner):
    from app.blueprint import ops
    result = ops.apply({}, [
        {"op": "create_table", "table": "materials", "fields": [{"key": "name", "type": "text"}, {"key": "unit", "type": "text"}]},
        {"op": "create_table", "table": "jobs", "fields": [
            {"key": "title", "type": "text"}, {"key": "material", "type": "link", "table": "materials"},
            {"key": "unit", "label": "Unit", "type": "lookup", "link": "materials"},  # the table, and no field: the job's material's unit
            {"key": "starts", "type": "date"}, {"key": "due_date", "type": "date"}, {"key": "status", "type": "select", "options": ["Open", "Done"]}],
         "sections": [{"label": "Job", "fields": ["title", "material", "lines"]}],  # "lines" names no field: skipped
         "views": [{"key": "plan", "label": "Plan", "type": "timeline", "start_field": "starts"}],  # its end found by name
         "public_form": {"fields": ["title", "name"]}},
        {"op": "create_table", "table": "invoices", "fields": [{"key": "job", "type": "link", "table": "jobs"}],
         "documents": [{"key": "invoice", "label": "Invoice"}]},
        {"op": "create_table", "table": "invoice_lines", "embedded": True, "fields": [
            {"key": "invoice", "type": "link", "table": "invoices"}, {"key": "item", "type": "text"}]},
        {"op": "create_table", "table": "job_lines", "embedded": True, "fields": [
            {"key": "job", "type": "link", "table": "jobs"}, {"key": "item", "type": "text"}]},
        {"op": "add_automation", "automation": {"key": "bill", "name": "Bill it", "trigger": {"type": "record_updated", "table": "jobs"},
                                                "conditions": [{"field": "status", "op": "equals", "value": "Done"}],
                                                "actions": [{"type": "create_record", "table": "invoices"},
                                                            {"type": "create_for_each", "source": {"table": "job_lines", "via": "job"}}]}},
        {"op": "add_automation", "automation": {"key": "send", "name": "Send it", "trigger": {"type": "record_created", "table": "invoices"},
                                                "actions": [{"type": "generate_document", "document": "invoice_pdf"}]}},
        {"op": "add_page", "page": {"key": "home", "label": "Home", "widgets": [
            {"type": "text", "label": "Welcome", "values": {"text": "Hello"}}, {"type": "list", "label": "Jobs", "tables": ["jobs"]}]}},
    ])
    assert not result.problems, result.problems
    bp = result.blueprint
    jobs = schema.table(bp, "jobs")
    assert schema.field(jobs, "unit")["link"] == "material" and schema.field(jobs, "unit")["field"] == "unit"
    plan = next(v for v in jobs["views"] if v["key"] == "plan")
    assert plan["end_field"] == "due_date" and jobs["sections"][0]["fields"] == ["title", "material"]
    assert jobs["public_form"]["fields"] == ["title"]
    bill, send = bp["automations"]
    assert bill["actions"][1]["table"] == "invoice_lines" and send["actions"][0]["document"] == "invoice"
    assert [w.get("text") or w.get("table") for w in bp["pages"][0]["widgets"]] == ["Hello", "jobs"]


def test_salvage_cuts_the_one_bad_part_and_says_so_plainly(app, owner):
    from app.blueprint import ops
    salvaged = ops.salvage({}, [
        {"op": "create_table", "table": "staff", "label": "Staff", "access": {"view": "admin"},
         "portal": {"via": "nobody", "fields": ["name"]}, "fields": [{"key": "name", "type": "text"}, {"key": "pay", "type": "money"},
                                                                      {"key": "bonus", "label": "Bonus", "type": "formula", "expression": "pay * missing"}]},
        {"op": "add_page", "page": {"key": "home", "label": "Home", "widgets": [
            {"type": "metric", "label": "Head count", "table": "staff"}, {"type": "metric", "label": "Ghosts", "table": "ghosts"}]}},
    ])
    assert not salvaged.problems
    staff = salvaged.blueprint["tables"][0]
    assert staff["access"] == {"view": "admin"} and "portal" not in staff and [f["key"] for f in staff["fields"]] == ["name", "pay"]
    assert [w["label"] for w in salvaged.blueprint["pages"][0]["widgets"]] == ["Head count"]
    assert set(salvaged.missing) == {"what the portal shows of Staff", "the field Bonus of Staff", "the Ghosts block on the page Home"}


def test_when_the_second_try_at_a_cut_part_fails_the_rest_of_the_plan_is_still_built(app, owner):
    _, ws = owner
    ghost = {"op": "add_page", "page": {"key": "home", "label": "Home", "widgets": [{"type": "metric", "label": "Ghosts", "table": "ghosts"}]}}
    first = {"ops": CONSULTANCY + [ghost], "reply": "Clients, projects and a home page.", "more": True, "plan": "payroll"}
    second = {"ops": [{"op": "create_table", "table": "payroll", "label": "Payroll", "fields": [{"key": "pay", "label": "Pay", "type": "money"}]}],
              "reply": "Payroll added.", "more": False}
    closing = {"reply": "Clients, projects and payroll are set up; the home page didn't fit."}
    scripted = ScriptedAI(first, first, first, {"ops": [ghost], "reply": "Fixed.", "more": True, "plan": "payroll"},
                          {"ops": [ghost], "reply": "Fixed.", "more": True, "plan": "payroll"},
                          {"ops": [ghost], "reply": "Fixed.", "more": True, "plan": "payroll"}, second, closing)
    ai.set_backend(scripted)
    m = _turn(ws, "Clients, projects, a home page and payroll.")
    assert m.status == "done" and "payroll" in [t["key"] for t in ws.blueprint["tables"]]
    assert "Carry on with the next part of your plan: payroll" in scripted.asked[6][-1]["content"]
    assert "the page Home" in scripted.asked[7][-1]["content"]  # the closing reply is told what's still missing
    assert m.text == closing["reply"]


def test_a_cut_part_the_second_try_skips_is_still_reported_missing(app, owner):
    _, ws = owner
    shop = {"op": "add_automation", "automation": {"key": "shop", "name": "Shop orders in", "trigger": {"type": "webhook_received", "table": "ghosts"},
                                                   "actions": [{"type": "notify", "message": "New order"}]}}
    first = {"ops": CONSULTANCY + [shop], "reply": "Clients, projects and shop orders coming in.", "more": False}
    retry = {"ops": [{"op": "add_field", "table": "clients", "field": {"key": "notes", "label": "Notes", "type": "long_text"}}],
             "reply": "Fixed it.", "more": False}  # quietly leaves the automation out
    closing = {"reply": "Clients and projects are set up; the shop orders automation isn't built yet."}
    scripted = ScriptedAI(first, first, first, retry, closing)
    ai.set_backend(scripted)
    m = _turn(ws, "Clients, projects, and orders from my web shop.")
    facts = scripted.asked[4][-1]["content"]
    assert "Not built: the automation “Shop orders in”" in facts and "Shop orders in" not in facts.split("Not built")[0]
    assert "Your replies while building" not in facts  # the drafts (which claim it) aren't shown
    assert m.text == closing["reply"]


def test_an_attached_spreadsheet_is_brought_in_once_its_tables_are_built(app, owner):
    from app import jobs
    from app.storage import save
    _, ws = owner
    sheet = save(ws.id, b"Customer,Email\nAcme Pty,ops@acme.com.au\nNew Co,hello@newco.com.au\n", "customers.csv", source="chat")
    tables = {"ops": [{"op": "create_table", "table": "customers", "label": "Customers", "fields": [
        {"key": "name", "type": "text"}, {"key": "email", "type": "email"}]},
        {"op": "add_records", "table": "customers", "records": [  # the sheet typed out: left to the import, not added twice
            {"name": "Acme Pty", "email": "ops@acme.com.au"}, {"name": "New Co", "email": "hello@newco.com.au"}]}],
        "reply": "Customers are set up.", "more": False}
    rows = {"ops": [{"op": "import_file", "file_id": sheet.id, "table": "customers", "mapping": {"Customer": "name", "Email": "email"}}],
            "reply": "Bringing your customers in.", "more": False}
    closing = {"reply": "I've set up Customers and I'm bringing your two customers in."}
    scripted = ScriptedAI(tables, rows, closing)
    ai.set_backend(scripted)
    reply = chat.ask(ws, "Here's my customer list. Make it a proper system and bring the data in.", files=[sheet.id])
    chat.work(reply.id)
    m = db.session.get(Message, reply.id)
    assert m.status == "done" and m.text == closing["reply"]
    assert "customers.csv (file id" in scripted.asked[1][-1]["content"] and "import_file" in scripted.asked[1][-1]["content"]
    assert any("Importing 2 rows into Customers" in c for c in m.changes)
    assert "Left the attached rows to the import, so they aren't added twice" in m.progress
    jobs.run(jobs.Job.query.filter_by(kind="import").one())
    assert sorted(r.data["name"] for r in Engine(ws).rows("customers")) == ["Acme Pty", "New Co"]


def test_a_big_build_is_checked_against_the_request_and_what_was_dropped_is_added(app, owner, monkeypatch):
    monkeypatch.setattr(chat, "REVIEW_AFTER", 3)
    _, ws = owner
    built = {"ops": CONSULTANCY, "reply": "I've set up clients, projects and invoices.", "more": False}
    lock = {"op": "update_table", "table": "invoices", "changes": {"lock_if": [{"field": "status", "op": "equals", "value": "Sent"}]}}
    portal = {"op": "set_portal", "portal": {"table": "clients", "email_field": "email"}}
    checked = {"checks": [{"asked": "Clients, projects and invoices", "built": True, "ops": [{"op": "set_theme", "theme": {"accent": "#ff0000"}}]},
                          {"asked": "once an invoice is sent it can't be changed", "built": False, "ops": [lock]},
                          {"asked": "a client portal for their invoices", "built": False, "ops": [portal]}],  # never asked for
               "reply": "I've also locked invoices once they're sent."}
    closing = {"reply": "I've set up clients, projects and invoices, locked once sent."}
    scripted = ScriptedAI(built, checked, closing)
    ai.set_backend(scripted)
    m = _turn(ws, "Clients, projects and invoices; once an invoice is sent it can't be changed.")
    assert "check it against what the owner asked for" in scripted.asked[1][-1]["content"]
    assert "Checking nothing you asked for was missed" in m.progress
    assert schema.table(ws.blueprint, "invoices")["lock_if"][0]["value"] == "Sent"
    assert not schema.portals(ws.blueprint) and (ws.blueprint.get("theme") or {}).get("accent") != "#ff0000"
    assert m.text == closing["reply"] and any("when Invoices lock" in c for c in m.changes)


def test_a_check_that_finds_nothing_missing_leaves_the_reply_as_it_was(app, owner, monkeypatch):
    monkeypatch.setattr(chat, "REVIEW_AFTER", 3)
    _, ws = owner
    scripted = ScriptedAI({"ops": CONSULTANCY, "reply": "I've set up clients, projects and invoices.", "more": False},
                          {"checks": [{"asked": "Clients, projects and invoices", "built": True}], "reply": "Everything asked for is there."})
    ai.set_backend(scripted)
    m = _turn(ws, "Clients, projects and invoices.")
    assert len(scripted.asked) == 2 and m.status == "done" and m.text == "I've set up clients, projects and invoices."


def test_an_answer_stuck_repeating_itself_is_asked_for_again_smaller(app, owner):
    _, ws = owner
    stuck = ai.Stuck("The AI got stuck writing its answer (it kept repeating itself).")
    scripted = ScriptedAI(stuck, stuck, {"ops": CONSULTANCY, "reply": "I've set up clients, projects and invoices."})  # stuck twice, as it can be
    ai.set_backend(scripted)
    m = _turn(ws, "I run a consultancy: clients, projects, invoices.")
    assert m.status == "done" and [t["key"] for t in ws.blueprint["tables"]] == ["clients", "projects", "invoices"]
    assert "stuck repeating itself" in scripted.asked[2][-1]["content"]  # asked a third time, for just the first part


def test_a_reply_never_claims_a_change_that_didnt_happen(app, owner):
    _, ws = owner
    ai.set_backend(ScriptedAI({"ops": CONSULTANCY, "reply": "I've set up clients, projects and invoices."}))
    _turn(ws, "I run a consultancy: clients, projects, invoices.")
    same = {"ops": [{"op": "update_table", "table": "clients", "changes": {"label": "Clients"}}], "reply": "I fixed the clients list for you."}
    ai.set_backend(ScriptedAI(same))
    m = _turn(ws, "the clients list is wrong, fix it")
    assert m.changes == [] and m.text.startswith("I didn't change anything this time") and "fixed" not in m.text
