Write a Python module that defines `class TTLCache`, a least-recently-used cache whose entries also expire.

```python
class TTLCache:
    def __init__(self, capacity: int, ttl: float, clock=time.monotonic): ...
    def get(self, key, default=None): ...
    def put(self, key, value, ttl: float | None = None) -> None: ...
    def delete(self, key) -> bool: ...
    def __len__(self) -> int: ...
    def __contains__(self, key) -> bool: ...
    def keys(self) -> list: ...
```

Behaviour:
- `capacity` must be an int >= 1 and `ttl` a number > 0, else raise `ValueError`.
- `clock()` returns the current time in seconds. Always read time through `clock`, never directly.
- `put` stores the value with expiry `clock() + (ttl if ttl is not None else self.ttl)`. A per-call `ttl` <= 0 raises `ValueError`. Putting an existing key replaces its value and expiry and makes it most recently used.
- An entry is expired when `clock() >= expiry`. Expired entries behave as absent everywhere: `get` returns `default`, `in` is False, `len` does not count them, `keys()` leaves them out, `delete` returns False for them.
- `get` of a live key makes it most recently used. `in` does NOT change recency.
- When `put` adds a NEW key and the number of live entries is already `capacity`, first discard every expired entry; if still full, evict the least recently used live entry.
- `keys()` returns live keys from least to most recently used.
- `delete` removes the key and returns True if it was live.
- `get`, `put`, `delete` must be O(1) on average (excluding the purge of expired entries).

Return the complete module in one ```python code block.
