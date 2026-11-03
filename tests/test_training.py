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
    monkeypatch.setattr(checking, "add", lambda ws, text, by=None: next(results))
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
