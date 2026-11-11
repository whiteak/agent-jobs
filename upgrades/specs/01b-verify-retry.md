# Upgrade 1b: Verify gate, part 2 (automatic fix rounds)

## Ground rules (same for every upgrade job)
- Work only in ~/agent-work/jobboard-u01b, a fresh copy of the current live Agent Jobs tool, on branch agent/jobboard-u01b. Never edit ~/agent-sandbox, never stop or restart any service (jobboard, basalt), never touch ~/basalt_config.json.
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

Builds on 1a (checks runner, job record, fake agent).

1. When checks fail, automatically start a fix round in the same work folder and branch: a follow-up agent run in the same mode (resume the same session if the mode supports it, otherwise a fresh session) with this prompt: the original task in 2 lines; which checks failed; the first error block plus the last 120 lines of each failing check, trimmed to about 6,000 tokens in total; then "Fix the cause so these checks pass. Do not delete, skip or weaken tests, and do not change the check commands. Update PLAN.md and AGENT_REPORT.md." Then run the checks again.
2. Max fix rounds per job: a setting, default 3. After the last failed round the status is "Failed after 3 fix rounds" with the remaining failures.
3. Cheating guard: before the first fix round, record the test files (paths and hashes) and the test count. After each round, if tests were deleted, the test count dropped, skip/ignore markers were added (@Ignore, @Disabled, @pytest.mark.skip, unittest.skip, it.skip, xit, test.skip, describe.skip, an early return at the top of a test) or AGENT_CHECKS.json/build test config changed, mark the job orange "Needs review", do not count it as a pass, and list exactly what changed.
4. Job page timeline line, e.g. "Agent finished → checks failed (2) → fix round 1 → checks passed".
5. Per-job "Don't verify" option (off by default) for jobs that can't have tests, such as writing docs. Those end yellow "Not verified".
6. Keep every round's results in the job record (rounds list); the review bundle shows the rounds.

Tests with the fake agent: passes first time; fails then gets fixed; fails 3 times; deletes a test; adds a skip marker; "Don't verify".
