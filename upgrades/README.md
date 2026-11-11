# agentjobs_upgrades

Standalone, tested logic for Agent Jobs upgrades 1–10 (stdlib only, Python 3.10+).
Written by Claude on 11 Nov 2026 before seeing the live Agent Jobs code, and still to be wired into
~/agent-sandbox/web once the PC is connected.

| Module | Upgrade | What it does |
|---|---|---|
| checks.py | 1a | AGENT_CHECKS.json / auto-detect, run in the jail (jail_wrap), parse test counts, banner text |
| guard.py | 1b | Cheating guard: deleted tests, skip markers, early returns, test config edits, count drop |
| verify.py | 1b | VerifyGate: fix rounds (default 3), fix prompt, timeline text, checkpoint hook |
| safegit.py | 2 | git with hooks/fsmonitor/pager/ssh off; optional jail wrap |
| checkpoints.py | 2 | Snapshots via temp index + worktree tests, auto-rollback at gates, manual rollback, cleanup |
| watchdog.py | 3 | Idle/loop/context/time rules, safety stop patterns, model-down pause, process-group kill |
| fakeagent.py | 1a | Scenario-driven fake agent for tests |
| events.py | 7a | Job event log (JSONL) + Server-Sent Events + status badges |
| diffparse.py | 7b | Unified diff parser, side-by-side rows, generated/binary detection |
| actions.py | 8 | Human-only secret + form token, merge / undo / reject, change-request prompt |
| auth.py | 9a | PBKDF2 passwords, sessions, lock-out, localhost bypass |
| notify.py | 9b | ntfy sender: quiet hours, retries, no code/paths in messages |
| bestofn.py | 5 | Winner selection and early stop |
| plan.py | 4a/4b | tasks.json schema + validation, dependency order, persistent queue (stop/skip, recover, cut-off) |
| report.py | 10 | Morning report from records only, optional labelled Qwen summary |
| vision.py | 6a | Vision request, robust verdict parsing, 2-vote rule |

Run tests: `python3 -m unittest discover -s tests -t .` (78 tests).

Upgrades 1-3 are WIRED into the live board (jobboard/web/upgrades.py, 11 Nov 2026; tests: jobboard/web/tests/test_u01.py).
Still needs the live code (wiring) for 4-10: job launcher hooks (call VerifyGate after the agent exits; feed
watchdog from each mode's log; CheckpointManager.on_plan_change from the PLAN.md watcher), routes and
pages in jobboard.py (dashboard, diff tab, buttons, login, settings, new project/plan review,
attempts tab, reports tab, phone CSS), Playwright/adb screenshot capture (6a/6b).
