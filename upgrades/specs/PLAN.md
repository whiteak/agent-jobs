# Agent Jobs upgrades 1–10: build plan

Prepared 11 Nov 2026 while the PC was offline. Upgrades 11 (review bundle) and 12 (bench auto-runs) are already done, and the prompts build on them. The work runs in normal sessions, not Fable.

Who does what: Qwen writes all the code as Agent Jobs jobs. Claude checks the PC, fills in placeholders, queues each job, reviews it from its review bundle, tests it for real, and swaps it in once Jason says OK.

## Build order

| # | Job | Prompt | Needs first | Why here |
|---|-----|--------|-------------|----------|
| 0 | No-network jail wired into the runner | done by the "Block agent internet" thread | none | 1, 2 and 5 then inherit the jail and don't need retrofitting |
| 1 | 1a Verify gate: find and run checks, plus the fake agent | `prompts/01a-verify-checks.md` | 0 | Every later job is gated by it, and the fake agent makes 2–5 testable without the model |
| 2 | 1b Verify gate: automatic fix rounds and cheating guard | `prompts/01b-verify-retry.md` | 1a | |
| 3 | 2 Git checkpoints and automatic rollback | `prompts/02-git-checkpoints.md` | 1a, 1b | "Worse" means check results got worse |
| 4 | 3 Runaway watchdog and safety stop | `prompts/03-watchdog.md` | 1a (fake agent) | Needed before any long unattended run |
| 5 | Voice jobs 1 and 2 (other thread) | `agent-jobs/voice-job-prompts.md` | 3 live | Agreed with the voice thread; small UI change before the dashboard work |
| 6 | 7a Live dashboard: timeline and live log | `prompts/07a-dashboard-timeline-log.md` | 1–3 (events to show) | |
| 7 | 7b Live dashboard: diff viewer and check badges | `prompts/07b-dashboard-diff.md` | 7a | |
| 8 | 8 Approve / merge buttons | `prompts/08-approve-merge.md` | 7b, 1 | Uses the diff view and check results |
| 9 | 9a Phone layout and phone login | `prompts/09a-phone-layout.md` | 8 | Login needs the action secret from 8 |
| 10 | 9b Push notifications (ntfy) | `prompts/09b-push-notifications.md` | 9a | |
| 11 | 5 Best-of-N | `prompts/05-best-of-n.md` | 1, 2, 3 | |
| 12 | 4a Outline to plan | `prompts/04a-plan-from-outline.md` | 1 | |
| 13 | 4b Run the plan as a queue | `prompts/04b-run-plan-queue.md` | 4a, 1–3, 5 | This is the overnight runner, so all the safety pieces must be in first |
| 14 | 10 Morning report | `prompts/10-morning-report.md` | 4b, 9b | Reports on the overnight queue |
| 15 | 6a Vision check: web and checking engine | `prompts/06a-vision-web.md` | 1 | Last because it is the most fragile and RAM-hungry |
| 16 | 6b Vision check: Android | `prompts/06b-vision-android.md` | 6a | |

That is 15 jobs. At roughly 20–60 minutes of Qwen time each, plus review, expect about 4–6 evenings. Jobs 1–4 fit in the first evening or two.

## How each job runs

1. Make a fresh copy of the live tool: `~/agent-work/jobboard-uNN`, branch `agent/jobboard-uNN`. A job never edits the tool it is running inside.
2. Claude fills in any `{{...}}` placeholders and queues the prompt as an Agent Jobs job (Qwen mode, 256K context, 32K output).
3. Claude reviews the review bundle and tests the change by hand on port 8091.
4. If it needs fixing, a follow-up job goes to Qwen with Claude's review notes. Claude doesn't patch the code.
5. Once Jason says OK and no job is running, Claude backs up to `*.bak-YYYYMMDD-uNN`, swaps the copy into `~/agent-sandbox`, restarts the `jobboard` service only (never basalt), and runs the full test suite.
6. Real test on a live job: a small throwaway task in a scratch repo, run through the new gate, watchdog or best-of-N.

## Design notes

