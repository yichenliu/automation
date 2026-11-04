"""What the analyst has learnt from how the business uses its ERP: suggested improvements, each with its evidence and
proof from the business's own history, applied with a click (undoable) or dismissed."""
from datetime import datetime

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import jobs, learning
from ..builder import chat, checking
from ..engine import automations
from ..extensions import db
from ..models import Feedback, Job, Run, Suggestion, Version
from .erp import bp, ctx


@bp.route("/improvements", methods=["GET", "POST"])
@login_required
def improvements(wid):
    ws, engine = ctx(wid, "admin")
    if request.method == "POST":
        jobs.enqueue("learn", {"ai": True}, workspace_id=ws.id, dedupe=f"learn-now:{ws.id}", by=current_user.id)
        flash("Your analyst is looking at how your app is being used. New suggestions appear here (it takes a few minutes).", "success")
        return redirect(url_for("app.improvements", wid=wid))
    items = Suggestion.query.filter_by(workspace_id=ws.id).order_by(Suggestion.id.desc()).all()
    looking = Job.query.filter(Job.workspace_id == ws.id, Job.kind == "learn", Job.status.in_(["queued", "running", "waiting_ai"])).first()
    last = Job.query.filter_by(workspace_id=ws.id, kind="learn", status="done").order_by(Job.id.desc()).first()
    said = Feedback.query.filter(Feedback.workspace_id == ws.id, Feedback.status.in_(["open", "asked"])).order_by(Feedback.id.desc()).all()
    return render_template("app/improvements.html", ws=ws, e=engine, open_=[s for s in items if s.status == "open"],
                           decided=[s for s in items if s.status in ("applied", "dismissed")][:20], looking=looking, last=last, said=said)


@bp.route("/checks", methods=["GET", "POST"])
@login_required
def checks(wid):
    """The app's own checks: what it must always do, tried after every change to it. Run them now, add one in your own
    words, or drop one that no longer applies."""
    ws, engine = ctx(wid, "admin")
    if request.method == "POST":
        what = request.form.get("do")
        if what == "run":
            broke = checking.check(ws, "you asked")
            state = ws.state["checks"]
            wrong = [x for x in state["examples"] if not x["ok"]] + state["issues"] + _stopped(ws)
            flash("Everything checked works." if not wrong else
                  f"{len(wrong)} thing{'s' if len(wrong) != 1 else ''} not right yet: see below." + (f" {len(broke)} of them worked before." if broke else ""),
                  "success" if not wrong else "error")
        elif what == "add":
            said = (request.form.get("said") or "").strip()
            if said:
                jobs.enqueue("check_add", {"said": said[:1500], "by": current_user.display}, workspace_id=ws.id)
                flash("Writing your check and trying it on your app. It appears here when that's done (a few minutes, longer while the builder is busy), and you'll get a "
                      "notification saying whether your app does it right.", "success")
        elif what == "fix_rule":  # a rule that stopped while the app was used: the builder finds why and fixes it
            stopped = next((x for x in _stopped(ws) if x["key"] == request.form.get("rule")), None)
            if stopped is not None:
                chat.ask(ws, f"The rule “{stopped['name']}” stopped {stopped['count']} time{'s' if stopped['count'] != 1 else ''} while people "
                             f"were using the app, most recently with: “{stopped['error']}”. Find what causes it and fix it, so it works "
                             f"when it's used for real.", user=current_user)
                return redirect(url_for("builder.build", wid=wid))
        elif what == "fix":  # what isn't right (all of it, or the one pressed on), sent to the builder in one press
            text = checking.fix_request(ws, only=request.form.get("only") or None)
            if text:
                chat.ask(ws, text, user=current_user)
                return redirect(url_for("builder.build", wid=wid))
            flash("Nothing here needs fixing now.", "success")
        elif what == "put_aside":  # a check that couldn't be written, no longer wanted
            said = request.form.get("said") or ""
            ws.state = {**(ws.state or {}), "refused_checks": [x for x in (ws.state or {}).get("refused_checks") or [] if x.get("said") != said]}
            db.session.commit()
        elif what == "remove":
            title = request.form.get("title") or ""
            state = dict(ws.state or {})
            state["examples"] = [x for x in checking.examples(ws) if x.get("title") != title]
            if state.get("checks"):
                state["checks"] = {**state["checks"], "examples": [x for x in state["checks"]["examples"] if x["title"] != title]}
            ws.state = state
            db.session.commit()
            flash("That check is gone. It won't be tried again.", "success")
        return redirect(url_for("app.checks", wid=wid))
    adding = Job.query.filter(Job.workspace_id == ws.id, Job.kind == "check_add", Job.status.in_(["queued", "running", "waiting_ai"])).all()
    checking_now = Job.query.filter(Job.workspace_id == ws.id, Job.kind == "check", Job.status.in_(["queued", "running"])).first()
    last = (ws.state or {}).get("checks") or {}
    results = {x["title"]: x for x in last.get("examples", [])}
    kept = [{"title": x["title"], "by": x.get("by", "builder"), "result": results.get(x["title"])} for x in checking.examples(ws)]
    last_at = datetime.fromisoformat(last["at"]) if last.get("at") else None
    return render_template("app/checks.html", ws=ws, e=engine, kept=kept, last=last, last_at=last_at, adding=adding, checking_now=checking_now,
                           stopped=_stopped(ws), refused=(ws.state or {}).get("refused_checks") or [])


