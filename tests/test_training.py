"""Lessons for fine-tuning are graded by what checked them: only a build an independent check confirmed, whose answer was
right as given, is gold. The owner's own checks (written from what they said, before the build) are that check for the
businesses the evals make up."""
import json

from app.blueprint import ops
from app.builder import training
from evals import analyst


def test_the_platforms_corrections_mark_an_answer_as_not_right_first_time(app):
    from app.blueprint import schema
    result = ops.apply(schema.empty("Shop"), [
        {"op": "create_table", "table": "quotes", "label": "Quotes", "fields": [
            {"key": "title", "type": "text", "required": True}, {"key": "discount", "type": "percent"},
            {"key": "status", "type": "select", "options": ["Draft", "Big", "Small"]}],
         "workflow": {"field": "status", "transitions": [
             {"from": ["Draft"], "to": "Big", "only_if": [{"field": "discount", "op": "greater_than", "value": 0.1}]},
             {"from": ["Draft"], "to": "Small", "only_if": [{"field": "discount", "op": "less_than", "value": 0.1}]}]}}])
    assert not result.problems and result.corrected and set(result.corrected) <= set(result.changes)


def test_owner_checks_decide_whether_a_build_is_verified(app, tmp_path, monkeypatch):
    from app.builder import checking
    folder = tmp_path / "training"
    monkeypatch.setattr(analyst.fresh, "checks", lambda trade, said: ["A $1,000 quote with GST is $1,100", "Nonsense", "A $6,000 order waits"])
    results = iter([{"ok": True, "kind": None}, None, {"ok": False, "kind": "app", "plain": "Order’s Status came out Sent"}])
    monkeypatch.setattr(checking, "add_and_tell", lambda ws, text, by=None: next(results))
    ws = type("W", (), {"id": 7})()
    out = analyst.owner_checks(ws, {"fresh": "bakery", "real": "We bake."}, folder)
    assert [c["result"] for c in out] == ["pass", "couldn't check", "fail"]
    said = [json.loads(line) for line in (folder / "verdicts.jsonl").read_text().splitlines()]
    assert said[0]["ok"] is False and said[0]["workspace"] == 7 and said[0]["why"] == "A $6,000 order waits"


def test_a_lesson_is_gold_only_when_verified_and_right_as_given(tmp_path):
    folder = tmp_path / "training"
    folder.mkdir()
    call = lambda kind, **x: {"kind": kind, "applied": True, "content": "{}", "messages": [{"role": "user", "content": "hi"}], **x}  # noqa: E731
    outcome = {"status": "done", "tried": True, "examples": [3, 3], "issues_left": 0, "left_out": 0,
               "first": {"wrong": 0, "examples": [3, 3]}}
    turn = {"workspace": 1, "message": 2, "business": "Fresh: bakery", "outcome": outcome,
            "calls": [call("design"), call("design", corrected=["Added a Cancel button"]), call("examples")]}
    (folder / "builder-2026-11.jsonl").write_text(json.dumps(turn) + "\n")
    assert [r["kind"] for r in training.examples([folder], "silver")] == ["design", "design", "examples"]
    assert list(training.examples([folder])) == []
    training.verdict(folder, True, by="owner checks", workspace=1)
    assert [r["kind"] for r in training.examples([folder])] == ["design", "examples"]  # the corrected answer isn't gold
    said = training.report([folder])
    assert "gold      2 lessons (design 1, examples 1)" in said and "first draft right, before its own fixes: 1 of 1" in said, said


def _kept(folder, *turns):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "builder-2026-11.jsonl").write_text("".join(json.dumps(t) + "\n" for t in turns))


RIGHT = {"status": "done", "tried": True, "examples": [2, 2], "issues_left": 0, "left_out": 0, "first": {"wrong": 0, "examples": [2, 2]}}


def _call(kind, content="{}", asked="hi", **x):
    return {"kind": kind, "content": content, "messages": [{"role": "user", "content": asked}], **x}


def test_keels_turns_kept_before_answers_were_marked_are_still_lessons(tmp_path):
    """Until November 2026 Keel's builder marked no answer as used, so no verified build ever gave a lesson. What a kept
    turn shows says which were: every answer of a step (one that failed isn't kept), the fix chosen, and which were put
    right (a repair sent; examples written again)."""
    folder = tmp_path / "training"
    plain = {"workspace": 1, "message": 1, "business": "a", "outcome": RIGHT,
             "calls": [_call("v2_understand"), _call("v2_data"), _call("v2_behaviour"), _call("v2_checks"),
                       _call("v2_fix", "a"), _call("v2_fix", "b", chosen=True)]}
    mended = {"workspace": 1, "message": 2, "business": "a", "outcome": RIGHT,
              "calls": [_call("v2_data"), _call("v2_repair"), _call("v2_checks"),
                        _call("v2_checks", asked="These examples couldn't be acted out. Send each corrected")]}
    _kept(folder, plain, mended)
    training.verdict(folder, True, by="owner checks", workspace=1)
    graded = [[(c["kind"], training.call_grade(c, t)) for c in t["calls"]] for t in training.turns([folder])]
    assert graded[0] == [("v2_understand", "gold"), ("v2_data", "gold"), ("v2_behaviour", "gold"), ("v2_checks", "gold"),
                         ("v2_fix", None), ("v2_fix", "gold")]
    assert graded[1] == [("v2_data", "silver"), ("v2_repair", "silver"), ("v2_checks", "silver"), ("v2_checks", "gold")]
    assert "gold design lessons 2 of 150" in training.report([folder])  # Keel's lists and behaviour are the design


