Write a Python module that defines `run(src: str)` and an exception class `LispError(Exception)`: an interpreter for a tiny Lisp. Do not use `eval`, `exec` or any parsing library.

Syntax:
- Tokens: `(`, `)`, `'` (quote shorthand), integers (optional leading `-`, e.g. `-12`), the literals `#t` and `#f`, and symbols (any other run of non-space, non-paren characters). `;` starts a comment that runs to the end of the line.
- `src` may contain several top-level expressions; `run` evaluates them in order in one global environment and returns the value of the LAST one (`None` if there are none).

Values: Python `int`, `True`/`False`, Python lists for Lisp lists (the empty list is `[]`), procedures (any Python object), and symbols returned by quote as Python `str`.

Special forms:
- `(quote x)` and `'x`: x unevaluated (a list becomes a Python list, a symbol a `str`, numbers/booleans as themselves).
- `(if c a b)`: only `#f` (False) is false; everything else, including `0` and `()`, is true. `b` is required.
- `(define name expr)` and `(define (name params...) body...)`: binds in the CURRENT environment; returns `None`.
- `(set! name expr)`: rebinds the NEAREST existing binding; `LispError` if unbound. Returns `None`.
- `(lambda (params...) body...)`: closure with lexical scope; body is one or more expressions, the last is the value.
- `(let ((n1 e1) (n2 e2) ...) body...)`: all `e` are evaluated in the OUTER environment (not sequentially).
- `(begin e...)`: evaluates in order, returns the last.
- `(and e...)` / `(or e...)`: short-circuit, return the deciding VALUE (`(and)` is `#t`, `(or)` is `#f`, `(or #f 3)` is `3`).
- `(cond (test body...)... (else body...))`: first true test wins; returns `None` if none match and there is no else.

Built-ins: `+ - *` (variadic; `(- x)` negates; `(+)` is 0, `(*)` is 1), `quotient` (2 args, truncates toward zero like C: `(quotient -7 2)` is `-3`), `remainder` (sign follows the dividend: `(remainder -7 2)` is `-1`), `= < > <= >=` (2 or more args, chained), `not`, `cons` (second argument must be a list; `(cons 1 '(2))` is `[1, 2]`), `car`, `cdr`, `list`, `null?`, `length`, `apply` (`(apply f '(1 2))`).

Errors - raise `LispError` (never a raw Python exception) for: unbound symbol, calling a non-procedure, wrong number of arguments to a lambda or built-in with fixed arity, `quotient`/`remainder` by zero, `car`/`cdr` of an empty list or non-list, unbalanced parentheses or an unexpected `)`, and malformed special forms (e.g. `(if 1 2)`).

Proper tail calls are REQUIRED: a procedure that calls itself (or another procedure) in tail position - including through `if`, `cond`, `begin`, `let`, `and`, `or` - must run in constant Python stack, so a loop of 1,000,000 iterations works without raising the recursion limit. Non-tail recursion only needs to handle depths of about 500.

Return the complete module in one ```python code block.
