"""What the AI is told outside the builder's own turn (the builder's instructions are its language's catalog, in
build.py): how to write an owner's check as a worked example, how to look at one again when it fails, and the app as
the in-app assistant sees it (outline).
"""
import json

from ..blueprint import schema


_STRING = {"type": "string"}

# Worked examples: what the owner asked for, acted out in a copy of the app and compared with what it then shows.
_EXAMPLE = {"type": "object", "required": ["title", "steps", "expect"], "properties": {
    "title": _STRING,
    "steps": {"type": "array", "items": {"type": "object", "required": ["do", "as"], "properties": {  # "as" always: the model skips it otherwise
        "do": {"type": "string", "enum": ["add", "change", "move", "press", "approve", "reject", "wait", "run"]},
        "as": _STRING, "table": _STRING, "record": _STRING, "values": {"type": "object"}, "to": _STRING, "button": _STRING,
        "rule": _STRING, "days": {"type": "integer"}, "refused": {"type": "boolean"}, "comment": _STRING, "by": _STRING}}},
    "expect": {"type": "array", "items": {"type": "object", "properties": {
        "record": _STRING, "field": _STRING, "equals": {}, "table": _STRING, "count": {"type": "integer"}}}}}}

EXAMPLES_SCHEMA = {"type": "object", "required": ["examples"], "properties": {"examples": {"type": "array", "items": _EXAMPLE}}}

ONE_EXAMPLE_SCHEMA = {"type": "object", "required": ["examples"], "properties": {"examples": {"type": "array", "items": _EXAMPLE,
                                                                                              "minItems": 1, "maxItems": 1}}}

CHECK_AGAIN_SCHEMA = {"type": "object", "required": ["verdict", "missing", "examples"], "properties": {
    "verdict": {"type": "string", "enum": ["example", "app"]}, "missing": _STRING,
    "examples": {"type": "array", "items": _EXAMPLE, "maxItems": 1}}}

EXAMPLES = """You check business apps before they're handed over. The owner asked for something and the app below is what
was built. Write worked examples that try out what they asked for, the way their people would use it on an ordinary
day. The platform acts out each example in a copy of the app and compares what the app then shows with what you expect.

Each example has a "title" in the owner's words ("A wedding quote with 15% off"), "steps" in order, and "expect".
Steps ("do"):
- "add": a record of "table", with "values" ({{field key: value}}), named by "as" (a short name like "c1" or "q1").
  Every step has "as": an added record's own name, or "" on other steps. A link field's value is the "as" name of a
  record added earlier in the same example, spelled exactly the same. Line items are added on their own, each linked
  to its parent by the parent's "as" name.
- "change": change "record" (an "as" name) to "values".
- "move": move "record" to the stage "to", as its button would.
- "press": press the button automation "button" (its key) on "record".
- "approve" / "reject": answer what's waiting for someone's OK on "record", with "comment": what they'd write (why
  they said no). Each answers one approval: a chain (the team lead, then finance over $2,000) needs an approve for each.
  An AI step whose work a person checks first ("review": "always") waits for that check: "approve" keeps its draft.
- AI steps aren't asked in a try-out: one answers with what the example expects of the detail it fills (a price check
  "Pass"), so say it, and what should follow; "equals": "non-empty" when any value will do (a drafted letter).
- "wait": let "days" pass (for reminders, chasing, anything overdue or so many days old). Each day of it, the app's
  rules that run on a schedule run as they would, so what's due by then has happened; nothing more is needed. After a
  wait, today is that many days later, and so is "this week". Write dates as "today" or "today+N" (in values and in
  what's expected), counted on the example's own calendar: after a 14-day wait, a rule that stamps today's date is
  expected to give "today", and one due 3 days after that "today+3".
Add "refused": true to a step the app should NOT allow (a move whose condition isn't met, approving your own request).
A change to a locked record (a sent invoice) is refused for staff; the owner and admins can still correct it, so a
refused change to a locked record is tried as an ordinary staff member.
Steps are done by the owner, who may do nearly anything. "by" has someone else do one: "staff" (an ordinary staff
member), a team's key, "admin", or a person's name. What only some may do (only the owner approves hires over six
months, only the kitchen sees orders) is shown by having someone else try it: "by": "staff" with "refused": true.
"expect": {{"record": an "as" name, "field": a field key (or a path through links, "event.status"), "equals": the value}}
or {{"table": a table key, "count": how many records it should then have}}. What it expects is looked at once, after the
last step: one value for each field, where it ends up (where it is part way is its own example that stops there).

Rules:
- Use only the tables, fields, stages, buttons and automations of the app below, by their keys. Never set a field
  that's worked out (formula, rollup, lookup, autonumber): expect it instead.
- Fill in every required field of what you add. Each example stands alone: it adds every record it needs.
- Work out every expected number yourself from what the owner said: 15% off $1,890 is $283.50 off, and GST at 10% on
  the $1,606.50 left is $160.65. Money to the cent; a percentage as people write it ("15").
- What the owner asked for decides what you expect, not what was built: if the app should do it, expect it.
- Steps are only what people do. After the move or button that sets something off, never add or change by hand what
  the app should then do itself (the stock movements a build start takes out, the invoice a delivery makes, a status
  a rule sets): expect it instead, so the example fails if the app doesn't do it. Quantities above 1 where they
  multiply anything (2 tables need twice the parts).
- Try what can go wrong: sums, discounts and tax, which way a record goes (over or under a limit), what a rule does
  when it fires, what's refused.
- {count}
{when_none}"""

