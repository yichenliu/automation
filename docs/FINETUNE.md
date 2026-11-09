# Fine-tuning the builder's model

The builder runs on a local model (`qwen3.8:27b-dflash2-nvfp4` in Ollama: qwen3.8 27B with a DFlash2 drafter for faster writing). Fine-tuning teaches that model this platform's own way of
building: the kit, the shapes of a good design, and the mistakes the try-out keeps catching. It uses the builder's own
best work as the lessons.

## 1. Collect good lessons first, and only train when there are enough

Fine-tuning is only worth doing on lessons we trust. Every builder turn is kept in `data/training/builder-YYYY-MM.jsonl`
(each conversation with the model, and how its answer turned out), and graded (`app/builder/training.py`):

| Grade | What it means | Trained on |
|---|---|---|
| gold | Something independent of the builder confirmed the app (the owner's own checks, written from what they said before the build; an eval story; a person who used it), and the model's answer was right as given: nothing the platform had to correct, nothing repaired, no fixing after the try-out | yes |
| silver | Verified, but the platform or a fix round had to put the answer right; or only the builder's own examples passed | no (`--grade silver` to look) |
| bronze | Finished, nobody checked | no |

The builder's own examples passing isn't enough: the beginner testers found apps with green checks that failed in real
use. Anyone's "this is wrong" outweighs every "yes", so the testers' findings are recorded too:

    .venv/bin/python -m app.builder.training label <data>/training --workspace 1 --bad "approvals never moved on"

**Collect** by building businesses the builder has never seen, in batches, overnight. A third of their owners start
vague; every owner answers the builder's questions and writes three checks of their own. The fixed scenarios are never
collected from: they stay the exam.

    .venv/bin/python -m evals.collect --out ~/keel-lessons

**See where it's got to**, at any time:

    .venv/bin/python -m app.builder.training report ~/keel-lessons data/training

It says how many lessons there are of each grade, why the rest weren't verified, how often the builder is right first
time, which model wrote the gold lessons, and whether there's enough. Every eval round's lessons are collected in
`data/training/evals/<round>/<app>` too (or `TRAINING_DIR`), whatever folder the round itself wrote to, so
`report data/training` counts them all.

Each answer says which answers went into the app and which the platform or a repair had to put right; that's what the
grade is read from. (Keel's builder marked none until November 2026, so every lesson of its first 45 eval rounds counted
as nothing; turns kept before then are marked from what they show: `_inferred` in `app/builder/training.py`.)

**A cloud model's answers.** When Settings › AI sends the builder to a cloud provider, the evals build with it too and
its answers are kept like any other, each with its provider and model. Its right answers are good lessons for the
platform's own model (distillation), but some providers' terms don't allow training another model on what theirs
wrote: check them, or export one model's lessons only with `--by` (`--by ollama`).

**Enough** is 300 gold lessons, 150 of them designs, from at least 40 different businesses. Fewer, or from too few
businesses, and the model learns those businesses rather than the platform. Until then, keep collecting, and keep
running the beginner testers (`evals/beginner.md`): each wave finds checks that passed when they shouldn't have, and
fixing those is what makes "gold" mean right.

## 2. Export the gold lessons

    .venv/bin/python -m app.builder.training ~/keel-lessons data/training --out ~/keel-finetune/data

This writes `train.jsonl` and `valid.jsonl` (10% kept back to check the training), in the chat format MLX-LM, Unsloth
and Axolotl read.

## 3. Train a LoRA adapter on this Mac (MLX)

A 64 GB M4 Pro fits a 4-bit 27B model with LoRA. Use the MLX copy of the same Qwen model the builder runs (search
Hugging Face for `mlx-community` and the model's name, 4-bit):

    python3 -m venv ~/keel-finetune/venv && ~/keel-finetune/venv/bin/pip install mlx-lm
    ~/keel-finetune/venv/bin/mlx_lm.lora --model mlx-community/<the Qwen 27B, 4-bit> --train \
        --data ~/keel-finetune/data --adapter-path ~/keel-finetune/adapters \
        --batch-size 1 --num-layers 16 --iters 1000 --learning-rate 1e-5 \
        --max-seq-length 16384 --grad-checkpoint --mask-prompt

- `--mask-prompt` trains on the answers only, not on the long instructions and design outline before them.
- The builder's prompts are long (the instructions plus the app's outline run to 8–12k tokens), so expect slow training:
  hours per pass over a few hundred lessons. Leave it running overnight.
- Watch the validation loss it prints. If it starts rising, stop: the model is memorising instead of learning.

## 4. Put it into Ollama

Fuse the adapter into the model, convert it to GGUF with llama.cpp, then make an Ollama model that uses the same chat
template as the original:

    ~/keel-finetune/venv/bin/mlx_lm.fuse --model mlx-community/<same model> --adapter-path ~/keel-finetune/adapters \
        --save-path ~/keel-finetune/fused --dequantize
    python llama.cpp/convert_hf_to_gguf.py ~/keel-finetune/fused --outfile ~/keel-finetune/keel-builder.gguf
    llama.cpp/build/bin/llama-quantize ~/keel-finetune/keel-builder.gguf ~/keel-finetune/keel-builder-q4.gguf Q4_K_M
    ollama show qwen3.8:27b-dflash2-nvfp4 --modelfile > ~/keel-finetune/Modelfile   # then change its FROM line to the q4 file
    ollama create keel-builder -f ~/keel-finetune/Modelfile

## 5. Switch only if it's better

Measure on the fixed scenarios, which were never trained on (the held-out exam), 3 runs each since the model varies.
Switch only if right when handed over goes up clearly (10 points or more) and stories passed, kept working and things
found wrong are no worse; otherwise the training didn't help, and the lessons go back to collecting.

    OLLAMA_MODEL=keel-builder .venv/bin/python -m evals.analyst --out ~/keel-evals/finetuned --repeat 3
    .venv/bin/python -m evals.analyst --out ~/keel-evals/base --repeat 3

Switch the app (`OLLAMA_MODEL=keel-builder` in `.env`) only when the fine-tuned model passes more stories, has fewer
things wrong when tried out, and isn't slower. Keep the base model installed so you can go back.

## Why this works

Only answers that were right as given, in apps something independent confirmed, are learned from, so the model learns
to get it right first time rather than to lean on the platform's corrections and fix rounds. The owner's checks and the
try-out find the good work without anyone checking by hand; the beginner testers keep those checks honest. Each round
of collecting and training starts from a better model, which builds more good work to learn from next time, and the
held-out exam says plainly whether it did.
