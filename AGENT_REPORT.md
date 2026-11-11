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

---

## Job 03 — Upgrade 8: approve / merge buttons

Branch: `agent/jobboard-next` (local only — **not pushed**; ready for Jason to review, push and
merge). PLAN.md has the findings and the step list for this job, all ticked.

This job ran twice. The first pass wrote the whole feature — `web/decisions.py`, the job-page
hooks and 97 tests — and **stopped before committing any of it**, so the re-run (12 Nov) committed
that work, ran the live copy on `127.0.0.1:8091` with its own state folders, and fixed the one real
bug that run caught (a decision that could vanish without a word, `24b06ad`). Nothing was
rewritten: the merge / reject / undo / token engine in `web/agentjobs_upgrades/actions.py` (already
tested in `test_rest.py::Actions`) is used exactly as written.

Commits in this job:

- `8ef8dd7` PLAN.md — findings (the engine is already there and tested, so this job is the glue:
  which branch a job worked on, the Decide card, the human-only secret, the follow-up jobs) and
  the step list
- `cb1346a` the watchdog half of requirement 6: `.local/state/agentjobs` is a private location, so
  a job that goes looking for the secret is a safety stop (bundled in that commit)
- `2212f33` `web/decisions.py` — the glue (623 lines) + the `changes` timeline icon
- `c27136c` the job-page hooks in `jobboard.py` (the card, the `aj=` cookie, `POST /decide/<id>`)
- `affca74` `web/tests/test_u08_decide.py` — 84 tests, temp repos (the commit message says 97; the
  file has 84 test functions — see Verification)
- `24b06ad` the bug the 8091 run caught: `POST /decide/<id>` used the worker's *launch* validator
  as its gate, so a press on a finished job whose record fails that check for an unrelated reason
  merged nothing, recorded nothing and said nothing; + 9 tests
- last commit: PLAN.md steps ticked and this section

### Files changed

| File | What it is |
| --- | --- |
| `web/decisions.py` | **new** (623 lines, `# U08`) — the glue, all of it on top of `actions.py`: the secret file + the browser's `aj` session + that session's form token (`secret`, `session_for`, `token_for`, `allowed`); `cannot_decide` (what a press is checked against); which branch is whose (`target_branch` from `git_branch_start`, else main/master; `job_branch` = the branch carrying this job's checkpoint commits → `agent/<id>` for a best-of-N winner → the folder's current `agent/...` branch → the newest `agent/...` branch with commits the target lacks, never a `-try` branch or one already merged); the six actions (`do_merge`, `do_undo_merge`, `do_reject`, `do_undo_reject`, `do_ask`, `do_fix_conflict`), each writing the record and the timeline; `fix_conflict_prompt`; the Decide card and its CSS. |
| `web/jobboard.py` | 36 lines: `import decisions`; the card between the review bundle and the Checks card; `GET /job/<id>` hands out the `aj=` session cookie and puts that session's token in the forms; `POST /decide/<id>` answers 403 without it and otherwise calls `decisions.action`; `decision_ctx()` is the only place the two modules are tied together. |
| `web/agentjobs_upgrades/events.py` | one icon: `"changes": "✎"`, so an "ask for changes" row has an icon in the U07 timeline like every other row. |
| `web/tests/test_u08_decide.py` | **new** (1006 lines), 93 tests (84 written by the first pass, +9 here). |
| `PLAN.md` | findings + steps. |

`agentjobs_upgrades/actions.py` is used as written — `Decisions.merge` (clean tree first,
fast-forward when the target is an ancestor, else the `Merge agent job <id>: <title>` commit,
`merge --abort` + `reset --hard` back to the recorded sha on a clash, the caller's branch restored
in `finally`), `undo_merge` (refuses once the target moved), `reject`/`unreject` (the
`agent/rejected/<id>` rename, 14 days), `merge_allowed`, `form_token`, `check_request`,
`change_request_prompt`. `load_secret`, `safegit.SafeGit` and `jailcfg.push_branch` are untouched.

### How each requirement is met

1. **The three buttons** — a finished job (`done` / `failed` / `stopped` / `interrupted` /
   `safety_stop`, the same set the board already uses) gets an "Approve or reject" card:
   `Merge into main`, an "Ask for changes" text box and `Reject`. A running job, a bench run or a
   read-only copy gets no buttons (a read-only copy still shows what was decided).