NONE_IF_LOOKS = 'If this change only touched looks, wording or layout, send "examples": [].'

CHECK_AGAIN = """You wrote the worked example below for a check the owner asked for, in their words: “{said}”.
Acted out in a copy of their app, it {how}:
{what}

Before the owner is told their app doesn't do it, look again at whose mistake it is.
- A step that should have been refused but was allowed, done by the owner (no "by"): when the owner's words say only
  they may do it (my signature, my OK, I approve), the app is right that the owner may; send the example with "by":
  "staff" on that step (keeping "refused": true), "verdict": "example".
- The example's: a number or date worked out wrong (after a "wait", the records' own dates are that many days
  older too), a value that isn't one of a field's choices, a name the app has under another key, a required field
  left out, a record added at a later stage than new ones start at (add it, then move it), a field the step must fill
  first, or a step the owner is allowed to do when it was meant to be refused (the owner may do nearly anything: have
  "by": "staff" try it). Then send "verdict": "example" and the example corrected in "examples", with what the owner
  asked for still what it expects.
- Before blaming a number on either side, work each expected number out again from the values the example typed, step
  by step, the way the app shows its working above (100 kg at $10 with 10% GST is $1,100, not $110). If yours differs
  from what the example expects, the example's sum is wrong: correct it.
- The app's: it has no such list, field, rule, limit or step, or does something other than what the owner asked. Then
  send "verdict": "app", "examples": [] and, in "missing", what the app doesn't do yet, in the owner's words, one
  short sentence ("nothing stops a sixth booking on a Saturday"). A step that should have been stopped but wasn't,
  where nothing in the app could stop it (no approval, lock, rule or limit on it), is this: say what's missing.

The example:
{example}"""

ALWAYS_ONE = ("Always write it, even when the app below doesn't do it yet: the example then shows that it doesn't, and the "
              "builder is asked to make it do it.")

