"""The registry: every piece of the Keel language, in one place, so what the AI is told can never fall behind what the
platform can do.

Each piece registers itself with a kind (field, when, step, patch, feature), its model (its shape and docstring) and a
short example. From here, on every run, come:
- the catalog in the builder's instructions (catalog());
- the output schema the model is held to (compile.patch_type and the unions below);
- the tool schemas;
- a fingerprint that changes whenever any of it does.

Adding a field kind, a step or a feature means writing its class with @register: the AI knows it from the next turn.
tests/test_lang.py fails for a piece with no docstring or example, an example that doesn't validate or compile, or a
feature whose own checks don't pass in the simulator.
"""
import hashlib
import json
from typing import Annotated, Union

from pydantic import Field

KINDS = ("field", "when", "step", "patch", "feature")
_ITEMS = {k: [] for k in KINDS}
TITLES = {"field": "FIELDS (in a list's \"fields\", by \"type\")",
          "when": "WHEN a rule starts (its \"when\", by \"on\")",
          "step": "STEPS a rule takes (in its \"steps\", by \"do\")",
          "patch": "PATCHES you send (each by \"do\")",
          "feature": "FEATURES: ready-made behaviour, each a patch (by \"do\"); use one whenever it fits, rather than rules"}
DISCRIMINATOR = {"field": "type", "when": "on", "step": "do", "patch": "do", "feature": "do"}


def register(kind, example, group=None):
    """Class decorator: this model is a piece of the language of this kind, with this example. A patch's group: "data"
    (lists, fields, stages) or "behaviour" (rules, features); a feature is always behaviour."""
    assert kind in KINDS, kind

    def wrap(cls):
        cls.__keel_kind__, cls.__keel_example__ = kind, example
        cls.__keel_group__ = "behaviour" if kind == "feature" else group
        if cls not in _ITEMS[kind]:
            _ITEMS[kind].append(cls)
        return cls
    return wrap


def items(kind, group=None):
    return tuple(c for c in _ITEMS[kind] if group is None or c.__keel_group__ == group)


def union(kind):
    members = items(kind)
    return Annotated[Union[members], Field(discriminator=DISCRIMINATOR[kind])] if len(members) > 1 else members[0]


def _names(cls, kind):
    """What a piece is called in its kind: "add_list", or "text|long_text|…" for a field kind with several types."""
    tag = cls.model_fields[DISCRIMINATOR[kind]].annotation
    args = getattr(tag, "__args__", ())
    return "|".join(str(a) for a in args) if args else str(tag)


def _fields_said(cls, kind):
    out = []
    for name, info in cls.model_fields.items():
        if name == DISCRIMINATOR[kind]:
            continue
        shown = info.alias or name
        optional = not info.is_required()
        out.append(f"{shown}{'?' if optional else ''}")
    return ", ".join(out)


def _doc(cls):
    return " ".join((cls.__doc__ or "").split())


def catalog(kinds=KINDS, examples=True):
    """The language as the AI is told it: each kind, each piece with its parts, what it does and an example."""
    blocks = []
    for kind in kinds:
        lines = [TITLES[kind] + ":"]
        for cls in items(kind):
            line = f"- {_names(cls, kind)} {{{_fields_said(cls, kind)}}}: {_doc(cls)}"
            if examples and cls.__keel_example__ is not None and kind in ("feature", "patch", "step"):
                line += f" e.g. {json.dumps(cls.__keel_example__, ensure_ascii=False)}"
            lines.append(line)
        blocks.append("\n".join(lines))
    if "field" in kinds or "step" in kinds:  # formulas are written in fields, conditions and steps
        from ..blueprint import expr
        blocks.append(expr.reference())
    return "\n\n".join(blocks)


def fingerprint():
    """Changes whenever any piece of the language does (its shape, its words, its example)."""
    from pydantic import TypeAdapter
    parts = [catalog()] + [json.dumps(TypeAdapter(union(k)).json_schema(), sort_keys=True) for k in KINDS if items(k)]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]
