"""Public forms: a link anyone can fill in (an enquiry, a booking request). Each answer becomes a record, and starts
the automations a new record would."""

from ..blueprint import schema
from ..engine import automations
from ..engine.records import Engine, FieldErrors
from ..models import Workspace
from ..security import client_ip, form_posts
from ..storage import FileRejected, save
from ..webkit import Blueprint, abort, render_template, request

bp = Blueprint("forms", __name__)


def _find(token):
    for ws in Workspace.query.all():
        for table, value in ((ws.settings or {}).get("forms") or {}).items():
            if value == token:
                t = schema.table(ws.blueprint, table)
                if t and (t.get("public_form") or {}).get("enabled"):
                    return ws, t
    abort(404)


@bp.route("/f/<token>", methods=["GET", "POST"])
def fill(token):
    ws, t = _find(token)
    form = t["public_form"]
    fields = [schema.field(t, k) for k in form["fields"] if schema.field(t, k)]
    if request.method == "POST":
        if request.form.get("website"):  # a hidden field only robots fill in
            return render_template("public_form.html", ws=ws, t=t, form=form, fields=fields, done=True)
        if not form_posts.allow(f"{client_ip()}:{token}"):
            return render_template("public_form.html", ws=ws, t=t, form=form, fields=fields, values=request.form,
                                   errors={"_": "Too many answers from here; please try again later."}), 429
        engine = Engine(ws, by="public form")
        data = {f["key"]: (request.form.getlist(f["key"]) if f["type"] == "multi_select" else
                           bool(request.form.get(f["key"])) if f["type"] == "checkbox" else request.form.get(f["key"]))
                for f in fields if f["type"] in schema.STORED}
        try:
            record = engine.create(t["key"], data, automations=False)
        except FieldErrors as exc:
            return render_template("public_form.html", ws=ws, t=t, form=form, fields=fields, values=request.form, errors=exc.errors), 400
        for f in fields:
            if f["type"] in schema.FILES:
                for up in request.files.getlist(f["key"])[:5]:
                    if up and up.filename:
                        try:
                            save(ws.id, up, up.filename, source="form", record_id=record.id, field=f["key"], by="public form")
                        except FileRejected:
                            pass
        engine.reset()
        automations.on_change(engine, engine.get(record.id), None)
        return render_template("public_form.html", ws=ws, t=t, form=form, fields=fields, done=True)
    return render_template("public_form.html", ws=ws, t=t, form=form, fields=fields, values={}, errors={})
