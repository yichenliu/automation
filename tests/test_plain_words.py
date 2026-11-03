"""What people read: rules described as plain sentences (details filled in from a record named the way people say
them, with a real example), sums in words, and a sum written as a condition's value worked out, not read as text."""
from app.blueprint import ops
from app.builder import chat
from app.engine import automations
from app.engine.records import Engine

from .conftest import login

DESIGN = [
    {"op": "create_table", "table": "engagements", "label": "Engagements", "fields": [
        {"key": "engagement_title", "label": "Engagement title", "type": "text"}, {"key": "budget", "type": "money"},
        {"key": "actual_spend", "type": "money"}, {"key": "status", "type": "select", "options": ["Active", "Done"]}]},
    {"op": "create_table", "table": "team_members", "label": "Team members", "fields": [{"key": "name", "type": "text"}]},
    {"op": "create_table", "table": "timesheets", "label": "Timesheets", "fields": [
        {"key": "team_member", "label": "Team member", "type": "link", "table": "team_members"}, {"key": "week_of", "label": "Week of", "type": "date"},
        {"key": "status", "type": "select", "options": ["Draft", "Submitted"]}]},
    {"op": "add_automation", "automation": {
        "key": "budget", "name": "Alert on budget overrun", "trigger": {"type": "schedule", "every": "day", "at": "08:00", "table": "engagements"},
        "conditions": [{"field": "actual_spend", "op": "greater_than", "value": "budget * 0.9"}, {"field": "status", "op": "equals", "value": "Active"}],
        "actions": [{"type": "notify", "to": "everyone",
                     "message": "Engagement {{record.engagement_title}} has reached 90% of budget (${{record.actual_spend}} of ${{record.budget}})"}]}},
    {"op": "add_automation", "automation": {
        "key": "submitted", "name": "Notify on timesheet submission", "trigger": {"type": "record_updated", "table": "timesheets"},
        "conditions": [{"field": "status", "op": "changed_to", "value": "Submitted"}],
        "actions": [{"type": "notify", "to": "everyone", "message": "Timesheet for {{record.team_member.name}} week of {{record.week_of}} submitted for approval"}]}},
]


def _built(ws):
    result = ops.apply(ws.blueprint, DESIGN)
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    return Engine(ws, by="Ethan")


def test_a_sum_written_as_a_conditions_value_is_worked_out(app, owner):
    _, ws = owner
    e = _built(ws)
    over = e.create("engagements", {"engagement_title": "Audit", "budget": 1000, "actual_spend": 950, "status": "Active"})
    under = e.create("engagements", {"engagement_title": "Review", "budget": 1000, "actual_spend": 500, "status": "Active"})
    conditions = ws.blueprint["automations"][0]["conditions"]
    assert Engine(ws).matches(over, conditions) and not Engine(ws).matches(under, conditions)


def test_rules_read_as_sentences_with_a_real_example(app, owner, client):
    _, ws = owner
    e = _built(ws)
    ann = e.create("team_members", {"name": "Ann Lee"})
    sheet = e.create("timesheets", {"team_member": ann.id, "week_of": "2026-11-02", "status": "Draft"})
    e.update(sheet, {"status": "Submitted"})
    budget, submitted = (automations.describe(Engine(ws), a) for a in ws.blueprint["automations"])
    assert budget["only_if"][0] == "Actual spend is more than 90% of Budget" and budget["when"] == "Every day at 8 am, for each engagement"
    assert submitted["when"] == "When a timesheet is moved to Submitted" and submitted["only_if"] == []
    assert "([Actual spend] of [Budget])" in budget["flow"][0]["text"]  # the amount brings its own $
    step = submitted["flow"][0]
    assert step["text"] == "Tell everyone: “Timesheet for [Team member’s name] week of [Week of] submitted for approval”"
    assert step["example"] == "Timesheet for Ann Lee week of 2 Nov 2026 submitted for approval"
    login(client)
    page = client.get(f"/w/{ws.id}/app/automations").get_data(as_text=True)
    assert '<span class="chip-field"' in page and "Team member’s name" in page and "Ann Lee" in page and "‹" not in page