def _field_line(f):
    bits = [f"{f['key']}: {f['type']}"]
    if f.get("required"):
        bits.append("required")
    if f.get("unique"):
        bits.append("unique")
    if f["type"] in ("select", "multi_select"):
        bits.append("[" + ", ".join(f["options"]) + "]")
    if f["type"] == "link":
        bits.append(f"→ {f['table']}")
    if f["type"] == "lookup" and "where" in f:
        bits.append(f"= {f['field']} of the {f['table']} found by conditions" + (f", by {f['sort']['field']}" if f.get("sort") else ""))
    elif f["type"] == "lookup":
        bits.append(f"= {f['link']}.{f['field']}")
    if f["type"] == "formula":
        bits.append(f"= {f['expression']} ({f.get('format', 'number')})")
    if f["type"] == "rollup":
        bits.append(f"= {f['aggregate']} of {f['table']}.{f.get('field', '*')} " + (f"via {f['via']}" if f["via"] else "across the table")
                    + (" (filtered)" if f.get("filters") else ""))
    if f["type"] == "autonumber":
        bits.append(f"prefix {f.get('prefix', '')}")
    if f.get("default") not in (None, ""):
        bits.append(f"default {f['default']}")
    if f.get("ai"):
        bits.append("AI-filled")
    if f.get("show_if") or f.get("hide_if"):
        bits.append("conditional")
    if f.get("readonly_if"):
        bits.append("read-only when conditions hold")
    if f.get("options_by"):
        bits.append(f"choices follow {f['options_by']['field']}")
    if f.get("access"):
        bits.append(f"seen by {f['access']}+" if f["access"] in schema.ROLE_NAMES else f"seen by the {f['access']} team")
    if f.get("sensitive"):
        bits.append("sensitive (encrypted, masked)")
    if f.get("multiple"):
        bits.append("many")
    if f.get("currency") or f.get("currency_field"):
        bits.append(f"in {f.get('currency') or 'the currency in ' + f['currency_field']}")
    if f.get("decimals") is not None or f.get("unit"):
        bits.append(" ".join(x for x in (f"{f['decimals']} decimals" if f.get("decimals") is not None else "", f"unit {f['unit']}" if f.get("unit") else "") if x))
    if f.get("option_colors"):
        bits.append("colours " + json.dumps(f["option_colors"], ensure_ascii=False))
    if f.get("before") or f.get("after"):
        bits.append(f"shown as {f.get('before', '')}…{f.get('after', '')}")
    if f.get("yes_label") or f.get("no_label"):
        bits.append(f"yes/no shown as {f.get('yes_label', 'Yes')}/{f.get('no_label', 'No')}")
    if f.get("template"):
        bits.append(f"link made from {f['template']}")
    label = f" \"{f['label']}\"" if f["label"].lower() != f["key"].replace("_", " ") else ""
    return " ".join(bits) + label

