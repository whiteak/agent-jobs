import re

_TOK = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|(.))")


def _tokens(s):
    out, i = [], 0
    while i < len(s):
        if s[i] in " \t":
            i += 1; continue
        m = re.match(r"\d+(\.\d+)?", s[i:])
        if m:
            j = i + m.end()
            if j < len(s) and (s[j] == "." or s[j].isdigit()):
                raise ValueError("bad number")
            out.append(("n", float(m.group()))); i = j; continue
        if s[i] in "+-*/^()":
            out.append(("o", s[i])); i += 1; continue
        raise ValueError("bad char")
    return out


def evaluate(expr):
    toks = _tokens(expr); pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else (None, None)

    def take():
        nonlocal pos
        t = peek()
        if t[0] is None:
            raise ValueError("eof")
        pos += 1; return t

    def add():
        v = mul()
        while peek() in (("o", "+"), ("o", "-")):
            op = take()[1]; r = mul(); v = v + r if op == "+" else v - r
        return v

    def mul():
        v = unary()
        while peek() in (("o", "*"), ("o", "/")):
            op = take()[1]; r = unary()
            if op == "/":
                if r == 0:
                    raise ZeroDivisionError("div by zero")
                v = v / r
            else:
                v = v * r
        return v

    def unary():
        if peek() == ("o", "-"):
            take(); return -unary()
        return power()

    def power():
        b = atom()
        if peek() == ("o", "^"):
            take(); e = unary(); return float(b ** e)
        return b

    def atom():
        k, v = take()
        if k == "n":
            return v
        if v == "(":
            r = add()
            if take() != ("o", ")"):
                raise ValueError("expected )")
            return r
        raise ValueError("unexpected token")

    v = add()
    if pos != len(toks):
        raise ValueError("trailing")
    return float(v)
