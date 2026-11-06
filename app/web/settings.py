"""Workspace settings: the ERP's name and business details (used on documents), appearance, members and invitations,
connections, public forms, data (backups and exports) and the AI."""
import io
import re
import secrets
import zipfile
import csv
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import ai, audit, backup
from ..blueprint import schema
from ..builder import chat
from ..connectors import microsoft
from ..core.context import current_app
from ..engine.records import Engine
from ..extensions import db
from ..models import STAFF_ROLES, Invite, Membership, User
from ..security import member_of
from ..storage import FileRejected, save
from .http import Form, PageRoute, check_csrf, current_user, flash, form_of, redirect, render, send_file, url_for

router = APIRouter(prefix="/w/{wid:int}/settings", route_class=PageRoute, dependencies=[Depends(check_csrf)])
BUSINESS = ["name", "legal_name", "abn", "address", "email", "phone", "website", "bank"]
TIMEZONES = sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/", "US/")))


def _settings(ws):
    return dict(ws.settings or {})


@router.api_route("/", methods=["GET", "POST"], name="settings.general")
def general(request: Request, wid: int, ws=Depends(member_of("admin")), form: Form = Depends(form_of)):
    if request.method == "POST":
        name = (form.get("name") or "").strip()[:200]
        if name:
            ws.name = name
            if (ws.blueprint or {}).get("name") != name:
                blueprint = {**ws.blueprint, "name": name}
                chat.save_version(ws, blueprint, [f"Renamed the ERP to “{name}”"], "manual", by=current_user(request).id)
        ws.about = (form.get("about") or "").strip()[:2000]
        tz = form.get("timezone")
        ws.timezone = tz if tz in TIMEZONES else ws.timezone
        ws.currency = (form.get("currency") or "$").strip()[:4] or "$"
        settings = _settings(ws)
        settings["business"] = {k: (form.get(f"business_{k}") or "").strip()[:1000] for k in BUSINESS}
        settings["page_size"] = "Letter" if form.get("page_size") == "Letter" else "A4"
        base = (form.get("base_currency") or "").strip().upper()[:3]
        rates, bad = {}, []
        for line in (form.get("rates") or "").splitlines():
            m = re.match(r"^\s*([A-Za-z]{3})\s*[=:,\s]\s*([0-9.,]+)\s*$", line)
            if m and float(m.group(2).replace(",", "")) > 0:
                rates[m.group(1).upper()] = float(m.group(2).replace(",", ""))
            elif line.strip():
                bad.append(line.strip())
        settings["money"] = {"base": base if re.fullmatch(r"[A-Z]{3}", base) else "", "rates": rates}
        holidays = []
        for line in (form.get("holidays") or "").splitlines():
            m = re.match(r"^\s*(\d{4}-\d{2}-\d{2})(.*)$", line)
            if m:
                holidays.append((m.group(1) + m.group(2).rstrip())[:80])
            elif line.strip():
                bad.append(line.strip())
        days = sorted({int(d) for d in form.getlist("workdays") if d.isdigit() and int(d) < 7}) or [0, 1, 2, 3, 4]
        settings["calendar"] = {"workdays": days, "holidays": holidays[:120],
                                "fy_start": min(max(form.get("fy_start", type=int) or 7, 1), 12)}
        if bad:
            flash(request, f"These weren't understood and were left out: {', '.join(bad[:5])}. Write rates like USD 1.52 and holidays like "
                  "2026-12-25 Christmas Day", "error")
        ws.settings = settings
        db.session.commit()
        audit.log(ws.id, "settings", "business details")
        flash(request, "Saved.", "success")
        return redirect(url_for("settings.general", wid=wid))
    return render(request, "settings/general.html", ws=ws, e=Engine(ws), timezones=TIMEZONES, business=_settings(ws).get("business", {}),
                  section="general")


