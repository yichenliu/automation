"""What the builder's model was asked and answered, kept to train a model that builds better, and how good each lesson is.

Every builder turn records its conversations with the model (ai.recording) and how each answer turned out: whether its
changes fitted (`applied`), whether the platform had to put mistakes right (`corrected`), what kind of answer it was
(design, review, fix, examples). `keep` appends the turn, with how the app did when it was tried out, to
DATA_DIR/training/builder-YYYY-MM.jsonl. Nothing leaves this computer.

The builder's own worked examples passing isn't proof: the beginner testers found apps whose own checks were green and
which failed in real use. So a turn is only *verified* when something independent of the builder agrees: the owner's
own checks (written from what they said, before anything was built, then tried on the app), a story of the eval, or a
person who used it. Those verdicts are kept beside the turns (verdicts.jsonl, `verdict`), and a "no" from anyone
outweighs every "yes". Each answer is then graded:

- gold: a verified turn, and the model's answer was right as given (nothing corrected, nothing repaired, and a design
  answer needed no fixing after the try-out). The only lessons trained on by default;
- silver: from a verified turn, but the platform or a fix round had to put it right; or its own checks passed and nobody
  independent looked;
- bronze: a finished turn nobody checked.

    .venv/bin/python -m app.builder.training report <folders>          how much good data there is, and whether it's enough
    .venv/bin/python -m app.builder.training <folders> --out <folder>   gold lessons as train.jsonl / valid.jsonl
    .venv/bin/python -m app.builder.training label <folder> --message 12 --bad "approvals never moved on"
    .venv/bin/python -m app.builder.training gaps <folders>             what builders couldn't make, most often first

docs/FINETUNE.md says when there's enough to train on, and how to tell whether training helped.
"""
import logging
import argparse
import json
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from ..core.settings import settings

_log = logging.getLogger(__name__)


KINDS = ("design", "review", "fix", "examples",  # the first builder's
         "v2_understand", "v2_data", "v2_behaviour", "v2_checks", "v2_fix", "v2_repair", "v2_discover", "v2_learn")  # Keel v2's, in the Keel language
GRADES = ("gold", "silver", "bronze")
# Enough to be worth training on (docs/FINETUNE.md): fewer, or from too few businesses, and the model learns the
# businesses rather than the platform.
NEEDED = {"gold": 300, "design": 150, "businesses": 40}


def keep(message, calls):
    """Append a finished turn's calls and how it went."""
    if not calls or not settings().get("TRAINING_LOG", True):
        return
    from ..models import Workspace
    from . import checking
    from ..extensions import db
    checks = message.checks or {}
    examples = checks.get("examples") or []
    workspace = db.session.get(Workspace, message.workspace_id)
    outcome = {"status": message.status, "changes": len(message.changes or []), "tried": bool(checks),
               "examples": [sum(1 for x in examples if x.get("ok")), len(examples)], "issues_left": len(checks.get("issues") or []),
               "fixes": checks.get("fixes", 0), "first": checks.get("first"), "build": not message.version_before,
               "left_out": sum(1 for p in message.progress or [] if str(p).startswith(("Couldn't fit", "Didn't fit")))}
    folder = Path(settings()["DATA_DIR"]) / "training"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        line = {"at": now.isoformat(timespec="seconds"), "workspace": message.workspace_id, "message": message.id,
                "business": (workspace.name if workspace else None), "platform": checking.platform_version(),
                "outcome": outcome, "calls": [c for c in calls if c.get("kind") in KINDS]}
        with open(folder / f"builder-{now:%Y-%m}.jsonl", "a", encoding="utf-8") as out:
            out.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
    except OSError:
        _log.warning("Couldn't keep builder turn %s for training", message.id, exc_info=True)


