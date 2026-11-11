"""Compiling Keel language patches into the design the engine runs.

Every name is resolved against the design as it stands (lists by key, label or singular; fields by key or label;
paths through links; stages and choices as the owner wrote them) before anything is saved. Each problem says which
patch, where in it, what's wrong and what's there instead, so the builder repairs that patch alone. Patches apply one
by one: a wrong one is refused, the rest still apply.

New lists are made in phases (their plain fields and stages, then links, then totals, lookups and formulas, then
their stage moves), so a batch can make lists that link to each other in any order.
"""
import ast
import contextvars
import copy
import re
from dataclasses import dataclass, field as dfield
from typing import Annotated, Union

from pydantic import Field, TypeAdapter, ValidationError

from ..blueprint import expr, ops as design_ops, schema
from . import model as m
from .design import CONTEXT_NAMES, TYPE_OF, Design, NameProblem, closest


class Problem(Exception):
    pass


def _same_check(text):
    """A rule's check (or message) as compared: its words, spaces and quotes aside ("quote.status == 'Approved'" is
    quote.status=="Approved")."""
    return re.sub(r"[\s'\"]+", "", str(text or "")).lower()


def _own_patch(name):
    """For a design_op naming what the language has a patch for (set_stages, add_list) or nearly (update_stages): that
    patch, sent as it is, with its example, so the next try is the patch itself rather than another wrapping of it."""
    import difflib
    import json
    from . import registry
    pieces = {str(c.__keel_example__.get("do")): c for kind in ("patch", "feature") for c in registry.items(kind)
              if isinstance(getattr(c, "__keel_example__", None), dict)}
    found = pieces.get(name) or pieces.get(next(iter(difflib.get_close_matches(name, list(pieces), n=1, cutoff=0.6)), ""))
    if found is None:
        return "lists, fields, stages and rules are made with add_list, add_field, set_stages, add_rule or a feature"
    do = found.__keel_example__["do"]
    said = "is a patch of its own" if do == name else f"isn't one; the patch for it is “{do}”"
    return f"“{name}” {said}: send it by itself (not inside a design_op), as {json.dumps(found.__keel_example__, ensure_ascii=False)}"


@dataclass
class Outcome:
    blueprint: dict
    changes: list = dfield(default_factory=list)
    problems: list = dfield(default_factory=list)  # "patch 3 (add_rule “Chase…”) › step 2: …"
    refused: list = dfield(default_factory=list)  # the numbers of the patches refused
    features: list = dfield(default_factory=list)  # keys of the features made or changed
    data: list = dfield(default_factory=list)  # records to add and files to bring in, done once the design is saved
    navigate: str | None = None


# --- parsing --------------------------------------------------------------------------------------------------------

def kinds(behaviour=True, data=True):
    """The patches a builder may send, from the registry: data (lists, fields, stages), behaviour (features, rules)."""
    from . import patterns  # noqa: F401 (the features register themselves)
    from .registry import items
    return ((items("patch", "data") if data else ()) + (items("feature") if behaviour else ())
            + (items("patch", "behaviour") if behaviour else ()))


def patch_type(allowed=None):
    allowed = allowed or kinds()
    return Annotated[Union[allowed], Field(discriminator="do")]


def _said(exc):
    out = []
    for e in exc.errors()[:4]:
        where = ".".join(str(x) for x in e["loc"] if not isinstance(x, str) or not x[0].isupper())
        out.append(f"{where or 'it'}: {e['msg']}")
    return "; ".join(out)


def _unwrapped(raw, adapter):
    """A patch of the language sent inside a design_op ({"do": "design_op", "op": {"op": "add_view", …}}): the patch
    itself, when that's what it is. Otherwise as sent (a design_op for what only it makes, or the hint at the patch)."""
    if not (isinstance(raw, dict) and raw.get("do") == "design_op" and isinstance(raw.get("op"), dict)):
        return raw
    name = schema.slug(str(raw["op"].get("op") or ""))
    if not name or name in ESCAPE:
        return raw
    inner = {"do": name, **{k: v for k, v in raw["op"].items() if k != "op"}}
    try:
        adapter.validate_python(inner)
    except ValidationError:
        return raw
    return inner


def parse(raw_patches, allowed=None):
    """The patches as models, and a problem for each one that isn't a patch of the language."""
    adapter = TypeAdapter(patch_type(allowed))
    parsed, problems = [], []
    for n, raw in enumerate(raw_patches or [], start=1):
        raw = _unwrapped(raw, adapter)
        what = raw.get("do") if isinstance(raw, dict) else None
        try:
            parsed.append((n, adapter.validate_python(raw)))
        except ValidationError as exc:
            problems.append(f"patch {n} ({what or 'no do'}): {_said(exc)}")
    return parsed, problems


# --- the parts: fields, conditions, values, rules ---------------------------------------------------------------------

def _list(d, name, where):
    t = d.list(name)
    if t is None:
        near = closest(name, d.list_names())
        raise Problem(f"{where}: there's no list “{name}”" + (f" (did you mean {', '.join(near)}?)" if near else "")
                      + f"; the lists: {', '.join(d.list_names()) or 'none yet'}")
    return t


ROLE_WORDS = {"admins": "admin", "owners": "owner", "editors": "editor", "viewers": "viewer", "staff": "editor", "managers": "admin",
              "manager": "admin", "everyone": "viewer"}


def _role_or_team(d, name, where):
    """A role (viewer, editor, admin, owner) or a team's key, as who may see or change something."""
    word = ROLE_WORDS.get(schema.slug(name), schema.slug(name))
    if word in schema.ROLE_NAMES or word in d.teams():
        return word
    raise Problem(f"{where}: “{name}” isn't a role (viewer, editor, admin, owner) or a team"
                  + (f" ({', '.join(d.teams())})" if d.teams() else " (add one first with set_team)"))


def ir_field(d, t, f, where):
    """The design's field for a language field on list t (t may be the list being made)."""
    out = _ir_field(d, t, f, where)
    for attr in ("show_if", "required_if"):
        conditions = getattr(f, attr, None)
        if conditions:
            if not t.get("fields"):  # a list being made: its own fields aren't there to check against yet, kept as written
                out[attr] = [_as_written(c) for c in conditions]
            else:
                out[attr] = ir_conditions(d, t, conditions, f"{where} › {attr}")
    return out


def _as_written(c):
    """A condition as it's written (a formula, a group, or a field's test), for a list whose fields aren't made yet."""
    if isinstance(c, str):
        return {"formula": c.strip()}
    if c.any is not None or c.all is not None:
        word = "any" if c.any is not None else "all"
        return {word: [_as_written(x) for x in (c.any if c.any is not None else c.all)]}
    return {"field": c.field, "op": c.op, **({"value": c.value} if c.value is not None else {})}


def _ir_field(d, t, f, where):
    out = {"key": schema.slug(f.key), "label": f.label}
    if isinstance(f, m.Plain):
        out["type"] = TYPE_OF.get(f.type, f.type)
        for k in ("required", "unique"):
            if getattr(f, k):
                out[k] = True
        if f.default is not None:
            out["default"] = f.default
        if f.help:
            out["help"] = f.help
        if f.sensitive:
            out["sensitive"] = True
            if f.shows_last is not None:
                out["shows_last"] = f.shows_last
        if f.many and f.type == "person":
            out["multiple"] = True
        elif f.many:
            raise Problem(f"{where}: only a person field (or a link) can hold several: “{f.key}” is {f.type}")
        if f.visible_to:
            out["access"] = _role_or_team(d, f.visible_to, f"{where} › visible_to")
    elif isinstance(f, m.Choice):
        out.update(type=TYPE_OF[f.type], options=[str(o).strip() for o in f.options if str(o).strip()])
        if not out["options"]:
            raise Problem(f"{where}: the choice field “{f.key}” needs its choices, in the owner's words")
        if f.default:
            match = next((o for o in out["options"] if o.lower() == f.default.lower()), None)
            if match is None:
                raise Problem(f"{where}: the default “{f.default}” isn't one of {f.key}'s choices ({', '.join(out['options'])})")
            out["default"] = match
        if f.required:
            out["required"] = True
        if f.help:
            out["help"] = f.help
        if f.visible_to:
            out["access"] = _role_or_team(d, f.visible_to, f"{where} › visible_to")
    elif isinstance(f, m.Link):
        target = _list(d, f.to, where)
        out.update(type="link", table=target["key"])
        if f.many:
            out["multiple"] = True
        if f.required:
            out["required"] = True
        if f.default:  # {{tender.buyer}}: read from the new record, so its path must be one there; or a record by its
            inner = re.fullmatch(r"\s*\{\{\s*(.+?)\s*\}\}\s*", f.default)  # name or code (EA: a unit of measure)
            if inner is not None:
                _check_formula(d, t, inner.group(1), f"{where} › default")
            elif "{{" in f.default:
                raise Problem(f"{where} › default: a link's default is a record of {target['label']} by its name or code (EA), or "
                              f"read from the new record, as {{{{tender.buyer}}}}")
            out["default"] = f.default.strip()
    elif isinstance(f, m.AutoNumber):
        out.update(type="autonumber", prefix=f.prefix)
    elif isinstance(f, m.Formula):
        _check_formula(d, t, f.formula, f"{where} › formula of {f.key}")
        written = schema.percents_fixed(t, f.formula, {x["key"]: x for x in d.bp.get("tables", [])})  # a percentage is kept as a fraction
        if f.shows == "percent":
            written = design_ops._as_fraction(written)  # have / par * 100 shown as a percentage: have / par
        same = next((x for x in (t or {}).get("fields", []) if x.get("type") == "formula" and x["key"] != schema.slug(f.key)
                     and re.sub(r"\s+", "", x.get("expression") or "") == re.sub(r"\s+", "", written)), None)
        if same is not None:
            raise Problem(f"{where}: {same['key']} works out exactly this already ({same.get('expression')}): use it, or say what's "
                          f"different")
        out.update(type="formula", expression=written, format={"yes_no": "checkbox"}.get(f.shows, f.shows))
        if schema.dated_formula(t, out):  # a date worked out (confirmed_at + 2h) shows as one, whatever "shows" was left at
            out["format"] = "date"
    elif isinstance(f, m.Total):
        source = _list(d, f.of, where)
        link = d.field(source, f.link)
        if link is None or link.get("type") != "link" or link.get("table") != t["key"]:
            links = [x["key"] for x in source["fields"] if x["type"] == "link" and x.get("table") == t["key"]]
            raise Problem(f"{where}: the total “{f.key}” needs “link”: {source['key']}'s link field to {t['key']}"
                          + (f" ({', '.join(links)})" if links else f" — {source['key']} has none: add one first"))
        out.update(type="rollup", table=source["key"], via=link["key"], aggregate=f.how)
        if f.how != "count":
            if not f.field:
                raise Problem(f"{where}: the total “{f.key}” needs the field to {f.how} (a number field of {source['key']})")
            try:
                inner, _, keys = d.path(source, f.field)
            except NameProblem as exc:
                raise Problem(f"{where} › field: {exc}") from None
            if d.kind(inner) != "number":
                raise Problem(f"{where}: {source['key']}.{f.field} isn't a number, so it can't be {f.how}med")
            out["field"] = ".".join(keys)
        if f.only:
            out["filters"] = ir_conditions(d, source, f.only, f"{where} › only")
    elif isinstance(f, m.Lookup):
        try:
            inner, _, keys = d.path(t, f.path)
        except NameProblem as exc:
            raise Problem(f"{where} › path: {exc}") from None
        if len(keys) < 2:
            raise Problem(f"{where}: a lookup reads a linked record's field, such as customer.email")
        out.update(type="lookup", link=keys[0], field=".".join(keys[1:]))
    return out


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _paths(source):
    tree = expr.parse(source)
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    inner = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.Attribute, ast.Name)) and id(n) not in called and id(n) not in inner:
            path = _dotted(n)
            if path:
                out.append(path)
    return out


# what a formula can't say: SQL, lookups by text
OVER_OTHERS = re.compile(r"\b(select|lookup|vlookup|xlookup|index|match)\s*\(|\bselect\b.+\bfrom\b", re.I)
OTHER_RECORDS = ("; to look at other records, a formula names them: the records linking to this one through their "
                 "link (client.invoices: a client's invoices; job.payments), or a whole list (bookings), with count, sum, "
                 "any, all, none, min, max, avg and where: count(client.invoices where status = \"Unpaid\" and "
                 "days_since(due_date) > 30) == 0; sum(job.payments, amount); not any(bookings where stylist == this.stylist "
                 "and id != this.id and overlaps(starts_at, ends_at, this.starts_at, this.ends_at)). Inside them, a name is "
                 "the field of the record looked at, and this.x the formula's own record's")
QUERY_NAMES = {name for name, to in expr.ALIASES.items() if to in expr.QUERIES} | expr.QUERIES


