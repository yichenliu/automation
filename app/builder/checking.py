"""Every app's own checks, kept and run whenever its design changes, however it changes, and when the platform does.

An app's checks are its worked examples (ws.state["examples"]): those the builder wrote for each change it made, and
those the owner asked for in their own words ("a quote for $1,000 with 10% off comes to $990 with GST"). `check` tries
the app out in a throwaway copy (trial.py) with all of them and keeps the result in ws.state["checks"]. When something
that worked before doesn't any more (after an undo, a suggestion applied, a rule turned off, an edit by hand, or an
update of the platform itself), whoever runs the app is told what and why, with the way back.

    .venv/bin/python -m app.builder.checking        check every app now (after updating the platform)
"""
import hashlib
import sys
from pathlib import Path

from flask import current_app

from .. import ai, jobs
from ..engine import automations
from ..engine.records import Engine
from ..extensions import db
from ..models import Workspace, utcnow
from . import prompts, trial

KEEP = 30  # checks kept per app
_PLATFORM = {}


def platform_version():
    """What the platform's code is now (so an update of it is noticed): a fingerprint of its files."""
    if "v" not in _PLATFORM:
        root = Path(__file__).resolve().parent.parent
        digest = hashlib.sha1()
        for path in sorted(p for p in root.rglob("*") if p.suffix in (".py", ".html") and "__pycache__" not in p.parts):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
        _PLATFORM["v"] = digest.hexdigest()[:12]
    return _PLATFORM["v"]


def examples(workspace):
    return [x for x in (workspace.state or {}).get("examples") or [] if isinstance(x, dict) and x.get("steps")]


def page(workspace):
    return f"/w/{workspace.id}/app/checks"


def remember(workspace, walked, results, why, examples_=None, tell=True):
    """Keep what trying the app out showed, and say what stopped working since last time (unless the builder is saying it
    itself). Returns what broke."""
    played = examples_ if examples_ is not None else examples(workspace)
    shown = [{"title": r["title"], "ok": r["ok"], "lines": [[ok, line] for ok, line in r["lines"]], "problem": r.get("plain") or r.get("problem") or "",
              "kind": r["kind"], "by": x.get("by", "builder")}
             for x, r in zip(played, results) if r["kind"] not in ("crash", "example")]  # the AI's own slips aren't the app's
    issues = [i["plain"] for i in walked["issues"] if i["level"] == "fix"]
    state = dict(workspace.state or {})
    before = state.get("checks") or {}
    worked = {x["title"] for x in before.get("examples", []) if x["ok"]}
    broke = [x["title"] for x in shown if not x["ok"] and x["title"] in worked] + \
            [i for i in issues if i not in set(before.get("issues", []))] if before else []
    state["checks"] = {"at": utcnow().isoformat(timespec="minutes"), "version": workspace.version, "why": why,
                       "platform": platform_version(), "examples": shown, "issues": issues}
    workspace.state = state
    db.session.commit()
    if broke and tell:
        engine = Engine(workspace, by="checks")
        automations.notify(engine, f"After {why}, {len(broke)} thing{'s' if len(broke) != 1 else ''} your app did right "
                                   f"{'don’t' if len(broke) != 1 else 'doesn’t'} any more: {'; '.join(broke[:3])}. "
                                   f"Undo the change under How it's set up, or ask the builder to fix it.",
                           link=page(workspace), to="admin", kind="error")
    return broke


def check(workspace, why):
    """Try the app out with all its checks now. Returns what broke since last time."""
    walked, results = trial.try_out(workspace, examples(workspace), screens=False)
    return remember(workspace, walked, results, why)


def soon(workspace, why):
    """Check the app in the background (a change made by hand, an undo, a suggestion applied)."""
    if (workspace.blueprint or {}).get("tables"):
        jobs.enqueue("check", {"why": why}, workspace_id=workspace.id, dedupe=f"check:{workspace.id}")


