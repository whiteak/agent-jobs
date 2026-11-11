# Job 04 (re-run) — Upgrade 9a: phone-friendly layout and safe phone access

## Findings

- Nothing of 9a exists yet: the branch ends at U08 job 03 (`2619db1`), `git log` has no
  `U09a` commit, and `grep -rn "U09a" web/` only hits `agentjobs_upgrades/auth.py`'s own
  docstring. `notifycfg.py` is U09b (ntfy alerts) and already has a settings page (`/notify`,
  `header()` links it as "Phone"), so 9a is layout + access + login + manifest, from zero.
- Pages: one HTML shell, `jobboard.PAGE` (1324), filled by `render(body)` (`@@BODY@@`).
  The viewport meta is already there; the base CSS lives inside PAGE and every module appends
  its own `<style>` in the body (`upgrades.CSS`, `badges.CSS`, `diffcard.CSS`,
  `dashboard.CSS`, `taskqueue.CSS`, `planning.CSS`, `jailcfg.BADGE_CSS`) — so a phone block
  appended in the head loses to a later module rule of the same specificity. Overrides that
  touch another module's classes go in that module's own CSS.
  Only fixed widths over 360 px in the whole tree: `.jobtxt{max-width:420px}` and
  `.wrap{max-width:980px}` (a desktop cap); everything else is ≤340 px
  (`.clamp` 340, `.act` 300, `td.st`/`.row>div`/`select.chref` 220, `img.shot` 230/46vw).
  There is one `@media (max-width:640px)` block already.
- The jobs list is `job_rows()` → `<tr>`s in a 7-column table whose `<tbody id=jobrows>` the
  3 s poller replaces wholesale from `/api/rows`. Same markup on both paths, so "jobs as
  cards" has to be CSS (label each cell with `data-l` and use `::before`), not a second HTML
  variant — a width-sniffing server render would break the poller anyway.
- The server (`# JAIL` untouched): `HOST, PORT = "127.0.0.1", JOBBOARD_PORT or 8090`;
  `main()` binds `(HOST, PORT)`; `H.guard()` demands `Host` ∈ {`127.0.0.1:PORT`,
  `localhost:PORT`}, the `jb=` token cookie (`?t=` hands it out once) and, for POSTs,
  `Origin` ∈ the same two. `PORT` is read per request (tests patch it), `HOST` only at bind
  time. Phone access therefore needs three things: a bind address, a host/origin allowlist
  that includes it, and a login for anything that is not 127.0.0.1.
- The login logic is given and tested (`agentjobs_upgrades/auth.py`, `tests/test_rest.py`):
  `hash_password`/`verify_password` (PBKDF2-HMAC-SHA256, 310 000 iters, random 16-byte salt),
  `is_local`, `Lockout(5 tries → 300 s)`, `Sessions` (ids stored hashed in one JSON file,
  0600, 30 days, `revoke_all`), `cookie_header` (`HttpOnly; SameSite=Strict; Max-Age=30d`) and
  `needs_login(remote, sid, sessions)`. Not reimplemented here — `phoneaccess.py` is the glue.
- "settings still need the secret from upgrade 8": `decisions.allowed(method, cookie, tok)`
  (the `aj` HttpOnly session cookie + the `ajtok` HMAC made from
  `~/.local/state/agentjobs/secret`, which no jail mounts). Only `GET /job/<id>` hands that
  session out today (`decisions.session_for`), so the two settings pages have to do the same
  before their POSTs can be gated on it. No test covers `POST /notify` today; the decide
  tests (`test_u08_decide.py::Pages`) keep working because `/job` and `/decide` are untouched.
- Timeline: `dashboard.timeline_card` renders the newest **300** rows and a U07 test pins both
  that count (`len(re.findall(r"<li ", h)) == 300`) and the card's exact empty HTML. So
  "latest 10 + Show all" must be CSS (`:nth-last-child`) + one button, not fewer rows server-side.
- Diff viewer: `diffcard.JS` is already unified by default (`sbs=false`, "Side by side" is
  opt-in) — on a phone the switch just has to be unreachable and a side-by-side table opened
  on a wide screen must fall back to unified after a resize.
- No browser here (job 09/10 findings: no Playwright, no Chromium, nothing installable), so
  360×800 / 412×915 is checked statically (CSS assertions on the rendered pages); the real
  device look stays Jason's, and the report says so.
- Sandbox has only `lo` (`ip -4 addr` → nothing, `/sys/class/net` → `lo`), so a test cannot
  connect through a second interface; the remote-address seam is `H.remote()` and tests patch it.
- Test command (`AGENT_CHECKS.json`, the verify gate): `cd web && PYTHONDONTWRITEBYTECODE=1
  python3 -W ignore -m unittest discover -s tests`. Baseline before my changes: **741 tests, OK**.

## Steps

- [ ] PLAN.md (this file) committed before the code.
- [ ] `web/phoneaccess.py` (new glue, `# U09a`): `state/access-settings.json` (0600) holding
      `phone_access` (off), the listen address (default the `{{PHONE_ACCESS}}` placeholder to
      fill in after Jason picks LAN or Tailscale) and only a salted PBKDF2 hash of the
      password; `bind_host` (applies on the next restart), the address to open on the phone,
      login/logout with `auth.Lockout` + `auth.Sessions`, the Access settings page and its form
      validation, and the host/origin names the board answers when phone access is on.
- [ ] `web/phonelayout.py` (new, `# U09a`): the ≤700 px stylesheet (one column, 44 px tap
      targets, sticky top bar, jobs as cards, no container wider than 360 px), the web app
      manifest, and the 192/512 icons drawn with stdlib `zlib`/`struct` (no image library here).
- [ ] `jobboard.py` hooks (`# U09a`): `ACCESS_FILE`, `H.remote()`, the guard's host/origin
      allowlist + login gate (`/login` GET+POST, 403 for a POST from an anonymous remote,
      lock-out after 5 wrong tries), `/access` (read on the page, changes need the U08 secret
      token like the decide buttons), `header()` sticky status bar + nav, `job_rows()` labelled
      cells, the manifest/icon routes, and `main()` binding the chosen address and printing the
      phone link.
- [ ] Timeline "Show all" in `dashboard.timeline_card` + the unified-only diff viewer in
      `diffcard` + the live top-bar status in the two page scripts.
- [ ] Tests `web/tests/test_u09a_phone.py`: phone access off by default (bind stays
      127.0.0.1, `{{PHONE_ACCESS}}` counts as unset), password hash (no plaintext on disk),
      login + 30-day HttpOnly/Strict cookie, localhost needs no login, a remote request
      without a session gets the login page and its POSTs get 403 without touching a job,
      lock-out after 5 wrong tries then recovery, a changed password signs phones out,
      the manifest + icons + viewport meta, and the CSS: one column, 44 px targets, sticky
      bar, cards, no fixed width over 360 px in the main containers, timeline "Show all",
      diff unified.
- [ ] Full suite green with the gate's command; PLAN ticked; AGENT_REPORT.md section for this
      job (click-by-click phone test at 360×800 / 412×915 and what must be checked on a real
      phone).

---

# Job 03 (re-run) — Upgrade 8: approve / merge buttons

## Findings