def _scoped_paths(tree, compared=None):
    """(path, scopes) for each name a formula reads. scopes: the records looked at by each count(...), sum(...), any(...)
    it's inside, innermost first (their expressions), so a name there is read on those records. `compared`: a list that
    gets (path, scopes, the words it's compared with) for each name compared with words (status == "Paid")."""
    out = []

    def walk(node, scopes):
        if compared is not None and isinstance(node, ast.Compare):
            sides = [node.left] + list(node.comparators)
            for (a, b) in zip(sides, sides[1:]):
                for name, said in ((a, b), (b, a)):
                    path = _dotted(name) if isinstance(name, (ast.Name, ast.Attribute)) else None
                    words = [said.value] if isinstance(said, ast.Constant) and isinstance(said.value, str) else \
                        [e.value for e in said.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)] \
                        if isinstance(said, (ast.List, ast.Tuple)) else []
                    if path and words:
                        compared.append((path, scopes, words))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.lower() in QUERY_NAMES and node.args:
            walk(node.args[0], scopes)
            for a in node.args[1:]:
                walk(a, [node.args[0]] + scopes)
            return
        if isinstance(node, (ast.Attribute, ast.Name)):
            path = _dotted(node)
            if path:
                out.append((path, scopes))
                return
        for child in ast.iter_child_nodes(node):
            if isinstance(node, ast.Call) and child is node.func:
                continue
            walk(child, scopes)
    walk(tree.body if isinstance(tree, ast.Expression) else tree, [])
    return out


def _set_list(d, t, node, scopes):
    """The list of the records a count(...)'s first part looks at (client.invoices, where(bookings, ...)), or None."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.lower() in ("where", "filter") and node.args:
        return _set_list(d, t, node.args[0], scopes)
    path = _dotted(node)
    if not path:
        return None
    for here in [_set_list(d, t, s, scopes[i + 1:]) for i, s in enumerate(scopes)] + [t]:
        if here is None:
            continue
        try:
            f, reached, many = d.reach(here, path)
        except NameProblem:
            continue
        if many:
            return reached
        if f is not None and f.get("type") == "link":  # the records it links to (one, or several)
            return d.list(f.get("table"))
    return None


def _check_formula(d, t, source, where, item=None, created=None, about=None):
    """Every name a formula reads is a field (or a linked one) of the record it's worked out on, or of the records a
    count(...), sum(...) or any(...) looks at (those linking to it, or a whole list). about: the list "record" names
    when the formula is about other records (an AI step's reads: record is the rule's record)."""
    compared = []
    try:
        tree = expr.parse(source)
        found = _scoped_paths(tree, compared)
        unknown = expr.unknown_calls(source)
    except expr.ExprError as exc:
        raise Problem(f"{where}: {exc}" + (OTHER_RECORDS if OVER_OTHERS.search(str(source)) or "where" in str(source).lower() else "")) from None
    if unknown:
        raise Problem(f"{where}: there's no function {', '.join(unknown)} (functions: {', '.join(sorted(expr.KNOWN))[:600]})"
                      + (OTHER_RECORDS if OVER_OTHERS.search(str(source)) else ""))
    for path, scopes in found:
        head, _, rest = path.partition(".")
        if head in ("item",) and item is not None and not scopes:
            if rest:
                try:
                    d.path(item, rest)
                except NameProblem as exc:
                    raise Problem(f"{where}: item.{rest}: {exc}") from None
            continue
        if head == "created" and created is not None:
            if rest:
                try:
                    d.path(created, rest)
                except NameProblem as exc:
                    raise Problem(f"{where}: created.{rest}: {exc}") from None
            continue
        if head == "record" and about is not None and not scopes:  # the rule's record, not the one being looked at
            if rest:
                try:
                    d.path(about, rest)
                except NameProblem as exc:
                    raise Problem(f"{where}: record.{rest}: {exc}") from None
            continue
        if head in ("record", "this") or (t is not None and not scopes and head == schema.slug(t.get("singular") or "")
                                          and d.field(t, head) is None):  # "client.name" in a rule about clients: the record
            if not rest:
                continue
            path, scopes = rest, []
        elif head.lower() in CONTEXT_NAMES and head not in ("item", "it"):
            if head == "owner" and t is not None and d.field(t, "owner") is not None:
                pass  # a field called owner: read as that field
            else:
                continue
        if t is None and not scopes:  # a rule started by an email or a file: there's no record yet, only what arrived
            raise Problem(f"{where}: “{path}” isn't something an email or file rule can read (it can read email.subject, "
                          f"email.body, email.sender, file.name, today())")
        looked = [_set_list(d, t, s, scopes[i + 1:]) for i, s in enumerate(scopes)]
        if head in ("item", "it") and scopes:
            if looked[0] is None or not rest:
                continue
            path, places = rest, [looked[0]]
        else:
            places = [x for x in looked if x is not None] + ([t] if t is not None else [])
        first = None
        for here in places:
            try:
                d.path(here, path)
                break
            except NameProblem:
                try:
                    d.reach(here, path)
                    break
                except NameProblem as exc:  # says the records linking there too
                    first = first or exc
        else:
            if first is not None:
                raise Problem(f"{where}: {first}") from None
    for path, scopes, words in compared:  # a choice compared with words that aren't one of its choices never holds
        f = _field_in_scope(d, t, path, scopes)
        if f is None or f.get("type") not in ("select", "multi_select") or not f.get("options"):
            continue
        options = [str(o) for o in f["options"]]
        wrong = [w for w in words if w.strip() and w.strip().lower() not in {o.lower() for o in options}]
        if wrong:
            raise Problem(f"{where}: “{wrong[0]}” isn't a choice of {path.split('.')[-1]} (its choices: {', '.join(options)})")


def _field_in_scope(d, t, path, scopes):
    """The field a name in a formula reads, where it's read (the records a count(...) looks at, then the formula's own)."""
    head, _, rest = path.partition(".")
    if head in ("record", "this"):
        path, scopes = rest, []
    looked = [_set_list(d, t, s, scopes[i + 1:]) for i, s in enumerate(scopes)]
    for here in [x for x in looked if x is not None] + ([t] if t is not None else []):
        try:
            return d.path(here, path)[0]
        except NameProblem:
            try:
                f, _, many = d.reach(here, path)
                if f is not None:
                    return f
            except NameProblem:
                continue
    return None


NEEDS_VALUE = {"equals", "not_equals", "contains", "greater_than", "less_than", "at_least", "at_most", "one_of",
               "within_next_days", "within_last_days", "more_than_days_ago", "changed_to"}
NUMBER_OPS = {"greater_than", "less_than", "at_least", "at_most"}
DATE_OPS = {"before_today", "after_today", "within_next_days", "within_last_days", "more_than_days_ago", "today", *schema.PERIOD_OPS}


def ir_conditions(d, t, conditions, where, about=None):
    return [ir_condition(d, t, c, f"{where} {i}", about=about) for i, c in enumerate(conditions, start=1)]


def ir_condition(d, t, c, where, about=None):
    """A condition read on records of t: its path resolved, its value as the field writes it. Text is a formula that
    must hold (it may look at other records: count(client.invoices where …) == 0); {"any": [...]} or {"all": [...]} a
    group of them."""
    if isinstance(c, str):
        if not c.strip():
            raise Problem(f"{where}: an empty condition")
        _check_formula(d, t, c, where, about=about)
        return {"formula": c.strip()}
    if c.any is not None or c.all is not None:
        word = "any" if c.any is not None else "all"
        parts = c.any if c.any is not None else c.all
        if not parts:
            raise Problem(f"{where}: an “{word}” group needs the conditions in it")
        return {word: [ir_condition(d, t, x, f"{where} › {word} {i}", about=about) for i, x in enumerate(parts, start=1)]}
    if not c.field or not c.op:
        raise Problem(f"{where}: a condition names a field and an op ({{\"field\": \"status\", \"op\": \"equals\", \"value\": \"Paid\"}}), "
                      f"or is a formula, or a group (any / all)")
    if c.op == "not_one_of":  # isn't any of them: each one it mustn't be
        values = [v for v in (c.value if isinstance(c.value, list) else [c.value]) if v not in (None, "")]
        if not values:
            raise Problem(f"{where}: “not_one_of” needs the values {c.field} mustn't be")
        parts = [ir_condition(d, t, m.Condition(field=c.field, op="not_equals", value=v), where, about=about) for v in values]
        return parts[0] if len(parts) == 1 else {"all": parts}
    said = schema.as_formula(c.field, c.op, c.value)
    if said:  # "field": "days_since(due_date)", "op": "greater_than", "value": 3: the formula days_since(due_date) > 3
        _check_formula(d, t, said, where)
        return {"formula": said}
    try:
        f, _, keys = d.path(t, c.field)
    except NameProblem as exc:
        raise Problem(f"{where}: {exc}") from None
    value = c.value
    if c.op in NEEDS_VALUE and value in (None, ""):
        raise Problem(f"{where}: “{c.op}” needs a value to compare {c.field} with")
    if schema.worked_out(value):  # "value": "now() - 48h": compared with what it works out to, {{now() - 48h}}
        value = schema.worked_out(value)
        _check_formula(d, t, value[2:-2], where)
        return {"field": ".".join(keys), "op": c.op, "value": value}
    kind = d.kind(f)
    if c.op in DATE_OPS and kind != "date":
        raise Problem(f"{where}: “{c.op}” is for dates, and {c.field} isn't one ({f.get('type')})")
    if c.op in NUMBER_OPS and kind not in ("number", "date"):
        raise Problem(f"{where}: “{c.op}” compares numbers or dates, and {c.field} is {f.get('type')}")
    if kind == "choice" and c.op in ("equals", "not_equals", "changed_to", "one_of", "contains") and value not in (None, ""):
        wanted = value if isinstance(value, list) else [value]
        found = []
        for w in wanted:
            o = d.choice(f, w)
            if o is None:
                hint = ""
                for link in (x for x in t["fields"] if x["type"] == "link"):
                    other = d.list(link.get("table"))
                    of = d.field(other, keys[-1]) if other else None
                    if of and d.choice(of, w):
                        hint = f" — if it means its {link['key']}'s {keys[-1]}, write {link['key']}.{keys[-1]}"
                        break
                raise Problem(f"{where}: “{w}” isn't a choice of {c.field} (its choices: {', '.join(f.get('options') or [])}){hint}")
            found.append(o)
        value = found if isinstance(value, list) else found[0]
    out = {"field": ".".join(keys), "op": c.op}
    if c.op not in schema.NO_VALUE:
        out["value"] = value
    return out


def _literal(d, f, value, where):
    if value is None:
        return None
    if isinstance(value, str) and not value.strip() or value == []:  # "" empties it (the last call-back's answer, once details change)
        return ""
    if f.get("type") == "user" and str(value).strip().lower() in ("owner", "the owner"):
        return "{{owner}}"  # the business's owner: one person, so a person field can hold them
    kind = d.kind(f)
    if kind == "choice" and f.get("type") == "select":
        o = d.choice(f, value)
        if o is None:
            raise Problem(f"{where}: “{value}” isn't a choice of {f['key']} (its choices: {', '.join(f.get('options') or [])})")
        return o
    return value


def ir_values(d, source, target, values, where, item=None, created=None):
    """{field: value} for a step that fills in fields of `target`, worked out from the rule's record (of `source`)."""
    out = {}
    for i, v in enumerate(values, start=1):
        here = f"{where} › value {i} ({v.field})"
        f = d.field(target, v.field)
        if f is None:
            near = closest(v.field, d.field_names(target))
            raise Problem(f"{here}: “{v.field}” isn't a field of {target['label']}" + (f" (did you mean {', '.join(near)}?)" if near else "")
                          + f"; its fields: {', '.join(d.field_names(target))}")
        if f["type"] in schema.CALCULATED:
            raise Problem(f"{here}: {target['key']}.{f['key']} is worked out ({f['type']}), so a step can't fill it in")
        if v.formula not in (None, ""):
            _check_formula(d, source, v.formula, here, item=item, created=created)
            out[f["key"]] = "{{" + v.formula.strip().removeprefix("{{").removesuffix("}}").strip() + "}}"
        elif v.to is not None:
            out[f["key"]] = _literal(d, f, v.to, here)
        else:
            raise Problem(f"{here}: give “to” (a value as it is) or “formula” (worked out)")
    return out


def _template(d, t, text, where):
    for m_ in expr.TEMPLATE.finditer(str(text or "")):
        _check_formula(d, t, m_.group(1), where)
    return str(text or "")


