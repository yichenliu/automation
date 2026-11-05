"""The runtime built to scale out: several web and worker processes at once, safely. Work is claimed so it's done once,
a claim that runs out is taken up again, one process is the scheduler, a schedule's time runs once, emails wait in an
outbox and are tried again, the rules a save starts come from an index, and rate limits are shared."""
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, text, update

from app import jobs, leases, outbox, scheduler, security, worker
from app.blueprint import ops
from app.builder import chat
from app.engine import automations
from app.engine.records import Engine
from app.extensions import db
from app.models import Job, Message, Notification, Outbox, RateHit, Run, ScheduleRun, User, Workspace, utcnow

from .designs import CONSULTANCY


@pytest.fixture(autouse=True)
def nothing_held_after():
    yield
    leases._held.clear()
    worker._stop.clear()


def _built(ws, extra=()):
    result = ops.apply(ws.blueprint, CONSULTANCY + list(extra))
    assert not result.problems, result.problems
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    return Engine(ws, by="Ethan")


def _at_once(app, n, fn):
    """`fn()` in n threads started together, each with its own app context: its own session and connection, as
    separate worker processes have. Returns what each returned."""
    barrier, out, errors = threading.Barrier(n), [None] * n, []

    def go(i):
        with app.app_context():
            try:
                barrier.wait(timeout=10)
                out[i] = fn()
            except Exception as exc:  # noqa: BLE001 - said by the test
                errors.append(exc)
            finally:
                db.session.remove()
    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    return out


def _other_workspace(user, name="Other Co"):
    ws = Workspace(owner_id=user.id, name=name, blueprint={}, version=0, state={}, settings={})
    db.session.add(ws)
    db.session.commit()
    return ws


# --- claiming ----------------------------------------------------------------------------------------------------------

def test_two_workers_never_take_the_same_job(app, owner):
    _, ws = owner
    db.session.add_all([Job(kind="noop", payload={}, workspace_id=ws.id) for _ in range(30)])
    db.session.commit()

    def take_all():
        got = []
        while (job := jobs.next_job()) is not None:
            got.append(job.id)
        return got
    taken = [i for got in _at_once(app, 3, take_all) for i in got]
    assert len(taken) == 30 and len(set(taken)) == 30  # every job taken, none twice
    for job in Job.query.all():
        assert job.status == "running" and job.attempts == 1 and job.claimed_by and job.lease_until > utcnow()


def test_two_workers_never_take_the_same_run_and_an_apps_runs_go_one_at_a_time(app, owner):
    user, ws = owner
    other = _other_workspace(user)
    db.session.add_all([Run(workspace_id=w.id, automation="x", name="X", status="queued", steps=[]) for w in (ws, ws, other, other)])
    db.session.commit()

    def take():
        run = leases.claim(Run, Run.status == "queued", values={"status": "running"}, one_per_workspace=lambda o: [])
        return run.id if run is not None else None
    taken = [i for i in _at_once(app, 4, take) if i]
    held = Run.query.filter(Run.id.in_(taken)).all()
    assert len(taken) == 2 and {r.workspace_id for r in held} == {ws.id, other.id}  # one for each app, never two of one
    assert all(r.status == "running" for r in held)
    first = next(r for r in held if r.workspace_id == ws.id)
    db.session.execute(update(Run).where(Run.id == first.id).values(status="done", claimed_by=None, lease_until=None))
    db.session.commit()
    nxt = leases.claim(Run, Run.status == "queued", values={"status": "running"}, one_per_workspace=lambda o: [])
    assert nxt is not None and nxt.workspace_id == ws.id and nxt.id not in taken  # the app's next run, once the first is done


