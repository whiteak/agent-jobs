# Job 09 — Upgrade 10: the morning report

## Findings

- Server + pages: `web/jobboard.py` (2211 lines). `main()` starts `worker()` (the
  one-at-a-time job loop: `queue_cv.wait(5)` → `process_job(name)`) plus
  `ThreadingHTTPServer` on `127.0.0.1:PORT`; routes are the `if` chain at the top of
  `H.do_GET`/`H.do_POST` behind `H.guard()` (`jb=` cookie, Host must match `jobboard.PORT`).
  `render()` wraps a page, `header()` is the top bar (Projects / New project / Bench / Phone —
  that is where a Reports tab goes), `esc()` escapes.
- Records, all outside anything the sandbox mounts (`state/…`, env-overridable): `JOBS/<id>/meta.json`
  (+ `events.jsonl`, `timeline.jsonl`, `review.md`, `checks/`), `PLANS/<folder>.json`,
  `QUEUES/<folder>.json`, `BENCH_RUNS/<id>/spec|samples|progress|env.json`, settings
  `agentjobs-settings.json` (`upgrades.load_settings`) and `notify-settings.json`
  (`notifycfg.settings`). The report must live in the same kind of place: new
  `REPORTS = state/reports/` (env `JOBBOARD_REPORTS`) — reports/`YYYY-MM-DD.md` + `index.json`.
- A job record's facts a report needs: `status` (queued/running/done/failed/stopped/cancelled/
  interrupted/safety_stop), `folder`, `job`, `created/started/finished`, `fail_note`,
  `verify = {status, rounds[{round, summary, issues}], issues, banner, final, timeline}`,
  `checkpoints = {checkpoints[], rollbacks, discarded}`, `watchdog = [{time, kind, reason,
  command?|path?}]` (a safety-stop reason already reads "Safety stop: tried to reach …", the
  command is in `command`), `attempts`/`attempt_log` (U05 tries, each with `tests_passed`),
  `task_id`/`task_try` (U04b), `push = {ok, branch, time}` (whether Jason pushed the job branch),
  `recheck.summary`. Checks summary = `{status, results[{name, cmd, ok, exit_code, passed,
  failed, skipped, first_error, timed_out, mode}]}`.
- Given logic (use, do not rewrite): `agentjobs_upgrades/report.py` — `build(jobs, bench_sets,
  since, until, base_url, qwen_summary)`. Its `DONE = {"passed"}`, `FAILED = {failed, stuck,
  safety_stop, stopped}`, a `needs_review` line, and its own field names (`title`, `project`,
  `rounds`, `rollbacks`, `attempts`, `approval`, `first_failure`, `watchdog_reason`,
  `safety_command`, `queue_blocked`, `tests_passed`) — so the glue maps board records onto that
  shape. It links `/jobs/<id>` and `/api/jobs/<id>/bundle` (the board's routes are `/job/<id>` and
  `/api/bundle/<id>`), and `notify.py` builds click links as `<base>/jobs/<id>` too → add those
  two aliases in `jobboard.py`; that also fixes 9b's click links.
- "The model server is idle": `basalt_status()` (`/v1/models` → `data[0].status.value`, which
  basalt answers as `loading` / `loaded` / `unloaded`) and nothing active on the board (the GPU
  serves one stream). A summary call goes through `vision.http_call(url)` (POST
  `/v1/chat/completions`); `tests/fakeserver.py` is the stand-in — tests never touch 8080.
- The push notification for a finished report already exists: `notifycfg.report_ready(path,
  lines)` → event `morning_report` (in `notify.EVENTS`, on the Phone page's tick list, tag
  `sunrise`). Nothing to change in 9b.
- A queue finishing is already detectable: `plan.Queue.mark` sets `status = "finished"` when
  every task is done/failed/skipped, and `taskqueue.job_finished(m, queue_ctx())` is the hook
  `process_job` already calls — so an overnight queue ending is one `# U10` hook there.
