"""The eval's headline: was the app right when handed over, and did later changes keep it right (evals/analyst.py)."""
from evals.analyst import handed_over, kept_working, totals


def turn(status="done", examples=(), stories=None, **more):
    return {"status": status, "passed": 1, "of": 1, "left_out": [], "tried": {"examples": list(examples)},
            **({"stories": stories} if stories is not None else {}), **more}


def app(*turns, fix=(), crashes=(), stories=(), owner=()):
    return {"turns": list(turns), "tryout": {"fix": list(fix), "check": [], "crashes": list(crashes), "made": 1, "tables": 1},
            "stories": [{"story": s, "result": r, "why": ""} for s, r in stories], "owner_checks": list(owner)}


def test_right_when_everything_holds_even_after_fix_rounds():
    first_draft_wrong = {"examples": [{"title": "Quote total", "ok": True}],
                         "first": {"wrong": 2, "examples": [0, 1]}}  # the builder fixed its own mistakes before handing over
    assert handed_over(app(turn(**{"tried": first_draft_wrong}), stories=[("totals", "pass")])) == []


def test_what_stops_it_being_right():
    why = handed_over(app(turn(examples=[{"title": "Rent run", "ok": False}]), turn(status="failed"), fix=["a rule stops"],
                          stories=[("approvals", "fail"), ("totals", "pass")],
                          owner=[{"check": "Paid invoices lock", "result": "fail"}]))
    text = "; ".join(why)
    for bit in ("a turn failed", "trying it out found 1", "story fail: approvals", "owner's check failed: Paid invoices lock"):
        assert bit in text, text
    assert "Rent run" not in text  # only the last handover's examples count: a later turn shows any still failing


def test_nothing_built_isnt_scored():
    assert handed_over({"turns": [turn()], "tryout": None}) is None


def test_a_later_change_breaking_a_story():
    r = app(turn(stories={"totals": "pass", "approvals": "missing"}), turn(stories={"totals": "fail", "approvals": "pass"}),
            turn(stories={"totals": "pass", "approvals": "pass"}))
    worked, broke = kept_working(r)
    assert worked == {"totals", "approvals"} and broke == {"totals": 2}
    said = totals([r])
    assert "KEPT WORKING through later changes: 1/2" in said and "RIGHT WHEN HANDED OVER: 1/1" in said, said


def test_old_runs_without_stories_each_turn():
    assert kept_working(app(turn(), turn())) == (set(), {})


def test_what_a_beginner_would_trip_over_counts_against_the_handover():
    from evals import ux
    stuck = dict(app(turn()), ux=[{"level": "blocker", "kind": "menu link doesn't open", "who": "staff", "where": "Jobs",
                                   "what": "“Jobs” is in staff's menu but doesn't open (403)"}])
    assert handed_over(stuck) == ["a beginner would be stuck: “Jobs” is in staff's menu but doesn't open (403) (staff, Jobs)"]
    ideas = dict(app(turn()), ux=[{"level": "idea", "kind": "wide list", "who": "the owner", "where": "Jobs list", "what": "wide"}])
    assert handed_over(ideas) == [] and "BEGINNER'S EYES: 0 places they'd be stuck, 0 confusing, 1 ideas" in totals([ideas])
    seen = ux._page(type("E", (), {"bp": {"pages": []}, "tables": {}})(), "the owner", "/w/1/app/", (
        "<p>{{record.total}}</p><td>None</td><td>approved_by</td><td>2026-11-03T09:00</td><p>Set a field</p>"
        "<p>Your paddock field</p><script>var x_y = 1;</script>"), theirs={"field"})
    assert [f["kind"] for f in seen] == ["code on screen", "code on screen", "code name on screen", "date written as code"]
    said = ux._replies({"turns": [turn(reply="Done! I added bid_lines to the workflow.",
                                       examples=[{"title": "A bid", "ok": False, "problem": "x"}])]}, theirs=set())
    assert {f["kind"] for f in said} == {"word they won't know", "code name on screen", "said ready when it wasn't"}
    owned = ux._replies({"turns": [turn(reply="I set up bids, but one example still didn't work: check the totals.",
                                        examples=[{"title": "A bid", "ok": False}])]}, theirs=set())
    assert not owned  # it said so


def test_consistency_counts_a_business_right_on_one_run_and_wrong_on_the_next():
    from evals.analyst import consistency
    right = lambda key: dict(app(turn(), stories=[("Invoices add up", "pass")]), key=key)  # noqa: E731
    wrong = lambda key: dict(app(turn(), stories=[("Invoices add up", "fail")]), key=key)  # noqa: E731
    runs = [right("plumbing-1"), right("plumbing-2"), right("cafe-1"), wrong("cafe-2"), wrong("farm-1"), wrong("farm-2")]
    steady = consistency(runs)
    assert steady["always"] == ["plumbing 2/2"] and steady["never"] == ["farm 0/2"] and steady["sometimes"] == ["cafe 1/2"]
    assert steady["flipping"] == ["cafe: Invoices add up (1/2)"]
    assert "CONSISTENT: 2/3 businesses came out the same every run" in totals(runs)
    assert consistency([right("plumbing")]) is None  # one run each: nothing to compare


def test_a_regrade_acts_the_last_turns_own_examples_out_again_with_todays_code(app, owner):
    """The examples kept with their results are played again on the saved design; a run saved before they were kept
    keeps what it found."""
    from app.blueprint import ops
    from app.builder import chat
    from evals.analyst import _replayed
    _, ws = owner
    result = ops.apply(ws.blueprint, [{"op": "create_table", "table": "jobs", "label": "Jobs", "fields": [
        {"key": "title", "type": "text", "required": True}, {"key": "hours", "type": "number"}]}])
    chat.save_version(ws, result.blueprint, result.changes, "chat")
    example = {"title": "A job's hours", "steps": [{"do": "add", "table": "jobs", "as": "j1", "values": {"title": "Tap", "hours": 2}}],
               "expect": [{"record": "j1", "field": "hours", "equals": 2}]}
    then = turn(examples=[{"title": "A job's hours", "ok": False, "lines": [], "problem": "a fault since fixed", "example": example}])
    again = _replayed(ws, [then])
    assert again[0]["tried"]["examples"][0]["ok"] and again[0]["tried"]["examples"][0]["problem"] == ""
    old = turn(examples=[{"title": "A job's hours", "ok": False, "lines": [], "problem": "a fault since fixed"}])
    assert _replayed(ws, [old]) == [old]