2. **Approve & merge** — the button names the branch the job started from (`git_branch_start`,
   main/master if the record has none) and the card spells out the pair: *Merges `agent/<id>` into
   `main` in the project folder, right here on this PC. Nothing is pushed.* Fast-forward when main
   has not moved, otherwise the merge commit `Merge agent job <id>: <title>`. Only enabled when
   `verify.status == "passed"`; for failed / never-run / `needs_review` / `stuck` / `verify_off` the
   button is grey with the reason in its tooltip and in a line above it ("The checks failed.", "The
   checks were not run.", …) and a **Merge anyway** box that only works when you type `MERGE`. A
   clash is aborted and the repo is left exactly as it was (`git status` clean, main unmoved, your
   branch still checked out), the card says so with a "What git said" box, and offers **Ask Qwen to
   fix the conflict** — a follow-up job whose prompt says *stay on `agent/<id>` and merge `main`
   into it, resolve everything, run the checks, commit the merge, do not merge into main yourself
   and never push*; it goes through the normal gate because it is an ordinary job.
3. **Ask for changes** — the text box reuses the existing follow-up feature
   (`new_job(mode, folder, prompt, offline, resume=session, parent=jid)`), so the child job is the
   same folder, the same branch and, when the job has one, the same agent session; its prompt is
   `actions.change_request_prompt`: your words plus the original task. An empty box records a
   refusal and starts nothing.
4. **Reject** — `m["decision"] = "rejected"`, the branch is renamed to `agent/rejected/<id>`, the
   folder and its files are left alone, and the card offers **Undo** (14 days; after that it says
   *Undo is only possible for 14 days*). A rejected card offers Undo only — no second Reject — and
   Undo brings the three buttons back. The board never deletes a branch or a folder.
5. **Pushing** — nothing here pushes: the merge is `SafeGit` on the project's own work copy, the
   record keeps `pushed: false`, and the card says *Not pushed — the board only ever pushes
   agent/... branches, so pushing main is yours to do*. `jailcfg.push_branch` /
   `jail/push-agent-branch.sh` still refuse anything that is not `agent/...`.
6. **Only a person at the page** — see "The secret and the sandbox" below.
7. **Record and timeline** — every decision appends to `decision_log` (kind, time, target, branch,
   how, the `before`/`after` shas, `pushed`, the follow-up job id, the reason for a refusal) and
   sets `decision` (`merged` / `merge-undone` / `rejected` / `rejection-undone` / `changes` /
   `conflict` / `fix-conflict`), and writes one timeline row: 🔀 merged, ↩ merge undone, 🗑 rejected,
   🔄 rejection undone, ✎ changes requested, 🔧 fix-the-clash job, ⚠ the clash. **Undo merge** is
   offered for the last merge and refuses once the target has moved since
   (*Something was committed on top since; undo it by hand.*); otherwise it puts the branch back at
   the recorded pre-merge commit.

### The secret and the sandbox (requirement 6)

A decision needs **all four** of: the board's existing `jb=` login cookie and Host/Origin checks,
a `POST`, an `aj=` session cookie, and that session's form token
(`HMAC(secret, "form:" + sid)`). Anything else is 403 before the record or the project is touched,
and `GET /decide/<id>` is not a route at all (404). The cookie is
`aj=…; Path=/; HttpOnly; SameSite=Strict`; a second visit keeps the session, so an old page still
works and a new one gets its own token. The secret itself never leaves the server.

How I checked that a job on this PC cannot read the secret file:

- It is `~/.local/state/agentjobs/secret`, created once by `actions.load_secret` as **0600 in a
  0700 directory**. Checked live, not assumed: the 8091 run printed
  `the secret file /home/agent/.local/state/agentjobs/secret is 0600 in a 0700 dir`.
- `jail/podman-local.sh` (`jail_local_args`) mounts **only** the job's own project folder
  (`$hostdir` over an empty tmpfs `/work`), `state/home-local` → `/home/agent`,
  `state/android-sdk` → `/opt/android-sdk` and two jail scripts. None of those contains
  `.local/state/agentjobs`, and the container's `$HOME` is `state/home-local`, not this home.
  `test_the_jail_scripts_mount_nothing_that_contains_it` reads the script's own `-v` arguments and
  asserts that no mount is the secret's directory or a parent of it, so the test breaks if anyone
  ever mounts the home folder.
- `jail/agent-jail.sh` (the bwrap jail) gives a fake empty `/home/agent` and refuses
  `$HOME/.local*` as a project folder, which the same test asserts.
- Defence in depth: `watchdog.SECRET_PATH_RE` names `.local/state/agentjobs`, so a job that runs
  `cat ~/.local/state/agentjobs/secret` gets a **safety stop**, not a read
  (`test_a_job_that_goes_looking_for_it_is_stopped`).
- **What was not done here:** this build sandbox has no container runtime
  (`command -v podman bwrap` → nothing), so I could not run the secret-cat inside a real job
  container. On your machine the one-line proof is
  `jail/run-local.sh sandbox-run <project folder> agentcheck-<id> -- cat ~/.local/state/agentjobs/secret`
  → no such file.
- A forged request from a script on the same machine is refused anyway: a job knows the URL, the job
  id and the record, but has no browser session and cannot make the token. That is a test
  (`test_a_forged_body_from_a_script_on_this_machine_is_refused`) and a smoke check.

### How to test it by hand, click by click

Start your own copy (never 8090) with its own state folders:

```
cd web && JOBBOARD_PORT=8091 JOBBOARD_WORK=/tmp/u08/work JOBBOARD_JOBS=/tmp/u08/jobs \
    JOBBOARD_PLANS=/tmp/u08/plans JOBBOARD_QUEUES=/tmp/u08/queues \
    JOBBOARD_SETTINGS=/tmp/u08/settings.json JOBBOARD_NOTIFY=/tmp/u08/notify.json \
    python3 jobboard.py
```

In that `$JOBBOARD_WORK/proj`, make a project whose work sits on a branch (`git init -b main`,
commit, `git checkout -qb agent/<job id>`, commit) and run a job in it, so the record has
`git_branch_start` and the gate ends `passed`. Then open that job's page:

1. The card reads *All checks passed.* with `Merge into main`, the ask box and `Reject`; under the
   button: *Merges `agent/<job id>` into `main` in the project folder, right here on this PC.
   Nothing is pushed.*
2. Press `Merge into main` → *Merged `agent/<job id>` into `main` (fast-forward) on 11 Nov 19:50*,
   *Not pushed — the board only ever pushes agent/... branches, so pushing main is yours to do.*,
   and `Undo merge`. The timeline gets 🔀 *Merged … (fast-forward) - not pushed*.
   `git log --oneline -1 main` shows the job's commit; `git branch --show-current` is still the job
   branch.
3. Press `Undo merge` → main is back at the recorded commit and the merge button returns.
4. Let main move on (`git checkout main`, commit, `git checkout -`), press merge again →
   `git log -1 --format=%s main` is `Merge agent job <id>: <title>`.
5. Take a job whose checks failed (the natural case): the button is grey with *The checks failed.*,
   and **Merge anyway** opens a box — `merge` does nothing, `MERGE` merges, and the record keeps
   which one it was.
6. Make the two branches change the same line: the press answers *These changes clash with the
   target branch. Nothing was changed.* with a "What git said" box; `git status` is clean and main
   has not moved. Press **Ask Qwen to fix the conflict** and the queued job's prompt starts
   *Jason asked to merge `agent/<id>` into main. The two changed the same lines, so the board
   aborted the merge and changed nothing. … Stay on `agent/<id>` and merge main into it …*.
7. Press `Reject` → *Rejected on … the branch is now `agent/rejected/<id>` and the folder is left
   as it is.* + `Undo`; `git branch` shows the rename and the files are still there. `Undo` puts the
   branch back and the three buttons return.
8. Type in the ask box → *You asked for changes on …: "…" — the follow-up job*, queued in the same
   folder with your words plus the original task.
9. From any job or script: a `POST /decide/<id>` without the cookie and token answers *That button
   only works from the job page in your browser* (403) and nothing moved.

There is a driver for all of that — 46 printed checks against a real server on 8091, in
`/home/agent/u08-smoke/smoke.py` (kept outside the repo on purpose):
`python3 /home/agent/u08-smoke/smoke.py` → `SMOKE OK`.

### Verification

- Gate command (`AGENT_CHECKS.json`):
  `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore -m unittest discover -s tests`
  → **`Ran 741 tests in 280.372s — OK`** (measured twice: 732 before this run's 9 tests, 741
  after). Where 741 comes from: `unittest.TestLoader().discover('tests').countTestCases()` = 741 =
  648 everywhere else + **93** in `web/tests/test_u08_decide.py` (84 from the first pass, +9 here).
  Two counts the first pass wrote down are off and worth naming: its "635 baseline" predates job
  01's 13 tests (the suite was 648 without any U08 test), and its "97 tests" for
  `test_u08_decide.py` is really 84. No test was deleted, skipped or weakened.
- Live copy: 46 checks, `SMOKE OK`, run twice. Timeline rows after the whole click-through:
  `merged, rollback, merged, needs_review, fix_round, rejected, restart, merged, changes`.
- The live run is what caught `24b06ad`: every in-process test used records that happened to pass
  `jobboard.check_record`, so the swallowed press stayed invisible until a real record (mode `fake`
  with plain text, which that validator refuses) went through the real route.
- Nothing in these tests touches `127.0.0.1:8080`, a real agent, port 8090, or `~/agent-sandbox`.

### Unfinished / assumed

- **Which branch to merge is discovered, not recorded** — the board never wrote down the branch an
  agent actually worked on. It tries, in order: a branch carrying this job's own checkpoint
  commits, `agent/<id>` (the best-of-N winner), the folder's current `agent/...` branch, the newest
  `agent/...` branch with commits the target lacks. It looks at the 8 newest `agent/...` branches
  (each candidate is one git call inside the jail), and the card always names the branch it means to
  merge. If the agent committed straight onto main the answer is honest: *Nothing to merge — this
  job left no branch with changes of its own*, plus how many commits main already took.
- A finished job's page now asks git for those branches: one `for-each-ref` plus up to 8
  `merge-base` checks, per job per 30 s, in that project's own jail (`upgrades.git_for`). A
  read-only copy never asks. If git cannot run at all, the card falls back to the record's own
  branch name and says nothing is mergeable instead of guessing.
- A merge needs a **clean working tree** in the project folder (the engine's rule, kept):
  *The project has uncommitted changes. Commit or clear them first.* Uncommitted work is never
  stashed or thrown away.
- Deciding while another job is running in that folder is refused, as is any decision for a bench
  run or for a record whose folder name is not a folder this board can use. A press the board will
  not take now comes back to the page with its word on the card (*The job has to finish before you
  can decide.*, *Another job is still working in that folder - decide once it has stopped.*)
  instead of vanishing — that was the bug the live run caught.
- All git for decisions goes through `upgrades.git_for` = `SafeGit` in the job's own jail
  (`-c core.hooksPath=/dev/null -c core.fsmonitor=false -c protocol.allow=never
  -c credential.helper=`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`), so a hook or config
  the agent planted in the project is not trusted and nothing can reach a remote. The live run used
  `JOBBOARD_JAIL=host` because there is no podman/bwrap in this sandbox; the wiring is the one
  upgrades 1–3 already use.
- `Undo merge` only resets a branch that still points where the merge left it; `Undo` for a
  rejection only renames back. Neither deletes anything, and neither undoes a **push** — pushing
  main stays yours.
- A decision does not change the job's `status` (a merged job stays `done`); the decision lives in
  `decision` / `decision_log`. The jobs list does not yet show a merged/rejected badge — not asked
  for, and easy to add on top of `badges.py` if you want it.
- Nothing expired is cleaned up: an `agent/rejected/<id>` branch older than 14 days just stays, and
  its Undo is not offered. Say the word if you want the board to prune them, or to list them.
- Two people can decide from two browsers (each gets its own session), but there is no audit line
  naming *who* decided — the board has no accounts, so a decision records its time only.
- `~/agent-sandbox` untouched; no installs, no network, stdlib only; no push. The branch is
  `agent/jobboard-next`, ready for Jason to review, push and merge.


---

## Job 07 — Upgrade 4a: requirements → plan (re-run, finished)

Branch: `agent/jobboard-next` (local only — **not pushed**; ready for Jason to review, push and
merge). PLAN.md holds this job's findings and its step list, all ticked, plus a "what this run
found" note.

This job had been started before. The whole feature — `web/planning.py`, `prompts/planner.md`,
`docs/tasks-schema.json`, the `jobboard.py` hooks and 53 tests — is commit `5b3efad`; the run that
wrote it stopped before ticking its steps, before the 8091 click-through and before its report
section. This run did not rebuild anything: it read it, clicked it end to end on its own port,
and fixed the four things that came out of doing that.

Commits in this run:

- `7a28338` job 09 residue: the U10 morning report (`web/morningreport.py`, its jobboard hooks,
  its PLAN.md note) left uncommitted by a run that stopped mid-way, committed as found so this
  job's diff stays clean — the suite was green with it, and it is not finished work of job 07.
- `ad8f340` U04a: a refused planning job stuck the project at "the planner is working on it" for
  ever, and a missing `tasks.json` printed "The plan is not usable yet (0 problems)"; 3 tests.
- `8c911c9` U04a: the New project page overwrote an existing project's record; 1 test.
- `63472dc` U04a: the plan page had no phone rules, so its Save/Approve bar hid under the
  upgrade 9a sticky header; 1 test.
- (docs) PLAN.md ticked + this section.

### Files changed in this run

| File | What changed |
| --- | --- |
| `web/planning.py` | `plan_finished(..., requeue=...)`; the "no tasks.json" sentence in `state_line`; a phone media block for the plan page's own `.pbar`; imports `phonelayout` for its width. |
| `web/jobboard.py` | `plan_hook(m, requeue)` (the one place the plan hook is called), now also on the two paths where the worker refuses a record before anything runs; `POST /project/new` refuses a folder that is already a project here. |
| `web/tests/test_u04a_plan.py` | 53 → **58** tests (all listed under "Verified"). |
| `PLAN.md` | job 07's steps ticked + the re-run findings. |
| `AGENT_REPORT.md` | this section. |

Nothing else was touched. `agentjobs_upgrades/plan.py` is used exactly as given (`validate`,
`order`, `SCHEMA`) — not reimplemented; `taskqueue` (4b), `decisions` (8), `phoneaccess`/
`phonelayout` (9a) and the `# JAIL:`, `# U01`, `# U05`, `# U07`, `# VOICE` and `# U10` blocks all
still work: the full suite runs them.

### How each requirement is met

1. **New project page** (`/project/new`): name, kind (Android app / Windows app / web app /
   other), the outline box, and the folder choice — an existing folder from `~/agent-work`, or
   "Start a new folder", which creates `~/agent-work/<name>` with `git init -b agent/<name>` and
   one starting commit, run through `SafeGit` inside that folder's own jail (`upgrades.make_wrap`
   → `jail/run-local.sh sandbox-run`). A folder whose git could not start is said on the page
   ("the agent will work without checkpoints there"), not raised.
2. **Make a plan** (`/project/edit/<folder>`, button `plan`) starts a **normal job** — same
   queue, same worker, same follow-up machinery — whose prompt comes from `prompts/planner.md`
   and opens "Do not write any app code, do not build anything and do not install anything". The
   record carries `plan_job` and `verify_off` (a plan is judged by its schema and by Jason, not
   by a build that does not exist yet). The planner writes `ROADMAP.md` and `tasks.json`; the
   schema those two files must satisfy is `docs/tasks-schema.json`, which a test keeps identical
   to `plan.SCHEMA`.
3. **Validation** is `agentjobs_upgrades/plan.py` (`validate` → fields, unknown fields, id
   charset, duplicate ids, unknown or self dependencies, `order()`'s cycles). An unusable plan
   goes back to the planner **once**, by itself: `plan_finished` queues a follow-up job (same
   folder, `parent` = the failed plan job, resuming its session) whose prompt carries the errors,
   and a second refusal stops with the errors on the timeline.
4. **Plan review page** (`/plan/<folder>`): milestones as `<details>` sections, one card per task
   with editable title / milestone / prompt / criteria / checks / size / screen, **Up** and
   **Down** to reorder, **Delete**, **Add task**, `after: t2` under each card, one **Save** and
   one **Approve plan**. Reordering, deleting and adding only change the page — **Save** writes
   `tasks.json`; approving copies the validated tasks into `state/plans/<folder>.json`, outside
   everything the sandbox mounts, so a later run follows what Jason approved. Nothing runs on
   approve: 4b's queue card then offers *Start the tasks*.
5. **`prompts/planner.md`** is the whole planner prompt, with `{{NAME}} {{KIND}} {{FOLDER}}
   {{OUTLINE}} {{ERRORS}}`, kept out of the code so it can be improved without a change to it.

### Verified

- Gate command, as in `AGENT_CHECKS.json`:
  `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore -m unittest discover -s tests`
  → **`Ran 790 tests in 281.322s` / `OK`**. Baseline at the start of this run: **785 tests, OK**
  (the committed U10 residue included). Nothing deleted, skipped or weakened; this run adds 5.
- This module alone: `python3 -W ignore -m unittest tests.test_u04a_plan` → **58 tests OK**.
  New since `5b3efad`:
  * `test_a_planner_that_wrote_no_file_is_said_plainly` — no "0 problems" wording.
  * `test_a_plan_job_that_never_ran_queues_nothing_behind_itself` — `requeue=False`: state
    becomes `invalid`, the timeline says the job never started, and no follow-up is queued.
  * `test_a_plan_job_the_worker_refused_leaves_nothing_waiting` — a real queued record refused
    by `check_record` (`plan_retry=99`), run through the real `process_job`: the job is `failed`
    with a "Refused:" note, the project is no longer `running`, its page no longer says the
    planner is working, and nothing was queued behind it.
  * `test_a_folder_that_is_already_a_project_is_not_started_again` — the old outline, name and
    `approved_at` survive an attempted second create in the same folder.
  * `test_the_plan_buttons_stay_reachable_on_a_phone` — `planning.CSS` carries a
    `@media (max-width:700px)` rule un-sticking `.pbar`, and the bar is repeated under the tasks.
- The 4a and 4b modules together: `python3 -W ignore -m unittest tests.test_u04a_plan
  tests.test_u04b_queue` → **128 tests OK** (approving a plan still hands a sound queue to 4b).
- Served the real board once on port **8091** with its own folders (`HOME`,
  `JOBBOARD_JOBS/WORK/PLANS/QUEUES/REPORTS` under `/tmp/u04a-smoke`, `JOBBOARD_JAIL=host`,
  `JOBBOARD_FAKE=1`) and clicked it with curl: **76 checks, 0 failed**, then stopped it and
  verified the port was closed and the log held no traceback. What those checks cover, in order:
  the guard (no cookie 403, the token link's 303 and its `jb=` cookie, a POST with no cookie
  403, unknown projects 404); the new-project page (fields and all four kinds, a name that
  cannot be a folder refused with what was typed kept, no outline refused, unknown kind refused);
  creating `notes-app` (folder made, branch `agent/notes-app`, one commit, README); the outline
  (empty refused, save 303, stored in the record, shown again); Make a plan (job queued with
  `plan_job`/`verify_off`, `plan_status=running`, the page says the planner is working, no
  *Review the plan* yet, the job waits behind a job running in another folder); the planner
  writing through the fake agent (`ROADMAP.md` + `tasks.json` in the folder); the plan page
  ("A draft to check: 3 tasks in 2 milestones", collapsible milestones, editable title/prompt/
  criteria cards, `after: t2`, up/down, delete, Add/Save/Approve, a task's `checks`, the screen
  note, the roadmap on the project page); reorder alone changes no file, Save writes the edited
  title; a circular `after` cannot be approved and the broken form writes nothing; a task with
  no title or criteria is not saved; Delete then Approve (record `approved`, the approved tasks
  in the record, 4b's `QUEUES/notes-app.json` on disk, **no job created**, "Approved - 2 tasks.
  Nothing runs until you start them.", `/projects` shows it); picking a folder that is already a
  project is refused.

### Try it by hand

1. `cd web && JOBBOARD_PORT=8123 python3 jobboard.py`, open the printed
   `http://127.0.0.1:8123/?t=…` link (never 8090, and never this sandbox).
2. **New project** → type a name, pick *What is it?* (Android / Windows / web / other), leave the
   folder on "Start a new folder", paste your outline in your own words → **Create project**.
   You land on the project page; `~/agent-work/<name>` exists with git on `agent/<name>`.
   (Picking a folder that already belongs to another project here is refused — that folder's
   plan is opened instead of being overwritten.)
3. **Make a plan**, pick the agent. The project page says "The planner is working on it." and the
   job looks like any other job; its own page has a **Plan** card. When it finishes: if it wrote
   no `tasks.json`, the page says so and tells you to press Make a plan again; if what it wrote
   was not a usable plan, the errors go back to the planner once, by itself, and the second
   refusal is reported instead of looping.
4. **Review the plan**. Milestones open and close; every task is a card you can rewrite (title,
   what to do, done-when lines, checks, size, screen note, `after:` ids). **Up**/**Down**/**Delete**/
   **Add task** move things on the page; press **Save** to put them in `tasks.json` — the page says
   so ("Moved t2 up. Press Save to keep it."). Fix anything the plan page complains about in red.
5. **Approve plan**. The record outside the sandbox now holds the approved tasks, the project
   page gains the 4b **Tasks** card, and nothing has started: "Approved - N tasks. Nothing runs
   until you start them."
6. Phone: open the same board on a phone with upgrade 9a's access on. The plan page is one column
   and the Save / Approve / Add bar scrolls with the page instead of hiding under the sticky
   header — the same bar is repeated under the last task.

### Unfinished / assumed

- **The planner itself was never run against the model here** — there is no agent and no model in
  this sandbox. The "a canned planner output through the fake agent" requirement is met by the
  `Run` class: a child process with `JOBBOARD_JAIL=host JOBBOARD_FAKE=1` that calls the real
  `process_job`, so the plan files, the record, the timeline line and the one automatic re-send
  are exercised for real, with the planner replaced by a scripted writer.
- On a `JOBBOARD_FAKE=1` copy, pressing **Make a plan** with mode "Fake agent (tests only)"
  produces a job whose text is the planner prompt, which the safety check refuses — fake mode
  needs a *scenario* as its job text, exactly as it does for every other fake job (4b's task jobs
  behave the same way). That is how this run proved the "a refused plan job leaves the project
  waiting for nothing" fix live. It was left as it is: no canned planner output is shipped in the
  board.
- **Two plan files, one source of truth for running.** The draft lives in the project folder
  (`tasks.json`, writable by anything running there); the approved copy lives in
  `state/plans/<folder>.json`. 4b runs the approved copy. Editing `tasks.json` after approving
  makes the page say so ("You approved an earlier version of this plan - approve again if this
  one is what you want"); it does not silently replace the approved plan.
- The automatic re-send is exactly one, as specified. A planner that writes nothing at all is not
  re-sent by itself (the plan page says to press Make a plan again) — an empty folder is more
  likely a stopped job than a malformed plan, and re-queueing a job nobody asked for was not
  wanted.
- Milestone sections are built from the order the tasks are written, so the same milestone name
  used in two distant places shows as two sections. Left alone: the planner writes them
  contiguous, and re-ordering is Jason's to do.
- A project record is one per folder. A folder can be planned again only after its project is
  gone from `state/plans/` (there is no "delete project" button — not asked for).
- No network, no installs, stdlib only. `~/agent-sandbox` untouched: every test and the 8091 run
  use their own `HOME` and state folders (`/tmp/u04a-smoke`, deleted after the run). Nothing was
  pushed; the branch `agent/jobboard-next` is ready for Jason to review, push and merge.

---

## Job 06 — Upgrade 5: best-of-N for hard steps (re-run, finished)

Branch: `agent/jobboard-next` (local only — **not pushed**; ready for Jason to review, push and
merge). PLAN.md holds this job's findings, its step list all ticked, and a "what this run found"
note.

This job had been started before. The feature itself — `web/attempts.py` (540 lines) plus the 16
`# U05` hooks in `jobboard.py`, the three settings keys in `upgrades.py` and 43 tests — is already
in `2903bb7`, `7520a60` and `54feb77` (the last of those was left uncommitted by a run that
stopped mid-way and was committed as found). That run never ticked its steps, never clicked the
pages on its own port and never wrote its report section. This run did not rebuild anything: it
clicked the whole feature end to end on 127.0.0.1:8091 with the fake agent, and fixed what the
clicking found.

Commits in this run:

- `52a35a9` U05 found by the 8091 click-through: **Stop did not stop the tries** (a hard job went
  on to attempt 3 of 3 after Jason had stopped it), and the card said "No more attempts: attempt 3
  passed cleanly" when the tries had simply run out and none passed; 1 test.
- (docs) PLAN.md ticked + this section.

### Files changed

| File | What changed |
| --- | --- |
| `web/attempts.py` | **new** (540 lines, `# U05`) — the plan (`attempts` 2-4, `hard`, `stop_early`), the new-job fields and their validation, attempt folder/branch names (`<folder>-try<k>-<id4>`, `agent/<id>-try<k>`), the phase tag, the git plumbing (`prepare` clone + branch, `collect` leftover commit + bundle, `bring_home`, `apply_winner`, `prune_old`, `changes`), the scores (`status_of`, `tests_passed`, `attempt_of`), the run (`run_best_of_n`), the buttons (`use_attempt`, `retry_extra`) and the Attempts card. This run added `stop_pressed()` and the accurate end-of-loop timeline line. |
| `web/jobboard.py` | 16 `# U05` hooks, all small: `check_record` validates `attempts`/`hard`/`stop_early`; `run_planned` (one run, or N attempts); `JobCtx` puts an attempt's check logs under `JOBS/<id>/attempts/try-k/`; `run_agent_job(m, name, fresh_log=)`; `phase_tag` in the gate's phases; `folders()` hides try folders; `maybe_auto_attempts`; the card on the job page and the fields on the new-job form; POST `/use-attempt/<id>` and `/try-hard/<id>`. |
| `web/upgrades.py` | `DEFAULTS` gained `attempts: 3`, `stop_early: True`, `auto_attempts: False`, and `load_settings` now keeps on/off settings (`test_settings_keep_the_new_keys`). |
| `web/tests/test_u05_bestofn.py` | 43 → **44** tests (all listed under "Verified"). |
| `PLAN.md`, `AGENT_REPORT.md` | this job's steps ticked + the re-run findings; this section. |

Nothing else was touched. The ranking is `agentjobs_upgrades/bestofn.py` used exactly as given
(`Attempt`, `pick_winner`, `should_continue`, `why`) — not reimplemented; `guard.is_test_file` and
`safegit.SafeGit` are reused. `decisions` (8), `taskqueue` (4b), `dashboard`/`badges` (7a/7b),
`phoneaccess`/`phonelayout` (9a), `morningreport` (10) and the `# JAIL:`, `# U01`, `# U05`,
`# U07` and `# VOICE` blocks all still work: the full suite runs them.

### How each requirement is met

1. **"Hard: try 3 times" (N 2-4, default 3) + the fresh-attempts button.** The new-job form has a
   `Hard job: try it 3 times` checkbox and a `Times` box (`min=2 max=4`, prefilled from the board
   setting); `attempts.form_extra` refuses anything else — the page answers "A hard job is tried 2
   to 4 times." (400), and `check_record` refuses a record whose `attempts` is not 0-4 / not an
   int / a bool, so a forged record never reaches the worker. A job that ended **Failed after fix
   rounds** (`verify.status == failed`, no attempts of its own) gets a **Try 3 fresh attempts**
   button (`POST /try-hard/<id>`), which starts the same prompt again as a **new** job with
   `attempts=N, hard=False, parent=<id>` so the failed one stays reviewable. `auto_attempts: true`
   in `state/agentjobs-settings.json` does the same press by itself (`maybe_auto_attempts`, off by
   default, never for a bench run, a job that already tried, a `verify_off` job or a
   non-git folder).
2. **Each attempt starts from the same base commit, in its own copy, one after another.** The base
   is `git_start` — the sha `record_git_start` took before the agent ran. `prepare()` clones that
   commit into `~/agent-work/<folder>-try<k>-<id4>` on branch `agent/<id>-try<k>`; the loop runs
   one attempt at a time in the worker (one GPU) through the ordinary `run_agent_job` — same
   checks (`AGENT_CHECKS.json`, frozen before the job by `freeze_checks`), same fix rounds, same
   watchdog, same checkpoints — with `resume=None` so no try ever carries on another try's
   conversation, and its check logs under `JOBS/<id>/attempts/try-k/checks/round-N`. **Why not a
   git worktree** (the spec's word): the jail mounts exactly one folder (`podman-local.sh`:
   `-v <dir>:/work/<name> -w /work/<name>`) and `agent --in` only accepts a top-level
   `~/agent-work` name, and a worktree's git dir stays in the main repo — invisible in the
   container, so git inside the sandbox would break. A full clone gives the same isolation
   (an attempt can only ever see its own copy: `test_attempts_never_see_each_other`). Only two git
   steps run on the host, because they need two folders at once — `clone --no-checkout` (objects
   and refs only, so no agent-planted smudge filter or hook can run there) and `fetch <bundle>` —
   both through `SafeGit` (`-c core.hooksPath=/dev/null`, `core.fsmonitor=false`,
   `GIT_CONFIG_NOSYSTEM=1`, `protocol.allow=never`, opening only the file transport); a bundle
   carries no repo config, so the project repo never reads an attempt repo's settings. Every step
   that writes a working tree (`checkout -B` in the attempt, `apply_winner` in the project folder)
   runs inside that folder's own jail. Try folders are hidden from the folder picker
   (`attempts.is_attempt_folder`), and `prune_old` clears attempt branches older than 7 days.
3. **The winner is chosen automatically, and every try's scores are kept.** Each finished attempt
   becomes a `bestofn.Attempt` — status from its checks (`verify.status`, or how the run itself
   ended), tests passing summed from `verify.final.results`, test files changed and diff size from
   `git diff --numstat` (`guard.is_test_file` for the test files), seconds from the clock — then
   `bestofn.pick_winner` in the spec's order: a clean pass with no needs-review flag, most tests
   passing, fewest test files changed, smallest diff, fastest (`why` says which one decided it, in
   plain words: "Attempt 3 won: a smaller change."). Every attempt's row (`k`, status, tests,
   files with +/-, diff lines, minutes, branch, commit, session, watchdog/checkpoint counts, the
   checks banner) is stored in `meta["attempt_log"]`, ranked in `attempts_final`, shown on the
   card and written to the timeline.
4. **Winner on the job branch, losers for 7 days, "Use this one instead".** `apply_winner` checks
   the winner out as `agent/<job id>` **in the project folder**, so the Changes card, the review
   bundle, Push and upgrade 8's merge all work exactly as before; the record's `verify`, `session`
   and check badges are the winner's. A loser is never checked out, and if nothing passed cleanly
   **nothing** goes in the folder — the note says so ("Nothing was put in the folder: no attempt
   passed its checks. Each try is still on its own branch - pick one with 'Use this one
   instead'."). The Attempts card on the job page (the board has no tabs) lists every try with its
   result, tests, changed files and branch; every non-kept row has **Use this one instead**, which
   re-points `agent/<job id>` at that try's branch (only for a finished job whose folder is idle
   and whose record passes `check_record`), says so on the card and timeline, and moves the
   "(kept)" marker. A branch that already went past 7 days answers "Branch … is gone - attempts
   are kept for 7 days."
5. **The cost and the progress are on the page.** Before starting: "A hard job is tried 3 times in
   a fresh copy of the project, one after another (about 3× longer)…" on the form, and the same
   "(about N× longer)" next to the fresh-attempts button. While it runs: the record's phase is
   `Attempt 2 of 3 - Running checks` / `… - Fix round 1 of 2` (the gate rewrites the phase, so the
   tag is put back by `attempts.phase_tag`), so the banner's action line and the job list both say
   which try is running; the card's own line says **Attempt 2 of 3** with the cost note; the
   timeline has `Attempt 2 of 3 started` and `Attempt 2 of 3: passed - 2 tests passing`.
6. **Stop early (default on).** `bestofn.should_continue(done, n, hard, stop_early)`: a job that
   was *not* marked hard stops as soon as attempt 1 passes cleanly (`stop_early` from the record,
   else the board setting, default on) — the timeline says "No more attempts: attempt 1 passed
   cleanly" and no further copy is ever made. A `hard` job always runs all N. A safety stop stops
   the rest at once; `len(log) >= n` ends with "All N attempts tried".
7. **Tests with the fake agent, a different scenario per attempt**: in fake mode the job text may
   be `{"attempts": [scenario1, scenario2, …]}` and `attempt_job` hands each try its own
   (`Jobs` runs real jobs in a child process with `JOBBOARD_JAIL=host JOBBOARD_FAKE=1`): every
   tie-break, early stop, every attempt failing, "Use this one instead", Stop, auto_attempts, and
   the buttons/routes.

### Verified

- Gate command, exactly as in `AGENT_CHECKS.json`:
  `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore -m unittest discover -s tests`
  → **`Ran 791 tests in 284.523s` / `OK`**. Baseline when this run started: **790 tests, OK**.
  Nothing deleted, skipped or weakened; this run adds 1.
- This module alone: `cd web && python3 -W ignore -m unittest tests.test_u05_bestofn` →
  **44 tests OK** (43 before this run). `Pieces` 18 (names, folder-picker, settings, plan, phase
  tag, record validation, form fields, retry fields, per-attempt scenarios, `status_of`,
  `tests_passed`, `attempt_of`, **every tie-break in order**, stop-early rules, `prune_old` with a
  fake clock, the card's three states) · `Git` 7 (own repo/branch, tries never see each other,
  leftovers committed and packed into a bundle, winner onto `agent/<id>`, a dirty project folder
  reported not bulldozed, a do-nothing try still gets its branch, per-file sizes) · `Jobs` 9 (hard
  job keeps the best, early stop, all fail, use-this-one-instead, a missing branch, auto_attempts,
  the button without the setting, a non-git folder runs once, each try its own conversation) ·
  `Run` 4 (a try that cannot start, Stop between tries, **a try that came back "stopped" does not
  start the next one** — this run's fix, a branch that will not come home) · `Pages` 6 (the form,
  the card, both POST routes, a try that is not there, a bad N).
- Click-through on a real board at **127.0.0.1:8091** (never 8090; 8080 never touched): a
  throwaway driver (`/tmp/u05_smoke.py`, not part of the repo) starts `web/jobboard.py` as a child
  with its own temp `HOME` and state folders, `JOBBOARD_FAKE=1`, `JOBBOARD_JAIL=host`, in a temp
  git project (`AGENT_CHECKS.json` runs unittest), then presses the real form and buttons over
  HTTP with the `jb=` cookie. **55 checks, all passing.** Key output:

```
[ok ] progress shown while running :: ['Attempt 1 of 3', 'Attempt 2 of 3', 'Attempt 3 of 3']
[ok ] scores recorded :: [('failed', 1, 0), ('passed', 2, 13), ('passed', 2, 2)]
[ok ] winner is the smallest clean try :: Attempt 3 won: a smaller change.
[ok ] winner on the job branch in the folder / every try kept on its own branch
        ['agent/…-c646', 'agent/…-c646-try1', '…-try2', '…-try3']
[ok ] tries' folders are gone :: ['proj']     each try's checks kept apart     one conversation per try
[ok ] card: cost note while a try runs :: <p><b>Attempt 3 of 3</b> … (about 3&times; longer)
[ok ] use this one instead: switch / branch is the job branch / (kept) moved to try 1
[ok ] all tries fail: job failed, folder left alone, both branches stay, "Nothing was put in the folder…"
[ok ] button starts a fresh best-of-N job :: (3, False, '20261111-212618-eb06')
[ok ] stop early: one clean try ends it :: [1]   · only one try folder used
[ok ] auto_attempts queues a fresh run :: ['20261111-212710-c16b']  · the automatic job is a 2-try job
[ok ] Stop between tries ends the job :: ('stopped', 2)  · the tries after it never start
[ok ] Stop: Stopped before all attempts finished. · 'Stopped after attempt 2 of 3: no more tries'
[ok ] folder picker never lists try folders · Changes card still works for a winner · board log clean
[ok ] board stopped, port 8091 closed
```

  The **first** pass through that click-through is what found the bug: the Stop job ended
  `('failed', 3)` — all three tries had run after Stop — and the timeline carried
  `Attempt 3 of 3 started` after `Attempt 2 of 3: stopped`. After `52a35a9` the same script ends
  `('stopped', 2)` with the explanation on the timeline (55/55; two of the earlier failures were
  the driver's own wrong expectations — the first job really does fix the project, so the later
  "failing" scenarios had to reset it, and the base repo already had one passing test).

### Try it by hand, click by click

1. `cd web && JOBBOARD_PORT=8091 python3 jobboard.py` (own state folders if you want them apart),
   open the printed `http://127.0.0.1:8091/?t=…` link. Pressing **Stop** is the same for a hard
   job as for any other.
2. Home page: tick **Hard job: try it 3 times**, set **Times** (2-4), pick the folder, describe the
   job. The words under the box say what it costs: a fresh copy each time, one after another,
   about 3× longer, best try kept, branches for 7 days. A hard job on a **non-git** folder says so
   and just runs once.
3. While it runs the banner action line reads `Attempt 2 of 3 - Running checks`, the job list says
   the same, and the **Attempts** card shows **Attempt 2 of 3** with the cost note. Stop now and
   the rest of the tries never start.
4. When it ends the card has one row per try: result (checks failed / passed / needs review /
   safety stop / stopped), tests passing and test files touched, `N files, M lines` (opens to the
   file list with +/-), minutes, its branch, and the kept one marked **(kept)**. Above the table
   one line says who won and why, in words.
5. **Use this one instead** on another row: confirm, and the folder switches to that try's work —
   `git -C ~/agent-work/<folder> rev-parse --abbrev-ref HEAD` stays `agent/<job id>`, now at that
   try's commit; the Changes card, the review bundle and the Decide buttons follow it. Press it on
   a try that is not in the list and the card just says so.
6. A job that ended **Failed after fix rounds** shows **Try 3 fresh attempts**; pressing it starts
   a new job (a follow-up of the first) that is best-of-3 and not marked hard, so a clean first
   try stops the rest. With `auto_attempts: true` in `state/agentjobs-settings.json` that press
   happens by itself, and the first job's timeline records it.
7. In the project folder: `git branch --list 'agent/*'` shows `agent/<job id>` (the kept try) plus
   `agent/<job id>-try1…k`. Old jobs' `-try` branches are deleted once they are 7 days past, the
   next time a best-of-N job runs in that folder.

### Unfinished / assumed

- **Nothing of upgrade 5 is unfinished** — all six requirements are implemented and tested, and
  the feature was clicked end to end on 8091.
- Two deliberate departures from the spec's wording, both forced by the jail and recorded in
  `attempts.py`'s docstring: a try is **a full clone of the base commit in its own `~/agent-work`
  folder**, not a git worktree (reasoning above), and the attempts live on an **Attempts card on
  the job page**, not a separate tab — this board has no tabs; every upgrade adds a card there.
- **The podman path was not exercised here** — this sandbox has no `podman` and no `bwrap`, so
  every test and the 8091 copy run with `JOBBOARD_JAIL=host`. On the live board the same calls go
  through `jail/run-local.sh`: the attempt's `checkout -B`, its checks and the winner's
  `apply_winner` run inside the jail; the host only does `clone --no-checkout` and
  `fetch <bundle>`, both with `protocol.allow=never` opened for the file transport only. The
  first real hard job is worth watching once for two things: that `agent --in <folder>-try1-<id4>`
  is accepted (it is a plain top-level `~/agent-work` name, ≤ 64 chars, matching `FOLDER_RE`) and
  that the bundle fetch lands (if it ever fails, the note on the card says which try and where its
  copy is left).
- **No real agent and no model here**: every attempt in the tests and the 8091 run is the fake
  agent, so "the usual checks, fix rounds and watchdog" are proven wired, not proven against a
  thinking agent. The ranking numbers come from `verify` and from git, so a real agent only
  changes the values.
- Board-level `attempts` / `stop_early` / `auto_attempts` have **no settings page** — like
  `fix_rounds` and the watchdog thresholds, they live in `state/agentjobs-settings.json` (written
  with the defaults on first start). 4b's project page has its own "Try big tasks (and ones that
  failed before) 3 times" tick, which is how a queue task becomes best-of-N (with `hard=False`, so
  stop-early applies).
- The 7-day prune runs when the next best-of-N job starts in that folder, not on a timer — an old
  `-try` branch of a folder that never runs again stays until some later hard job clears it. A
  try whose branch could not be brought home keeps its folder in `~/agent-work` on purpose, and the
  card names the folder; nothing deletes a folder it did not make.
- A finished best-of-N job has **no checkpoint list of its own**: checkpoints were taken in the
  try copies, which are gone, so the record clears `checkpoints` and the Checkpoints card stays
  empty for it. Upgrade 8 picks the branch by `agent/<id>` for such a job (tested in
  `test_u08_decide.py`).
- "about 3× longer" is a plain-language warning on the form, not a measured estimate; a real
  estimate per task would want the task's own history (4b's queue already multiplies its minutes by
  the number of tries).
- No network, no installs, stdlib only. `~/agent-sandbox` untouched — the tests and the 8091 copy
  use their own `HOME`, and the throwaway state folders under `/tmp/u05-smoke-*` were left there
  for inspection only. Nothing was pushed; the branch `agent/jobboard-next` is ready for Jason to
  review, push and merge.

---

## Fix job A — review fixes for upgrades 6b, 6a, 4b and 7b

Branch: `agent/jobboard-next` (local only — **not pushed**; ready for Jason to review, push and
merge). Four findings from the review, one commit each, plus this section and PLAN.md. No feature
work: each finding is a small local fix plus tests.

Commits in this run:

- `0d37a77` **6b (a)** — `adb shell` is a shell on the phone: a check's `text` step could run
  anything on the device. 5 tests.
- `c62cc14` **6b (b)** — installing an APK is opt-in (`"install": true` **and** a named device
  serial). 5 tests.
- `013744f` **6a (a)+(b)** — the sandbox's screenshot data is read with hard limits, and a missing
  Playwright/Chromium is a skip, never a failed job. 11 tests.
- `1bc9b63` **4b** — a task job of a plan queue is created with `offline` on. 1 test.
- `ed96686` **7b** — `bad_path` refused every path merely starting `.git`; only the `.git` folder
  is refused now. 1 test.
- (docs) PLAN.md section for this job + this section.

### Files changed

| File | What changed |
| --- | --- |
| `web/agentjobs_upgrades/android.py` | `SHELL_META` / `SHELL_META_RE` and the guard in `Adb.shell` (the one place every `shell` word passes; a word holding a metacharacter is refused with exit 126 and a note, and never reaches adb); `TEXT_RE` (letters, numbers and plain punctuation, ≤ 200) for a `text` step; `package_list()` keeps only names matching `PKG_RE`, so a package name the device invents cannot be fed back into the shell; `$` removed from `COMP_RE` (the component pattern). |
| `web/androidcheck.py` | `check_ok` refuses a `text` step `TEXT_RE` rejects (so it never gets frozen into a job record); `install` is an entry key, must be a bool, and is frozen; `install_opt_in(check, cfg)`, `NO_INSTALL`, `NO_SERIAL`; both `run_check` and `run_smoke` gate on it **before** `pick_device`, so an opted-out check sends not one adb command; `_skipped()` sets the `word` the badge and the Checks card show. |
| `web/visioncheck.py` | `MAX_OUT`/`MAX_PNG` = 10 MB, `MAX_PIXELS` = 8 M, `MAX_RAW` = 48 MB; `default_run` (Popen + two reader threads) keeps at most 10 MB of the sandbox run's stdout/stderr, drains the rest and appends a note; `_inflate()` uses `zlib.decompressobj().decompress(..., max_length)` and returns `None` past the limit; `_png_decode` refuses an over-cap stream, an absurd `width × height`, and a header whose raw scanlines would be too big; `capture()` drops an over-cap shot with a note and sets `no_browser`; `run_check` turns a missing browser into a `skipped` verdict forced to `advisory` with the exact sentence the review asked for, and puts `word: "no browser"` in the result. |
| `web/visionshot.py`, `web/visionshot.mjs` | Both drivers say "no browser" themselves instead of dying: the Python one sets `no_browser` on the `ImportError`; the Node one imports Playwright **dynamically** (a static import killed the process before it could answer, so the board only saw "the screenshot program did not answer"). |
| `web/badges.py`, `web/upgrades.py` | A device/vision check that was skipped shows why (`install not asked for` / `no device named` / `no device` / `no adb` / `no browser`) instead of always "no device". |
| `web/taskqueue.py` | `start_next` creates the task job with `offline=True` whatever agent the plan uses. |
| `web/diffview.py` | `bad_path` refuses a `.git` path **segment** (`.git`, `.git/config`, `src/.git/hooks/…`) instead of anything starting `.git`. |
| `web/tests/test_u06b_android.py` | 41 → **51** tests. |
| `web/tests/test_u06a_vision.py` | 42 → **53** tests. |
| `web/tests/test_u04b_queue.py` | 72 → **73** tests. |
| `web/tests/test_u07b_changes.py` | 36 → **37** tests. |
| `PLAN.md`, `AGENT_REPORT.md` | this job's findings, steps ticked, and this section. |

`FILE_SCRIPT`'s shell-side path checks (`.git|.git/*`, `/*`, `../*`, `*/..`, `*/../*`) were
already right, so they are unchanged — the new `bad_path` matches them. `# JAIL:`, `# U01`,
`# U05`, `# U07` and `# VOICE` blocks are untouched and still run in the suite.

### How each finding is met

1. **(6b a) No check step can run a command on the phone.** adb joins the words of `adb shell …`
   with spaces and `/system/bin/sh` on the device runs the result, so the injection surface is the
   joined string, not the argv list. Every word going through `Adb.shell` is now checked at that
   one choke point against `SHELL_META` (`; | & $ ` ( ) < > \ " ' newline/tab, `* ? ~ # ! { } [ ]`)
   and refused — return code 126, an error the step records, and **nothing sent to adb**, so a
   forged or hand-edited check cannot get past it even if it skipped validation. On top of that a
   `text` step is an allowlist, `TEXT_RE`: only letters, numbers and plain punctuation
   (` ,.:/=+@^_-`), up to 200 of them, spaces typed as `%s`; `androidcheck.check_ok` refuses a text
   step that fails it, so such a step never enters a job record, and the page's own step validation
   explains the rule. Values that come back **from** the device are treated the same way:
   `package_list()` keeps only names matching `PKG_RE` before one is used in a later `shell`
   command, and the launchable-component pattern no longer accepts `$`. `adb install` and
   `exec-out` are client-side (never parsed by a shell), so they are unaffected.
2. **(6b b) Installing is opt-in.** `install_opt_in()` needs both switches: the check says
   `"install": true` (validated as a bool, frozen into the record) **and** a serial is named — the
   check's own `"device"` or the board's `android_device` setting. Otherwise the check is reported
   as skipped, before any adb call, with the message naming both (`Nothing was put on a device: an
   Android check only installs when it says "install": true and names a device …`) and, when only
   the serial is missing, `The check says "install": true but names no device, so nothing was put
   on a phone: put "device": "emulator-5554" in the check or choose the phone in the board's
   settings (android_device)`. The `app runs` check an Android project gets with no
   `AGENT_CHECKS.json` has neither, so a plugged-in phone is never touched by itself; that skip is
   advisory and the badge/Checks card say `install not asked for`.
3. **(6a a) Nothing trusted comes out of the sandbox without a limit.** The run's stdout and
   stderr are each read by their own thread into at most 10 MB (`MAX_OUT`); the rest is drained so
   the child cannot block, and a note says it was dropped. A screenshot over 10 MB is left out of
   the check with a note, and so is one whose decode would need more than `MAX_PNG` compressed
   bytes or `MAX_PIXELS`/`MAX_RAW` raw bytes (a header claiming 60000×60000 stops there). The
   inflate itself is bounded: `zlib.decompressobj().decompress(chunk, max_length)` in a loop over
   `unconsumed_tail`, giving up past the limit rather than inflating a small file into gigabytes.
   Every one of these is a note in the check output ("… could not be resized", "… is more than
   10 MB - left out"), never a traceback, and the check still finishes.
4. **(6a b) A missing browser is a skip.** `run_check` forces `mode = "advisory"` and returns
   `verdict: skipped` with `Playwright isn't installed in the sandbox image; vision check skipped`
   first in `issues`, plus the driver's own how-to-fix line when it gave one. It is recognised from
   the driver's `no_browser` flag (both drivers set it) or from the error text
   (`NO_BROWSER_MARKS`), and `summarize()` then leaves the job `unverified` rather than `failed` —
   asserted for a check declared `mode: required`. The Node driver's import is now dynamic, which
   is what lets it answer at all in an image without Playwright.
5. **(4b) Plan tasks stay offline.** `start_next` passes `True` for `offline`, because
   `jobboard.new_job` only forces the flag for `jailcfg.LOCAL_MODES`; the agent Jason chose for the
   plan is unchanged, and the record still validates with `jobboard.check_record`.
6. **(7b) Only the `.git` folder is a bad path.** `bad_path` tests the path **segments**, so
   `.gitignore`, `.gitattributes`, `.github/workflows/ci.yml`, `.gitlab-ci.yml` and `src/.gitkeep`
   are listed, shown and diffed like any other file, while `.git`, `.git/`, `.git/config`,
   `.git/refs/heads/main`, `src/.git/hooks/pre-commit`, `.git//config` and `src/../.git/config`
   stay refused. The `FILE_SCRIPT` shell cases already did exactly this, so the two sides now
   agree.

### Verified

- Gate command, exactly as in `AGENT_CHECKS.json`:
  `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore -m unittest discover -s tests`
  → **`Ran 814 tests in 294.996s` / `OK`** (4 m 55 s). Before this job: **791 tests** (counted the
  same way at `3888bb0`), so this job adds **23** and deletes, skips or weakens nothing.
- The four touched modules alone, each run separately (running several web page modules in one
  process is flaky in this repo — shared ports and threads — so the gate command is the authority):
  `tests.test_u06b_android` **51 OK** · `tests.test_u06a_vision` **53 OK** ·
  `tests.test_u04b_queue` **73 OK** · `tests.test_u07b_changes` **37 OK**.
- New tests, by finding:
  - *adb shell*: `test_a_text_step_is_only_ever_typed`,
    `test_text_that_the_phone_would_run_is_refused`,
    `test_the_shell_guard_never_sends_a_dangerous_word_to_adb`,
    `test_a_package_name_the_device_invents_never_goes_back_into_the_shell`,
    `test_a_dangerous_text_step_is_never_sent_to_the_device` (the last three read the fake adb's
    argv log and assert the dangerous command is **not in it**), plus two junk cases added to the
    existing record tests.
  - *install opt-in*: `test_nothing_is_installed_unless_the_check_asks_for_it`,
    `test_install_true_without_a_named_device_installs_nothing_either`,
    `test_the_default_app_runs_check_never_installs_by_itself`,
    `test_an_opted_in_check_named_only_in_the_settings_is_allowed`,
    `test_the_screen_check_installs_nothing_unless_the_check_asks`.
  - *vision limits*: `Limits` class — `test_the_sandbox_output_is_read_with_a_limit`,
    `test_a_normal_run_of_the_driver_is_read_as_before`, `test_an_absurd_picture_size_is_refused`,
    `test_a_png_that_lies_about_its_size_stops_at_the_limit`,
    `test_undecodable_or_truncated_pixel_data_is_still_just_a_note`,
    `test_the_limits_are_the_ones_the_review_asked_for`; and
    `test_a_shot_bigger_than_the_limit_is_left_out_by_capture` / `…_by_the_check`.
  - *missing browser*: `test_no_browser_check_is_a_skip_and_says_why`,
    `test_a_required_vision_check_is_never_failed_by_a_missing_browser`,
    `test_a_real_run_with_no_browser_in_the_image_skips`,
    `test_capture_marks_a_missing_browser_as_such`, and `test_drivers_are_valid` now checks both
    drivers say `no_browser` (and still runs `node --check` on the `.mjs`).
  - *offline*: `test_a_task_job_is_offline_even_for_a_plan_that_uses_an_online_agent`, plus an
    `assertIs(m["offline"], True)` in the existing start test.
  - *.git paths*: `test_only_the_dot_git_folder_is_a_bad_path_not_every_dot_git_name`.
- **Existing tests that were changed, none of them weakened** - each change is the behaviour the
  review asked for: `test_no_browser_check_is_not_sure_and_says_why` →
  `test_no_browser_check_is_a_skip_and_says_why` (a missing browser must be a skip carrying that
  message, not "not sure"); `test_drivers_are_valid` now expects the dynamic
  `await import("playwright")` and the `no_browser` answer in both drivers (it still runs
  `node --check`); the android smoke tests now declare `"install": true` and a device, because the
  checks they exercise are opted in - their assertions are unchanged; the two no-device tests now
  assert `Device emulator-5554 is not connected (adb devices shows: no device)`, since a named
  serial is now required before anything installs. No test was deleted, skipped or loosened.

### Unfinished / uncertain

- Nothing of the job is unfinished: all four findings are fixed, tested and committed, and the gate
  command passes.
- **Not verifiable in this sandbox, and how it is covered instead:** there is no phone and no real
  adb, so the Android findings are proven against `tests/fixtures/fakeadb/adb`, which records every
  argv it is given — "never sent to adb" is read back from that log; the quoting/allowlist rules
  are the same ones a real device's `/system/bin/sh` would see. Playwright and Chromium are not
  installed here, so the skip is exercised through the drivers' own `no_browser` answers and the
  error-text marks, not through a real Chromium launch; a sandbox image that *does* have them still
  goes down the old path (unchanged). There is no podman/bwrap here, so all of this ran in the host
  jail (`JOBBOARD_JAIL=host`), as in the earlier jobs.
- This job made no network calls at all — no board was served on 8091 (the fixes are library-level;
  the badge and Checks-card wording is asserted in-process by the tests above). `~/agent-sandbox`
  untouched, nothing installed, port 8090 never contacted. Nothing was pushed; the branch
  `agent/jobboard-next` is ready for Jason to review, push and merge.


---

## Fix job B — merge the live voice code (VOICE) into this copy

Branch: `agent/jobboard-next` (local only — **not pushed**; ready for Jason to review, push and
merge). This copy was branched off the live board **before voice chat existed**, so it had no voice
code at all: swapping this copy onto the live board would have deleted the microphone, the spoken
status and the hands-free commands. The job was to bring voice in here, keeping every line this
copy already has. Nothing was swapped into the live board and `~/agent-sandbox` was not touched.

Commits in this run:

- `47f7980` the merge itself (4 files copied in + the nine hunks in `web/jobboard.py` + the new
  merge test). 16 tests.
- (docs) PLAN.md section for this job + this section, and the `.live-voice-ref/` snapshot deleted.

### Files changed

| File | What changed |
| --- | --- |
| `web/voice_core.py` | new, **byte-identical** to the live file (md5 `cbf17522…`): `WhisperWorker` (long-lived CPU-only child, unloaded after 10 idle minutes), `transcribe_bytes`, `speak_to_file` (piper + a 50-file WAV cache), `clean_markdown`, `spoken_summary`, `status_speech`, `parse_command`. |
| `web/voice_worker.py` | new, byte-identical (`c866e651…`): the faster-whisper child (int8, CPU, `initial_prompt` biased to Agent Jobs words), JSON lines over stdin/stdout. |
| `web/static/voice.js` | new, byte-identical (`637982c6…`): mic buttons on the two textareas, read-aloud on the report, hands-free "Agent, …" commands; `window.AgentVoice`. |
| `web/tests/test_jobvoice.py` | new, byte-identical (`f72696e8…`): **11 tests** — a real read-only board on a free port, the six routes, the page hooks, login/origin guard, size limits, the jobstatus → voice status mapping. 2 of them use the real whisper/piper models and skip where they are not installed, exactly as on the live board. |
| `web/jobboard.py` | the nine hunks of `live-changes.patch`, by hand: `import voice_core`; `<script src="/static/voice.js">` in `PAGE`; `data-voice="new-job"`, `data-voice="follow-up" data-job="…"` and `<pre data-voice-read>`; the voice helper block (`VOICE_JS`, `VOICE_AUDIO_MAX`, `VOICE_STATE`, `voice_latest`, `voice_report_md`, `voice_status`, `voice_report`); `H.voice_post`; the `GET /static/voice.js` + `/api/voice/status` + `/api/voice/report` routes; the `POST /api/transcribe` / `/api/speak` / `/api/voice/command` routes. +86 lines, 4 lines rewritten (the only ones the patch rewrites). |
| `web/tests/test_jobvoice_merge.py` | new, **5 tests**, pinning the merge from fixed lists (see below). |
| `PLAN.md`, `AGENT_REPORT.md` | this job's findings, steps ticked, and this section. |

### How the merge was done, and how it was checked

The patch does not apply to this file (it grew from 1767 to 2537 lines since the branch point:
U07/U07b cards, U09a login and layout, U04/U05/U06 blocks…), so each hunk was placed by hand in the
same position relative to its neighbours as in the live file:

- `voice_core` is imported where the live file imports it (after `reviewbundle`, before `upgrades`).
- The voice helper block sits immediately before `class H`; `voice_post` immediately after `send`
  (above U07's `sse_job`, which is where live has `redirect`).
- `do_GET`: the voice routes go **after** `self.guard()` (a phone that has not logged in still gets
  403, asserted) and **below** this copy's pre-login manifest/icon routes, which stay.
- `do_POST`: the `/login` route stays above the guard; the voice routes come after
  `guard(post=True)` and **before** the `READONLY` refusal — that is where live puts them, so the
  speech routes keep working on a read-only board.
- `PAGE` keeps this copy's `@@PHONE@@` (U09a phone CSS) and gains the voice script tag:
  `</style>@@PHONE@@</head>…@@BODY@@</div><script src="/static/voice.js"></script></body>`.

Checked three ways: (1) every line the patch adds is present here — 82 added lines, 81 of them word
for word and exactly one deliberately different, the `PAGE` line, which here also holds
`@@PHONE@@`; (2) every line containing
`VOICE` in the live `jobboard.py` (13 of them) is here word for word; (3) `git diff` shows only
additions plus those 4 rewritten lines, i.e. no code of this copy was dropped. `web/jobboard.py` was
never replaced with the live file.

`web/tests/test_jobvoice_merge.py` keeps that guarantee after the snapshot is gone: it compares the
source of `jobboard.py` against **fixed lists in the test file** (all 13 `VOICE` lines, the helpers,
hooks and route bodies, the four voice files by name and size) and against one marker line from
every block that only this copy has (JAIL, U01, U04a, U04b, U05, U06, U06b, U07, U07b, U08, U09a,
U10). It does not read `.live-voice-ref/`, which this run deleted at the end (it was never tracked,
so a plain delete, not `git rm`). All 13 voice lines are absent from the commit before `47f7980`, so
the test fails if voice is ever lost again — it is not vacuous.

### Verification

```
cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore -m unittest discover -s tests
Ran 830 tests in 291.975s
OK (skipped=2)
```

**830 tests, up from 814** before this job: +11 (`test_jobvoice.py`) and +5 (`test_jobvoice_merge.py`).
Nothing was deleted, skipped or weakened; no existing test was edited. The only skips are
`test_speak_real_piper` and `test_transcribe_real_whisper`, which `test_jobvoice.py` skips itself
when the models are not installed — copied verbatim. `python3 -m py_compile jobboard.py
voice_core.py voice_worker.py` is clean, and `/static/voice.js` passes `node --check` (done by
`test_static_voice_js`).

### Unfinished / uncertain here

- No whisper or piper install and no microphone in this sandbox (`~/agent-sandbox/voice` does not
  exist and nothing can be downloaded), so transcription and talk-back were only exercised as far
  as their error paths ("Voice is not installed…", empty recording, 413 over 25 MB). The routes,
  limits, guards and status mapping are covered by the 11 HTTP tests.
- `static/voice.js` is copied verbatim and syntax-checked, but its browser behaviour (MediaRecorder,
  wake-word listening, autoplay) cannot be run here — there is no browser and no Playwright.
- No live board was touched: this only makes the swap safe in one direction (this copy keeps voice).
  The actual swap and release stay with Jason.
- No build output: this is the web app (no APK). Run it with `python3 web/jobboard.py`.
