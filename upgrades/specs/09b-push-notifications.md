# Upgrade 9b: Push notifications to the phone

## Ground rules (same for every upgrade job)
- Work only in ~/agent-work/jobboard-u09b, a fresh copy of the current live Agent Jobs tool, on branch agent/jobboard-u09b. Never edit ~/agent-sandbox, never stop or restart any service (jobboard, basalt), never touch ~/basalt_config.json.
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

Use ntfy: the tool sends a plain HTTP POST to a configurable ntfy server + topic, and the ntfy app on Jason's phone shows it. Server default: empty (= off). Claude fills in {{NTFY_SERVER}} after Jason chooses.

1. Settings: server URL, topic (pre-filled with a random 20-character string so it can't be guessed), optional access token, which events to send, "Send test notification".
2. Events: job finished (✓/✗ with the checks summary), waiting for your approval, safety stop (high priority), bench set finished with its overall verdict, morning report ready, model server down. Title plus 1–2 lines, plus a click link to the page if phone access (9a) is on.
3. Never put code, diffs, log text or file paths in a notification: only titles, status and counts.
4. The tool sends notifications, never the agent; sending runs outside the agent sandbox. Failed sends are logged and retried 3 times and never hold up a job.
5. Quiet hours (default 22:00–07:00 local) except safety stops.

Tests: mock HTTP; message content (no log text), quiet hours, retries, settings validation.