def ir_when(d, when, where):
    if isinstance(when, m.EmailIn):  # no record yet: what arrived is what the steps read
        trigger = {"type": "email_received"}
        for key in ("from_contains", "subject_contains"):
            if getattr(when, key):
                trigger[key] = getattr(when, key)
        if when.with_attachment:
            trigger["has_attachment"] = True
        return None, trigger, []
    if isinstance(when, m.FileIn):
        return None, {"type": "file_added", "folder": when.folder.strip("/ ")}, []
    if isinstance(when, m.WebhookIn):  # another system sends it: what arrived is payload.x
        if not when.list:
            return None, {"type": "webhook_received"}, []
        t = _list(d, when.list, f"{where} › when")
        return t, {"type": "webhook_received", "table": t["key"]}, []
    if isinstance(when, m.Every) and not when.list:  # on a schedule, about no one list (a weekly summary)
        trigger = {"type": "schedule", "every": when.period, "at": when.at}
        if when.period == "week":
            trigger["weekday"] = when.weekday or "mon"
        return None, trigger, []
    t = _list(d, when.list, f"{where} › when")
    trigger, conditions = {"table": t["key"]}, []
    if isinstance(when, m.Added):
        trigger["type"] = "record_created"
    elif isinstance(when, m.Changed):
        trigger["type"] = "record_updated"
        if when.field:
            f = d.field(t, when.field)
            if f is None:
                raise Problem(f"{where} › when: “{when.field}” isn't a field of {t['label']}; its fields: {', '.join(d.field_names(t))}")
            if when.to is not None:
                conditions.append({"field": f["key"], "op": "changed_to", "value": _literal(d, f, when.to, f"{where} › when")})
            else:
                conditions.append({"field": f["key"], "op": "changed"})
    elif isinstance(when, m.AddedOrChanged):
        trigger["type"] = "record_created_or_updated"
    elif isinstance(when, m.Reaches):
        f = d.stage_field(t)
        if f is None:
            raise Problem(f"{where} › when: {t['label']} has no stages, so nothing “reaches” one; give it stages first (set_stages)")
        stage = d.choice(f, when.stage)
        if stage is None:
            raise Problem(f"{where} › when: “{when.stage}” isn't a stage of {t['label']} (its stages: {', '.join(f.get('options') or [])})")
        trigger["type"] = "record_created_or_updated"
        conditions.append({"field": f["key"], "op": "changed_to", "value": stage})
    elif isinstance(when, m.Button):
        trigger.update(type="button", label=when.label[:40])
    elif isinstance(when, m.Every):
        trigger.update(type="schedule", every=when.period, at=when.at)
        if when.period == "week":
            trigger["weekday"] = when.weekday or "mon"
    elif isinstance(when, m.Deleted):
        trigger["type"] = "record_deleted"
    return t, trigger, conditions


def _target(d, t, on, where, made=None):
    """The list a step changes, and the path the design writes for it (None for the rule's record; created: the record
    an earlier step of the rule made)."""
    if on in ("record", "", None, "this", "it"):
        return t, None
    if on == "created":
        if made is None:
            raise Problem(f"{where} › on: created is the record an earlier step made, and no step before this one makes one")
        return made, "created"
    try:
        f, here, keys = d.path(t, on)
    except NameProblem as exc:
        raise Problem(f"{where} › on: {exc}") from None
    if f.get("type") != "link":
        raise Problem(f"{where} › on: {on} isn't a link of {t['label']}, so there's no record there to change")
    return d.list(f["table"]), ".".join(keys)


def _whole_list(d, t, said):
    """(a list, the condition its records must meet) when an add_each's for_each names a whole list ("customers where
    delivery_day == record.day") rather than a field of the rule's record; else None."""
    m_ = re.match(r"^\s*([A-Za-z_][\w ]*?)\s*(?:\bwhere\b\s*(.+))?$", str(said or ""), re.S | re.I)
    if not m_ or (t is not None and d.field(t, m_.group(1)) is not None and not m_.group(2)):
        return None  # a field of the record: one for each chosen in it
    each = d.list(m_.group(1).strip())
    if each is None:
        return None
    return each, (m_.group(2) or "").strip() or None


NO_RECORD = (m.ImportFile, m.SetValues, m.AddEach, m.MakeDocument, m.PaymentLink, m.Task, m.Approval, m.Wait, m.Assign, m.Allocate, m.Copy, m.Delete, m.RunRule, m.AiFill,
             m.AiCheck, m.UpdateEach)


def ir_step(d, t, s, where, made=None):
    if t is None and isinstance(s, NO_RECORD):
        raise Problem(f"{where}: a rule started by an email or a file has no record yet for “{s.do}”: make one first (ai_create or "
                      f"add), then have a rule on that list do the rest")
    only = lambda: ir_conditions(d, t, s.only_if, f"{where} › only_if") if getattr(s, "only_if", None) and t is not None else []  # noqa: E731
    if isinstance(s, m.SetValues):
        target, path = _target(d, t, s.on, where, made)
        out = {"type": "update_record", "values": ir_values(d, t, target, s.values, where, created=made)}
        if path:
            out["target"] = path
    elif isinstance(s, m.AddRecord):
        target = _list(d, s.list, where)
        out = {"type": "create_record", "table": target["key"], "values": ir_values(d, t, target, s.values, where, created=made),
               "copy_matching": s.copy_same}
    elif isinstance(s, m.AddEach) and s.for_each and _whole_list(d, t, s.for_each):  # each customer whose day is the round's
        target = _list(d, s.list, where)
        each, cond = _whole_list(d, t, s.for_each)
        conditions = ([cond] if cond else []) + list(s.lines_where)
        for i, c in enumerate(conditions, start=1):  # inside it, names are the list's fields and record.x the rule's record's
            text = c if isinstance(c, str) else schema.as_formula(c.field, c.op, c.value) if c.field and c.op else None
            if isinstance(text, str):
                _check_formula(d, t, f"count({each['key']} where {text}) >= 0", f"{where} › for_each {i}")
        out = {"type": "create_for_each", "table": target["key"], "source": {"table": each["key"], "all": True},
               "values": ir_values(d, t, target, s.values, where, item=each, created=made), "copy_matching": s.copy_same}
        rest = ir_conditions(d, each, [c for c in s.lines_where if not isinstance(c, str)], f"{where} › lines_where") if s.lines_where else []
        out["where"] = ([{"formula": cond}] if cond else []) + [{"formula": c.strip()} for c in s.lines_where if isinstance(c, str)] + rest
    elif isinstance(s, m.AddEach) and s.for_each:  # one for each one chosen in a field (each invited supplier)
        target = _list(d, s.list, where)
        f = d.field(t, s.for_each)
        if f is None or f["type"] not in ("link", "user"):
            raise Problem(f"{where} › for_each: {t['label']} has no link or person field “{s.for_each}”")
        each = d.list(f.get("table")) if f["type"] == "link" else None
        out = {"type": "create_for_each", "table": target["key"], "source": {"field": f["key"]},
               "values": ir_values(d, t, target, s.values, where, item=each, created=made), "copy_matching": s.copy_same}
        if s.lines_where and each is not None:
            out["where"] = ir_conditions(d, each, s.lines_where, f"{where} › lines_where")
    elif isinstance(s, m.AddEach):
        if not s.lines or not s.link:
            raise Problem(f"{where}: add_each needs lines and link (the lines and their link to the record), or for_each (a field)")
        target = _list(d, s.list, where)
        lines = _list(d, s.lines, f"{where} › lines")
        link = d.field(lines, s.link)
        tables = {x["key"]: x for x in d.lists}
        if link is None or link.get("type") != "link" or (link.get("table") != t["key"] and not schema.reach(t, lines, link["key"], tables)):
            links = [x["key"] for x in lines["fields"] if x["type"] == "link" and x.get("table") == t["key"]]
            raise Problem(f"{where}: “link” is {lines['key']}'s link field to {t['key']} (or to the record it links to)"
                          + (f" ({', '.join(links)})" if links else ", and it has none"))
        out = {"type": "create_for_each", "table": target["key"], "source": {"table": lines["key"], "via": link["key"], **_of(d, t, s.of, where), **_nested(lines, s.nested, where)},
               "values": ir_values(d, t, target, s.values, where, item=lines, created=made), "copy_matching": s.copy_same}
        if s.lines_where:
            out["where"] = ir_conditions(d, lines, s.lines_where, f"{where} › lines_where")
    elif isinstance(s, m.Notify):
        try:
            who = d.who(t, s.who)
        except NameProblem as exc:
            raise Problem(f"{where} › who: {exc}") from None
        out = {"type": "notify", "to": who, "message": _template(d, t, s.message, f"{where} › message")}
    elif isinstance(s, m.Email):
        to = s.to.strip()
        if "@" not in to and "{{" not in to:
            try:
                f, _, keys = d.path(t, to)
            except NameProblem as exc:
                reach = d.paths_to(t, {"email"})
                raise Problem(f"{where} › to: {exc}. The email addresses it can reach: {', '.join(reach) or 'none: add an email field'}") from None
            if f.get("type") not in ("email", "text", "lookup", "formula"):
                raise Problem(f"{where} › to: {to} isn't an email address field ({f.get('type')})"
                              + ("; to text a phone, use the text step" if f.get("type") == "phone" else ""))
            to = "{{" + ".".join(keys) + "}}"
        else:
            _template(d, t, to, f"{where} › to")
        docs = {x["key"] for x in t.get("documents", [])} | _new_documents().get(t["key"], set())
        for key in s.attach:
            if schema.slug(key) not in docs and not d.field(t, key):
                raise Problem(f"{where} › attach: {t['label']} has no document or file field “{key}” (documents: {', '.join(docs) or 'none'})")
        out = {"type": "send_email", "to": to, "subject": _template(d, t, s.subject, f"{where} › subject"),
               "body": _template(d, t, s.body, f"{where} › body")}
        if s.attach:
            out["attach"] = [schema.slug(a) for a in s.attach]
    elif isinstance(s, m.Text):
        to = s.to.strip()
        if "{{" not in to and not to.lstrip("+").replace(" ", "").isdigit():
            try:
                f, _, keys = d.path(t, to)
            except NameProblem as exc:
                reach = d.paths_to(t, {"phone"})
                raise Problem(f"{where} › to: {exc}. The phone numbers it can reach: {', '.join(reach) or 'none: add a phone field'}") from None
            if f.get("type") not in ("phone", "text", "lookup", "formula"):
                raise Problem(f"{where} › to: {to} isn't a phone number field ({f.get('type')})")
            to = "{{" + ".".join(keys) + "}}"
        else:
            _template(d, t, to, f"{where} › to")
        out = {"type": "send_sms", "to": to, "message": _template(d, t, s.message, f"{where} › message")}
    elif isinstance(s, m.MakeDocument):
        whose, path = _target(d, t, s.on, where, made)  # the rule's record's document, or the record a step made
        docs = {x["key"] for x in whose.get("documents", [])}
        if schema.slug(s.document) not in docs:  # none by that name: a standard one, laid out from the record, is made
            _new_documents().setdefault(whose["key"], set()).add(schema.slug(s.document))
        out = {"type": "generate_document", "document": schema.slug(s.document)}
        if path:
            out["target"] = path
        if s.save_to:
            f = d.field(whose, s.save_to)
            if f is None or f["type"] != "file":
                raise Problem(f"{where} › save_to: {s.save_to} isn't a file field of {whose['label']}")
            out["field"] = f["key"]
    elif isinstance(s, m.Task):
        try:
            who = d.who(t, s.who)
        except NameProblem as exc:
            raise Problem(f"{where} › who: {exc}") from None
        out = {"type": "task", "to": who, "message": _template(d, t, s.message, f"{where} › message")}
        if s.fields:
            missing = [x for x in s.fields if not d.field(t, x)]
            if missing:
                raise Problem(f"{where} › fields: {', '.join(missing)} aren't fields of {t['label']} (its fields: {', '.join(d.field_names(t))})")
            out["fields"] = [d.field(t, x)["key"] for x in s.fields]
        if s.priority:
            out["priority"] = schema.slug(s.priority)
        if s.not_by:
            out["not_by"] = _not_by(d, t, s.not_by, f"{where} › not_by")
        _follow_up(d, t, s, out, where)
    elif isinstance(s, m.Approval):
        try:
            who = d.who(t, s.who)
            several = [d.who(t, w) for w in s.approvers]
        except NameProblem as exc:
            raise Problem(f"{where} › who: {exc}") from None
        out = {"type": "request_approval", "approver": several[0] if several else who, "message": _template(d, t, s.message, f"{where} › message")}
        if len(several) > 1:
            out["approvers"], out["mode"] = list(dict.fromkeys(several)), s.mode
        if s.not_by:
            out["not_by"] = _not_by(d, t, s.not_by, f"{where} › not_by")
        if s.step_up:
            out["step_up"] = True
        _follow_up(d, t, s, out, where)
        if s.if_rejected:
            out["if_rejected"] = [ir_step(d, t, x, f"{where} › if_rejected {i}", made)[0] for i, x in enumerate(s.if_rejected, start=1)]
    elif isinstance(s, m.Branch):
        if not s.ways:
            raise Problem(f"{where}: a branch needs its ways (each: when, and its steps)")
        paths = []
        for i, way in enumerate(s.ways, start=1):  # a way with no steps yet (made a step at a time) does nothing
            paths.append({"label": way.label[:80], "when": ir_conditions(d, t, way.when, f"{where} › way {i} › when"),
                          "steps": [ir_step(d, t, x, f"{where} › way {i} › step {n}", made)[0] for n, x in enumerate(way.steps, start=1)]})
        out = {"type": "branch", "paths": paths}
        if s.otherwise:
            out["otherwise"] = [ir_step(d, t, x, f"{where} › otherwise {n}", made)[0] for n, x in enumerate(s.otherwise, start=1)]
    elif isinstance(s, m.Wait):
        out = {"type": "wait", "days": max(int(s.days), 1)}
    elif isinstance(s, m.PaymentLink):
        _check_formula(d, t, s.amount, f"{where} › amount")
        f = d.field(t, s.save_to)
        if f is None or f["type"] != "url":
            raise Problem(f"{where} › save_to: {s.save_to} isn't a web address (url) field of {t['label']}")
        out = {"type": "payment_link", "amount": s.amount, "save_to": f["key"]}
        if s.when_paid:
            out["paid"] = ir_values(d, t, t, s.when_paid, f"{where} › when_paid")
        if s.currency:
            out["currency"] = s.currency[:40]
        if s.description:
            out["description"] = s.description[:200]
    elif isinstance(s, (m.AiFill, m.AiExtract, m.AiCreate, m.AiCheck)):
        out = _ai_step(d, t, s, where)
    elif isinstance(s, m.Assign):
        f = d.field(t, s.field) if t else None
        if f is None or f["type"] != "user":
            raise Problem(f"{where} › field: {s.field} isn't a person field of {(t or {}).get('label', 'the record')}")
        among = s.among
        if among not in schema.WHO_WORDS and among not in d.teams():
            raise Problem(f"{where} › among: “{among}” isn't a role (editor, admin) or a team ({', '.join(d.teams()) or 'none'})")
        out = {"type": "assign", "field": f["key"], "from": among}
    elif isinstance(s, m.Allocate):
        f = d.field(t, s.field)
        if f is None or f["type"] != "link" or d.list(f.get("table")) is None:
            links = [x["key"] for x in t["fields"] if x["type"] == "link"]
            raise Problem(f"{where} › field: {s.field} isn't a link field of {t['label']} to the list it picks from"
                          + (f" (its links: {', '.join(links)})" if links else ": add one first (a link to the list of drivers, rooms...)"))
        if s.how_many > 1 and not f.get("multiple"):
            raise Problem(f"{where} › how_many: {f['label']} holds one {d.list(f['table'])['singular'].lower()}: pick 1, or let it hold several (multiple)")
        out = {"type": "allocate", "field": f["key"], "count": s.how_many}
        if s.among:
            out["where"] = ir_conditions(d, d.list(f["table"]), s.among, f"{where} › among")
        if s.free_on:
            df = d.field(t, s.free_on)
            if df is None or d.kind(df) != "date":
                dates = [x["key"] for x in t["fields"] if d.kind(x) == "date"]
                raise Problem(f"{where} › free_on: {s.free_on} isn't a date field of {t['label']} (its dates: {', '.join(dates) or 'none'})")
            out["free_on"] = df["key"]
    elif isinstance(s, m.UpdateEach):
        if t is None:
            raise Problem(f"{where}: update_each changes the records linked to the rule's record, and an email rule has none")
        lines = _list(d, s.list, where)
        link = d.field(lines, s.link)
        tables = {x["key"]: x for x in d.lists}
        if link is None or link.get("type") != "link" or (link.get("table") != t["key"] and not schema.reach(t, lines, link["key"], tables)):
            raise Problem(f"{where}: “link” is {lines['key']}'s link field to {t['key']} (or to the record it links to)")
        out = {"type": "update_each", "values": ir_values(d, t, lines, s.values, where, item=lines),
               "source": {"table": lines["key"], "via": link["key"], **_of(d, t, s.of, where), **_nested(lines, s.nested, where)}}
        if s.lines_where:
            out["where"] = ir_conditions(d, lines, s.lines_where, f"{where} › lines_where")
    elif isinstance(s, m.Copy):
        if t is None:
            raise Problem(f"{where}: an email rule has no record to copy")
        out = {"type": "copy_record", "values": ir_values(d, t, t, s.values, where)}
        if not s.lines:
            out["lines"] = False
    elif isinstance(s, m.Delete):
        out = {"type": "delete_record"}
    elif isinstance(s, m.RunRule):
        known = [a["key"] for a in d.bp.get("automations", [])]
        if schema.slug(s.rule) not in known:
            raise Problem(f"{where}: there's no rule “{s.rule}” (rules: {', '.join(known) or 'none'})")
        out = {"type": "run_automation", "automation": schema.slug(s.rule)}
    elif isinstance(s, m.ImportFile):
        f = d.field(t, s.file)
        if f is None or f["type"] not in schema.FILES:
            raise Problem(f"{where} › file: {t['label']} has no file field “{s.file}”")
        target = _list(d, s.list, where)
        out = {"type": "import_rows", "field": f["key"], "table": target["key"], "values": ir_values(d, t, target, s.values, where)}
        for col, key in (s.columns or {}).items():
            if d.field(target, key) is None:
                raise Problem(f"{where} › columns: “{key}” (for the column {col}) isn't a field of {target['label']}")
        if s.columns:
            out["columns"] = {c: schema.slug(k) for c, k in s.columns.items()}
        if s.match_on:
            if d.field(target, s.match_on) is None:
                raise Problem(f"{where} › match_on: “{s.match_on}” isn't a field of {target['label']}")
            out["match_on"] = schema.slug(s.match_on)
        if s.result:
            r = d.field(t, s.result)
            if r is None or r["type"] not in ("text", "long_text", "rich_text"):
                raise Problem(f"{where} › result: {t['label']} has no text field “{s.result}” to say what came of each row")
            out["result"] = r["key"]
    elif isinstance(s, m.Webhook):
        from_data = s.url.strip().startswith("{{")
        if not s.url.startswith(("https://", "http://")) and not from_data:
            raise Problem(f"{where} › url: a web address starts with https://, or comes from the record ({{{{integration.endpoint}}}}/orders)")
        if from_data and not s.hosts:
            raise Problem(f"{where} › hosts: an address from the record needs the hosts it may reach (a list, or a field holding one per line), "
                          f"so a record can't send it anywhere")
        if "{{" in s.url and t is not None:
            _template(d, t, s.url, f"{where} › url")
        out = {"type": "webhook", "url": s.url, "method": s.method}
        if s.payload:
            out["payload"] = s.payload
        if s.headers:
            out["headers"] = {k: str(v) for k, v in s.headers.items()}
        if s.hosts:
            out["hosts"] = s.hosts
        if s.save:
            if t is None:
                raise Problem(f"{where} › save: a rule about no list has no record to keep the answer on")
            for key in s.save:
                if d.field(t, key) is None:
                    raise Problem(f"{where} › save: “{key}” isn't a field of {t['label']}")
            out["save"] = {schema.slug(k): str(v) for k, v in s.save.items()}
    else:
        raise Problem(f"{where}: unknown step")
    if getattr(s, "only_if", None):
        out["only_if"] = only()
    # "created" is the record a step made: one added (or made by the AI), never a line of add_each, which the engine
    # keeps as the record made before it (an order's lines are added, then the order is sent: on created)
    makes_one = out.get("type") in ("create_record", "ai_create_record")
    return out, (d.list(out["table"]) if makes_one and out.get("table") else made)


