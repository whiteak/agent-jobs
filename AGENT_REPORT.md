# Agent Jobs — work log

## Job 01 — Upgrade 7a: live dashboard, part 1 (timeline + live log)

Branch: `agent/jobboard-next` (local only — **not pushed**; ready for Jason to review, push and
merge). PLAN.md has the findings and the step list for this job, all ticked.

This job ran twice. The first pass found that 7a was **already merged but unproven** (WIP
`9d3683f` from `agent/jobboard-u07`, its own breakage fixed later in `656742c` / `b433423`) and
wrote the missing tests plus the icon polish, then stopped before the smoke run and the report.
The re-run (12 Nov) finished the last three steps and audited what was left.

Commits in this job:

- `2c8bb3e` PLAN.md — findings (what 7a already gave, how the timeline gets filled, the gaps I
  actually found) and the step list
- `f66d147` 11 more tests (every timeline kind with icon/time/colour, the whole `EventLog` →
  timeline mapping, escaped event text, the board's own gate lines as log rows, the polling
  fallback, the log controls, and one real fake-agent job whose `timeline.jsonl` the real
  `/api/jobs/<id>/events` route replays); icons for the `attempt`/`plan`/`queue` rows the other
  upgrades add; dead `.hit` CSS rule dropped
- `28e8795` the one real bug the smoke run caught: the timeline's `started` row was built by
  cutting the mode label at its colon, so it wrote `Started (Local` for a real agent and
  `Started (Fake agent (tests only` for the fake one. Now `AGENT_NAMES` + `started_text()` put a
  short agent name in the row: `Started (Qwen Code)`, `Started (fake agent)`. 1 new test plus 1
  end-to-end assertion (the commit message says "2 more tests"; the suite moved 731 → 732).
- last commit: PLAN.md steps ticked, `HANDOFF-7a.md` marked resolved, this section.

### Files changed

| File | What it is |
| --- | --- |
| `web/dashboard.py` | the 7a feature (192 lines, already merged): `timeline_for`/`tl_add` over `JOBS/<id>/timeline.jsonl`, `job_stream()` (SSE), `tl_row`/`timeline_card`/`log_card`, `badge`/`checks_cell`, `TL_KIND`. **Unchanged by this job** apart from dropping the dead `.hit` rule (`f66d147`). |
| `web/jobboard.py` | this job: `AGENT_NAMES` + `started_text()` and the `started` row (`28e8795`). The routes (`/api/jobs/<id>/events` → `H.sse_job`, `/api/raw/<id>`, `/api/events`, `/api/statuses`) and the job page's live script were already there. |
| `web/agentjobs_upgrades/events.py` | `ICONS` gained `attempt`/`plan`/`queue` (`f66d147`), so no timeline row falls back to `•`. |
| `web/tests/test_u07_dashboard.py` | **now 33 tests** (11 added by `f66d147`, 1 more here). |
| `HANDOFF-7a.md` | every open item marked resolved, with the commit that resolved it; the original note kept below unchanged. |
| `PLAN.md` | findings + steps for this job, all ticked. |

Nothing in `agentjobs_upgrades/` was rewritten; the timeline is fed through the engines that
already exist (`upgrades.EventLog.emit` → `dashboard.TL_KIND` → `tl_add`, and `tl_add` calls for
`queued`/`started`/`step`/`finished`, `plan`, `queue`, `attempt`). The `# JAIL:`, `# U01`, `# U05`,
`# U06`, `# U07`, `# U08` and `# VOICE` blocks all still work — the full suite runs them.

### How each requirement is met

1. **SSE instead of polling, with a fallback.** One `EventSource` per job page on
   `/api/jobs/<id>/events?log=<offset>&last=<timeline id>`; the server (`H.sse_job` →
   `dashboard.job_stream`) sends `retry: 3000`, timeline rows with `id:` lines, `log` batches with
   byte offsets, a `status` row only when it changed, `: keepalive` every 15 s and `end` after the
   job settles. `onerror` reconnects with the *current* offsets; after 3 failures the page falls
   back to the old `/api/events/` + `/api/status/` polling (`startPolling`), so a dropped stream
   never leaves a dead page.
2. **Timeline per job.** `JOBS/<id>/timeline.jsonl` through `events.JobEvents`:
   `{id, time, type, text, data}`, ids 1..n, so `Last-Event-ID` resumes exactly where the browser
   stopped. Rows are a vertical list with icon + time + text; `events.ICONS` covers every kind the
   spec names (queued 🕒, started ▶, step ☑, checks 🧪, fix round 🔧, checkpoint 📌, rollback ↩,
   watchdog 🐕, safety stop ⛔, needs review ⚠, restart 🔄, finished 🏁, plus attempt/plan/queue),
   and `tl_row` colours the row from `data.colour` (checks) or the kind (stop/rollback/warn).
   The card shows the newest 300 rows and says where the stream resumes (`data-last`).
3. **Readable live log.** `jobboard.events()` classifies each line of `events.jsonl`
   (`k` = text/think/tool/result/gate/final/log, `cat` = cmd/edit/read/other through `tool_cat`),
   so commands, file edits, reads and model messages are separately styled rows; results over
   10 lines are sent whole and collapsed on the page with **Show more (N more lines)** / Show less;
   auto-scroll follows while you are at the bottom and says **Paused - scroll to the bottom to
   follow** when you scroll up; the search box filters the rows already on the page;
   **Show raw log** (`/api/raw/<id>`, last 8 MB) is kept.
4. **Jobs list.** `dashboard.badge` gives Working / Queued / Done ✓ / Failed ✗ / Not verified /
   Needs review / Safety stop (plus Stuck, Interrupted, Done for pre-gate jobs), the row keeps its
   `Time` duration and gains a **Checks** column (`checks_cell`: `42 passed`, `1 failed`,
   `needs review`, `not checked`, `none found`), and the same badge text drives the status pill.
   U07b's per-check badges render inside that cell.
5. **Banner, checklist and notifications kept.** The status banner, step checklist, tab title,
   stuck line, beep and the "Enable alerts" button are untouched and asserted in
   `Pages.test_job_page_has_timeline_log_and_keeps_banner`; the `status` rows on the stream feed
   them exactly as the old 3 s poller did.

### Tests — exact commands and results

First pass, before any of this job's changes (`cb1346a`):
`Ran 635 tests in 270.529s` / `OK`.

Re-run, before my change (the shared copy also holds job 03's **uncommitted** U08 tests, 96 of
them, so the tree is 731, not 635):

```
cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore -m unittest discover -s tests
→ Ran 731 tests in 284.000s
→ OK
```

After `28e8795` (same, the command in `AGENT_CHECKS.json`, so the verify gate runs it):

```
cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore -m unittest discover -s tests
→ Ran 732 tests in 283.280s
→ OK
```

