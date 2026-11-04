"""What a beginner would trip over in a finished app, found by looking at it the way they would: every screen the owner
and an ordinary staff member can reach, the builder's replies, and what the app says when it won't do something. No AI,
so the same app always gets the same findings and runs can be compared.

Each finding is {"level", "kind", "who", "where", "what"}. Levels:
- blocker: they can't go on, or they're misled (a menu link that doesn't open, code on the screen, a reply that says the
  app is ready while its own check found it isn't). One of these and the app isn't right when handed over;
- confusing: they'd stop and wonder (a word they don't use, a date written like 2026-11-03, a field's code name);
- idea: it works, but could be easier (a form asking for a lot, a list too wide for a phone, a long reply).
"""
import html as htmllib
import re

from app.builder import trial as trials
from app.extensions import db
from app.models import Membership, User

JARGON = re.compile(r"\b(fields?|tables?|workflows?|automations?|schema|json|webhooks?|rollups?|lookups?|formulas?|boolean|"
                    r"enum|null|api|erp|crud|blueprint|embedded|ops|records? id|regex|payload|trigger(ed|s)?)\b", re.I)
CODE = re.compile(r"\{\{|\}\}|Traceback|\[object |\bundefined\b|\bNaN\b|\{'|\['")
SNAKE = re.compile(r"(?<![\w@./:-])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![\w@/-])")
ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2})?\b")
EMPTYISH = {"None", "null", "[]", "{}", "nan", "undefined", "False", "True"}
OWNED = re.compile(r"\b(couldn'?t|could not|not yet|still|didn'?t|did not|left out|isn'?t|is not|doesn'?t|wasn'?t|but|"
                   r"check|missing|failed|problem)\b", re.I)
RAW_NOTES = re.compile(r"came out .{0,60}?, expected|\bexpected \$?[\d.,]+\b.*;|wasn't allowed: “", re.I)
SKIP = re.compile(r"(?is)<(script|style|template|svg|code|pre|kbd)\b.*?</\1>")
INPUT = re.compile(r"<(input|select|textarea)\b([^>]*)>", re.I)
FORM_INPUTS, REQUIRED, COLUMNS, MENU, REPLY_WORDS, QUESTIONS = 15, 10, 8, 14, 160, 3


def review(workspace, result):
    """The findings for one finished app, the owner's words in `result["turns"]` taken as theirs (an owner who says
    "fields" for their paddocks isn't confused by it)."""
    said = " ".join(t.get("say") or "" for t in result.get("turns") or []).lower()
    said += " " + " ".join(str(o) for t in (workspace.blueprint or {}).get("tables", []) for f in t["fields"]
                           for o in f.get("options") or []).lower()  # the business's own choices (a furniture maker's Table)
    theirs = {w for m in JARGON.finditer(said) for w in (m.group(0).lower(), m.group(0).lower().rstrip("s"), m.group(0).lower() + "s")}
    out = _replies(result, theirs) + _refusals(result, theirs)
    with trials.copy_of(workspace) as trial:
        walked = trials.walk(trial, screens=False)  # a record in every list it can make, as people would have
        engine = trial.engine()
        people = [("the owner", trial.owner_id)] + _staff(trial, engine)
        choices = {str(o) for t in engine.tables.values() for f in t["fields"] for o in f.get("options") or []}
        for who, user_id in people:
            out += _screens(trial, engine, who, user_id, walked["made"], theirs | choices)
    return _unique(out)


def _staff(trial, engine):
    """An ordinary staff member with no team, and one in each of the app's first two teams."""
    teams = [tm["key"] for tm in (engine.bp.get("teams") or [])][:2]
    out = []
    for n, team in enumerate([None] + teams):
        user = User(email=f"staff{n}@example.com", name=f"Staff member {n + 1}")
        user.password_hash = "!"
        db.session.add(user)
        db.session.flush()
        db.session.add(Membership(workspace_id=trial.ws_id, user_id=user.id, role="editor", teams=[team] if team else None))
        db.session.commit()
        name = next((tm["name"] for tm in engine.bp.get("teams") or [] if tm["key"] == team), team)
        out.append((f"staff in {name}" if team else "staff", user.id))
    return out


class _Failed:
    """A screen that raised instead of answering: to the person, the same as a 500 ("Something went wrong")."""
    status_code = 500


def _get(client, path, **kw):
    try:
        return client.get(path, **kw)
    except Exception:  # noqa: BLE001 - a fault of the app's, found and reported, and the rest still looked at
        return _Failed()


