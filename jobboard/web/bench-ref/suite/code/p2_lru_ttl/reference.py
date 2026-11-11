import time
from collections import OrderedDict


class TTLCache:
    def __init__(self, capacity, ttl, clock=time.monotonic):
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise ValueError("capacity")
        if not ttl > 0:
            raise ValueError("ttl")
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self._d = OrderedDict()

    def _live(self, key):
        e = self._d.get(key)
        if e is None:
            return None
        if self.clock() >= e[1]:
            del self._d[key]
            return None
        return e

    def _purge(self):
        now = self.clock()
        for k in [k for k, e in self._d.items() if now >= e[1]]:
            del self._d[k]

    def get(self, key, default=None):
        e = self._live(key)
        if e is None:
            return default
        self._d.move_to_end(key)
        return e[0]

    def put(self, key, value, ttl=None):
        t = self.ttl if ttl is None else ttl
        if not t > 0:
            raise ValueError("ttl")
        if self._live(key) is None:
            if len(self._d) >= self.capacity:
                self._purge()
            if len(self._d) >= self.capacity:
                self._d.popitem(last=False)
        self._d[key] = (value, self.clock() + t)
        self._d.move_to_end(key)

    def delete(self, key):
        if self._live(key) is None:
            return False
        del self._d[key]
        return True

    def __len__(self):
        self._purge(); return len(self._d)

    def __contains__(self, key):
        e = self._d.get(key)
        return e is not None and self.clock() < e[1]

    def keys(self):
        self._purge(); return list(self._d)
