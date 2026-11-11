# Upgrade 7b: Live dashboard, part 2 (diff viewer and check badges)

## Ground rules (same for every upgrade job)
- Work only in ~/agent-work/jobboard-u07b, a fresh copy of the current live Agent Jobs tool, on branch agent/jobboard-u07b. Never edit ~/agent-sandbox, never stop or restart any service (jobboard, basalt), never touch ~/basalt_config.json.
- No network. Do not pip/npm/apt install or download anything. Use the Python standard library and what the project already uses. If something you need is missing, stop and say so in AGENT_REPORT.md.
- Read before you change anything: list the files, read web/jobboard.py, web/jobstatus.py, the code that starts, sandboxes and tracks jobs, the review-bundle code and the tests. Then write PLAN.md with a short "Findings" section (where jobs are created, launched, sandboxed, stored and shown; the job record format; the exact test command) and a checkbox list of steps. Tick steps as you finish them.
- Put new logic in new, small modules with unit tests. Keep every existing feature (including voice buttons if present) and the current look working. Match the existing code style.
- Never delete, skip or weaken an existing test. Run the full test suite after each step; it must pass before you tick the step.
- Sandbox: anything that runs the project's own code (builds, tests, checks, screenshots, attempts) must go through the same jail the agent uses (agent-jail.sh --project <job dir> -- <cmd>, see the jail docs in the tool), never directly on the host. Any git command the tool itself runs on a job repo must use `git -c core.hooksPath=/dev/null` and must not trust that repo's own config (the agent could have planted hooks); pushing goes only through push-agent-branch.sh.
- Tests must never call the model server (127.0.0.1:8080) or start a real agent; mock them (use the fake agent once upgrade 1a adds it).
- Commit locally after each working step with a clear message. Do not push.
- To try the web page, run your copy on port 8091 (add a --port option if there isn't one) with its own state folder. Never use port 8090.
- Voice (merged after upgrade 3): keep every "# VOICE" block in jobboard.py, the data-voice and data-voice-read attributes, the static/voice.js script tag and the 5 voice routes working.
- UI text: short, plain, friendly English. No jargon on buttons.
- Finish with AGENT_REPORT.md: what changed (files), how to test it by hand click by click, test counts before and after, and anything unfinished or assumed.

Builds on 7a.

1. "Changes" tab per job: files changed since the job's base commit (including uncommitted), with +/- counts. Click a file to see its diff: unified view by default, side-by-side toggle, coloured added/removed lines, unchanged parts collapsed, "Next change" button. Refreshes while the job runs (on events or every 10 s).
2. Binary and generated files listed but not shown, using the same rules as the review bundle (reuse that code).
3. Pick a commit or checkpoint to see only its changes.
4. One badge per check (build ✓, tests 41/42 ✗) linking to its output; the same badges on the jobs list.
5. Large diffs render lazily; the page must stay responsive with 10,000+ changed lines.

Write the diff parser yourself (Python or plain JS); no CDN. Tests for the parser (new, deleted and renamed files, binary markers, "no newline at end of file") and the endpoint.
