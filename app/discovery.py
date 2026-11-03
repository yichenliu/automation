"""Designing an ERP from a business's own Microsoft 365: its OneDrive files and its mail.

The platform takes stock (spreadsheets' columns and row counts, document names, who emails the business about what, and
how often), then the builder AI proposes an ERP from that: tables for what the spreadsheets track, automations for the
mail that keeps coming, and which spreadsheets to import as the starting data. Nothing is built until the person looks
at the proposal and says so; building it is then one click, and can be undone like any other change.
"""
import json
import re
from collections import Counter, defaultdict

from . import ai, jobs
from .builder import chat, prompts
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


DISCOVERY = """The owner connected their Microsoft 365 so you can design their ERP from how the business already works.
Below is what the platform found in their OneDrive and mailbox. Design the first version of their ERP from it:
- a table for each thing their spreadsheets track (use the spreadsheets' columns as fields, with proper types,
  links between tables where one spreadsheet refers to another's items, and statuses as selects); a column the
  spreadsheet works out ("worked_out_by_formulas") becomes a formula, lookup or rollup doing the same, never a typed number;
- for each spreadsheet worth bringing in, an import_file op with its file_id, sheet and a mapping of its column
  headings to your field keys, after the create_table that defines that table;
- automations for mail that clearly keeps coming and creates work (bills or invoices from suppliers → ai_create_record
  with an email_received trigger; enquiries → records to follow up; and notify where someone should act);
- one Overview page.
Make what the evidence supports, not a generic ERP. In "reply", explain in a few short sentences what you found and
what you built from it (name the spreadsheets and the kinds of email), and suggest a next step."""


def propose(workspace, found, job=None):
    engine = Engine(workspace)
    convo = [{"role": "system", "content": prompts.system()},
             {"role": "user", "content": f"About the business: {workspace.about or workspace.name}\nToday: {engine.today.isoformat()}\n\n"
                                         f"The ERP right now:\n{prompts.outline(workspace.blueprint, engine.counts())}"},
             {"role": "assistant", "content": "Understood. What would you like?"},
             {"role": "user", "content": DISCOVERY + "\n\nWhat was found:\n" + json.dumps(found, default=str, ensure_ascii=False)[:60000]}]
    notes = []

    def progress(_thinking, content):
        if job:
            lines = chat._progress_lines(content)
            jobs.stage(job, lines[-1] if lines else "Designing your app", 75)

    answer, result = chat.design(workspace, convo, progress, notes.append)
    return answer, result


@jobs.handler("discover")
def _discover(job):
    ws = db.session.get(Workspace, job.workspace_id)
    found = (job.result or {}).get("found") or take_stock(ws, job)
    job.result = {"found": found}
    db.session.commit()
    jobs.stage(job, "The AI is designing your ERP from what it found", 70)
    answer, result = propose(ws, found, job)
    if result.problems:
        raise ValueError("The AI's design didn't fit together: " + "; ".join(result.problems[:4]))
    return {"found": found, "reply": answer.get("reply", ""), "ops": answer.get("ops") or [], "changes": result.changes,
            "imports": [d for d in result.data if d["op"] == "import_file"], "version": ws.version}


def build(workspace, job, user):
    """The person accepted the proposal: apply it (checked again against the design as it is now) and start the imports."""
    from . import importer
    from .blueprint import ops
    proposal = job.result or {}
    result = ops.apply(workspace.blueprint, proposal.get("ops") or [])
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