- Test command (`AGENT_CHECKS.json`): `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore
  -m unittest discover -s tests`. Baseline before my changes: **550 tests, OK** (job 08's count).

## Steps

- [ ] `web/morningreport.py` (new glue, `# U10`): settings (`state/report-settings.json`: the
      time, default `07:00`, whether to ask the model); the "since last report" window from
      `reports/index.json`; `fact(m)` (a job record → report.py's dict: status words, the first
      failing check line, the watchdog reason, `Safety stop: <command>`, fix rounds, rollbacks,
      attempts, tests passing, whether it blocks its queue, waiting-for-approval);
      `facts_for(...)` (jobs + bench sets + skipped queue tasks + plans waiting for approval, in
      the window); `make(...)` (report.build → `reports/YYYY-MM-DD.md` + index + push
      notification, model paragraph only when the server is idle); `due(...)` for the clock
      trigger; the Reports-tab HTML + a small markdown→HTML so the links work.
- [ ] `jobboard.py` hooks: `REPORTS`, the Reports nav link, `GET /reports` and
      `GET /reports/<name>`, `POST /report/now` and `/report/settings`, the `tick()` call in
      `worker()`, the queue-finished hook after `taskqueue.job_finished`, and the
      `/jobs/<id>` + `/api/jobs/<id>/bundle` aliases report.py/notify.py link to.
- [ ] `web/tests/test_u10_report.py`: fixture records covering every state (done ✓ with checks,
      failed ✗ with the first failing check line, watchdog stop, safety stop, needs review,
      stopped, interrupted, cancelled, running, skipped queue task, waiting plan, bench sets),
      the since-last-report window (and that a second report covers only what came after), the
      no-model-call-when-busy rule (busy: a job running / model not loaded), the saved file +
      index + push line, the clock trigger, and the routes in the real handler (Reports tab,
      Make report now, the time form, the `/jobs/…` aliases, the guard).
- [ ] Full suite passes with the gate's command; smoke-serve the board on a free port (never
      8090) with its own state folders and click the Reports tab with curl.
- [ ] PLAN.md steps ticked; AGENT_REPORT.md section for this job.

---

## Plan left by the previous job (Upgrade 4b: the task queue) — kept verbatim so nothing is lost

# Job 08 — Upgrade 4b: run the approved plan as a queue

## Findings

- Server + pages: `web/jobboard.py` (2160 lines). `main()` starts `worker()` +
  `ThreadingHTTPServer` on `127.0.0.1:PORT`; routes in `H.do_GET`/`H.do_POST` behind `H.guard()`.
  Jobs run ONE at a time: `worker()` waits for a queued record and calls `process_job(name)`
  (safety `check_record` → jail self-test → `record_git_start` → `freeze_checks` →
  `run_planned` → follow-ups → notify → review bundle). A finished job can queue the next one
  from the worker thread (`maybe_auto_attempts`, `planning.plan_finished` → `new_job(...)`),
  which is exactly how a task queue advances.
- 4a already did the plan side: `web/planning.py` (project records `PLANS/<folder>.json`,
  `create_folder` = git init on `agent/<folder>` + first commit, the planner job,
  `read_tasks`/`validate`/`approve` — the APPROVED tasks are copied into the record outside the
  sandbox — plan review page with Save/Approve). `H.plan_edit` is where "Approve plan" lands.