@router.api_route("/appearance", methods=["GET", "POST"], name="settings.appearance")
def appearance(request: Request, wid: int, ws=Depends(member_of("admin")), form: Form = Depends(form_of)):
    if request.method == "POST":
        user = current_user(request)
        theme = {}
        accent = (form.get("accent") or "").strip().lower()
        if re.fullmatch(r"#[0-9a-f]{6}", accent):
            theme["accent"] = accent
        if form.get("density") in schema.DENSITIES:
            theme["density"] = form["density"]
        if theme != (ws.blueprint.get("theme") or {}):
            chat.save_version(ws, {**ws.blueprint, "theme": theme}, ["Changed the look"], "manual", by=user.id)
        logo = form.files.get("logo")
        settings = _settings(ws)
        if logo and logo.filename:
            if not (logo.mimetype or "").startswith("image/") or logo.filename.lower().endswith(".svg"):
                flash(request, "The logo needs to be a PNG or JPG image.", "error")
            else:
                try:
                    settings["logo_file_id"] = save(ws.id, logo, logo.filename, source="upload", by=user.display).id
                except FileRejected as exc:
                    flash(request, str(exc), "error")
        if form.get("remove_logo"):
            settings.pop("logo_file_id", None)
        ws.settings = settings
        db.session.commit()
        flash(request, "Saved.", "success")
        return redirect(url_for("settings.appearance", wid=wid))
    return render(request, "settings/appearance.html", ws=ws, theme=ws.blueprint.get("theme") or {}, colours=schema.COLOURS,
                  section="appearance")


