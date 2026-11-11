# Upgrade 1a: Verify gate, part 1 (find and run the checks)

## Ground rules (same for every upgrade job)
- Work only in ~/agent-work/jobboard-u01a, a fresh copy of the current live Agent Jobs tool, on branch agent/jobboard-u01a. Never edit ~/agent-sandbox, never stop or restart any service (jobboard, basalt), never touch ~/basalt_config.json.
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

Goal: after the agent says it is done, the tool itself runs the project's build and tests. A job only shows "Done" when they pass. Part 1b adds automatic fix rounds.

1. Check config: an optional AGENT_CHECKS.json in the project root, e.g. {"checks":[{"name":"build","cmd":"./gradlew --offline assembleDebug","timeout_s":1200},{"name":"tests","cmd":"./gradlew --offline testDebugUnitTest","timeout_s":1200}]}. The new-job form gets an optional "Checks" box that writes it.
2. No file: detect checks and record what was detected. gradlew → offline assembleDebug + testDebugUnitTest; package.json with a test script → npm test (and npm run build if present); test_*.py / tests/ → python3 -m pytest -q (unittest if pytest isn't installed); Makefile with a test target → make test; Cargo.toml → cargo test --offline; *.sln/*.csproj → dotnet build + dotnet test. Nothing found → status "Done – not verified" (yellow), never green.
3. Run the checks inside exactly the same sandbox the agent uses (find how jobs are sandboxed and reuse it; checks must never get more access than the agent), in the job's work folder, with the timeout, saving exit code and output to one file per check.
4. Parse pass/fail/skip counts where possible: pytest summary line, unittest "Ran N tests", JUnit XML (Gradle build/test-results and others), jest/node summaries. Keep the raw output either way.
5. Save results in the job record as a list of {name, cmd, exit_code, passed, failed, skipped, seconds, output_file}. Make the review bundle's checks section use this record when it exists.
6. UI: the finished banner shows green "Checks passed (42 tests)", red "Checks failed: tests (3 failed)" with the first error lines, or yellow "Not verified". Add a "Run checks again" button.
7. Add a fake agent mode for tests and demos: a small script that follows a scenario file (edit/create/delete files, print log lines in the same format the real modes produce, tick PLAN.md steps, sleep, exit with a code). Later upgrades reuse it, so document the scenario format in docs/fake-agent.md.

Tests: detection for every project type (temp folders), timeout, count parsing (sample outputs in tests/fixtures), job record, banner text, a full fake-agent job that ends green and one that ends red.
