"""The App map through a beginner's eyes, in a real browser: the page is drawn by its script, so it can only be judged as
it's shown. A copy of the finished app is served, with a record in each list, and the owner signs in and looks at every
view: the map, each list, its rules, what each list keeps, a column, making a rule, changing the stages, trying an
example; then the map, a list and what each keeps on a phone.

Findings as evals/ux.py: {"level", "kind", "who", "where", "what"}. Blockers: the page breaks (an error, a failed
request, a view that doesn't open), code on screen, a list that isn't on the map. Confusing: a word they won't know, a
code name, a rule they can't find from the map, a page that scrolls sideways on a phone. Ideas: text too small to read,
text cut off, a view with a lot to read.
"""
import threading

from app.builder import trial as trials
from app.extensions import db
from app.models import User

from . import ux

PASSWORD = "map-eyes-1"
READ = r"""() => {
  const main = document.querySelector("main") || document.body;
  const shown = (el) => { const r = el.getBoundingClientRect(), s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none"; };
  const texts = [], tiny = [], cut = [];
  const walk = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  while (walk.nextNode()) {
    const n = walk.currentNode, t = n.textContent.replace(/\s+/g, " ").trim(), el = n.parentElement;
    if (!t || !el || el.closest("script, style, svg, template, [hidden], .sr-only") || !shown(el)) continue;
    texts.push(t);
    if (parseFloat(getComputedStyle(el).fontSize) < 11) tiny.push(t);
  }
  main.querySelectorAll("input[placeholder], textarea[placeholder]").forEach((el) => { if (shown(el)) texts.push(el.placeholder); });
  main.querySelectorAll("*").forEach((el) => {
    if (shown(el) && el.children.length === 0 && el.scrollWidth > el.clientWidth + 2 && getComputedStyle(el).textOverflow === "ellipsis") cut.push(el.textContent.trim().slice(0, 60));
  });
  return {texts, tiny, cut, opens: [...document.querySelectorAll("[data-open]")].map((e) => e.dataset.open),
          wide: document.documentElement.scrollWidth > innerWidth + 1, words: texts.join(" ").split(/\s+/).length};
}"""


def review(workspace, theirs):
    """The App map of this app looked at as the owner would, desktop and phone. Returns findings."""
    from playwright.sync_api import sync_playwright
    from app.webkit.serving import make_server
    out = []
    with trials.copy_of(workspace) as trial:
        trials.walk(trial, screens=False)  # a record in each list, as people would have
        owner = db.session.get(User, trial.owner_id)
        owner.set_password(PASSWORD)
        db.session.commit()
        email, wid = owner.email, trial.ws_id
        engine = trial.engine()
        server = make_server("127.0.0.1", 0, trial.app, threaded=True)  # stops cleanly when it's done
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                try:
                    out += _desktop(browser, base, wid, email, engine, theirs)
                    out += _phone(browser, base, wid, email, theirs)
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001 - the eyes failing is said, never the run
            out.append(ux._finding("idea", "not looked at", "the owner", "App map", f"the App map couldn't be looked at: {type(exc).__name__}: {str(exc)[:120]}"))
        finally:
            server.shutdown()
            thread.join(timeout=5)
    return ux._unique(out)


def looked(findings):
    """How many views were looked at isn't kept: a run with nothing found is told apart from one that couldn't look."""
    return not any(f["kind"] == "not looked at" for f in findings)


def _page(browser, base, email, width, height, broke):
    page = browser.new_page(viewport={"width": width, "height": height}, is_mobile=width < 500, has_touch=width < 500)
    where = {"now": "signing in"}
    page.on("pageerror", lambda e: broke.append((where["now"], f"the page fails: {str(e)[:140]}")))
    page.on("console", lambda m: broke.append((where["now"], f"the page says an error: {m.text[:140]}")) if m.type == "error" and "favicon" not in m.text else None)
    page.on("response", lambda r: broke.append((where["now"], f"a request failed ({r.status}) {r.url.split('/app/')[-1][:60]}")) if r.status >= 500 else None)
    page.goto(base + "/login")
    page.fill("input[name=email]", email)
    page.fill("input[name=password]", PASSWORD)
    page.click("form button.primary")
    page.wait_for_load_state("networkidle")
    return page, where


def _look(page, where, name, url=None, then=None):
    """Open a view (or do something on the one open) and read it. Returns what READ finds, or None if it didn't open."""
    where["now"] = name
    try:
        if url:
            page.goto(url)
            page.wait_for_load_state("networkidle")
        if then:
            then(page)
        page.wait_for_timeout(250)
        return page.evaluate(READ)
    except Exception:  # noqa: BLE001 - a view that doesn't open is the finding
        return None