def _stopped(ws):
    """The rules that stopped half way while people used the app, since its design was last changed: the checks passing
    doesn't make the app right while one of these keeps happening. [{"key", "name", "count", "error"}], latest first."""
    current = Version.query.filter_by(workspace_id=ws.id, number=ws.version).first()
    runs = (Run.query.filter(Run.workspace_id == ws.id, Run.status == "failed", Run.created_at >= (current.created_at if current else datetime.min))
            .order_by(Run.id.desc()).limit(300).all())
    keys = {a["key"] for a in (ws.blueprint or {}).get("automations", [])}
    out = {}
    for run in runs:
        if run.automation in keys:
            out.setdefault(run.automation, {"key": run.automation, "name": run.name, "count": 0, "error": run.error or "it stopped"})["count"] += 1
    return list(out.values())


@bp.route("/feedback", methods=["POST"])
@login_required
def feedback(wid):
    """Anyone using the app says something isn't right, in their own words, from wherever they are."""
    ws, engine = ctx(wid, "viewer")
    page = request.form.get("page") or ""
    back = page if page.startswith("/") and not page.startswith("//") else url_for("app.home", wid=wid)
    text = (request.form.get("text") or "").strip()[:3000]
    if not text:
        flash("Say what isn't right, in your own words, and send it again.", "error")
        return redirect(back)
    db.session.add(Feedback(workspace_id=ws.id, user_id=current_user.id, page=page[:300], text=text, version=ws.version))
    db.session.commit()
    automations.notify(engine, f"{current_user.display} says something isn't right: “{text[:200]}”",
                       link=url_for("app.improvements", wid=wid) + "#said", to="admin")
    flash("Thanks: it's under Improvements, where you can have the builder fix it in a click." if engine._at_least("admin") else
          "Thanks: whoever runs your app will see this, and can have it fixed in a click.", "success")
    return redirect(back)


@bp.route("/feedback/<int:fid>", methods=["POST"])
@login_required
def decide_feedback(wid, fid):
    """The owner has the builder fix what someone said (it tries the fix out with an example of it, and keeps that
    example to check after every later change), or puts it aside."""
    ws, engine = ctx(wid, "admin")
    item = db.session.get(Feedback, fid)
    if item is None or item.workspace_id != ws.id:
        abort(404)
    if request.form.get("decision") == "fix":
        who = engine.members().get(item.user_id, "Someone")
        where = f" (they were on {item.page})" if item.page else ""
        more = (request.form.get("more") or "").strip()[:2000]
        reply = chat.ask(ws, f"{who} using the app said this isn't right{where}: “{item.text}”." + (f" {current_user.display} adds: “{more}”." if more else "")
                             + " Find what causes it and fix it, so it works the way they expected.", page=item.page, user=current_user)
        item.status, item.message_id = "asked", reply.id
        db.session.commit()
        return redirect(url_for("builder.build", wid=wid))
    item.status = "done" if request.form.get("decision") == "done" else "dismissed"
    db.session.commit()
    flash("Put aside." if item.status == "dismissed" else "Marked as sorted.", "success")
    return redirect(url_for("app.improvements", wid=wid) + "#said")


@bp.route("/improvements/<int:sid>", methods=["POST"])
@login_required
def decide_improvement(wid, sid):
    ws, engine = ctx(wid, "admin")
    s = db.session.get(Suggestion, sid)
    if s is None or s.workspace_id != ws.id or s.status != "open":
        abort(404)
    if request.form.get("decision") == "apply":
        try:
            number = learning.apply(ws, s, current_user.display)
            flash(f"Done: {s.title}. It's version {number} of the design; undo it there if you change your mind.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
    else:
        from ..models import utcnow
        s.status, s.decided_by, s.decided_at = "dismissed", current_user.display, utcnow()
        db.session.commit()
        flash("Dismissed. Your analyst won't suggest that again.", "success")
    return redirect(url_for("app.improvements", wid=wid))
