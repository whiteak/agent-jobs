Write a Python module that defines `evaluate(expr: str) -> float`, an arithmetic expression evaluator. Do not use `eval`, `exec`, `ast` or any parsing library.

Rules:
- Numbers are non-negative decimal literals such as `3`, `0.5`, `12.75`, `.5` is NOT valid.
- Binary operators: `+`, `-`, `*`, `/`, and `^` (power).
- Precedence from lowest to highest: `+ -`, then `* /`, then unary minus, then `^`. So `-2^2` is `-4` and `2^-1` is `0.5` (unary minus is allowed as the right operand of `^`).
- `+ - * /` are left-associative; `^` is right-associative (`2^3^2` is `512`).
- Unary minus may repeat (`--3` is `3`). There is no unary plus.
- Parentheses group. Spaces and tabs may appear anywhere between tokens.
- Division by zero raises `ZeroDivisionError`.
- Any malformed input (empty string, unknown character, missing operand, unbalanced parentheses, number like `1.2.3` or `.5` or `5.`) raises `ValueError`.
- Always return a `float`.

Return the complete module in one ```python code block.
