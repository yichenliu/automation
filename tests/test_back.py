"""Every rule the design has reads back into the Keel language and compiles to the same rule: so the App map, the
ready-made parts and the analyst change rules in the AI's own language, and the AI reads a rule a person made by hand
in the words it writes itself."""
import copy
import json

import pytest

from app import lang
from app.blueprint import modules, schema
from app.lang import back

from .test_lang import everything


def _same(a, b):
    def norm(x):
        x = copy.deepcopy(x)
        x.pop("feature", None)
        if x.get("enabled", True):
            x.pop("enabled", None)
        return json.dumps(x, sort_keys=True)
    return norm(a) == norm(b)


def _round_trip(bp, a):
    k = back.rule_in_keel(bp, a)
    out = lang.apply(bp, [{"do": "add_rule", "rule": k}])
    assert not out.problems, (a["key"], k, out.problems)
    again = next(x for x in out.blueprint["automations"] if x["key"] == a["key"])
    assert _same(a, again), (a, again)


RULES = [
    {"key": "big_order", "name": "Big orders go the right way", "when": {"on": "changed", "list": "jobs", "field": "priority"},
     "steps": [{"do": "branch", "ways": [{"label": "Urgent", "when": [{"field": "priority", "op": "equals", "value": "Urgent"}],
                                          "steps": [{"do": "approval", "who": "owner", "message": "Rush job: OK?", "approvers": ["owner", "finance"],
                                                     "mode": "all", "not_by": ["creator"], "remind_after_days": 1, "escalate_after_days": 3,
                                                     "escalate_to": "owner", "if_rejected": [{"do": "set", "values": [{"field": "priority", "to": "Standard"}]},
                                                                                             {"do": "notify", "who": "everyone", "message": "Not a rush job after all"}]}]}],
                "otherwise": [{"do": "task", "who": "office", "message": "Book it in", "fields": ["scheduled_for"], "priority": "high",
                               "not_by": ["assigned_to"]}]}]},
    {"key": "summary_ai", "name": "The AI writes a summary", "when": {"on": "added", "list": "jobs"}, "enabled": False,
     "steps": [{"do": "ai_fill", "field": "title", "instructions": "A short title", "review": "always", "reviewer": "office",
                "reads": [{"list": "services", "where": [{"field": "price", "op": "greater_than", "value": 0}]}], "learn": False, "read_files": True}]},
    {"key": "pay_link", "name": "A payment link", "when": {"on": "reaches", "list": "invoices", "stage": "Sent"},
     "steps": [{"do": "payment_link", "amount": "balance", "save_to": "payment_link", "when_paid": [{"field": "status", "to": "Paid"}], "currency": "NZD"}]},
    {"key": "lines_on", "name": "Lines on", "when": {"on": "reaches", "list": "jobs", "stage": "Done"},
     "steps": [{"do": "add", "list": "invoices", "values": [{"field": "job", "formula": "record"}, {"field": "due_date", "formula": "today() + 14"}]},
               {"do": "document", "document": "tax_invoice", "on": "created"},
               {"do": "add_each", "list": "invoice_lines", "lines": "job_lines", "link": "job", "values": [{"field": "invoice", "formula": "created"}],
                "lines_where": ["description != ''"]},
               {"do": "update_each", "list": "job_lines", "link": "job", "values": [{"field": "description", "formula": "item.description + ' (billed)'"}]}]},
    {"key": "from_shop", "name": "Orders from the shop", "when": {"on": "webhook", "list": "orders"},
     "steps": [{"do": "notify", "who": "owner", "message": "An order came in"}]},
    {"key": "weekly", "name": "A weekly nudge", "when": {"on": "every", "period": "week", "weekday": "fri", "at": "16:00"},
     "steps": [{"do": "notify", "who": "owner", "message": "Week's end"}]},
    {"key": "bills_by_email", "name": "Bills by email", "when": {"on": "email", "subject_contains": "invoice", "with_attachment": True},
     "steps": [{"do": "ai_create", "list": "notes", "instructions": "A note of the bill", "review": "when_unsure"}]},
]


@pytest.mark.parametrize("n", range(len(RULES)))
def test_every_kind_of_rule_reads_back_and_compiles_the_same(n):
    out = lang.apply(everything(), [{"do": "add_rule", "rule": RULES[n]}])
    assert not out.problems, out.problems
    made = next(a for a in out.blueprint["automations"] if a["key"] == RULES[n]["key"])
    _round_trip(out.blueprint, made)


def test_every_rule_of_the_ready_made_parts_reads_back_the_same():
    from app.web.workspaces import with_parts
    bp = with_parts(schema.empty("Parts"), list(modules.MODULES)).blueprint
    rules = [a for a in bp["automations"] if not a.get("feature")]
    assert rules
    for a in rules:
        _round_trip(bp, a)
