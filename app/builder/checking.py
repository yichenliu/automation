"""Every app's own checks, kept and run whenever its design changes, however it changes, and when the platform does.

An app's checks are its worked examples (ws.state["examples"]): those the builder wrote for each change it made, and
those the owner asked for in their own words ("a quote for $1,000 with 10% off comes to $990 with GST"). `check` tries
the app out in a throwaway copy (trial.py) with all of them and keeps the result in ws.state["checks"]. When something
that worked before doesn't any more (after an undo, a suggestion applied, a rule turned off, an edit by hand, or an
update of the platform itself), whoever runs the app is told what and why, with the way back.

    .venv/bin/python -m app.builder.checking        check every app now (after updating the platform)
"""
import hashlib
import json
import re
import sys
from pathlib import Path


from .. import ai, jobs
from ..blueprint import schema
from ..engine import automations
from ..engine.records import Engine
from ..extensions import db
from ..models import Workspace, utcnow
from .. import lang
from . import prompts, trial
from ..core.settings import settings

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


def theirs(check):
    """Whether a check is the owner's own (written from what they said), not the builder's or a feature's."""
    by = str((check or {}).get("by") or "builder")
    return by != "builder" and not by.startswith("feature:")


def examples(workspace):
    return [x for x in (workspace.state or {}).get("examples") or [] if isinstance(x, dict) and x.get("steps")]


def page(workspace):
    return f"/w/{workspace.id}/app/checks"


def remember(workspace, walked, results, why, examples_=None, tell=True):
    """Keep what trying the app out showed, and say what stopped working since last time (unless the builder is saying it
    itself). Returns what broke."""
    played = examples_ if examples_ is not None else examples(workspace)
    shown = [{"title": r["title"], "ok": r["ok"], "lines": [[ok, line] for ok, line in r["lines"]], "problem": r.get("plain") or r.get("problem") or "",
              "kind": r["kind"], "by": x.get("by", "builder")} if r["kind"] not in ("crash", "example") else
             {"title": r["title"], "ok": False, "lines": [], "kind": "example", "by": x.get("by"),
              "problem": f"it can't be tried on your app as it is now: {r.get('plain') or r.get('problem') or 'a step of it no longer fits'}"}
             for x, r in zip(played, results)  # the AI's own slips aren't the app's, but an owner's check never quietly goes
             if r["kind"] not in ("crash", "example") or (x.get("by", "builder") != "builder" and r["kind"] == "example")]
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
                                   f"Undo the change under Versions, or ask the builder to fix it.",
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
    convo = _convo(workspace, engine, said)
    steady = ai.building.set(True)
    try:
        content, _ = ai.chat(convo, schema=prompts.ONE_EXAMPLE_SCHEMA, think=settings().get("AI_THINK_BUILDER", False))
    finally:
        ai.building.reset(steady)
    found = [x for x in ai.parse_json(content).get("examples") or [] if isinstance(x, dict) and x.get("steps")][:1]
    if not found:
        return None
    example = {**found[0], "title": said.strip()[:160], "by": by or "owner"}
    with trial.copy_of(workspace) as copy:
        result = trial.play(copy, example)
    if not result["ok"] and result["kind"] in ("app", "example"):  # the example's slip, or the app's? Looked at once more
        example, result = _again(workspace, engine, convo, said, example, result)
    result = {**result, "example": example}  # what was acted out, to look into
    if result["kind"] in ("example", "missing"):  # it names what isn't in the app: not kept, so it can't fail for ever
        return result
    state = dict(workspace.state or {})
    state["examples"] = ([example] + [x for x in examples(workspace) if x.get("title") != example["title"]])[:KEEP]
    workspace.state = state
    db.session.commit()
    check(workspace, "a check was added")
    return result


def _convo(workspace, engine, said):
    """What the AI is shown to write the owner's check as a worked example of the app as it is."""
    return [{"role": "system", "content": prompts.EXAMPLES.format(count="Write exactly one example: the one the owner describes below, "
                                                                           "with their numbers.", when_none=prompts.ALWAYS_ONE)},
            {"role": "user", "content": f"The owner wants this checked, always: {said[:1500]}\n\nToday: {schema.today_words(engine.today)}"
                                        f"\n\nThe app (use only the lists, fields, stages, buttons and rules named here, by their keys; its "
                                        f"features say how it already works):\n{lang.describe(workspace.blueprint or {})}"}]


def stale(workspace):
    """The owner's checks that, when last tried, couldn't be acted out on the app as it is now (a field they named was
    renamed or taken away, something the app now needs wasn't given): [(check, result)]."""
    last = {x["title"]: x for x in ((workspace.state or {}).get("checks") or {}).get("examples") or []}
    out = []
    for x in examples(workspace):
        shown = last.get(x.get("title"))
        if theirs(x) and shown and not shown.get("ok") and shown.get("kind") == "example":
            out.append((x, {"kind": "example", "ok": False, "lines": [], "problem": str(shown.get("problem") or "").removeprefix(
                "it can't be tried on your app as it is now: ")}))
    return out


