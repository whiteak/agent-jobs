# Upgrade 7a: Live dashboard, part 1 (timeline and live log)

## Ground rules (same for every upgrade job)
- Work only in ~/agent-work/jobboard-u07a, a fresh copy of the current live Agent Jobs tool, on branch agent/jobboard-u07a. Never edit ~/agent-sandbox, never stop or restart any service (jobboard, basalt), never touch ~/basalt_config.json.
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

1. Live updates by Server-Sent Events (/api/jobs/<id>/events) instead of polling every 3 s; fall back to polling if the stream drops.
2. Timeline per job: a vertical list of events with time and icon: queued, started, step ticked, checks (pass/fail), fix rounds, checkpoints, rollbacks, watchdog actions, safety stop, finished. Read from the job record events; if there is no events list yet, add one ({time, type, text, data}) and have upgrades 1–3 write to it.
3. Live log: a readable view of the agent log with commands, file edits and model messages as separately styled rows; long outputs collapsed to 10 lines with "Show more"; auto-scroll that pauses when you scroll up; a search box. Keep "Show raw log".
4. Jobs list: a badge per job (Working, Queued, Done ✓, Failed ✗, Not verified, Needs review, Safety stop), duration and checks result.
5. Keep the current status banner, step checklist and notifications.

No CDN or new libraries. Tests for the event stream (format, reconnect with Last-Event-ID), the log parser on 2–3 real logs copied from the live state folder into tests/fixtures, and the badges.
