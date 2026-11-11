# Job 06 — Upgrade 5: best-of-N for hard steps

## Findings

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

## Steps

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

## Plan left by the previous job (Upgrade 9b, phone alerts) — kept verbatim so nothing is lost

### Findings

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

### Steps

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
