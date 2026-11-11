Write a Python module that defines `apply_patch(original: str, diff: str) -> str` and an exception class `PatchError(Exception)`. It applies a single-file unified diff to the text `original` and returns the new text. Do not shell out or use any diff/patch library.

Diff format:
- Optional header lines starting with `--- ` and `+++ ` (ignore them), and any other lines before the first hunk (ignore them too).
- Each hunk starts with `@@ -A,B +C,D @@` (anything after the second `@@` is ignored). `,B` / `,D` may be omitted, meaning 1. `B` (`D`) is the number of old (new) lines in the hunk.
- Hunk body lines start with `' '` (context, in both old and new), `'-'` (old only) or `'+'` (new only); the rest of the line is the content. A body line `\ No newline at end of file` means the PREVIOUS body line has no trailing newline (in the old text if that line was `' '`/`'-'`, the new text if `'+'`; for a `' '` line it applies to both). All other body lines represent a line ending in `\n`.
- If the number of `' '`+`'-'` lines is not B, or `' '`+`'+'` lines is not D, raise `PatchError`. An empty diff line inside a hunk (a bare newline, as some editors produce) counts as a context line with empty content.

Applying:
- Lines of `original` include their `\n`; the last line may lack one. A hunk's old lines must match the original EXACTLY, including whether the line ends in `\n`.
- The hunk's expected position is line A (1-based), i.e. index A-1; when B is 0 (pure insertion) the new lines go AFTER line A, i.e. at index A (A may be 0 = at the very start).
- If the old lines do not match at the expected position, search for the nearest position where they match: try offsets -1, +1, -2, +2, ... (smaller distance wins; at equal distance the EARLIER position, i.e. the negative offset, wins). Positions are only allowed if they do not overlap or precede the region used by the previous hunk (hunks apply in order, positions refer to the ORIGINAL text, and offsets found for one hunk do NOT shift the expected position of later hunks).
- If no position matches, raise `PatchError`. No fuzz (every context line must match).
- A pure-insertion hunk with no old lines is placed at its expected position (raise `PatchError` if that is past the end of the text or before the previous hunk's region).

Return the complete module in one ```python code block.
