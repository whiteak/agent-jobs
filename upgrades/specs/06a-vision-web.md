# Upgrade 6a: Vision check, part 1 (web pages and the checking engine)

## Ground rules (same for every upgrade job)
- Work only in ~/agent-work/jobboard-u06a, a fresh copy of the current live Agent Jobs tool, on branch agent/jobboard-u06a. Never edit ~/agent-sandbox, never stop or restart any service (jobboard, basalt), never touch ~/basalt_config.json.
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

Goal: screenshots of the app are checked against the spec by the local model, which can see images.

1. New check type in AGENT_CHECKS.json: {"name":"looks right","type":"vision","target":"web","open":"index.html or http://127.0.0.1:<port>/","steps":[optional clicks/keys/waits],"spec":"what the screen must show","size":[1280,800],"mode":"advisory"}.
2. Web target: screenshots with Playwright headless Chromium. Find out whether Python or Node Playwright is already usable inside the sandbox; don't install anything; if neither is, stop and say so. Serve local files with a temporary local http server.
3. Ask the model: POST http://127.0.0.1:8080/v1/chat/completions with the screenshot (PNG, max 1280 px wide, base64 image_url) and a strict prompt: go through each requirement in the spec, say whether it is visible, then reply with JSON {"pass": true|false, "issues": ["..."], "confidence": 0-1}. Parse robustly (find the JSON block); no valid JSON after one retry → "Inconclusive".
4. The model varies run to run, so ask twice: pass only if both pass; if they disagree → "Inconclusive".
5. Vision checks run as part of the verify gate after the agent has stopped, so they never compete with the agent for the model.
6. mode "advisory" (default): shown on the job page and in the review bundle but doesn't fail the job. mode "required": a fail counts as a failed check and the issues go into the fix round prompt.
7. Job page shows each screenshot next to its verdict and issues; the bundle lists screenshot file names and issues.

Tests: mock the model server with canned replies (pass, fail, garbage, disagreeing pair), JSON parsing, image size handling. Put a manual test in AGENT_REPORT.md using ~/cline-test/index.html (Orb Chase).
