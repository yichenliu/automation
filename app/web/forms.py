"""Public forms: a link anyone can fill in (an enquiry, a booking request). Each answer becomes a record, and starts
the automations a new record would."""
from fastapi import APIRouter, Depends, HTTPException, Request

from ..blueprint import schema
from ..engine import automations
from ..engine.records import Engine, FieldErrors
from ..models import Workspace
from ..security import client_ip, form_posts
from ..storage import FileRejected, save
from .http import Form, PageRoute, check_csrf, form_of, render

router = APIRouter(route_class=PageRoute, dependencies=[Depends(check_csrf)])  # the form posts the token its page gave


def _find(token):
    for ws in Workspace.query.all():
        for table, value in ((ws.settings or {}).get("forms") or {}).items():
            if value == token:
                t = schema.table(ws.blueprint, table)
                if t and (t.get("public_form") or {}).get("enabled"):
                    return ws, t
    raise HTTPException(404)


@router.api_route("/f/{token}", methods=["GET", "POST"], name="forms.fill")
def fill(request: Request, token: str, posted: Form = Depends(form_of)):
    ws, t = _find(token)
    form = t["public_form"]
    fields = [schema.field(t, k) for k in form["fields"] if schema.field(t, k)]
    if request.method == "POST":
        if posted.get("website"):  # a hidden field only robots fill in
            return render(request, "public_form.html", ws=ws, t=t, form=form, fields=fields, done=True)
        if not form_posts.allow(f"{client_ip(request)}:{token}"):
            return render(request, "public_form.html", status_code=429, ws=ws, t=t, form=form, fields=fields, values=posted,
                          errors={"_": "Too many answers from here; please try again later."})
        engine = Engine(ws, by="public form")
        data = {f["key"]: (posted.getlist(f["key"]) if f["type"] == "multi_select" else
                           bool(posted.get(f["key"])) if f["type"] == "checkbox" else posted.get(f["key"]))
                for f in fields if f["type"] in schema.STORED}
        try:
            record = engine.create(t["key"], data, automations=False)
        except FieldErrors as exc:
            return render(request, "public_form.html", status_code=400, ws=ws, t=t, form=form, fields=fields, values=posted, errors=exc.errors)
        for f in fields:
            if f["type"] in schema.FILES:
                for up in posted.files.getlist(f["key"])[:5]:
                    if up and up.filename:
                        try:
                            save(ws.id, up, up.filename, source="form", record_id=record.id, field=f["key"], by="public form")
                        except FileRejected:
                            pass
        engine.reset()
        automations.on_change(engine, engine.get(record.id), None)
        return render(request, "public_form.html", ws=ws, t=t, form=form, fields=fields, done=True)
    return render(request, "public_form.html", ws=ws, t=t, form=form, fields=fields, values={}, errors={})
