import pprint
from itertools import chain
from collections import abc

_marker = object()


class MultiDict(abc.Mapping):
    """Read-only ordered dictionary that can have multiple values for each key.

    This type of MultiDict must be used for request headers and query args.
    """

    __slots__ = ('_items',)

    def __init__(self, *args, **kwargs):
        if len(args) > 1:
            raise TypeError("MultiDict takes at most 2 positional "
                            "arguments ({} given)".format(len(args) + 1))
        self._items = []
        if args:
            if hasattr(args[0], 'items'):
                args = list(args[0].items())
            else:
                args = list(args[0])
                for arg in args:
                    if not len(arg) == 2:
                        raise TypeError(
                            "MultiDict takes either dict or list of \
                            (key, value) tuples"
                        )

        self._fill(chain(args, kwargs.items()))

    def _fill(self, ipairs):
        self._items.extend(ipairs)

    def getall(self, key, default=_marker):
        """
        Return a list of all values matching the key (may be an empty list)
        """
        res = tuple([v for k, v in self._items if k == key])
        if res:
            return res
        if not res and default != _marker:
            return default
        raise KeyError('Key not found: %r' % key)

    def getone(self, key, default=_marker):
        """
        Get one value matching the key, raising a KeyError if multiple
        values were found.
        """
        v = self.getall(key, default=_marker)
        if len(v) > 1 and v != default:
            raise KeyError('Multiple values match %r: %r' % (key, v))
        return v[0]

    # extra methods #

    def copy(self):
        """Returns a copy itself."""
        cls = self.__class__
        return cls(self.items(getall=True))

    # Mapping interface #

    def __getitem__(self, key):
        for k, v in self._items:
            if k == key:
                return v
        raise KeyError(key)

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def keys(self, *, getall=False):
        return _KeysView(self._items, getall=getall)

    def items(self, *, getall=False):
        return _ItemsView(self._items, getall=getall)

    def values(self, *, getall=False):
        return _ValuesView(self._items, getall=getall)

    def __eq__(self, other):
        if not isinstance(other, abc.Mapping):
            return NotImplemented
        if isinstance(other, MultiDict):
            return self._items == other._items
        return dict(self.items()) == dict(other.items())

    def __contains__(self, key):
        for k, v in self._items:
            if k == key:
                return True
        return False

    def __repr__(self):
        return '<{}>\n{}'.format(
            self.__class__.__name__, pprint.pformat(
                list(self.items(getall=True)))
        )


class CaseInsensitiveMultiDict(MultiDict):
    """Case insensitive multi dict."""

    @classmethod
    def _from_uppercase_multidict(cls, dct):
        # NB: doesn't check for uppercase keys!
        ret = cls.__new__(cls)
        ret._items = dct._items
        return ret

    def _fill(self, ipairs):
        for key, value in ipairs:
            uppkey = key.upper()
            self._items.append((uppkey, value))

    def getall(self, key, default=_marker):
        return super().getall(key.upper(), default)

    def getone(self, key, default=_marker):
        return super().getone(key.upper(), default)

    def __getitem__(self, key):
        return super().__getitem__(key.upper())

    def __contains__(self, key):
        return super().__contains__(key.upper())


class MutableMultiDictMixin(abc.MutableMapping):

    def add(self, key, value):
        """
        Add the key and value, not overwriting any previous value.
        """
        self._items.append((key, value))

    def extend(self, *args, **kwargs):
        """Extends current MutableMultiDict with more values.

        This method must be used instead of update.
        """
        if len(args) > 1:
            raise TypeError("extend takes at most 2 positional arguments"
                            " ({} given)".format(len(args) + 1))
        if args:
            if isinstance(args[0], MultiDict):
                items = args[0].items(getall=True)
            elif hasattr(args[0], 'items'):
                items = args[0].items()
            else:
                items = args[0]
        else:
            items = []
        for key, value in chain(items, kwargs.items()):
            self.add(key, value)

    def clear(self):
        """Remove all items from MutableMultiDict"""
        self._items.clear()

    # MutableMapping interface #

    def __setitem__(self, key, value):
        try:
            del self[key]
        except KeyError:
            pass
        self._items.append((key, value))

    def __delitem__(self, key):
        items = self._items
        found = False
        for i in range(len(items)-1, -1, -1):
            if items[i][0] == key:
                del items[i]
                found = True
        if not found:
            raise KeyError(key)

    def pop(self, key, default=None):
        """Method not allowed."""
        raise NotImplementedError

    def popitem(self):
        """Method not allowed."""
        raise NotImplementedError

    def update(self, *args, **kw):
        """Method not allowed."""
        raise NotImplementedError("Use extend method instead")


class MutableMultiDict(MutableMultiDictMixin, MultiDict):
    """An ordered dictionary that can have multiple values for each key."""


class CaseInsensitiveMutableMultiDict(
        MutableMultiDictMixin, CaseInsensitiveMultiDict):
    """An ordered dictionary that can have multiple values for each key."""

    def add(self, key, value):
        super().add(key.upper(), value)

    def __setitem__(self, key, value):
        super().__setitem__(key.upper(), value)

    def __delitem__(self, key):
        super().__delitem__(key.upper())


class _ItemsView(abc.ItemsView):

    def __init__(self, items, *, getall=False):
        self._getall = getall
        self._keys = [item[0] for item in items]
        if not getall:
            self._keys = set(self._keys)

        items_to_use = []
        if getall:
            items_to_use = items
        else:
            for key in self._keys:
                for k, v in items:
                    if k == key:
                        items_to_use.append((k, v))
                        break
        assert len(items_to_use) == len(self._keys)

        super().__init__(items_to_use)

    def __contains__(self, item):
        assert isinstance(item, tuple) or isinstance(item, list)
        assert len(item) == 2
        return item in self._mapping

    def __iter__(self):
        yield from self._mapping


class _ValuesView(abc.ValuesView):

    def __init__(self, items, *, getall=False):
        self._getall = getall
        self._keys = [item[0] for item in items]
        if not getall:
            self._keys = set(self._keys)

        items_to_use = []
        if getall:
            items_to_use = items
        else:
            for key in self._keys:
                for k, v in items:
                    if k == key:
                        items_to_use.append((k, v))
                        break

        assert len(items_to_use) == len(self._keys)

        super().__init__(items_to_use)

    def __contains__(self, value):
        values = [item[1] for item in self._mapping]
        return value in values

    def __iter__(self):
        values = (item[1] for item in self._mapping)
        yield from values


class _KeysView(abc.KeysView):

    def __init__(self, items, *, getall=False):
        self._getall = getall
        self._keys = [item[0] for item in items]
        if not getall:
            self._keys = set(self._keys)

        items_to_use = []
        if getall:
            items_to_use = items
        else:
            for key in self._keys:
                for k, v in items:
                    if k == key:
                        items_to_use.append((k, v))
                        break
        assert len(items_to_use) == len(self._keys)

        super().__init__(items_to_use)

    def __contains__(self, key):
        return key in self._keys

    def __iter__(self):
        yield from self._keys