def test_an_apps_builder_turns_are_taken_one_after_another(app, owner):
    user, ws = owner
    other = _other_workspace(user)
    turns = [Message(workspace_id=w.id, channel="builder", role="assistant", status="queued", progress=[]) for w in (ws, ws, other)]
    question = Message(workspace_id=ws.id, channel="assistant", role="assistant", status="queued", progress=[])
    db.session.add_all(turns + [question])
    db.session.commit()

    def take():
        message = worker._next_message("builder")
        return message.id if message is not None else None
    taken = [i for i in _at_once(app, 3, take) if i]
    assert sorted(taken) == [turns[0].id, turns[2].id]  # each app's first turn; its second waits for the first
    assert db.session.get(Message, turns[0].id).status == "queued"  # (the turn's own code marks it thinking as it starts)
    assert worker._next_message("assistant").id == question.id  # the app's questions don't wait for its builder turn
    db.session.execute(update(Message).where(Message.id == turns[0].id).values(status="done", claimed_by=None, lease_until=None))
    db.session.commit()
    assert worker._next_message("builder").id == turns[1].id


# --- claims that run out -----------------------------------------------------------------------------------------------

def test_work_whose_claim_ran_out_is_taken_up_again_and_held_work_is_left_alone(app, owner):
    _, ws = owner
    gone, later = utcnow() - timedelta(minutes=5), utcnow() + timedelta(minutes=5)
    stopped = Job(kind="noop", payload={}, status="running", attempts=1, claimed_by="gone:1:1", lease_until=gone)
    elsewhere = Job(kind="noop", payload={}, status="running", attempts=1, claimed_by="other:2:2", lease_until=later)
    stopped_run = Run(workspace_id=ws.id, automation="x", status="running", steps=[], claimed_by="gone:1:1", lease_until=gone)
    elsewhere_run = Run(workspace_id=ws.id, automation="x", status="running", steps=[], claimed_by="other:2:2", lease_until=later)
    taken_never_started = Message(workspace_id=ws.id, channel="builder", role="assistant", status="queued", progress=[],
                                  claimed_by="gone:1:1", lease_until=gone)
    half_answered = Message(workspace_id=ws.id, channel="assistant", role="assistant", status="thinking", progress=[],
                            started_at=utcnow(), claimed_by="gone:1:1", lease_until=gone)
    sending = Outbox(workspace_id=ws.id, channel="email", to="kim@example.com", status="sending", claimed_by="gone:1:1", lease_until=gone)
    db.session.add_all([stopped, elsewhere, stopped_run, elsewhere_run, taken_never_started, half_answered, sending])
    db.session.commit()
    worker.recover()
    assert stopped.status == "queued" and stopped.claimed_by is None and elsewhere.status == "running"
    assert stopped_run.status == "queued" and elsewhere_run.status == "running" and elsewhere_run.claimed_by == "other:2:2"
    assert half_answered.status == "queued" and sending.status == "queued"
    assert jobs.next_job().id == stopped.id  # free again: the next worker takes it
    assert worker._next_message("builder").id == taken_never_started.id  # a claim that ran out is no claim


def test_a_working_process_keeps_its_claims_and_finds_out_when_one_was_taken(app, owner):
    _, ws = owner
    db.session.add_all([Job(kind="noop", payload={}, workspace_id=ws.id) for _ in range(2)])
    db.session.commit()
    first, second = jobs.next_job(), jobs.next_job()
    first_id, second_id = first.id, second.id
    db.session.execute(update(Job).values(lease_until=utcnow() + timedelta(seconds=5)))  # about to run out
    db.session.execute(update(Job).where(Job.id == second_id).values(claimed_by="other:2:2"))  # taken after we went quiet
    db.session.commit()
    assert leases.renew() == [("job", second_id)]
    assert db.session.get(Job, first_id).lease_until > utcnow() + timedelta(seconds=leases.LEASE_SECONDS - 10)
    assert (Job, second_id) not in leases.holding()