def _ai_step(d, t, s, where):
    """An AI step: what it fills in, makes or checks is checked by name, like any step."""
    def field_of(name, what, kinds=None):
        f = d.field(t, name) if t else None
        if f is None:
            raise Problem(f"{where} › {what}: “{name}” isn't a field of {(t or {}).get('label', 'the record')}"
                          + (f" (its fields: {', '.join(d.field_names(t))})" if t else ""))
        if kinds and f["type"] not in kinds:
            raise Problem(f"{where} › {what}: {f['key']} is {f['type']}, and this needs a {' or '.join(kinds)} field")
        if f["type"] in schema.CALCULATED:
            raise Problem(f"{where} › {what}: {f['key']} is worked out, so nothing fills it in")
        return f["key"]
    def sureness(out):
        if getattr(s, "confidence", None):
            out["confidence"] = field_of(s.confidence, "confidence", ("number", "percent"))
        if getattr(s, "sure_above", None) is not None:
            out["sure_above"] = min(max(int(s.sure_above), 0), 100)
    if isinstance(s, m.AiFill):
        out = {"type": "ai_fill", "field": field_of(s.field, "field"), "instructions": s.instructions}
        sureness(out)
        if s.read_files:
            out["read_files"] = True
        if s.use_knowledge:
            out["use_knowledge"] = True
    elif isinstance(s, m.AiExtract):
        source = schema.slug(s.source)
        if source not in ("email", "file"):
            source = field_of(s.source, "source", ("file",))
        if t is None:
            raise Problem(f"{where}: ai_extract fills in a record's fields: in an email or file rule, use ai_create to make the record")
        out = {"type": "ai_extract", "from": source, "fields": [field_of(x, "fields") for x in s.fields]}
        if s.instructions:
            out["instructions"] = s.instructions
    elif isinstance(s, m.AiCreate):
        target = _list(d, s.list, where)
        out = {"type": "ai_create_record", "table": target["key"], "instructions": s.instructions}
        if s.attach_to:
            f = d.field(target, s.attach_to)
            if f is None or f["type"] != "file":
                raise Problem(f"{where} › attach_to: {s.attach_to} isn't a file field of {target['label']}")
            out["attach_to"] = f["key"]
    else:
        out = {"type": "ai_check", "field": field_of(s.field, "field", ("select",)), "instructions": s.instructions}
        sureness(out)
        if s.notes_field:
            out["notes_field"] = field_of(s.notes_field, "notes_field", ("long_text", "text", "rich_text"))
    if getattr(s, "review", "never") != "never":
        reviewer = "editor"
        if getattr(s, "reviewer", None):
            try:
                reviewer = d.who(t, s.reviewer)
            except NameProblem as exc:
                raise Problem(f"{where} › reviewer: {exc}") from None
        out.update(review=s.review, reviewer=reviewer)
    if getattr(s, "read_files", False):
        out["read_files"] = True
    if getattr(s, "use_knowledge", False):
        out["use_knowledge"] = True
    reads = []
    for i, r in enumerate(getattr(s, "reads", None) or [], start=1):  # what else it reads (the supplier's contract prices)
        other = _list(d, r.list, f"{where} › reads {i}")
        one = {"table": other["key"]}
        if r.where:
            one["where"] = ir_conditions(d, other, r.where, f"{where} › reads {i} › where", about=t)  # record: the rule's record
        reads.append(one)
    if reads:
        out["reads"] = reads[:4]
    if getattr(s, "learn", True) is False:
        out["learn"] = False
    if getattr(s, "manual", False):
        out["manual"] = True
    return out


def _of(d, t, of, where):
    """{"of": the rule's record's link to the record whose lines these are}, or nothing (the lines are the record's own)."""
    if not of:
        return {}
    f = d.field(t, of)
    if f is None or f.get("type") != "link":
        raise Problem(f"{where} › of: {t['label']} has no link “{of}” (its links: {', '.join(x['key'] for x in t['fields'] if x['type'] == 'link') or 'none'})")
    return {"of": f["key"]}


def _nested(lines, nested, where):
    """{"nested": the lines' link to a sub-assembly}, or nothing."""
    if not nested:
        return {}
    f = schema.field(lines, schema.slug(nested))
    if f is None or f.get("type") != "link":
        raise Problem(f"{where} › nested: {lines['label']} has no link “{nested}”")
    return {"nested": f["key"]}


def _follow_up(d, t, s, out, where):
    """A task's or an approval's nudges: reminded after so many days, then passed up to someone else."""
    if s.remind_after_days and s.remind_after_days > 0:
        out["remind_after_days"] = s.remind_after_days
    if s.escalate_after_days and s.escalate_after_days > 0:
        try:
            out.update(escalate_after_days=s.escalate_after_days, escalate_to=d.who(t, s.escalate_to or "owner"))
        except NameProblem as exc:
            raise Problem(f"{where} › escalate_to: {exc}") from None


def _not_by(d, t, names, where, stages=()):
    """Whoever mustn't do it: creator (whoever raised it), the person in a field of the record, a team, or (for a move)
    whoever moved the record to one of its stages."""
    out = []
    for name in names:
        stage = next((s for s in stages if s.lower() == str(name).strip().lower()), None)
        if stage:  # whoever moved it there: the one who recommended an award doesn't approve it
            out.append(stage)
            continue
        said = schema.slug(name)
        if said in ("creator", "created_by", "raised_by", "requester", "whoever_raised_it"):
            out.append("creator")
            continue
        if said in schema.WHO_WORDS or said in d.teams():  # never someone in this team, or with this role
            out.append(said)
            continue
        if t and "." in str(name):  # a person through links (the order's request's requester): the engine follows it
            try:
                found, _, keys = d.path(t, name)
            except NameProblem:
                found = None
            if found is not None and found.get("type") == "user":
                out.append(".".join(keys))
                continue
        f = d.field(t, name) if t else None
        if f is None or f["type"] not in ("user", "link"):
            raise Problem(f"{where}: “{name}” is creator (whoever raised it), a team, a person field of {(t or {}).get('label', 'the record')}"
                          + (f", or one of its stages ({', '.join(stages)})" if stages else ""))
        out.append(f["key"])
    return out