@router.api_route("/members", methods=["GET", "POST"], name="settings.members")
def members(request: Request, wid: int, ws=Depends(member_of("admin")), form: Form = Depends(form_of)):
    user = current_user(request)
    me = Membership.query.filter_by(workspace_id=ws.id, user_id=user.id).first()
    if request.method == "POST":
        action = form.get("action")
        if action == "invite":
            email = (form.get("email") or "").strip().lower()
            role = form.get("role") if form.get("role") in ("viewer", "editor", "admin") else "editor"
            if "@" not in email:
                flash(request, "Enter the person's email address.", "error")
            else:
                existing = User.query.filter_by(email=email).first()
                if existing and Membership.query.filter_by(workspace_id=ws.id, user_id=existing.id).first():
                    flash(request, f"{email} is already a member.", "error")
                else:
                    known = {tm["key"] for tm in schema.teams(ws.blueprint or {})}
                    invite = Invite(workspace_id=ws.id, email=email, role=role, token=secrets.token_urlsafe(24), invited_by=user.id,
                                    teams=[k for k in form.getlist("teams") if k in known] or None)
                    db.session.add(invite)
                    db.session.commit()
                    audit.log(ws.id, "invited", f"{email} as {role}" + (f", in {', '.join(invite.teams)}" if invite.teams else ""))
                    flash(request, f"Invitation for {email} created: copy the link below and send it to them.", "success")
        elif action in ("role", "remove"):
            target = db.session.get(Membership, int(form.get("member", 0) or 0))
            if target is None or target.workspace_id != ws.id:
                raise HTTPException(404)
            if target.role == "owner" or (target.role == "admin" and me.role != "owner"):
                flash(request, "Only the owner can change admins, and the owner can't be changed here.", "error")
            elif action == "remove":
                who = target.user.email
                db.session.delete(target)
                db.session.commit()
                audit.log(ws.id, "removed", who)
                flash(request, "Removed.", "success")
            elif form.get("role") in ("viewer", "editor", "admin"):
                was, target.role = target.role, form["role"]
                db.session.commit()
                audit.log(ws.id, "role_changed", f"{target.user.email}: {was} → {target.role}")
                flash(request, "Role changed.", "success")
        elif action == "reset_link":
            target = db.session.get(Membership, int(form.get("member", 0) or 0))
            if target is None or target.workspace_id != ws.id or (target.role in ("admin", "owner") and me.role != "owner"):
                raise HTTPException(404)
            elsewhere = Membership.query.filter(Membership.user_id == target.user_id, Membership.workspace_id != ws.id).count()
            if elsewhere:  # their account opens other ERPs too: only they (or the platform's owner) may reset it
                flash(request, f"{target.user.email} also uses other apps on this platform, so their password can't be reset from here. "
                      "They can sign in with Microsoft, or ask whoever runs the platform for a reset link.", "error")
                return redirect(url_for("settings.members", wid=wid))
            from .auth import reset_link
            audit.log(ws.id, "password_reset", f"a reset link made for {target.user.email}")
            flash(request, f"A link for {target.user.email} to choose a new password (it works for 24 hours, once): {reset_link(target.user)}",
                  "success")
        elif action == "teams":  # who is in which of the design's teams
            target = db.session.get(Membership, int(form.get("member", 0) or 0))
            if target is None or target.workspace_id != ws.id or target.role == "portal":
                raise HTTPException(404)
            known = {tm["key"] for tm in schema.teams(ws.blueprint or {})}
            target.teams = [k for k in form.getlist("teams") if k in known]
            db.session.commit()
            audit.log(ws.id, "teams_changed", f"{target.user.email}: {', '.join(target.teams) or 'no team'}")
            flash(request, f"{target.user.display}'s teams saved.", "success")
        elif action == "cover":  # someone away: who covers their approvals and tasks
            from datetime import date as _date
            target = db.session.get(Membership, int(form.get("member", 0) or 0))
            if target is None or target.workspace_id != ws.id or target.role == "portal":
                raise HTTPException(404)
            if form.get("clear"):
                target.away_from = target.away_until = target.cover_id = None
            else:
                try:
                    start, end = _date.fromisoformat(form.get("from") or ""), _date.fromisoformat(form.get("until") or "")
                except ValueError:
                    flash(request, "Give the first and last day they're away.", "error")
                    return redirect(url_for("settings.members", wid=wid))
                cover = Membership.query.filter_by(workspace_id=ws.id, user_id=form.get("cover", type=int)).first()
                if cover is None or cover.role == "portal" or cover.id == target.id or end < start:
                    flash(request, "Choose who covers, and an end on or after the start.", "error")
                    return redirect(url_for("settings.members", wid=wid))
                target.away_from, target.away_until, target.cover_id = start, end, cover.user_id
            db.session.commit()
            flash(request, "Saved.", "success")
        elif action == "withdraw":
            invite = db.session.get(Invite, int(form.get("invite", 0) or 0))
            if invite and invite.workspace_id == ws.id and invite.accepted_at is None:
                db.session.delete(invite)
                db.session.commit()
                flash(request, "Invitation withdrawn.", "success")
        return redirect(url_for("settings.members", wid=wid))
    people = Membership.query.filter_by(workspace_id=ws.id).order_by(Membership.id).all()
    invites = Invite.query.filter_by(workspace_id=ws.id, accepted_at=None).order_by(Invite.id.desc()).all()
    return render(request, "settings/members.html", ws=ws, people=people, invites=invites, me=me, roles=STAFF_ROLES,
                  base=current_app.config["BASE_URL"], section="members", teams=schema.teams(ws.blueprint or {}),
                  names={m.user_id: m.user.display for m in people})


@router.api_route("/connections", methods=["GET"], name="settings.connections")
def connections(request: Request, wid: int, ws=Depends(member_of("admin"))):
    from ..connectors import apps
    connected = {a.key: apps.connection(ws, a.key) for a in apps.all_apps()}
    syncs = {}
    for sync in (ws.blueprint or {}).get("syncs") or []:
        conn = connected.get(sync["app"])
        syncs.setdefault(sync["app"], []).append({**sync, "done": ((conn.state or {}).get("syncs") or {}).get(sync["key"], {}) if conn else {}})
    return render(request, "settings/connections.html", ws=ws, ms=microsoft.status(ws), redirect_uri=microsoft.redirect_uri(),
                  base=current_app.config["BASE_URL"], section="connections", apps=apps.all_apps(), connected=connected, syncs=syncs)