def test_a_run_the_worker_holds_isnt_carried_out_again_by_a_save(app, owner):
    _, ws = owner
    e = _built(ws, [{"op": "add_automation", "automation": {"name": "Hello", "trigger": {"type": "button", "table": "clients", "label": "Hello"},
                                                            "actions": [{"type": "notify", "message": "Hello {{record.name}}"}]}}])
    client = e.create("clients", {"name": "Acme"})
    run = Run(workspace_id=ws.id, automation="hello", name="Hello", record_id=client.id, table="clients", status="running", steps=[],
              claimed_by="worker:9:9", lease_until=utcnow() + timedelta(minutes=2))
    db.session.add(run)
    db.session.commit()
    assert automations.execute(e.system(), run).status == "running" and Notification.query.count() == 0  # left to the worker
    run.lease_until = utcnow() - timedelta(seconds=1)  # the worker stopped
    db.session.commit()
    assert automations.execute(e.system(), run).status == "done" and Notification.query.one().text == "Hello Acme"
    assert run.claimed_by is None and run.lease_until is None  # let go once done


# --- one scheduler -----------------------------------------------------------------------------------------------------

def test_only_one_process_is_the_scheduler_and_another_takes_over_when_it_stops(app, owner, monkeypatch):
    done = []
    monkeypatch.setattr(scheduler, "WORK", {name: (lambda name=name: done.append(name)) for name in scheduler.EVERY})
    from app.builder import checking
    took_over = []
    monkeypatch.setattr(checking, "after_platform_update", lambda: took_over.append(1))
    one, two = scheduler.Scheduler(), scheduler.Scheduler()
    try:
        assert one.tick() is True and two.tick() is False
        assert sorted(done) == sorted(scheduler.EVERY) and took_over == [1]  # all of it, once, by the one holding the lock
        done.clear()
        assert one.tick() is True and two.tick() is False and done == []  # nothing due again yet
        one.stop()  # the scheduler's process stops (its connection closes, and its lock goes with it)
        assert two.tick() is True and sorted(done) == sorted(scheduler.EVERY) and took_over == [1, 1]
        assert one.lock.held() is False  # (and it doesn't get it back while the other holds it)
    finally:
        one.stop()
        two.stop()


def test_a_schedules_time_runs_once_however_many_schedulers_look(app, owner):
    _, ws = owner
    e = _built(ws, [{"op": "add_automation", "automation": {"name": "Chase overdue", "trigger": {"type": "schedule", "every": "day", "at": "08:00", "table": "invoices"},
                                                            "conditions": [{"field": "due_date", "op": "before_today"}],
                                                            "actions": [{"type": "notify", "message": "{{record.number}} is overdue"}]}}])
    project = e.create("projects", {"name": "Survey"})
    e.create("invoices", {"project": project.id, "amount": 5, "due_date": (e.today - timedelta(days=3)).isoformat()})
    assert automations.due(e) == []  # a new schedule waits for its first time
    ws.state = {"schedules": {"chase_overdue": (e.now - timedelta(days=1, hours=1)).isoformat()}}
    db.session.commit()

    def look():  # two schedulers for a moment, as one takes over from another
        return len(automations.due(Engine(db.session.get(Workspace, ws.id), by="schedule")))
    assert sorted(_at_once(app, 2, look)) == [0, 1]
    assert Notification.query.count() == 1 and ScheduleRun.query.count() == 1
    slot = ScheduleRun.query.one().slot
    assert automations._take_slot(e, "chase_overdue", slot) is False  # taken: nobody runs that time again
    assert automations._take_slot(e, "chase_overdue", slot + timedelta(days=1)) is True  # tomorrow's is still free