- The engine is already written and tested: `web/agentjobs_upgrades/actions.py` —
  `load_secret` (file, dir 0700 / file 0600), `form_token` = HMAC(secret, "form:"+sid),
  `check_request` (POST + cookie session + matching form token), `merge_allowed`
  (`MERGEABLE = {"passed"}`, else only a typed `MERGE`), `Decisions.merge` (clean tree
  first; fast-forward when the target is an ancestor of the job branch, otherwise a
  `Merge agent job <id>: <title>` commit; on a conflict `merge --abort` + `reset --hard`
  back to the recorded target sha and the caller's branch is restored in `finally`),
  `Decisions.undo_merge` (refuses when the target moved past the recorded `after`),
  `Decisions.reject`/`unreject` (renames to `agent/rejected/<id>`, 14 days),
  `change_request_prompt` (the request + the original prompt's first 15 lines).
  `tests/test_rest.py::Actions` already covers ff / merge commit / conflict-leaves-nothing /
  reject+undo / the token / the gate. **What is missing is all the glue**: no page buttons,
  no routes, no branch choice, no follow-up jobs, no record/timeline entries, nothing
  that ever calls `check_request`.
- Pages and routes: `web/jobboard.py` (2237 lines). `job_page(jid)` (1515) is a stack of
  cards; `can_act = not active and not READONLY and not folder_busy(folder)` is the guard
  every finished-job button uses; `H.do_GET`/`H.do_POST` (1876/1994) are `if` chains behind
  `H.guard()` (Host must match `jobboard.PORT`, `jb=` cookie, Origin check for POSTs).
  Buttons post to `/push/<jid>`, `/recheck/<jid>`, `/followup/<jid>` … — so the new
  decision routes fit the same shape.
- Where board git runs on a project: always `upgrades.git_for(m, root)` =
  `SafeGit(root, wrap=make_wrap(root, folder, "agentgit-<jid>"))`. `SafeGit` adds
  `-c core.hooksPath=/dev/null -c core.fsmonitor=false -c protocol.allow=never
  -c credential.helper= …` plus `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`, so the
  repo's own (agent-planted) config and hooks are not trusted; the wrap puts the whole
  call inside the job's own jail (`jail/run-local.sh sandbox-run` → `podman-local.sh`:
  `--network none`, only the project folder mounted). `JOBBOARD_JAIL=host` (tests) runs it
  straight on the temp folder. Pushing is only ever `jailcfg.push_branch` →
  `jail/push-agent-branch.sh`, and it refuses anything that is not
  `agent/...` (`BRANCH_RE`), so "merge into main" staying local is already the rule.
- Branch names in a job: base = `m["git_branch_start"]` (written by `record_git_start`,
  jobboard.py:619, before the agent runs); a best-of-N winner is put on `agent/<job id>`
  (`attempts.job_branch`); a normal agent commits on a branch it made itself
  (`agent/<task>`), and 4a projects start on `agent/<folder>` (`planning.branch_of`).
  So the job branch has to be *found*: the folder's current `agent/...` branch, else
  `agent/<jid>`, else the newest `agent/...` ref that has commits the target does not.
- The checks gate: `m["verify"]["status"]` ∈ passed / unverified / needs_review / stuck /
  failed / aborted (from `verify.VerifyGate`, shown by `upgrades.overlay_status`), and
  `m["verify_off"]` / no `verify` at all = "weren't run". Only `passed` is mergeable
  without typing MERGE.
- Timeline: `events.ICONS` already has `approved` ✅ / `merged` 🔀 / `rejected` 🗑, so
  decision rows land in the U07 timeline with an icon and no new kinds.
- Follow-up: `/followup/<jid>` (jobboard.py:2045) needs `prev["session"]` and carries the
  U01 fields — "Ask for changes" reuses `new_job(mode, folder, prompt, offline,
  resume=session, parent=jid, extra=carry)`; a conflict-fix job is the same call with a
  different prompt (works without a session too).
- The secret and the jail (checked, not assumed): `jail/podman-local.sh jail_local_args`
  mounts **only** the job's project folder (`/work/<name>` over an empty tmpfs `/work`),
  `state/home-local` → `/home/agent`, `state/android-sdk` → `/opt/android-sdk` and two jail
  scripts; `jail/agent-jail.sh` (bwrap) gives a fake empty `/home/agent` and refuses
  `$HOME/.local*` as a project folder. A secret at `~/.local/state/agentjobs/secret`
  (actions.DEFAULT_SECRET) is therefore never inside the container. I add
  `.local/state/agentjobs` to `watchdog.SECRET_PATH_RE` so a job that goes looking for it
  is a safety stop, and a test that reads the jail script's own `-v` mounts.
- Test command (`AGENT_CHECKS.json`, the verify gate): `cd web && PYTHONDONTWRITEBYTECODE=1
  python3 -W ignore -m unittest discover -s tests`. Baseline before my changes: **635 tests,
  OK**.

## Steps

- [x] PLAN.md (this file) committed before the code.
- [x] `web/decisions.py` (new glue, `# U08`): the secret file + session cookie + form token
      (`decide_allowed`), which branch a job worked on and which it merges into, the Decide
      card (Approve & merge / Ask for changes / Reject + Merge anyway + Undo + Ask Qwen to fix
      the conflict), and `action()` doing merge / undo-merge / reject / undo-reject /
      changes-requested / fix-conflict through `actions.Decisions`, writing the record
      (`decision_log`) and the timeline. (`2212f33`; the guard is `cannot_decide`, not
      `jobboard.check_record` — see the re-run step)
- [x] `jobboard.py` hooks: `import decisions`, the decision card on the job page, the `aj=`
      HttpOnly cookie on `GET /job/<id>`, and the POST routes that refuse anything without
      the secret token. (`c27136c`)
- [x] `watchdog.SECRET_PATH_RE`: `.local/state/agentjobs` is a private location. (`cb1346a`)
- [x] Tests `web/tests/test_u08_decide.py`: temp repos — fast-forward, merge commit,
      conflict leaves nothing and offers the fix job, merge blocked when checks failed,
      "Merge anyway" needs the typed word, the request without the cookie/token is rejected
      (and a GET is), reject + undo (+ the branch rename), undo merge and undo refusing when
      something was committed on top, the follow-up jobs' prompts, the card states, the
      record + timeline rows, the "Not pushed" rule, and that the jail never mounts the
      secret. (`affca74`, 84 tests — the commit message says 97; the file has 84 test functions)
- [x] Re-run (12 Nov): the work above was sitting uncommitted in the shared copy — committed it
      as three commits and ran the live copy on 8091, which caught one real bug: `/decide/<id>`
      gated on `jobboard.check_record` (the worker's "could I *start* this record" check), so a
      press on a finished job whose record fails that check for an unrelated reason did nothing
      at all — no merge, no record, no word on the page. `decisions.cannot_decide` is the check
      that fits a decision, and a press the board will not take now comes back with its word on
      the card; a rejected card offers Undo only. 9 more tests. (`24b06ad`)
- [x] Full suite green with the gate's command; PLAN ticked; AGENT_REPORT.md section for
      this job (including how the sandbox can't read the secret).
      `Ran 741 tests in 280.372s — OK` (measured 732 before my 9 tests, 741 after;
      741 = 648 everywhere else + 93 in `test_u08_decide.py`).

---

# Job 01 — Upgrade 7a: live dashboard, part 1 (timeline + live log)

## Findings

- Already here (WIP `9d3683f` from `agent/jobboard-u07`, merged into this shared copy; `656742c`
  and `b433423` fixed its own broken bits): `web/dashboard.py` = `timeline_for`/`tl_add`
  (`JOBS/<id>/timeline.jsonl` through `agentjobs_upgrades.events.JobEvents`), `job_stream()`
  (SSE: `retry`, `job` events with ids, `log` batches with byte offsets, `status`, keepalive,
  `end`), `badge`/`checks_cell`, the timeline + live-log cards and their CSS; routes
  `/api/jobs/<id>/events` (`H.sse_job`, jobboard.py:1733, honours `Last-Event-ID` **and**
  `?log=`/`?last=`) and `/api/raw/<id>` (last 8 MB); the job page's live-log JS (rows by kind,
  10 lines + "Show more", search, follow/pause) and the Checks column in the jobs list.
  `web/tests/test_u07_dashboard.py` already covers the SSE format, Last-Event-ID resume, the
  log parser on the three real logs in `web/sample-jobs` and the badges. So this job is
  "audit, finish, test, smoke, report", not a rewrite.
- How the timeline gets filled: `upgrades.EventLog.emit` also calls `dashboard.tl_add` using
  `dashboard.TL_KIND`, so everything upgrades 1–3 already emits lands there — `checks`,
  `fix_round`, `needs_review` (from `verify.VerifyGate` via `upgrades.gate_emit`),
  `checkpoint`/`checkpoint_skipped`/`checkpoint_error`/`rollback` (from `CheckpointManager`),
  `watchdog`/`safety_stop` (from the watchdog, upgrades.py:497-505), `guard`, `restart`;
  `queued`/`started`/`step`/`finished` come from `process_job`, and 4a/4b/5 call
  `dashboard.tl_add` directly for `plan`/`queue`/`attempt`. `checks_start` is deliberately not
  a timeline row. `verify["timeline"]` (a one-line summary string) is a different, older thing
  and stays untouched.
- Real gaps found: `events.ICONS` has no icon for the `attempt`/`plan`/`queue` rows those
  modules add, so they fell back to "•"; nothing asserts that every kind the spec names gets a
  row with icon/time/colour, that the page keeps the **polling fallback** when the stream
  drops, or that a real fake-agent job writes the timeline (the U01 gate path is asserted
  against `events.jsonl` but never against `timeline.jsonl`); the `.hit` CSS rule is dead.
- Live-log rows: `jobboard.events()` classifies each line (`k` = text/think/tool/result/gate/
  final/log, `cat` = cmd/edit/read/other through `tool_cat`), sends long results whole up to
  20 000 chars (the page collapses to 10 lines) and returns the next byte offset so a reload
  or a reconnect resumes where it left off.
- Private copy: `JOBBOARD_PORT/JOBS/WORK/PLANS/QUEUES/SETTINGS/NOTIFY` put everything in my own
  folders (`HOME` decides `SANDBOX`, where the web token goes); `JOBBOARD_FAKE=1` +
  `JOBBOARD_JAIL=host` runs the fake agent. Never port 8090; 8091 is mine.
- Test command (`AGENT_CHECKS.json`, the verify gate): `cd web && PYTHONDONTWRITEBYTECODE=1
  python3 -W ignore -m unittest discover -s tests`. Baseline before my changes: **635 tests,
  OK** (270 s).

## Steps

- [x] PLAN.md (this file) committed before the code.
- [x] Run the suite as it stands (the 7a WIP is already merged) and record the count.
      `Ran 635 tests in 270.529s — OK`.
- [x] Timeline polish: icons for the `attempt`/`plan`/`queue` rows, remove the dead `.hit` rule.
      (`f66d147`)
- [x] Tests in `web/tests/test_u07_dashboard.py`: a row with icon, time and colour for every
      timeline kind the spec names (queued, started, step ticked, checks pass/fail, fix round,
      checkpoint, rollback, watchdog, safety stop, finished, restart, needs review) + the whole
      `EventLog` kind → timeline mapping; event text is escaped; `finished_text`; the board's
      own `agentjobs` lines and unparsable lines in the live log; the page keeps the polling
      fallback and the collapse/follow wording. (`f66d147`, 11 tests)
- [x] End-to-end proof: a fake-agent job (child process, temp folders, `JOBBOARD_JAIL=host`)
      writes queued → started → step ticked → checks → finished into `timeline.jsonl`, and the
      real `/api/jobs/<id>/events` route replays them with ids and the log offset.
      (`f66d147`, `ARealJobFillsIt`)
- [x] Re-run (12 Nov): audit what 7a still owed. One real bug, found by the 8091 smoke run —
      the `started` row cut the `MODES` label at its colon, so the timeline read
      "Started (Local" / "Started (Fake agent (tests only". `AGENT_NAMES` + `started_text()`
      and 1 test plus 1 end-to-end assertion. (`28e8795`)
- [x] Full suite green with the gate's command. `Ran 732 tests in 283.280s — OK`
      (731 before my change: 635 at `cb1346a` + 96 from job 03's uncommitted U08 tests).
- [x] Smoke run: private copy on `127.0.0.1:8091` (own state folders, fake agent) — job page,
      the live SSE stream watched as it grows, Last-Event-ID resume, raw log, list badges.
- [x] AGENT_REPORT.md section for this job; HANDOFF-7a.md marked resolved.

---

# Job 11 — Upgrade 6b: vision check, part 2 (Android)

## Findings

- What 6a already gives me (`web/visioncheck.py`, `# U06`): `run_check(project, check,
  out_dir, *, wrap, model_url, emit, capture_fn, call, votes, budget)` does everything
  except taking the picture — it saves the PNGs into `JOBS/<id>/checks/round-N/`, sizes
  them to ≤1280 px, writes `check-<name>.log`, asks the model `votes` times through the
  tested engine `agentjobs_upgrades/vision.py`, and returns the dict `checks.run_all`
  wants (`name/cmd/type/mode/ok/exit_code/verdict/issues/screenshots/passed/failed/
  skipped/output_file/seconds`). A `capture_fn(project, spec)` returning
  `{"shots": [(name, png)], "url", "steps", "log", "error"}` is the whole contract, so an
  Android capture drops straight in — requirement 3 ("the same model check as 6a") comes
  free. The model is only asked when a PNG came back.
- `visioncheck.check_ok()` rejects `target != "web"` on purpose and the 6a test asserts
  that, so Android entries get their own validator (`androidcheck.check_ok`) and
  `visioncheck.check_ok` is left alone.
- Runners are registered in two places only: `jobboard.run_gate()` (jobboard.py:462) and
  the "Run checks again" thread in `jobboard.u01_button()` (jobboard.py:2153), both as
  `extra_runners=visioncheck.runners(...)`. `checks.run_all` skips a non-command check
  with no runner silently; `summarize()` only fails a job over results whose
  `mode != "advisory"` — which is how "no device" can be reported without failing a job.
- Check types/records: `checks.Check` already has `type` + `extra` and its comment names
  `"command" | "vision" | "android-smoke"`. `upgrades.checks_ok()`,
  `upgrades.freeze_checks()` and `job_checks()` know `command` and `vision` only.
  `detect_checks()` returns two checks for a `gradlew` project and
  `test_checks.py::test_gradle` pins exactly those two, so the default `android-smoke`
  must only appear when the folder really is an Android app (a manifest) — that keeps the
  existing test true.
- Where results are shown: `upgrades._res_rows`/`_vision_row` (Checks card),
  `upgrades._check_hint` (the frozen-checks line), `upgrades.bundle_section`/`_vision_md`
  (review bundle), `badges.badges` (one badge per check), `/api/shot/<jid>` →
  `visioncheck.shot_file` (serves any PNG listed under `JOBS/<id>/`, so Android
  screenshots need no new route). `verify.build_fix_prompt` already feeds `issues` to the
  agent, which is where the crash lines belong.
- Environment (checked, not assumed): `adb` **is** installed and on PATH
  (`/opt/android-sdk/platform-tools/adb`, `ANDROID_HOME=/opt/android-sdk`);
  `adb devices` → **no devices attached**; there is **no emulator package**
  (`/opt/android-sdk/emulator` missing, `avdmanager` exists but no system image and no
  network to fetch one), and `~/agent-sandbox/state/android-sdk` does not exist in this
  container. So the Android path is exercised here against a **fake adb script on PATH**,
  and a real run must skip with a clear message instead of failing the job.
- Jail (`# JAIL`): `jail/podman-local.sh` starts every job container with `--network none`
  and no USB, so **adb cannot run inside the jail at all** — a container has no way to
  reach a device. Decision: adb runs on the host, argv-only (never a shell, never
  `shell=True`), the APK path is resolved and must stay inside the project folder, the
  device commands are fixed words, and nothing is written inside the project folder
  (screenshots come back as bytes, like the 6a driver's envelope). The project's own code
  still only ever runs on the device, never on the host. Documented in the module and the
  report.
- Test command (`AGENT_CHECKS.json`, the verify gate): `cd web &&
  PYTHONDONTWRITEBYTECODE=1 python3 -W ignore -m unittest discover -s tests`.
  Baseline before my changes: **592 tests, OK** (≈186 s).

## Steps

- [x] PLAN.md (this file) committed before the code.
- [x] `web/agentjobs_upgrades/android.py` (new, tested engine): adb lookup, `devices`
      parsing, APK find under `app/build/outputs/apk/debug`, `install -r`, package
      detection (`pm list packages -3` diff, `applicationId` fallback),
      `cmd package resolve-activity --brief`, `am start -W`, crash detection (`pidof` +
      `logcat` blocks), `exec-out screencap -p`, the stable-screen wait (two shots 1 s
      apart identical, max 20 s), device steps (`input tap/swipe/text/keyevent`, a
      screenshot after each) and the `capture()`/`smoke()` sequences that answer in the 6a
      capture shape. Every device command goes through one `Adb.call(argv)` — argv only,
      no shell — with `run`/`sleep`/`now` injectable so the tests are fast.
- [x] Fake adb on PATH for the tests: a small python script (scenario + argv log through
      env vars) covering devices, install ok/fail, package lists, resolve-activity,
      `am start`, `pidof`, logcat crash blocks, screencap (moving/steady frames) and the
      `input` steps.
- [x] `web/androidcheck.py` (new, `# U06b` glue): entry validation + frozen record for
      `target: "android"` vision checks and `type: "android-smoke"`; device choice (check
      `device` → settings `android_device` → first connected, with an unfilled
      `{{ANDROID_DEVICE}}` treated as auto); clear errors (`No device`, `APK not built`,
      `App crashed on start` with the logcat lines); `run_check` = the 6a model check with
      an Android `capture_fn`; `run_smoke` = install + start + 10 s without a crash (no
      model); `runners()` = `android-smoke` plus a `vision` that sends
      `target: "android"` here and everything else to 6a.
- [x] Two small additions in `visioncheck.run_check`: a capture that reports a real
      device-side failure (`fatal`) is a **fail**, not "not sure"; a skipped check gets a
      word and colour of its own.
- [x] Wiring: `upgrades.checks_ok`/`freeze_checks` accept both Android shapes;
      `checks.detect_checks` adds a required `app runs` android-smoke check for a real
      Android app; settings get `android_device`; jobboard registers the Android runners
      in the gate and in "Run checks again"; the Checks card, the frozen-check hint, the
      badge text and the review bundle show the device, the package and the crash lines.
- [x] Tests `web/tests/test_u06b_android.py`: the engine against the fake adb (exact argv
      sequences: install, launch, crash, stable wait, steps), the three clear errors,
      no-device = skipped and never fatal, the model check on an Android screenshot, the
      smoke check pass/crash, `run_all` + banner + fix prompt, records
      (validate/freeze/detect), pages (card row, badge, bundle).
- [x] Full suite green; PLAN ticked; AGENT_REPORT.md section for job 11.

---

# Job 10 — Upgrade 6a: vision check, part 1 (web pages + the checking engine)

## Findings

- Checks engine (given, do not rewrite): `web/agentjobs_upgrades/checks.py`.
  `load_checks()` already keeps a check entry's unknown keys in `Check.extra` and
  `type` is a first-class field (`"command" | "vision" | "android-smoke"`);
  `run_all(project, out_dir, wrap, checks=..., extra_runners={type: fn})` calls
  `fn(project, check, out_dir)` for every non-command check and **skips it silently
  when no runner is registered**. `summarize()` decides pass/fail only over results
  whose `mode != "advisory"`, so an advisory vision check never fails a job;
  `banner_text()` names the failing required ones.
- The vision engine (given): `web/agentjobs_upgrades/vision.py` — `build_request()`
  (base64 `image_url`), `parse_verdict()` (last JSON object with a bool `pass`,
  `think` tags stripped, python-ish quoting tolerated), `ask_once(retries=1)`,
  `check(votes=2)` → pass only when both votes pass, disagreement / no JSON →
  `inconclusive`, `to_check_result(name, verdict, mode, screenshots)` and
  `http_call(url)` posting to `http://127.0.0.1:8080/v1/chat/completions`.
  Already unit-tested in `web/tests/test_rest.py::Vision`.
- The gate: `jobboard.run_gate()` (jobboard.py:453) builds `wrap =
  upgrades.make_wrap(ctx.root, m["folder"], "agentcheck-<jid>")` and calls
  `ck.run_all(ctx.root, ctx.checks_root / "checks" / "round-<r>", wrap,
  checks=upgrades.job_checks(m))`. It only ever runs **after the agent process has
  ended** (and after each fix round) — requirement 5 comes free. "Run checks again"
  repeats the same call in `jobboard.u01_button()` (jobboard.py:2135).
- Frozen checks (what has to change): `upgrades.freeze_checks()` keeps only
  `c.type == "command"`, `upgrades.job_checks()` rebuilds `ck.Check(name, cmd,
  timeout_s)` dropping `type`/`extra`, and `upgrades.checks_ok()` rejects an empty
  `cmd`. A vision check has no `cmd`, so all three need a `type == "vision"` path.
- Where results are shown: `upgrades.checks_card_html()` + `upgrades._res_rows()`
  (the Checks card on the job page), `badges.badges()` (one badge per check →
  `/api/check/<jid>?src=<round|recheck>&n=<i>`; its body comes from
  `badges.check_text()`, which reads **only inside `JOBS/<id>/`** — the containment
  pattern to copy for serving PNGs). Routes are the `if` chain in `H.do_GET`
  (jobboard.py:1867). The review bundle is `reviewbundle.build_bundle(...)` with
  `upgrades.bundle_section(m)` spliced in by `jobboard.bundle_md()`.
- Jail (`# JAIL`): `upgrades.make_wrap(root, folder, prefix)` → `jail/run-local.sh
  sandbox-run` → `jail/podman-local.sh`: `--network none`, only the project folder
  mounted (rw) at `/work/<folder>`, `HOME=/home/agent`. A screenshot helper
  therefore cannot be mounted as a file: the driver goes **inline**
  (`python3 - <<HEREDOC`, its spec in a base64 env var) and the PNGs come back
  **base64 on stdout** behind a marker line, so nothing is written inside the
  project folder (a stray file would show up in the diff and in the guard).
  `upgrades.stop_job_containers()` filters `name=^agent(check|ckpt|git)-` — the
  browser container needs its own prefix added there.
- Environment (checked, not assumed): **no Playwright anywhere** — `python3 -c
  "import playwright"` → ModuleNotFoundError, `node -e "require.resolve('playwright')"`
  → not found, no `playwright`/`chromium`/`google-chrome`/`firefox` on PATH, no
  `~/.cache/ms-playwright`, and `Containerfile` installs neither Chromium nor
  Playwright. No Pillow either, so image sizing must be stdlib (`zlib`/`struct`).
  Nothing can be installed (no network), so screenshots cannot actually be taken in
  this sandbox: the engine, pages, bundle and wiring are built and tested against a
  mock capture and a fake model server, and the exact missing pieces go in the report.
- Test command (`AGENT_CHECKS.json`): `cd web && PYTHONDONTWRITEBYTECODE=1 python3
  -W ignore -m unittest discover -s tests`. Baseline before my changes: **550
  tests, OK** (≈3 min).

## Steps

- [x] Commit job 09's leftover PLAN.md as residue first (it was left uncommitted in
      the shared copy; it is not mine to rewrite).
- [x] `web/visioncheck.py` (new, `# U06`): PNG size read + max-1280 px downscale
      (stdlib zlib), check-spec parsing (`target`/`open`/`steps`/`size`/`mode`/`votes`),
      backend detection, `capture()` (jail-aware, stdout envelope),
      `run_vision_check()` → `vision.to_check_result()` plus a text log and PNG files
      in the check dir, `runners()` for `ck.run_all(extra_runners=...)`, and
      `shot_file()` for the route.
- [x] `web/visionshot.py` / `web/visionshot.mjs`: the in-sandbox capture drivers
      (temporary local http server for local files; steps click / type / key / wait /
      wait_for / shot; final screenshot; JSON envelope on stdout).
- [x] Tests `web/tests/test_u06a_vision.py`: canned model replies (pass / fail /
      garbage / disagreeing pair) through a real in-process fake model server, JSON
      parsing, image size handling, the capture envelope (real heredoc command: driver
      present, Playwright absent → clean "no browser" outcome), the runner end-to-end
      through `ck.run_all`, the checks-record round trip.
- [x] `upgrades.py`: keep vision checks in `freeze_checks`/`job_checks`, accept them
      in `checks_ok`, show verdict + issues + screenshot links in `_res_rows` /
      `checks_card_html`, list screenshots + issues in `bundle_section`, add `shot`
      to `stop_job_containers`.
- [x] `jobboard.py`: pass `extra_runners` in the gate and in "Run checks again",
      serve `GET /api/shot/<jid>?src=&n=&i=` (only inside `JOBS/<id>/`), emit a
      timeline event per vision verdict.
- [x] Page/bundle tests: the Checks card markup, the `/api/shot` route (200, 404s,
      cookie guard), the bundle text.
- [x] Full suite green; PLAN.md ticked; AGENT_REPORT.md section with the manual test
      (`~/cline-test/index.html`, Orb Chase) and what must be installed for real
      screenshots.

---

# Job 09 — Upgrade 10: the morning report

## Findings

- Server + pages: `web/jobboard.py` (2211 lines). `main()` starts `worker()` (the
  one-at-a-time job loop: `queue_cv.wait(5)` → `process_job(name)`) plus
  `ThreadingHTTPServer` on `127.0.0.1:PORT`; routes are the `if` chain at the top of
  `H.do_GET`/`H.do_POST` behind `H.guard()` (`jb=` cookie, Host must match `jobboard.PORT`).
  `render()` wraps a page, `header()` is the top bar (Projects / New project / Bench / Phone —
  that is where a Reports tab goes), `esc()` escapes.
- Records, all outside anything the sandbox mounts (`state/…`, env-overridable): `JOBS/<id>/meta.json`
  (+ `events.jsonl`, `timeline.jsonl`, `review.md`, `checks/`), `PLANS/<folder>.json`,
  `QUEUES/<folder>.json`, `BENCH_RUNS/<id>/spec|samples|progress|env.json`, settings
  `agentjobs-settings.json` (`upgrades.load_settings`) and `notify-settings.json`
  (`notifycfg.settings`). The report must live in the same kind of place: new
  `REPORTS = state/reports/` (env `JOBBOARD_REPORTS`) — reports/`YYYY-MM-DD.md` + `index.json`.
- A job record's facts a report needs: `status` (queued/running/done/failed/stopped/cancelled/
  interrupted/safety_stop), `folder`, `job`, `created/started/finished`, `fail_note`,
  `verify = {status, rounds[{round, summary, issues}], issues, banner, final, timeline}`,
  `checkpoints = {checkpoints[], rollbacks, discarded}`, `watchdog = [{time, kind, reason,
  command?|path?}]` (a safety-stop reason already reads "Safety stop: tried to reach …", the
  command is in `command`), `attempts`/`attempt_log` (U05 tries, each with `tests_passed`),
  `task_id`/`task_try` (U04b), `push = {ok, branch, time}` (whether Jason pushed the job branch),
  `recheck.summary`. Checks summary = `{status, results[{name, cmd, ok, exit_code, passed,
  failed, skipped, first_error, timed_out, mode}]}`.
- Given logic (use, do not rewrite): `agentjobs_upgrades/report.py` — `build(jobs, bench_sets,
  since, until, base_url, qwen_summary)`. Its `DONE = {"passed"}`, `FAILED = {failed, stuck,
  safety_stop, stopped}`, a `needs_review` line, and its own field names (`title`, `project`,
  `rounds`, `rollbacks`, `attempts`, `approval`, `first_failure`, `watchdog_reason`,
  `safety_command`, `queue_blocked`, `tests_passed`) — so the glue maps board records onto that
  shape. It links `/jobs/<id>` and `/api/jobs/<id>/bundle` (the board's routes are `/job/<id>` and
  `/api/bundle/<id>`), and `notify.py` builds click links as `<base>/jobs/<id>` too → add those
  two aliases in `jobboard.py`; that also fixes 9b's click links.
- "The model server is idle": `basalt_status()` (`/v1/models` → `data[0].status.value`, which
  basalt answers as `loading` / `loaded` / `unloaded`) and nothing active on the board (the GPU
  serves one stream). A summary call goes through `vision.http_call(url)` (POST
  `/v1/chat/completions`); `tests/fakeserver.py` is the stand-in — tests never touch 8080.
- The push notification for a finished report already exists: `notifycfg.report_ready(path,
  lines)` → event `morning_report` (in `notify.EVENTS`, on the Phone page's tick list, tag
  `sunrise`). Nothing to change in 9b.
- A queue finishing is already detectable: `plan.Queue.mark` sets `status = "finished"` when
  every task is done/failed/skipped, and `taskqueue.job_finished(m, queue_ctx())` is the hook
  `process_job` already calls — so an overnight queue ending is one `# U10` hook there.
- Test command (`AGENT_CHECKS.json`): `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore
  -m unittest discover -s tests`. Baseline before my changes: **550 tests, OK** (job 08's count).

## What this run found on top of that plan (re-run, 12 Nov)

- The plan above was left uncommitted by the run that started this job; `37ebbf2` committed it
  as found. Nothing of the U10 code existed, so the plan is still the plan.
- Two baselines were wrong on the way here: the suite is **741** committed tests, not 550, and
  the shared copy's working tree was **red** — job 04's (U09a) phone tests were left untracked
  and half-written, 8 of them failing. `210271c` finishes that residue (Origin check on the
  login POST, the Access page's restart wording, `.clamp`/`.act` split, and the four broken
  tests: they now carry the `aj=` session their page was served with, so the upgrade 8 token is
  really exercised). Suite is green at **785** from here on.
- `report.build`'s own test (`tests/test_rest.py::Report`) pins its output, so `report.py` is
  used exactly as it is. Every oddity is handled in the glue instead: it links `/jobs/<id>`, so
  jobboard grows that alias (which also fixes 9b's click links), and a queue task that never
  ran has no job id, so its line links to its project — the alias answers a folder name with
  `/project/<folder>`.
- A job record never carries `approval` (checked: nothing in the board writes it), so the glue
  reads upgrade 8's `decision_log` through `decisions.state`: a finished job nobody has touched
  is "waiting" — nothing on this board merges by itself.

## Steps

- [ ] `web/morningreport.py` (new glue, `# U10`): settings (`state/report-settings.json`: the
      time, default `07:00`, whether to ask the model); the "since last report" window from
      `reports/index.json`; `fact(m)` (a job record → report.py's dict: status words, the first
      failing check line, the watchdog reason, `Safety stop: <command>`, fix rounds, rollbacks,
      attempts, tests passing, whether it blocks its queue, waiting-for-approval);
      `facts_for(...)` (jobs + bench sets + skipped queue tasks + plans waiting for approval, in
      the window); `make(...)` (report.build → `reports/YYYY-MM-DD.md` + index + push
      notification, model paragraph only when the server is idle); `due(...)` for the clock
      trigger; the Reports-tab HTML + a small markdown→HTML so the links work.
- [ ] `jobboard.py` hooks: `REPORTS`, the Reports nav link, `GET /reports` and
      `GET /reports/<name>`, `POST /report/now` and `/report/settings`, the `tick()` call in
      `worker()`, the queue-finished hook after `taskqueue.job_finished`, and the
      `/jobs/<id>` + `/api/jobs/<id>/bundle` aliases report.py/notify.py link to.
- [ ] `web/tests/test_u10_report.py`: fixture records covering every state (done ✓ with checks,
      failed ✗ with the first failing check line, watchdog stop, safety stop, needs review,
      stopped, interrupted, cancelled, running, skipped queue task, waiting plan, bench sets),
      the since-last-report window (and that a second report covers only what came after), the
      no-model-call-when-busy rule (busy: a job running / model not loaded), the saved file +
      index + push line, the clock trigger, and the routes in the real handler (Reports tab,
      Make report now, the time form, the `/jobs/…` aliases, the guard).
- [ ] Full suite passes with the gate's command; smoke-serve the board on a free port (never
      8090) with its own state folders and click the Reports tab with curl.
- [ ] PLAN.md steps ticked; AGENT_REPORT.md section for this job.

---

## Plan left by the previous job (Upgrade 4b: the task queue) — kept verbatim so nothing is lost

# Job 08 — Upgrade 4b: run the approved plan as a queue

## Findings

- Server + pages: `web/jobboard.py` (2160 lines). `main()` starts `worker()` +
  `ThreadingHTTPServer` on `127.0.0.1:PORT`; routes in `H.do_GET`/`H.do_POST` behind `H.guard()`.
  Jobs run ONE at a time: `worker()` waits for a queued record and calls `process_job(name)`
  (safety `check_record` → jail self-test → `record_git_start` → `freeze_checks` →
  `run_planned` → follow-ups → notify → review bundle). A finished job can queue the next one
  from the worker thread (`maybe_auto_attempts`, `planning.plan_finished` → `new_job(...)`),
  which is exactly how a task queue advances.
- 4a already did the plan side: `web/planning.py` (project records `PLANS/<folder>.json`,
  `create_folder` = git init on `agent/<folder>` + first commit, the planner job,
  `read_tasks`/`validate`/`approve` — the APPROVED tasks are copied into the record outside the
  sandbox — plan review page with Save/Approve). `H.plan_edit` is where "Approve plan" lands.
- Given logic (use, don't rewrite): `agentjobs_upgrades/plan.py` — `order()` (stable Kahn),
  `job_prompt(outline_summary, milestone_text, task)` (the 4b task prompt: outline + milestone +
  task + acceptance + "Read AGENT_NOTES.md first… add 2-5 lines"), and `Queue`
  (`load_plan`, `recover`, `set_cutoff`, `next_task`, `mark` with on_fail stop/skip,
  `progress`), already unit-tested in `tests/test_rest.py`.
- U05 (`web/attempts.py`): `run_best_of_n` is reached from `run_planned` whenever the record
  carries `attempts` (2-4) — so a queue task gets best-of-N by setting `attempts` in its
  record; `MIN_N`/`MAX_N`/`DEFAULT_N` are the bounds; `phase_tag` keeps the banner honest.
- Board git on a project folder always goes through `safegit.SafeGit` wrapped in that folder's
  own jail (`upgrades.make_wrap(root, folder, "agentgit-…")` → `jail/run-local.sh sandbox-run`);
  `upgrades.HOST_MODE` (`JOBBOARD_JAIL=host`) is how the tests run it on temp folders.
- Fake agent for tests: hidden mode `fake` (`JOBBOARD_FAKE=1`), job text = a scenario JSON
  (`{"rounds": [[write/say/tick/cmd/run/exit…]]}`), so a queued task in a test needs its prompt
  swapped for a scenario — `tests/test_u04a_plan.py::DRIVER` already does that and keeps the real
  prompt in a file, which is how the 4b prompt gets asserted end-to-end.
- State must live outside anything the sandbox mounts (the agent can write anything in
  `WORK/<folder>`): 4a added `PLANS`; 4b adds `QUEUES = state/queues/<folder>.json`
  (env `JOBBOARD_QUEUES`), holding the `plan.Queue` state file.
- Test command (`AGENT_CHECKS.json`): `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore
  -m unittest discover -s tests`. Baseline before my changes: **478 tests, OK** (job 07 left its
  PLAN.md uncommitted in this shared copy; committed as-is first).

## Steps

- [x] Commit job 07's leftover PLAN.md as-is (its own commit) so this job's diffs stay clean.
- [x] `web/taskqueue.py` (new glue, `# U04b`): the queue file `QUEUES/<folder>.json` built from
      the APPROVED tasks (`plan.Queue`, dependency order, nothing runs until started); the task
      prompt (10-line outline summary + its ROADMAP.md milestone + task + acceptance via
      `plan.job_prompt`); `start_next` (one job, in the project folder, task checks, best-of-N
      for size M or a task that already failed when the project has it on, resume for a retried
      task, AGENT_NOTES.md hash taken before it runs, base commit recorded, project branch kept);
      `job_finished` (AGENT_NOTES note in the job record, seconds, end commit, done/failed/
      interrupted, on_fail stop vs skip, then the next task); `startup` recovery; the time-left
      estimate; the project-page card (milestone progress, current task, Pause/Resume/Cancel,
      "Don't start new tasks after", on-fail choice, failed/skipped shown).
- [x] `jobboard.py` hooks: `QUEUES`, `check_record` validation for the task fields, `worker()`
      startup (interrupted job records + `taskqueue.startup`), `taskqueue.job_finished` in
      `process_job`, the card on the project and plan pages, `POST /queue/<folder>`,
      project-record settings (which agent runs tasks, best-of-N on/off).
- [x] `web/tests/test_u04b_queue.py`: queue from an approved plan, prompt content, best-of-N rule,
      notes check, estimate, milestone progress, stop vs skip, cutoff, restart recovery, the
      branch keeping, the card HTML, the routes — plus fake-agent runs through `process_job`
      (dependency order + chained commits, checks of a task, a failure that stops vs skips, the
      "don't start after" time, and the notes note in the job record).
- [x] Full suite passes with the gate's command; smoke-serve the board on a free port (never
      8090) and click the queue with curl.
- [x] AGENT_REPORT.md section for this job.

---

## Plan left by the previous job (Upgrade 4a: requirements → plan) — kept verbatim so nothing is lost

## Job 07 — Upgrade 4a: requirements → plan

### Findings

- Server + pages: `web/jobboard.py` (2022 lines). `main()` starts `worker()` +
  `http.server.ThreadingHTTPServer` on `127.0.0.1:PORT` (`jobboard.PORT`, env `JOBBOARD_PORT`);
  routes live in `H.do_GET`/`H.do_POST` behind `H.guard()` (Host must match `jobboard.PORT`,
  `jb=` cookie). `render(body)` wraps a page, `header()` is the top bar, `esc()` escapes.
- Job life: `new_job(mode, folder, job, offline, resume=, parent=, extra=)` →
  `JOBS/<id>/meta.json` (env `JOBBOARD_JOBS`, outside everything the sandbox mounts) →
  `worker()` → `process_job(name)` → `check_record()` (safety re-check) → jail self-test (only
  for `local-*` modes) → `record_git_start` → `upgrades.freeze_checks` → `run_planned`
  (`run_agent_job`, or `attempts.run_best_of_n` when the record asks for tries) →
  `maybe_auto_attempts` → notify → `build_review_md`. Project folders: `WORK/<folder>`
  (env `JOBBOARD_WORK`), `folders()` lists them and hides attempt folders.
- A finished job can queue a follow-up job from the worker thread: `maybe_auto_attempts` and
  `hard_retry` call `new_job(..., parent=..., extra=...)`. That is the pattern for "send the
  errors back to the planner once".
- **`H.do_POST` parses the body with `urllib.parse.parse_qs` WITHOUT `keep_blank_values`**, so
  an empty field disappears from its list. A repeating card form therefore cannot align
  same-name multi-values — the plan form names every field with its card index
  (`tid1`, `title1`, `prompt1`, …) and the server reads them in index order.
- The button that was pressed arrives as a form field (`<button name="move" value="t3:up">`),
  which is how one endpoint does save / approve / reorder / delete / add.
- Given logic (use, don't rewrite): `agentjobs_upgrades/plan.py` — `TASK_FIELDS`/`REQUIRED`,
  `SCHEMA` (draft 2020-12 JSON Schema for the task list), `validate(tasks) -> [errors]`
  (required fields, unknown fields, id charset, duplicate ids, unknown/self dependencies,
  and `order()`'s cycles), `order(tasks)` (stable Kahn), `job_prompt(...)` (per-task prompt,
  that is 4b's), `Queue` (4b's persistent queue). Already covered by `tests/test_rest.py`.
- Jail rules for this job: any git the board runs on a project repo goes through
  `agentjobs_upgrades/safegit.SafeGit` (`-c core.hooksPath=/dev/null`, config not trusted,
  `GIT_CONFIG_NOSYSTEM=1`) wrapped in the project's own jail with
  `upgrades.make_wrap(root, folder, prefix)` → `jail/run-local.sh sandbox-run <root> <folder>
  <name> --` (only `<root>` mounted at `/work/<folder>`, no network). `upgrades.HOST_MODE`
  (`JOBBOARD_JAIL=host`) runs it straight on the host — how the tests work. Plain file writes
  into `WORK/<folder>` are the existing house pattern (`checks.write_checks`).
- Plan state must live outside the sandbox (the planner can write anything inside
  `WORK/<folder>`): new `PLANS = state/plans/<folder>.json` (env `JOBBOARD_PLANS`), same
  safety rule as `JOBS`.
- Fake agent for tests: hidden mode `fake` (`JOBBOARD_FAKE=1`), job text = a scenario
  (`{"rounds": [[step…]]}`; `write`/`say`/`tick`/`cmd`/`exit` steps). U01/U05 tests run one
  fake job per test in a child process with `JOBBOARD_JAIL=host` + `JOBBOARD_FAKE=1`
  (`tests/test_u01.py::Board.run_job`); page tests serve `jobboard.H` in-process on their own
  port with `jobboard.PORT` set (`tests/test_u05_bestofn.py::Pages`).
- Test command (`AGENT_CHECKS.json`): `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore
  -m unittest discover -s tests`. Baseline before my changes: **425 tests, OK** (job 06's
  leftover best-of-N work was left uncommitted in this shared copy; committed as-is first).

### What this run found on top of that plan (re-run, 12 Nov)

- The plan above was written by the run that started this job; its code and its 53 tests are
  `5b3efad`, but that run stopped before ticking anything, before the port-8091 click-through
  and before its AGENT_REPORT.md section. Nothing here needed rebuilding — this run verified the
  feature, clicked it, and fixed what the clicking found.
- Two baselines were wrong on the way here. The note above says "425 tests, OK"; the shared copy
  was at **785**, and job 09's (U10 morning report) work was still uncommitted from a run that
  stopped mid-way — committed as found (`7a28338`) so this job's diff stays clean. Suite is
  green at **790** from here (58 tests in `test_u04a_plan.py`).
- Four real bugs, each found by clicking the pages or reading the state machine, each with a test:
  - `ad8f340` — a planning job whose record the worker refuses, or whose jail self-test fails,
    left `plan_status=running` for ever: the project page kept saying "The planner is working on
    it.", hid **Review the plan**, and no retry was ever queued. `process_job` now runs the plan
    hook on those paths too (`requeue=False`, so a job that never ran queues nothing behind
    itself and the automatic re-send stays at one).
  - `ad8f340` — when the planner wrote no `tasks.json` at all the page said "The plan is not
    usable yet (0 problems)". Now: "The planner wrote no tasks.json - press Make a plan again."
  - `8c911c9` — the New project page lists every folder in `~/agent-work`; picking one that was
    already a project wrote a fresh record over the old one, losing its outline, its plan and
    what Jason had approved, while the folder's 4b queue carried on under a project that no
    longer said it was approved. One record per folder, so a second create is refused instead.
  - `63472dc` — `planning.CSS` had no phone block (phonelayout's rule is that each module styles
    its own classes), so on a phone the plan page's sticky Save / Approve bar sat under the
    upgrade 9a sticky header — the buttons Jason needs after scrolling a long plan.
- What the 8091 copy could not click: a real planner. There is no model agent in this sandbox, so
  the planner-run path is covered by the `Run` tests (a child process with the real worker and the
  fake agent, plan files written into a real folder). Pressing **Make a plan** there with mode
  "Fake agent" gets its record refused — fake mode wants a scenario as its job text, as it does
  for every fake job — which is exactly the case the first fix above now reports honestly.

### Steps

- [x] Commit job 06's leftover work as-is (its own commit) so this job's diffs stay clean.
- [x] `prompts/planner.md` (repo root): the planner prompt with `{{NAME}} {{KIND}} {{FOLDER}}
      {{OUTLINE}} {{ERRORS}}` placeholders — "do not write app code", write ROADMAP.md +
      tasks.json, ~45-minute tasks, task 1 = skeleton + test setup + AGENT_CHECKS.json,
      testable acceptance criteria.
- [x] `docs/tasks-schema.json`: the task schema, exactly `plan.SCHEMA` (a test keeps it in sync).
- [x] `web/planning.py` (new glue, `# U04a`): project records under `PLANS/`, project folder
      creation (jail git init on branch `agent/<name>` + a first commit), the planner prompt,
      `start_plan` (a normal job, `plan_job` marker, checks off), reading + validating the
      folder's `tasks.json`/`ROADMAP.md`, the one automatic re-send of the errors,
      save/approve, and the page HTML (new project, project, plan review).
- [x] `jobboard.py` hooks: `PLANS`, header links, `check_record` validation for
      `plan_job`/`plan_retry`, `planning.plan_finished(...)` after `run_planned` (and now on the
      paths where the record was refused), the plan card on a planning job's page, GET
      `/projects` `/project/new` `/project/<folder>` `/plan/<folder>`, POST `/project/new`
      `/project/edit/<folder>` `/plan/edit/<folder>`.
- [x] `web/tests/test_u04a_plan.py`: schema (good/bad/cycles) + the docs file stays in sync,
      prompt content, project folder creation (git init, branch, first commit), records +
      approval state, the plan form parse (index-named fields, move/drop/add, blank values),
      a canned planner output through the fake agent (valid plan, and an invalid plan sent
      back to the planner exactly once), and the pages/routes served in-process. 58 tests.
- [x] Full suite passes with the gate's command; smoke-serve the board on a free port
      (never 8090) and click the new pages with curl. (790 tests OK; 76 curl checks on 8091.)
- [x] AGENT_REPORT.md section for this job.

---

## Plan left by the previous job (Upgrade 5, best-of-N) — kept verbatim so nothing is lost

## Job 06 — Upgrade 5: best-of-N for hard steps

### Findings

- Server + pages: `web/jobboard.py` (1941 lines). `main()` starts `worker()` +
  `http.server.ThreadingHTTPServer` on `127.0.0.1:PORT` (`jobboard.PORT`, env `JOBBOARD_PORT`);
  routes are in `H.do_GET`/`H.do_POST` behind `H.guard()` (Host must match PORT, `jb=` cookie).
- Job life: `new_job()` → `JOBS/<id>/meta.json` (env `JOBBOARD_JOBS`, never inside a mounted
  folder) → `worker()` → `process_job(name)` → safety `check_record()` → jail self-test →
  `record_git_start(m)` (`git_start` = base sha, `git_branch_start`) → `upgrades.freeze_checks()`
  → `run_agent_job(m, name)` → `notify_finished` + `build_review_md`. Project folders live in
  `WORK/<folder>` (env `JOBBOARD_WORK`).
- One run inside `run_agent_job`: `JobCtx(m, name, cfg)` (root = `WORK/m["folder"]`,
  `EventLog` on `JOBS/<id>/events.jsonl`, `Watchdog` with project path `/work/<folder>`,
  `CheckpointManager` in `<root>/.git/agentjobs/<id>`) → `run_agent_loop` (agent + watchdog
  restarts) → `run_gate` (`verify.VerifyGate` + `checks.run_all(root, job_dir/checks/round-N, wrap)`).
- Jail: `upgrades.make_wrap(root, folder, prefix)` → `jail/run-local.sh sandbox-run <root>
  <folder> <name> --` → `jail/podman-local.sh jail_local_args` mounts **only** `<root>` at
  `/work/<folder>` (`-w /work/<folder>`), no network. The real agent goes through
  `~/agent-sandbox/agent <mode> --in <folder>`, which only accepts a top-level `~/agent-work`
  folder name (no slashes). `upgrades.HOST_MODE` (`JOBBOARD_JAIL=host`) runs git/checks/the fake
  agent straight on the host — that is how the tests work.
- All board git on a job repo goes through `agentjobs_upgrades/safegit.SafeGit`
  (`-c core.hooksPath=/dev/null`, `core.fsmonitor=false`, `protocol.allow=never`, …,
  `GIT_CONFIG_NOSYSTEM=1`). Pushing only via `jail/push-agent-branch.sh` (`jailcfg.push_branch`),
  and only for branches matching `agent/...` (`jailcfg.BRANCH_RE`).
- Given logic (use, don't rewrite): `agentjobs_upgrades/bestofn.py` — `Attempt` (k, branch,
  status, tests_passed, test_files_changed, diff_lines, seconds, issues; `clean_pass`),
  `sort_key` (clean pass → most tests → fewest test files → smallest diff → fastest → k),
  `pick_winner`, `should_continue(done, n, hard, stop_early)`, `why`. Also
  `guard.is_test_file()` for test-file paths and `checks.counts_total()`.
- Ranking needs per-attempt numbers the gate does not store: tests passing come from
  `verify["final"]["results"]`, the rest (test files changed, diff size, file list) from git.
- Fake agent for tests: hidden mode `fake` (`JOBBOARD_FAKE=1`), job text = a scenario
  (`{"rounds": [[step…], …]}`; steps `write/delete/tick/cmd/say/sleep/exit`), run by
  `upgrades.fake_cmd(m, root, folder, scenario_text, rnd, resume)`.
- Test command (`AGENT_CHECKS.json`): `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore
  -m unittest discover -s tests`. Baseline before my changes: **382 tests, OK**.
  In-process page tests serve `jobboard.H` on their own port and set `jobboard.PORT`
  (`tests/test_u07b_changes.py::Pages`); the U01 integration tests (`tests/test_u01.py`) run one
  fake job per test in a child process with `JOBBOARD_JAIL=host` + `JOBBOARD_FAKE=1`.
- Working tree when I started: job 05's ntfy work was left **uncommitted** in this shared copy
  (`web/notifycfg.py` untracked, `# U09b` hooks in `web/jobboard.py`, its PLAN.md). It is not my
  job; I commit it as-is in its own commit first so my diffs stay clean and nothing is lost.
- A git **worktree** cannot be an attempt's project folder: the jail mounts one folder and a
  worktree's git dir lives in the main repo (invisible in the container), so git inside the
  sandbox would break. Attempts are therefore **full clones** of the base commit in their own
  `~/agent-work` folder — same base commit, one branch `agent/<job id>-try<k>` each, and an
  attempt can only ever see its own copy. Branches are brought back to the project repo through a
  **git bundle** made inside the attempt's jail (`protocol.allow=never` stays; only the file
  transport is opened for the two steps that cannot run in the jail: `clone --no-checkout` and
  `fetch <bundle>`, and the working trees are built inside the jail so no agent-planted filter
  ever runs on the host).

### What this run found on top of that plan (re-run, 12 Nov)

- The run that wrote the plan above got as far as the code and its tests — `2903bb7`
  (`web/attempts.py` + the `jobboard.py` hooks), `7520a60` (branches come home even when a try
  changed nothing; only a clean winner is checked out; 38 tests) and `54feb77` (the phase tag and
  the stopped/failed endings, left uncommitted). It stopped before ticking anything, before the
  port-8091 click-through and before its AGENT_REPORT.md section. Nothing needed rebuilding: this
  run clicked the feature and fixed what the clicking found.
- Two baselines were wrong on the way here. The plan says "382 tests, OK"; the shared copy was at
  **790** (jobs 01-11 landed in between), and `test_u05_bestofn.py` is at **44** tests, not 38 —
  this run's Stop fix adds the 44th, so the suite is green at **791**.
- One real bug, found by clicking the page and reading the record afterwards (`52a35a9`): **Stop
  did not stop the tries**. Pressing Stop while an attempt ran ended that try (its row said
  "stopped"), but `run_best_of_n` only looked for status `stopping`, and `run_agent_job` has
  already written `stopped` by the time the loop looks — so attempt 3 of 3 started after Jason had
  stopped the job. `stop_pressed()` accepts `stopping`/`cancelled`/`stopped`/`interrupted` and is
  checked after every try and again before the next one; the timeline now says why the rest were
  skipped. The same click-through showed the card claiming "No more attempts: attempt 3 passed
  cleanly" when the tries had simply run out and none passed — a hard job now ends with
  "All 3 attempts tried".
- Two places where the implementation deliberately differs from the words of the spec, both
  because of the jail (recorded in `attempts.py`'s docstring and in the report):
  - "its own git worktree" → **a full clone of the base commit** in its own `~/agent-work` folder.
    The jail mounts exactly one folder and `agent --in` only takes a top-level `~/agent-work`
    name, and a worktree's git dir stays in the main repo — invisible inside the container, so git
    in the sandbox would break. Same base commit, own repo, own branch: an attempt still can only
    ever see its own copy.
  - "an Attempts tab" → an **Attempts card** on the job page (the board has no tabs; every
    upgrade's information is a card there: Review bundle, Decide, Checks, Checkpoints, Changes).

### Steps

- [x] Commit job 05's leftover ntfy work as-is (its own commit, `d04d5ab`) so this job's commits
      stay clean.
- [x] `web/attempts.py` (new glue): settings keys (`attempts` 2-4 default 3, `stop_early` on,
      `auto_attempts` off), record fields + plan, attempt folder/branch names, and the git
      plumbing: clone the base commit into its own folder, branch `agent/<id>-try<k>`, save
      leftover edits, bring the branch back through a bundle, `apply_winner` (job branch
      `agent/<id>`), `prune_old` (7 days) — all through SafeGit, working trees inside the jail.
- [x] Scores: turn one finished attempt into a `bestofn.Attempt` (status, tests passing, test
      files changed, diff lines, seconds, needs-review issues) + the file list for the page;
      rank with `bestofn.pick_winner`/`why`, stop early with `bestofn.should_continue`.
- [x] `run_best_of_n`: attempts one after another in the worker (one GPU), phase
      "Attempt 2 of 3", per-attempt check logs under `JOBS/<id>/attempts/try-k/checks/…`,
      attempts never resume each other's session, then apply the winner and clear the
      per-attempt folders/verify/checkpoints from the record. Re-run: Stop now really stops the
      run (`stop_pressed`), and the timeline says which way it ended ("All 3 attempts tried" vs
      "No more attempts: attempt 1 passed cleanly" vs "Stopped after attempt 2 of 3").
- [x] `jobboard.py` hooks: `check_record` validation, form fields ("Hard job: try it 3 times",
      2-4, "about 3× longer"), `run_agent_job(..., fresh_log=)`, `JobCtx` per-attempt checks
      dir, `folders()` hides attempt folders, the auto "try fresh attempts" setting
      (`maybe_auto_attempts`, off by default).
- [x] UI: the Attempts card on the job page (every attempt's scores, changes and branch, cost
      note), "Use this one instead", "Try 3 fresh attempts" on a job that failed after fix
      rounds, and their POST routes (`/use-attempt/<id>`, `/try-hard/<id>`).
- [x] `web/tests/test_u05_bestofn.py`: unit tests for names/settings/plan, scores + every
      tie-break, early stop, all-attempts-fail, clone/branch/bundle/apply/prune on a real temp
      repo, and fake-agent runs (hard job with 3 different scenarios, early stop, all fail +
      "Use this one instead", the buttons/routes). 44 tests; the re-run adds
      `test_a_try_that_came_back_stopped_does_not_start_the_next_one`.
- [x] Full suite passes with the gate's command; click the pages on a copy of the board on
      127.0.0.1:8091 (never 8090). (791 tests OK; 55 checks clicked on 8091, all passing — the
      first two passes through the click-through are what found `52a35a9`.)
- [x] AGENT_REPORT.md section for this job.

---

### Plan left by the previous job (Upgrade 9b, phone alerts) — kept verbatim so nothing is lost

#### Findings

- Server + pages: `web/jobboard.py` (1889 lines). `main()` starts the `worker()` thread and
  `http.server.ThreadingHTTPServer` on `127.0.0.1:PORT` (`jobboard.PORT`, env `JOBBOARD_PORT`).
  Routes live in `H.do_GET`/`H.do_POST` behind `H.guard()` (Host must match `jobboard.PORT`,
  `jb=` cookie). `render(body)` wraps a page; `header()` is the top bar (Bench, GPU stats).
- Jobs: `new_job()` → `JOBS/<id>/meta.json` (`JOBBOARD_JOBS`), `worker()` → `process_job()` →
  `run_agent_job()` → `run_gate()`. Final `meta["status"]` ∈
  `done|failed|safety_stop|stopped|cancelled|interrupted`; bench runs use `kind: "bench"` with
  `done|failed|stopped|interrupted`. `meta["verify"]` = `agentjobs_upgrades/verify.py` outcome
  (`status` ∈ passed|unverified|needs_review|stuck|failed|aborted, `final` summary with
  `results[]` of `{name, cmd, passed, failed, skipped, ok, exit_code, output_file}`).
- Where a job ends: `process_job()` calls `notify_finished(m, ok)` (the old **desktop**
  notification + sound, `notify-send`/`pw-play`) in three places — after a bench run, after a
  failed sandbox self-test, and after `run_agent_job()`. `run_agent_job()` also writes the
  timeline line via `dashboard.tl_add`.
- Settings: `upgrades.load_settings(SETTINGS_FILE)` — but it only keeps numeric keys plus
  `watchdog`, so string settings (server/topic/token/times) need their own file:
  `~/agent-sandbox/state/agentjobs-settings.json` sibling → `notify-settings.json`
  (env override `JOBBOARD_NOTIFY`).
- Given logic (use, don't rewrite): `web/agentjobs_upgrades/notify.py` —
  `NotifySettings`, `random_topic()` (aj- + 20 chars), `validate()`, `in_quiet_hours()`,
  `clean()` (drops code, paths, non-text), `build()` (title/message ≤2 lines, priority
  5 for safety_stop, `click` link when `base_url` is set), `send()` (off when `server` empty
  or the event is unticked, quiet-hours suppression except safety_stop, 3 tries with
  exponential backoff, `post` injectable, `_post` = urllib POST with a Bearer token).
  `report.py` is upgrade 10 (morning report) and is not wired into the board yet.
- Nothing in `jobboard.py` uses `auth.py` (9a phone access) yet, so the click link stays
  opt-in through a `base_url` setting.
- Glue-module pattern in this repo: `web/badges.py`, `web/diffcard.py`, `web/dashboard.py`,
  `web/jailcfg.py`, `web/upgrades.py` — logic in the module, tiny `# U0x` hooks in jobboard.
- Test command (`AGENT_CHECKS.json`): `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore
  -m unittest discover -s tests`. Baseline before my changes: **382 tests, OK**.
  In-process page tests serve `jobboard.H` on their own free port and set `jobboard.PORT`
  (`tests/test_u07b_changes.py::Pages`, `tests/test_jobweb_bench.py::BenchPagesInProcess`).

#### Steps

- [ ] New `web/notifycfg.py` (glue): settings load/save (0600, env `NTFY_SERVER`/`NTFY_TOPIC`
      seed the defaults, topic pre-filled with `notify.random_topic()`), form ↔ settings with
      `notify.validate`, message builders (job finished ✓/✗ with checks counts, waiting for
      you, safety stop, bench verdict, morning report, model down — counts/status only, never
      log text or paths), `push()` on a background queue (never blocks a job, retries 3× via
      `notify.send`, outcomes appended to `state/notify-log.jsonl`), edge-triggered
      `model_check()`, and the settings-page HTML.
- [ ] `jobboard.py` hooks: `NOTIFY_FILE`, `phone_notify(m)` at the three finish sites,
      `phone_model_check()` in the worker loop, `GET /notify` + `POST /notify`, "Phone" link in
      the header, defaults written in `main()`, `--test-phone` CLI.
- [ ] `web/tests/test_u09b_notify.py`: settings/validation, message content (no log text,
      paths, diffs), quiet hours (safety stop exempt), retries + logging, off by default,
      never blocks the caller, model-server edge trigger, and the pages/POST on their own port.
- [ ] Full suite passes (same command as the verify gate); smoke-serve the board on a free
      port (never 8090) against a local fake ntfy server and curl the settings page.
- [ ] AGENT_REPORT.md section for this job.

---

# Fix job A — review fixes for upgrades 6b, 6a, 4b and 7b

Four findings from the review of those upgrades. Small, local changes; each finding needed tests;
nothing else touched. Branch `agent/jobboard-next`, nothing pushed.

## Findings

1. **6b (a) — `adb shell` is a shell on the PHONE.** A check step `{"text": "a; pm uninstall x"}`
   or `{"text": "$(reboot)"}` is joined by adb and run by `/system/bin/sh`, so the step's text was
   a command, not typing. Reject (clear error, step not sent) any text holding a shell
   metacharacter, or quote it so the device cannot interpret it; check every other value that goes
   to `adb shell` the same way.
2. **6b (b) — installing an APK must be opt-in.** Only when the check says `"install": true` AND
   names a device serial (its own `device`, or the `android_device` setting). The automatic
   `app runs` check an Android project gets when it has no `AGENT_CHECKS.json` must install
   nothing and report a clear skip saying how to turn it on.
3. **6a (a) — the screenshot data coming out of the sandbox is not trusted.** 10 MB cap on the
   sandbox output that is read and on each decoded PNG; `_png_decode` must never call
   `zlib.decompress` without a limit (`decompressobj` + `max_length`, and refuse absurd
   width × height). Oversized data gives a clear note, never a crash or huge memory use.
4. **6a (b) — no Playwright/Chromium in the sandbox image is a skip**, never a failed job:
   "Playwright isn't installed in the sandbox image; vision check skipped", advisory.
5. **4b — a task job of a plan queue was created with `offline=False`,** so a plan set to a
   non-local agent gave its tasks the internet. Task jobs must be created with the flag on.
6. **7b — `bad_path` refused every path that starts with ".git",** so `.gitignore`,
   `.github/workflows/x.yml` and `.gitattributes` showed "bad path". Only the `.git` folder itself
   (`.git` and `.git/...`) may be refused, the shell-side `FILE_SCRIPT` cases included.

## Steps

- [x] 1: `agentjobs_upgrades/android.py` — one guard at the `Adb.shell` choke point (every word
      that holds a metacharacter is refused before anything is sent), `TEXT_RE` allowlist for a
      `text` step (validated in `androidcheck.check_ok` as well, so a dangerous step never gets
      into the job record), `PKG_RE` filter for package names the device reports, `$` dropped from
      the component pattern. `test_u06b_android.py` +5. Commit `0d37a77`.
- [x] 2: `androidcheck.py` — `install_opt_in()` (the check's `install` flag AND a named serial)
      gates both runners before any adb call; the skip text names both switches; `install` is
      validated (a bool) and frozen into the record; the badge and the Checks card say WHY a
      device check was skipped. `test_u06b_android.py` +5. Commit `c62cc14`.
- [x] 3: `visioncheck.py` — `MAX_OUT`/`MAX_PNG` 10 MB, `MAX_PIXELS`, `MAX_RAW`; `default_run`
      keeps at most 10 MB of the run's stdout/stderr (drains the rest, leaves a note); a shot over
      the cap is left out with a note; `_png_decode` inflates with `decompressobj(max_length)` and
      refuses an absurd header. `test_u06a_vision.py` `Limits` +6. Commit `013744f`.
- [x] 4: `visioncheck.py` — a missing browser (the driver's `no_browser` flag or the error text)
      gives a `skipped` verdict forced to advisory with that exact sentence; both drivers say so
      themselves (`visionshot.mjs` imports Playwright dynamically so it can answer at all).
      `test_u06a_vision.py` +5. In the same commit as 3.
- [x] 5: `taskqueue.start_next` passes `offline=True` whatever the plan's mode. `test_u04b_queue.py`
      +1 (a supervised plan: the agent is kept, the flag is on, the record still validates).
      Commit `1bc9b63`.
- [x] 6: `diffview.bad_path` refuses a `.git` path **segment** (FILE_SCRIPT's shell cases
      `.git|.git/*` were already right). `test_u07b_changes.py` +1. Commit `ed96686`.
- [x] Gate: `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore -m unittest discover -s tests`
      → **Ran 814 tests in 294.996s / OK** (791 before this job: +23).
- [x] PLAN.md steps ticked + AGENT_REPORT.md section for this job.

## What this job could not do here

There is no phone and no real adb in this sandbox: the Android findings are verified against the
fake-adb fixture (`tests/fixtures/fakeadb/adb`), which logs every argv it is given, so "never sent
to adb" is proven from that log. Playwright and Chromium are not installed here either, so the
missing-browser skip is exercised through the drivers' own `no_browser` answer and the error-text
marks, not through a real browser run; the screenshot path itself is covered by the envelope tests
with fake shot data, as before. No podman/bwrap jail: the tests run in the host jail
(`JOBBOARD_JAIL=host`), as in the earlier jobs.


---

# Fix job B — merge the live voice code (VOICE) into this copy

This copy branched off the live board before voice chat existed, so it has no voice code at all.
A later swap of this copy onto the live board would delete voice. The job: bring the voice code in
here, keeping every line this copy already has (JAIL / U01 / U04 / U05 / U06 / U07 blocks included).
`.live-voice-ref/` (live files + base + the base→live patch) is the only source; it is deleted at
the end so the merge is not left depending on a snapshot.

## Findings

1. **No voice files here:** `voice_core.py`, `voice_worker.py`, `static/voice.js`,
   `tests/test_jobvoice.py` are missing from `web/`.
2. **`web/jobboard.py` has none of the nine voice changes** in `live-changes.patch` (import,
   `voice.js` script tag, `data-voice` hooks on the two textareas and the report `<pre>`, the
   voice helper block, `H.voice_post`, the two GET routes + static js, the POST route).
   The patch does not apply: this file grew from 1767 to 2537 lines since the branch point.
3. **Nothing may be lost from this copy** — the merge is by hand, block by block, never by
   copying the live file over.

## Steps

- [x] 1+2: copy `voice_core.py`, `voice_worker.py`, `static/voice.js`, `tests/test_jobvoice.py` in
      verbatim (same relative paths under `web/`, md5-identical to the live files) and apply the
      nine hunks of `live-changes.patch` to `web/jobboard.py` by hand in the same commit — the two
      only work together (the copied test needs the routes). `@@PHONE@@` (U09a), the U09a `/login`
      route in `do_POST`, the U07 dashboard cards in the job page and the pre-login static routes
      in `do_GET` all stay; `git diff` shows 86 added lines and only the 4 lines the patch itself
      rewrites.
- [x] 3: marker check — `tests/test_jobvoice_merge.py` (5 tests) pins all 13 `VOICE` lines of the
      live `jobboard.py`, the helpers/hooks/routes around them, the four voice files, and one
      marker line per block only this copy has (JAIL, U01, U04a/b, U05, U06, U06b, U07, U07b, U08,
      U09a, U10). Fixed lists in the test file; nothing reads `.live-voice-ref`. All 13 voice lines
      are absent from `HEAD`, so this test fails if the merge is ever lost.
- [x] 4: full suite green: `cd web && PYTHONDONTWRITEBYTECODE=1 python3 -W ignore -m unittest
      discover -s tests` → **Ran 830 tests in 291.975s / OK (skipped=2)** — 814 before this job
      (+11 `test_jobvoice.py`, +5 `test_jobvoice_merge.py`).
- [ ] 5: `git rm -r .live-voice-ref`, commit, AGENT_REPORT.md section for this job.

## What this job cannot do here

No whisper/piper models and no voice venv in this sandbox (`~/agent-sandbox/voice` does not
exist), so the two real-model tests in `tests/test_jobvoice.py` skip exactly as they do on the
live board — they are copied verbatim, not edited. There is no microphone either, so
`static/voice.js` is only checked as served bytes plus `node --check`.
