# Resolved — job 01 re-run (12 Nov). The original note is kept below, unchanged.

- **403 "Bad host" in `test_u07_dashboard.Pages` (8 tests)** — fixed in `b433423` (job 02): that
  class now sets and restores `jobboard.PORT` around serving `jobboard.H` on its own free port,
  the way `tests/test_jobweb_bench.py::BenchPagesInProcess` does. `guard()`'s Host check was not
  weakened and no test was skipped or loosened.
- **A real smoke test on port 8091** — done in job 01: the board served on `127.0.0.1:8091` with
  its own state folders (`JOBBOARD_JOBS/WORK/PLANS/QUEUES/SETTINGS/NOTIFY`, temp `HOME`) and the
  fake agent; one job was started through the real form, its SSE stream watched as it grew,
  dropped on purpose and resumed with `Last-Event-ID` + `?log=`, and the raw log, the job page
  and the list badges were all read. Output is in `AGENT_REPORT.md` (job 01). It found one real
  bug: the timeline's "started" row cut the new-job-form label at its colon and wrote
  "Started (Fake agent (tests only" — fixed in `28e8795`.
- **AGENT_REPORT.md section for 7a** — added by job 01.
- The 7a tests stand at 33 in `web/tests/test_u07_dashboard.py`, and the whole suite
  (`AGENT_CHECKS.json`'s command) is green.

---

# Upgrade 7a hand-off note (11 Nov, evening)

State of 7a at this commit:
- Fixed: the two failures of the old suite (JS "\n" escapes inside the job page f-string; the banner label is back to
  "Checks passed (...)" and the new badges live in status["badge"] / the jobs list pill instead).
- New: web/tests/test_u07_dashboard.py (SSE format, Last-Event-ID resume, log parser on sample-jobs, badges, pages).
- STILL FAILING in test_u07_dashboard.Pages (8 tests): the handler answers 403 "Bad host" because this test serves
  jobboard.H on its own port while the Host check expects jobboard.PORT. See how tests/test_jobweb_bench.py
  (BenchPagesInProcess) serves the same module on its own port and do the same. Do not weaken the Host check.
- Not yet done: a real smoke test on port 8091, AGENT_REPORT.md section.
Run with: cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore -m unittest discover -s tests
(always through discover; dotted names like tests.test_jobstatus import the board twice).