def _desktop(browser, base, wid, email, engine, theirs):
    out, broke, seen = [], [], {}
    page, where = _page(browser, base, email, 1440, 900, broke)
    studio = f"{base}/w/{wid}/app/studio"
    drawn = page.request.get(f"{studio}/map.json").json()["map"]
    lists = [c for p in drawn["processes"] for c in p["cards"]] + drawn["reference"]
    visible = [t for t in engine.visible_blueprint().get("tables", []) if not t.get("embedded") and not t.get("combines")]
    for t in visible:
        if not any(c["key"] == t["key"] for c in lists):
            out.append(ux._finding("blocker", "list missing from the map", "the owner", "App map", f"{t['label']} isn't anywhere on the App map"))
    seen["the App map"] = _look(page, where, "the App map", studio)
    for c in lists[:14]:
        seen[f"{c['label']} on the App map"] = _look(page, where, f"{c['label']} on the App map", f"{studio}?open=list:{c['key']}")
    rules = [a for a in engine.bp.get("automations", []) if not a["trigger"].get("table") or a["trigger"]["table"] in engine.tables]
    reachable = {o for v in seen.values() if v for o in v["opens"]}
    for a in rules:
        if f"rule:{a['key']}" not in reachable:
            out.append(ux._finding("confusing", "rule not on the map", "the owner", "App map", f"the rule “{a['name']}” can't be found from the map or its lists"))
    for a in rules[:3]:
        seen[f"the rule “{a['name']}”"] = _look(page, where, f"the rule “{a['name']}”", f"{studio}?open=rule:{a['key']}")
    busiest = max((c for p in drawn["processes"] for c in p["cards"]), key=lambda c: (len(c["stages"]), len(c["rules"])), default=None)
    if busiest:
        seen["making something happen automatically"] = _look(page, where, "making something happen automatically", f"{studio}?open=list:{busiest['key']}",
                                                              lambda pg: pg.click("[data-new-rule]"))
        if busiest["stages"]:
            seen["changing the stages"] = _look(page, where, "changing the stages", f"{studio}?open=list:{busiest['key']}",
                                                lambda pg: pg.click("[data-edit-stages]"))
    seen["what each list keeps"] = _look(page, where, "what each list keeps", f"{studio}?view=data")
    seen["a list's columns"] = _look(page, where, "a list's columns", then=lambda pg: pg.click(".data-card >> nth=0"))
    seen["a column"] = _look(page, where, "a column", then=lambda pg: pg.click(".data-card [data-col] >> nth=0"))
    if drawn.get("examples"):
        seen["trying an example"] = _look(page, where, "trying an example", studio, lambda pg: (
            pg.click("[data-form=play] button"), pg.wait_for_selector(".map-played", timeout=60000)))
    page.close()
    return out + _judged(seen, broke, theirs, "the owner")


def _phone(browser, base, wid, email, theirs):
    broke, seen, out = [], {}, []
    page, where = _page(browser, base, email, 390, 844, broke)
    studio = f"{base}/w/{wid}/app/studio"
    drawn = page.request.get(f"{studio}/map.json").json()["map"]
    seen["the App map on a phone"] = _look(page, where, "the App map on a phone", studio)
    first = next((c for p in drawn["processes"] for c in p["cards"]), None) or next(iter(drawn["reference"]), None)
    if first:
        seen[f"{first['label']} on a phone"] = _look(page, where, f"{first['label']} on a phone", f"{studio}?open=list:{first['key']}")
    seen["what each list keeps on a phone"] = _look(page, where, "what each list keeps on a phone", f"{studio}?view=data")
    for name, v in seen.items():
        if v and v["wide"]:
            out.append(ux._finding("confusing", "scrolls sideways on a phone", "the owner", name, "the page is wider than the phone: it has to be dragged sideways"))
    page.close()
    return out + _judged(seen, broke, theirs, "the owner on a phone")


def _judged(seen, broke, theirs, who):
    out = [ux._finding("blocker", "App map breaks", who, where, what) for where, what in broke]
    for name, v in seen.items():
        if v is None:
            out.append(ux._finding("blocker", "view doesn't open", who, name, f"{name} doesn't open"))
            continue
        for text in v["texts"]:
            out += ux.words_found(text, theirs, who, name)
        if v["tiny"]:
            out.append(ux._finding("idea", "text too small", who, name, f"{len(v['tiny'])} bits of text are too small to read, like “{v['tiny'][0][:50]}”"))
        if v["cut"]:
            out.append(ux._finding("idea", "text cut off", who, name, f"{len(v['cut'])} bits of text are cut off, like “{v['cut'][0]}”"))
        if v["words"] > 1500:
            out.append(ux._finding("idea", "a lot to read", who, name, f"{v['words']} words on one view"))
    return out
