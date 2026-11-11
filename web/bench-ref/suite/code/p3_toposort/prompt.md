Write a Python module that defines

```python
def plan(tasks: dict[str, list[str]]) -> tuple[str, list[str]]: ...
```

`tasks` maps each task name to the list of task names it depends on (those must run first). A dependency name that is not a key of `tasks` is an external task: treat it as a task with no dependencies.

- If the tasks can be ordered, return `("order", order)` where `order` contains every task (including external ones) exactly once, every task after all its dependencies, and among all valid orders it is the lexicographically smallest (compare names with normal Python string comparison, at each step pick the smallest available task).
- If there is a dependency cycle, return `("cycle", cycle)` where `cycle` is a list of distinct task names `[t0, t1, ..., tk]` such that each `t(i)` depends directly on `t(i+1)` and `tk` depends directly on `t0`. A task that depends on itself is the cycle `[t]`. Any cycle is accepted.
- Duplicate entries in a dependency list are allowed and count once.
- Must handle 50,000 tasks in well under 2 seconds and must not hit Python's recursion limit.

Return the complete module in one ```python code block.