def _screens(trial, engine, who, user_id, made, theirs):
    client, wid, out = trial.client(user_id), trial.ws_id, []
    home = _get(client, f"/w/{wid}/app/", follow_redirects=True)
    if home.status_code >= 400:
        return [_finding("blocker", "can't get in", who, "home", f"the app doesn't open for {who} ({home.status_code})")]
    page = home.get_data(as_text=True)
    menu = re.search(r'(?is)<nav class="nav">(.*?)</nav>', page)
    links = dict.fromkeys(re.findall(rf'href="(/w/{wid}/app/[^"#]*)"', menu.group(1) if menu else ""))
    if menu and len(links) > MENU:
        out.append(_finding("idea", "long menu", who, "menu", f"the menu has {len(links)} things in it: a lot to take in"))
    visits = {f"/w/{wid}/app/": page}
    for link in list(links)[:40]:
        response = _get(client, htmllib.unescape(link), follow_redirects=True)
        name = _link_name(menu.group(1), link) if menu else link
        if response.status_code >= 400:
            out.append(_finding("blocker", "menu link doesn't open", who, name,
                                f"“{name}” is in {who}'s menu but doesn't open ({response.status_code})"))
            continue
        visits[link] = response.get_data(as_text=True)
    for t in engine.tables.values():  # each list's first record, its form, and a new one, where they can get to them
        if t.get("embedded"):
            continue
        base = f"/w/{wid}/app/t/{t['key']}"
        listed = next((html for path, html in visits.items() if path.split("?")[0] == base), None)
        if listed is None and who != "the owner":
            continue
        for path in [base + "/new"] + ([f"{base}/{made[t['key']]}", f"{base}/{made[t['key']]}/edit"] if made.get(t["key"]) else []):
            response = _get(client, path)
            if response.status_code < 400:
                visits[path] = response.get_data(as_text=True)
            elif response.status_code >= 500:
                out.append(_finding("blocker", "screen errors", who, path, f"a {t['singular'].lower()} screen fails for {who}"))
    for path, html in visits.items():
        out += _page(engine, who, path, html, theirs)
    return out


def _page(engine, who, path, html, theirs):
    where = _where(engine, path)
    out = []
    nodes = _text(html)
    for node in nodes:
        out += words_found(node, theirs, who, where)
    if path.endswith("/new"):  # editing shows what's filled in: it's theirs
        form = re.search(r'(?is)<form\b[^>]*id="record-form".*?</form>', html)  # the form's own: not the search or Ask box around it
        upfront = re.sub(r'(?is)<details class="more-fields".*?</details>', " ", form.group(0) if form else html)  # what waits under "More details" isn't asked up front
        asked = [a for _, a in INPUT.findall(upfront)
                 if not re.search(r'type="(hidden|submit|button|search)"|name="(csrf_token|q)"|data-remove-line', a)]
        required = upfront.count('class="req"')
        if len(asked) > FORM_INPUTS:
            out.append(_finding("idea", "long form", who, where, f"the form asks for {len(asked)} things"))
        if required > REQUIRED:
            out.append(_finding("idea", "long form", who, where, f"{required} things must be filled in before it saves"))
    table = re.search(r"(?is)<table\b[^>]*>.*?</thead>", html)
    columns = len(re.findall(r'<th(?! class="sel")\b', table.group(0))) if table else 0
    if table and columns > COLUMNS and "as-cards" not in table.group(0)[:200]:  # a list that turns into cards on a phone reads fine
        out.append(_finding("idea", "wide list", who, where, f"the list has {columns} columns: hard to read on a phone"))
    return out


def words_found(text, theirs, who, where):
    """What a beginner would trip over in one piece of text they're shown: code, a value that's code ("None"), a code
    name, a date the way computers write it, a word they won't use (unless it's theirs)."""
    out = []
    if CODE.search(text):
        out.append(_finding("blocker", "code on screen", who, where, f"shows code: “{text[:80]}”"))
    elif text in EMPTYISH and text not in theirs:  # "None" as the app's own choice (no shortage) is a word, not code
        out.append(_finding("confusing", "code on screen", who, where, f"shows “{text}” where a value or a blank belongs"))
    for token in SNAKE.findall(text):
        out.append(_finding("confusing", "code name on screen", who, where, f"shows the code name “{token}” instead of words"))
    for date in ISO.findall(text):
        out.append(_finding("confusing", "date written as code", who, where, f"shows the date “{date}” the way computers write it"))
    for word in {m.group(0).lower() for m in JARGON.finditer(text)} - theirs:
        out.append(_finding("confusing", "word they won't know", who, where, f"says “{word}”: “{text[:90]}”"))
    return out


