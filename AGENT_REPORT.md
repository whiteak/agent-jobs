# Agent Jobs — work log

## Job 02 — Upgrade 7b: live dashboard, part 2 (diff viewer + check badges)

Branch: `agent/jobboard-next` (local only — **not pushed**; ready for Jason to review, push
and merge). PLAN.md has the findings and the step list, all ticked.

Commits in this job (on top of `55a0325`, the 7a merge):

- `b433423` fix 7a leftovers so the suite is green (pages serve on their own port, `EventLog`
  is callable, sample-log tool order)
- `4b451aa` U07b: diff collection (sandboxed safe git) and per-check badges
- `076b6e1` U07b: the Changes tab on the job page, check badges in the list and banner,
  `/api/changes` + `/api/check`, 35 tests
- last commit: honest counts for uncountable untracked files + PLAN.md ticks + this report

### Files changed

| File | What it is |
| --- | --- |
| `web/diffview.py` | **new** (505 lines) — the read side: which views exist (`all`, `c:<sha>`, `k:<n>`), the file list with `+/-` counts, one file's diff as rows. All git runs in the job's jail; results cached briefly. |
| `web/diffcard.py` | **new** (289 lines) — the Changes card: the HTML shell, its CSS, and the page script (file list, unified/side-by-side, collapsed unchanged runs, "Next change", lazy chunked rendering, 10 s + event refresh). |
| `web/badges.py` | **new** (122 lines) — one badge per check (`build ✓`, `tests 41/42 ✗`) with the link to its output, plus the output reader. |
| `web/jobboard.py` | routes `/api/changes/<jid>`, `/api/changes/<jid>/file`, `/api/check/<jid>`; the card replaces the old git-status card (that feature is kept inside it, in "Git status of the folder"); badges in the jobs list row and the job banner; `jobactivity`/`jobbadges` events from the 7a stream. |
| `web/upgrades.py` | `EventLog.__call__` (7a's tests and gate callbacks call `ev("checks", …)`; the class only had `.emit`). |
| `web/tests/test_u07b_changes.py` | **new**, 36 tests. |
| `web/tests/test_u07_dashboard.py` | 7a leftovers fixed, not weakened: `Pages` sets/restores `jobboard.PORT` (that is why `guard()` answered 403 "Bad host"), and one expected tool order was corrected to the order the `sample-jobs/20261107-180819-71a7/events.jsonl` fixture really has. |
| `PLAN.md` | findings + steps. |

### How each requirement is met

1. **Changes per job** — `diffview.summary()` runs one shell script in the jail that does
   `git diff --name-status -M <base>` + `git diff --numstat -M <base>` + `git ls-files --others`
   in a single call, where `<base>` is the job's recorded `git_start` (falls back to the parent
   of the oldest commit in the job window, then to the empty tree). Uncommitted and untracked
   files are included. Clicking a row fetches `/api/changes/<jid>/file?path=…`; the page draws
   unified by default, "Side by side" toggles to `diffparse.side_by_side()` pairs, added/removed
   rows are coloured, runs of more than 6 unchanged lines collapse into a clickable
   "Show N more unchanged lines", and **Next change** jumps to the next changed row (rendering
   ahead of itself if needed).
2. **Binary and generated files listed, not shown** — the same rules as the review bundle:
   `reviewbundle._skip()` (locks, reports, skip dirs) and `diffparse.is_generated()`, plus
   git's own `-` counts and a NUL-byte probe for untracked files. Such rows are greyed, say why
   (`binary`, `generated`, `not reviewed`, `too big to count`) and cannot be opened.
3. **Pick a commit or checkpoint** — the picker offers "Everything the job changed", one entry
   per commit made inside the job window, and one per checkpoint (`m["checkpoints"]`, skipped
   when its commit is gone). A ref is accepted only if it is one of those; the range is then
   built inside the shell script from validated shas only.
4. **One badge per check** — `badges.badges()` reads the job record (a later `recheck` wins,
   else the last verify round): text `build` / `tests 41/42` / `… timed out`, ✓/✗ colour, and
   `href=/api/check/<jid>?src=round0&n=1`. The same badges are rendered server-side in the jobs
   list ("Checks" column) and in the job banner, and re-sent live through the 7a status stream.
   `/api/check/...` returns the tail (200 KB max) of that check's `output_file`, and only when
   the resolved path stays inside `JOBS/<id>/` — a tampered record cannot make the board read
   another file.
5. **Large diffs stay responsive** — the server caps a file at 400 KB of diff text and
   12 000 hunk lines (one gigantic hunk is cut inside itself so the two views stay aligned);
   the page inserts only 300 rows at a time, collapses unchanged runs, and offers
   "Show N more lines". `test_huge_diff_is_cut_but_still_usable` checks a 15 000-line diff
   stays bounded and usable.

The diff parser is `agentjobs_upgrades/diffparse.py` (used, not rewritten); the page script has
no CDN and no external JS.