- Given logic (use, don't rewrite): `agentjobs_upgrades/plan.py` — `order()` (stable Kahn),
  `job_prompt(outline_summary, milestone_text, task)` (the 4b task prompt: outline + milestone +
  task + acceptance + "Read AGENT_NOTES.md first… add 2-5 lines"), and `Queue`
  (`load_plan`, `recover`, `set_cutoff`, `next_task`, `mark` with on_fail stop/skip,
  `progress`), already unit-tested in `tests/test_rest.py`.
- U05 (`web/attempts.py`): `run_best_of_n` is reached from `run_planned` whenever the record
  carries `attempts` (2-4) — so a queue task gets best-of-N by setting `attempts` in its
  record; `MIN_N`/`MAX_N`/`DEFAULT_N` are the bounds; `phase_tag` keeps the banner honest.
- Board git on a project folder always goes through `safegit.SafeGit` wrapped in that folder's
  own jail (`upgrades.make_wrap(root, folder, "agentgit-…")` → `jail/run-local.sh sandbox-run`);
  `upgrades.HOST_MODE` (`JOBBOARD_JAIL=host`) is how the tests run it on temp folders.
- Fake agent for tests: hidden mode `fake` (`JOBBOARD_FAKE=1`), job text = a scenario JSON
  (`{"rounds": [[write/say/tick/cmd/run/exit…]]}`), so a queued task in a test needs its prompt
  swapped for a scenario — `tests/test_u04a_plan.py::DRIVER` already does that and keeps the real
  prompt in a file, which is how the 4b prompt gets asserted end-to-end.
- State must live outside anything the sandbox mounts (the agent can write anything in
  `WORK/<folder>`): 4a added `PLANS`; 4b adds `QUEUES = state/queues/<folder>.json`
  (env `JOBBOARD_QUEUES`), holding the `plan.Queue` state file.
- Test command (`AGENT_CHECKS.json`): `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore
  -m unittest discover -s tests`. Baseline before my changes: **478 tests, OK** (job 07 left its
  PLAN.md uncommitted in this shared copy; committed as-is first).

## Steps

- [x] Commit job 07's leftover PLAN.md as-is (its own commit) so this job's diffs stay clean.
- [x] `web/taskqueue.py` (new glue, `# U04b`): the queue file `QUEUES/<folder>.json` built from
      the APPROVED tasks (`plan.Queue`, dependency order, nothing runs until started); the task
      prompt (10-line outline summary + its ROADMAP.md milestone + task + acceptance via
      `plan.job_prompt`); `start_next` (one job, in the project folder, task checks, best-of-N
      for size M or a task that already failed when the project has it on, resume for a retried
      task, AGENT_NOTES.md hash taken before it runs, base commit recorded, project branch kept);
      `job_finished` (AGENT_NOTES note in the job record, seconds, end commit, done/failed/
      interrupted, on_fail stop vs skip, then the next task); `startup` recovery; the time-left
      estimate; the project-page card (milestone progress, current task, Pause/Resume/Cancel,
      "Don't start new tasks after", on-fail choice, failed/skipped shown).
- [x] `jobboard.py` hooks: `QUEUES`, `check_record` validation for the task fields, `worker()`
      startup (interrupted job records + `taskqueue.startup`), `taskqueue.job_finished` in
      `process_job`, the card on the project and plan pages, `POST /queue/<folder>`,
      project-record settings (which agent runs tasks, best-of-N on/off).
- [x] `web/tests/test_u04b_queue.py`: queue from an approved plan, prompt content, best-of-N rule,
      notes check, estimate, milestone progress, stop vs skip, cutoff, restart recovery, the
      branch keeping, the card HTML, the routes — plus fake-agent runs through `process_job`
      (dependency order + chained commits, checks of a task, a failure that stops vs skips, the
      "don't start after" time, and the notes note in the job record).
- [x] Full suite passes with the gate's command; smoke-serve the board on a free port (never
      8090) and click the queue with curl.
- [x] AGENT_REPORT.md section for this job.

---

## Plan left by the previous job (Upgrade 4a: requirements → plan) — kept verbatim so nothing is lost

## Job 07 — Upgrade 4a: requirements → plan

### Findings

- Server + pages: `web/jobboard.py` (2022 lines). `main()` starts `worker()` +
  `http.server.ThreadingHTTPServer` on `127.0.0.1:PORT` (`jobboard.PORT`, env `JOBBOARD_PORT`);
  routes live in `H.do_GET`/`H.do_POST` behind `H.guard()` (Host must match `jobboard.PORT`,
  `jb=` cookie). `render(body)` wraps a page, `header()` is the top bar, `esc()` escapes.
- Job life: `new_job(mode, folder, job, offline, resume=, parent=, extra=)` →
  `JOBS/<id>/meta.json` (env `JOBBOARD_JOBS`, outside everything the sandbox mounts) →
  `worker()` → `process_job(name)` → `check_record()` (safety re-check) → jail self-test (only
  for `local-*` modes) → `record_git_start` → `upgrades.freeze_checks` → `run_planned`
  (`run_agent_job`, or `attempts.run_best_of_n` when the record asks for tries) →
  `maybe_auto_attempts` → notify → `build_review_md`. Project folders: `WORK/<folder>`
  (env `JOBBOARD_WORK`), `folders()` lists them and hides attempt folders.