def test_the_scheduler_reads_only_workspaces_that_are_due_and_a_new_design_makes_one_due(app, owner, monkeypatch):
    user, ws = owner
    e = _built(ws, [{"op": "add_automation", "automation": {"name": "Hourly", "trigger": {"type": "schedule", "every": "hour", "at": "00:15"},
                                                            "actions": [{"type": "notify", "message": "Tick"}]}}])
    assert ws.next_due_at is None  # a new design: looked at next time round
    automations.due(e)
    assert utcnow() < ws.next_due_at <= utcnow() + timedelta(hours=1) and ws.next_due_at.minute == 15
    quiet = _other_workspace(user, "Quiet")
    automations.due(Engine(quiet))
    assert quiet.next_due_at > utcnow() + timedelta(hours=23)  # no schedules: looked at again in a day, or when its design changes
    looked = []
    monkeypatch.setattr(automations, "due", lambda engine: looked.append(engine.ws.id) or [])
    assert scheduler.schedules() == 0 and looked == []  # nobody due
    ws.next_due_at = utcnow() - timedelta(seconds=1)  # its quarter past has come
    db.session.commit()
    assert scheduler.schedules() == 1 and looked == [ws.id]
    chat.save_version(quiet, {**quiet.blueprint, "name": "Quiet"}, ["Renamed"], "manual")
    assert quiet.next_due_at is None  # the design changed: its schedules are looked at again at once
    looked.clear()
    scheduler.schedules()
    assert quiet.id in looked


# --- the outbox -------------------------------------------------------------------------------------------------------

def _welcome(ws):
    return _built(ws, [{"op": "add_automation", "automation": {
        "name": "Welcome", "trigger": {"type": "record_created", "table": "clients"},
        "actions": [{"type": "send_email", "to": "{{record.email}}", "subject": "Welcome {{record.name}}", "body": "Hello."},
                    {"type": "notify", "message": "Welcomed {{record.name}}"}]}}])


def test_an_email_waits_in_the_outbox_and_is_tried_again_until_it_goes(app, owner, monkeypatch):
    from app.connectors import microsoft
    _, ws = owner
    monkeypatch.setattr(outbox, "at_once", lambda: False)  # as in real use: the worker sends it, not the save
    monkeypatch.setattr(microsoft, "status", lambda ws_: {"connected": True})
    tries = []

    def send(ws_, to, subject, body, cc="", attachments=()):
        tries.append((to, subject))
        if len(tries) == 1:
            raise microsoft.MicrosoftProblem("Microsoft 365 didn't answer")
    monkeypatch.setattr(microsoft, "send_mail", send)
    e = _welcome(ws)
    e.create("clients", {"name": "Kim", "email": "kim@example.com"})
    run = Run.query.one()
    assert run.status == "done" and tries == []  # the save didn't wait on the mailbox, and the rule carried on
    assert run.steps[0]["result"] == "Email to kim@example.com on its way: “Welcome Kim”"
    assert Notification.query.filter_by(text="Welcomed Kim").count() == 1
    item = Outbox.query.one()
    assert item.status == "queued" and item.run_id == run.id and item.subject == "Welcome Kim"

    assert outbox.work() is True and len(tries) == 1
    item = db.session.get(Outbox, item.id)
    assert item.status == "queued" and item.attempts == 1 and item.next_try > utcnow() + timedelta(seconds=50)
    said = db.session.get(Run, run.id).steps[0]["result"]
    assert said.startswith("Couldn't email kim@example.com yet (Microsoft 365 didn't answer); trying again at ")
    assert outbox.work() is False and len(tries) == 1  # not due again yet

    item.next_try = utcnow() - timedelta(seconds=1)
    db.session.commit()
    assert outbox.work() is True and tries == [("kim@example.com", "Welcome Kim")] * 2
    item = db.session.get(Outbox, item.id)
    assert item.status == "sent" and item.sent_at and item.claimed_by is None and item.reported
    assert db.session.get(Run, run.id).steps[0]["result"] == "Emailed kim@example.com: “Welcome Kim”"


