Write a Python module that defines

```python
def find_slots(calendars, work_hours, duration, step=15, max_results=None) -> list[tuple[str, str]]: ...
```

It finds meeting slots for several people on one day.

- `calendars`: a list (one per person) of lists of busy intervals `("HH:MM", "HH:MM")` (start, end), 24-hour times. Intervals may overlap, touch, be unsorted, or be empty lists. An interval whose end is not after its start raises `ValueError`.
- `work_hours`: a list (one per person, same length as `calendars`) of `("HH:MM", "HH:MM")` working windows. A meeting must fit inside everyone's working window. A length mismatch raises `ValueError`.
- `duration`: meeting length in minutes (int > 0, else `ValueError`).
- A slot start must be a multiple of `step` minutes after midnight (`step` int > 0).
- A slot `[s, s+duration)` is valid when it lies inside every working window and does not overlap any busy interval. Touching is fine: busy `09:00-10:00` allows a meeting starting `10:00`.
- Return every valid slot as `("HH:MM", "HH:MM")` (start, end) in increasing start order, at most `max_results` of them if given. Overlapping candidate slots are all returned (with `step=15` and `duration=60`, `09:00-10:00` and `09:15-10:15` can both appear).
- Bad time strings (not `HH:MM`, hour > 23, minute > 59) raise `ValueError`. `"24:00"` is allowed only as an END time meaning midnight at the end of the day.

Return the complete module in one ```python code block.