Safety notes: every git call goes through the job's jail wrap (`upgrades.make_wrap` →
`jail/run-local.sh sandbox-run`) with `agentjobs_upgrades.safegit.SAFE_FLAGS`
(`-c core.hooksPath=/dev/null`, `-c core.fsmonitor=false`, …) plus `GIT_CONFIG_NOSYSTEM=1`,
`GIT_TERMINAL_PROMPT=0`, `GIT_PAGER=cat`; a job repo's own config is never trusted. File paths
only reach the script base64'd, and are re-checked in the shell against `..`, absolute paths and
`.git`. Probe results are cached 30 s and file lists 8 s so a running job's 10 s refresh does
not start a container on every poll; failures are never cached, and the cache key includes the
project folder, so one record can never show another job's files.

### Tests — exact commands and results

Before my changes the suite was red (7a leftovers, as recorded in `HANDOFF-7a.md`):

```
cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore -m unittest discover -s tests
→ Ran 346 tests … FAILED (failures=7, errors=1)
```

After fixing those leftovers (same command): `Ran 346 tests … OK`.

After 7b (`web/tests/test_u07b_changes.py`, 36 tests added):

```
cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore -m unittest discover -s tests
→ Ran 382 tests in 113.515s
→ OK
```

That is also the command in `AGENT_CHECKS.json`, so the verify gate runs it. New coverage:
the parser (added, deleted, renamed-with-changes, `Binary files … differ`,
`\ No newline at end of file`, generated paths, side-by-side pairing), collection against a
real temp git repo through `SafeGit` (modified/added/deleted/renamed/untracked/binary/
generated, counts, view picker, one-commit view, checkpoint view made the way
`CheckpointManager` makes one, 15 000-line diff, not-a-repo, cache/no-cache-on-failure),
badges (texts, recheck wins, escaping, output confined to `JOBS/<id>`), and the endpoints
served in-process on their own port (list, one file, bad ref/path → 404, cookie guard,
`/api/rows` and `/api/statuses` badges, job page markers, and `node --check` on every page
`<script>` both separately and joined).

I also smoke-served the real server (never port 8090):

```
HOME=$T JOBBOARD_JOBS=$T/jobs JOBBOARD_WORK=$T/work JOBBOARD_READONLY=1 JOBBOARD_JAIL=host \
  JOBBOARD_PORT=8123 python3 jobboard.py
```

against a fixture repo, then with curl: `/` showed `build ✓` and `tests 41/42 ✗` in the row;
`/job/<jid>` showed the card and the banner badges; `/api/changes/<jid>` returned
`7 files, +7/-2` with `logo.png → binary`, `build/gen.txt → generated`, the rename and the
untracked file; `/api/changes/<jid>/file?path=src/a.py` returned the rows and the side-by-side
pairs; a commit view returned only that commit's changes; `ref=c:deadbeef` → 404,
`path=../etc/passwd` → 404, no cookie → 403; `/api/check/<jid>?src=round0&n=1` returned the
pytest output and `n=9` → 404. The server was stopped in the same command; nothing is left
running and port 8123 is free.

### Try it by hand

1. `cd web && HOME=$HOME JOBBOARD_PORT=8123 python3 jobboard.py`, open the printed
   `http://127.0.0.1:8123/?t=…` link.
2. Start a job (fake mode is fine). While it runs, the row's "Checks" column shows one badge
   per check as soon as a check run lands.
3. Open the job. In the banner under the check line you get the same badges; click
   `tests 41/42 ✗` → its output opens in a new tab.
4. Scroll to the **Changes** card. The picker says what is shown ("everything since
   <base> (where the job started), uncommitted included"); the list shows every changed file
   with `+/-` and a state mark (`+` new, `-` deleted, `↴` renamed, `?` untracked); binary and
   generated files are greyed with the reason instead of counts.
5. Click a file: unified diff, added/removed coloured, long unchanged runs collapsed to
   "Show N more unchanged lines" (click to open). Press **Side by side** for the two-column
   view, **Next change** to step through the changed lines, **Refresh** to re-read.
6. Pick a commit or a checkpoint in the picker: only that range is listed.
7. For a running job the card re-reads itself every 10 s and right after a live-stream event.

### Unfinished / assumed

- The spec's "Changes tab" is delivered as a **card on the job page** — this board has no tab
   strip; 7a's timeline/log are cards too, so the card is the house style. Everything the
   requirement asks for is inside it.
- A file diff is capped at 400 KB of text / 12 000 rows; the page then says the review bundle
   has the rest. Untracked files over 2 MB are listed without a line count ("too big to count")
   instead of guessing one.
- No syntax highlighting (only added/removed/unchanged colouring) — stdlib/plain JS only.
- Tests use `JOBBOARD_JAIL=host` for the git calls (same as the U01 and 7a tests): they check
   the wrap is used, not that podman runs. The podman path itself is untouched; a real board
   sends these same commands through `jail/run-local.sh sandbox-run`.
- Nothing installed, no network used, stdlib only, no CDN. No test deleted, skipped or weakened.