def _needs_its_list(when, where):
    """A rule on a schedule that's about records (only_if, or steps that change one) names its list."""
    raise Problem(f"{where} › when: a rule on a schedule that checks or changes records runs for each record of a list: give its "
                  f"when the list ({{\"on\": \"every\", \"list\": …, \"period\": \"{when.period}\"}})")


def _names_of(conditions):
    """The fields conditions read (the first part of a path; a formula's own names), groups included."""
    out = set()
    for c in conditions or []:
        if isinstance(c, str):
            try:
                out |= expr.outer_names(c) - set(CONTEXT_NAMES) - {"this", "today", "now"}
            except expr.ExprError:
                pass
        elif c.any is not None or c.all is not None:
            out |= _names_of(c.any if c.any is not None else c.all)
        elif c.field:
            out.add(c.field.split(".")[0])
    return out


def _list_meant(d, conditions, steps=()):
    """The one list a rule on a schedule that names none must be about: the only one with every field its conditions
    read and its steps set on the record. None when no list, or more than one, could be it."""
    names = _names_of(conditions)
    for s in steps or []:
        if isinstance(s, m.SetValues) and s.on in ("record", "", None):
            names |= {v.field for v in s.values}
    if not names:
        return None
    fits = [t for t in d.bp.get("tables", []) if all(schema.field(t, n) for n in names)]
    return fits[0] if len(fits) == 1 else None


def ir_rule(d, rule, where, feature=None):
    if isinstance(rule.when, m.Every) and not rule.when.list and (rule.only_if or any(isinstance(s, NO_RECORD) for s in rule.steps)):
        meant = _list_meant(d, rule.only_if, rule.steps)  # what it checks is on one list only: about that list
        if meant is not None:
            rule = rule.model_copy(update={"when": rule.when.model_copy(update={"list": meant["key"]})})
    t, trigger, conditions = ir_when(d, rule.when, where)
    if t is None and isinstance(rule.when, m.Every) and (rule.only_if or any(isinstance(s, NO_RECORD) for s in rule.steps)):
        _needs_its_list(rule.when, where)
    if t is None and rule.only_if:
        raise Problem(f"{where} › only_if: a rule started by an email or a file has no record to check yet: narrow it with the "
                      f"trigger's own from_contains / subject_contains instead")
    conditions += ir_conditions(d, t, rule.only_if, f"{where} › only_if") if t is not None else []
    actions, made = [], None
    for n, s in enumerate(rule.steps, start=1):
        step, made = ir_step(d, t, s, f"{where} › step {n}", made)
        actions.append(step)
    if not actions:
        raise Problem(f"{where}: a rule needs at least one step")
    out = {"key": schema.slug(rule.key), "name": rule.name, "trigger": trigger, "conditions": conditions, "actions": actions}
    if not rule.enabled:
        out["enabled"] = False
    if feature:
        out["feature"] = feature
    return out


def ir_stages(d, t, st, where):
    """(the stage field, the workflow) for a list's stages."""
    names = [str(s).strip() for s in st.stages if str(s).strip()]
    if len(names) < 2:
        raise Problem(f"{where}: stages need at least two names, in order")
    start = st.start or names[0]
    if start not in names:
        raise Problem(f"{where}: the start “{start}” isn't one of the stages ({', '.join(names)})")
    status = {"key": schema.slug(st.field or "status"), "label": schema.label_of(schema.slug(st.field or "status")), "type": "select",
              "options": names, "default": start}
    moves = []
    for n, mv in enumerate(st.moves, start=1):
        here = f"{where} › move {n}"
        for s in mv.from_ + [mv.to]:
            if s not in names:
                match = next((x for x in names if x.lower() == str(s).lower()), None)
                if match is None:
                    raise Problem(f"{here}: “{s}” isn't one of the stages ({', '.join(names)})")
        move = {"from": [next(x for x in names if x.lower() == s.lower()) for s in mv.from_],
                "to": next(x for x in names if x.lower() == mv.to.lower()), "label": mv.button[:60]}
        if mv.who:
            try:
                move["who"] = [d.who(t, w) for w in mv.who]
            except NameProblem as exc:
                raise Problem(f"{here} › who: {exc}") from None
        if mv.needs:
            missing = [x for x in mv.needs if not d.field(t, x)]
            if missing:
                raise Problem(f"{here} › needs: {', '.join(missing)} aren't fields of {t['label']} (its fields: {', '.join(d.field_names(t))})")
            move["requires"] = [d.field(t, x)["key"] for x in mv.needs]
        if mv.only_if:
            move["only_if"] = ir_conditions(d, t, mv.only_if, f"{here} › only_if")
        # separation of duties and a fresh sign-in, as a move added on its own keeps them: written with the list's
        # stages they were dropped without a word, so whoever raised a request could approve it after all
        if mv.not_by:
            move["not_by"] = _not_by(d, t, mv.not_by, f"{here} › not_by", stages=names)
        if mv.step_up:
            move["step_up"] = True
        moves.append(move)
    if not moves:  # each stage a button to the next
        moves = [{"from": [a], "to": b, "label": f"Move to {b}"[:60]} for a, b in zip(names, names[1:])]
    return status, {"field": status["key"], "start": start, "transitions": moves}


# --- applying -------------------------------------------------------------------------------------------------------

def _apply_ops(out, ir_ops, where):
    """Apply design ops to the outcome's design; a refused set leaves it as it was and says why."""
    if not ir_ops:
        return True
    result = design_ops.apply(out.blueprint, ir_ops, already=design_ops.wrong_already(out.blueprint), strict=True)
    if result.problems:
        out.problems.extend(f"{where}: {re.sub(r'^change \\d+ \\([^)]*\\): ', '', p)}" for p in result.problems)
        return False
    out.blueprint = result.blueprint
    out.changes.extend(result.changes)
    out.data.extend(getattr(result, "data", None) or [])
    out.navigate = out.navigate or getattr(result, "navigate", None)
    return True


def _name(p):
    for attr in ("key", "list", "rule", "feature"):
        v = getattr(p, attr, None)
        if isinstance(v, str) and v:
            return f"{p.do} {v}"
    if isinstance(p, m.AddRule):
        return f"add_rule {p.rule.key}"
    return p.do


LATE = (m.Link, m.Lookup, m.Total, m.Formula)
_NEW_DOCUMENTS = contextvars.ContextVar("keel_new_documents", default=None)


def _new_documents():
    """While a rule is compiled: the documents its steps name that the list doesn't have yet (made with it). Each
    compile has its own (builder turns and API calls compile at the same time)."""
    found = _NEW_DOCUMENTS.get()
    if found is None:
        found = {}
        _NEW_DOCUMENTS.set(found)
    return found
# what a design_op may do: only what the language has no patch of its own for
ESCAPE = {"add_page", "update_page", "remove_page", "add_document", "update_document", "remove_document", "set_public_form",
          "set_portal", "set_team", "set_space", "remove_space", "set_sync", "remove_sync", "add_flow", "update_flow",
          "remove_flow", "set_theme", "rename_app", "set_module", "update_view", "remove_view", "order_fields", "update_table"}
POLICY_KEYS = {"op", "table", "access", "permissions", "row_access", "own_records", "rules", "validations", "unique_together",
               "approve_changes", "sealed", "colors", "journey", "portal", "audit_views", "wizard",
               "sections", "layout",  # how a record's fields are grouped on its form (a step-by-step form's steps), and tabs
               "lock_if", "lock_except",  # when records can't be changed, by a condition (a bid once the tender's bids close)
               "keep_for"}  # how long records are kept, and what (a legal hold) stops their deletion


