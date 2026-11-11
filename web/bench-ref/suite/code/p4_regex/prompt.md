Write a Python module that defines `match(pattern: str, text: str) -> bool`, a small regular-expression matcher. Do not use the `re` module or any regex library.

The match must cover the WHOLE text. Pattern syntax:
- A literal character matches itself.
- `.` matches any single character.
- `[...]` is a character class: a set of characters and ranges like `a-z`; `[^...]` negates it. Inside a class, `]` as the first character (after an optional `^`) is literal, `-` as the first or last character is literal, and `\` escapes the next character.
- `\` outside a class escapes the next character so it is literal (`\.` matches a dot, `\\` matches a backslash).
- `*`, `+`, `?` apply to the preceding atom (a literal, `.`, a class, or a group): zero or more, one or more, zero or one.
- `(...)` groups; `|` is alternation with the lowest precedence, inside a group or at the top level. An empty alternative matches the empty string (`a(|b)c` matches `ac` and `abc`).
- A pattern that is malformed (unbalanced parentheses, a quantifier with nothing before it, `**`, an unterminated class, a trailing lone `\`) raises `ValueError`.

It must not take exponential time: `match("(a*)*b", "a" * 30)` and `match("(a|aa)*c", "a" * 40)` must return False in under one second.

Return the complete module in one ```python code block.
