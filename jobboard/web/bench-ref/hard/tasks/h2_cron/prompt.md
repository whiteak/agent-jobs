Write a Python module that defines `next_fire(expr: str, after: datetime.datetime) -> datetime.datetime`: the first time strictly AFTER `after` at which a cron schedule fires. Times are naive datetimes; results always have `second == 0` and `microsecond == 0`. (If `after` is 10:00:30, a schedule firing every minute next fires at 10:01:00.)

`expr` has exactly five whitespace-separated fields: minute (0-59), hour (0-23), day of month (1-31), month (1-12), day of week (0-7, where both 0 and 7 mean Sunday). Leading/trailing whitespace is ignored.

Each field is a comma-separated list of items. An item is one of:
- `*` (every value), a number `n`, or a range `a-b` (inclusive, requires a <= b);
- any of those followed by a step `/s` (s >= 1): `*/15`, `10-50/20` (10, 30, 50), and `5/20`, which means `5-<max>/20`.
- Month names `JAN`-`DEC` and weekday names `SUN`-`SAT` may be used instead of numbers in their fields, in any case (`jan`, `Mon`), including in ranges (`MON-FRI`).

Day rule (as in classic Vixie cron): if the day-of-month field and the day-of-week field are BOTH restricted, the schedule fires on days matching EITHER of them. If either of them is unrestricted, only the other one has to match. A field counts as unrestricted exactly when its text starts with `*`. So with `*/2` in the day-of-month field and `MON` in the day-of-week field, a day must be odd AND a Monday (the OR rule does not apply, because `*/2` starts with `*`).

Macros: `@yearly` (= `0 0 1 1 *`), `@monthly` (`0 0 1 * *`), `@weekly` (`0 0 * * 0`), `@daily` (`0 0 * * *`), `@hourly` (`0 * * * *`).

Raise `ValueError` for: the wrong number of fields, a value out of range, a bad range (`a > b`), step 0, unknown names or characters, empty items, and schedules that do not fire within 10 years after `after` (e.g. `0 0 31 4 *`, or `0 0 30 2 *`). `0 0 29 2 *` is valid and fires on the next 29 February.

It must be fast: each call should take well under a second, even for schedules that fire rarely or never.

Return the complete module in one ```python code block.
