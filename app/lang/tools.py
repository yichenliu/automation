"""The tools every builder uses, whatever it is: the AI builder, the in-app assistant, a person's own agent over MCP,
or a script through the API. Each one is a plain function over a workspace, with a typed input and a plain result, so
the same ones serve every caller. A tool's description is its docstring and its input schema is its model's: one
definition, as everywhere in the language.

    language()                     what can be written: the catalog, and its fingerprint
    describe(ws)                   the app in the language's terms, and its version
    what_if(ws, patches)           what these patches would change, what would be refused and why, and how the
                                   app's checks would come out, all without saving anything
    apply(ws, patches, version)    the same, saved as a new version if nothing was refused and the app is still at
                                   `version` (another change made meanwhile is never overwritten)
    test(ws)                       every check the app has, acted out in the simulator now
    explain(ws, record, check)     why: a record's rules, what ran on it and what's open to it; a check acted out, and why
    undo(ws, version)              back to an earlier version
"""
from pydantic import BaseModel, Field

from ..extensions import db

from . import apply as apply_patches, checks as feature_checks, describe as describe_app
from .registry import catalog, fingerprint


class Patches(BaseModel):
    patches: list[dict] = Field(description="Keel language patches (the language tool lists them all)")


class Applying(Patches):
    version: int = Field(description="the app's version the patches were written for (describe gives it)")


class Explaining(BaseModel):
    record: int | None = Field(None, description="a record's id: why it's where it is, what may happen to it next, what its rules found")
    check: str | None = Field(None, description="a check's title (as test lists them): acted out now, with why it comes out as it does")


class Version(BaseModel):
    version: int


def _examples(ws, bp):
    kept = [x for x in (ws.state or {}).get("examples") or [] if isinstance(x, dict) and not str(x.get("by", "")).startswith("feature:")]
    return feature_checks(bp) + kept


def _tried(ws, bp):
    from ..builder import trial
    walked, results = trial.try_out(ws, _examples(ws, bp), screens=False, blueprint=bp)
    return {"checks": [{"title": r["title"], "ok": r["ok"], "what": r.get("plain") or r.get("problem") or "",
                        **({"why": r["why"]} if r.get("why") else {})} for r in results],
            "found": [i["plain"] for i in walked.get("issues", []) if i.get("level") == "fix"]}  # what walking the app found wrong


def language():
    """What a builder can write: every field, rule, step, patch and feature of the Keel language, with examples."""
    return {"catalog": catalog(), "fingerprint": fingerprint()}


def describe(ws):
    """The app as it is now, in the language's terms, and its version."""
    return {"version": ws.version or 0, "app": describe_app(ws.blueprint or {})}


def what_if(ws, patches):
    """What these patches would do, saving nothing: the changes, any patch refused and exactly why, and how every
    check of the app would come out with them."""
    out = apply_patches(ws.blueprint or {}, patches)
    return {"changes": out.changes, "refused": out.refused, "problems": out.problems, **_tried(ws, out.blueprint)}


def apply(ws, patches, version, by=None):
    """Make these patches as a new version, if none is refused and the app is still at `version`; otherwise nothing
    is saved and the answer says why."""
    from ..builder.chat import save_version
    if (ws.version or 0) != version:
        return {"saved": False, "why": f"the app is at version {ws.version or 0} now, not {version}: describe it again and write the patches for it"}
    out = apply_patches(ws.blueprint or {}, patches)
    if out.problems:
        return {"saved": False, "why": "some patches were refused", "refused": out.refused, "problems": out.problems}
    if not out.changes and not out.data:
        return {"saved": False, "why": "nothing would change"}
    number = save_version(ws, out.blueprint, out.changes, "api", by=by) if out.changes else (ws.version or 0)
    added = []
    if out.data:  # the records the patches add (the owner's staff, the usual units), as the builder's own save adds them
        from ..builder.chat import _data
        added, _ = _data(ws, out.data, by=by)
        db.session.commit()
    return {"saved": True, "version": number, "changes": out.changes + added, **_tried(ws, ws.blueprint)}


def test(ws):
    """Act out every check the app has (its features' own, the builder's and the owner's) in the simulator now."""
    return _tried(ws, ws.blueprint or {})


def explain(ws, record=None, check=None):
    """Why: for a record, the rules its list keeps and whether it keeps each now, the rules that ran on it and how they
    ended, and the moves open to it with what stops each; for a check, it acted out now and why it comes out as it does
    (the rules about it and what each found)."""
    from ..builder import trial
    from ..engine.records import Engine
    from ..models import Run
    out = {}
    if check:
        wanted = [x for x in _examples(ws, ws.blueprint or {}) if str(x.get("title", "")).strip().lower() == check.strip().lower()]
        if not wanted:
            return {"why": f"there's no check called “{check}”: test lists them"}
        _, results = trial.try_out(ws, wanted[:1], screens=False)
        r = results[0]
        out["check"] = {"title": r["title"], "ok": r["ok"], "what": r.get("plain") or r.get("problem") or "", "why": r.get("why") or []}
    if record:
        engine = Engine(ws)
        found = engine.get(int(record))
        if found is None:
            return {**out, "why": f"there's no record {record}"}
        t = engine.tables[found.table]
        rules = []
        for rule in t.get("rules") or []:
            applies = not rule.get("when") or engine.matches(found, rule["when"])
            try:
                kept = engine._keeps(t, found, rule)
            except Exception:  # noqa: BLE001 - a rule that can't be worked out is said so
                kept = None
            rules.append({"rule": rule.get("message"), "applies": applies, "keeps_it": kept})
        runs = [{"rule": r.name or r.automation, "status": r.status, "error": r.error or "", "when": r.created_at.isoformat(timespec="minutes")}
                for r in Run.query.filter_by(workspace_id=ws.id, record_id=found.id).order_by(Run.id.desc()).limit(10)]
        moves = [{"button": m.get("label") or m["to"], "to": m["to"], "stopped_by": problem or ""} for _, m, problem in engine.moves(found)]
        flow = t.get("workflow") or {}
        out["record"] = {"list": t["key"], "title": engine.title(found), "stage": (found.data or {}).get(flow.get("field")) if flow else None,
                         "rules": rules, "rules_that_ran": runs, "moves": moves}
    return out or {"why": "say which record (its id) or which check (its title)"}


def undo(ws, version, by=None):
    """Put the app back as it was at an earlier version (saved as a new version, the same as that one)."""
    from ..builder.chat import restore
    restore(ws, version, by=by)
    return {"version": ws.version}


TOOLS = {"language": (language, None), "describe": (describe, None), "what_if": (what_if, Patches), "apply": (apply, Applying),
         "test": (test, None), "explain": (explain, Explaining), "undo": (undo, Version)}


def schemas():
    """Each tool as an agent sees it: its name, what it does (its docstring) and its input (its model's schema)."""
    out = []
    for name, (fn, model) in TOOLS.items():
        out.append({"name": f"keel_{name}", "description": " ".join((fn.__doc__ or "").split()),
                    "inputSchema": model.model_json_schema() if model else {"type": "object", "properties": {}}})
    return out


def call(ws, name, arguments, by=None):
    """Run a tool by its name (keel_describe or describe) with its arguments, checked against its model."""
    key = name.removeprefix("keel_")
    if key not in TOOLS:
        raise KeyError(f"there's no tool “{name}” (tools: {', '.join('keel_' + k for k in TOOLS)})")
    fn, model = TOOLS[key]
    if key == "language":
        return fn()
    args = model.model_validate(arguments or {}).model_dump() if model else {}
    if key in ("apply", "undo"):
        args["by"] = by
    return fn(ws, **args)