@router.api_route("/connections/microsoft/disconnect", methods=["POST"], name="settings.disconnect")
def disconnect(request: Request, wid: int, ws=Depends(member_of("admin"))):
    microsoft.disconnect(ws)
    audit.log(ws.id, "connected", "Microsoft 365 disconnected")
    flash(request, "Microsoft 365 disconnected. Automations that read or send email will wait until it's connected again.", "success")
    return redirect(url_for("settings.connections", wid=wid))


@router.api_route("/printers", methods=["GET", "POST"], name="settings.printers")
def printers(request: Request, wid: int, ws=Depends(member_of("admin")), form: Form = Depends(form_of)):
    """Which printer labels and documents go to: one of this Mac's printers, or a label printer on the network."""
    from .. import printing
    from ..documents import labels
    names, default = printing.mac_printers()
    if request.method == "POST":
        settings = _settings(ws)
        chosen = dict(settings.get("printers") or {})
        for role in printing.ROLES:
            pick = form.get(role, "")
            if pick.startswith("mac:") and pick[4:] in names:
                chosen[role] = {"kind": "mac", "name": pick[4:]}
            elif pick == "zpl" and role == "labels":
                address = (form.get("zpl_address") or "").strip()
                if not re.fullmatch(r"[\w.-]+(:\d{2,5})?", address):
                    flash(request, "Write the label printer's address as its IP address (192.168.1.50), with :port if it isn't 9100.", "error")
                    return redirect(url_for("settings.printers", wid=wid))
                chosen[role] = {"kind": "zpl", "address": address, "dpi": form.get("dpi") if form.get("dpi") in printing.DPI else "203"}
            else:
                chosen.pop(role, None)
            if role == "labels" and role in chosen:
                chosen[role]["size"] = labels.size_of(form.get("size"))
        settings["printers"] = chosen
        ws.settings = settings
        db.session.commit()
        audit.log(ws.id, "printers", "; ".join(f"{r}: {printing.describe(c)}" for r, c in chosen.items()) or "none")
        if form.get("test"):
            try:
                if form["test"] == "labels":
                    flash(request, printing.print_labels(ws, [{"title": "Test label", "lines": [ws.name, "If you can read this, it works"],
                                                               "code": "TEST-0001", "kind": "barcode"}]), "success")
                else:
                    from ..documents import pdf
                    flash(request, printing.print_pdf(ws, pdf.render({"title": "Test page", "number": "TEST", "business": {"name": ws.name},
                                                                       "notes": "If you can read this, documents print here."})), "success")
            except printing.PrintProblem as exc:
                flash(request, str(exc), "error")
        else:
            flash(request, "Saved.", "success")
        return redirect(url_for("settings.printers", wid=wid))
    return render(request, "settings/printers.html", ws=ws, names=names, default=default,
                  chosen={r: printing.chosen(ws, r) for r in printing.ROLES}, roles=printing.ROLES,
                  sizes={k: v[0] for k, v in labels.SIZES.items() if v[3] is None}, section="printers")


@router.api_route("/health", methods=["GET"], name="settings.health_page")
def health_page(request: Request, wid: int, ws=Depends(member_of("admin"))):
    """How the app is running, each part with what to do about it."""
    from .. import health
    return render(request, "settings/health.html", ws=ws, parts=health.report(ws), section="health")