def test_whose_answers_the_lessons_are_is_said_and_can_be_kept_to_one_model(tmp_path):
    """A cloud model may build some apps: its right answers are kept like any other, with its provider and model, and the
    report says how many each wrote (some providers' terms don't allow training another model on theirs)."""
    folder = tmp_path / "training"
    ours = dict(provider="ollama", model="qwen3:32b", applied=True)
    theirs = dict(provider="anthropic", model="claude-opus-5-5", applied=True)
    _kept(folder, {"workspace": 1, "message": 1, "business": "a", "outcome": RIGHT,
                   "calls": [_call("v2_understand", **ours), _call("v2_checks", **theirs), _call("v2_data", **theirs)]})
    training.verdict(folder, True, by="owner checks", workspace=1)
    said = training.report([folder])
    assert "gold lessons written by: anthropic claude-opus-5-5 2, ollama qwen3:32b 1" in said, said
    assert "2 gold lessons were written by a cloud model" in said
    assert [r["kind"] for r in training.examples([folder], by="ollama")] == ["v2_understand"]
    assert [r["kind"] for r in training.examples([folder], by="Claude-Opus")] == ["v2_checks", "v2_data"]
    out = tmp_path / "set"
    assert training.main([str(folder), "--out", str(out), "--by", "ollama", "--valid", "0"]) == 0
    assert len((out / "train.jsonl").read_text().splitlines()) == 1


def test_an_eval_builds_with_the_ai_the_platform_chose(app, tmp_path):
    """Each app an eval builds has a database of its own, so the platform's choice under Settings › AI (a cloud model,
    say) never reached it: it built with the .env's. Now it's read once from the platform's database, given to each
    app, its saved keys with it, and each result says which AI built it."""
    from app import ai, ai_providers as providers
    from app.security import encrypt
    providers.save({"use": {"builder": {"provider": "anthropic", "model": "claude-opus-5-5"}}, "urls": {},
                    "keys": {"anthropic": encrypt("sk-ant-test")}}, by="admin")
    choice = analyst.platform_ai(app.config["SQLALCHEMY_DATABASE_URI"], app.config["SECRET_KEY"])
    assert choice == {"use": {"builder": {"provider": "anthropic", "model": "claude-opus-5-5"}}, "urls": {}, "keys": {"anthropic": "sk-ant-test"}}
    assert analyst.platform_ai(app.config["SQLALCHEMY_DATABASE_URI"], "another platform's secret")["keys"] == {}  # from .env then
    providers.save({}, by="admin")  # an app's own database, as an eval makes it: nothing chosen
    assert providers.resolve("builder")[0] != "anthropic"
    analyst._build_with(choice)
    assert providers.resolve("builder")[:2] == ("anthropic", "https://api.anthropic.com/v1") and providers.resolve("builder")[2] == "sk-ant-test"
    used = analyst.ai_used()
    assert used["builder"] == "Anthropic (Claude) claude-opus-5-5" and used["cloud"] and used["owner"] != used["builder"]
    assert analyst._ai_line(used).startswith("built by Anthropic (Claude) claude-opus-5-5, owner played by ")


def test_a_round_built_again_keeps_only_the_checks_an_owner_wrote_from_their_first_words(tmp_path):
    """An owner who answered the first builder's questions wrote checks from those answers; another builder asks other
    questions, so holding it to them tests what it was never told. Their checks are written again from what they tell it."""
    check = lambda text: {"check": text, "result": "pass"}  # noqa: E731
    runs = [{"key": "fresh1", "title": "Fresh: fridge repairer", "said": "fridge biz is chaos", "style": "vague", "real": "We fix fridges.",
             "turns": [{"say": "fridge biz is chaos"}, {"say": "Book it straight away, invoice after"}], "owner_checks": [check("Invoices at once")]},
            {"key": "fresh2", "title": "Fresh: surf school", "said": "We teach surfing.", "style": "plain",
             "turns": [{"say": "We teach surfing."}], "owner_checks": [check("A beginner gets a beginner class")]}]
    (tmp_path / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in runs))
    again = {s["key"]: s for s in analyst.same_as(tmp_path)}
    assert again["fresh1"]["checks"] is None and again["fresh1"]["turns"] == [{"say": "fridge biz is chaos", "expect": {}}]
    assert again["fresh2"]["checks"] == ["A beginner gets a beginner class"]


