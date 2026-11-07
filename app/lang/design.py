"""A read-only view of a design (the compiled form the engine runs) that finds lists, fields, paths and stages by the
names a builder uses for them, and says plainly what's there when a name isn't."""
import difflib

from ..blueprint import schema

CONTEXT_NAMES = {"record", "this", "created", "item", "it", "today", "now", "approver", "reason", "email", "file", "payload", "true",
                 "false", "none", "null", "owner"}
TYPE_OF = {"yes_no": "checkbox", "choice": "select", "choices": "multi_select", "person": "user", "auto_number": "autonumber",
           "total": "rollup"}
TYPE_BACK = {v: k for k, v in TYPE_OF.items()}
NUMBERS = {"number", "money", "percent", "duration", "rating", "autonumber"}


def _s(name):
    return schema.slug(str(name or ""))


def closest(word, options, n=3):
    return difflib.get_close_matches(str(word), [str(o) for o in options], n=n, cutoff=0.5)


class Design:
    def __init__(self, bp):
        self.bp = bp

    # lists ----------------------------------------------------------------------------------------------------------
    @property
    def lists(self):
        return self.bp.get("tables", [])

    def list(self, name):
        wanted = _s(name)
        if not wanted:
            return None
        for t in self.lists:
            if t["key"] == wanted:
                return t
        for t in self.lists:
            names = {_s(t.get("label")), _s(t.get("singular"))}
            if wanted in names or wanted.rstrip("s") in names or wanted + "s" in names or wanted + "es" in names:
                return t
        return None

    def list_names(self):
        return [t["key"] for t in self.lists]

    # fields ---------------------------------------------------------------------------------------------------------
    def field(self, t, name):
        wanted = _s(name)
        if t is None or not wanted:
            return None
        for f in t.get("fields", []):
            if f["key"] == wanted:
                return f
        for f in t.get("fields", []):
            if _s(f.get("label")) == wanted:
                return f
        return None

    def field_names(self, t):
        return [f["key"] for f in (t or {}).get("fields", [])]

    def path(self, t, path):
        """(field, the list it's on, the keys of the path) for "status" or "order.customer.email", or raises
        NameProblem saying which part isn't there."""
        parts = [p for p in str(path or "").replace("record.", "", 1).split(".") if p] if str(path).startswith("record.") else \
            [p for p in str(path or "").split(".") if p]
        if not parts:
            raise NameProblem("a field is needed here")
        here, keys = t, []
        for i, part in enumerate(parts):
            f = self.field(here, part)
            if f is None:
                if part in schema.SYSTEM_TIMES.values() or part in ("created_at", "updated_at", "id"):
                    return {"key": part, "type": "datetime" if part != "id" else "number", "label": part}, here, keys + [part]
                near = closest(part, self.field_names(here))
                raise NameProblem(f"“{part}” isn't a field of {here['label']}" + (f" (did you mean {', '.join(near)}?)" if near else "")
                                  + f"; its fields: {', '.join(self.field_names(here))}")
            keys.append(f["key"])
            if i < len(parts) - 1 and f["type"] == "user" and parts[i + 1] in ("name", "email", "first_name") and i + 1 == len(parts) - 1:
                return {"key": parts[i + 1], "type": "email" if parts[i + 1] == "email" else "text", "label": parts[i + 1]}, here, keys + [parts[i + 1]]
            if i < len(parts) - 1:
                if f["type"] == "link":
                    here = self.list(f.get("table"))
                elif f["type"] == "lookup" and f.get("link"):
                    link = self.field(here, f["link"])
                    target = self.list((link or {}).get("table"))
                    inner, here2, _ = self.path(target, f.get("field") or "") if target else (None, None, None)
                    if not inner or inner.get("type") != "link":
                        raise NameProblem(f"“{part}” of {here['label']} doesn't link anywhere, so “{'.'.join(parts[i + 1:])}” can't be read through it")
                    here = self.list(inner.get("table"))
                else:
                    raise NameProblem(f"“{part}” of {here['label']} isn't a link, so “{'.'.join(parts[i + 1:])}” can't be read through it")
                if here is None:
                    raise NameProblem(f"“{part}” links to a list that isn't there")
        return f, here, keys

    def linking(self, t):
        """The lists whose records link to a record of t, as a formula names them (a client's invoices)."""
        out = []
        for other in self.lists:
            links = [f for f in other.get("fields", []) if f.get("type") == "link" and f.get("table") == (t or {}).get("key")]
            if links:
                out.append(other["key"] if len(links) == 1 else " or ".join(f"{other['key']}_by_{f['key']}" for f in links))
        return out

    def reach(self, t, path):
        """(field or None, the list reached, many) for a path a formula may read through the records linking back to a
        record (client.invoices: a client's invoices) or a whole list (bookings): ending at a field of those
        (client.invoices.amount), or at the records themselves. Raises NameProblem saying what's there."""
        parts = [p for p in str(path or "").split(".") if p]
        if parts and parts[0] in ("record", "this"):
            parts = parts[1:]
        here, many = t, False
        for i, part in enumerate(parts):
            f = self.field(here, part)
            if f is not None:
                if i == len(parts) - 1:
                    return f, here, many
                if f["type"] == "link":
                    here = self.list(f.get("table"))
                    if here is None:
                        raise NameProblem(f"“{part}” links to a list that isn't there")
                    continue
                raise NameProblem(f"“{part}” of {here['label']} isn't a link, so “{'.'.join(parts[i + 1:])}” can't be read through it")
            back = schema.backlink(self.lists, here, part)
            if back is not None:
                here, many = back[0], True
                continue
            whole = schema.list_named(self.lists, part) if i == 0 else None
            if whole is not None:
                here, many = whole, True
                continue
            if part in ("id", "created_at", "updated_at") and i == len(parts) - 1:
                return {"key": part, "type": "number" if part == "id" else "datetime", "label": part}, here, many
            near = closest(part, self.field_names(here))
            linking = self.linking(here)
            raise NameProblem(f"“{part}” isn't a field of {here['label']}" + (f" (did you mean {', '.join(near)}?)" if near else "")
                              + f"; its fields: {', '.join(self.field_names(here))}"
                              + (f"; the records linking to it: {', '.join(linking)}" if linking else ""))
        return None, here, many

    def kind(self, f):
        """What a field's values are: number, date, text, choice, link, yes_no…"""
        ftype = f.get("type")
        if ftype == "formula":
            if schema.dated_formula(next((x for x in self.lists if any(g is f for g in x.get("fields", []))), None), f):
                return "date"  # confirmed_at + 2h, left showing a number: a date
            return {"money": "number", "percent": "number", "checkbox": "yes_no"}.get(f.get("format"), f.get("format") or "number")
        if ftype == "rollup":
            return "number"
        if ftype == "lookup" and f.get("link"):  # a lookup is what it looks up (a job's wedding date is a date)
            t = next((x for x in self.lists if any(g is f for g in x.get("fields", []))), None)
            link = self.field(t, f["link"]) if t else None
            there = self.list((link or {}).get("table"))
            inner = self.field(there, str(f.get("field") or "").split(".")[0]) if there else None
            if inner is not None and inner is not f and inner.get("type") != "lookup":
                return self.kind(inner)
        if ftype in NUMBERS:
            return "number"
        if ftype in ("date", "datetime", "created_time", "modified_time"):
            return "date"
        if ftype == "checkbox":
            return "yes_no"
        if ftype in ("select", "multi_select"):
            return "choice"
        if ftype in ("link", "user"):
            return ftype
        return "text"

    def paths_to(self, t, types, depth=2, _here="", _seen=None):
        """Every path from list t to a field of these types, through links (customer.email, dog.owner.email)."""
        seen = _seen or {t["key"]} if t else set()
        out = []
        for f in (t or {}).get("fields", []):
            path = f"{_here}{f['key']}"
            if f["type"] in types:
                out.append(path)
            elif f["type"] == "link" and depth > 0 and f.get("table") not in seen:
                other = self.list(f.get("table"))
                if other is not None:
                    out += self.paths_to(other, types, depth - 1, f"{path}.", seen | {other["key"]})
        return out

    # stages ---------------------------------------------------------------------------------------------------------
    def stage_field(self, t):
        flow = (t or {}).get("workflow") or {}
        f = self.field(t, flow.get("field") or "status") if t else None
        return f if f and f["type"] == "select" else None

    def stages(self, t):
        f = self.stage_field(t)
        return list(f.get("options") or []) if f else []

    def choice(self, f, value):
        """The option as it's written, for a value written any way (case, spaces), or None."""
        options = f.get("options") or []
        for o in options:
            if str(o) == str(value):
                return o
        for o in options:
            if _s(o) == _s(value):
                return o
        return None

    # people ---------------------------------------------------------------------------------------------------------
    def teams(self):
        return [tm["key"] for tm in schema.teams(self.bp)]

    def who(self, t, name):
        """A role word, a team's key or a person field (as a path) for who's told or asked, or raises NameProblem."""
        word = _s(name)
        aliases = {"staff": "editor", "editors": "editor", "admins": "admin", "manager": "admin", "managers": "admin", "me": "owner",
                   "boss": "owner", "all": "everyone", "team": "everyone", "the_owner": "owner", "creator": "creator"}
        word = aliases.get(word, word)
        if word in schema.WHO_WORDS or word == "approver":  # approver: whoever answered the approval before this step
            return word
        if word in self.teams():
            return word
        try:
            f, _, keys = self.path(t, name)
        except NameProblem:
            f = None
        if f is not None and f.get("type") == "user":
            return ".".join(keys)
        people = [f["key"] for f in (t or {}).get("fields", []) if f["type"] == "user"]
        raise NameProblem(f"“{name}” isn't someone to tell: use owner, admin, editor, everyone" + (f", a team ({', '.join(self.teams())})" if self.teams() else "")
                          + (f" or a person field ({', '.join(people)})" if people else ""))


class NameProblem(Exception):
    pass