def test_percentages_work_the_way_people_write_them(app, owner, client):
    """15% is kept as 0.15, but people (and the AI) write "discount / 100" and "more than 10": both mean what they say."""
    _, ws = owner
    result = ops.apply(ws.blueprint, [{"op": "create_table", "table": "quotes", "label": "Quotes", "fields": [
        {"key": "subtotal", "type": "money"}, {"key": "discount_percent", "label": "Discount %", "type": "percent", "default": 0},
        {"key": "discount_amount", "type": "formula", "format": "money", "expression": "round(subtotal * discount_percent / 100, 2)"},
        {"key": "status", "type": "select", "options": ["Draft", "Awaiting approval", "Sent"]}],
        "workflow": {"field": "status", "transitions": [
            {"from": ["Draft"], "to": "Awaiting approval", "label": "Send for approval", "only_if": [{"field": "discount_percent", "op": "greater_than", "value": 10}]},
            {"from": ["Draft"], "to": "Sent", "label": "Send", "only_if": [{"field": "discount_percent", "op": "at_most", "value": 10}]}]}}])
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    quotes = ws.blueprint["tables"][0]
    assert quotes["fields"][2]["expression"] == "round(subtotal * discount_percent, 2)"  # saved as it works
    e = Engine(ws, by="Ethan")
    big = e.create("quotes", {"subtotal": 1890, "discount_percent": "15"})
    small = e.create("quotes", {"subtotal": 500, "discount_percent": "0"})
    assert Engine(ws).value(big, "discount_amount") == 283.5
    moves = lambda r: [m["to"] for _, m, problem in Engine(ws).moves(Engine(ws).get(r.id)) if not problem]  # noqa: E731
    assert moves(big) == ["Awaiting approval"] and moves(small) == ["Sent"]
    blank = e.create("quotes", {"subtotal": 500, "discount_percent": ""})
    assert moves(blank) == ["Sent"]  # left empty, the discount is its default: 0%
    stuck = dict((m["to"], problem) for _, m, problem in Engine(ws).moves(Engine(ws).get(big.id)))["Sent"]
    assert stuck.startswith("You can send once Discount % is"), stuck
    login(client)
    form = client.get(f"/w/{ws.id}/app/t/quotes/new").get_data(as_text=True)
    assert 'name="discount_percent" value="0"' in form  # its default shows, so it isn't saved empty
    assert 'name="then" value="-1"' in form and ">Send</button>" in form  # which way it goes depends on the discount
    for discount, stage in (("0", "Sent"), ("15", "Awaiting approval")):
        client.post(f"/w/{ws.id}/app/t/quotes/new", data={"subtotal": "100", "discount_percent": discount, "then": "-1"})
        assert Engine(ws).rows("quotes")[-1].data["status"] == stage
    words = automations.describe_condition(Engine(ws), quotes["workflow"]["transitions"][0]["only_if"][0], "quotes")
    assert words == "Discount % is more than 10%"


def test_picking_a_menu_item_on_a_line_fills_its_name_and_price(app, owner, client):
    _, ws = owner
    result = ops.apply(ws.blueprint, [
        {"op": "create_table", "table": "menu_items", "label": "Menu items", "fields": [{"key": "name", "type": "text"}, {"key": "price", "type": "money"}]},
        {"op": "create_table", "table": "quotes", "label": "Quotes", "fields": [{"key": "title", "type": "text"}]},
        {"op": "create_table", "table": "quote_lines", "label": "Quote lines", "embedded": True, "fields": [
            {"key": "quote", "type": "link", "table": "quotes"}, {"key": "menu_item", "type": "link", "table": "menu_items"},
            {"key": "description", "type": "text"}, {"key": "quantity", "type": "number"}, {"key": "unit_price", "type": "money"}]}])
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    e = Engine(ws, by="Ethan")
    platter = e.create("menu_items", {"name": "Grazing platter", "price": 25})
    quote = e.create("quotes", {"title": "Wedding"})
    line = e.create("quote_lines", {"quote": quote.id, "menu_item": platter.id, "quantity": 40})
    assert line.data["description"] == "Grazing platter" and line.data["unit_price"] == 25
    own = e.create("quote_lines", {"quote": quote.id, "menu_item": platter.id, "unit_price": 20})
    assert own.data["unit_price"] == 20  # what a person typed stays
    login(client)
    form = client.get(f"/w/{ws.id}/app/t/quotes/{quote.id}/edit").get_data(as_text=True)
    assert "data-fill=" in form and "Grazing platter" in form


def test_a_reminder_two_days_before_an_event_at_a_time_matches_once(app, owner):
    _, ws = owner
    result = ops.apply(ws.blueprint, [{"op": "create_table", "table": "events", "label": "Events", "fields": [
        {"key": "title", "type": "text"}, {"key": "starts", "type": "datetime"}]}])
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    e = Engine(ws, by="Ethan")
    from datetime import timedelta
    soon = e.create("events", {"title": "Wedding", "starts": f"{(e.today + timedelta(days=2)).isoformat()} 18:00"})
    later = e.create("events", {"title": "Lunch", "starts": f"{(e.today + timedelta(days=3)).isoformat()} 12:00"})
    when = [{"field": "starts", "op": "equals", "value": "{{add_days(today(), 2)}}"}]
    assert Engine(ws).matches(soon, when) and not Engine(ws).matches(later, when)