def test_an_email_that_never_goes_is_given_up_on_and_the_admins_are_told(app, owner, monkeypatch):
    from app.connectors import microsoft
    _, ws = owner
    monkeypatch.setattr(outbox, "at_once", lambda: False)
    monkeypatch.setattr(microsoft, "status", lambda ws_: {"connected": True})

    def refuse(*a, **k):
        raise microsoft.MicrosoftProblem("Microsoft 365 needs signing in to again")
    monkeypatch.setattr(microsoft, "send_mail", refuse)
    _welcome(ws).create("clients", {"name": "Kim", "email": "kim@example.com"})
    for _ in range(outbox.TRIES):
        db.session.execute(update(Outbox).values(next_try=utcnow() - timedelta(seconds=1)))
        db.session.commit()
        outbox.work()
    item = Outbox.query.one()
    assert item.status == "failed" and item.attempts == outbox.TRIES
    said = Run.query.one().steps[0]["result"]
    assert said == f"Couldn't email kim@example.com: Microsoft 365 needs signing in to again (tried {outbox.TRIES} times)"
    told = Notification.query.filter_by(kind="error").one()
    assert "The rule “Welcome” couldn't email kim@example.com" in told.text


def test_in_tests_and_try_outs_an_email_goes_there_and_then(app, owner, monkeypatch):
    from app.connectors import microsoft
    _, ws = owner
    sent = []
    monkeypatch.setattr(microsoft, "status", lambda ws_: {"connected": True})
    monkeypatch.setattr(microsoft, "send_mail", lambda ws_, to, subject, body, **k: sent.append(to))
    _welcome(ws).create("clients", {"name": "Kim", "email": "kim@example.com"})
    assert sent == ["kim@example.com"] and Run.query.one().steps[0]["result"] == "Emailed kim@example.com: “Welcome Kim”"
    assert Outbox.query.one().status == "sent"
    app.config["TRYING_OUT"] = True
    try:
        Engine(ws).create("clients", {"name": "Lee", "email": "lee@example.com"})
    finally:
        app.config.pop("TRYING_OUT")
    assert sent == ["kim@example.com"]  # a copy being tried reaches nothing outside
    said = Run.query.order_by(Run.id.desc()).first().steps[0]["result"]
    assert said == "Would email lee@example.com: “Welcome Lee” (not sent while trying the app out)"


def test_a_text_message_goes_through_a_connected_texting_service(app, owner, monkeypatch):
    from app.connectors import apps
    _, ws = owner
    e = _built(ws)
    client = e.create("clients", {"name": "Kim"})
    run = Run(workspace_id=ws.id, automation="x", name="Tell them", record_id=client.id, table="clients", steps=[])
    db.session.add(run)
    db.session.commit()
    rule, step = {"key": "x", "name": "Tell them"}, {"type": "send_sms", "to": "0400 111 222", "message": "Your table for {{record.name}} is ready"}
    with pytest.raises(outbox.OutboxProblem, match="no texting service is connected"):
        automations._do(e, rule, step, client, run)
    texts = []
    service = SimpleNamespace(key="texts", send_sms=lambda creds, to, body: texts.append((to, body)))
    monkeypatch.setattr(apps, "all_apps", lambda: [service])
    monkeypatch.setattr(apps, "connected", lambda ws_, key: True)
    monkeypatch.setattr(apps, "connection", lambda ws_, key: None)
    assert automations._do(e, rule, step, client, run)["result"] == "Texted 0400111222: “Your table for Kim is ready”"
    assert texts == [("0400111222", "Your table for Kim is ready")]
    with pytest.raises(ValueError, match="isn't a phone number"):
        automations._do(e, rule, {**step, "to": "call me"}, client, run)


# --- the rule index ---------------------------------------------------------------------------------------------------

def _scan(engine, table, created):
    """Which rules a save of `table` started before the index: every rule of the design read in turn."""
    out = []
    for a in engine.bp.get("automations", []):
        trig = a["trigger"]
        if not a.get("enabled", True) or trig.get("table") != table:
            continue
        if trig["type"] == "record_created" and not created or trig["type"] == "record_updated" and created:
            continue
        if trig["type"] not in ("record_created", "record_updated", "record_created_or_updated"):
            continue
        out.append(a["key"])
    return out


RULES = [{"op": "add_automation", "automation": {"key": f"r{n}", "name": f"Rule {n}", "enabled": n % 5 != 3,
                                                 "trigger": {"type": kind, "table": table, **({"label": "Go"} if kind == "button" else {})},
                                                 "actions": [{"type": "notify", "message": "x"}]}}
         for n, (kind, table) in enumerate([(k, t) for t in ("clients", "projects", "invoices")
                                            for k in ("record_created", "record_updated", "record_created_or_updated",
                                                      "record_deleted", "button")] * 2)]


