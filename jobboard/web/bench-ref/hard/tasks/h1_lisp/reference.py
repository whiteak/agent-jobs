class LispError(Exception):
    pass


class Sym(str):
    pass


def tokenize(src):
    out = []
    for line in src.split("\n"):
        line = line.split(";", 1)[0]
        out += line.replace("(", " ( ").replace(")", " ) ").replace("'", " ' ").split()
    return out


def atom(t):
    if t == "#t":
        return True
    if t == "#f":
        return False
    try:
        return int(t)
    except ValueError:
        return Sym(t)


def parse_all(src):
    toks = tokenize(src)
    pos = 0

    def read():
        nonlocal pos
        if pos >= len(toks):
            raise LispError("unexpected EOF")
        t = toks[pos]; pos += 1
        if t == "(":
            lst = []
            while True:
                if pos >= len(toks):
                    raise LispError("unbalanced")
                if toks[pos] == ")":
                    pos += 1
                    return lst
                lst.append(read())
        if t == ")":
            raise LispError("unexpected )")
        if t == "'":
            return [Sym("quote"), read()]
        return atom(t)

    exprs = []
    while pos < len(toks):
        exprs.append(read())
    return exprs


class Env(dict):
    def __init__(self, params=(), args=(), outer=None):
        super().__init__(zip(params, args))
        self.outer = outer

    def find(self, k):
        e = self
        while e is not None:
            if k in e:
                return e
            e = e.outer
        raise LispError(f"unbound {k}")


class Proc:
    def __init__(self, params, body, env):
        self.params, self.body, self.env = params, body, env


def strip(x):
    if isinstance(x, list):
        return [strip(y) for y in x]
    if isinstance(x, Sym):
        return str(x)
    return x


def arity(n, f):
    def g(*a):
        if len(a) != n:
            raise LispError("arity")
        return f(*a)
    return g


def chain(op):
    def g(*a):
        if len(a) < 2:
            raise LispError("arity")
        return all(op(a[i], a[i + 1]) for i in range(len(a) - 1))
    return g


def q(a, b):
    if b == 0:
        raise LispError("div0")
    r = abs(a) // abs(b)
    return r if (a >= 0) == (b >= 0) else -r


def rem(a, b):
    if b == 0:
        raise LispError("div0")
    return a - b * q(a, b)


def car(x):
    if not isinstance(x, list) or not x:
        raise LispError("car")
    return x[0]


def cdr(x):
    if not isinstance(x, list) or not x:
        raise LispError("cdr")
    return x[1:]


def cons(a, b):
    if not isinstance(b, list):
        raise LispError("cons")
    return [a] + b


def minus(*a):
    if not a:
        raise LispError("arity")
    return -a[0] if len(a) == 1 else a[0] - sum(a[1:])


def mul(*a):
    r = 1
    for x in a:
        r *= x
    return r


def global_env():
    e = Env()
    e.update({"+": lambda *a: sum(a), "-": minus, "*": mul, "quotient": arity(2, q), "remainder": arity(2, rem),
              "=": chain(lambda a, b: a == b), "<": chain(lambda a, b: a < b), ">": chain(lambda a, b: a > b),
              "<=": chain(lambda a, b: a <= b), ">=": chain(lambda a, b: a >= b),
              "not": arity(1, lambda x: x is False), "cons": arity(2, cons), "car": arity(1, car),
              "cdr": arity(1, cdr), "list": lambda *a: list(a), "null?": arity(1, lambda x: x == []),
              "length": arity(1, lambda x: len(x)), "apply": None})
    e["apply"] = arity(2, lambda f, args: apply_proc(f, list(args)))
    return e


def apply_proc(f, args):
    if isinstance(f, Proc):
        if len(args) != len(f.params):
            raise LispError("arity")
        return evaluate_body(f.body, Env(f.params, args, f.env))
    if callable(f):
        try:
            return f(*args)
        except LispError:
            raise
        except Exception as ex:
            raise LispError(str(ex))
    raise LispError("not a procedure")


def evaluate_body(body, env):
    for x in body[:-1]:
        ev(x, env)
    return ev(body[-1], env)


def ev(x, env):
    while True:
        if isinstance(x, Sym):
            return env.find(x)[x]
        if not isinstance(x, list):
            return x
        if not x:
            raise LispError("empty combination")
        op = x[0]
        if op == "quote":
            if len(x) != 2:
                raise LispError("quote")
            return strip(x[1])
        if op == "if":
            if len(x) != 4:
                raise LispError("if")
            x = x[2] if ev(x[1], env) is not False else x[3]
            continue
        if op == "define":
            if len(x) < 3:
                raise LispError("define")
            if isinstance(x[1], list):
                if not x[1]:
                    raise LispError("define")
                env[x[1][0]] = Proc(x[1][1:], x[2:], env)
            else:
                if len(x) != 3:
                    raise LispError("define")
                env[x[1]] = ev(x[2], env)
            return None
        if op == "set!":
            if len(x) != 3:
                raise LispError("set!")
            e = env.find(x[1])
            e[x[1]] = ev(x[2], env)
            return None
        if op == "lambda":
            if len(x) < 3 or not isinstance(x[1], list):
                raise LispError("lambda")
            return Proc(x[1], x[2:], env)
        if op == "let":
            if len(x) < 3 or not isinstance(x[1], list):
                raise LispError("let")
            names = [b[0] for b in x[1]]
            vals = [ev(b[1], env) for b in x[1]]
            env = Env(names, vals, env)
            for y in x[2:-1]:
                ev(y, env)
            x = x[-1]
            continue
        if op == "begin":
            if len(x) == 1:
                return None
            for y in x[1:-1]:
                ev(y, env)
            x = x[-1]
            continue
        if op == "and":
            if len(x) == 1:
                return True
            for y in x[1:-1]:
                if ev(y, env) is False:
                    return False
            x = x[-1]
            continue
        if op == "or":
            if len(x) == 1:
                return False
            done = False
            for y in x[1:-1]:
                v = ev(y, env)
                if v is not False:
                    return v
            x = x[-1]
            continue
        if op == "cond":
            nxt = None
            for clause in x[1:]:
                if not isinstance(clause, list) or not clause:
                    raise LispError("cond")
                if clause[0] == "else" or ev(clause[0], env) is not False:
                    nxt = clause
                    break
            if nxt is None:
                return None
            if len(nxt) == 1:
                return True
            for y in nxt[1:-1]:
                ev(y, env)
            x = nxt[-1]
            continue
        f = ev(op, env)
        args = [ev(a, env) for a in x[1:]]
        if isinstance(f, Proc):
            if len(args) != len(f.params):
                raise LispError("arity")
            env = Env(f.params, args, f.env)
            for y in f.body[:-1]:
                ev(y, env)
            x = f.body[-1]
            continue
        return apply_proc(f, args)


def run(src):
    env = global_env()
    r = None
    for e in parse_all(src):
        r = ev(e, env)
    return r
