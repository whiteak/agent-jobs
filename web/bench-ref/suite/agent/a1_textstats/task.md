Work in this repository (the `textstats` package, run as `python -m textstats`). Implement all of the following, editing the existing files:

1. Fix `core.words`: a word is a maximal run of letters, digits, apostrophes and hyphens (`re` pattern `[A-Za-z0-9'-]+`), with leading and trailing apostrophes and hyphens stripped; pieces that become empty are dropped. So `"Hello, world -- it's 'quoted' re-use!"` gives `["Hello", "world", "it's", "quoted", "re-use"]`.
2. Add `core.bigrams(text, ignore_case=False)` returning a `Counter` of adjacent word pairs as tuples, using `core.words`. With `ignore_case=True` words are lowercased first.
3. Give `word_counts` and `top_words` an `ignore_case=False` parameter that lowercases words first. `top_words` must break count ties alphabetically (ascending) so output is deterministic.
4. CLI: `count` and a new `bigrams` subcommand both take `FILE` (or `-` for standard input) and the options `--top N` (default 10, must be >= 1, otherwise exit with status 2 and an error on stderr), `--ignore-case`, and `--json`.
   - Plain output: one line per entry, `word<TAB>count` for count and `first second<TAB>count` for bigrams, most common first, ties broken alphabetically.
   - `--json` output: a JSON array of objects, `{"word": w, "count": n}` for count and `{"bigram": "first second", "count": n}` for bigrams, same order, printed on one line.
   - A missing file prints `error: cannot read FILE` to stderr and exits with status 1.
5. Add a `stats` subcommand taking `FILE` or `-` that prints exactly three lines: `words: N`, `unique: N` (case-sensitive), `lines: N` (number of lines in the input; a final line without a newline still counts; empty input has 0 lines).
6. Add unit tests for the new behaviour in `tests/` (standard library `unittest`), and document every command and option in `README.md`.

Do not add third-party dependencies.