def test_an_owner_left_with_no_checks_by_chance_is_asked_again_and_an_app_nobody_checked_is_said_apart(app, monkeypatch):
    """The check writer sometimes gave none for words it could check (and some the next time): none is asked again. An app
    right with no check of the owner's own passing is right only by the builder's say, and the totals count it apart."""
    from app import ai
    answers = iter(['{"checks": []}', '{"checks": ["I didn\'t mention prices"]}', '{"checks": ["Unpaid invoices are flagged"]}'])
    monkeypatch.setattr(ai, "chat", lambda *a, **k: (next(answers), ""))
    assert analyst.fresh.checks("fridge repairer", "Unpaid invoices") == ["Unpaid invoices are flagged"]
    monkeypatch.setattr(ai, "chat", lambda *a, **k: ('{"checks": []}', ""))
    assert analyst.fresh.checks("fridge repairer", "need help") == []
    turn = {"say": "hi", "status": "done", "tried": {"examples": [{"title": "x", "ok": True}]}, "passed": 0, "of": 0, "checks": {},
            "seconds": 9, "parts": 1, "repairs": 0, "changes": 3, "reply": "Your app is ready to try."}
    tryout = {"fix": [], "crashes": [], "check": [], "made": 1, "tables": 1}
    looked = {"key": "a", "title": "Fresh: a", "style": "vague", "turns": [turn], "tryout": tryout, "owner_checks": [{"check": "c", "result": "pass"}]}
    alone = {"key": "b", "title": "Fresh: b", "style": "vague", "turns": [turn], "tryout": tryout, "owner_checks": []}
    assert analyst.owner_vouched(looked) and not analyst.owner_vouched(alone)
    said = analyst.totals([looked, alone])
    assert "RIGHT WHEN HANDED OVER: 2/2" in said and "1 of them right by the builder's own checks alone" in said, said
    assert "started vague: right when handed over 2/2 (1 by the builder's own checks alone)" in said
    assert "only the builder's own say it's right" in analyst.report(alone) and "own say" not in analyst.report(looked)


def test_an_evals_lessons_are_collected_where_a_temporary_folder_cant_lose_them(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAINING_DIR", str(tmp_path / "kept"))
    folder = tmp_path / "run45" / "fresh3" / "training"
    _kept(folder, {"workspace": 1, "message": 1, "outcome": RIGHT, "calls": []})
    training.verdict(folder, True, by="owner checks", workspace=1)
    analyst._keep_lessons(tmp_path / "run45", "fresh3", folder)
    kept = tmp_path / "kept" / "evals" / "run45" / "fresh3"
    assert sorted(p.name for p in kept.iterdir()) == ["builder-2026-11.jsonl", "verdicts.jsonl"]


def test_the_builder_asks_once_more_for_an_answer_it_cant_read(app, monkeypatch):
    """One unreadable answer failed the owner's change outright; it's asked for again (warmer, another seed) first."""
    from app import ai
    from app.builder import build
    said, asked = iter(['<invoke name="noop"></invoke> {"patches": [{"do": "add_st', '{"patches": [], "reply": "Done."}']), []
    monkeypatch.setattr(ai, "chat_thinking", lambda messages, **k: (asked.append(ai.again.get()), (next(said), "", None))[1])
    schema = {"type": "object", "required": ["patches"], "properties": {"patches": {"type": "array"}, "reply": {"type": "string"}}}
    assert build._ask("system", "add a stage", schema, kind="behaviour")["reply"] == "Done."
    assert asked == [0, 1]
    monkeypatch.setattr(ai, "chat_thinking", lambda messages, **k: ("not json", "", None))
    import pytest
    with pytest.raises(ai.AIError):
        build._ask("system", "add a stage", schema, kind="behaviour")


def test_a_fix_that_replaces_a_feature_is_still_asked_what_the_feature_was_checked_for():
    """A ready-made feature's own checks go with it; a fix that swapped it for rules doing the same job (a deposit
    reminder that skips quotes already paid) was scored as if those checks had failed. They're acted out on it instead."""
    from app.builder import build
    before = [{"title": "Paid quotes aren't chased"}, {"title": "A quote still Accepted after 7 days is chased"}]
    after = [{"title": "Paid quotes aren't chased"}, {"title": "Deposit reminder"}]
    assert [x["title"] for x in build._still_asked(after, before)] == [
        "Paid quotes aren't chased", "Deposit reminder", "A quote still Accepted after 7 days is chased"]
    assert build._still_asked(before, before) == before
