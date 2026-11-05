"""How needs like the owner's are made: worked solutions the builder is shown beside its needs, so a 27-billion-parameter
model starts from something that's known to work rather than from the catalog alone.

Two sources:
- RECIPES: the needs owners ask for most (what must be stopped, approved, chased, reminded, counted), each the owner's
  sentence and the Keel that makes it, written for a sample app (customers, invoices, jobs, quotes, bookings…). The
  tests compile every one and act out what its features check (tests/test_recipes.py), so none is ever wrong.
- solved needs, gathered from builds that something independent agreed were right (an owner's own checks; see
  training.py): DATA_DIR/training/solved.jsonl, one {"need", "patches"} a line (`python -m app.builder.recipes gather`).

`for_needs(needs)` picks the few closest to each need by the words they share; the builder adapts their names to the
app it's building.
"""
import json
import re
import sys
from pathlib import Path

RECIPES = [
    {"need": "Refuses a new booking for a customer who owes on an invoice unpaid for over 30 days", "also": ["No new work for a client who hasn't paid", "Block bookings while the client has overdue invoices"],
     "patches": [{"do": "add_field", "list": "bookings", "field": {"type": "link", "key": "customer", "label": "Customer", "to": "customers"}},
                 {"do": "add_validation", "list": "bookings", "message": "This customer has an invoice unpaid for over 30 days: clear it first",
                  "check": "count(customer.invoices where status != 'Paid' and days_since(due_date) > 30) == 0"}]},
    {"need": "Never books the same staff member for two jobs at overlapping times", "also": ["No double-booking", "A room can't be booked twice at the same time", "Each stylist one appointment at a time"],
     "patches": [{"do": "add_field", "list": "jobs", "field": {"type": "datetime", "key": "starts_at", "label": "Starts at"}},
                 {"do": "add_field", "list": "jobs", "field": {"type": "datetime", "key": "ends_at", "label": "Ends at"}},
                 {"do": "no_overlap", "list": "jobs", "per": ["assigned_to"], "starts": "starts_at", "ends": "ends_at"}]},
    {"need": "At most 8 bookings on the same day", "also": ["No more than 8 people in a class", "A class holds at most 12 children", "Capacity limit per session"],
     "patches": [{"do": "limit", "list": "bookings", "per": ["date"], "max": 8, "only_if": [{"field": "status", "op": "not_equals", "value": "Cancelled"}]}]},
    {"need": "A job can't start until its deposit is paid", "also": ["No work starts until the deposit has cleared", "Hold the order until 50% is paid"],
     "patches": [{"do": "add_field", "list": "jobs", "field": {"type": "yes_no", "key": "deposit_paid", "label": "Deposit paid"}},
                 {"do": "gate", "list": "jobs", "stage": "In progress", "needs": ["deposit_paid"]}]},
    {"need": "A job can't be booked until its quote is accepted",
     "patches": [{"do": "add_field", "list": "jobs", "field": {"type": "link", "key": "quote", "label": "Quote", "to": "quotes"}},
                 {"do": "add_validation", "list": "jobs", "message": "Its quote has to be accepted first", "check": "quote.status == 'Accepted'"}]},
    {"need": "Quotes over $5,000 wait for the owner's OK before they're sent", "also": ["Orders over a set amount need my sign-off", "Anything above the limit waits for my approval", "Big purchases need the manager to approve"],
     "patches": [{"do": "approval", "list": "quotes", "stage": "Sent", "by": "owner", "over": {"field": "total", "amount": 5000}, "tell_approver": True}]},
    {"need": "Chases the customer by email when an invoice is still unpaid 14 days after it's sent, and every 7 days after", "also": ["Payment reminder after 14 days", "Follow up unpaid invoices"],
     "patches": [{"do": "chase", "list": "invoices", "stage": "Sent", "days": 14, "every_days": 7, "email_to": "customer.email",
                  "message": "A friendly reminder: invoice {{number}} is still unpaid."}]},
    {"need": "Marks an invoice overdue 30 days after its due date and tells the owner", "also": ["Flag invoices unpaid after 30 days", "Show what's overdue"],
     "patches": [{"do": "overdue", "list": "invoices", "date_field": "due_date", "from_stages": ["Sent"], "to_stage": "Overdue", "days_after": 30, "tell": "owner"}]},
    {"need": "Makes an invoice from a job when it's done, with the job's lines", "also": ["Turn a finished job into an invoice", "Create the invoice when the work is completed"],
     "patches": [{"do": "convert", "from_list": "jobs", "to_list": "invoices", "when_stage": "Done", "from_lines": "job_lines", "to_lines": "invoice_lines"}]},
    {"need": "Reminds the customer by email the day before their appointment", "also": ["Send a reminder before the booking", "Remind clients 2 days before pickup"],
     "patches": [{"do": "reminder_before", "list": "appointments", "date_field": "starts_on", "days": 1, "email_to": "client.email",
                  "message": "A reminder: your appointment is tomorrow."}]},
    {"need": "Makes a monthly invoice for each active member",
     "patches": [{"do": "recurring", "from_list": "members", "to_list": "invoices", "every": "month",
                  "only_if": [{"field": "status", "op": "equals", "value": "Active"}]}]},
    {"need": "Tells the owner when a job is done",
     "patches": [{"do": "stage_alert", "list": "jobs", "stage": "Done", "tell": "owner"}]},
    {"need": "Emails the invoice as a PDF to the customer when it's sent",
     "patches": [{"do": "send_document", "list": "invoices", "button": "Send invoice", "email_to": "customer.email", "then_stage": "Sent"}]},
    {"need": "A sent invoice can't be changed",
     "patches": [{"do": "lock", "list": "invoices", "stages": ["Sent", "Paid"]}]},
    {"need": "Shows each invoice's balance after its payments, and marks it paid when nothing's left",
     "patches": [{"do": "payments", "list": "invoices", "payments": "payments", "paid_stage": "Paid"}]},
    {"need": "Adds up each invoice's lines with 10% GST",
     "patches": [{"do": "line_totals", "list": "invoices", "lines": "invoice_lines", "tax_percent": 10, "tax_label": "GST"}]},
    {"need": "Only the owner sees what each service costs us",
     "patches": [{"do": "add_field", "list": "services", "field": {"type": "money", "key": "cost", "label": "Cost", "visible_to": "owner"}}]},
    {"need": "Tells the office when a customer's balance owing goes over $2,000",
     "patches": [{"do": "add_field", "list": "customers", "field": {"type": "formula", "key": "owing", "label": "Owing",
                                                                     "formula": "sum(invoices where status != 'Paid', balance)", "shows": "money"}},
                 {"do": "add_rule", "rule": {"key": "owing_over_limit", "name": "Tell the office when a customer owes over $2,000",
                                             "when": {"on": "every", "list": "customers"}, "only_if": ["owing > 2000"],
                                             "steps": [{"do": "notify", "who": "office", "message": "{{name}} owes {{owing}}"}]}}]},
    {"need": "Staff can't mark a job done until its photos are attached",
     "patches": [{"do": "add_field", "list": "jobs", "field": {"type": "image", "key": "photo", "label": "Photo"}},
                 {"do": "gate", "list": "jobs", "stage": "Done", "needs": ["photo"]}]},
    {"need": "A booking can't be confirmed while the customer has any invoice still in draft",
     "patches": [{"do": "add_field", "list": "bookings", "field": {"type": "link", "key": "customer", "label": "Customer", "to": "customers"}},
                 {"do": "gate", "list": "bookings", "stage": "Done", "blocked_if": ["any(customer.invoices where status == 'Draft')"]}]},
]