def verdict(folder, ok, why="", by="", workspace=None, message=None, checks=None):
    """Something independent of the builder says whether an app (every turn of `workspace`) or one turn (`message`) is
    right: the owner's own checks, an eval's stories, a person who used it."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    line = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "workspace": workspace, "message": message,
            "ok": bool(ok), "why": why, "by": by, "checks": checks or []}
    with open(folder / "verdicts.jsonl", "a", encoding="utf-8") as out:
        out.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")


def good(outcome):
    """A turn whose own checks passed: done, tried out, every example right, nothing left to fix or left out."""
    passed, total = (outcome.get("examples") or [0, 0])[:2]
    return outcome.get("status") == "done" and outcome.get("tried") and passed == total and not outcome.get("issues_left") \
        and not outcome.get("left_out")


def right_first_time(outcome):
    """The try-out found nothing wrong and every example was right before anything was fixed."""
    first = outcome.get("first") or {}
    ok, n = (first.get("examples") or [0, -1])[:2]
    return bool(first) and not first.get("wrong") and ok == n


def turns(paths):
    """Every kept turn, with the folder it came from and that folder's verdicts."""
    for path in paths:
        path = Path(path)
        for file in sorted(path.rglob("builder-*.jsonl")) if path.is_dir() else [path]:
            said = _verdicts(file.parent)
            with open(file, encoding="utf-8") as lines:
                for line in lines:
                    if line.strip():
                        turn = json.loads(line)
                        turn["_folder"] = str(file.parent)
                        turn["_verdicts"] = said.get(("message", turn.get("message")), []) + said.get(("workspace", turn.get("workspace")), [])
                        yield turn


def _verdicts(folder):
    out = {}
    path = Path(folder) / "verdicts.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                v = json.loads(line)
                key = ("message", v["message"]) if v.get("message") is not None else ("workspace", v.get("workspace"))
                out.setdefault(key, []).append(v)
    return out


def turn_grade(turn):
    """verified | own (only its own checks passed) | done (nobody checked) | wrong (someone found it wrong) | failed."""
    said, outcome = turn.get("_verdicts") or [], turn["outcome"]
    if any(not v["ok"] for v in said):
        return "wrong"
    if outcome.get("status") != "done":
        return "failed"
    if good(outcome):
        return "verified" if any(v["ok"] for v in said) else "own"
    return "done"


def call_grade(call, turn):
    if not (call.get("applied") and call.get("content")):
        return None
    grade = turn_grade(turn)
    if grade == "verified":
        if call.get("corrected") or call.get("repair"):
            return "silver"
        if call.get("kind") in ("design", "review") and not right_first_time(turn["outcome"]):
            return "silver"  # fitted, but the try-out found it needed fixing
        return "gold"
    return {"own": "silver", "done": "bronze"}.get(grade)


def examples(paths, grade="gold"):
    """The fine-tuning examples at this grade or better."""
    allowed = GRADES[:GRADES.index(grade) + 1]
    for turn in turns(paths):
        for call in turn["calls"]:
            if call_grade(call, turn) in allowed:
                yield {"messages": call["messages"] + [{"role": "assistant", "content": call["content"]}], "kind": call.get("kind"),
                       "business": _business(turn)}


def _business(turn):
    return turn.get("business") or f"{turn['_folder']}#{turn.get('workspace')}"


def why_not(turn):
    """Why a turn isn't verified, in a few words."""
    o, said = turn["outcome"], turn.get("_verdicts") or []
    if any(not v["ok"] for v in said):
        return "found wrong: " + next((v["why"] or v["by"] for v in said if not v["ok"]), "")[:60]
    if o.get("status") != "done":
        return f"turn {o.get('status')}"
    if not o.get("tried"):
        return "not tried out"
    if o.get("issues_left"):
        return "try-out found things to fix"
    passed, total = (o.get("examples") or [0, 0])[:2]
    if passed != total:
        return "its own examples failed"
    if o.get("left_out"):
        return "parts didn't fit"
    return "nobody independent checked it"


