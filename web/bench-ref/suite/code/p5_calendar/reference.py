import re


def _t(s, end=False):
    if not isinstance(s, str) or not re.fullmatch(r"\d\d:\d\d", s):
        raise ValueError(s)
    h, m = int(s[:2]), int(s[3:])
    if end and h == 24 and m == 0:
        return 1440
    if h > 23 or m > 59:
        raise ValueError(s)
    return h * 60 + m


def _f(x):
    return f"{x // 60:02d}:{x % 60:02d}"


def find_slots(calendars, work_hours, duration, step=15, max_results=None):
    if len(calendars) != len(work_hours):
        raise ValueError("length")
    if not isinstance(duration, int) or duration <= 0 or not isinstance(step, int) or step <= 0:
        raise ValueError("duration/step")
    busy = []
    for cal in calendars:
        for a, b in cal:
            s, e = _t(a), _t(b, True)
            if e <= s:
                raise ValueError("interval")
            busy.append((s, e))
    lo, hi = 0, 1440
    for a, b in work_hours:
        s, e = _t(a), _t(b, True)
        if e <= s:
            raise ValueError("window")
        lo, hi = max(lo, s), min(hi, e)
    out = []
    start = -(-lo // step) * step
    while start + duration <= hi:
        end = start + duration
        if all(e <= start or s >= end for s, e in busy):
            out.append((_f(start), _f(end)))
            if max_results is not None and len(out) >= max_results:
                break
        start += step
    return out
