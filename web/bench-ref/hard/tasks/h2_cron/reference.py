import datetime as dt

MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
DAYS = {d: i for i, d in enumerate("SUN MON TUE WED THU FRI SAT".split())}
MACROS = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
          "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *"}


def val(tok, lo, hi, names):
    t = tok.upper()
    if names and t in names:
        v = names[t]
    elif t.isdigit():
        v = int(t)
    else:
        raise ValueError(tok)
    if not lo <= v <= hi:
        raise ValueError(tok)
    return v


def field(text, lo, hi, names=None):
    out = set()
    for item in text.split(","):
        if not item:
            raise ValueError("empty item")
        step = 1
        if "/" in item:
            base, s = item.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                raise ValueError(item)
            step = int(s)
        else:
            base = item
        if base == "*":
            a, b = lo, hi
        elif "-" in base:
            x, y = base.split("-", 1)
            a, b = val(x, lo, hi, names), val(y, lo, hi, names)
            if a > b:
                raise ValueError(item)
        else:
            a = val(base, lo, hi, names)
            b = hi if "/" in item else a
        out.update(range(a, b + 1, step))
    return out


def next_fire(expr, after):
    expr = expr.strip()
    expr = MACROS.get(expr.lower(), expr)
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("fields")
    mins = field(parts[0], 0, 59)
    hrs = field(parts[1], 0, 23)
    dom = field(parts[2], 1, 31)
    mon = field(parts[3], 1, 12, MONTHS)
    dow = {d % 7 for d in field(parts[4], 0, 7, DAYS)}
    dom_star, dow_star = parts[2].startswith("*"), parts[4].startswith("*")

    def day_ok(d):
        if d.month not in mon:
            return False
        a = d.day in dom
        b = (d.weekday() + 1) % 7 in dow
        if dom_star or dow_star:
            return a and b
        return a or b

    t = after.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    limit = after + dt.timedelta(days=3653)
    day = t.date()
    first = True
    while dt.datetime.combine(day, dt.time()) <= limit:
        if day_ok(day):
            for h in sorted(hrs):
                for m in sorted(mins):
                    c = dt.datetime(day.year, day.month, day.day, h, m)
                    if c >= t:
                        if c > limit:
                            raise ValueError("never")
                        return c
        day += dt.timedelta(days=1)
    raise ValueError("never")