def report(paths):
    """How much good data there is, why the rest isn't, and whether it's enough to train on."""
    all_turns = list(turns(paths))
    grades, kinds, businesses, gold_businesses = Counter(), {g: Counter() for g in GRADES}, set(), set()
    reasons, turn_grades, first = Counter(), Counter(), Counter()
    for turn in all_turns:
        businesses.add(_business(turn))
        g = turn_grade(turn)
        turn_grades[g] += 1
        if g != "verified":
            reasons[why_not(turn)] += 1
        if turn["outcome"].get("tried"):
            first["right" if right_first_time(turn["outcome"]) else "not"] += 1
        for call in turn["calls"]:
            cg = call_grade(call, turn)
            if cg:
                grades[cg] += 1
                kinds[cg][call.get("kind")] += 1
                if cg == "gold":
                    gold_businesses.add(_business(turn))
    lines = [f"{len(all_turns)} builder turns from {len(businesses)} businesses",
             f"  turns: verified {turn_grades['verified']}, own checks only {turn_grades['own']}, unchecked {turn_grades['done']}, "
             f"found wrong {turn_grades['wrong']}, didn't finish {turn_grades['failed']}"]
    if first:
        lines.append(f"  first draft right, before its own fixes: {first['right']} of {first['right'] + first['not']} tried turns")
    for g in GRADES:
        lines.append(f"  {g:6} {grades[g]:4} lessons" + (f" ({', '.join(f'{k} {n}' for k, n in sorted(kinds[g].items()))})" if grades[g] else ""))
    if reasons:
        lines.append("  why turns weren't verified: " + "; ".join(f"{r} ({n})" for r, n in reasons.most_common(6)))
    have = {"gold": grades["gold"], "design": kinds["gold"]["design"], "businesses": len(gold_businesses)}
    short = [f"{label} {have[k]} of {NEEDED[k]}" for k, label in (("gold", "gold lessons"), ("design", "gold design lessons"),
                                                                     ("businesses", "businesses with gold lessons")) if have[k] < NEEDED[k]]
    lines.append("Ready to try fine-tuning: " + ("yes (docs/FINETUNE.md step 3; switch only if the held-out eval says it's better)"
                                                 if not short else "not yet: " + ", ".join(short) + ". Keep collecting (evals/collect.py)."))
    return "\n".join(lines)


def gaps(paths):
    """What builders couldn't make, or made wrong, most often first: [(need, how many times, what the platform said)]."""
    from collections import Counter
    seen, said = Counter(), {}
    for folder in paths:
        for path in Path(folder).rglob("gaps.jsonl"):
            for line in path.open():
                try:
                    x = json.loads(line)
                except ValueError:
                    continue
                key = str(x.get("need") or "").strip().lower()
                if key:
                    seen[key] += 1
                    said.setdefault(key, (x.get("refused") or []) + (x.get("failing") or []))
    return [(need, n, said[need][:3]) for need, n in seen.most_common()]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["report"]:
        print(report(argv[1:] or ["data/training"]))
        return 0
    if argv[:1] == ["gaps"]:  # what builders couldn't make, most often first: what the language should learn to say next
        for need, n, said in gaps(argv[1:] or ["data/training"]):
            print(f"{n:3}  {need}" + "".join(f"\n       {x[:200]}" for x in said))
        return 0
    if argv[:1] == ["label"]:
        parser = argparse.ArgumentParser(description="Say whether an app or a turn is right, as someone who used it.")
        parser.add_argument("folder", help="the training folder the turn is kept in")
        parser.add_argument("--message", type=int)
        parser.add_argument("--workspace", type=int)
        parser.add_argument("--bad", help="it's wrong: why")
        parser.add_argument("--ok", action="store_true", help="it's right")
        parser.add_argument("--by", default="a person")
        args = parser.parse_args(argv[1:])
        if not (args.message or args.workspace) or bool(args.bad) == args.ok:
            parser.error("give --message or --workspace, and one of --ok or --bad")
        verdict(args.folder, args.ok, args.bad or "", args.by, workspace=args.workspace, message=args.message)
        return 0
    parser = argparse.ArgumentParser(description="Export the builder's best answers as a fine-tuning set.")
    parser.add_argument("paths", nargs="+", help="training folders or .jsonl files")
    parser.add_argument("--out", required=True, help="a folder: train.jsonl and valid.jsonl go in it")
    parser.add_argument("--grade", choices=GRADES, default="gold", help="the lowest grade of lesson to include (gold: only verified, right as given)")
    parser.add_argument("--loose", action="store_true", help="the same as --grade bronze")
    parser.add_argument("--valid", type=float, default=0.1, help="share kept apart to check the training on")
    args = parser.parse_args(argv)
    rows = list(examples(args.paths, "bronze" if args.loose else args.grade))
    random.Random(7).shuffle(rows)
    cut = max(1, int(len(rows) * args.valid)) if len(rows) > 1 else 0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, part in (("train", rows[cut:]), ("valid", rows[:cut])):
        with open(out / f"{name}.jsonl", "w", encoding="utf-8") as fh:
            for row in part:
                fh.write(json.dumps({"messages": row["messages"]}, ensure_ascii=False) + "\n")
    kinds = Counter(row["kind"] for row in rows)
    print(f"{len(rows)} examples ({', '.join(f'{k} {n}' for k, n in sorted(kinds.items())) or 'none'}) from "
          f"{len({row['business'] for row in rows})} businesses → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
