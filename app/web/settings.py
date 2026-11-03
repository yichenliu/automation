"""Workspace settings: the ERP's name and business details (used on documents), appearance, members and invitations,
connections, public forms, data (backups and exports) and the AI."""
import io
import re
import secrets
import zipfile
import csv
from zoneinfo import available_timezones

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from .. import ai, backup
from ..blueprint import schema
from ..builder import chat
from ..connectors import microsoft
from ..engine.records import Engine
from ..extensions import db
from ..models import STAFF_ROLES, Invite, Membership, User
from ..security import workspace
from ..storage import FileRejected, save

bp = Blueprint("settings", __name__, url_prefix="/w/<int:wid>/settings")
BUSINESS = ["name", "legal_name", "abn", "address", "email", "phone", "website", "bank"]
TIMEZONES = sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/", "US/")))


def _settings(ws):
    return dict(ws.settings or {})


@bp.route("/", methods=["GET", "POST"])
@login_required
def general(wid):
    ws = workspace(wid, "admin")
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:200]
        if name:
            ws.name = name
            if (ws.blueprint or {}).get("name") != name:
                blueprint = {**ws.blueprint, "name": name}
                chat.save_version(ws, blueprint, [f"Renamed the ERP to “{name}”"], "manual", by=current_user.id)
        ws.about = (request.form.get("about") or "").strip()[:2000]
        tz = request.form.get("timezone")
        ws.timezone = tz if tz in TIMEZONES else ws.timezone
        ws.currency = (request.form.get("currency") or "$").strip()[:4] or "$"
        settings = _settings(ws)
        settings["business"] = {k: (request.form.get(f"business_{k}") or "").strip()[:1000] for k in BUSINESS}
        settings["page_size"] = "Letter" if request.form.get("page_size") == "Letter" else "A4"
        base = (request.form.get("base_currency") or "").strip().upper()[:3]
        rates, bad = {}, []
        for line in (request.form.get("rates") or "").splitlines():
            m = re.match(r"^\s*([A-Za-z]{3})\s*[=:,\s]\s*([0-9.,]+)\s*$", line)
            if m and float(m.group(2).replace(",", "")) > 0:
                rates[m.group(1).upper()] = float(m.group(2).replace(",", ""))
            elif line.strip():
                bad.append(line.strip())
        settings["money"] = {"base": base if re.fullmatch(r"[A-Z]{3}", base) else "", "rates": rates}
        holidays = []
        for line in (request.form.get("holidays") or "").splitlines():
            m = re.match(r"^\s*(\d{4}-\d{2}-\d{2})(.*)$", line)
            if m:
                holidays.append((m.group(1) + m.group(2).rstrip())[:80])
            elif line.strip():
                bad.append(line.strip())
        days = sorted({int(d) for d in request.form.getlist("workdays") if d.isdigit() and int(d) < 7}) or [0, 1, 2, 3, 4]
        settings["calendar"] = {"workdays": days, "holidays": holidays[:120],
                                "fy_start": min(max(request.form.get("fy_start", type=int) or 7, 1), 12)}
        if bad:
            flash(f"These weren't understood and were left out: {', '.join(bad[:5])}. Write rates like USD 1.52 and holidays like "
                  "2026-12-25 Christmas Day", "error")
        ws.settings = settings
        db.session.commit()
        flash("Saved.", "success")
        return redirect(url_for(".general", wid=wid))
    return render_template("settings/general.html", ws=ws, timezones=TIMEZONES, business=_settings(ws).get("business", {}), section="general")


@bp.route("/appearance", methods=["GET", "POST"])
@login_required
def appearance(wid):
    ws = workspace(wid, "admin")
    if request.method == "POST":
        theme = {}
        accent = (request.form.get("accent") or "").strip().lower()
        if re.fullmatch(r"#[0-9a-f]{6}", accent):
            theme["accent"] = accent
        if request.form.get("density") in schema.DENSITIES:
            theme["density"] = request.form["density"]
        if theme != (ws.blueprint.get("theme") or {}):
            chat.save_version(ws, {**ws.blueprint, "theme": theme}, ["Changed the look"], "manual", by=current_user.id)
        logo = request.files.get("logo")
        settings = _settings(ws)
        if logo and logo.filename:
            if not (logo.mimetype or "").startswith("image/") or logo.filename.lower().endswith(".svg"):
                flash("The logo needs to be a PNG or JPG image.", "error")
            else:
                try:
                    settings["logo_file_id"] = save(ws.id, logo, logo.filename, source="upload", by=current_user.display).id
                except FileRejected as exc:
                    flash(str(exc), "error")
        if request.form.get("remove_logo"):
            settings.pop("logo_file_id", None)
        ws.settings = settings
        db.session.commit()
        flash("Saved.", "success")
        return redirect(url_for(".appearance", wid=wid))
    return render_template("settings/appearance.html", ws=ws, theme=ws.blueprint.get("theme") or {}, colours=schema.COLOURS, section="appearance")