- A finished job can queue a follow-up job from the worker thread: `maybe_auto_attempts` and
  `hard_retry` call `new_job(..., parent=..., extra=...)`. That is the pattern for "send the
  errors back to the planner once".
- **`H.do_POST` parses the body with `urllib.parse.parse_qs` WITHOUT `keep_blank_values`**, so
  an empty field disappears from its list. A repeating card form therefore cannot align
  same-name multi-values — the plan form names every field with its card index
  (`tid1`, `title1`, `prompt1`, …) and the server reads them in index order.
- The button that was pressed arrives as a form field (`<button name="move" value="t3:up">`),
  which is how one endpoint does save / approve / reorder / delete / add.
- Given logic (use, don't rewrite): `agentjobs_upgrades/plan.py` — `TASK_FIELDS`/`REQUIRED`,
  `SCHEMA` (draft 2020-12 JSON Schema for the task list), `validate(tasks) -> [errors]`
  (required fields, unknown fields, id charset, duplicate ids, unknown/self dependencies,
  and `order()`'s cycles), `order(tasks)` (stable Kahn), `job_prompt(...)` (per-task prompt,
  that is 4b's), `Queue` (4b's persistent queue). Already covered by `tests/test_rest.py`.
- Jail rules for this job: any git the board runs on a project repo goes through
  `agentjobs_upgrades/safegit.SafeGit` (`-c core.hooksPath=/dev/null`, config not trusted,
  `GIT_CONFIG_NOSYSTEM=1`) wrapped in the project's own jail with
  `upgrades.make_wrap(root, folder, prefix)` → `jail/run-local.sh sandbox-run <root> <folder>
  <name> --` (only `<root>` mounted at `/work/<folder>`, no network). `upgrades.HOST_MODE`
  (`JOBBOARD_JAIL=host`) runs it straight on the host — how the tests work. Plain file writes
  into `WORK/<folder>` are the existing house pattern (`checks.write_checks`).
- Plan state must live outside the sandbox (the planner can write anything inside
  `WORK/<folder>`): new `PLANS = state/plans/<folder>.json` (env `JOBBOARD_PLANS`), same
  safety rule as `JOBS`.
- Fake agent for tests: hidden mode `fake` (`JOBBOARD_FAKE=1`), job text = a scenario
  (`{"rounds": [[step…]]}`; `write`/`say`/`tick`/`cmd`/`exit` steps). U01/U05 tests run one
  fake job per test in a child process with `JOBBOARD_JAIL=host` + `JOBBOARD_FAKE=1`
  (`tests/test_u01.py::Board.run_job`); page tests serve `jobboard.H` in-process on their own
  port with `jobboard.PORT` set (`tests/test_u05_bestofn.py::Pages`).
- Test command (`AGENT_CHECKS.json`): `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore
  -m unittest discover -s tests`. Baseline before my changes: **425 tests, OK** (job 06's
  leftover best-of-N work was left uncommitted in this shared copy; committed as-is first).

### Steps

- [ ] Commit job 06's leftover work as-is (its own commit) so this job's diffs stay clean.
- [ ] `prompts/planner.md` (repo root): the planner prompt with `{{NAME}} {{KIND}} {{FOLDER}}
      {{OUTLINE}} {{ERRORS}}` placeholders — "do not write app code", write ROADMAP.md +
      tasks.json, ~45-minute tasks, task 1 = skeleton + test setup + AGENT_CHECKS.json,
      testable acceptance criteria.
- [ ] `docs/tasks-schema.json`: the task schema, exactly `plan.SCHEMA` (a test keeps it in sync).
- [ ] `web/planning.py` (new glue, `# U04a`): project records under `PLANS/`, project folder
      creation (jail git init on branch `agent/<name>` + a first commit), the planner prompt,
      `start_plan` (a normal job, `plan_job` marker, checks off), reading + validating the
      folder's `tasks.json`/`ROADMAP.md`, the one automatic re-send of the errors,
      save/approve, and the page HTML (new project, project, plan review).
- [ ] `jobboard.py` hooks: `PLANS`, header links, `check_record` validation for
      `plan_job`/`plan_retry`, `planning.plan_finished(...)` after `run_planned`, the plan card
      on a planning job's page, GET `/projects` `/project/new` `/project/<folder>`
      `/plan/<folder>`, POST `/project/new` `/project/edit/<folder>` `/plan/edit/<folder>`.
- [ ] `web/tests/test_u04a_plan.py`: schema (good/bad/cycles) + the docs file stays in sync,
      prompt content, project folder creation (git init, branch, first commit), records +
      approval state, the plan form parse (index-named fields, move/drop/add, blank values),
      a canned planner output through the fake agent (valid plan, and an invalid plan sent
      back to the planner exactly once), and the pages/routes served in-process.
- [ ] Full suite passes with the gate's command; smoke-serve the board on a free port
      (never 8090) and click the new pages with curl.
- [ ] AGENT_REPORT.md section for this job.

---

## Plan left by the previous job (Upgrade 5, best-of-N) — kept verbatim so nothing is lost

## Job 06 — Upgrade 5: best-of-N for hard steps

### Findings

- Server + pages: `web/jobboard.py` (1941 lines). `main()` starts `worker()` +
  `http.server.ThreadingHTTPServer` on `127.0.0.1:PORT` (`jobboard.PORT`, env `JOBBOARD_PORT`);
  routes are in `H.do_GET`/`H.do_POST` behind `H.guard()` (Host must match PORT, `jb=` cookie).
- Job life: `new_job()` → `JOBS/<id>/meta.json` (env `JOBBOARD_JOBS`, never inside a mounted
  folder) → `worker()` → `process_job(name)` → safety `check_record()` → jail self-test →
  `record_git_start(m)` (`git_start` = base sha, `git_branch_start`) → `upgrades.freeze_checks()`
  → `run_agent_job(m, name)` → `notify_finished` + `build_review_md`. Project folders live in
  `WORK/<folder>` (env `JOBBOARD_WORK`).
- One run inside `run_agent_job`: `JobCtx(m, name, cfg)` (root = `WORK/m["folder"]`,
  `EventLog` on `JOBS/<id>/events.jsonl`, `Watchdog` with project path `/work/<folder>`,
  `CheckpointManager` in `<root>/.git/agentjobs/<id>`) → `run_agent_loop` (agent + watchdog
  restarts) → `run_gate` (`verify.VerifyGate` + `checks.run_all(root, job_dir/checks/round-N, wrap)`).
- Jail: `upgrades.make_wrap(root, folder, prefix)` → `jail/run-local.sh sandbox-run <root>
  <folder> <name> --` → `jail/podman-local.sh jail_local_args` mounts **only** `<root>` at
  `/work/<folder>` (`-w /work/<folder>`), no network. The real agent goes through
  `~/agent-sandbox/agent <mode> --in <folder>`, which only accepts a top-level `~/agent-work`
  folder name (no slashes). `upgrades.HOST_MODE` (`JOBBOARD_JAIL=host`) runs git/checks/the fake
  agent straight on the host — that is how the tests work.
- All board git on a job repo goes through `agentjobs_upgrades/safegit.SafeGit`
  (`-c core.hooksPath=/dev/null`, `core.fsmonitor=false`, `protocol.allow=never`, …,
  `GIT_CONFIG_NOSYSTEM=1`). Pushing only via `jail/push-agent-branch.sh` (`jailcfg.push_branch`),
  and only for branches matching `agent/...` (`jailcfg.BRANCH_RE`).
- Given logic (use, don't rewrite): `agentjobs_upgrades/bestofn.py` — `Attempt` (k, branch,
  status, tests_passed, test_files_changed, diff_lines, seconds, issues; `clean_pass`),
  `sort_key` (clean pass → most tests → fewest test files → smallest diff → fastest → k),
  `pick_winner`, `should_continue(done, n, hard, stop_early)`, `why`. Also
  `guard.is_test_file()` for test-file paths and `checks.counts_total()`.
- Ranking needs per-attempt numbers the gate does not store: tests passing come from
  `verify["final"]["results"]`, the rest (test files changed, diff size, file list) from git.
- Fake agent for tests: hidden mode `fake` (`JOBBOARD_FAKE=1`), job text = a scenario
  (`{"rounds": [[step…], …]}`; steps `write/delete/tick/cmd/say/sleep/exit`), run by
  `upgrades.fake_cmd(m, root, folder, scenario_text, rnd, resume)`.
- Test command (`AGENT_CHECKS.json`): `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore
  -m unittest discover -s tests`. Baseline before my changes: **382 tests, OK**.
  In-process page tests serve `jobboard.H` on their own port and set `jobboard.PORT`
  (`tests/test_u07b_changes.py::Pages`); the U01 integration tests (`tests/test_u01.py`) run one
  fake job per test in a child process with `JOBBOARD_JAIL=host` + `JOBBOARD_FAKE=1`.
- Working tree when I started: job 05's ntfy work was left **uncommitted** in this shared copy
  (`web/notifycfg.py` untracked, `# U09b` hooks in `web/jobboard.py`, its PLAN.md). It is not my
  job; I commit it as-is in its own commit first so my diffs stay clean and nothing is lost.
- A git **worktree** cannot be an attempt's project folder: the jail mounts one folder and a
  worktree's git dir lives in the main repo (invisible in the container), so git inside the
  sandbox would break. Attempts are therefore **full clones** of the base commit in their own
  `~/agent-work` folder — same base commit, one branch `agent/<job id>-try<k>` each, and an
  attempt can only ever see its own copy. Branches are brought back to the project repo through a
  **git bundle** made inside the attempt's jail (`protocol.allow=never` stays; only the file
  transport is opened for the two steps that cannot run in the jail: `clone --no-checkout` and
  `fetch <bundle>`, and the working trees are built inside the jail so no agent-planted filter
  ever runs on the host).

