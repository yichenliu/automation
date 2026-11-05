"""Designing an ERP from a business's own Microsoft 365: its OneDrive files and its mail.

The platform takes stock (spreadsheets' columns and row counts, document names, who emails the business about what, and
how often), then the builder AI proposes an ERP from that: tables for what the spreadsheets track, automations for the
mail that keeps coming, and which spreadsheets to import as the starting data. Nothing is built until the person looks
at the proposal and says so; building it is then one click, and can be undone like any other change.
"""
import json
import re
from collections import Counter, defaultdict

from . import ai, jobs, lang
from .builder import chat
from .connectors import microsoft
from .engine.records import Engine
from .extensions import db
from .models import Workspace

SPREADSHEETS = (".xlsx", ".xlsm", ".xls", ".csv")
DOCUMENTS = (".pdf", ".docx")
MAX_SHEETS = 12
MAX_DOCS = 6


def _pattern(subject):
    """Subjects with their numbers and dates taken out, so "Invoice 1043" and "Invoice 1044" count as one kind."""
    text = re.sub(r"^(re|fw|fwd)\s*:\s*", "", (subject or "").strip(), flags=re.I)
    text = re.sub(r"\d[\d/.,:-]*", "#", text)
    return re.sub(r"\s+", " ", text).strip()[:80] or "(no subject)"


def take_stock(workspace, job=None):
    """What's in the business's OneDrive and mailbox, summarised for the AI (and shown to the person)."""
    from .files import readers
    from .storage import path_of
    found = {"files": 0, "folders": [], "spreadsheets": [], "documents": [], "mail": {}}
    if job:
        jobs.stage(job, "Looking through OneDrive", 5)
    files = microsoft.walk(workspace, depth=4, limit=600)
    found["files"] = len(files)
    folders = Counter(f["path"].rsplit("/", 1)[0] if "/" in f["path"] else "(top level)" for f in files)
    found["folders"] = [{"folder": k, "files": v} for k, v in folders.most_common(25)]
    sheets = sorted((f for f in files if f["name"].lower().endswith(SPREADSHEETS) and not f["name"].startswith("~$")),
                    key=lambda f: f.get("modified") or "", reverse=True)[:MAX_SHEETS]
    for i, item in enumerate(sheets):
        if job:
            jobs.stage(job, f"Reading {item['name']}", 10 + 40 * i / max(len(sheets), 1))
        try:
            stored = microsoft.download(workspace, item["id"], item["name"], by="discovery")
        except microsoft.MicrosoftProblem:
            continue
        content = readers.read(path_of(stored), name=item["name"], max_rows=8)
        if content.error or not content.sheets:
            continue
        found["spreadsheets"].append({"file_id": stored.id, "name": item["name"], "path": item["path"], "modified": item.get("modified"),
                                      "sheets": [{"name": s.name, "columns": s.headers, "rows": s.row_count,
                                                  "examples": [[str(v)[:40] if v is not None else "" for v in r] for r in s.rows[:5]],
                                                  **({"worked_out_by_formulas": s.formulas} if s.formulas else {})}
                                                 for s in content.sheets[:4]]})
    docs = [f for f in files if f["name"].lower().endswith(DOCUMENTS)]
    found["documents"] = [{"name": d["name"], "path": d["path"]} for d in docs[:60]]
    for item in sorted(docs, key=lambda f: f.get("modified") or "", reverse=True)[:MAX_DOCS]:
        try:
            stored = microsoft.download(workspace, item["id"], item["name"], by="discovery")
        except microsoft.MicrosoftProblem:
            continue
        content = readers.read(path_of(stored), name=item["name"], max_chars=1500, max_images=0)
        if content.text:
            found.setdefault("document_samples", []).append({"name": item["name"], "text": content.text[:1500]})
    if job:
        jobs.stage(job, "Reading the last six months of mail", 55)
    messages = microsoft.recent_messages(workspace, days=180, limit=500)
    senders, kinds = Counter(), defaultdict(lambda: {"count": 0, "senders": Counter(), "attachments": 0, "example": ""})
    for m in messages:
        domain = m["from"].split("@")[-1].lower() if "@" in m["from"] else m["from"]
        senders[domain] += 1
        k = kinds[_pattern(m["subject"])]
        k["count"] += 1
        k["senders"][domain] += 1
        k["attachments"] += int(m["has_attachments"])
        k["example"] = k["example"] or m["preview"][:200]
    found["mail"] = {"messages": len(messages), "top_senders": [{"domain": d, "emails": n} for d, n in senders.most_common(20)],
                     "recurring": [{"subject": s, "emails": k["count"], "from": [d for d, _ in k["senders"].most_common(3)],
                                    "with_attachments": k["attachments"], "example": k["example"]}
                                   for s, k in sorted(kinds.items(), key=lambda x: -x[1]["count"]) if k["count"] >= 2][:30]}
    return found


