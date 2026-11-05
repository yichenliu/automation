"""The builder chat around a turn (the turn itself is the Keel v2 builder's: tests/test_lang.py)."""
from app import ai
from app.blueprint import schema
from app.builder import chat
from app.extensions import db
from app.models import Message

from .conftest import ScriptedAI


def _turn(ws, text, page=None):
    reply = chat.ask(ws, text, page)
    chat.work(reply.id)
    return db.session.get(Message, reply.id)






















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




def test_a_smaller_try_that_fails_too_says_so_rather_than_crashing(app, owner):
    """Cut off, then asked again smaller, and that fails as well: nothing was built, so the turn fails plainly, as a
    first part would (it isn't taken for a later part with something saved)."""
    _, ws = owner
    ai.set_backend(ScriptedAI(ai.CutOff("cut off"), ai.CutOff("cut off again")))
    m = _turn(ws, "Everything for my business.")
    assert m.status == "failed" and m.error and not ws.blueprint.get("tables")
    ai.set_backend(ScriptedAI(ai.CutOff("cut off"), RuntimeError("a fault of ours")))
    m = _turn(ws, "Everything for my business.")
    assert m.status == "failed" and "went wrong on our side" in m.error




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
















