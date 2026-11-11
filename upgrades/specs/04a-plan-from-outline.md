# Upgrade 4a: Requirements → plan

## Ground rules (same for every upgrade job)
- Work only in ~/agent-work/jobboard-u04a, a fresh copy of the current live Agent Jobs tool, on branch agent/jobboard-u04a. Never edit ~/agent-sandbox, never stop or restart any service (jobboard, basalt), never touch ~/basalt_config.json.
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

Goal: Jason pastes an outline of an app, and the tool turns it into milestones and small tasks that he can check and edit before anything runs.

1. New "New project" page: name, type (Android app / Windows app / web app / other), outline box, and either an existing project folder or "Start new" (creates ~/agent-work/<name> with git init on branch agent/<name>).
2. "Make a plan" starts a planning job (a normal job whose prompt says: do not write app code). The planner writes ROADMAP.md (milestones in plain English) and tasks.json: [{id, milestone, title, prompt, acceptance: [testable criteria], checks: [optional commands], depends_on: [ids], size: "S"|"M", screen: "what the screen should look like (UI tasks only)"}]. Define the schema in docs/tasks-schema.json. Planner rules: each task finishable in about 45 minutes; task 1 sets up the project skeleton, the test setup and AGENT_CHECKS.json; every task has testable acceptance criteria.
3. Validate tasks.json (schema, unique ids, no missing or circular dependencies). If invalid, send the errors back to the planner once automatically.
4. Plan review page: milestones as collapsible sections, tasks as cards with editable title, prompt and criteria; up/down buttons to reorder; delete; add task; "after: …" shows dependencies. "Save" and "Approve plan". Nothing runs until approved.
5. Keep the planner prompt in prompts/planner.md so it can be improved later.

Tests: schema validation (good and bad samples, cycles), new project folder creation, plan page endpoints, a canned planner output via the fake agent.
