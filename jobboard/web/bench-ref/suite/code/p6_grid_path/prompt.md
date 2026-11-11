Write a Python module that defines

```python
def cheapest_route(grid: list[str], max_keys: int = 0) -> tuple[int, list[tuple[int, int]]] | None: ...
```

`grid` is a rectangular map of single-character cells:
- `S` start (exactly one), `E` exit (exactly one), `#` wall (impassable).
- A digit `1`-`9`: entering this cell costs that many points.
- `.` costs 1 to enter. `E` costs 1 to enter.
- A lowercase letter `a`-`d` is a key cell (costs 1 to enter); the matching uppercase `A`-`D` is a door (costs 1 to enter) that can only be entered while you hold its key. Picking up a key is automatic when you enter its cell, and keys are never used up. `max_keys` limits how many DIFFERENT keys you may hold in total: once you hold `max_keys` keys, entering another key cell is allowed but does not pick that key up. Cells may be visited more than once.
- Moves are up, down, left, right. The start cell is not paid for; re-entering `S` later costs 1.

Return `(total_cost, path)` for the cheapest way from `S` to `E`, where `path` is the list of `(row, col)` cells from `S` to `E` inclusive, or `None` if `E` cannot be reached. If several paths share the minimum cost, any of them is accepted. A grid that is empty, has zero or several `S` or `E` cells, non-rectangular rows, or any other character raises `ValueError`.

It must solve a 200 x 200 grid with 4 kinds of keys and `max_keys=2` in under 5 seconds.

Return the complete module in one ```python code block.
