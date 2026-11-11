# Upgrade 3: Runaway watchdog

## Ground rules (same for every upgrade job)
- Work only in ~/agent-work/jobboard-u03, a fresh copy of the current live Agent Jobs tool, on branch agent/jobboard-u03. Never edit ~/agent-sandbox, never stop or restart any service (jobboard, basalt), never touch ~/basalt_config.json.
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

Goal: stop jobs that waste hours or misbehave. It runs inside the job runner and checks every 15 s. All thresholds are settings with these defaults.

1. Thinking without acting: no tool call and no file change for 15 min while running → if the mode can take a new message, nudge: "Stop planning. Take the smallest next step now and run it." If it can't, or 10 min after the nudge, stop the agent and restart it once in the same folder with "You were restarted because you stopped making progress. Read PLAN.md and git log, then continue with the next unticked step." Max 2 restarts per job, then "Stopped: no progress".
2. Loops: the same command with the same output 3 times in a row, the same file written with identical content 3 times, or the same assistant message 3 times → the same restart, naming the loop in the restart message.
3. Context limit: find where each mode's log shows token usage (or the usage field in model responses). At 85% of the context size (setting, default 262,144 → about 223K) stop and restart with a fresh session and a handover message: the task, PLAN.md, this job's `git log --oneline`, `git diff --stat`, and the last error if any.
4. Time limit: job wall time over 4 h → stop, "Stopped: time limit".
5. Safety stop (most important). If the agent runs anything that reaches outside the machine or the project, stop the job at once (kill the whole process group), mark it red "Safety stop", leave the work folder untouched for review, highlight the command in the log and send the finish notification. Patterns: curl, wget, nc/ncat, ssh, scp, sftp, ftp, rsync to a host, telnet, git push / git remote add / git clone of a URL, pip/pip3/npm/yarn/apt/snap install, aws, ossutil, gsutil, az, gcloud, any http(s) URL except http://127.0.0.1:8080, anything aimed at 127.0.0.1:8090 (the Agent Jobs page itself), and reading or writing outside the work folder in places like ~/.ssh, ~/.config, ~/.mozilla, ~/snap, ~/.gnupg, browser profiles or keyrings. Normal work (./gradlew test, git commit, python3 -m pytest, curl to 127.0.0.1:8080) must not trigger it. Find where commands appear in the log for each mode and list in AGENT_REPORT.md which modes you could cover.
6. Model server health: if http://127.0.0.1:8080/v1/models doesn't answer for 2 min, pause the job ("Waiting for the model server") and notify. Never restart the model server.
7. Every watchdog action goes into the job record and timeline with its reason (the review bundle and morning report use it). Stopping always kills the whole process group (no orphans) and still lets checks, checkpoints and the bundle work afterwards.

Tests with fake agent scenarios: idle, nudge then restart, loop, token count over the limit, time limit, each safety pattern, no false alarm on the normal commands above, process group really killed (no child left running).
