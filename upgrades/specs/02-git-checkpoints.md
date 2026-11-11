# Upgrade 2: Git checkpoints and automatic rollback

## Ground rules (same for every upgrade job)
- Work only in ~/agent-work/jobboard-u02, a fresh copy of the current live Agent Jobs tool, on branch agent/jobboard-u02. Never edit ~/agent-sandbox, never stop or restart any service (jobboard, basalt), never touch ~/basalt_config.json.
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

Builds on 1a/1b (checks runner, fix rounds, fake agent).

1. Snapshot without disturbing the agent: whenever a PLAN.md step gets ticked (the tool already watches PLAN.md), snapshot the work folder into a commit object without touching the agent's files, index or branch: use a temporary index (GIT_INDEX_FILE), `git add -A` into it, `git write-tree`, `git commit-tree` with the branch head as parent. Run the checks on that snapshot in a separate `git worktree` inside the job's state folder, sandboxed exactly like 1a. If the previous snapshot's checks are still running, skip this one.
2. Snapshot passes → it becomes a checkpoint: ref refs/agent/<job-id>/ckpt-<n> plus a job record entry {n, step text, commit, passed, failed, time}.
3. "Getting worse" is judged only at gate points (end of the agent run, end of each fix round): the result is worse than the best checkpoint if the build passed before and fails now, or fewer tests pass. Then automatically: save the current state as branch agent/<job-id>-discarded-<k>, reset this job's work folder to the best checkpoint (never touch any other branch or folder), and start the next fix round with "Your last changes made things worse (<what>). They were rolled back to the checkpoint after step <n>. Try a different approach." Max 2 automatic rollbacks per job, then stop as "Stuck – needs a look".
4. On success the job branch holds normal commits; checkpoint commits use the message "checkpoint: step <n>: <step text>" so the history reads well.
5. Job page: checkpoint list (step, time, tests passing) with "View changes from previous" and "Roll back to here" (confirm dialog; only when the job isn't running).
6. Clean-up: worktrees removed when the job ends; a "Clean up" button deletes discarded branches and checkpoint refs older than 14 days.
7. The review bundle lists checkpoints and rollbacks.

Tests with temp repos and the fake agent: a snapshot leaves `git status`, the index and file mtimes unchanged; checkpoint on a passing step; rollback when the test count drops; rollback limit; manual roll back.
