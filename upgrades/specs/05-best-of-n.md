# Upgrade 5: Best-of-N for hard steps

## Ground rules (same for every upgrade job)
- Work only in ~/agent-work/jobboard-u05, a fresh copy of the current live Agent Jobs tool, on branch agent/jobboard-u05. Never edit ~/agent-sandbox, never stop or restart any service (jobboard, basalt), never touch ~/basalt_config.json.
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

Builds on 1 (checks, fix rounds), 2 (checkpoints, worktrees) and 3 (watchdog).

1. Job option "Hard: try 3 times" (N setting 2–4, default 3). Also: when a job ends "Failed after fix rounds", offer a "Try 3 fresh attempts" button (setting to start it automatically).
2. Each attempt starts from the same base commit in its own git worktree and branch agent/<job-id>-try<k>, one after another (one GPU), with the normal checks, fix rounds and watchdog. Attempts never see each other.
3. Winner chosen automatically: (a) checks pass with no "Needs review" flag; then (b) most tests passing; then (c) fewest test files changed; then (d) smallest diff; then (e) fastest. Record every attempt's scores.
4. The winner's commits go on the job branch. Losing attempts stay as branches for 7 days and appear on an "Attempts" tab with their results and changes, with "Use this one instead".
5. Show the cost up front ("about 3× longer") and progress "Attempt 2 of 3".
6. Stop early (default on): if attempt 1 passes cleanly and the job wasn't marked hard, don't run more.

Tests with the fake agent (a different scenario per attempt): each tie-break, early stop, all attempts fail, "Use this one instead".