STOP = set("a an the and or of to for in on at by with from is are be it its it's their they them this that when while "
           "until after before every each any all can can't cannot not no don't doesn't won't must should would will has have "
           "had our we us my me i you your get gets got make makes made so as than over under into up out".split())


# the same idea, said other ways: a word read as the idea's own (the meaning, when the embedding model is there, does
# this better)
SAME = {"overlap": "overlap", "overlapping": "overlap", "double": "overlap", "clash": "overlap", "twice": "overlap",
        "owe": "owe", "owes": "owe", "owing": "owe", "unpaid": "owe", "overdue": "owe", "outstanding": "owe", "debt": "owe",
        "approve": "approve", "approval": "approve", "approved": "approve", "sign": "approve", "signoff": "approve", "ok": "approve",
        "chase": "remind", "chased": "remind", "remind": "remind", "reminder": "remind", "follow": "remind",
        "block": "stop", "blocks": "stop", "stop": "stop", "stops": "stop", "prevent": "stop", "prevents": "stop", "refuse": "stop",
        "refuses": "stop", "until": "stop", "unless": "stop", "capacity": "limit", "most": "limit", "max": "limit", "maximum": "limit",
        "limit": "limit", "client": "customer", "clients": "customer", "customer": "customer", "customers": "customer",
        "booking": "book", "bookings": "book", "booked": "book", "appointment": "book", "appointments": "book", "session": "book"}


def _stem(w):
    for end in ("ing", "ed", "es", "s"):
        if w.endswith(end) and len(w) - len(end) >= 3:
            return w[:-len(end)]
    return w