def refit(workspace, found):
    """The owner's checks that can't be acted out on the app as it is now, each written again from their own words for
    the app as it is (never dropped, never quietly kept failing for a reason that isn't the app's). One that names what
    the app doesn't do at all goes to the Checks page as such, for the builder to make. `found`: [(check, result)].
    Returns the titles rewritten."""
    engine, done, missing = Engine(workspace), {}, []
    for x, r in found[:4]:
        said = x["title"]
        try:
            example, result = _again(workspace, engine, _convo(workspace, engine, said), said, x, r)
        except ai.AIError:
            continue
        if result.get("kind") == "missing":
            missing.append({"said": said, "why": result["plain"].split(": ", 1)[-1][:300], "missing": True,
                            "by": x.get("by") or "owner", "at": utcnow().isoformat()})
        elif example is not x and result.get("kind") != "example":
            done[said] = example
    if not done and not missing:
        return []
    state = dict(workspace.state or {})
    gone = {m_["said"] for m_ in missing}
    state["examples"] = [done.get(x.get("title"), x) for x in examples(workspace) if x.get("title") not in gone]
    state["refused_checks"] = (missing + [x for x in state.get("refused_checks") or [] if x.get("said") not in gone])[:5]
    workspace.state = state
    db.session.commit()
    return list(done) + list(gone)


def _again(workspace, engine, convo, said, example, result):
    """Before the owner hears their app doesn't do what they asked: the AI looks at what happened and either corrects
    its own example (a sum worked out wrong, a step the owner may do tried as the owner), which is then acted out
    instead, or says what the app doesn't do yet. Returns (example, result)."""
    staff = _as_staff(workspace, example, result)
    if staff is not None:
        return staff
    lines = "\n".join(f"- {'as expected' if ok else 'NOT as expected'}: {line}" for ok, line in result.get("lines") or [])
    what = "\n".join(x for x in (result.get("problem") or "", lines) if x)
    how = "couldn't be acted out" if result["kind"] == "example" else "didn't come out as expected"
    ask = convo + [{"role": "user", "content": prompts.CHECK_AGAIN.format(
        said=said[:1500], how=how, what=what[:3000], example=json.dumps({k: v for k, v in example.items() if k != "by"}, ensure_ascii=False)[:6000])}]
    steady = ai.building.set(True)
    try:
        content, _ = ai.chat(ask, schema=prompts.CHECK_AGAIN_SCHEMA, think=settings().get("AI_THINK_BUILDER", False))
        answer = ai.parse_json(content)
    except ai.AIError:
        return example, result
    finally:
        ai.building.reset(steady)
    fixed = [x for x in answer.get("examples") or [] if isinstance(x, dict) and x.get("steps")][:1]
    missing = str(answer.get("missing") or "").strip().rstrip(".")
    if answer.get("verdict") == "example" and fixed:
        corrected = {**fixed[0], "title": example["title"], "by": example["by"]}
        with trial.copy_of(workspace) as copy:
            again = {**trial.play(copy, corrected), "looked_again": "example"}
        staff = _as_staff(workspace, corrected, again)
        if staff is not None:
            return staff
        if again["kind"] == "example" and ABSENT.search(again.get("problem") or ""):  # still needs what isn't there: it isn't
            return corrected, {**again, "kind": "missing", "plain": f"your app doesn't do this yet: {missing or _absent(again)}"}
        return corrected, again
    if result["kind"] == "example" and ABSENT.search(result.get("problem") or "") and answer.get("verdict") != "example":
        return example, {**result, "kind": "missing", "plain": f"your app doesn't do this yet: {missing or _absent(result)}", "looked_again": "app"}
    if answer.get("verdict") == "app" and missing and (result["kind"] == "example" or _nothing_stops(result)):
        # it isn't in the app at all: it doesn't do it (not “should have been stopped”: nothing there could stop it)
        return example, {**result, "kind": "missing", "plain": f"your app doesn't do this yet: {missing}", "looked_again": "app"}
    return example, {**result, "looked_again": answer.get("verdict") or ""}


def _as_staff(workspace, example, result):
    """A step that should have been refused but went through, done by the owner (no "by"): the owner may do nearly
    anything (their own sign-off is the sign-off), so what the check means is that others can't. Tried again with an
    ordinary staff member doing those steps; (example, result) if that comes out right, else None."""
    steps = example.get("steps") or []
    if "should have been stopped" not in str(result.get("plain") or result.get("problem") or "") \
            or not any(s.get("refused") and not s.get("by") for s in steps):
        return None
    variant = {**example, "steps": [{**s, "by": "staff"} if s.get("refused") and not s.get("by") else s for s in steps]}
    with trial.copy_of(workspace) as copy:
        again = trial.play(copy, variant)
    return (variant, {**again, "looked_again": "example"}) if again["ok"] else None


ABSENT = re.compile(r"there's no (automation|button) “")