@jobs.handler("check")
def _job(job):
    workspace = db.session.get(Workspace, job.workspace_id)
    broke = check(workspace, (job.payload or {}).get("why") or "a change")
    return {"broke": broke}


def add(workspace, said, by=None):
    """A check the owner asked for in their own words: the AI writes it as a worked example, it's tried at once and kept.
    Returns its result, or None if the AI couldn't make one of it."""
    engine = Engine(workspace)
    convo = [{"role": "system", "content": prompts.EXAMPLES.format(count="Write exactly one example: the one the owner describes below, "
                                                                           "with their numbers.")},
             {"role": "user", "content": f"The owner wants this checked, always: {said[:1500]}\n\nToday: {engine.today.isoformat()}"
                                         f"\n\nThe app:\n{prompts.outline(workspace.blueprint, engine.counts())}"}]
    steady = ai.building.set(True)
    try:
        content, _ = ai.chat(convo, schema=prompts.EXAMPLES_SCHEMA, think=current_app.config.get("AI_THINK_BUILDER", False))
    finally:
        ai.building.reset(steady)
    found = [x for x in ai.parse_json(content).get("examples") or [] if isinstance(x, dict) and x.get("steps")][:1]
    if not found:
        return None
    example = {**found[0], "title": said.strip()[:160], "by": by or "owner"}
    with trial.copy_of(workspace) as copy:
        result = trial.play(copy, example)
    if result["kind"] == "example":  # it names what isn't in the app: not kept, so it can't fail for ever
        return result
    state = dict(workspace.state or {})
    state["examples"] = ([example] + [x for x in examples(workspace) if x.get("title") != example["title"]])[:KEEP]
    workspace.state = state
    db.session.commit()
    check(workspace, "a check was added")
    return result


@jobs.handler("check_add")
def _add_job(job):
    workspace = db.session.get(Workspace, job.workspace_id)
    result = add(workspace, job.payload["said"], job.payload.get("by"))
    engine = Engine(workspace, by="checks")
    said = job.payload["said"][:120]
    refused = [x for x in (workspace.state or {}).get("refused_checks") or [] if x.get("said") != job.payload["said"]]
    if result is None or result["kind"] == "example":
        text = f"I couldn't turn “{said}” into a check: try saying it with the names and numbers you see on the screen."
        why = (result or {}).get("plain") or (result or {}).get("problem") or "it couldn't be written as steps in your app"
        refused = [{"said": job.payload["said"], "why": why[:300], "at": utcnow().isoformat()}] + refused
    elif result["ok"]:
        text = f"Your new check “{said}” works: your app does this right, and it's checked after every change from now on."
    else:
        text = f"Your new check “{said}” doesn't work yet: {result.get('plain') or result.get('problem')}. Ask the builder to fix it."
    workspace.state = {**(workspace.state or {}), "refused_checks": refused[:5]}  # on the Checks page until tried again or put aside
    db.session.commit()
    automations.notify(engine, text, link=page(workspace), to="admin", kind="info" if result and result["ok"] else "error")
    return {"ok": bool(result and result["ok"])}


def after_platform_update():
    """Check every app whose checks were last run on other platform code (the worker does this when it starts)."""
    for workspace in Workspace.query.all():
        last = ((workspace.state or {}).get("checks") or {}).get("platform")
        if examples(workspace) and last != platform_version():
            soon(workspace, "the platform was updated")


def main():
    from .. import create_app
    app = create_app({"WORKER": False})
    with app.app_context():
        bad = 0
        for workspace in Workspace.query.order_by(Workspace.id).all():
            if not (workspace.blueprint or {}).get("tables"):
                continue
            broke = check(workspace, "the platform was updated")
            state = workspace.state["checks"]
            failing = [x["title"] for x in state["examples"] if not x["ok"]] + state["issues"]
            bad += bool(failing)
            print(f"{workspace.id} {workspace.name}: {len(state['examples'])} checks, {len(failing)} not right"
                  + (f" ({len(broke)} newly)" if broke else "") + "".join(f"\n   x {f}" for f in failing[:6]))
        return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
