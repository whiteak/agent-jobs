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
