# Upgrade 9a: Phone-friendly layout and safe phone access

## Ground rules (same for every upgrade job)
- Work only in ~/agent-work/jobboard-u09a, a fresh copy of the current live Agent Jobs tool, on branch agent/jobboard-u09a. Never edit ~/agent-sandbox, never stop or restart any service (jobboard, basalt), never touch ~/basalt_config.json.
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

1. Responsive layout: under 700 px wide, one column, 44 px tap targets, a sticky top bar with the current job status, jobs as cards, diff viewer in unified mode, timeline showing the latest 10 events with "Show all". Check it at 360×800 and 412×915 (Galaxy Note10+).
2. Phone access is OFF by default (keep listening on 127.0.0.1 only). Add a setting "Allow phone access" with a listen address ({{PHONE_ACCESS}}, filled in after Jason chooses LAN or Tailscale) that applies on the next restart, and show the address to open on the phone.
3. Requests that don't come from 127.0.0.1 must log in: a password set on the settings page (store only a salted PBKDF2 hash), session cookie (HttpOnly, SameSite=Strict, 30 days), 5 minute lock-out after 5 wrong tries. Approve/merge/reject and settings still need the secret from upgrade 8.
4. Add a web app manifest and icon so Chrome on Android can "Add to Home screen".

Tests: login, lock-out, localhost needs no login, remote requests without login rejected, viewport meta present, no fixed widths over 360 px in the main containers.