@bp.route("/members", methods=["GET", "POST"])
@login_required
def members(wid):
    ws = workspace(wid, "admin")
    me = Membership.query.filter_by(workspace_id=ws.id, user_id=current_user.id).first()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "invite":
            email = (request.form.get("email") or "").strip().lower()
            role = request.form.get("role") if request.form.get("role") in ("viewer", "editor", "admin") else "editor"
            if "@" not in email:
                flash("Enter the person's email address.", "error")
            else:
                existing = User.query.filter_by(email=email).first()
                if existing and Membership.query.filter_by(workspace_id=ws.id, user_id=existing.id).first():
                    flash(f"{email} is already a member.", "error")
                else:
                    known = {tm["key"] for tm in schema.teams(ws.blueprint or {})}
                    invite = Invite(workspace_id=ws.id, email=email, role=role, token=secrets.token_urlsafe(24), invited_by=current_user.id,
                                    teams=[k for k in request.form.getlist("teams") if k in known] or None)
                    db.session.add(invite)
                    db.session.commit()
                    flash(f"Invitation for {email} created: copy the link below and send it to them.", "success")
        elif action in ("role", "remove"):
            target = db.session.get(Membership, int(request.form.get("member", 0) or 0))
            if target is None or target.workspace_id != ws.id:
                abort(404)
            if target.role == "owner" or (target.role == "admin" and me.role != "owner"):
                flash("Only the owner can change admins, and the owner can't be changed here.", "error")
            elif action == "remove":
                db.session.delete(target)
                db.session.commit()
                flash("Removed.", "success")
            elif request.form.get("role") in ("viewer", "editor", "admin"):
                target.role = request.form["role"]
                db.session.commit()
                flash("Role changed.", "success")
        elif action == "reset_link":
            target = db.session.get(Membership, int(request.form.get("member", 0) or 0))
            if target is None or target.workspace_id != ws.id or (target.role in ("admin", "owner") and me.role != "owner"):
                abort(404)
            elsewhere = Membership.query.filter(Membership.user_id == target.user_id, Membership.workspace_id != ws.id).count()
            if elsewhere:  # their account opens other ERPs too: only they (or the platform's owner) may reset it
                flash(f"{target.user.email} also uses other apps on this platform, so their password can't be reset from here. "
                      "They can sign in with Microsoft, or ask whoever runs the platform for a reset link.", "error")
                return redirect(url_for(".members", wid=wid))
            from .auth import reset_link
            flash(f"A link for {target.user.email} to choose a new password (it works for 24 hours, once): {reset_link(target.user)}", "success")
        elif action == "teams":  # who is in which of the design's teams
            target = db.session.get(Membership, int(request.form.get("member", 0) or 0))
            if target is None or target.workspace_id != ws.id or target.role == "portal":
                abort(404)
            known = {tm["key"] for tm in schema.teams(ws.blueprint or {})}
            target.teams = [k for k in request.form.getlist("teams") if k in known]
            db.session.commit()
            flash(f"{target.user.display}'s teams saved.", "success")
        elif action == "cover":  # someone away: who covers their approvals and tasks
            from datetime import date as _date
            target = db.session.get(Membership, int(request.form.get("member", 0) or 0))
            if target is None or target.workspace_id != ws.id or target.role == "portal":
                abort(404)
            if request.form.get("clear"):
                target.away_from = target.away_until = target.cover_id = None
            else:
                try:
                    start, end = _date.fromisoformat(request.form.get("from") or ""), _date.fromisoformat(request.form.get("until") or "")
                except ValueError:
                    flash("Give the first and last day they're away.", "error")
                    return redirect(url_for(".members", wid=wid))
                cover = Membership.query.filter_by(workspace_id=ws.id, user_id=request.form.get("cover", type=int)).first()
                if cover is None or cover.role == "portal" or cover.id == target.id or end < start:
                    flash("Choose who covers, and an end on or after the start.", "error")
                    return redirect(url_for(".members", wid=wid))
                target.away_from, target.away_until, target.cover_id = start, end, cover.user_id
            db.session.commit()
            flash("Saved.", "success")
        elif action == "withdraw":
            invite = db.session.get(Invite, int(request.form.get("invite", 0) or 0))
            if invite and invite.workspace_id == ws.id and invite.accepted_at is None:
                db.session.delete(invite)
                db.session.commit()
                flash("Invitation withdrawn.", "success")
        return redirect(url_for(".members", wid=wid))
    people = Membership.query.filter_by(workspace_id=ws.id).order_by(Membership.id).all()
    invites = Invite.query.filter_by(workspace_id=ws.id, accepted_at=None).order_by(Invite.id.desc()).all()
    from flask import current_app
    return render_template("settings/members.html", ws=ws, people=people, invites=invites, me=me, roles=STAFF_ROLES,
                           base=current_app.config["BASE_URL"], section="members", teams=schema.teams(ws.blueprint or {}),
                           names={m.user_id: m.user.display for m in people})


