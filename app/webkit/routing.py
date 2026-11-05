"""Addresses and the views behind them, written as they always have been: `/w/<int:wid>/app/t/<table>`.

Matching goes one part of the address at a time, and a part written out in full ("new") is tried before one that takes
any value ("<rid>"); among those, a number before any text, and more fixed text before less ("<key>.pdf" before
"<key>"). So `/t/orders/new` is the new-order form, never order "new". An address written with a closing slash is also
found without it, and sent on to it.

Building an address (url_for) fills in the parts the rule names; anything else becomes the query string."""
import re
from urllib.parse import quote, urlencode

from .exceptions import MethodNotAllowed, NotFound, RequestRedirect

_PART = re.compile(r"<(?:(?P<converter>[a-zA-Z_][a-zA-Z0-9_]*):)?(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)>")
_URL_SAFE = "!$&'()*+,/:;=@"


class Converter:
    regex = "[^/]+"
    weight = 100
    final = False

    def to_python(self, value):
        return value

    def to_url(self, value):
        return quote(str(value), safe=_URL_SAFE)


class IntConverter(Converter):
    regex = r"\d+"
    weight = 50

    def to_python(self, value):
        return int(value)

    def to_url(self, value):
        return str(int(value))


class PathConverter(Converter):
    regex = "[^/].*?"
    weight = 200
    final = True


CONVERTERS = {"default": Converter, "string": Converter, "int": IntConverter, "path": PathConverter}


class _Segment:
    """One part of a rule between slashes: fixed text, or a pattern with values in it."""

    def __init__(self, text, converters):
        self.text = text
        self.static = not _PART.search(text)
        self.final = False
        self.names = []
        if self.static:
            return
        pattern, statics, arguments, pos = "", [], [], 0
        for m in _PART.finditer(text):
            if m.start() > pos:
                fixed = text[pos:m.start()]
                statics.append((len(statics), -len(fixed)))
                pattern += re.escape(fixed)
            conv = converters[m.group("name")]
            arguments.append(conv.weight)
            self.final = self.final or conv.final
            self.names.append(m.group("name"))
            pattern += f"({conv.regex})"
            pos = m.end()
        if pos < len(text):
            fixed = text[pos:]
            statics.append((len(statics), -len(fixed)))
            pattern += re.escape(fixed)
        self.regex = re.compile(f"^{pattern}$")
        self.weight = (-len(statics), statics, -len(arguments), arguments)

    def key(self):
        return self.text


class Rule:
    def __init__(self, rule, endpoint, view_func, methods=None):
        self.rule = rule
        self.endpoint = endpoint
        self.view_func = view_func
        methods = {m.upper() for m in (methods or ["GET"])}
        if "GET" in methods:
            methods.add("HEAD")
        methods.add("OPTIONS")
        self.methods = methods
        self.converters = {}
        for m in _PART.finditer(rule):
            name = m.group("converter") or "default"
            if name not in CONVERTERS:
                raise ValueError(f"The address {rule!r} uses a converter the platform doesn't have: {name}")
            self.converters[m.group("name")] = CONVERTERS[name]()
        self.arguments = set(self.converters)
        self.strict_slash = rule.endswith("/") and rule != "/"
        body = rule.strip("/")
        self.segments = [_Segment(part, self.converters) for part in body.split("/")] if body else []

    def build(self, values):
        """(path, the values it didn't use)."""
        def fill(m):
            name = m.group("name")
            return self.converters[name].to_url(values[name])
        path = _PART.sub(fill, self.rule)
        return path, {k: v for k, v in values.items() if k not in self.arguments}

    def __repr__(self):
        return f"<Rule {self.rule!r} ({', '.join(sorted(self.methods))}) -> {self.endpoint}>"


class _State:
    __slots__ = ("static", "dynamic", "rules", "slash_rules")

    def __init__(self):
        self.static, self.dynamic, self.rules, self.slash_rules = {}, [], [], []


class Map:
    def __init__(self):
        self._root = _State()
        self.rules = []
        self.by_endpoint = {}

    def add(self, rule):
        self.rules.append(rule)
        self.by_endpoint.setdefault(rule.endpoint, []).append(rule)
        state = self._root
        for segment in rule.segments:
            if segment.static:
                state = state.static.setdefault(segment.text, _State())
            else:
                for seg, nxt in state.dynamic:
                    if seg.text == segment.text:
                        state = nxt
                        break
                else:
                    nxt = _State()
                    state.dynamic.append((segment, nxt))
                    state.dynamic.sort(key=lambda entry: entry[0].weight)
                    state = nxt
        (state.slash_rules if rule.strict_slash else state.rules).append(rule)

    def match(self, path, method):
        """(rule, its values), or raises NotFound, MethodNotAllowed, or RequestRedirect to the address with its slash."""
        parts = [p for p in path.strip("/").split("/")] if path.strip("/") else []
        slashed = path.endswith("/") and path != "/"
        allowed = set()

        def walk(state, rest, values):
            if not rest:
                pool = state.slash_rules if slashed else state.rules
                for rule in pool:
                    if method in rule.methods:
                        return rule, values
                    allowed.update(rule.methods)
                if not slashed and state.slash_rules:
                    if any(method in rule.methods for rule in state.slash_rules):
                        raise _SlashRequired()
                return None
            part = rest[0]
            if part in state.static:
                found = walk(state.static[part], rest[1:], values)
                if found is not None:
                    return found
            for segment, nxt in state.dynamic:
                target, remaining = (("/".join(rest), []) if segment.final else (part, rest[1:]))
                m = segment.regex.match(target)
                if m is None:
                    continue
                found = walk(nxt, remaining, values + list(m.groups()))
                if found is not None:
                    return found
            return None

        try:
            found = walk(self._root, parts, [])
        except _SlashRequired:
            raise RequestRedirect(path + "/") from None
        if found is None:
            if allowed:
                raise MethodNotAllowed(valid_methods=sorted(allowed))
            raise NotFound()
        rule, raw = found
        values = {}
        for name, value in zip(_names(rule), raw):
            try:
                values[name] = rule.converters[name].to_python(value)
            except (ValueError, TypeError):
                raise NotFound() from None
        return rule, values

    def build(self, endpoint, values, method=None):
        """The path for an endpoint (and its query string from the values the rule doesn't name)."""
        rules = self.by_endpoint.get(endpoint)
        if not rules:
            raise BuildError(endpoint, values, "there's no view by that name")
        given = {k: v for k, v in values.items() if v is not None}
        for rule in sorted(rules, key=lambda r: -len(r.arguments)):
            if rule.arguments <= set(given) and (method is None or method in rule.methods):
                path, rest = rule.build(given)
                query = _query(rest)
                return path + (f"?{query}" if query else "")
        missing = sorted(rules[0].arguments - set(given))
        raise BuildError(endpoint, values, f"it needs {', '.join(missing)}" if missing else "no rule fits")


def _names(rule):
    out = []
    for segment in rule.segments:
        out.extend(segment.names)
    return out


def _query(values):
    items = []
    for key, value in values.items():
        if isinstance(value, (list, tuple, set)):
            items.extend((key, v) for v in value if v is not None)
        elif value is not None:
            items.append((key, value))
    return urlencode(items, safe="!$'()*,/:;?@")


class _SlashRequired(Exception):
    pass


class BuildError(LookupError):
    def __init__(self, endpoint, values, why):
        super().__init__(f"Could not build url for endpoint {endpoint!r} with values {sorted(values)}: {why}.")
        self.endpoint, self.values = endpoint, values
