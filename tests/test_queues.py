"""Work queues: tasks for a team taken one at a time, the most pressing first, put off till a day, or done through a
flow."""
from datetime import timedelta

from app.blueprint import schema
from app.engine.records import Engine
from app.extensions import db
from app.models import Approval, FlowRun

from .conftest import login
from .test_wave2 import _as, _built, _person

DESIGN = [
    {"op": "set_team", "team": {"key": "warehouse", "name": "Warehouse"}},
    {"op": "create_table", "table": "counts", "label": "Stock checks", "singular": "Stock check", "fields": [
        {"key": "area", "type": "text", "required": True}, {"key": "found", "type": "number"},
        {"key": "urgency", "type": "select", "options": ["Routine", "Urgent"], "default": "Routine"}]},
    {"op": "add_flow", "flow": {"key": "do_count", "label": "Count it", "table": "counts",
                                "steps": [{"kind": "form", "fields": ["found"]}]}},
    {"op": "add_automation", "automation": {
        "key": "ask", "name": "Someone checks the stock", "trigger": {"type": "record_created", "table": "counts"},
        "actions": [{"type": "task", "to": "warehouse", "message": "Count {{record.area}}", "priority": "urgency", "flow": "do_count"}]}},
]


def test_a_teams_queue_most_pressing_first_taken_put_off_and_done_through_its_flow(app, client, owner):
    user, ws = owner
    _built(ws, DESIGN)
    wes = _person(ws, "wes@example.com", "Wes", "editor", teams=["warehouse"])
    pia = _person(ws, "pia@example.com", "Pia", "editor", teams=["warehouse"])
    e = Engine(ws)
    a = e.create("counts", {"area": "Aisle 1"})
    b = e.create("counts", {"area": "Cold room", "urgency": "Urgent"})
    c = e.create("counts", {"area": "Aisle 2"})
    tasks = {t.record_id: t for t in Approval.query.filter_by(kind="task")}
    assert (tasks[a.id].priority, tasks[b.id].priority, tasks[c.id].flow) == (1, 3, "do_count")

    client.post("/login", data={"email": "wes@example.com", "password": "password1234"})
    base = f"/w/{ws.id}/app"
    page = client.get(f"{base}/approvals").get_data(as_text=True)
    assert page.index("Count Cold room") < page.index("Count Aisle 1") and "Urgent" in page and "Next task" in page
    go = client.post(f"{base}/approvals/next")  # the urgent one, taken, and its flow started
    assert "/flows/run/" in go.headers["Location"]
    db.session.refresh(tasks[b.id])
    assert tasks[b.id].taken_by_id == wes.id
    from flask import g
    from flask_login import login_user
    from app.web import erp
    with app.test_request_context():
        login_user(pia)
        g.engine = _as(ws, pia)
        assert tasks[b.id] not in erp.my_approvals(ws, g.engine.member)  # Wes took it: not in Pia's queue
    client.post(go.headers["Location"], data={"go": "next", "found": "42"})
    db.session.refresh(tasks[b.id])
    assert tasks[b.id].status == "done" and Engine(ws).get(b.id).data["found"] == 42  # doing the flow did the task

    tomorrow = (Engine(ws).today + timedelta(days=1)).isoformat()
    client.post(f"{base}/approvals/{tasks[a.id].id}", data={"decision": "defer", "until": tomorrow})
    page = client.get(f"{base}/approvals").get_data(as_text=True)
    assert "Put off" in page and page.index("Put off") < page.index("Count Aisle 1")  # out of the queue till then
    client.post(f"{base}/approvals/{tasks[c.id].id}", data={"decision": "take"})
    db.session.refresh(tasks[c.id])
    assert tasks[c.id].taken_by_id == wes.id
    client.post(f"{base}/approvals/{tasks[c.id].id}", data={"decision": "give_back"})
    db.session.refresh(tasks[c.id])
    assert tasks[c.id].taken_by_id is None


def test_a_tasks_flow_and_priority_are_checked():
    bp = {"name": "x", "tables": [{"key": "counts", "label": "Counts", "singular": "Count", "fields": [{"key": "area", "label": "Area", "type": "text"}]}],
          "pages": [], "automations": [schema.normalize_automation({
              "key": "ask", "name": "Ask", "trigger": {"type": "record_created", "table": "counts"},
              "actions": [{"type": "task", "to": "editor", "message": "Do it", "priority": "whenever", "flow": "nope"}]})]}
    problems = schema.check(bp)
    assert any("priority “whenever”" in p for p in problems) and any("“nope”, which isn't a flow" in p for p in problems)
