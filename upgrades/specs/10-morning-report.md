# Upgrade 10: Morning report

## Ground rules (same for every upgrade job)
- Work only in ~/agent-work/jobboard-u10, a fresh copy of the current live Agent Jobs tool, on branch agent/jobboard-u10. Never edit ~/agent-sandbox, never stop or restart any service (jobboard, basalt), never touch ~/basalt_config.json.
- No network. Do not pip/npm/apt install or download anything. Use the Python standard library and what the project already uses. If something you need is missing, stop and say so in AGENT_REPORT.md.
- Read before you change anything: list the files, read web/jobboard.py, web/jobstatus.py, the code that starts, sandboxes and tracks jobs, the review-bundle code and the tests. Then write PLAN.md with a short "Findings" section (where jobs are created, launched, sandboxed, stored and shown; the job record format; the exact test command) and a checkbox list of steps. Tick steps as you finish them.
- Put new logic in new, small modules with unit tests. Keep every existing feature (including voice buttons if present) and the current look working. Match the existing code style.
- Never delete, skip or weaken an existing test. Run the full test suite after each step; it must pass before you tick the step.
- Sandbox: anything that runs the project's own code (builds, tests, checks, screenshots, attempts) must go through the same jail the agent uses (agent-jail.sh --project <job dir> -- <cmd>, see the jail docs in the tool), never directly on the host. Any git command the tool itself runs on a job repo must use `git -c core.hooksPath=/dev/null` and must not trust that repo's own config (the agent could have planted hooks); pushing goes only through push-agent-branch.sh.
- Tests must never call the model server (127.0.0.1:8080) or start a real agent; mock them (use the fake agent once upgrade 1a adds it).
- Commit locally after each working step with a clear message. Do not push.
- To try the web page, run your copy on port 8091 (add a --port option if there isn't one) with its own state folder. Never use port 8090.
- UI text: short, plain, friendly English. No jargon on buttons.
- Finish with AGENT_REPORT.md: what changed (files), how to test it by hand click by click, test counts before and after, and anything unfinished or assumed.

1. Covers everything since the previous report: jobs, project queues and bench sets.
2. Facts come only from job and bench records, never written by the model: per project, tasks done ✓ (with checks), failed ✗ with the reason (first failing check line, watchdog reason, or "Safety stop: <command>"), waiting for approval, skipped; time used, fix rounds, rollbacks, best-of-N attempts; bench verdicts.
3. "Needs you" list at the top: approvals waiting, safety stops, failures blocking a queue, each with a link.
4. Optional short paragraph at the end, labelled "Qwen's summary", only if the model server is idle; skipped otherwise.
5. Made automatically at a set time (default 07:00 local) and when an overnight queue finishes, plus a "Make report now" button. Saved as reports/YYYY-MM-DD.md in the state folder, shown on a phone-friendly "Reports" tab, and sent as a push notification (9b) if set up.
6. Each failed item links to "Export for review" (the review bundle).

Tests: a report from fixture records covering every state, the "since last report" window, no model call when the server is busy.