def test_the_rule_index_finds_what_reading_every_rule_found(app, owner):
    _, ws = owner
    e = _built(ws, RULES)
    assert any(not a.get("enabled", True) for a in ws.blueprint["automations"])  # (some rules turned off, to be passed over)
    for engine in (e, Engine(ws)):  # made by the first, found again by the second
        for table in ("clients", "projects", "invoices", "nowhere"):
            for created in (True, False):
                found = [a["key"] for a in automations.rules(engine, table, automations.CREATED if created else automations.UPDATED)
                         if a.get("enabled", True)]
                assert found == _scan(engine, table, created), (table, created)
            deleted = [a["key"] for a in automations.rules(engine, table, ("record_deleted",))]
            assert deleted == [a["key"] for a in engine.bp["automations"] if a["trigger"]["type"] == "record_deleted"
                               and a["trigger"].get("table") == table]
    key = automations._rules_key(Engine(ws), ws.blueprint["automations"])
    assert key[0] == "saved" and key[3] == ws.version and key in automations._rule_index
    more = ops.apply(ws.blueprint, [{"op": "add_automation", "automation": {"key": "late", "name": "Late", "trigger": {"type": "record_created", "table": "clients"},
                                                                            "actions": [{"type": "notify", "message": "y"}]}}])
    chat.save_version(ws, more.blueprint, more.changes, "chat")  # a new version: its own index
    assert _scan(Engine(ws), "clients", True)[-1] == "late"
    assert [a["key"] for a in automations.rules(Engine(ws), "clients", automations.CREATED) if a.get("enabled", True)] == _scan(Engine(ws), "clients", True)
    looking = Engine(SimpleNamespace(id=ws.id, blueprint=ws.blueprint, timezone=None, currency=None, settings={}, name="x", about=None))
    assert [a["key"] for a in automations.rules(looking, "clients", automations.UPDATED) if a.get("enabled", True)] == _scan(looking, "clients", False)


def test_the_rule_index_keeps_only_the_most_recent_designs(app, owner, monkeypatch):
    monkeypatch.setattr(automations, "RULES_KEPT", 5)
    kept = []
    for n in range(12):
        design = {"automations": [{"key": f"a{n}", "trigger": {"type": "record_created", "table": "t"}}]}
        kept.append(design)  # (each still in use)
        engine = SimpleNamespace(ws=SimpleNamespace(), bp=design)
        assert [a["key"] for a in automations.rules(engine, "t", automations.CREATED)] == [f"a{n}"]
    assert len(automations._rule_index) <= 5


# --- shared rate limits -----------------------------------------------------------------------------------------------

def test_rate_limits_are_counted_once_for_every_web_process(app):
    here = security.Throttle(3, 60, name="test sign-ins", shared=True)
    there = security.Throttle(3, 60, name="test sign-ins", shared=True)  # the same limit, in another web process
    assert here.allow("10.0.0.1") and there.allow("10.0.0.1") and here.allow("10.0.0.1")
    assert not there.allow("10.0.0.1") and here.blocked("10.0.0.1")
    assert there.allow("10.0.0.2")  # someone else has their own count
    assert not here.hits and not there.hits  # (none of it in memory)
    keys = [k for (k,) in db.session.query(RateHit.key)]
    assert len(keys) == 2 and not any("10.0.0" in k for k in keys)  # kept as fingerprints, not addresses
    assert not security.Throttle(3, 60, name="another limit", shared=True).blocked("10.0.0.1")
    db.session.execute(update(RateHit).values(bucket=RateHit.bucket - 100_000))
    db.session.commit()
    assert security.forget_old_hits() == 2 and not here.blocked("10.0.0.1")
    assert security.sign_ins._in_database() is False  # under TESTING the sign-in limit counts in memory, as it always did