def theirs_of(workspace, result):
    """The owner's own words (what they said, and their lists' choices): not jargon to them."""
    said = " ".join(t.get("say") or "" for t in result.get("turns") or []).lower()
    said += " " + " ".join(str(o) for t in (workspace.blueprint or {}).get("tables", []) for f in t["fields"] for o in f.get("options") or []).lower()
    return {w for m in JARGON.finditer(said) for w in (m.group(0).lower(), m.group(0).lower().rstrip("s"), m.group(0).lower() + "s")}


def _replies(result, theirs):
    out = []
    for n, t in enumerate(result.get("turns") or [], 1):
        if t.get("crash"):
            out.append(_finding("blocker", "builder failed", "the owner", f"turn {n}", f"the builder stopped with an error: {t['crash'][:120]}"))
            continue
        reply = t.get("reply") or ""
        where = f"reply {n}"
        for word in {m.group(0).lower() for m in JARGON.finditer(reply)} - theirs:
            out.append(_finding("confusing", "word they won't know", "the owner", where, f"the builder's reply says “{word}”"))
        for token in SNAKE.findall(reply):
            out.append(_finding("confusing", "code name on screen", "the owner", where, f"the builder's reply says “{token}”"))
        words = len(reply.split())
        if words > REPLY_WORDS:
            out.append(_finding("idea", "long reply", "the owner", where, f"the builder's reply is {words} words: a lot to read"))
        if RAW_NOTES.search(reply):
            out.append(_finding("confusing", "raw check notes", "the owner", where,
                                "the builder's reply passes on the try-out's raw notes (“… came out …, expected …”)"))
        if reply.count("?") > QUESTIONS:
            out.append(_finding("confusing", "many questions", "the owner", where, f"the builder asks {reply.count('?')} questions at once"))
        tried = t.get("tried") or {}
        failed = [e for e in tried.get("examples") or [] if not e.get("ok")]
        if t.get("status") == "done" and (tried.get("issues") or failed) and not OWNED.search(reply):
            out.append(_finding("blocker", "said ready when it wasn't", "the owner", where,
                                "the builder's reply says it's done, though its own try-out found something still wrong"))
        if t.get("status") not in (None, "done"):
            out.append(_finding("blocker", "builder failed", "the owner", where, f"the builder's turn ended “{t.get('status')}”"))
    return out


def _refusals(result, theirs):
    """What the app says when it won't do something (the builder's examples were refused): plain words, or not."""
    out = []
    for t in result.get("turns") or []:
        for e in (t.get("tried") or {}).get("examples") or []:
            said = str(e.get("problem") or "")
            reason = said.split("wasn't allowed:", 1)[1] if "wasn't allowed:" in said else ""
            for token in SNAKE.findall(reason):
                out.append(_finding("confusing", "refusal in code", "staff", "a refusal", f"when it says no, it says “{token}”: “{reason[:100]}”"))
            for word in {m.group(0).lower() for m in JARGON.finditer(reason)} - theirs:
                out.append(_finding("confusing", "refusal in code", "staff", "a refusal", f"when it says no, it says “{word}”: “{reason[:100]}”"))
    return out


def _text(html):
    """A page's words as people read them, without the code behind them, plus what its empty boxes say."""
    html = SKIP.sub(" ", html)
    nodes = [htmllib.unescape(n).strip() for n in re.split(r"<[^>]+>", html)]
    return [n for n in nodes if n] + [htmllib.unescape(p) for p in re.findall(r'placeholder="([^"]+)"', html)]


def _link_name(menu, link):
    found = re.search(rf'href="{re.escape(link)}"[^>]*>(.*?)</a>', menu, re.S)
    return re.sub(r"<[^>]+>", "", found.group(1)).strip() if found else link


def _where(engine, path):
    match = re.search(r"/app/(t|p)/([\w-]+)(?:/(\w+))?(?:/(edit))?", path)
    if not match:
        return "home" if path.rstrip("/").endswith("/app") else path.split("/app/")[-1]
    kind, key, rid, edit = match.groups()
    if kind == "p":
        return next((p["label"] for p in engine.bp.get("pages", []) if p["key"] == key), key) + " page"
    t = engine.tables.get(key) or {"label": key, "singular": key}
    one = t["singular"].lower()
    if rid == "new":
        return f"new {one} form"
    if rid:
        return f"{'an' if one[:1] in 'aeiou' else 'a'} {one}" + (" (editing)" if edit else "")
    return f"{t['label']} list"


def _finding(level, kind, who, where, what):
    return {"level": level, "kind": kind, "who": who, "where": where, "what": what}


def _unique(findings):
    seen, out = set(), []
    for f in findings:
        key = (f["level"], f["kind"], f["where"], f["what"])
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out
