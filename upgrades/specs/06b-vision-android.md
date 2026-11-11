# Upgrade 6b: Vision check, part 2 (Android)

## Ground rules (same for every upgrade job)
- Work only in ~/agent-work/jobboard-u06b, a fresh copy of the current live Agent Jobs tool, on branch agent/jobboard-u06b. Never edit ~/agent-sandbox, never stop or restart any service (jobboard, basalt), never touch ~/basalt_config.json.
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

Builds on 6a. The Android SDK is at ~/agent-sandbox/state/android-sdk.

1. Target "android": install the debug APK (path from the check, or found under app/build/outputs/apk/debug), find and launch the main activity (`cmd package resolve-activity --brief`), wait until the screen is stable (two screenshots 1 s apart identical, max 20 s), take `adb exec-out screencap -p`, run optional steps (`input tap/text/keyevent`) with a screenshot after each.
2. Device: {{ANDROID_DEVICE}} (Claude fills this in after checking: either a headless emulator AVD Claude has created, started by the check with `-no-window -gpu swiftshader_indirect -no-snapshot-save -no-audio -memory 1536` and stopped afterwards, or an adb device that's already connected). Never start an emulator while an agent job or bench set is using the model, and always stop it afterwards: RAM is tight.
3. Then the same model check as 6a.
4. Clear errors: "No device", "APK not built", "App crashed on start" (detect with pidof + logcat crash lines, and include the crash lines in the result).
5. Add check type "android-smoke" (installs, starts, no crash for 10 s, no model needed), required by default for Android projects.

Tests: a fake adb script on PATH for install, launch, crash detection, stable-screen wait and steps.