@router.api_route("/audit", methods=["GET"], name="settings.audit_log")
def audit_log(request: Request, wid: int, ws=Depends(member_of("admin"))):
    """Who did what that matters, newest first: this app's entries, and its people's sign-ins. Filtered by what and who;
    as CSV for an auditor. Whether the chain of entries is whole is checked every time it's opened."""
    from ..models import AuditEvent
    args = request.query_params
    people = {m.user_id for m in Membership.query.filter_by(workspace_id=ws.id)}
    query = AuditEvent.query.filter((AuditEvent.workspace_id == ws.id) | ((AuditEvent.workspace_id.is_(None)) & (AuditEvent.user_id.in_(people))))
    what, who = args.get("what", ""), args.get("who", "")
    if what in audit.WORDS:
        query = query.filter(AuditEvent.action == what)
    if who.isdigit():
        query = query.filter(AuditEvent.user_id == int(who))
    entries = query.order_by(AuditEvent.id.desc()).limit(5000 if args.get("csv") else 300).all()
    if args.get("csv"):
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["When (UTC)", "Who", "What", "Detail", "From", "Fingerprint"])
        for e in entries:
            writer.writerow([e.at.isoformat(timespec="seconds"), e.by, audit.WORDS.get(e.action, e.action), e.detail, e.ip or "", e.fingerprint])
        audit.log(ws.id, "exported", "the audit log as CSV")
        return send_file(request, io.BytesIO(("\ufeff" + out.getvalue()).encode()), mimetype="text/csv", as_attachment=True,
                         download_name=f"{schema.slug(ws.name) or 'app'}-audit-log.csv")
    whole, at = audit.verify(ws.id)
    names = {m.user_id: m.user.display for m in Membership.query.filter_by(workspace_id=ws.id)}
    return render(request, "settings/audit.html", ws=ws, entries=entries, words=audit.WORDS, what=what, who=who, names=names,
                  whole=whole, broken=None if whole else at, count=at if whole else None, section="audit")


@router.api_route("/forms", methods=["GET", "POST"], name="settings.forms")
def forms(request: Request, wid: int, ws=Depends(member_of("admin")), form: Form = Depends(form_of)):
    settings = _settings(ws)
    tokens = dict(settings.get("forms") or {})
    tables = [t for t in ws.blueprint.get("tables", []) if t.get("public_form")]
    for t in tables:
        tokens.setdefault(t["key"], secrets.token_urlsafe(12))
    if request.method == "POST" and form.get("renew") in tokens:
        tokens[form["renew"]] = secrets.token_urlsafe(12)
        flash(request, "New link made; the old one no longer works.", "success")
    if tokens != settings.get("forms"):
        settings["forms"] = tokens
        ws.settings = settings
        db.session.commit()
    return render(request, "settings/forms.html", ws=ws, tables=tables, tokens=tokens, base=current_app.config["BASE_URL"], section="forms")


@router.api_route("/data", methods=["GET"], name="settings.data")
def data(request: Request, wid: int, ws=Depends(member_of("admin"))):
    """Data and backups: everything as spreadsheets, the backups, and the database it's all kept in."""
    from sqlalchemy import text as sql
    try:
        row = db.session.execute(sql("SELECT current_setting('server_version'), pg_size_pretty(pg_database_size(current_database())), "
                                     "(SELECT extversion FROM pg_extension WHERE extname = 'vector')")).one()
        database = {"version": row[0].split()[0], "size": row[1], "vector": row[2]}
    except Exception:  # noqa: BLE001 - the page shows without it
        database = None
    return render(request, "settings/data.html", ws=ws, latest=backup.latest(), section="data", database=database)


@router.api_route("/ai", methods=["GET"], name="settings.ai_settings")
def ai_settings(request: Request, wid: int, ws=Depends(member_of("admin"))):
    """The AI on this computer: which model answers, which finds things by meaning, and what it can look through."""
    from .. import knowledge
    from .erp import ai_count
    return render(request, "settings/ai.html", ws=ws, section="ai_settings", ai=ai.describe(), known=knowledge.status(ws),
                  embed=current_app.config.get("OLLAMA_EMBED_MODEL") or None, helpers=ai_count(ws))


