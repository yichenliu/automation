"""The list of a person's ERPs, and starting a new one (by describing it, from Microsoft 365, or from a spreadsheet)."""

from ..blueprint import schema
from ..extensions import db
from ..models import Membership, Record, Version, Workspace
from ..webkit import Blueprint, current_user, flash, login_required, redirect, render_template, request, url_for

bp = Blueprint("workspaces", __name__)


@bp.route("/")
def home():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    member = Membership.query.filter_by(user_id=current_user.id).order_by(Membership.id.desc()).first()
    if member is None:
        return redirect(url_for("workspaces.new"))
    if member.role == "portal":
        return redirect(url_for("portal.home", wid=member.workspace_id))
    if member.can("admin") and not (db.session.get(Workspace, member.workspace_id).blueprint or {}).get("tables"):
        return redirect(url_for("builder.build", wid=member.workspace_id))
    return redirect(url_for("app.home", wid=member.workspace_id))


@bp.route("/workspaces")
@login_required
def index():
    members = Membership.query.filter_by(user_id=current_user.id).all()
    spaces = [(m, db.session.get(Workspace, m.workspace_id)) for m in members]
    counts = {ws.id: Record.query.filter_by(workspace_id=ws.id, deleted_at=None).count() for _, ws in spaces}
    return render_template("workspaces.html", spaces=spaces, counts=counts)


@bp.route("/workspaces/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:200]
        if not name:
            flash("Give your app a name, such as your business's name.", "error")
            from ..blueprint import modules
            return render_template("new_workspace.html", form=request.form, parts=modules.MODULES), 400
        blueprint = schema.empty(name)
        ws = Workspace(owner_id=current_user.id, name=name, about=(request.form.get("about") or "").strip()[:2000],
                       blueprint=blueprint, version=0, state={}, settings={"business": {"name": name}})
        db.session.add(ws)
        db.session.flush()
        db.session.add(Membership(workspace_id=ws.id, user_id=current_user.id, role="owner"))
        db.session.add(Version(workspace_id=ws.id, number=0, blueprint=blueprint, changes=["Started your app (empty)"], source="start",
                               by=current_user.display))
        db.session.commit()
        start = request.form.get("start", "chat")
        if start == "parts":  # ready-made parts, then the builder carries on from them
            from ..blueprint import modules
            chosen = [k for k in request.form.getlist("parts") if k in modules.MODULES]
            if chosen:
                _add_parts(ws, chosen)
                return redirect(url_for("app.home", wid=ws.id))
        if start == "microsoft":
            return redirect(url_for("builder.discover", wid=ws.id))
        return redirect(url_for("builder.build", wid=ws.id, start=start))
    from ..blueprint import modules
    return render_template("new_workspace.html", form={}, parts=modules.MODULES)


def with_parts(blueprint, chosen):
    """A design with ready-made parts added (not saved), made by their Keel language patches. Returns the outcome (its
    blueprint and changes), or raises ValueError saying what didn't fit."""
    from .. import lang
    from ..blueprint import modules
    out = lang.apply(blueprint, modules.ops_for(chosen, blueprint))
    if out.problems:
        raise ValueError("; ".join(out.problems))
    return out


def _add_parts(ws, chosen):
    from ..blueprint import modules
    from ..builder import chat
    result = with_parts(ws.blueprint, chosen)
    names = ", ".join(modules.MODULES[k]["label"] for k in chosen)
    chat.save_version(ws, result.blueprint, [f"Started from the ready-made {names}"] + result.changes, "parts", by=current_user.id)