### Steps

- [ ] Commit job 05's leftover ntfy work as-is (its own commit) so this job's commits stay clean.
- [ ] `web/attempts.py` (new glue): settings keys (`attempts` 2-4 default 3, `stop_early` on,
      `auto_attempts` off), record fields + plan, attempt folder/branch names, and the git
      plumbing: clone the base commit into its own folder, branch `agent/<id>-try<k>`, save
      leftover edits, bring the branch back through a bundle, `apply_winner` (job branch
      `agent/<id>`), `prune_old` (7 days) — all through SafeGit, working trees inside the jail.
- [ ] Scores: turn one finished attempt into a `bestofn.Attempt` (status, tests passing, test
      files changed, diff lines, seconds, needs-review issues) + the file list for the page;
      rank with `bestofn.pick_winner`/`why`, stop early with `bestofn.should_continue`.
- [ ] `run_best_of_n`: attempts one after another in the worker (one GPU), phase
      "Attempt 2 of 3", per-attempt check logs under `JOBS/<id>/attempts/try-k/checks/…`,
      attempts never resume each other's session, then apply the winner and clear the
      per-attempt folders/verify/checkpoints from the record.
- [ ] `jobboard.py` hooks: `check_record` validation, form fields ("Hard: try 3 times",
      2-4, "about 3× longer"), `run_agent_job(..., fresh_log=)`, `JobCtx` per-attempt checks
      dir, `folders()` hides attempt folders, the auto "try fresh attempts" setting.
