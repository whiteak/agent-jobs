# Job 02 — Upgrade 7b: live dashboard, part 2 (diff viewer + check badges)

## Findings

- Server/pages: `web/jobboard.py` (1850 lines). `main()` starts the worker + `http.server`
  on `127.0.0.1:8090`; routes live in `H.do_GET`/`H.do_POST` (all behind `H.guard()`, which
  checks `Host` against the module global `jobboard.PORT` and the `jb=` cookie).
- Jobs: `new_job()` writes `JOBS/<id>/meta.json` (`JOBS` = `~/agent-sandbox/state/jobs`,
  overridable with `JOBBOARD_JOBS`); `worker()` → `process_job()` → `run_agent_job()`.
  `record_git_start()` stores `git_start` (the base commit) + `git_branch_start` before the
  agent runs. Project folders live in `WORK/<folder>` (`JOBBOARD_WORK`).
- Git in a job repo: `jobboard.in_sandbox()` (read-only podman mount) is used by
  `git_info()`/`/api/git/<jid>`; U01 uses `upgrades.make_wrap(root, folder, prefix)` →
  `jail/run-local.sh sandbox-run` and `agentjobs_upgrades/safegit.SafeGit`
  (`git -c core.hooksPath=/dev/null …`, `GIT_CONFIG_NOSYSTEM=1`). `upgrades.HOST_MODE`
  (`JOBBOARD_JAIL=host`) runs those commands straight on the host — that is how tests do it.
- Checkpoints: `m["checkpoints"] = {"checkpoints": [{n, step, commit, passed, failed,
  build_ok, time}], "rollbacks", "discarded", "base"}` (from `checkpoints.CheckpointManager`).
- Checks: `m["verify"]["rounds"][i]["summary"]["results"]` and `m["recheck"]["summary"]`;
  each result = `{name, cmd, exit_code, passed, failed, skipped, seconds, output_file,
  timed_out, first_error, ok}`; logs live in `JOBS/<id>/checks/round-<r>/check-<name>.log`
  (`ck.run_all(..., job_dir(name) / "checks" / f"round-{r}", ...)`).
- Review bundle rules to reuse: `reviewbundle._skip()` (locks, `AGENT_REPORT.md`/`PLAN.md`,
  `SKIP_DIRS`), `_parse_numstat`, `UNTRACKED_SCRIPT`/`_untracked_listing`; plus
  `agentjobs_upgrades/diffparse.py` (given): `parse()`, `summary()`, `side_by_side()`,
  `is_generated()`.
- 7a (`web/dashboard.py`): `timeline_for/tl_add`, `job_stream()` SSE, `badge()`,
  `checks_cell()`; job page cards + one EventSource; job rows from `/api/rows`.
- Test command: `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore -m unittest discover -s tests`
  (also in `AGENT_CHECKS.json`). Page `<script>` blocks are syntax-checked with `node --check`
  (node v22 is installed) — and all scripts of a page must parse *together*, so page JS stays
  inside its own IIFE.
- Baseline before my changes: **346 tests, 7 failures + 1 error**, all in
  `tests/test_u07_dashboard.py` (7a leftovers, see HANDOFF-7a.md): the `Pages` class serves
  `jobboard.H` on its own port without setting `jobboard.PORT` (403 "Bad host"),
  `EventLog` is not callable, and one expected tool order in `test_qwen_log_rows` does not
  match the `sample-jobs` fixture.

## Steps

- [ ] Fix the 7a leftovers so the suite starts green (no test deleted/weakened):
  `Pages` sets `jobboard.PORT`, `upgrades.EventLog.__call__`, correct the fixture-order assert.
- [ ] New `web/changes.py`: sandboxed git collection — refs (all / commit / checkpoint),
  file list with +/- counts (numstat + name-status + untracked listing in one call, safe git
  flags), per-file diff (untracked via `git diff --no-index`), binary/generated marked and
  not shown (reuse `reviewbundle._skip` + `diffparse.is_generated`), small TTL cache.
- [ ] Parser tests for `diffparse` (added/deleted/renamed, binary markers, "no newline at end
  of file") and side-by-side pairing.
- [ ] Check badges: `dashboard.check_badges()` (one per check, `build ✓`, `tests 41/42 ✗`)
  linking to a new `/api/check/...` output endpoint (path resolved from meta, must stay
  inside `JOBS/<id>`); badges on the job page and in the jobs list.
- [ ] Endpoints + card in `jobboard.py`: `/api/changes/<jid>`, `/api/changes/<jid>/file`,
  `/api/check/<jid>/<source>/<n>`; Changes card with file list, unified/side-by-side toggle,
  collapsed unchanged parts, "Next change", ref picker, 10 s refresh + refresh on stream
  events, lazy (chunked) rendering.
- [ ] `web/tests/test_u07b_changes.py`: collection against a real temp git repo (host jail),
  endpoint tests on its own port, badge tests, page/script tests (node --check).
- [ ] Full suite passes; smoke-serve the board on a free port (never 8090) and click through
  the endpoints with curl.
- [ ] AGENT_REPORT.md section for this job.