@bp.route("/connections")
@login_required
def connections(wid):
    ws = workspace(wid, "admin")
    from flask import current_app
    return render_template("settings/connections.html", ws=ws, ms=microsoft.status(ws), redirect_uri=microsoft.redirect_uri(),
                           base=current_app.config["BASE_URL"], section="connections")


@bp.route("/connections/microsoft/disconnect", methods=["POST"])
@login_required
def disconnect(wid):
    ws = workspace(wid, "admin")
    microsoft.disconnect(ws)
    flash("Microsoft 365 disconnected. Automations that read or send email will wait until it's connected again.", "success")
    return redirect(url_for(".connections", wid=wid))


@bp.route("/forms", methods=["GET", "POST"])
@login_required
def forms(wid):
    ws = workspace(wid, "admin")
    settings = _settings(ws)
    tokens = dict(settings.get("forms") or {})
    tables = [t for t in ws.blueprint.get("tables", []) if t.get("public_form")]
    for t in tables:
        tokens.setdefault(t["key"], secrets.token_urlsafe(12))
    if request.method == "POST" and request.form.get("renew") in tokens:
        tokens[request.form["renew"]] = secrets.token_urlsafe(12)
        flash("New link made; the old one no longer works.", "success")
    if tokens != settings.get("forms"):
        settings["forms"] = tokens
        ws.settings = settings
        db.session.commit()
    from flask import current_app
    return render_template("settings/forms.html", ws=ws, tables=tables, tokens=tokens, base=current_app.config["BASE_URL"], section="forms")


@bp.route("/data")
@login_required
def data(wid):
    ws = workspace(wid, "admin")
    latest = backup.latest()
    from .. import knowledge
    return render_template("settings/data.html", ws=ws, latest=latest, section="data", ai=ai.describe(), known=knowledge.status(ws))


@bp.route("/data/export.zip")
@login_required
def export_all(wid):
    ws = workspace(wid, "admin")
    engine = Engine(ws, by=current_user.display)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for t in engine.bp.get("tables", []):
            text = io.StringIO()
            writer = csv.writer(text)
            fields = [f for f in t["fields"] if f["type"] not in schema.FILES]
            writer.writerow(["id"] + [f["label"] for f in fields])
            for r in engine.rows(t["key"]):
                writer.writerow([r.id] + [engine.display(f, engine.value(r, f["key"])).replace("—", "") for f in fields])
            z.writestr(f"{t['key']}.csv", "﻿" + text.getvalue())
        import json
        z.writestr("design.json", json.dumps(ws.blueprint, indent=2, ensure_ascii=False))
    out.seek(0)
    return send_file(out, mimetype="application/zip", as_attachment=True, download_name=f"{schema.slug(ws.name) or 'erp'}-export.zip")


@bp.route("/api", methods=["GET", "POST"])
@login_required
def api_keys(wid):
    """Keys for other systems to use the API, and the addresses of webhook automations."""
    from ..engine import automations
    from ..models import ApiKey, utcnow
    from .api import make_key
    ws = workspace(wid, "admin")
    made = None
    if request.method == "POST":
        if request.form.get("revoke"):
            key = db.session.get(ApiKey, int(request.form["revoke"])) if request.form["revoke"].isdigit() else None
            if key is None or key.workspace_id != ws.id:
                abort(404)
            key.revoked_at = utcnow()
            db.session.commit()
            flash(f"Revoked “{key.name}”. Anything using it stops working now.", "success")
            return redirect(url_for(".api_keys", wid=wid))
        role = request.form.get("role") if request.form.get("role") in ("viewer", "editor", "admin") else "editor"
        if not g.member.can(role):
            abort(403)
        key, made = make_key(ws, (request.form.get("name") or "").strip() or "API key", role, current_user.id)
    keys = ApiKey.query.filter_by(workspace_id=ws.id, revoked_at=None).order_by(ApiKey.id.desc()).all()
    hooks = [{"name": a["name"], "url": automations.hook_url(ws.id, a["key"])} for a in (ws.blueprint or {}).get("automations", [])
             if a["trigger"]["type"] == "webhook_received"]
    return render_template("settings/api.html", ws=ws, keys=keys, made=made, hooks=hooks, section="api")


@bp.route("/data/backup", methods=["POST"])
@login_required
def make_backup(wid):
    workspace(wid, "owner")
    path = backup.make()
    flash(f"Backup made: {path.name}. It holds every app on this platform, their files and the key for stored sign-ins.", "success")
    return redirect(url_for(".data", wid=wid))


@bp.route("/data/backup/latest")
@login_required
def download_backup(wid):
    workspace(wid, "owner")
    path = backup.latest()
    if path is None:
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name)
