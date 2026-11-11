# Thompson NFA: parse to AST, simulate with state sets.

def _parse(p):
    i = 0

    def alt():
        nonlocal i
        branches = [seq()]
        while i < len(p) and p[i] == "|":
            i += 1; branches.append(seq())
        return ("alt", branches) if len(branches) > 1 else branches[0]

    def seq():
        nonlocal i
        items = []
        while i < len(p) and p[i] not in "|)":
            a = atom()
            while i < len(p) and p[i] in "*+?":
                if a[0] == "q":
                    raise ValueError("double quantifier")
                a = ("q", p[i], a); i += 1
            items.append(a)
        return ("seq", items)

    def atom():
        nonlocal i
        c = p[i]
        if c in "*+?":
            raise ValueError("nothing to repeat")
        if c == "(":
            i += 1; a = alt()
            if i >= len(p) or p[i] != ")":
                raise ValueError("unbalanced")
            i += 1; return ("g", a)
        if c == "\\":
            if i + 1 >= len(p):
                raise ValueError("trailing backslash")
            i += 2; return ("c", lambda ch, x=p[i - 1]: ch == x)
        if c == ".":
            i += 1; return ("c", lambda ch: True)
        if c == "[":
            return cls()
        i += 1; return ("c", lambda ch, x=c: ch == x)

    def cls():
        nonlocal i
        i += 1; neg = False
        if i < len(p) and p[i] == "^":
            neg = True; i += 1
        items = []; first = True
        while True:
            if i >= len(p):
                raise ValueError("unterminated class")
            c = p[i]
            if c == "]" and not first:
                i += 1; break
            if c == "\\":
                if i + 1 >= len(p):
                    raise ValueError("trailing backslash")
                c = p[i + 1]; i += 2
            else:
                i += 1
            if i + 1 < len(p) and p[i] == "-" and p[i + 1] != "]":
                hi = p[i + 1]; i += 2
                if hi == "\\":
                    if i >= len(p):
                        raise ValueError("bad")
                    hi = p[i]; i += 1
                items.append((c, hi))
            else:
                items.append((c, c))
            first = False
        return ("c", lambda ch, it=tuple(items), n=neg: any(a <= ch <= b for a, b in it) != n)

    tree = alt()
    if i != len(p):
        raise ValueError("unbalanced )")
    return tree


def _compile(node, states):
    # returns (start, accept) ; states: list of [kind, pred, out1, out2]
    def new(kind="eps", pred=None):
        states.append([kind, pred, None, None]); return len(states) - 1

    k = node[0]
    if k == "c":
        s = new("chr", node[1]); e = new(); states[s][2] = e; return s, e
    if k == "g":
        return _compile(node[1], states)
    if k == "seq":
        s = new(); cur = s
        for it in node[1]:
            a, b = _compile(it, states); states[cur][2] = a; cur = b
        return s, cur
    if k == "alt":
        s = new(); e = new(); outs = []
        for br in node[1]:
            a, b = _compile(br, states); outs.append(a); states[b][2] = e
        states[s][2] = outs
        states[s][0] = "split"
        return s, e
    if k == "q":
        op, inner = node[1], node[2]
        a, b = _compile(inner, states)
        s = new("split"); e = new()
        if op == "*":
            states[s][2] = [a, e]; states[b][2] = s
        elif op == "+":
            states[s][2] = [a]; states[b][0] = "split"; states[b][2] = [a, e]
        else:
            states[s][2] = [a, e]; states[b][2] = e
        return s, e


def match(pattern, text):
    states = []
    start, accept = _compile(_parse(pattern), states)

    def closure(ss):
        stack = list(ss); seen = set(ss)
        while stack:
            s = stack.pop(); kind, _, out, _ = states[s]
            if kind == "chr":
                continue
            nxt = out if isinstance(out, list) else ([out] if out is not None else [])
            for n in nxt:
                if n not in seen:
                    seen.add(n); stack.append(n)
        return seen

    cur = closure({start})
    for ch in text:
        nxt = {states[s][2] for s in cur if states[s][0] == "chr" and states[s][1](ch)}
        cur = closure(nxt)
        if not cur:
            return False
    return accept in cur