def apply(bp, raw_patches, allowed=None):
    """Apply a builder's patches to a design. Returns an Outcome: the new design (with every patch that could be
    applied), what changed in plain words, and a problem for each patch refused."""
    from . import patterns
    out = Outcome(blueprint=copy.deepcopy(bp or schema.empty()))
    for k in ("tables", "automations", "pages", "modules"):
        out.blueprint.setdefault(k, [])
    parsed, out.problems = parse(raw_patches, allowed)
    refused = {int(re.match(r"patch (\d+)", p).group(1)) for p in out.problems}

    def where(n, p):
        return f"patch {n} ({_name(p)})"

    lists = [(n, p) for n, p in parsed if isinstance(p, m.AddList)]
    made_here = {schema.slug(p.key) for _, p in lists}
    lists.sort(key=lambda np: 1 if np[1].inside and schema.slug(np[1].inside) in made_here else 0)  # what holds lines first
    order = (m.ChangeList, m.RemoveList, m.AddField, m.ChangeField, m.RemoveField, m.RenameChoice, m.SetStages, m.DesignOp)
    # a feature taken away goes first: one sent again in the same answer ("remove_feature X", then X as it should be)
    # is made afresh, never made and then taken away
    rest = sorted([(n, p) for n, p in parsed if not isinstance(p, m.AddList)],
                  key=lambda np: (-1 if isinstance(np[1], m.RemoveFeature) else 0 if isinstance(np[1], order) else
                                  1 if isinstance(np[1], patterns.ALL) else
                                  2 if isinstance(np[1], (m.AddRule, m.ChangeRule, m.RemoveRule)) else 3, np[0]))
    # first the teams: a new list's moves, fields and access can be for one made in the same batch (only Purchasing
    # officers send an order); made after the lists, they weren't there yet when the lists named them
    teams = [(n, p) for n, p in rest if isinstance(p, m.SetTeam) or (isinstance(p, m.DesignOp) and p.op.get("op") == "set_team")]
    for n, p in teams:
        try:
            ok = _apply_ops(out, translate(Design(out.blueprint), p, where(n, p)), where(n, p))
        except Problem as exc:
            out.problems.append(str(exc))
            ok = False
        if not ok:
            refused.add(n)
    rest = [(n, p) for n, p in rest if n not in {t for t, _ in teams}]
    # phase A: each new list, its plain fields and its stage field
    for n, p in lists:
        here, d = where(n, p), Design(out.blueprint)
        early = [f for f in p.fields if not isinstance(f, LATE)]
        try:
            stub = {"key": schema.slug(p.key), "label": p.label, "fields": []}
            fields = [ir_field(d, stub, f, f"{here} › field {f.key}") for f in early]
            status, _ = ir_stages(d, stub, m.Stages(**{**p.stages.model_dump(by_alias=True), "moves": []}), f"{here} › stages") if p.stages else (None, None)
        except Problem as exc:
            out.problems.append(str(exc))
            refused.add(n)
            continue
        if status and not any(f["key"] == status["key"] for f in fields):
            fields.append(status)
        elif status:
            fields = [status if f["key"] == status["key"] else f for f in fields]
        op = {"op": "create_table", "key": schema.slug(p.key), "label": p.label, "singular": p.singular, "fields": fields or
              [{"key": "name", "label": "Name", "type": "text"}]}
        if p.inside:  # lines inside another list's records: made with their link to it
            parent = d.list(p.inside)
            if parent is None:
                out.problems.append(f"{here}: “inside” names {p.inside}, which isn't a list")
                refused.add(n)
                continue
            link = next((f for f in p.fields if isinstance(f, m.Link) and d.list(f.to) is parent), None)
            ir = ir_field(d, stub, link, f"{here} › field {link.key}") if link else \
                {"key": schema.slug(parent.get("singular") or parent["key"]), "label": parent.get("singular") or parent["label"],
                 "type": "link", "table": parent["key"]}
            ir["required"] = True
            op["fields"] = [f for f in op["fields"] if f["key"] != ir["key"]] + [ir]
            op["embedded"] = True
        if not _apply_ops(out, [op], here):
            refused.add(n)
    # fields added to lists that were there already, once the new lists are: a new list's formula can read one (an
    # adjustment's price reads its contract line's new share); one that can't be added yet waits for the rest, as before
    added = [(n, p) for n, p in rest if isinstance(p, (m.AddField, m.ChangeField)) and schema.slug(p.list) not in made_here]
    for n, p in added:
        before, snapshot = len(out.problems), copy.deepcopy(out.blueprint)
        try:
            ok = _apply_ops(out, translate(Design(out.blueprint), p, where(n, p)), where(n, p))
        except Problem:
            ok = False
        if ok:
            rest = [(k, x) for k, x in rest if k != n]
        else:
            out.blueprint = snapshot
            del out.problems[before:]
    # phase B and C: links, then what's worked out. A worked-out field can read one another new list hasn't made yet
    # (a quote's total sums its lines' amounts, a formula of the lines'): what can't be made is tried again once the rest
    # is in, and only what still can't is refused, with its last problem
    for phase in ((m.Link,), (m.Lookup, m.Total, m.Formula)):
        waiting = [(n, p, f) for n, p in lists if n not in refused for f in p.fields if isinstance(f, phase)]
        said = {}
        for _ in range(4):
            again = []
            for n, p, f in waiting:
                here, d = where(n, p), Design(out.blueprint)
                t = d.list(p.key)
                if t is None or d.field(t, f.key):
                    continue
                before = len(out.problems)
                try:
                    ir = ir_field(d, t, f, f"{here} › field {f.key}")
                    ok = _apply_ops(out, [{"op": "add_field", "table": t["key"], "field": ir}], here)
                except Problem as exc:
                    out.problems.append(str(exc))
                    ok = False
                problems = out.problems[before:]
                del out.problems[before:]
                if ok:
                    said.pop((n, f.key), None)
                else:
                    said[(n, f.key)] = problems
                    again.append((n, p, f))
            if not again or len(again) == len(waiting):
                break
            waiting = again
        for (n, _), problems in said.items():
            out.problems.extend(problems)
            refused.add(n)
    # fields added by patches of their own to the new lists (an order's totals, sent after it), before their settings,
    # which can name them (what the portal shows of the order); one that can't be made yet waits for the rest, as before
    for n, p in [(n, p) for n, p in rest if isinstance(p, (m.AddField, m.ChangeField)) and schema.slug(p.list) in made_here]:
        before, snapshot = len(out.problems), copy.deepcopy(out.blueprint)
        try:
            ok = _apply_ops(out, translate(Design(out.blueprint), p, where(n, p)), where(n, p))
        except Problem:
            ok = False
        if ok:
            rest = [(k, x) for k, x in rest if k != n]
        else:
            out.blueprint = snapshot
            del out.problems[before:]
    # the new lists' own settings (their portal, who sees them, their policies), now their fields are there: their moves
    # can then be for whoever those settings make (the supplier through the portal acknowledges an order). One that can't
    # be made yet waits for the rest, as before.
    early = [(n, p) for n, p in rest if isinstance(p, m.DesignOp) and p.op.get("op") == "update_table"
             and schema.slug(str(p.op.get("table") or p.op.get("key") or "")) in made_here]
    for n, p in early:
        before, snapshot = len(out.problems), copy.deepcopy(out.blueprint)
        try:
            ok = _apply_ops(out, translate(Design(out.blueprint), p, where(n, p)), where(n, p))
        except Problem:
            ok = False
        if ok:
            rest = [(k, x) for k, x in rest if k != n]
        else:  # tried again with the rest
            out.blueprint = snapshot
            del out.problems[before:]
    # phase D: each new list's stage moves, and its fields in the order given
    for n, p in lists:
        d = Design(out.blueprint)
        t = d.list(p.key)
        if t is None:
            continue
        if p.stages:
            try:
                _, flow = ir_stages(d, t, p.stages, f"{where(n, p)} › stages")
            except Problem as exc:
                out.problems.append(str(exc))
                refused.add(n)
                continue
            _apply_ops(out, [{"op": "update_table", "table": t["key"], "workflow": flow}], where(n, p))
        wanted = [schema.slug(f.key) for f in p.fields]
        have = [f["key"] for f in t["fields"]]
        if [k for k in have if k in wanted] != [k for k in wanted if k in have]:
            _apply_ops(out, [{"op": "order_fields", "table": t["key"], "fields": [k for k in wanted if k in have] + [k for k in have if k not in wanted]}], where(n, p))
    for key in made_here:  # what names each new record, and its list's columns, chosen once its links are there too
        t = schema.table(out.blueprint, key)
        if t is None:
            continue
        t["title_field"] = schema.title_field({**t, "title_field": None})
        every = next((v for v in t.get("views") or [] if v.get("key") == "all" and v.get("type") == "list"), None)
        if every is not None:
            every["columns"] = schema.default_columns(t)
    # everything else, one patch at a time; one refused is tried again once the rest are in (a feature that reads a
    # total another feature in the same batch makes), and only its last problems are kept
    waiting, said = list(rest), {}
    for _ in range(3):
        again = []
        for n, p in waiting:
            here = where(n, p)
            before = len(out.problems)
            try:
                if isinstance(p, patterns.ALL):
                    ok = patterns.apply(out, p, here)
                    if ok:
                        out.features.append(p.feature_key())
                elif isinstance(p, m.RemoveFeature) and not patterns._entry(out.blueprint, schema.slug(p.feature)) \
                        and any(a["key"] == schema.slug(p.feature) for a in out.blueprint.get("automations", [])):
                    ok = _apply_ops(out, [{"op": "remove_automation", "key": schema.slug(p.feature)}], here)  # a rule, not a feature
                elif isinstance(p, m.RemoveFeature):
                    ok = patterns.remove(out, schema.slug(p.feature), here)
                elif isinstance(p, m.RemoveRule) and not any(a["key"] == schema.slug(p.rule) or a.get("name", "").lower() == p.rule.lower()
                                                             for a in out.blueprint.get("automations", [])) \
                        and patterns._entry(out.blueprint, schema.slug(p.rule)):
                    ok = patterns.remove(out, schema.slug(p.rule), here)  # a feature named as a rule
                elif isinstance(p, m.RemoveRule) and feature_rule(out.blueprint, p.rule) and _only_rule(out.blueprint, feature_rule(out.blueprint, p.rule)):
                    ok = patterns.remove(out, _only_rule(out.blueprint, feature_rule(out.blueprint, p.rule)), here)
                else:
                    snapshot, said_before = copy.deepcopy(out.blueprint), len(out.changes)
                    ok = _apply_ops(out, translate(Design(out.blueprint), p, here), here)
                    if ok and snapshot.get("features") and not patterns.kept(out, snapshot, p, here, translate):
                        out.blueprint, ok = snapshot, False  # it would take a feature's parts away: not made
                        del out.changes[said_before:]
                    if ok and isinstance(p, m.AddRule):  # a feature's rule written anew is the writer's own from now on
                        _given_up(out, snapshot, schema.slug(p.rule.key))
                    elif ok and isinstance(p, (m.ChangeRule, m.RemoveRule)) and feature_rule(snapshot, p.rule):  # changed: the same
                        _given_up(out, snapshot, feature_rule(snapshot, p.rule)["key"])
            except Problem as exc:
                out.problems.append(str(exc))
                ok = False
            new = out.problems[before:]
            del out.problems[before:]
            if ok:
                said.pop(n, None)
            else:
                said[n] = new
                again.append((n, p))
        if not again or len(again) == len(waiting):
            break
        waiting = again
    for n in sorted(said):
        out.problems.extend(said[n])
        refused.add(n)
    _complete_stages(out)
    out.refused = sorted(refused)
    return out


def _filled_in(m):
    """A move the platform added so no stage was out of reach, not the builder's (nor a plain list of stages' own buttons)."""
    return bool(m.get("filled"))


def _complete_stages(out):
    """What the platform works out rather than the builder: every stage can be got to, and none with stages after it
    is a dead end (each a button to the stage after, unless a rule moves records on). Worked out again on every change:
    a button added before a rule moved records there (Approved, once the approval rule came) goes again, as it would let
    anyone skip the rule."""
    for t in out.blueprint.get("tables", []):
        if not t.get("workflow"):
            continue
        before = list(t["workflow"].get("transitions") or [])
        kept = [m for m in before if not _filled_in(m)]
        added = schema.complete_moves(out.blueprint, {**t, "workflow": {**t["workflow"], "transitions": kept}})
        moves = kept + [{**m, "filled": True} for m in added]
        if [(m["to"], tuple(m.get("from") or [])) for m in moves] == [(m["to"], tuple(m.get("from") or [])) for m in before]:
            continue
        flow = {**t["workflow"], "transitions": moves}
        said = len(out.problems)
        if not _apply_ops(out, [{"op": "update_table", "table": t["key"], "workflow": flow}], f"the stages of {t['label']}"):
            del out.problems[said:]  # not the builder's to put right: left as it was


def feature_rule(bp, name):
    """The rule a feature made that a change_rule or remove_rule names: by its own key or name, or by its feature's key
    when that feature makes one rule. None for a rule of nobody's feature."""
    key = schema.slug(name)
    rules = bp.get("automations", [])
    rule = next((a for a in rules if a["key"] == key or a.get("name", "").lower() == str(name).lower()), None)
    if rule is not None:
        return rule if rule.get("feature") else None
    made = [a for a in rules if a.get("feature") == key]
    return made[0] if len(made) == 1 else None


def _only_rule(bp, rule):
    """The feature whose whole doing is its rules (a chase: its reminder and the date it keeps), not a list's moves, a
    lock or a limit: taking its rule away takes the feature."""
    entry = next((e for e in bp.get("features", []) if e["key"] == rule.get("feature")), None)
    if entry is None:
        return None
    undo, made = entry.get("undo") or {}, entry.get("made") or {}
    return None if undo.get("workflow") or undo.get("lock_if") or made.get("rules") or made.get("limit") else entry["key"]


def _given_up(out, before, key):
    """A rule a feature made, written anew by someone (by hand on the App map, or by the builder): it's theirs now, and
    the feature no longer makes it (so making the feature again never undoes their change)."""
    was = next((a for a in before.get("automations", []) if a["key"] == key), None)
    feature = (was or {}).get("feature")
    entry = next((e for e in out.blueprint.get("features", []) if e["key"] == feature), None) if feature else None
    if entry is None:
        return
    entry["given_up"] = sorted(set(entry.get("given_up") or []) | {key})
    out.changes.append(f"“{was.get('name') or key}” is your own rule now: {feature} no longer changes it")


def _documents_for(d):
    """The standard documents a rule's steps named that their lists don't have yet."""
    out = []
    for table, keys in _new_documents().items():
        t = d.list(table)
        for key in sorted(keys):
            label = schema.label_of(key)
            out.append({"op": "add_document", "table": t["key"], "document": {"key": key, "label": label, "title": label}})
    _new_documents().clear()
    return out


def _names_field(params, t, f):
    """Whether a feature on this list names this field in its parts (a gate's needs, a chase's since, a total's field)."""
    lists = {str(params.get(k) or "").lower() for k in ("list", "from_list", "to_list", "lines", "payments")}
    if not lists & {t["key"].lower(), str(t.get("label") or "").lower()}:
        return False
    names = {f["key"].lower(), str(f.get("label") or "").lower()}

    def walk(v):
        if isinstance(v, dict):
            return any(walk(x) for k, x in v.items() if k not in ("list", "message", "subject", "body"))
        if isinstance(v, list):
            return any(walk(x) for x in v)
        return isinstance(v, str) and (v.lower() in names or v.split(".")[0].lower() in names)
    return walk(params)


# how many records have a value in a field (or that value): what's in use is never thrown away by a change. Whoever
# applies patches to an app in use says (the App map, the builder); left unsaid, nothing is in use.
FILLED = contextvars.ContextVar("keel_filled", default=None)


def _filled():
    return FILLED.get() or (lambda table, field, value=None: 0)


def _studio(fn, d, change, where):
    """The ops the App map's own edit makes for this change (one logic for a click and a patch), its refusals as ours."""
    from .. import studio
    try:
        made = fn(d.bp, change, _filled()) if fn is not studio.rule_ops else fn(d.bp, change)
    except design_ops.SchemaError as exc:
        raise Problem(f"{where}: {exc}") from None
    return made[0]


SETTINGS = {"label", "help", "required", "unique", "default", "placeholder", "options", "multiple", "sensitive", "shows_last", "readonly",
            "decimals", "unit", "currency", "prefix", "digits", "min", "max", "visible_to", "access", "option_colors", "before",
            "after", "yes_label", "no_label", "many", "ai"}


def _choices_kept(d, t, f, options, where):
    """A choice taken away that a rule waits for, checks or sets is refused, naming the rule: taking it away would quietly
    stop the rule (whoever takes it away: the App map, the builder, an outside agent)."""
    from .. import studio
    kept = {str(o).strip().lower() for o in options}
    for gone in [o for o in f.get("options") or [] if str(o).strip().lower() not in kept]:
        try:
            studio._not_used(d.bp, t, f, gone, "stage" if f["key"] == (t.get("workflow") or {}).get("field") else "choice")
        except design_ops.SchemaError as exc:
            raise Problem(f"{where}: {exc}") from None


def _stages_of(d, t, where):
    flow = t.get("workflow")
    if not flow:
        raise Problem(f"{where}: {t['label']} has no stages (set_stages gives it some)")
    return flow, list((d.field(t, flow["field"]) or {}).get("options") or [])


def _stage_named(d, t, names, said, where):
    found = next((x for x in names if x.lower() == str(said or "").strip().lower()), None)
    if found is None:
        raise Problem(f"{where}: “{said}” isn't one of {t['label']}'s stages ({', '.join(names)})")
    return found