@router.api_route("/security", methods=["GET", "POST"], name="settings.security")
def security(request: Request, wid: int, ws=Depends(member_of("admin")), form: Form = Depends(form_of)):
    """Security: signing people out of shared computers, single sign-on, two-step sign-in, and the audit log."""
    from ..connectors import oidc
    from ..security import IDLE_CHOICES
    if request.method == "POST":
        minutes = form.get("idle_minutes", type=int)
        ws.settings = {**(ws.settings or {}), "idle_minutes": minutes if minutes in IDLE_CHOICES else 0}
        db.session.commit()
        audit.log(ws.id, "settings", f"sign people out after {minutes or 0} idle minutes")
        flash(request, "Saved.", "success")
        return redirect(url_for("settings.security", wid=wid))
    people = [u for m, u in db.session.query(Membership, User).filter(Membership.workspace_id == ws.id, Membership.user_id == User.id,
                                                                       Membership.role.in_(STAFF_ROLES))]
    return render(request, "settings/security.html", ws=ws, section="security", sso=oidc.configured(),
                  sso_name=oidc.name() if oidc.configured() else None, people=len(people), two_step=sum(1 for u in people if u.totp_secret),
                  sso_linked=sum(1 for u in people if u.sso_identity))


@router.api_route("/data/export.zip", methods=["GET"], name="settings.export_all")
def export_all(request: Request, wid: int, ws=Depends(member_of("admin"))):
    engine = Engine(ws, by=current_user(request).display)
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
    audit.log(ws.id, "exported", "everything (every list and the design) as a zip")
    return send_file(request, out, mimetype="application/zip", as_attachment=True, download_name=f"{schema.slug(ws.name) or 'erp'}-export.zip")


@router.api_route("/api", methods=["GET", "POST"], name="settings.api_keys")
def api_keys(request: Request, wid: int, ws=Depends(member_of("admin")), form: Form = Depends(form_of)):
    """Keys for other systems to use the API, and the addresses of webhook automations."""
    from ..engine import automations
    from ..models import ApiKey, utcnow
    from .api import make_key
    made = None
    if request.method == "POST":
        if form.get("revoke"):
            key = db.session.get(ApiKey, int(form["revoke"])) if form["revoke"].isdigit() else None
            if key is None or key.workspace_id != ws.id:
                raise HTTPException(404)
            key.revoked_at = utcnow()
            db.session.commit()
            audit.log(ws.id, "api_key", f"revoked “{key.name}”")
            flash(request, f"Revoked “{key.name}”. Anything using it stops working now.", "success")
            return redirect(url_for("settings.api_keys", wid=wid))
        role = form.get("role") if form.get("role") in ("viewer", "editor", "admin") else "editor"
        if not request.state.member.can(role):
            raise HTTPException(403)
        key, made = make_key(ws, (form.get("name") or "").strip() or "API key", role, current_user(request).id)
        audit.log(ws.id, "api_key", f"made “{key.name}” ({role})")
    keys = ApiKey.query.filter_by(workspace_id=ws.id, revoked_at=None).order_by(ApiKey.id.desc()).all()
    hooks = [{"name": a["name"], "url": automations.hook_url(ws.id, a["key"])} for a in (ws.blueprint or {}).get("automations", [])
             if a["trigger"]["type"] == "webhook_received"]
    return render(request, "settings/api.html", ws=ws, keys=keys, made=made, hooks=hooks, section="api", base=current_app.config["BASE_URL"])


@router.api_route("/data/backup", methods=["POST"], name="settings.make_backup")
def make_backup(request: Request, wid: int, ws=Depends(member_of("owner"))):
    path = backup.make()
    audit.log(wid, "backup", f"made {path.name}")
    flash(request, f"Backup made: {path.name}. It holds every app on this platform, their files and the key for stored sign-ins.", "success")
    return redirect(url_for("settings.data", wid=wid))


@router.api_route("/data/backup/latest", methods=["GET"], name="settings.download_backup")
def download_backup(request: Request, wid: int, ws=Depends(member_of("owner"))):
    path = backup.latest()
    if path is None:
        raise HTTPException(404)
    audit.log(wid, "backup", f"downloaded {path.name}")
    return send_file(request, path, as_attachment=True, download_name=path.name)