def _words(text):
    out = set()
    for w in re.findall(r"[a-z0-9$%]+", str(text or "").lower().replace("-", " ")):
        if w in SAME:
            out.add(SAME[w])
            continue
        if w in STOP or len(w) < 3 and not w.isdigit():
            continue
        out.add(_stem(w))
    return out


_VECTORS = {}  # each worked solution's need, as the embedding model reads it (worked out once)


def _meant(needs, pool):
    """[[how close each of pool is to the need]] by meaning, from the embedding model; None when it isn't there."""
    from .. import ai
    try:
        wanted = [r["need"] for r in pool if r["need"] not in _VECTORS]
        if wanted:
            _VECTORS.update(zip(wanted, ai.embed(wanted)))
        asked = ai.embed(list(needs))
    except Exception:  # noqa: BLE001 - no embedding model (or it's down): the words alone
        return None

    def cos(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na, nb = sum(x * x for x in a) ** 0.5, sum(y * y for y in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0
    return [[cos(v, _VECTORS[r["need"]]) for r in pool] for v in asked]


def solved():
    """The needs builds have solved, verified (DATA_DIR/training/solved.jsonl)."""
    from ..core.settings import settings
    try:
        path = Path(settings()["DATA_DIR"]) / "training" / "solved.jsonl"
    except RuntimeError:  # outside the platform (a script, a test): the recipes alone
        return []
    if not path.exists():
        return []
    out = []
    for line in path.open():
        try:
            x = json.loads(line)
        except ValueError:
            continue
        if isinstance(x, dict) and x.get("need") and x.get("patches"):
            out.append(x)
    return out


def for_needs(needs, k=2, most=6):
    """The worked solutions closest to these needs (each a plain sentence): up to k for each, most in all, the
    closest first, none that share too little with any need."""
    pool = RECIPES + solved()
    needs = [n for n in needs if str(n or "").strip()]
    sayings = [(i, said) for i, r in enumerate(pool) for said in [r["need"], *(r.get("also") or [])]]  # each way it's said
    flat = [{"need": said} for _, said in sayings]
    meant = _meant(needs, flat) if needs else None
    chosen, seen = [], set()
    for at, need in enumerate(needs):
        mine = _words(need)
        if meant is not None:  # by meaning: close enough to be the same kind of need
            each, enough = meant[at], 0.6
        else:
            each = [len(mine & _words(said)) / len(mine | _words(said)) if mine else 0 for _, said in sayings]
            enough = 0.2
        best = {}
        for (i, _), score in zip(sayings, each):
            best[i] = max(best.get(i, 0), score)
        scored = sorted(((score, i) for i, score in best.items()), reverse=True)
        for score, i in scored[:k]:
            if score >= enough and i not in seen:
                seen.add(i)
                chosen.append((score, pool[i]))
    return [r for _, r in sorted(chosen, key=lambda x: -x[0])[:most]]


def said(recipes):
    """Worked solutions as the builder is shown them."""
    if not recipes:
        return ""
    return ("How needs like these are made (from another app: use this app's own lists and fields, and leave out what it "
            "doesn't need):\n" + "\n".join(f"- {r['need']}: {json.dumps(r['patches'], ensure_ascii=False)}" for r in recipes))


def gather(folders, out_path):
    """The needs of verified builds (gold and silver turns: something independent agreed they're right) that came out
    right, each with the patches the builder sent for it: written to out_path as solved.jsonl."""
    from . import training
    kept, seen = [], set()
    for turn in training.turns(folders):
        if training.turn_grade(turn) not in ("gold", "silver"):
            continue
        for call in turn.get("calls") or []:
            if call.get("kind") != "v2_behaviour":
                continue
            answer = json.loads(call.get("content") or "{}") if isinstance(call.get("content"), str) else {}
            asked = next((m["content"] for m in call.get("messages") or [] if m["role"] == "user"), "")
            needs = dict(re.findall(r"^(\d+)\. (.+?) \(from:", asked, re.M))
            for w in answer.get("work") or []:
                text = needs.get(str(w.get("need")))
                if text and w.get("patches") and text.lower() not in seen:
                    seen.add(text.lower())
                    kept.append({"need": text, "patches": w["patches"]})
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        for x in kept:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    return len(kept)


if __name__ == "__main__":  # python -m app.builder.recipes gather <run folders> --out <file>
    args = sys.argv[1:]
    if args[:1] == ["gather"] and "--out" in args:
        out = args[args.index("--out") + 1]
        print(f"{gather([a for a in args[1:args.index('--out')]], out)} solved needs kept in {out}")
    else:
        print(__doc__)
