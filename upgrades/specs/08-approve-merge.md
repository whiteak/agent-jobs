# Upgrade 8: Approve / merge buttons

## Ground rules (same for every upgrade job)
- Work only in ~/agent-work/jobboard-u08, a fresh copy of the current live Agent Jobs tool, on branch agent/jobboard-u08. Never edit ~/agent-sandbox, never stop or restart any service (jobboard, basalt), never touch ~/basalt_config.json.
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

Goal: Jason looks at a finished job and decides with one click.

1. A finished job's page gets "Approve & merge", "Ask for changes" and "Reject".
2. Approve & merge merges the job branch into the branch the job started from, in that project's own work copy; the button says which ("Merge into main"). Fast-forward when possible, otherwise a merge commit "Merge agent job <id>: <title>". Only enabled when checks passed. If checks failed, weren't run, or the job needs review, the button is grey and a "Merge anyway" link asks you to type MERGE. On conflicts: abort, leave everything exactly as it was, and offer "Ask Qwen to fix the conflict" (a follow-up job that merges the target into the job branch and resolves it, then goes through the normal checks).
3. Ask for changes: a text box that starts a follow-up job on the same branch with that text plus the original prompt (reuse the existing follow-up feature if there is one).
4. Reject: marks the job rejected, renames the branch to agent/rejected/<id>, keeps the folder; "Undo" for 14 days.
5. Pushing: keep the rule that the tool only ever pushes agent/... branches. Merging into main is local only; show "Not pushed" and leave pushing main to Jason.
6. Security (important): these actions must only come from a person using the page, never from an agent job running on the same PC. Require POST plus a per-session secret (HttpOnly cookie + matching hidden form token) kept in a file outside every folder the agent sandbox can read. Reject anything without it. Explain in AGENT_REPORT.md how you checked the sandbox can't read the secret.
7. Every decision goes into the job record and timeline. "Undo merge" for the last merge if nothing was committed on top since (reset that branch to the recorded pre-merge commit).

Tests with temp repos: fast-forward, merge commit, conflict (nothing changed after the abort), merge blocked when checks failed, "Merge anyway", request without the secret rejected, reject and undo, undo merge.