The 7a module alone: `… -m unittest discover -s tests -p "test_u07_dashboard.py"` →
`Ran 33 tests in 19.063s / OK`. No test was deleted, skipped or weakened — the two
`test_u07_dashboard` expectations that were wrong were corrected to what the code and the
`sample-jobs/20261107-180819-71a7` fixture really do (job 02's report records that).

What the 33 tests cover: SSE (`id`/`event`/one `data:` line, `retry`, `log` batch offsets, `status`
sent once, keepalive, `end` for a gone job and after a finished one); `Last-Event-ID` resume sends
only newer rows; `tl_add` never raises; the full `EventLog` kind → timeline mapping
(`checks_start` deliberately excluded, `guard` → needs review, the three checkpoint variants →
one kind); a row for every kind with icon, `HH:MM:SS` and colour; event text cannot inject HTML;
the card keeps the newest 300 rows and the `data-last` resume id; `finished_text`; the log parser
on the three real logs in `web/sample-jobs` (tool categories and order, resume from a byte offset,
a half-written last line not consumed, a 30 000-char output cut with a pointer to the raw log,
the board's own `agentjobs` gate lines as rows, unparsable lines still shown, a job with no log);
all seven badges plus `checks_cell`; the routes in-process on their own port (stream 200/404/403,
resume with `?log=` + `Last-Event-ID`, raw log, the list's Checks column, `/api/statuses`
badge+checks, the polling fallback and every live-log control on the page); and one real
fake-agent job through `process_job` — its `timeline.jsonl` (queued → started → step → checkpoint
→ checks → rollback → fix_round → checks → finished, ids 1..n, colour on the checks row) replayed
by the real route, with a resume that asks for nothing it already has.

### Smoke run on 127.0.0.1:8091 (never 8090)

One throwaway driver (`/tmp/u07a_smoke.py`, not part of the repo: the routes are already covered
in-process) that starts the **real** `jobboard.py` as a child with its own state folders —
temp `HOME`, `JOBBOARD_JOBS/WORK/PLANS/QUEUES/SETTINGS/NOTIFY` all under it, `JOBBOARD_FAKE=1`,
`JOBBOARD_JAIL=host`, `JOBBOARD_PORT=8091` — in a temp project folder (git repo, `PLAN.md`,
`AGENT_CHECKS.json` with a unittest check), POSTs a fake-agent scenario through the real form,
reads the stream, then stops the server and verifies the port is closed. Key output:

```
[stream] Content-Type text/event-stream; charset=utf-8; 9 timeline rows, 4 log batches
         (log offset 3301), 10 status rows, retry line 'retry: 3000'
[stream] timeline ids as they arrived: [(1, 0.0), (2, 0.0), (3, 2.0), (4, 2.0), (5, 5.0),
         (6, 5.0), (7, 5.0), (8, 6.0), (9, 6.0)]
[stream] closed the connection on purpose at t=9.0s (last id 9, log offset 3301)
[job] ended: state=FINISHED badge='Done ✓' checks='1 passed' elapsed=5s
[resume] Last-Event-ID 9 + ?log=3301: timeline=[] log_batches=0 end=('end', {'status': 'done'})
[stream] full replay ids=[1, 2, 3, 4, 5, 6, 7, 8, 9], log next=[3301], ends ('end', {'status': 'done'})
[timeline.jsonl] 1:queued, 2:started, 3:step, 4:checkpoint, 5:checks, 6:rollback, 7:fix_round,
                 8:checks, 9:finished
   19:15:00 queued    Queued
   19:15:00 started   Started (fake agent)
   19:15:01 step      Step ticked: make the calculator better
   19:15:01 checkpoint Checkpoint after step 1: 1 tests passing
   19:15:04 checks    Checks failed: tests (1 failed)
   19:15:04 rollback  Your last changes made things worse (passing tests dropped from 1 to 0)…
   19:15:04 fix_round Fix round 1 started
   19:15:05 checks    Checks passed (1 tests)
   19:15:05 finished  Finished - Checks passed (1 tests)
[list] header has <th>Checks</th>: True; <th>Time</th>: True
[list] row: … | ✓ Done ✓ 1/2 | fake | smoke-proj | … | 0m 05s | 1 passed tests 1/1 ✓
[api/statuses] badge='Done ✓' checks='1 passed'
[page] Timeline/Live log/search/Show raw log/banner/EventSource/fails>=3 polling/Show more/
       Paused…/tl data-last/bsteps/Enable alerts: all yes; timeline rows: 9;
       icons: ['↩','▶','☑','🏁','📌','🔧','🕒','🧪']
[raw] text/plain; charset=utf-8, 3301 bytes | without the cookie: 403 as expected
[log rows] next offset 3301; kinds {final: 2, gate: 7, result: 2, text: 1, tool/edit: 2}
[board] stopped, port 8091 is closed again
```

The first smoke run (before `28e8795`) is what showed `started  Started (Fake agent (tests only)`
and sent this job's only code fix. Nothing is left running; ports 8090 and 8080 were never
touched by the smoke copy (8080 was only checked for being free/used, never queried).

### Try it by hand

1. `cd web && JOBBOARD_PORT=8091 python3 jobboard.py` (your own state folders if you want them
   separate), open the printed `http://127.0.0.1:8091/?t=…` link.
2. Start a job. On the job page the **Live log** fills up by itself: `$ command` rows in green,
   `✎ Edit`/`write_file` rows in amber, reads grey, model text plain, gate lines (checks, fix
   rounds, checkpoints, the watchdog) in their own tone, the final answer in a box.
3. A long output shows its first 10 lines and **Show more (N more lines)**; click it again to
   collapse. Type in **Search the log** to filter, scroll up and the corner says
   *Paused - scroll to the bottom to follow*, scroll back down and it follows again.
   **Show raw log** opens the whole file.
4. The **Timeline** card grows row by row: 🕒 Queued, ▶ Started (Qwen Code), ☑ Step ticked: …,
   📌 Checkpoint, 🧪 Checks failed/passed (coloured red/green), ↩ rollback, 🔧 fix round,
   ⛔ safety stop, 🏁 Finished - Checks passed (…).
5. Kill the board's connection (or block the stream): after three failed reconnects the page
   quietly falls back to polling, so the log and the banner keep moving.
6. Back on the list, each row shows the badge (Working / Queued / Done ✓ / Failed ✗ / Not
   verified / Needs review / Safety stop), its `Time` and the **Checks** cell (`42 passed`,
   `1 failed`, `needs review`, `not checked`) with U07b's per-check badges under it.

### Unfinished / assumed

- **Nothing of 7a is unfinished.** Every item in `HANDOFF-7a.md` is resolved, and all five
  requirements are implemented and tested (see the mapping above).
- The smoke run used the **fake agent** with `JOBBOARD_JAIL=host`, exactly like the U01 and U07
  tests: it proves the stream, the timeline, the log reader and the badges on a real board, not
  that podman runs. On the live board the same job's checks and git calls still go through
  `jail/run-local.sh`; nothing about the jail was touched here.
- Keep the smoke driver out of the repo (it is in `/tmp`); its checks are the in-process tests'
  job. Say the word if you want it committed as `web/tests/smoke_8091.py`.
- The timeline is a **second view**: `events.jsonl` keeps everything (the live log reads it),
  `timeline.jsonl` holds only the rows worth showing, each one's text cut to 1000 characters by
  `dashboard.tl_add`, and the card sends only the newest 300 rows (`data-last` tells the stream
  where to continue). The file itself has no cap - it stays small because only board milestones
  go into it. `verify["timeline"]` (an older one-line summary string in the record) is separate
  and untouched.
- The live-log search filters the rows already fetched; it does not search the raw file (the raw
  log link is there for that).
- The `started` row now uses `AGENT_NAMES`; a mode not in that dict shows its own key
  (`Started (some-new-mode)`) rather than a cut label. A new agent mode should add a name there.
- No network, no installs, stdlib only, no CDN, no new JS library. `~/agent-sandbox` was never
  touched: every test and the smoke copy use their own `HOME`/state folders under `/tmp`.

---

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

---

## Job 08 — Upgrade 4b: run the approved plan as a queue

Branch: `agent/jobboard-next` (local only — **not pushed**; ready for Jason to review, push and
merge). PLAN.md has the findings and the step list for this job, all ticked.

Commits in this job (on top of `5b3efad`, job 07's 4a work):

- `4b205d6` job 07 residue: the 4a PLAN.md left uncommitted in the shared copy (committed as-is
  so this job's diffs stay clean)
- `e092197` U04b: `web/taskqueue.py` + the `jobboard.py` hooks + the project/plan page card
- `e51099b` U04b: the 61 in-process tests; fixes they found (the queue lock deadlocked
  `action → start_next`, a job page follow-up link, the mode `<select>`)
- `c695c1a` U04b: the fake-agent queue runs (dependency order, chained commits, task checks,
  stop vs skip, best-of-N, the notes check, the cutoff, a board that died mid-task)
- `00bbc17` U04b: pressing **Resume** past the "don't start new tasks after" time clears that
  time — the card says "press Resume to carry on", so pressing it has to carry on

### Files changed

| File | What it is |
| --- | --- |
| `web/taskqueue.py` | **new** (643 lines, `# U04b`) — the glue between an approved plan and the board's worker. It does **not** reimplement the queue: the state machine is the given `agentjobs_upgrades/plan.py` (`Queue`, `load_plan`, `order`, `next_task`, `mark`, `recover`, `set_cutoff`, `progress`, `job_prompt`), already unit-tested in `tests/test_rest.py`. What is new: the queue file `QUEUES/<folder>.json`, the task prompt, `start_next` / `job_finished` / `startup` / `action`, the AGENT_NOTES check, the estimate, the card HTML. |
| `web/jobboard.py` | +59 lines: `QUEUES` (env `JOBBOARD_QUEUES`, outside anything the sandbox mounts); `check_record` refuses a task record that lies about `task_id` / `task_try` / `notes_before`; `worker()` now runs `mark_interrupted()` then `taskqueue.startup(...)`; `process_job` calls `taskqueue.job_finished(m, queue_ctx())`; the queue card on `/project/<folder>` and `/plan/<folder>`; one route `POST /queue/<folder>` for every button; `queue_ctx()` hands the board's tools to taskqueue (no circular import). |
| `web/tests/test_u04b_queue.py` | **new**, 72 tests (61 in-process + 11 that run real jobs through `process_job` in a child process with the fake agent). |
| `PLAN.md` | this job's findings and steps, all ticked. |

Nothing else was touched: `agentjobs_upgrades/*`, `planning.py`, `attempts.py`, `safegit.py`,
`upgrades.py`, `dashboard.py` and every earlier test file are unchanged, and the `# JAIL:`,
`# U01`, `# U05`, `# U07`, `# VOICE` blocks all still work (the full suite runs them).

### How each requirement is met

1. **"Approve plan" turns the tasks into queued jobs, in dependency order.** The approve button
   writes `QUEUES/<folder>.json` from the **approved record** (never from the agent-writable
   `tasks.json` in the folder), in `plan.order()` order, and it still runs nothing — 4a's
   "Plan approved. Nothing runs until you start the tasks." is unchanged; the card then offers
   **Start the tasks**. Every task job is `plan.job_prompt`: a 10-line summary of the outline,
   that task's milestone taken from the project's `ROADMAP.md` (falling back to the milestone and
   its task titles if there is no ROADMAP), the task prompt, its acceptance criteria, and "Read
   AGENT_NOTES.md first… add 2–5 lines of lessons when you finish", plus where the task sits
   ("You are task 2 of 3 in a plan Jason already approved").
2. **One job at a time, each from the previous job's final commit.** `start_next` queues exactly
   **one** job; the board's single worker is what keeps them one at a time, so a task also waits
   politely behind any other job on the board. The new job is a follow-up of the previous task's
   job (`parent`), runs in the same folder on `agent/<folder>`, and the queue records that job's
   `base` commit at the start and its `end` commit at the finish — the test asserts
   `t2.base == t1.end` and that the t2 job's own `git_start` is that same commit. Checks (from
   the task itself, `checks_source: "the approved plan"`, or the project's `AGENT_CHECKS.json`),
   fix rounds, checkpoints and the watchdog are the normal job path — nothing new there. Tasks of
   size **M**, or a task that already failed once, get best-of-N when the project ticked "Try big
   tasks…": the record simply carries `attempts`, which is how U05 is reached.
3. **A task fails: stop (default) or skip its dependents.** `plan.Queue.mark` does it; the card
   shows `failed` and `skipped` plainly, the job's timeline says "Task t2 failed, so the queue
   stopped…" or "…3 task(s) that depend on it were skipped", and **Run it again** puts one task
   back in the queue. A task you **stopped** yourself goes back as `interrupted` and the queue
   pauses instead of blaming it.
4. **The queue is on disk and survives a restart.** On board start `mark_interrupted()` marks the
   running job `interrupted` (as before) and `taskqueue.startup()` turns the task that was running
   into `interrupted`, notes the checkpoint it continues from (its last checkpoint commit is
   stored as `resume_from` and named on the timeline), and starts it again **resuming that job's
   session** — the two-boot test kills the board mid-task and shows the task running twice, the
   second run with `task_try 2` and `resume` set. A `paused`, `stopped` or `ready` queue is never
   started by a restart.
5. **The project page card.** `Tasks · <name>`: status line (`running`, `paused`, `stopped after
   a failed task`, …), `3 of 5 tasks done`, `1 failed or skipped`, `2 left - about 1h 25m`, a
   progress bar, **Now:** with a link to the running task's job (or why nothing is starting),
   milestone lines (`M1 - it runs: 1 of 2 tasks`), the task table (state, try number, its job,
   Run it again), the buttons **Start the tasks / Pause / Resume / Cancel**, and the settings form:
   which agent runs the tasks, **If a task fails** (Stop the queue / Skip the tasks that depend on
   it), **Don't start new tasks after** `06:30`, and the best-of-N tick.
6. **The AGENT_NOTES.md check.** Before a task's job starts the queue stores the hash of
   `AGENT_NOTES.md`; when the job ends and the file is unchanged, the job record gets
   `notes_note` ("AGENT_NOTES.md was not changed by this task - no lessons for the tasks after
   it.", or "No AGENT_NOTES.md in the project folder - …"), the line goes on the job's timeline
   and shows on the job page.

### Verified

- Gate command, as in `AGENT_CHECKS.json`:
  `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore -m unittest discover -s tests`
  → **`Ran 550 tests in 178.471s` / `OK`** (baseline before this job: 478 tests, OK; this job
  adds 72). No test deleted, skipped or weakened; the earlier suites (4a's 35, U05's 38,
  `test_rest.py`, the jail and dashboard ones) pass untouched.
- The new module alone: `python3 -W ignore -m unittest tests.test_u04b_queue` →
  `Ran 72 tests in 37.790s / OK`. Its `Runs` and `Restart` classes start a child process with
  `JOBBOARD_JAIL=host JOBBOARD_FAKE=1` and drive the real `process_job`, so dependency order,
  chained commits, a task's own checks failing the task, stop vs skip, best-of-N with a real
  winner, the notes note and a board killed mid-task are all exercised end to end, not mocked.
- Served the real board once on port **8123** with its own state folders (`JOBBOARD_PORT=8123
  JOBBOARD_JOBS/WORK/PLANS/QUEUES=…`), seeded two projects (one `ready` queue, one `stopped` with
  a failed task and its job record) and curled: `/projects` lists both; `/project/notes-a` shows
  `ready to start`, `M1 - it runs: 0 of 2 tasks`, `3 left - about 1h 25m`, `Start the tasks`,
  `Don't start new tasks after`; `/project/notes-b` shows `stopped after a failed task`,
  `1 of 3 tasks done`, `1 failed or skipped`, `Run it again`; `/plan/notes-a` carries the same
  card; the job page shows `task t1 of …` and the missing-lessons line; `POST
  /queue/notes-a act=setup` answered `303 → /project/notes-a?note=saved` and the queue file on
  disk kept `on_fail: skip` and the `06:30` cutoff. Nothing started by itself. Server stopped and
  the port verified closed.

### Try it by hand

1. `cd web && JOBBOARD_PORT=8123 python3 jobboard.py`, open the printed
   `http://127.0.0.1:8123/?t=…` link.
2. Projects → make a plan for a project → **Approve plan**. The project page now has the
   **Tasks** card: the tasks in dependency order, all `waiting`, and "Start the tasks".
3. Press **Start the tasks**. The first task becomes `running now` with a link to its job; when
   that job finishes, the next one is queued automatically (one at a time — a task waits behind
   anything else already on the board).
4. Watch the card: `M1 - it runs: 1 of 2 tasks`, `2 left - about 1h 05m`, and **Now:** with the
   current task. Open a task's job: its page says `task t2 of <project>`; after a retry also
   `run 2 of this task`; if the agent wrote no lessons, a line says so.
5. Let a task fail: with **Stop the queue** the rest wait and the card says the queue stopped;
   with **Skip the tasks that depend on it** they go `skipped` and unrelated tasks carry on.
   **Run it again** under a failed, skipped or interrupted task puts just that task back.
6. **Pause** stops new starts (the running task finishes), **Resume** carries on, **Cancel** also
   stops the job that is running. Set **Don't start new tasks after** `06:30`: past that time the
   card says "No new task starts after 06:30 - press Resume to carry on", and Resume clears it.
7. Restart the board (`Ctrl-C`, start it again) while a task runs: the job says `Interrupted`,
   the card shows the task starting again (its job is resumed by session id, and the timeline
   names the checkpoint it continues from). A queue you had paused stays paused.

### Unfinished / assumed

- **Approve does not launch.** 4a's rule ("Nothing runs until you start the tasks") and 4b's
  wording ("Approve plan turns the tasks into queued jobs") pull apart, so approving writes the
  queue — tasks `queued`, in dependency order, on disk — and the card's **Start the tasks**
  launches it. Both suites' wording is kept.
- **Restart re-runs the interrupted task in the folder as it stands** (resuming its session), and
  records + displays the last checkpoint commit as where it left off. It does **not** hard-`reset`
  the folder to that commit: an agent may have committed work after the last checkpoint, and
  rolling back is U02's explicit button, not something to do silently. Say the word if you want
  the automatic rollback instead.
- **Best-of-N count and early stop come from the board settings** (`attempts` 2–4, `stop_early`),
  not from a per-task choice: a size-M task (or a task's 2nd run) gets N attempts when the
  project ticked the box. U05's rule is untouched, so the rest of the attempts are skipped only
  when the **first** attempt passed cleanly — a task that already failed once really does use all
  N (that is the point of the box).
- The **time-left estimate** is rough on purpose: S = 20 min, M = 45 min (what the planner prompt
  promises), recalibrated once real tasks have finished with measured seconds, and a best-of-N task
  counts one try per attempt.
- The cutoff is stored as the **next** occurrence of that clock time (`plan.set_cutoff`), so
  "06:30" pressed at 22:00 means tonight; after it passes, nothing new starts until Resume or a
  new time.
- Only one queue per project, and it follows the approved record. Editing the plan after starting
  the tasks means approving again; a queue that is `running`, `paused` or `stopped` is never
  silently replaced — approving again replaces only a `finished`/`ready` one.
- No network, no installs, stdlib only. `~/agent-sandbox` untouched: every test uses its own temp
  `HOME`/state folders, and the smoke run used its own folders under `/tmp` and port 8123.

---

## Job 10 — Upgrade 6a: vision check, part 1 (screenshots + the model's verdict)

Branch: `agent/jobboard-next` (local only — **not pushed**; ready for Jason to review, push and
merge). PLAN.md has the findings and the step list, all ticked.

**The one thing that cannot work in this sandbox: there is no browser.** Checked, not assumed —
`python3 -c "import playwright"` → ModuleNotFoundError, `node -e "require.resolve('playwright')"`
→ not found, no `playwright` / `chromium` / `google-chrome` / `firefox` on PATH, no
`~/.cache/ms-playwright`, and `Containerfile` installs neither. No Pillow either. With no network
nothing can be installed, so **screenshots were never taken here**: the engine, the jail command,
the pages, the bundle and the routes are built and tested against a mocked browser and a fake
model server, and both drivers were *run* far enough to prove they fail cleanly. The model side is
fine — `GET 127.0.0.1:8080/v1/models` here reports
`"architecture": {"input_modalities": ["text", "image"]}`, so the local model can see pictures.
What has to be installed is in "To make screenshots work" below.

Commits in this job (on top of `a311abc`, job 08's last commit):

- `37ebbf2` job 09 residue: the U10 (morning report) PLAN.md left uncommitted in the shared copy
  — committed as found, not rewritten
- `53072c6` U06a: the check itself — engine, jail capture, page, bundle, `/api/shot` (39 tests)
- last commit: a screen check cannot stall the gate (its own time budget) + a plain message when
  the model has no image input; PLAN.md ticks and this report (42 tests)

### Files changed

| File | What it is |
| --- | --- |
| `web/visioncheck.py` | **new** (544 lines, `# U06`) — the glue: the check-entry rules (`check_ok`/`frozen`), PNG width + the max-1280 px downscale (stdlib `zlib`, no Pillow), backend detection, the jail capture (driver in a here-doc, spec in a base64 env var, PNGs back as one base64 line on stdout), `run_check` → the `checks.run_all` result with its log file and saved shots, `runners()` for `extra_runners`, and `shot_file()` for the route. |
| `web/visionshot.py` | **new** (140 lines) — the capture driver for a sandbox with **Python** Playwright: temporary local http server for local files, the steps (`click`/`type`/`key`/`wait`/`wait_for`/`shot`), the final screenshot, JSON envelope on stdout. |
| `web/visionshot.mjs` | **new** (84 lines) — the same driver for a sandbox with **Node** Playwright. |
| `web/upgrades.py` | `freeze_checks`/`job_checks`/`checks_ok` keep a vision check's `type` and fields (it has no `cmd`); the Checks card shows verdict + issues + the pictures; the review bundle lists the screenshot names and what the model saw wrong; `stop_job_containers` also stops the browser container (`agentshot-…`). |
| `web/jobboard.py` | `run_gate` and "Run checks again" pass `extra_runners=visioncheck.runners(...)` (the screenshot runs in the same jail as the checks, the model is asked only after the agent stopped); `GET /api/shot/<jid>?src=&n=&i=`. |
| `web/badges.py` | an unsure screen check badges as `looks right (not sure)` instead of looking like a hard fail. |
| `web/tests/test_u06a_vision.py` | **new**, 42 tests. |
| `PLAN.md` | findings + steps. |

Nothing in `web/agentjobs_upgrades/` was changed: `vision.check` (two votes), `vision.check
→ to_check_result`, `checks.run_all(extra_runners=…)` and `verify.build_fix_prompt`'s
"Problems seen on screen" block are used exactly as they were written.

### How each requirement is met

1. **New check type** — `{"name":"looks right","type":"vision","target":"web",
   "open":"index.html","steps":[…],"spec":"…","size":[1280,800],"mode":"advisory"}` is read by
   `checks.load_checks` (extras land in `Check.extra`) and validated by `visioncheck.check_ok`
   before it is frozen into the job record at job start (`freeze_checks` → `m["checks"]`), so the
   agent cannot change what it will be checked with. `steps` understands
   `{"click": sel}`, `{"type"/"fill": sel, "text": …}`, `{"key": k}`, `{"wait": ms}`,
   `{"wait_for": sel}` and `{"shot": name}` (an extra picture at that point).
2. **Web target, Playwright, temporary http server** — `visioncheck.backend()` picks Python or
   Node Playwright, and says which piece is missing when neither is usable (module there but no
   Chromium → "run `playwright install chromium`"). `open` is a file inside the project (served by
   a throw-away `127.0.0.1` static server started by the driver, because ES modules do not work
   over `file://`) or a URL; `<port>` in a URL means that server's port. The browser runs in the
   **same jail as the checks** (`make_wrap` → `jail/run-local.sh sandbox-run`, `--network none`,
   only the project mounted), and the driver and its PNGs travel over the command's stdin/stdout,
   so nothing is written inside the project folder — no stray file in the diff or the guard.
3. **Ask the model** — `vision.build_request` (PNG, `data:image/png;base64,…`) POSTs to
   `http://127.0.0.1:8080/v1/chat/completions` (`JOBBOARD_MODEL_URL` overrides it); the strict
   prompt that walks the spec point by point and answers with
   `{"pass": …, "issues": […], "confidence": …}` is the one in `vision.PROMPT`. `vision.parse_verdict`
   finds the last JSON object with a bool `pass` (think tags stripped, python-ish quoting
   tolerated); no JSON after one retry → `inconclusive`. Width is held to 1280 px: the viewport is
   clamped and `fit_width` rescales anything bigger (nearest pixel, 8-bit PNG) before it is sent.
4. **Ask twice** — `vision.check(votes=2)`: pass only when both pass, disagreeing votes or no
   answer → `not sure`. `votes` in the entry allows 1–5.
5. **After the agent** — the only place the runner is registered is `run_gate` (round 0 and after
   every fix round) and the manual "Run checks again" button, so a screenshot never competes with
   a running agent for the model.
6. **advisory / required** — `checks.summarize` ignores `mode: "advisory"` when it decides
   pass/fail, so an advisory screen check is shown but never fails a job. With
   `"mode": "required"` a fail makes the summary `failed`, the banner names the check, and
   `verify.build_fix_prompt` puts the issues under "Problems seen on screen:".
7. **Page and bundle** — the Checks card row shows `✓/✗/?` + the name, `vision: index.html ·
   advisory · asked 2x · <page url>`, the verdict in words ("looks right" / "does not look right" /
   "not sure"), the screenshots (thumbnails, click for full size, served by `/api/shot`, only from
   inside `JOBS/<job id>/`, login cookie needed) and the issues underneath. The badge links to the
   check's log, which holds the spec, the steps, both model answers and the verdict. The bundle gets
   `- screen check \`looks right\`: fail (required)`, `- seen on screen: …` and
   `- screenshot: vision-looks_right-1-final.png` per round (and for a re-check).

### To make screenshots work (needs Jason, once)

The image has `python3` and `node` but no browser and no Playwright. Either:

```
python3 -m pip install playwright && python3 -m playwright install --with-deps chromium
```

or

```
npm install -g playwright && npx playwright install --with-deps chromium   # plus NODE_PATH
```

The **browser must live in the image**: the sandbox mounts only `/work/<folder>`, `/home/agent`
(the job's own `state/home-local`) and the SDK, so `~/.cache/ms-playwright` cannot be mounted in —
unless you point `PLAYWRIGHT_BROWSERS_PATH` at a folder inside one of those mounts. `web/tests`
stay green either way; nothing here calls the model server or a real browser.

Until that is done every vision check answers **not sure** with the exact missing piece
("no Playwright and no Chromium … this is not a problem with the code — do not try to install
anything"). In `advisory` mode nothing else changes; in `required` mode it does fail the job, on
purpose — a check you asked to be required that cannot run should be loud.

### Verified

- Gate command, as in `AGENT_CHECKS.json`:
  `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore -m unittest discover -s tests`
  → **`Ran 592 tests in 184.461s` / `OK`** (baseline before this job: 550 tests, OK; +42).
  Nothing deleted, skipped or weakened — `test_rest.py::Vision` and every earlier suite pass
  untouched.
- The new module alone: `python3 -W ignore -m unittest tests.test_u06a_vision` →
  `Ran 42 tests in 6.794s / OK`. What it really runs, not mocks: the capture **command** built and
  executed for real (`python3 - <<EOF` with the real `visionshot.py`, which on this machine
  answers "Playwright for Python is not installed…" — that proves the here-doc quoting, the
  envelope protocol and the honest failure path); `node --check` on `visionshot.mjs`; the model
  behind a real in-process http server with canned answers (both pass, both fail, garbage → 4
  requests because of the retry, a disagreeing pair, a server answering 500); the PNG path
  (2400×1500 → the bytes the model is sent are ≤1280 wide, exact pixels on a small downscale,
  an undecodable PNG passed through with a note, a non-PNG shot left out); AGENT_CHECKS.json →
  frozen record → `checks.Check` keeping `type`/`spec`/`open`/`steps`; a required fail reaching
  `build_fix_prompt`.
- Served the real board once, **read-only**, on port **8123** with its own state folders
  (`JOBBOARD_JOBS/WORK/PLANS/QUEUES/SETTINGS/NOTIFY` under `/tmp`, `~/agent-sandbox` untouched),
  seeded a finished job whose checks were `python3 -m unittest` plus a `looks right` vision check,
  and curled: the job page carries `screenshot of index.html`, `not sure`, `asked 2x`,
  `The two looks disagreed.` and `src="/api/shot/20261111-120000-ab12?src=round0&amp;n=1&amp;i=0"`;
  that URL answered `200`, `Content-Type: image/png`, `Content-Length: 1454` (a real 320×200 PNG),
  without the cookie `403`, with a bad `i=7` `404`; `/api/bundle/…` printed
  `screen check \`looks right\`: inconclusive (advisory)` with
  `- seen on screen: The two looks disagreed.` and `- screenshot: vision-looks_right-1-final.png`.
  Server stopped and the port verified closed.
- One small probe of the model server (read-only): `GET /v1/models` reports
  `input_modalities: ["text","image"]`; `POST /v1/chat/completions` returns 404 **from this
  sandbox** (the harness only answers `/v1/models`), so a real screen-check conversation could not
  be run here. The request shape is exactly `vision.build_request`'s, which the tests check.

### Try it by hand (Orb Chase, `~/cline-test/index.html`)

1. Install the browser (above). Then make the game a board project:
   `cp -r ~/cline-test ~/agent-work/orb-chase` and open Projects → *orb-chase*.
2. Put this in `~/agent-work/orb-chase/AGENT_CHECKS.json` (keep any tests you have alongside it):

   ```json
   {"checks": [
     {"name": "the game looks right", "type": "vision", "target": "web", "open": "index.html",
      "steps": [{"click": "#start"}, {"wait": 600}],
      "spec": "a dark play field, one glowing red orb the player chases, the score in the top right and a Start button before the game begins",
      "size": [1280, 800], "mode": "advisory"}]}
   ```
3. New job in that folder — "make the orb chase game look right". The check is frozen into the
   record before the agent starts (the page says *Checks from AGENT_CHECKS.json at job start:
   `python3 …`, `screenshot of index.html`*).
4. When the agent finishes, the **Checks** card shows *After the agent finished*, then one row per
   check. The screen row shows the verdict in words and, right next to it, the screenshot; click it
   for full size. Under it are the problems the model listed. The badge
   `the game looks right` in the banner opens the log: the spec, the steps the browser took, the
   page URL, both model answers verbatim and the verdict.
5. **Run checks again** re-screenshots and re-asks without running an agent (the model is free).
6. Now make it matter: change `"mode"` to `"required"`, and break the screen on purpose (delete the
   score). The job ends *Checks failed: the game looks right*, the agent gets a fix round whose
   prompt contains *Problems seen on screen: no score…*, and the timeline shows
   `Screen check "the game looks right": does not look right`.
7. **Download .md** (review bundle) and look under `## Verify gate`: verdict, mode, each issue, and
   the screenshot file names — the reviewer can ask for the picture by name.
8. Take the browser away again (or start with none): the row reads `not sure`, the issue says no
   Playwright/Chromium and that it is not a code problem, and an advisory check leaves the job alone.

### Unfinished / assumed

- **Screenshots have never actually been taken here** (no browser, see above). The drivers are
  executed as far as this machine allows and syntax-checked, but the Playwright calls themselves —
  `--no-sandbox`, `wait_until="load"` + a 400 ms settle, the 10 s per-step timeout,
  `page.fill`/`waitForSelector` — are untested against a real Chromium and may need tuning for a
  given app (an app that draws with WebGL or needs a pointer may want extra steps).
- The **model was never asked about a real screenshot here** (this sandbox's proxy refuses
  `/v1/chat/completions`). The prompt, the two-vote rule, the parsing and the sizing are all tested
  against a fake server with that exact shape.
- One judged screenshot per check: the final screen. `{"shot": "name"}` in `steps` adds pictures
  for the page and the bundle; the last one is what the model is shown.
- Vision checks come from **AGENT_CHECKS.json only**. The new-job form's Checks box stays
  "one command per line" (it writes commands into the project). A UI for adding a screen check from
  the board is a separate slice.
- Checkpoints (`at_gate`) and best-of-N ranking do **not** take screenshots — no runner is
  registered there, and those comparisons are about tests. A `required` screen check still decides
  the job at the gate. Say the word if you want a picture per checkpoint instead.
- `target` only accepts `"web"`; the `android-smoke` type in `checks.Check` is untouched (6b).
- A check's `timeout_s` is its wall-clock budget: the browser gets ≤600 s, at most 5 votes, and no
  new vote once the budget is spent (that is what keeps a hung page or a slow model from stalling
  the whole gate).
- `inconclusive` is not a pass: `advisory` → shown, job unaffected; `required` → fails the job. If
  you would rather an unsure check never fail even when required, that is a small third mode.
- The resizer is nearest-pixel and 8-bit RGB/RGBA only (what Playwright writes); anything else is
  sent as it came, with a note in the log. The viewport is clamped ≤1280 wide first, so normally it
  is only a safety net.
- **No vision check was added to this repo's own `AGENT_CHECKS.json`**: it would try to screenshot
  the board on every job here and answer "not sure" until Chromium is in the image. The exact entry
  is documented above instead — say the word and I'll add it.
- `~/agent-sandbox` untouched (its `state/web-token` only ever read); every test and the smoke run
  used their own temp folders, port 8123, and `JOBBOARD_READONLY=1`. No installs, no network,
  stdlib only.

---

## Job 11 — Upgrade 6b: vision check, part 2 (Android)

Branch: `agent/jobboard-next` (local only — **not pushed**; ready for Jason to review, push and
merge). PLAN.md has the findings and the step list, all ticked.

**The one thing that cannot work in this sandbox: there is no device.** Checked, not assumed —
`adb` **is** installed (`/opt/android-sdk/platform-tools/adb`, on PATH), but
`adb devices` → **no devices attached**, and there is **no emulator to start**
(`/opt/android-sdk/emulator` and `system-images` are not in the image; `avdmanager` exists but
there is nothing to create an AVD from and no network to fetch one). `~/agent-sandbox/state/
android-sdk` does not exist in this container. So **no real phone or emulator was ever touched
here**: the engine, the checks, the pages and the bundle are built and tested against **a fake adb
script on PATH** (`web/tests/fixtures/fakeadb/adb`), and on this machine every Android check
resolves to the honest skip the job asked for:

```
$ python3 -c "from agentjobs_upgrades import android; a=android.Adb('',android.find_adb(''));
              print(android.devices(a), android.pick_device(a,''))"
[] ('', 'No device connected - plug in a phone or start an emulator.')
```

That is reported as `skipped`, forced to `advisory`, and **does not fail the job** — which is
exactly the ground rule for this job.

Commits in this job (on top of `5a85117`, job 10's last commit):

- `778ecb8` U06b: PLAN.md — findings (what 6a's `run_check` contract already gives me, where the
  runners/records/pages are wired, adb is here but no device and no emulator package, and why adb
  cannot run in the jail) plus the step list
- `37a3da5` U06b: the device engine (`agentjobs_upgrades/android.py`)
- `da14f79` U06b: the two checks (glue + gate/cards/bundle/settings wiring)
- `debf33f` U06b: 40 tests against a fake adb on PATH
- last commit: the device log now says whether the screen settled (`the screen held still after 2
  screenshots` / `the screen was still changing after 3 screenshots - using the last one`) + 1 test;
  PLAN.md ticks and this report (41 tests)

### Files changed

| File | What it is |
| --- | --- |
| `web/agentjobs_upgrades/android.py` | **new** (494 lines) — the tested engine: `find_adb`, `devices`/`pick_device`, `find_apk`, `install -r`, which app got installed (`pm list packages -3` diff → `applicationId` in `app/build.gradle` → the check's `pkg`), `cmd package resolve-activity --brief`, `am force-stop` + `am start -W`, crash detection (`pidof` + `logcat -d` blocks that name *our* package), `exec-out screencap -p`, the stable-screen wait, the `input tap/swipe/text/keyevent/wait` steps, and the two sequences `capture()` (6a's capture shape) and `smoke()`. Every device command is one `Adb.call(argv)` — argv only, never a shell — with `run`/`sleep`/`now`/`deadline` injectable so the tests are quick. |
| `web/androidcheck.py` | **new** (318 lines, `# U06b`) — the glue: what a valid Android check entry is (`check_ok`) and its frozen record (`frozen`), the spec the engine is given, which device to use, the three clear errors, the result + log the gate wants, the "no device" skip, and `runners()` (adds `android-smoke`, and a `vision` that routes `target: "android"` here and everything else to 6a). |
| `web/visioncheck.py` | three small additions: `spec_fn` (so a check's own fields reach the capture — that is how the APK and the steps get to the phone), a capture that reports `fatal` is a **fail** and not "not sure" (with `issue_extra`, i.e. the crash lines, in the issues), and the word `skipped` → "no device". The log label "browser output:" is now "capture log:". |
| `web/upgrades.py` | `checks_ok`/`freeze_checks` accept `target: "android"` vision checks and `android-smoke`; settings gain `android_device` and `android_adb` (and `load_settings` now keeps text settings); the Checks card gets `_android_row` and names the device/app/crash in the vision row; `_check_hint` says "install and start the app on a phone" / "screenshot of the app on a phone"; the review bundle gets `_android_md`. |
| `web/agentjobs_upgrades/checks.py` | `detect_checks` adds a **required** `app runs` `android-smoke` check for a real Android app (a `gradlew` **and** an `AndroidManifest.xml` under `app/src/main/`), so an Android project with no AGENT_CHECKS.json still has to start. `test_checks.py::test_gradle` (which pins the two gradle checks) stays true — a plain Gradle project gets no device check. |
| `web/jobboard.py` | `run_gate` and "Run checks again" register the Android runners on top of the 6a ones (`androidcheck.runners(web=…, cfg=…)`). |
| `web/badges.py` | a device check badges `… (no device)` when nothing was checked on, and keeps `… (not sure)` for an unsure screen check. |
| `web/tests/fixtures/fakeadb/adb` | **new** (executable, 147 lines) — the fake device. |
| `web/tests/test_u06b_android.py` | **new** (619 lines), 41 tests. |
| `PLAN.md` | findings + steps. |

`web/agentjobs_upgrades/vision.py`, `checks.py`, `verify.py` and `report.py` are used as written —
`vision.check(votes=2)`, `to_check_result`, `checks.run_all(extra_runners=…)`,
`summarize`'s advisory rule and `verify.build_fix_prompt`'s "Problems seen on screen:" block all do
their part; the model half of requirement 3 **is** 6a: an Android check goes through
`visioncheck.run_check(..., capture_fn=android.capture)`, so the PNGs are saved, sized and asked
about exactly as a web screenshot is.

### How each requirement is met

1. **Target `android`** — `{"name": "the screen", "type": "vision", "target": "android",
   "spec": "…", "apk": "app/build/outputs/apk/debug/app-debug.apk", "pkg": "com.example.app",
   "steps": [{"tap": [360, 1900]}, {"text": "buy milk"}, {"key": "KEYCODE_ENTER"},
   {"wait": 400}]}`:
   * **Install** — `find_apk` takes the check's `apk` or globs `app/build/outputs/apk/debug/*.apk`
     (also `*/build/outputs/apk/debug/` and `build/…`), then `adb install -r`.
   * **Find and launch the main activity** — `adb shell cmd package resolve-activity --brief
     <pkg>`, then `am force-stop` (so an old instance cannot fake a pass) and `am start -W`. The
     package comes from the check, or from the third-party package list before/after the install,
     or from `applicationId` in `app/build.gradle`.
   * **Wait until the screen is stable** — `wait_stable`: two `exec-out screencap -p` 1 s apart
     that are byte-identical, at most 20 s. A device that animates forever never settles: the last
     frame is used and the log says `stable: false`.
   * **The screenshot** — `adb exec-out screencap -p` (raw PNG over stdout, nothing written inside
     the project folder).
   * **Optional steps with a screenshot after each** — `input tap x y`, `input swipe x1 y1 x2 y2`,
     `input text <t>` (spaces → `%s`), `input keyevent <KEYCODE_…>`, `{"wait": ms}`; the shots are
     `start`, `step1…N`, `final`, and `final` is what the model is shown. A step the device refuses
     is recorded and the check carries on.
2. **The device** — the check's `"device"`, else the board's setting `android_device`, else the
   first entry in `adb devices`; a `{{ANDROID_DEVICE}}` nobody filled in counts as unset (auto).
   **No emulator is ever started** (the spec forbids it while the model is busy, and this image has
   no emulator anyway); there is nothing to stop, and the app itself is `force-stop`ped before each
   start so every check begins clean.
3. **Then the same model check as 6a** — the capture answers in 6a's shape, so
   `visioncheck.run_check` sizes it (≤1280 px), saves the PNGs under `JOBS/<id>/checks/round-N/`,
   writes `check-<name>.log` (the adb transcript + both model answers), asks twice
   (`vision.check(votes=2)`) and returns the `checks.run_all` dict. `/api/shot` serves the pictures
   with no new route.
4. **Clear errors** — `No device connected - plug in a phone or start an emulator.` /
   `APK not built - there is no debug APK under app/build/outputs/apk/debug.` (also
   `APK not built: <rel> is not a file inside the project folder`, and
   `APK not built: <pkg> has no launcher activity to open`) /
   `App crashed on start - it was not still running after 10 seconds.` (smoke) and
   `App crashed on start - the screen never settled.` (screen check), with the **logcat lines in
   the result** (`crash`, and the same lines in `issues` → "Problems seen on screen:" in the fix
   prompt, the card and the bundle). A crash is found with `pidof` *and* `logcat -d` blocks
   (`FATAL EXCEPTION`, `Fatal signal`, `ANR in`, `beginning of crash`, `has died`) that name our
   package; a device with no `pidof` still gets a verdict from logcat, and another app's crash is
   never blamed on ours. An install the device refused and a start it refused are their own errors.
5. **`android-smoke`, required by default** — `{"name": "app runs", "type": "android-smoke",
   "mode": "required"}`: install, start, the app must still be running after 10 s (`hold_s` 1–120),
   **no model, no screenshots**. `detect_checks` gives it to any real Android app automatically, so
   a project with no AGENT_CHECKS.json cannot pass the gate with an app that will not start. A
   check with no device is `skipped` + `advisory`, so it never fails a job; no APK, a refused
   install and a crash are real fails that start a fix round.

### Verified

- Gate command, as in `AGENT_CHECKS.json`:
  `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore -m unittest discover -s tests`
  → **`Ran 633 tests in 266.201s` / `OK`** (baseline before this job: **592 tests, OK**; +41).
  Nothing deleted, skipped or weakened — the earlier suites (`test_checks`, `test_u01`,
  `test_u06a_vision`, the JAIL/U07/VOICE ones) all still pass untouched.
- The new file alone: `python3 -W ignore -m unittest tests.test_u06b_android` →
  `Ran 41 tests in 79.734s / OK`. It really runs the fake binary as a subprocess through PATH, so
  what is asserted is the exact argv the device would see, e.g.
  `-s d install -r <apk>`, `-s d shell pm list packages -3`, `-s d shell cmd package
  resolve-activity --brief com.example.app`, `-s d shell am force-stop …`,
  `-s d shell am start -W -n com.example.app/.MainActivity`, `-s d shell logcat -c/-d -t 200`,
  `-s d exec-out screencap -p`, `-s d shell input tap 360 1900`, `-s d shell input text buy%milk`.
  Covered: install ok/refused; the activity lookup and a refused start; which app got installed
  (pm diff, `applicationId`, and "the device would not say which app it is"); the stable wait
  needing **two matching frames** (frames `a,b,b` → stable after 3 shots; `a,b,c,…` → not stable,
  still proceeds, and the log says *the screen was still changing after 3 screenshots — using the
  last one*) and never waiting past its own budget; crash by `pidof`, crash by logcat only
  when `pidof` does not exist, another app's crash ignored; steps sent in order with a shot after
  each and a refused step reported but not fatal; `tap`/`swipe` arity, printable text,
  `KEYCODE_*`/digit keys; the APK outside the project folder refused; no device and no adb;
  settings → device; `hold_s`; the model being asked **twice** over a phone screenshot (in-process
  fake model server) and **not asked at all** when the device said it crashed; `checks.run_all` →
  `failed` → `build_fix_prompt` carrying `App crashed on start` + `FATAL EXCEPTION`; the frozen
  record and the rejects (`../x.apk`, an absolute path, a junk `pkg`/`device`, a 3-number tap);
  `detect_checks` for an app vs a plain Gradle project; the card row, the badge and the bundle.
- One run **outside the tests**, end to end (`checks.run_all` + the fake adb + a throw-away Android
  project in `/tmp`: `gradlew`, `app/src/main/AndroidManifest.xml`,
  `app/build/outputs/apk/debug/app-debug.apk`, `app/build.gradle`):

  ```
  detected: [('build', 'command'), ('tests', 'command'), ('app runs', 'android-smoke', {'mode': 'required'})]
  == app stays up ==  ('green', 'Checks passed')            verdict=pass    device=demo-5554 pkg=com.example.notes
  == app crashes ==   ('red', 'Checks failed: app runs')   verdict=fail    + "Problems seen on screen: E AndroidRuntime: FATAL EXCEPTION: main" in the fix prompt
  == no device ==     ('yellow', 'Done – not verified …')  verdict=skipped (advisory), no fix round
  ```

### Try it by hand (with a phone plugged in)

1. `adb devices` must list the phone (USB debugging on). Optionally set the serial in the board's
   settings (`android_device`) — leave it empty to use the first device.
2. In the project's `AGENT_CHECKS.json`:

   ```json
   {"checks": [
     {"name": "app runs", "type": "android-smoke", "mode": "required"},
     {"name": "the notes screen", "type": "vision", "target": "android",
      "steps": [{"tap": [360, 1900]}, {"text": "buy milk"}, {"key": "KEYCODE_ENTER"}],
      "spec": "a list of notes and a + button bottom right", "mode": "required"}]}
   ```

   With no AGENT_CHECKS.json an Android app gets the `app runs` smoke check by default.
3. Run a job. After the agent finishes, the Checks card shows
   `✓ app runs · android: install + start app-debug.apk · required · emulator-5554 · com.example.app · the app ran`
   and the screen check's row with the phone's screenshot (click for full size). The badge opens
   the log: the adb transcript, `stable: true`, both model answers, the verdict.
4. Break the app so it dies at launch: the job ends *Checks failed: app runs*, the timeline says
   `Device check "app runs": the app did not run`, and the fix round's prompt contains
   *Problems seen on screen:* followed by the `FATAL EXCEPTION` lines.
5. Unplug the phone: the row reads `no device`, the log says *No device connected — plug in a
   phone or start an emulator.*, and the job is **not** failed for it.
6. **Download .md**: `- device check \`app runs\`: fail (required)` / `- device emulator-5554, app
   com.example.app, apk app-debug.apk` / `- logcat: E AndroidRuntime: FATAL EXCEPTION: main`.

### Unfinished / assumed

- **No real device or emulator was used here** (see the top). Every Android claim above is verified
  against the fake adb: the argv, the parsing of real `adb devices`/`pm list packages`/
  `resolve-activity`/`am start -W`/logcat/screencap output shapes, and the PNG bytes. What a real
  device may still need: a device that needs `adb wait-for-device` or a `adb root`/`adb tcpip`
  step first, foldables/emulators whose `screencap` is slow (the defaults are 120 s for one adb
  command, 300 s for the install and 60 s for a screenshot, inside a whole-check budget of
  `timeout_s` — 1200 s for a detected check, so a stalled device cannot hold the gate), and apps whose
  launcher activity is not the one `resolve-activity` returns (put `"pkg"`/fix the manifest).
- **adb runs on the host, not in the jail.** This is a decision, not an oversight:
  `jail/podman-local.sh` starts every job container with `--network none` and no USB, so a
  container has no way to reach a device — and the project's own code still never runs on the host.
  The guard rails are: one `Adb.call(argv)` that never goes through a shell, the device words are
  fixed, the APK path is resolved and must stay inside the project folder, `pkg`/`device`/step text
  are validated (a serial is `[A-Za-z0-9._:-]`, text is printable ASCII, keys are `KEYCODE_*`), and
  nothing is written inside the project folder (screenshots come back as bytes, like 6a's driver).
  The device is outside the sandbox's blast radius either way — if one is connected, a check can
  install and tap on it. Say the word if you want Android checks limited to a fixed allow-list of
  serials, or off unless `android_device` is set.
- `{{ANDROID_DEVICE}}` in a check is treated as "not filled in" (use the first device), so a spec
  copied verbatim cannot point at a device called `{{ANDROID_DEVICE}}`.
- The **smoke check is not run at checkpoints or in best-of-N ranking** (same as 6a's screen
  checks) — only at the gate and by "Run checks again". A `required` one still decides the job.
- A user **Stop** pressed during the smoke check's 10 s hold is not interrupted mid-sleep; the
  check's own `timeout_s` budget bounds it (that is what keeps a device check from stalling the
  gate).
- A phone screenshot is portrait; 6a's resizer only shrinks, so a 1080-wide phone picture is sent
  as it came (≤1280 already). A tablet in landscape gets downscaled.
- No Android check was added to **this repo's own** `AGENT_CHECKS.json` — with no device here it
  would only ever say "no device". The `app runs` default appears for Android projects, not here.
- The screen-stability comparison is exact PNG bytes, so a screen with a blinking cursor or a
  spinner never reports `stable: true`; the check proceeds with the last frame and the log says so.
  `"stable"` is in the result for the page; a tolerance-based compare is possible if real apps need it.
- The board never starts or stops an emulator (nothing to start here, and the spec forbids it while
  the model is busy). If you want the board to boot an AVD for a check, that is a separate slice —
  it needs an emulator in the image and a rule for who owns it.
- `~/agent-sandbox` untouched; no installs, no network, stdlib only; no push.