DISCOVERY = """The owner connected their Microsoft 365 so you can design their app from how the business already works.
Below is what the platform found in their OneDrive and mailbox. Design the first version of their app from it, in
patches of the Keel language:
- a list for each thing their spreadsheets track (the spreadsheets' columns as fields, with proper types; links where
  one spreadsheet refers to another's items; stages where a column is a status). A column the spreadsheet works out
  ("worked_out_by_formulas") is a formula, a total or a lookup doing the same, never a typed number;
- an import_rows patch for each spreadsheet worth bringing in: its file id, sheet, and each column heading to the field
  it fills in;
- for mail that clearly keeps coming and makes work (bills from suppliers, enquiries), a rule started by the email
  (when {{"on": "email", …}}) that makes the record with ai_create, and tells whoever should act;
- features wherever one fits (line totals, payments, chasing what's overdue).
Make what the evidence supports, not a generic app. In "reply", say in a few short sentences what you found and what
you'd build from it (name the spreadsheets and the kinds of email), and suggest a next step.

{catalog}"""


def propose(workspace, found, job=None):
    """The app the AI would build from what was found, in the Keel language, checked against the design as it is (not
    saved): (answer, outcome). A patch the platform can't apply is sent back to be corrected once."""
    from .builder import build
    from .lang import describe

    def progress(_thinking, _content):
        if job:
            jobs.stage(job, "Designing your app", 75)
    system = DISCOVERY.format(catalog=build.catalog("all"))
    user = (f"About the business: {workspace.about or workspace.name}\n\nThe app now:\n{describe(workspace.blueprint or {})}\n\n"
            f"What was found:\n" + json.dumps(found, default=str, ensure_ascii=False)[:60000])
    schema_ = build._answer_schema("all")
    answer = build._ask(system, user, schema_, progress, kind="discover")
    patches = answer.get("patches") or []
    out = lang.apply(workspace.blueprint or {}, patches)
    if out.problems and out.refused:
        refused = [patches[n - 1] for n in out.refused if 0 < n <= len(patches)]
        again = build._ask(build.REPAIR.format(catalog=build.catalog("all")),
                           user + "\n\nRefused patches:\n" + "\n".join(json.dumps(p, ensure_ascii=False)[:1500] for p in refused)
                           + "\n\nWhy:\n- " + "\n- ".join(out.problems[:12]), schema_, progress, tries=1, kind="repair")
        kept = [p for n, p in enumerate(patches, start=1) if n not in out.refused]
        patches = kept + (again.get("patches") or [])
        out = lang.apply(workspace.blueprint or {}, patches)
        refused_now = set(out.refused)
        patches = [p for n, p in enumerate(patches, start=1) if n not in refused_now]  # what doesn't fit is left out, said
        out = lang.apply(workspace.blueprint or {}, patches)
    answer["patches"] = patches
    return answer, out


@jobs.handler("discover")
def _discover(job):
    ws = db.session.get(Workspace, job.workspace_id)
    found = (job.result or {}).get("found") or take_stock(ws, job)
    job.result = {"found": found}
    db.session.commit()
    jobs.stage(job, "The AI is designing your ERP from what it found", 70)
    answer, result = propose(ws, found, job)
    if not result.changes:
        raise ValueError("The AI's design didn't fit together: " + ("; ".join(result.problems[:4]) or "it proposed nothing"))
    return {"found": found, "reply": answer.get("reply", ""), "patches": answer.get("patches") or [], "changes": result.changes,
            "imports": [d for d in result.data if d["op"] == "import_file"], "version": ws.version}


def build(workspace, job, user):
    """The person accepted the proposal: apply it (checked again against the design as it is now) and start the imports."""
    from . import importer
    proposal = job.result or {}
    result = lang.apply(workspace.blueprint or {}, proposal.get("patches") or [])
    if result.problems:
        raise ValueError("The design has changed since this was proposed; propose again. (" + "; ".join(result.problems[:3]) + ")")
    number = chat.save_version(workspace, result.blueprint, result.changes, "discovery", by=user.id)
    started = []
    for item in result.data:
        if item["op"] == "import_file":
            started.append(importer.start(workspace, item["file_id"], item["table"], item.get("mapping"), item.get("sheet"), by=user.id))
    proposal["built"] = number
    job.result = dict(proposal)
    db.session.commit()
    return number, started