- [ ] UI: the Attempts card on the job page (every attempt's scores, changes and branch, cost
      note), "Use this one instead", "Try 3 fresh attempts" on a job that failed after fix
      rounds, and their POST routes.
- [ ] `web/tests/test_u05_bestofn.py`: unit tests for names/settings/plan, scores + every
      tie-break, early stop, all-attempts-fail, clone/branch/bundle/apply/prune on a real temp
      repo, and fake-agent runs (hard job with 3 different scenarios, early stop, all fail +
      "Use this one instead", the buttons/routes).
- [ ] Full suite passes with the gate's command; AGENT_REPORT.md section for this job.

---

### Plan left by the previous job (Upgrade 9b, phone alerts) — kept verbatim so nothing is lost

#### Findings

- Server + pages: `web/jobboard.py` (1889 lines). `main()` starts the `worker()` thread and
  `http.server.ThreadingHTTPServer` on `127.0.0.1:PORT` (`jobboard.PORT`, env `JOBBOARD_PORT`).
  Routes live in `H.do_GET`/`H.do_POST` behind `H.guard()` (Host must match `jobboard.PORT`,
  `jb=` cookie). `render(body)` wraps a page; `header()` is the top bar (Bench, GPU stats).
- Jobs: `new_job()` → `JOBS/<id>/meta.json` (`JOBBOARD_JOBS`), `worker()` → `process_job()` →
  `run_agent_job()` → `run_gate()`. Final `meta["status"]` ∈
  `done|failed|safety_stop|stopped|cancelled|interrupted`; bench runs use `kind: "bench"` with
  `done|failed|stopped|interrupted`. `meta["verify"]` = `agentjobs_upgrades/verify.py` outcome
  (`status` ∈ passed|unverified|needs_review|stuck|failed|aborted, `final` summary with
  `results[]` of `{name, cmd, passed, failed, skipped, ok, exit_code, output_file}`).