- **1. Verify gate:** checks come from `AGENT_CHECKS.json` or are detected automatically (gradle, npm, pytest, make, cargo, dotnet). Without checks a job ends yellow "Not verified", never green. Fix rounds default to 3. The cheating guard flags deleted tests, skip markers and changed check configs; Cline did exactly that on Orb Chase. Checks run in the agent's jail.
- **2. Checkpoints:** a snapshot is taken from a temporary git index each time a step is ticked, so the agent's files are never touched. Snapshots are tested in a separate worktree. Rollback happens only at gate points, never mid-edit. Bad attempts are kept as `agent/<job>-discarded-<k>`. There is a limit of 2 automatic rollbacks.
- **3. Watchdog:** it handles no progress for 15 minutes (nudge, then restart), loops seen 3 times, 85% of 256K context (fresh session with a handover), and a 4 hour limit. It never restarts Basalt. The safety stop kills the job on curl/wget/ssh/pip/push, any URL other than the model server, anything aimed at the Agent Jobs page itself, or access to ~/.ssh, ~/.config or browser profiles. It is a second layer behind the no-network jail (Cohere finding).
- **4. Planner and queue:** the planner writes `tasks.json` with testable acceptance criteria and dependencies. Jason edits and approves the plan on a page before anything runs. The queue survives a reboot, stops on failure by default, and can stop starting new tasks after a set time.
- **5. Best-of-N:** 3 sequential attempts in separate worktrees. Tests pick the winner (passes, then most tests, then fewest test edits, then smallest diff). It stops early if attempt 1 passes cleanly.
- **6. Vision check:** Playwright screenshots for web apps and an emulator or the phone for Android. Basalt is asked twice and must agree. It starts as **advisory only** until we see how often it raises false alarms. Windows apps can't run on Ubuntu, so Windows vision checks are deferred.
- **7. Dashboard:** live updates via Server-Sent Events, an event timeline, a readable live log, its own diff viewer and check badges. It uses no CDN or new libraries.
- **8. Approve/merge:** merges are local only, and pushing main stays manual. Merging is blocked unless checks passed ("Merge anyway" needs MERGE typed). The buttons need a secret the agent's jail can't read, so the agent can't approve its own work.
- **9. Phone:** a responsive layout. Phone access is off by default, and the phone needs a password login. Push notifications go through ntfy and carry titles and counts only, never code or logs.
- **10. Morning report:** facts come only from job records. An optional "Qwen's summary" is added when Basalt is idle. It has a "Needs you" list at the top, is made at 07:00, and sends a push notification.

## Check on the PC first (Claude, via Remote Control)

0. **CUDA version:** run `nvidia-smi` and record the driver and CUDA version (memory says 13.5, an older note said 13.4).
1. **Agent Jobs layout:** the `~/agent-sandbox` tree, how jobs are launched per mode (local, local-claude, supervised, Qwen), the job record and log format, where state lives, the test command and test count.
2. **The 11 and 12 code:** where the review bundle and bench runner live, so the prompts can point at them, and the bundle CLI/endpoint used for reviews.
3. **Jail status** (other thread): is `agent-jail.sh` wired in yet? If not, it goes first.
4. **Live jobboard = latest:** were 11 and 12 swapped in, and are any `agent/jobboard-*` copies left unmerged?
5. **Basalt free:** the 432 Hz and Orb Chase jobs are discarded and tuning is on hold, so only the jail and voice jobs share Basalt with these upgrades.
6. **Token usage in logs:** can the watchdog read context usage per mode? This decides how prompt 3 step 3 works.
7. **Can the agent reach 127.0.0.1:8090 from inside the jail?** It shouldn't be able to (needed for 8).
8. **For 6:** is Playwright usable inside the jail, does `/dev/kvm` exist, is there an emulator or system image in the SDK, and how much RAM is free with Basalt loaded (about 4.6 GB last check; the emulator needs about 1.5 GB). Fills in `{{ANDROID_DEVICE}}`.
9. **Tailscale or ntfy installed?** Fills in `{{PHONE_ACCESS}}` and `{{NTFY_SERVER}}`.

## Jason's choices (not needed until upgrade 9)

- **Phone access:** LAN only (works at home) or Tailscale (works from work too; a free app on PC and phone). Recommended: Tailscale.
- **Push server:** your own ntfy on the PC (private, needs Tailscale when away) or public ntfy.sh (easiest, but titles pass through a third-party server). Recommended: own ntfy with Tailscale.
- **Android vision device:** headless emulator (unattended, uses about 1.5 GB of RAM) or your Note10+ over Wi-Fi adb (only when it's on and paired). Recommended: emulator, if RAM allows.

## When Jason is home

1. Boot Ubuntu, open a terminal, run `cd ~ && claude remote-control`, keep it open and tap **Allow**.
2. Say "home" in this thread. Claude runs the checks above, then shows the first prompt (1a) for a quick OK before queuing it.
