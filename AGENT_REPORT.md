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
