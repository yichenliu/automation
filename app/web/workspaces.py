"""The list of a person's ERPs, and starting a new one (by describing it, from Microsoft 365, or from a spreadsheet)."""
from fastapi import APIRouter, Depends, Request

from ..blueprint import schema
from ..extensions import db
from ..models import Membership, Record, Version, Workspace
from .http import Form, PageRoute, check_csrf, current_user, flash, form_of, redirect, render, signed_in, url_for

router = APIRouter(route_class=PageRoute, dependencies=[Depends(check_csrf)])


@router.api_route("/", methods=["GET"], name="workspaces.home")
def home(request: Request):
    user = current_user(request)
    if not user.is_authenticated:
        return redirect(url_for("auth.login"))
    member = Membership.query.filter_by(user_id=user.id).order_by(Membership.id.desc()).first()
    if member is None:
        return redirect(url_for("workspaces.new"))
    if member.role == "portal":
        return redirect(url_for("portal.home", wid=member.workspace_id))
    if member.can("admin") and not (db.session.get(Workspace, member.workspace_id).blueprint or {}).get("tables"):
        return redirect(url_for("builder.build", wid=member.workspace_id))
    return redirect(url_for("app.home", wid=member.workspace_id))


@router.api_route("/workspaces", methods=["GET"], name="workspaces.index")
def index(request: Request, user=Depends(signed_in)):
    members = Membership.query.filter_by(user_id=user.id).all()
    spaces = [(m, db.session.get(Workspace, m.workspace_id)) for m in members]
    counts = {ws.id: Record.query.filter_by(workspace_id=ws.id, deleted_at=None).count() for _, ws in spaces}
    return render(request, "workspaces.html", spaces=spaces, counts=counts)


@router.api_route("/workspaces/new", methods=["GET", "POST"], name="workspaces.new")
def new(request: Request, user=Depends(signed_in), form: Form = Depends(form_of)):
    from ..blueprint import modules
    if request.method == "POST":
        name = (form.get("name") or "").strip()[:200]
        if not name:
            flash(request, "Give your app a name, such as your business's name.", "error")
            return render(request, "new_workspace.html", status_code=400, form=form, parts=modules.MODULES)
        blueprint = schema.empty(name)
        ws = Workspace(owner_id=user.id, name=name, about=(form.get("about") or "").strip()[:2000],
                       blueprint=blueprint, version=0, state={}, settings={"business": {"name": name}})
        db.session.add(ws)
        db.session.flush()
        db.session.add(Membership(workspace_id=ws.id, user_id=user.id, role="owner"))
        db.session.add(Version(workspace_id=ws.id, number=0, blueprint=blueprint, changes=["Started your app (empty)"], source="start",
                               by=user.display))
        db.session.commit()
        start = form.get("start", "chat")
        if start == "parts":  # ready-made parts, then the builder carries on from them
            chosen = [k for k in form.getlist("parts") if k in modules.MODULES]
            if chosen:
                _add_parts(ws, chosen, user)
                return redirect(url_for("app.home", wid=ws.id))
        if start == "microsoft":
            return redirect(url_for("builder.discover", wid=ws.id))
        return redirect(url_for("builder.build", wid=ws.id, start=start))
    return render(request, "new_workspace.html", form={}, parts=modules.MODULES)


def with_parts(blueprint, chosen):
    """A design with ready-made parts added (not saved), made by their Keel language patches. Returns the outcome (its
    blueprint and changes), or raises ValueError saying what didn't fit."""
    from .. import lang
    from ..blueprint import modules
    out = lang.apply(blueprint, modules.ops_for(chosen, blueprint))
    if out.problems:
        raise ValueError("; ".join(out.problems))
    return out


def _add_parts(ws, chosen, user):
    from ..blueprint import modules
    from ..builder import chat
    result = with_parts(ws.blueprint, chosen)
    names = ", ".join(modules.MODULES[k]["label"] for k in chosen)
    chat.save_version(ws, result.blueprint, [f"Started from the ready-made {names}"] + result.changes, "parts", by=user.id)