- Where a job ends: `process_job()` calls `notify_finished(m, ok)` (the old **desktop**
  notification + sound, `notify-send`/`pw-play`) in three places — after a bench run, after a
  failed sandbox self-test, and after `run_agent_job()`. `run_agent_job()` also writes the
  timeline line via `dashboard.tl_add`.
- Settings: `upgrades.load_settings(SETTINGS_FILE)` — but it only keeps numeric keys plus
  `watchdog`, so string settings (server/topic/token/times) need their own file:
  `~/agent-sandbox/state/agentjobs-settings.json` sibling → `notify-settings.json`
  (env override `JOBBOARD_NOTIFY`).
- Given logic (use, don't rewrite): `web/agentjobs_upgrades/notify.py` —
  `NotifySettings`, `random_topic()` (aj- + 20 chars), `validate()`, `in_quiet_hours()`,
  `clean()` (drops code, paths, non-text), `build()` (title/message ≤2 lines, priority
  5 for safety_stop, `click` link when `base_url` is set), `send()` (off when `server` empty
  or the event is unticked, quiet-hours suppression except safety_stop, 3 tries with
  exponential backoff, `post` injectable, `_post` = urllib POST with a Bearer token).
  `report.py` is upgrade 10 (morning report) and is not wired into the board yet.
- Nothing in `jobboard.py` uses `auth.py` (9a phone access) yet, so the click link stays
  opt-in through a `base_url` setting.
- Glue-module pattern in this repo: `web/badges.py`, `web/diffcard.py`, `web/dashboard.py`,
  `web/jailcfg.py`, `web/upgrades.py` — logic in the module, tiny `# U0x` hooks in jobboard.
- Test command (`AGENT_CHECKS.json`): `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore
  -m unittest discover -s tests`. Baseline before my changes: **382 tests, OK**.
  In-process page tests serve `jobboard.H` on their own free port and set `jobboard.PORT`
  (`tests/test_u07b_changes.py::Pages`, `tests/test_jobweb_bench.py::BenchPagesInProcess`).

#### Steps

- [ ] New `web/notifycfg.py` (glue): settings load/save (0600, env `NTFY_SERVER`/`NTFY_TOPIC`
      seed the defaults, topic pre-filled with `notify.random_topic()`), form ↔ settings with
      `notify.validate`, message builders (job finished ✓/✗ with checks counts, waiting for
      you, safety stop, bench verdict, morning report, model down — counts/status only, never
      log text or paths), `push()` on a background queue (never blocks a job, retries 3× via
      `notify.send`, outcomes appended to `state/notify-log.jsonl`), edge-triggered
      `model_check()`, and the settings-page HTML.
- [ ] `jobboard.py` hooks: `NOTIFY_FILE`, `phone_notify(m)` at the three finish sites,
      `phone_model_check()` in the worker loop, `GET /notify` + `POST /notify`, "Phone" link in
      the header, defaults written in `main()`, `--test-phone` CLI.
- [ ] `web/tests/test_u09b_notify.py`: settings/validation, message content (no log text,
      paths, diffs), quiet hours (safety stop exempt), retries + logging, off by default,
      never blocks the caller, model-server edge trigger, and the pages/POST on their own port.
- [ ] Full suite passes (same command as the verify gate); smoke-serve the board on a free
      port (never 8090) against a local fake ntfy server and curl the settings page.
- [ ] AGENT_REPORT.md section for this job.