def _move_parts(d, t, names, mv, where):
    """A Move as the App map's edit takes it, and what it adds that the map's form doesn't (only_if, not_by, step_up)."""
    change = {"from": [_stage_named(d, t, names, x, f"{where} › from") for x in mv.from_], "to": _stage_named(d, t, names, mv.to, f"{where} › to"),
              "label": mv.button}
    try:
        change["who"] = [d.who(t, w) for w in mv.who]
    except NameProblem as exc:
        raise Problem(f"{where} › who: {exc}") from None
    missing = [x for x in mv.needs if not d.field(t, x)]
    if missing:
        raise Problem(f"{where} › needs: {', '.join(missing)} aren't fields of {t['label']} (its fields: {', '.join(d.field_names(t))})")
    change["requires"] = [d.field(t, x)["key"] for x in mv.needs]
    extra = {}
    if mv.only_if:
        extra["only_if"] = ir_conditions(d, t, mv.only_if, f"{where} › only_if")
    if mv.not_by:
        extra["not_by"] = _not_by(d, t, mv.not_by, f"{where} › not_by", stages=names)
    if mv.step_up:
        extra["step_up"] = True
    return change, extra


def _with_move_extra(ops_, index, extra):
    for op in ops_:
        flow = op.get("workflow") if op.get("op") == "update_table" else None
        if flow and extra:
            flow["transitions"][index] = {**flow["transitions"][index], **extra}
    return ops_


def _button_at(d, t, flow, button, where, to=None):
    said = str(button or "").strip().lower()
    i = next((i for i, mv in enumerate(flow.get("transitions") or []) if str(mv.get("label") or mv["to"]).lower() == said
              and (to is None or str(mv["to"]).lower() == str(to).strip().lower())), None)
    if i is None:
        raise Problem(f"{where}: {t['label']} has no move with the button “{button}” (its buttons: "
                      f"{', '.join(mv.get('label') or mv['to'] for mv in flow.get('transitions') or [])})")
    return i


def _rule_to_change(d, name, where):
    old = next((a for a in d.bp.get("automations", []) if a["key"] == schema.slug(name) or a["name"].lower() == str(name).lower()), None)
    if old is None:
        keys = [a["key"] for a in d.bp.get("automations", [])]
        raise Problem(f"{where}: there's no rule “{name}” (rules: {', '.join(keys) or 'none'})")
    if old.get("feature"):
        raise Problem(f"{where}: {old['key']} is part of the feature {old['feature']}: change the feature (send it again with its key)")
    t = d.list(old["trigger"].get("table")) if old["trigger"].get("table") else None
    return copy.deepcopy(old), t


def _place(at, n, where, adding=False):
    top = n + 1 if adding else n
    if at is None:
        return n if adding else n - 1
    if not 1 <= int(at) <= max(top, 1):
        raise Problem(f"{where}: the rule has {n} step{'s' if n != 1 else ''}, so {at} isn't a place in it")
    return int(at) - 1


