"""Collecting lessons to fine-tune the builder's model on: new businesses built in batches with the local model (a third
of their owners vague, every owner answering the builder's questions), each checked against its owner's own checks, and
how much good data there is printed after each batch. Runs until stopped (Ctrl-C), or for --batches.

    .venv/bin/python -m evals.collect --out ~/keel-lessons                 overnight
    .venv/bin/python -m app.builder.training report ~/keel-lessons         where it's got to

The fixed scenarios (evals/scenarios.py) are never collected from: they stay the exam a fine-tuned model is judged on.
"""
import argparse
import sys
import time
from pathlib import Path

from app.builder import training

from . import analyst


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build new businesses in batches to collect verified lessons.")
    parser.add_argument("--out", required=True, help="a folder: each batch goes in its own folder inside it")
    parser.add_argument("--batch", type=int, default=4, help="businesses per batch")
    parser.add_argument("--batches", type=int, default=0, help="how many batches (0: until stopped)")
    parser.add_argument("--seed", type=int, help="the first batch's seed (each batch after takes the next)")
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    seed = args.seed if args.seed is not None else int(time.time()) % 100000
    done = 0
    while not args.batches or done < args.batches:
        analyst.main(["--out", str(out / f"batch-{seed}"), "--fresh", str(args.batch), "--seed", str(seed)])
        print("\n" + training.report([out]), flush=True)
        seed, done = seed + 1, done + 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
