"""What a request carries, in the shapes the views read it in: form and query values (a key can be sent more than once),
headers, and uploaded files."""
import shutil

from .exceptions import BadRequestKeyError


class MultiDict(dict):
    """Values by key, where a key may come more than once (ticked boxes, several files). As a dict it holds each key's
    first value, so `form["x"]`, `dict(form)` and `json.dumps(form)` see one value; `getlist` gives them all.
    A key that isn't there is a 400 to the browser (BadRequestKeyError), as a missing form value always has been."""

    def __init__(self, items=None):
        super().__init__()
        self._lists = {}
        if items is None:
            return
        if isinstance(items, MultiDict):
            items = items.items(multi=True)
        elif isinstance(items, dict):
            items = [(k, x) for k, v in items.items() for x in (v if isinstance(v, (list, tuple)) else [v])]
        for key, value in items:
            self.add(key, value)

    def __getitem__(self, key):
        try:
            return dict.__getitem__(self, key)
        except KeyError:
            raise BadRequestKeyError(key) from None

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        self._lists[key] = [value]

    def __delitem__(self, key):
        dict.__delitem__(self, key)
        self._lists.pop(key, None)

    def add(self, key, value):
        if key not in self._lists:
            dict.__setitem__(self, key, value)
            self._lists[key] = []
        self._lists[key].append(value)

    def get(self, key, default=None, type=None):  # noqa: A002 - the name every view uses
        try:
            value = dict.__getitem__(self, key)
        except KeyError:
            return default
        if type is not None:
            try:
                return type(value)
            except (ValueError, TypeError):
                return default
        return value

    def getlist(self, key, type=None):  # noqa: A002
        values = list(self._lists.get(key, []))
        if type is None:
            return values
        out = []
        for value in values:
            try:
                out.append(type(value))
            except (ValueError, TypeError):
                pass
        return out

    def setlist(self, key, values):
        values = list(values)
        if not values:
            self.pop(key, None)
            return
        dict.__setitem__(self, key, values[0])
        self._lists[key] = values

    def items(self, multi=False):
        if multi:
            return [(k, v) for k, values in self._lists.items() for v in values]
        return list(dict.items(self))

    def lists(self):
        return [(k, list(v)) for k, v in self._lists.items()]

    def values(self):
        return list(dict.values(self))

    def pop(self, key, *default):
        self._lists.pop(key, None)
        return dict.pop(self, key, *default)

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return dict.__getitem__(self, key)

    def update(self, other=(), **more):
        for key, value in MultiDict(other).items(multi=True) + list(more.items()):
            self.add(key, value)

    def clear(self):
        dict.clear(self)
        self._lists.clear()

    def to_dict(self, flat=True):
        if flat:
            return dict(dict.items(self))
        return {k: list(v) for k, v in self._lists.items()}

    def copy(self):
        return type(self)(self.items(multi=True))

    def __copy__(self):
        return self.copy()

    def __reduce__(self):
        return type(self), (self.items(multi=True),)

    def __repr__(self):
        return f"{type(self).__name__}({self.items(multi=True)!r})"


class CombinedMultiDict(MultiDict):
    """`request.values`: the query's values, then the form's."""

    def __init__(self, dicts):
        super().__init__()
        for d in dicts:
            for key, value in d.items(multi=True):
                self.add(key, value)


class Headers:
    """A response's (or a request's) headers: names in any case, a name may come more than once (Set-Cookie)."""

    def __init__(self, items=None):
        self._list = []
        if items is None:
            return
        if isinstance(items, Headers):
            items = items.items()
        elif isinstance(items, dict):
            items = items.items()
        for key, value in items:
            self.add(key, value)

    def __getitem__(self, key):
        found = self.get(key)
        if found is None:
            raise KeyError(key)
        return found

    def get(self, key, default=None, type=None):  # noqa: A002
        low = key.lower()
        for k, v in self._list:
            if k.lower() == low:
                if type is not None:
                    try:
                        return type(v)
                    except (ValueError, TypeError):
                        return default
                return v
        return default

    def getlist(self, key):
        low = key.lower()
        return [v for k, v in self._list if k.lower() == low]

    get_all = getlist

    def __contains__(self, key):
        low = key.lower()
        return any(k.lower() == low for k, _ in self._list)

    def __setitem__(self, key, value):
        self.set(key, value)

    def set(self, key, value):
        low, value = key.lower(), str(value)
        for i, (k, _) in enumerate(self._list):
            if k.lower() == low:
                self._list[i] = (key, value)
                self._list[i + 1:] = [(kk, vv) for kk, vv in self._list[i + 1:] if kk.lower() != low]
                return
        self._list.append((key, value))

    def setdefault(self, key, default):
        if key in self:
            return self[key]
        self.set(key, default)
        return str(default)

    def add(self, key, value):
        self._list.append((key, str(value)))

    def extend(self, items):
        for key, value in (items.items() if hasattr(items, "items") else items):
            self.add(key, value)

    def update(self, items):
        for key, value in (items.items() if hasattr(items, "items") else items):
            self.set(key, value)

    def __delitem__(self, key):
        low = key.lower()
        self._list = [(k, v) for k, v in self._list if k.lower() != low]

    def pop(self, key, default=None):
        found = self.get(key, default)
        del self[key]
        return found

    def remove(self, key):
        del self[key]

    def items(self):
        return list(self._list)

    def keys(self):
        return [k for k, _ in self._list]

    def values(self):
        return [v for _, v in self._list]

    def __iter__(self):
        return iter(list(self._list))

    def __len__(self):
        return len(self._list)

    def __bool__(self):
        return bool(self._list)

    def to_list(self):
        return list(self._list)

    def __repr__(self):
        return f"Headers({self._list!r})"

    def __str__(self):
        return "".join(f"{k}: {v}\r\n" for k, v in self._list) + "\r\n"


class FileStorage:
    """An uploaded file: its name, its type, and its contents to read (or save) once."""

    def __init__(self, stream=None, filename=None, name=None, content_type=None, headers=None):
        self.stream = stream
        self.filename = filename
        self.name = name
        self.content_type = content_type
        self.headers = headers or Headers()

    @property
    def mimetype(self):
        return (self.content_type or "").split(";")[0].strip().lower()

    @property
    def mimetype_params(self):
        params = {}
        for part in (self.content_type or "").split(";")[1:]:
            key, _, value = part.partition("=")
            params[key.strip().lower()] = value.strip().strip('"')
        return params

    def read(self, *args):
        return self.stream.read(*args)

    def save(self, dst, buffer_size=16384):
        if isinstance(dst, (str, bytes)) or hasattr(dst, "__fspath__"):
            with open(dst, "wb") as out:
                shutil.copyfileobj(self.stream, out, buffer_size)
        else:
            shutil.copyfileobj(self.stream, dst, buffer_size)

    def close(self):
        try:
            self.stream.close()
        except Exception:  # noqa: BLE001 - closing what's already gone
            pass

    def __getattr__(self, name):
        if name == "stream":
            raise AttributeError(name)
        return getattr(self.stream, name)

    def __iter__(self):
        return iter(self.stream)

    def __bool__(self):
        return bool(self.filename)

    def __repr__(self):
        return f"<FileStorage: {self.filename!r} ({self.content_type!r})>"
