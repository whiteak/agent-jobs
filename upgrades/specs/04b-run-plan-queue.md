# Upgrade 4b: Run the approved plan as a queue

## Ground rules (same for every upgrade job)
- Work only in ~/agent-work/jobboard-u04b, a fresh copy of the current live Agent Jobs tool, on branch agent/jobboard-u04b. Never edit ~/agent-sandbox, never stop or restart any service (jobboard, basalt), never touch ~/basalt_config.json.
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

Builds on 4a and upgrades 1, 2, 3 and 5.

1. "Approve plan" turns the tasks into queued jobs on the project branch, in dependency order. Each job prompt = a 10-line outline summary + its ROADMAP milestone + the task prompt + acceptance criteria + "Read AGENT_NOTES.md first, and add 2–5 lines of lessons to it when you finish."
2. One job at a time; each starts from the previous job's final commit; checks, fix rounds, checkpoints and watchdog all apply. Tasks of size M, or ones that failed before, use best-of-N when that's switched on for the project.
3. When a task finally fails: setting "Stop the queue" (default) or "Skip tasks that depend on it and carry on". Failed and skipped tasks are clearly shown.
4. The queue is saved on disk and survives a restart of the tool or a reboot. On start, a job that was running is marked "Interrupted" and re-run from its last checkpoint.
5. Project page: progress per milestone ("Milestone 2: 3 of 5 tasks"), current task, time left estimate, Pause / Resume / Cancel, and "Don't start new tasks after" a time (e.g. 06:30).
6. After each task the tool checks AGENT_NOTES.md was updated and notes it in the job record if not.

Tests with the fake agent: dependency order, stop vs skip, restart recovery, the "don't start after" time, the notes check.