# --- the worker as its own process, and the simulator -----------------------------------------------------------------

def test_the_worker_runs_the_kinds_of_work_it_is_given(app, monkeypatch):
    assert worker.kinds_of(app) == list(worker.KINDS)
    app.config.update(WORKER_KINDS=["builder", "outbox", "builder"], BUILDER_CONCURRENCY=3)
    assert worker.kinds_of(app) == ["builder", "outbox"]
    for kind in ("builder", "outbox"):
        monkeypatch.setitem(worker.TICKS, kind, lambda: False)
    threads = worker.run_threads(app, worker.kinds_of(app))
    try:  # every builder thread is named "builder": the AI gateway knows what its calls are for by the name
        assert sorted(t.name for t in threads) == ["builder", "builder", "builder", "heartbeat", "outbox"]
        assert all(t.is_alive() for t in threads)
    finally:
        worker.stop(threads, wait=10)
    assert not any(t.is_alive() for t in threads)
    app.config["WORKER_KINDS"] = ["builder", "emails"]
    with pytest.raises(ValueError, match="emails"):
        worker.kinds_of(app)


def test_claims_and_schedule_times_work_in_the_simulator_too(app, tmp_path):
    from app import create_app, sim
    config = {k: v for k, v in app.config.items() if k.isupper()}
    config.update({"TESTING": True, "WORKER": False, "DATA_DIR": tmp_path, "SECRET_KEY": "sim", **sim.overrides()})
    copy = create_app(config)
    with copy.app_context():
        assert db.engine.dialect.name == "sqlite"
        person = User(email="sim@example.com", name="Sim", password_hash="!")
        db.session.add(person)
        db.session.flush()
        ws = Workspace(owner_id=person.id, name="Sim", blueprint={}, version=0, state={}, settings={})
        db.session.add(ws)
        db.session.add_all([Job(kind="noop", payload={}) for _ in range(2)])
        db.session.commit()
        first, second = jobs.next_job(), jobs.next_job()
        assert first.id != second.id and jobs.next_job() is None
        at = datetime(2026, 11, 5, 8)
        assert automations._take_slot(Engine(ws), "daily", at) is True and automations._take_slot(Engine(ws), "daily", at) is False
        run = Run(workspace_id=ws.id, automation="x", status="queued", steps=[])
        db.session.add(run)
        db.session.commit()
        assert leases.hold(run) is True and leases.mine(run)
        db.session.remove()
        db.engine.dispose()


def test_an_older_database_gets_the_new_columns(database_url, tmp_path):
    from app import create_app, database, migrate
    with database.throwaway(database_url, "migrate") as url:
        older = create_app({"TESTING": True, "WORKER": False, "SQLALCHEMY_DATABASE_URI": url, "DATA_DIR": tmp_path, "SECRET_KEY": "t",
                            "SQLALCHEMY_ENGINE_OPTIONS": {"pool_size": 2, "max_overflow": 2}})
        with older.app_context():
            with db.engine.begin() as conn:
                for table in ("job", "message", "run"):
                    conn.execute(text(f"ALTER TABLE {table} DROP COLUMN claimed_by, DROP COLUMN lease_until"))
                conn.execute(text("ALTER TABLE workspace DROP COLUMN next_due_at"))
                conn.execute(text("DROP INDEX ix_message_status"))
                conn.execute(text("UPDATE schema_step SET number = 16"))
            migrate.upgrade()
            seen = inspect(db.engine)
            for table in ("job", "message", "run"):
                assert {"claimed_by", "lease_until"} <= {c["name"] for c in seen.get_columns(table)}
            assert "next_due_at" in {c["name"] for c in seen.get_columns("workspace")}
            assert "ix_message_status" in {i["name"] for i in seen.get_indexes("message")}
            with db.engine.connect() as conn:
                assert conn.execute(text("SELECT number FROM schema_step")).scalar() == migrate.latest() == 17
            db.session.remove()
            db.engine.dispose()