def translate(d, p, where):
    """The design ops for one patch (not a new list, not a feature)."""
    from .. import studio
    if isinstance(p, m.ChangeList):
        t = _list(d, p.list, where)
        changes = {k: v for k, v in (("label", p.label), ("singular", p.singular)) if v}
        out = [{"op": "update_table", "table": t["key"], "changes": changes}] if changes else []
        if p.title_field is not None:
            f = d.field(t, p.title_field)
            if f is None:
                raise Problem(f"{where} › title_field: {t['label']} has no field “{p.title_field}” (its fields: {', '.join(d.field_names(t))})")
            out.append({"op": "update_table", "table": t["key"], "title_field": f["key"]})
        if p.module is not None:
            out.append({"op": "update_table", "table": t["key"], "module": p.module})
        if p.icon is not None:
            out.append({"op": "update_table", "table": t["key"], "icon": p.icon})
        return out
    if isinstance(p, m.SetField):
        t = _list(d, p.list, where)
        f = d.field(t, p.field)
        if f is None:
            raise Problem(f"{where}: {t['label']} has no field “{p.field}” (its fields: {', '.join(d.field_names(t))})")
        from .patterns import LOOKS
        if f.get("feature") and not set(p.settings) <= set(LOOKS):  # how it looks is anyone's to change; what it is, the feature's
            raise Problem(f"{where}: {f['key']} is made by the feature {f['feature']}, which decides "
                          f"{', '.join(k for k in p.settings if k not in LOOKS)}: change the feature (send it again with its key); "
                          f"how it looks ({', '.join(LOOKS[:6])}…) can be set here")
        wrong = [k for k in p.settings if k not in SETTINGS]
        if wrong:
            raise Problem(f"{where}: “{wrong[0]}” isn't a setting a field has (settings: {', '.join(sorted(SETTINGS - {'access', 'many'}))})")
        change = {{"visible_to": "access", "many": "multiple"}.get(k, k): v for k, v in p.settings.items()}
        if isinstance(change.get("options"), list):
            _choices_kept(d, t, f, change["options"], where)
        if isinstance(change.get("access"), list):
            try:
                change["access"] = [d.who(t, w) for w in change["access"]]
            except NameProblem as exc:
                raise Problem(f"{where} › visible_to: {exc}") from None
        return _studio(studio.data_ops, d, {"do": "update_field", "table": t["key"], "key": f["key"], **change}, where)
    if isinstance(p, m.OrderFields):
        t = _list(d, p.list, where)
        wanted = []
        for name in p.fields:
            f = d.field(t, name)
            if f is None:
                raise Problem(f"{where}: {t['label']} has no field “{name}” (its fields: {', '.join(d.field_names(t))})")
            wanted.append(f["key"])
        return [{"op": "order_fields", "table": t["key"], "fields": wanted + [k for k in d.field_names(t) if k not in wanted]}]
    if isinstance(p, m.RemoveStages):
        t = _list(d, p.list, where)
        _stages_of(d, t, where)
        return _studio(studio.stage_ops, d, {"do": "remove_stages", "table": t["key"]}, where)
    if isinstance(p, m.RemoveStage):
        t = _list(d, p.list, where)
        flow, names = _stages_of(d, t, where)
        gone = _stage_named(d, t, names, p.stage, where)
        ops_ = _studio(studio.stage_ops, d, {"do": "remove_stage", "table": t["key"], "name": gone}, where)
        into = [x for mv in flow.get("transitions") or [] if mv["to"] == gone for x in (mv.get("from") or []) if x != gone]
        onto = [mv["to"] for mv in flow.get("transitions") or [] if gone in (mv.get("from") or []) and mv["to"] != gone]
        for op in ops_:  # the chain closes up: what led into it now leads on to where it led
            moves = (op.get("workflow") or {}).get("transitions") if op.get("op") == "update_table" else None
            if moves is None:
                continue
            for a in dict.fromkeys(into):
                for b in dict.fromkeys(onto):
                    if a != b and not any(b == mv["to"] and a in (mv.get("from") or []) for mv in moves):
                        moves.append({"from": [a], "to": b, "label": f"Move to {b}"[:60]})
        return ops_
    if isinstance(p, m.OrderStages):
        t = _list(d, p.list, where)
        _, names = _stages_of(d, t, where)
        order = [_stage_named(d, t, names, x, where) for x in p.stages]
        if sorted(order) != sorted(names):
            raise Problem(f"{where}: name every stage once, in order ({', '.join(names)})")
        ops_ = _studio(studio.stage_ops, d, {"do": "order_stages", "table": t["key"], "names": order}, where)
        if p.start:
            start = _stage_named(d, t, names, p.start, f"{where} › start")
            for op in ops_:
                if op.get("op") == "update_table" and op.get("workflow"):
                    op["workflow"]["start"] = start
        return ops_
    if isinstance(p, (m.AddMove, m.ChangeMove)):
        t = _list(d, p.list, where)
        flow, names = _stages_of(d, t, where)
        change, extra = _move_parts(d, t, names, p.move, f"{where} › move")
        if isinstance(p, m.AddMove):
            return _with_move_extra(_studio(studio.stage_ops, d, {"do": "add_move", "table": t["key"], **change}, where), -1, extra)
        i = _button_at(d, t, flow, p.button, where, p.to)
        return _with_move_extra(_studio(studio.stage_ops, d, {"do": "update_move", "table": t["key"], "i": i, **change}, where), i, extra)
    if isinstance(p, m.RemoveMove):
        t = _list(d, p.list, where)
        flow, _ = _stages_of(d, t, where)
        i = _button_at(d, t, flow, p.button, where, p.to)
        gone = flow["transitions"][i]
        rest = {**t, "workflow": {**flow, "transitions": [mv for n, mv in enumerate(flow["transitions"]) if n != i]}}
        if any(mv["to"] == gone["to"] for mv in schema.complete_moves(d.bp, rest)):  # it was the only way there
            raise Problem(f"{where}: “{gone.get('label') or gone['to']}” is the only way to {gone['to']}: add another way there first "
                          f"(add_move), or take the stage away (remove_stage)")
        return _studio(studio.stage_ops, d, {"do": "remove_move", "table": t["key"], "i": i}, where)
    if isinstance(p, (m.AddStep, m.ChangeStep, m.RemoveStep, m.MoveStep)):
        new, t = _rule_to_change(d, p.rule, where)
        actions = list(new.get("actions") or [])
        _new_documents().clear()
        if isinstance(p, (m.AddStep, m.ChangeStep)):
            if t is None:
                raise Problem(f"{where}: steps need the list the rule is about")
            step, _ = ir_step(d, t, p.step, f"{where} › step", None)
            if isinstance(p, m.AddStep):
                actions.insert(_place(p.at, len(actions), where, adding=True), step)
            else:
                actions[_place(p.at, len(actions), where)] = step
        elif isinstance(p, m.RemoveStep):
            if len(actions) <= 1:
                raise Problem(f"{where}: a rule needs at least one step: remove_rule takes the rule away")
            actions.pop(_place(p.at, len(actions), where))
        else:
            step = actions.pop(_place(p.at, len(actions), where))
            actions.insert(_place(p.to, len(actions), f"{where} › to", adding=True), step)
        new["actions"] = actions
        return _documents_for(d) + [{"op": "add_automation", "automation": new}]
    if isinstance(p, m.RemoveList):
        return [{"op": "delete_table", "table": _list(d, p.list, where)["key"]}]
    if isinstance(p, m.AddField):
        t = _list(d, p.list, where)
        old = d.field(t, p.field.key)
        if old is not None:  # sent again (the data step made it already): it's made as sent, never refused for being there
            new = {**ir_field(d, t, p.field, where), "key": old["key"]}
            if old.get("feature"):
                raise Problem(f"{where}: {t['label']} already has “{old['key']}”, made by the feature {old['feature']}: change "
                              f"the feature instead (send it again with its key)")
            if old["key"] == ((t.get("workflow") or {}).get("field")) and list(new.get("options") or []) != list(old.get("options") or []):
                raise Problem(f"{where}: {old['key']} is {t['label']}'s stages ({', '.join(old.get('options') or [])}): set_stages "
                              f"changes them, add_stage adds one")
            same = all(old.get(k) == v for k, v in new.items() if k not in ("label",))
            return [] if same else [{"op": "update_field", "table": t["key"], "key": old["key"], "changes": new}]
        return [{"op": "add_field", "table": t["key"], "field": ir_field(d, t, p.field, where)}]
    if isinstance(p, m.ChangeField):
        t = _list(d, p.list, where)
        old = d.field(t, p.field.key)
        if old is None:
            raise Problem(f"{where}: {t['label']} has no field “{p.field.key}” (its fields: {', '.join(d.field_names(t))}); add_field adds one")
        new = ir_field(d, t, p.field, where)
        if isinstance(new.get("options"), list):
            _choices_kept(d, t, old, new["options"], where)
        if new.get("type") != old.get("type"):  # its records' values must fit the new kind: checked as the App map checks it
            _studio(studio.data_ops, d, {"do": "update_field", "table": t["key"], "key": old["key"], "type": new.get("type")}, where)
        return [{"op": "update_field", "table": t["key"], "key": old["key"], "changes": {**new, "key": old["key"]}}]
    if isinstance(p, m.RemoveField):
        t = _list(d, p.list, where)
        f = d.field(t, p.field)
        if f is None:
            raise Problem(f"{where}: {t['label']} has no field “{p.field}”")
        using = [e["key"] for e in d.bp.get("features", []) if _names_field(e.get("params") or {}, t, f) and not f.get("feature") == e["key"]]
        if using:
            raise Problem(f"{where}: {f['key']} is used by the feature{'s' if len(using) != 1 else ''} {', '.join(using)}: send "
                          f"{'each' if len(using) != 1 else 'it'} again without it, or take {'them' if len(using) != 1 else 'it'} "
                          f"away first (remove_feature)")
        return [{"op": "remove_field", "table": t["key"], "key": f["key"]}]
    if isinstance(p, m.RenameChoice):
        t = _list(d, p.list, where)
        f = d.field(t, p.field)
        if f is None or f["type"] not in ("select", "multi_select"):
            raise Problem(f"{where}: {p.field} isn't a choice field of {t['label']}")
        old = d.choice(f, p.old)
        if old is None:
            raise Problem(f"{where}: “{p.old}” isn't a choice of {f['key']} ({', '.join(f.get('options') or [])})")
        return [{"op": "rename_choice", "table": t["key"], "field": f["key"], "old": old, "new": p.new}]
    if isinstance(p, m.SetStages):
        t = _list(d, p.list, where)
        status, flow = ir_stages(d, t, p.stages, where)
        old = d.field(t, status["key"])
        if old is None:
            first = {"op": "add_field", "table": t["key"], "field": status}
        else:
            first = {"op": "update_field", "table": t["key"], "key": old["key"], "changes": {**old, "type": "select", "options": status["options"],
                                                                                             "default": status["default"]}}
        return [first, {"op": "update_table", "table": t["key"], "workflow": flow}]
    if isinstance(p, m.AddValidation):
        t = _list(d, p.list, where)
        _check_formula(d, t, p.check, f"{where} › check")
        rule = {"check": p.check, "message": p.message}
        if p.only_if:
            rule["when"] = ir_conditions(d, t, p.only_if, f"{where} › only_if")
        same = _same_check(p.check)
        kept = [r for r in t.get("rules") or [] if r.get("check") == "false" or _same_check(r.get("check")) != same]
        return [{"op": "update_table", "table": t["key"], "rules": kept + [rule]}]  # sent again: it replaces the one it was
    if isinstance(p, m.RemoveValidation):
        t = _list(d, p.list, where)
        wanted = _same_check(p.check)
        mine = [r for r in t.get("rules") or [] if r.get("check") != "false"]
        gone = [r for r in mine if wanted in (_same_check(r.get("check")), _same_check(r.get("message")))]
        if not gone and wanted:  # named by a part of its check or message: the one rule it's part of
            part = [r for r in mine if wanted in _same_check(r.get("check")) or wanted in _same_check(r.get("message"))]
            gone = part if len(part) == 1 else []
        if not gone:
            raise Problem(f"{where}: {t['label']} has no rule “{p.check}” (its rules: "
                          + ("; ".join(f"{r['check']} (“{r.get('message')}”)" for r in mine) or "none") + ")")
        return [{"op": "update_table", "table": t["key"], "rules": [r for r in t.get("rules") or [] if r not in gone]}]
    if isinstance(p, m.SetTeam):
        team = {"key": schema.slug(p.key), "name": p.name}
        if p.about:
            team["about"] = p.about
        return [{"op": "set_team", "team": team}]
    if isinstance(p, m.SetAccess):
        t = _list(d, p.list, where)
        access, teams = {}, []
        for action in ("view", "edit"):
            value = getattr(p, action)
            if value:
                who = _role_or_team(d, value, f"{where} › {action}")
                if who in schema.ROLE_NAMES:
                    access[action] = who
                elif action == "edit" and (access.get("view") or not p.view):  # everyone (with that role) sees it and picks
                    access.setdefault("view", "viewer")  # from it; only the team changes it ("only curators change units")
                    access["edit_teams"] = [who]
                else:
                    teams.append(who)
        if teams:
            access["teams"] = list(dict.fromkeys(teams))
        if not access:
            raise Problem(f"{where}: say who may see (view) or change (edit) {t['label']}")
        return [{"op": "update_table", "table": t["key"], "access": access}]
    if isinstance(p, m.OwnRecords):
        t = _list(d, p.list, where)
        try:
            f, _, keys = d.path(t, p.field)
        except NameProblem as exc:
            raise Problem(f"{where} › field: {exc}") from None
        if f.get("type") != "user":
            people = [x["key"] for x in t["fields"] if x["type"] == "user"]
            raise Problem(f"{where} › field: {p.field} isn't a person field of {t['label']}" + (f" (its person fields: {', '.join(people)})" if people
                                                                                                 else ": add one first (type person)"))
        rows = {"field": ".".join(keys), "roles": list(p.roles)}
        if p.see_all:
            rows["teams"] = [_role_or_team(d, x, f"{where} › see_all") for x in p.see_all]
        if p.shared_when_empty:
            rows["shared_when_empty"] = True
        kept = [r for r in schema.row_rules(t) if r.get("field") != rows["field"]]  # another field's rule holds too
        return [{"op": "update_table", "table": t["key"], "row_access": kept + [rows] if kept else rows}]
    if isinstance(p, m.AddStage):
        t = _list(d, p.list, where)
        f = d.stage_field(t)
        if f is None:
            raise Problem(f"{where}: {t['label']} has no stages yet: set_stages gives it some")
        after = d.choice(f, p.after)
        if after is None:
            raise Problem(f"{where}: “{p.after}” isn't a stage of {t['label']} (its stages: {', '.join(f.get('options') or [])})")
        new = p.stage.strip()
        if d.choice(f, new) is not None:
            options = list(f.get("options") or [])
            if options.index(d.choice(f, new)) == options.index(after) + 1:
                return []  # there already, where it's asked for: nothing to do
            raise Problem(f"{where}: {t['label']} already has the stage “{new}”")
        options = list(f.get("options") or [])
        options.insert(options.index(after) + 1, new)
        flow = copy.deepcopy(t.get("workflow") or {"field": f["key"], "transitions": []})
        onward = [mv for mv in flow.get("transitions", []) if after in (mv.get("from") or [])]
        for mv in onward:  # from the new stage, on wherever the one before it could go
            mv["from"] = list(mv["from"]) + [new]
        flow.setdefault("transitions", []).append({"from": [after], "to": new, "label": (p.button or f"Move to {new}")[:60]})
        return [{"op": "update_field", "table": t["key"], "key": f["key"], "changes": {**f, "options": options}},
                {"op": "update_table", "table": t["key"], "workflow": flow}]
    if isinstance(p, m.AddView):
        t = _list(d, p.list, where)
        v = p.view
        view = {"key": schema.slug(v.label), "label": v.label, "type": v.type}
        if v.filters:
            view["filters"] = ir_conditions(d, t, v.filters, f"{where} › filters")
        for attr, key in (("date_field", "start_field" if v.type == "timeline" else "date_field"), ("group_by", "group_by"),
                          ("end_field", "end_field"), ("image_field", "image_field")):
            name = getattr(v, attr)
            if name:
                f = d.field(t, name)
                if f is None and attr == "group_by" and "." in name:  # customer.name: grouped by the customer
                    f = d.field(t, name.split(".")[0])
                    f = f if f is not None and f["type"] == "link" else None
                if f is None:
                    raise Problem(f"{where} › {attr}: {name} isn't a field of {t['label']}")
                view[key] = f["key"]
        for name in v.columns:
            f = d.field(t, name)
            if f is None:
                raise Problem(f"{where} › columns: {name} isn't a field of {t['label']} (its fields: {', '.join(d.field_names(t))})")
            view.setdefault("columns", []).append(f["key"])
        if v.sort:
            f = d.field(t, v.sort.lstrip("-").strip())
            if f is None and v.sort.lstrip("-").strip() not in ("created_at", "updated_at"):
                raise Problem(f"{where} › sort: {v.sort.lstrip('-')} isn't a field of {t['label']}")
            view["sort"] = {"field": f["key"] if f else v.sort.lstrip("-").strip(), "dir": "desc" if v.sort.startswith("-") else "asc"}
        group = d.field(t, view.get("group_by") or "")
        if v.type == "board" and group is not None and group["type"] != "select":  # a board's columns are choices: by
            view["type"] = "list"  # anything else (a customer, a driver) it's the list grouped under each one
        return [{"op": "add_view", "table": t["key"], "view": view}]
    if isinstance(p, m.AddRecords):
        t = _list(d, p.list, where)
        return [{"op": "add_records", "table": t["key"], "records": p.records}]
    if isinstance(p, m.ImportRows):
        t = _list(d, p.list, where)
        mapping = {}
        for heading, name in p.columns.items():
            try:
                _, _, keys = d.path(t, name)
            except NameProblem as exc:
                child = next((x for x in d.lists if x["key"] == schema.slug(name.partition(".")[0])), None)
                if child is not None and d.field(child, name.partition(".")[2]):  # a line's field: order_lines.quantity
                    mapping[heading] = f"{child['key']}.{d.field(child, name.partition('.')[2])['key']}"
                    continue
                raise Problem(f"{where} › columns › {heading}: {exc}") from None
            mapping[heading] = ".".join(keys)
        op = {"op": "import_file", "table": t["key"], "file_id": p.file, "mapping": mapping}
        if p.sheet:
            op["sheet"] = p.sheet
        return [op]
    if isinstance(p, m.AddRule):
        _new_documents().clear()
        rule = ir_rule(d, p.rule, where)
        same = lambda a: {k: a.get(k) for k in ("trigger", "conditions", "actions")}  # noqa: E731
        twin = next((a for a in d.bp.get("automations", []) if a["key"] != rule["key"] and same(a) == same(rule)), None)
        if twin is not None:  # made twice, it would do everything twice
            raise Problem(f"{where}: the rule “{twin['name']}” does exactly this already")
        return _documents_for(d) + [{"op": "add_automation", "automation": rule}]
    if isinstance(p, m.AddDocument):
        t = _list(d, p.list, where)
        return [{"op": "add_document", "table": t["key"], "document": {"key": schema.slug(p.key), "label": p.label, "title": p.title or p.label}}]
    if isinstance(p, (m.ChangeRule, m.RemoveRule)):
        made = feature_rule(d.bp, p.rule)
        if made is not None and made["key"] != schema.slug(p.rule):  # named by its feature: that feature's one rule
            p = p.model_copy(update={"rule": made["key"]})
    if isinstance(p, m.ChangeRule):
        old = next((a for a in d.bp.get("automations", []) if a["key"] == schema.slug(p.rule) or a["name"].lower() == p.rule.lower()), None)
        if old is None:
            keys = [a["key"] for a in d.bp.get("automations", [])]
            raise Problem(f"{where}: there's no rule “{p.rule}” (rules: {', '.join(keys) or 'none'})")
        new = copy.deepcopy(old)
        new.pop("feature", None)  # a feature's rule changed is the writer's own (the feature gives it up)
        if p.name:
            new["name"] = p.name
        t = d.list(old["trigger"].get("table")) if old["trigger"].get("table") else None
        if p.when is not None:
            t, trigger, conds = ir_when(d, p.when, where)
            kept = [c for c in old.get("conditions", []) if c.get("op") not in ("changed", "changed_to")]
            new["trigger"], new["conditions"] = trigger, conds + kept
        if t is None and old["trigger"].get("type") == "schedule" and (p.only_if or p.steps):
            meant = _list_meant(d, p.only_if or [], p.steps or [])  # what it now checks is on one list only: about that list
            if meant is not None:
                t, new["trigger"] = meant, {**new["trigger"], "table": meant["key"]}
        if p.only_if is not None:
            if t is None and old["trigger"].get("type") == "schedule":
                raise Problem(f"{where}: this rule runs on a schedule about no list, so it has nothing to check: send its when "
                              f"again with the list ({{\"on\": \"every\", \"list\": …}}) and the only_if with it")
            if t is None:
                raise Problem(f"{where}: this rule isn't about a list's records, so it has no only_if")
            whens = [c for c in new.get("conditions", []) if c.get("op") in ("changed", "changed_to")]
            new["conditions"] = whens + ir_conditions(d, t, p.only_if, f"{where} › only_if")
        _new_documents().clear()
        if p.steps is not None:
            if t is None:
                raise Problem(f"{where}: steps need the list the rule is about")
            actions, made = [], None
            for n, s in enumerate(p.steps, start=1):
                step, made = ir_step(d, t, s, f"{where} › step {n}", made)
                actions.append(step)
            new["actions"] = actions
        if p.enabled is not None:
            new["enabled"] = bool(p.enabled)
        return _documents_for(d) + [{"op": "add_automation", "automation": new}]
    if isinstance(p, m.RemoveRule):
        old = next((a for a in d.bp.get("automations", []) if a["key"] == schema.slug(p.rule) or a["name"].lower() == p.rule.lower()), None)
        if old is None:
            raise Problem(f"{where}: there's no rule “{p.rule}”")
        return [{"op": "remove_automation", "key": old["key"]}]
    if isinstance(p, m.DesignOp):
        name = schema.slug(str(p.op.get("op") or ""))
        if name not in ESCAPE:
            raise Problem(f"{where}: “{name or 'no op'}” can't be sent as a design_op: design_op is only for "
                          f"{', '.join(sorted(ESCAPE))}; {_own_patch(name)}")
        if name == "update_table":
            other = set(p.op) - POLICY_KEYS
            if other:
                raise Problem(f"{where}: an update_table design_op may only set who may see and change a list's records "
                              f"({', '.join(sorted(POLICY_KEYS - {'op', 'table'}))}), not {', '.join(sorted(other))}: "
                              f"change_list, set_stages and the field patches change the rest")
        return [p.op]
    raise Problem(f"{where}: unknown patch")