def outline(bp, counts, extra=""):
    """The current design, compactly, for the AI to read."""
    if not bp.get("tables") and not bp.get("automations"):
        return f"The ERP “{bp.get('name')}” is empty: nothing has been built yet." + (f"\n{extra}" if extra else "")
    lines = [f"ERP name: {bp.get('name')}" + (f"; theme {json.dumps(bp['theme'])}" if bp.get("theme") else ""),
             "Modules: " + (", ".join(f"{m['key']} \"{m['label']}\"" for m in bp.get("modules", [])) or "none")]
    for t in bp.get("tables", []):
        flags = "; line items (embedded)" if t.get("embedded") else ""
        if t.get("combines"):
            flags += "; put together from " + ", ".join(f"{a['table']} ({json.dumps(a['values'])}{', where ' + json.dumps(a['where']) if a.get('where') else ''})"
                                                         for a in t["combines"])
        lines.append(f"\nTable {t['key']} \"{t['label']}\" (one {t['singular']}; module {t.get('module') or '-'}; icon {t.get('icon')}; "
                     f"named by {t.get('title_field')}; {counts.get(t['key'], 0)} records{flags})")
        for f in t["fields"]:
            lines.append(f"  - {_field_line(f)}")
        views = [f"{v['key']} ({v['type']}{' by ' + v['group_by'] if v.get('group_by') else ''}{', filtered' if v.get('filters') else ''})" for v in t.get("views", [])]
        if views:
            lines.append("  views: " + ", ".join(views))
        if t.get("sections"):
            lines.append("  sections: " + "; ".join(f"{s['label']}: {', '.join(s['fields'])}" + (" (conditional)" if s.get("show_if") else "")
                                                   for s in t["sections"]))
        policy = {k: t[k] for k in ("access", "row_access", "rules", "lock_if", "lock_except", "colors", "wizard", "portal", "workflow",
                                    "unique_together", "limits", "approve_changes", "sealed", "journey") if t.get(k)}
        if policy:
            lines.append("  " + json.dumps(policy, ensure_ascii=False))
        for d in t.get("documents", []):
            lines.append(f"  document {d['key']} \"{d['label']}\"" + (f" (a .{d['extension']} file)" if d.get("format") == "text" else ""))
        if t.get("public_form"):
            lines.append(f"  public form: {', '.join(t['public_form']['fields'])}" + ("" if t["public_form"]["enabled"] else " (off)"))
    for p in bp.get("pages", []):
        widgets = [f"{w['type']} \"{w.get('label', '')}\"" + (f" on {w['table']}" if w.get("table") else "") for w in p["widgets"]]
        extra = "".join(f" ({k} {', '.join(p[k]) if isinstance(p[k], list) else p[k]})" for k in ("home_for", "access") if p.get(k))
        lines.append(f"\nPage {p['key']} \"{p['label']}\"{extra}: " + "; ".join(widgets))
    for portal in schema.portals(bp):
        lines.append(f"\nPortal: {json.dumps(portal, ensure_ascii=False)}")
    if schema.teams(bp):
        lines.append("\nTeams: " + ", ".join(f"{tm['key']} \"{tm['name']}\"" for tm in schema.teams(bp)))
    for sp in schema.spaces(bp):
        lines.append(f"\nSpace (a team's menu): {json.dumps(sp, ensure_ascii=False)}")
    for sy in schema.syncs(bp):
        lines.append(f"\nSync (from a connected app): {json.dumps(sy, ensure_ascii=False)}")
    if bp.get("flows"):
        lines.append("\nFlows (full JSON, needed to update one):")
        for f in bp["flows"]:
            lines.append("  " + json.dumps(f, ensure_ascii=False))
    if bp.get("automations"):
        lines.append("\nAutomations (full JSON, needed to update one):")
        for a in bp["automations"]:
            lines.append("  " + json.dumps(a, ensure_ascii=False))
    if extra:
        lines.append("\n" + extra)
    return "\n".join(lines)


def looking_at(page):
    """What the owner has open, so "add a field here" means something (a screen, or what's open in a studio)."""
    if not page:
        return ""
    from urllib.parse import parse_qs, urlsplit
    split = urlsplit(page)
    opened = (parse_qs(split.query).get("open") or [""])[0]
    page = split.path
    if page.rstrip("/").endswith(("/studio", "/studio/data", "/studio/workflows")):  # the App map (and its old addresses)
        kind, _, key = opened.partition(":")
        if page.rstrip("/").endswith("/studio/data") and opened and not kind == "data":
            kind, key = "data", opened
        what = {"rule": f"the automation {key}", "stages": f"the workflow (stages) of the table {key}", "flow": f"the flow {key}",
                "list": f"the table {key}", "data": f"the table {key}" if key else "", "process": f"the process that starts at the table {key}"}.get(kind)
        return "The owner is in the App map (their app drawn as processes and the data model)" + (f", looking at {what} (\"this\" and \"here\" mean it)." if what else ".")
    if page.rstrip("/").endswith("/design"):
        return "The owner is looking at the versions of the design."
    parts = page.strip("/").split("/")
    if "t" in parts and parts.index("t") + 1 < len(parts):
        i = parts.index("t")
        table = parts[i + 1]
        if i + 2 < len(parts) and parts[i + 2].isdigit():
            return f"The owner is looking at one record (id {parts[i + 2]}) of the table {table}."
        return f"The owner is looking at the table {table}."
    if "p" in parts and parts.index("p") + 1 < len(parts):
        return f"The owner is looking at the page {parts[parts.index('p') + 1]}."
    if "automations" in parts:
        return "The owner is looking at the automations."
    return ""