def _absent(result):
    """Said plainly: the rule or button an example of the check needs, which the app hasn't got."""
    found = re.search(r"there's no (automation|button) “([^”]*)”", result.get("problem") or "")
    what = "rule" if found and found.group(1) == "automation" else "button"
    return f"it has no {what} that does it" + (f" (“{found.group(2).replace('_', ' ')}”)" if found and found.group(2) else "")


def _nothing_stops(result):
    """A check that came out wrong only because a step that should have been refused wasn't (“should have been stopped,
    but wasn't”): with the AI saying what's missing, the owner hears their app doesn't do it yet."""
    return "should have been stopped" in (result.get("plain") or "") and not any(not ok for ok, _ in result.get("lines") or [])


@jobs.handler("check_add")
def _add_job(job):
    workspace = db.session.get(Workspace, job.workspace_id)
    result = add_and_tell(workspace, job.payload["said"], job.payload.get("by"))
    return {"ok": bool(result and result["ok"])}


def add_and_tell(workspace, said, by=None):
    """A check the owner asked for, added (add) and its outcome told them: one the app doesn't do yet, or that couldn't be
    written, stays on the Checks page (with the way on) until it's tried again or put aside. Returns add's result."""
    result = add(workspace, said, by)
    engine = Engine(workspace, by="checks")
    short = said[:120]
    refused = [x for x in (workspace.state or {}).get("refused_checks") or [] if x.get("said") != said]
    if result is not None and result["kind"] == "missing":  # it isn't in the app yet: said so, and kept in view
        missing = result["plain"].split(": ", 1)[-1]
        text = f"Your app doesn't do “{short}” yet: {missing}. Ask the builder to add it (there's a button on the Checks page)."
        refused = [{"said": said, "why": missing[:300], "missing": True, "by": by or "owner", "at": utcnow().isoformat()}] + refused
    elif result is None or result["kind"] == "example":
        text = f"I couldn't turn “{short}” into a check: try saying it with the names and numbers you see on the screen."
        why = (result or {}).get("plain") or (result or {}).get("problem") or "it couldn't be written as steps in your app"
        refused = [{"said": said, "why": why[:300], "by": by or "owner", "at": utcnow().isoformat()}] + refused
    elif result["ok"]:
        text = f"Your new check “{short}” works: your app does this right, and it's checked after every change from now on."
    else:
        text = f"Your new check “{short}” doesn't work yet: {result.get('plain') or result.get('problem')}. Ask the builder to fix it."
    workspace.state = {**(workspace.state or {}), "refused_checks": refused[:5]}  # on the Checks page until tried again or put aside
    db.session.commit()
    automations.notify(engine, text, link=page(workspace), to="admin", kind="info" if result and result["ok"] else "error")
    return result


def not_right(workspace):
    """What the Checks page shows isn't right: {"failing": [{"title", "why"}], "issues": [plain], "missing": [{"said", "why"}]}
    (the checks that don't come out right, what trying the app out found wrong, and what the owner asked to be checked
    that the app doesn't do yet)."""
    last = (workspace.state or {}).get("checks") or {}
    failing = []
    for x in last.get("examples") or []:
        if not x.get("ok"):
            lines = [line for ok, line in x.get("lines") or [] if not ok]
            failing.append({"title": x["title"], "why": "; ".join(lines[:4]) or x.get("problem") or "it didn't come out right"})
    missing = [{"said": x["said"], "why": x.get("why") or ""} for x in (workspace.state or {}).get("refused_checks") or [] if x.get("missing")]
    return {"failing": failing, "issues": list(last.get("issues") or []), "missing": missing}


def fix_request(workspace, only=None):
    """The message the owner sends the builder with one press ("Ask the builder to fix them"): what isn't right, in the
    words the Checks page uses. `only`: just the check (a title, or what was said) the owner pressed it on. "" if nothing."""
    found = not_right(workspace)
    if only:
        found = {"failing": [x for x in found["failing"] if x["title"] == only], "issues": [i for i in found["issues"] if i == only],
                 "missing": [x for x in found["missing"] if x["said"] == only]}
    parts = []
    if found["failing"] or found["issues"]:
        parts.append("Please fix what isn't right on my Checks page:\n" + "\n".join(
            [f"- “{x['title']}”: {x['why']}" for x in found["failing"][:8]] + [f"- {i}" for i in found["issues"][:6]]))
    if found["missing"]:
        parts.append(("Please also make my app do these" if parts else "Please make my app do these") + " (it doesn't yet):\n"
                     + "\n".join(f"- “{x['said']}” ({x['why']})" if x["why"] else f"- “{x['said']}”" for x in found["missing"][:5]))
    return "\n\n".join(parts)


def try_again_missing(workspace):
    """After the builder has had a go at what the app didn't do yet: those checks are tried again, as the owner would."""
    for x in (workspace.state or {}).get("refused_checks") or []:
        if x.get("missing"):
            jobs.enqueue("check_add", {"said": x["said"], "by": x.get("by") or "owner"}, workspace_id=workspace.id,
                         dedupe=f"check_add:{workspace.id}:{hashlib.sha1(x['said'].encode()).hexdigest()[:10]}")


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
