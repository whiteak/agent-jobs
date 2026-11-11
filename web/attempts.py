#!/usr/bin/env python3
"""Upgrade 5: best-of-N for hard steps (added 2026-11-11 by Claude; jobboard.py keeps small
"# U05" hooks).

A hard job is tried N times (2-4, default 3) from the same starting point, one attempt after
another (the GPU serves one stream), and the best attempt wins. The ranking itself is
`agentjobs_upgrades/bestofn.py` (not rewritten here): checks pass with no "needs review" flag,
then most tests passing, then fewest test files changed, then the smallest change, then fastest.

How an attempt is run, and why it looks like this:

  * The jail mounts exactly ONE project folder (`jail/podman-local.sh`:
    `-v <hostdir>:/work/<name> -w /work/<name>`), and `~/agent-sandbox/agent --in <folder>`
    only accepts a folder directly under ~/agent-work. A git **worktree** of the job repo
    therefore cannot be an attempt's folder: its git dir stays in the main repo, invisible in
    the container, so git inside the sandbox would break. Each attempt gets its own folder
    `~/agent-work/<folder>-try<k>-<id>` holding a full clone of the job's base commit, on
    branch `agent/<job id>-try<k>`. Same base commit, its own repo: an attempt can only ever
    see its own copy - never the main folder and never another attempt.
  * Everything that runs project code inside an attempt (the agent, checks, fix rounds, the
    watchdog, checkpoints) is the normal job run - `jobboard.run_agent_job` with the attempt's
    folder, its own check logs under `JOBS/<id>/attempts/try-<k>/checks/`, and no `--resume`
    of another attempt's session.
  * Only two git steps cannot run in the jail because they need two folders at once:
    `git clone --no-checkout` (objects and refs only - no working tree, so no agent-planted
    smudge filter or hook ever runs on the host) and `git fetch <bundle>` (a bundle carries no
    repo config, so the project repo never reads the attempt repo's settings). Both keep
    safegit's flags and `protocol.allow=never`, opening only the file transport
    (`-c protocol.file.allow=always`). Every step that writes a working tree - the attempt's
    `checkout -B` and the winner's checkout in the project folder - runs inside the jail.
  * The winner's commits go on the job branch (`agent/<job id>`) in the project folder, so the
    Changes card, the review bundle and the Push button work as they always did. The losing
    attempts stay as branches for KEEP_DAYS days on the Attempts card ("Use this one instead");
    their folders are deleted once their branch is safely back in the project repo.

Settings (~/agent-sandbox/state/agentjobs-settings.json): attempts (2-4), stop_early,
auto_attempts. Standard library only.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import time
from pathlib import Path

import dashboard
import upgrades
from agentjobs_upgrades import bestofn
from agentjobs_upgrades.guard import is_test_file
from agentjobs_upgrades.safegit import GitError, SafeGit

MIN_N, MAX_N = 2, 4
DEFAULT_N = 3
KEEP_DAYS = 7                       # losing attempts stay on their branch this long
FILES_MAX = 40                      # changed files kept per attempt in the job record

ATTEMPT_TAIL = re.compile(r"-try([1-9])-[0-9a-f]{4}$")       # a folder we made for an attempt
TRY_BRANCH_RE = re.compile(r"-try[1-9]$")                    # agent/<job id>-try<k>
SUB_RE = re.compile(r"attempts/try-[1-9]$")


# ---------------------------------------------------------------- the plan

def plan(m, cfg):
    """The job's best-of-N plan, or None for a normal single run. `hard` jobs always run all
    N attempts; otherwise a clean first attempt stops the rest (stop_early)."""
    n = m.get("attempts")
    if isinstance(n, bool) or not isinstance(n, int) or not MIN_N <= n <= MAX_N:
        return None
    se = m.get("stop_early")
    stop_early = bool((cfg or {}).get("stop_early", True)) if not isinstance(se, bool) else se
    return {"n": n, "hard": bool(m.get("hard")), "stop_early": stop_early}


def form_extra(form, cfg):
    """The new-job form -> record fields. {} = a normal job, None = the number is unusable."""
    if not (form.get("hard", [""])[0]).strip():
        return {}
    raw = (form.get("attempts", [""])[0]).strip()
    try:
        n = int(raw) if raw else int((cfg or {}).get("attempts") or DEFAULT_N)
    except ValueError:
        return None
    if not MIN_N <= n <= MAX_N:
        return None
    return {"attempts": n, "hard": True}


def new_job_fields(cfg):
    """The two fields on the new-job form (plain words, no jargon)."""
    n = int((cfg or {}).get("attempts") or DEFAULT_N)
    n = min(MAX_N, max(MIN_N, n))
    return (f'<div class="brow" style="margin-top:6px">'
            f'<label style="font-weight:400;margin:0"><input type="checkbox" name="hard"> '
            f'Hard job: try it {n} times</label>'
            f'<label style="font-weight:400;margin:0">Times <input type="number" name="attempts" '
            f'min="{MIN_N}" max="{MAX_N}" value="{n}" style="width:70px;display:inline-block"></label></div>'
            f'<p class="hint">A hard job is tried {n} times in a fresh copy of the project, one after '
            f'another (about {n}&times; longer). The best try is kept: checks passing first, then more '
            f'tests passing, then a smaller change. Every try stays on its own branch for '
            f'{KEEP_DAYS} days.</p>')


# ---------------------------------------------------------------- names

def attempt_folder(folder, jid, k):
    return f"{folder[:40]}-try{k}-{jid[-4:]}"


def attempt_branch(jid, k):
    return f"agent/{jid}-try{k}"


def job_branch(jid):
    return f"agent/{jid}"


def attempt_sub(k):
    return f"attempts/try-{k}"


def is_attempt_folder(name):
    """A ~/agent-work folder this module made - it must not clutter the folder picker."""
    return bool(ATTEMPT_TAIL.search(str(name or "")))


# ---------------------------------------------------------------- git, the safe way

def _host_git(repo):
    """git on the host for the two steps that need two folders at once. safegit's flags
    (hooks off, the job repo's config not trusted) plus the file transport only, and never
    a working tree: nothing an agent planted in a repo can run here."""
    return SafeGit(repo, wrap=None, timeout=900)


def _jail_git(root, folder, jid):
    return SafeGit(root, wrap=upgrades.make_wrap(root, folder, f"agentgit-{jid}"), timeout=600)


def prepare(m, k, work, base):
    """A fresh copy of the job's base commit in its own folder, on agent/<id>-try<k>."""
    folder = attempt_folder(m["folder"], m["id"], k)
    src = Path(work) / m["folder"]
    dest = Path(work) / folder
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    _host_git(src).run("-c", "protocol.file.allow=always", "clone", "-q", "--no-checkout",
                       str(src), str(dest))
    g = _jail_git(dest, folder, m["id"])
    g.run("checkout", "-q", "-B", attempt_branch(m["id"], k), base)
    return folder, g


def changes(git, base, head):
    """(files, changed lines, test files changed) for one attempt: [path, added, deleted]."""
    if not base or not head:
        return [], 0, 0
    out = git.run("diff", "--numstat", "-M", base, head, check=False)
    files, lines, test_files = [], 0, 0
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) < 3:
            continue
        path = "\t".join(parts[2:])
        if "=>" in path:                                      # renames: "a.py => b.py"
            path = path.split(" => ")
            path = (path[-1] if len(path) < 2 else
                    (path[0].split(" -> ")[-1] if " -> " in path[0] else path[-1]))
            path = path.replace("{", "").replace("}", "").rstrip("/. ").strip() or path[-1]
        add = int(parts[0]) if parts[0].isdigit() else 0
        dele = int(parts[1]) if parts[1].isdigit() else 0
        lines += add + dele
        if is_test_file(path):
            test_files += 1
        if len(files) < FILES_MAX:
            files.append([path, add, dele])
    return files, lines, test_files


def collect(m, k, git, base):
    """Save what the agent left uncommitted, measure the attempt, and pack its branch into a
    bundle inside the attempt folder (a bundle carries no repo config). Raises GitError."""
    branch = attempt_branch(m["id"], k)
    if git.run("status", "--porcelain").strip():
        git.run("add", "-A")
        git.run("commit", "-qm", f"attempt {k}: what was left in the folder")
    head = git.head() or ""
    files, lines, test_files = changes(git, base, head)
    bundle = Path(git.repo) / ".git" / f"agentjobs-try-{k}.bundle"
    git.run("bundle", "create", str(bundle), f"{base}..{branch}")
    return {"commit": head, "files": files, "diff_lines": lines, "test_files_changed": test_files,
            "bundle": str(bundle)}


def bring_home(m, work, bundle, branch):
    """Put an attempt's branch into the project repo (from its bundle, never from the attempt
    repo's own config). Raises GitError."""
    _host_git(Path(work) / m["folder"]).run("-c", "protocol.file.allow=always", "fetch", "-q",
                                           str(bundle), f"{branch}:refs/heads/{branch}")


def apply_winner(m, work, branch):
    """Check the winning attempt out as the job's own branch in the project folder - inside the
    project's jail. Returns an error text, or "" when it worked."""
    root = Path(work) / m["folder"]
    g = _jail_git(root, m["folder"], m["id"])
    try:
        g.run("checkout", "-q", "-B", job_branch(m["id"]), branch)
        return ""
    except GitError as ex:
        return (f"Could not put attempt {branch} in the folder ({str(ex)[:180]}). The branch is "
                f"still there - roll back to a checkpoint or use it by hand.")


def prune_old(git, days=KEEP_DAYS, now=None):
    """Delete attempt branches older than `days` (the losing tries of earlier jobs)."""
    now = now if now is not None else time.time()
    out = git.run("for-each-ref", "--format=%(refname) %(committerdate:unix)", "refs/heads/agent/")
    removed = []
    for line in out.splitlines():
        ref, _, ts = line.rpartition(" ")
        if not ref or not TRY_BRANCH_RE.search(ref):
            continue
        try:
            old = now - int(ts) > days * 86400
        except ValueError:
            old = False
        if old:
            git.run("update-ref", "-d", ref)
            removed.append(ref)
    return removed


# ---------------------------------------------------------------- scores

def status_of(run_meta):
    """One finished attempt as bestofn sees it: the check result, or how the run itself ended."""
    st = (run_meta or {}).get("status")
    if st == "safety_stop":
        return "safety_stop"
    if st in ("stopped", "cancelled", "interrupted"):
        return "stopped"
    return ((run_meta or {}).get("verify") or {}).get("status") or "unverified"


def tests_passed(run_meta):
    res = (((run_meta or {}).get("verify") or {}).get("final") or {}).get("results") or []
    return sum(int(r.get("passed") or 0) for r in res)


def trimmed_verify(run_meta):
    v = (run_meta or {}).get("verify") or {}
    return {k: v[k] for k in ("status", "banner", "timeline", "issues", "final") if k in v}


def attempt_of(rec):
    """A stored attempt record -> the bestofn.Attempt the ranking works on."""
    return bestofn.Attempt(k=int(rec.get("k") or 0), branch=str(rec.get("branch") or ""),
                           status=str(rec.get("status") or "unverified"),
                           tests_passed=int(rec.get("tests_passed") or 0),
                           test_files_changed=int(rec.get("test_files_changed") or 0),
                           diff_lines=int(rec.get("diff_lines") or 0),
                           seconds=float(rec.get("seconds") or 0.0),
                           issues=list(rec.get("issues") or []))


def attempt_job(m, k):
    """The prompt for attempt k. In the hidden fake mode the job text may be
    {"attempts": [scenario, ...]} so a test can give every attempt its own scenario."""
    if m.get("mode") != upgrades.FAKE_MODE:
        return m["job"]
    try:
        sc = json.loads(m["job"])
    except (TypeError, ValueError):
        return m["job"]
    if isinstance(sc, dict) and isinstance(sc.get("attempts"), list) and sc["attempts"]:
        return json.dumps(sc["attempts"][min(k, len(sc["attempts"])) - 1])
    return m["job"]


# ---------------------------------------------------------------- the run

def run_best_of_n(m, name, *, work, job_dir, load, update, run_one, settings):
    """N fresh attempts one after another, then the winner's commits go on the job branch.

    run_one(meta, name, fresh_log) is jobboard.run_agent_job: the agent run, the checks and
    the fix rounds, all in the folder the meta points at. Returns the final job meta."""
    jid, jd = m["id"], job_dir(name)
    p = plan(m, settings) or {"n": DEFAULT_N, "hard": True, "stop_early": False}
    n, hard, stop_early = p["n"], p["hard"], p["stop_early"]
    base = m.get("git_start")
    work = Path(work)

    if not base:
        note = "Best of N needs a git folder, so this job ran once instead."
        update(name, attempts_note=note, phase=None)
        dashboard.tl_add(jd, "attempt", note)
        return run_one(m, name, True)

    log = []
    update(name, attempts_total=n, attempts_done=0, attempts_current=0, attempt_log=[],
           attempts_note=None, phase=f"Attempt 1 of {n}")
    main_git = _jail_git(Path(work) / m["folder"], m["folder"], jid)
    try:                                   # old attempts of any job in this folder, past 7 days
        gone = prune_old(main_git)
        if gone:
            dashboard.tl_add(jd, "attempt", f"Removed {len(gone)} attempt branch(es) older than "
                                            f"{KEEP_DAYS} days")
    except GitError:
        pass
    stopped, how = False, None

    for k in range(1, n + 1):
        if (load(name) or {}).get("status") in ("stopping", "cancelled"):
            stopped, how = True, "stopped"
            break
        update(name, phase=f"Attempt {k} of {n}", attempts_current=k)
        dashboard.tl_add(jd, "attempt", f"Attempt {k} of {n} started")
        folder = attempt_folder(m["folder"], jid, k)
        try:
            folder, ag = prepare(m, k, work, base)
        except (GitError, OSError, ValueError) as ex:
            note = f"Attempt {k} could not start: {str(ex)[:200]}"
            update(name, attempts_note=note)
            dashboard.tl_add(jd, "attempt", note)
            break
        am = dict(m, folder=folder, resume=None, attempt_sub=attempt_sub(k), job=attempt_job(m, k))
        out = run_one(am, name, k == 1) or {}
        extra, note = {}, None
        try:
            extra = collect(m, k, ag, base)
            bring_home(m, work, extra["bundle"], attempt_branch(jid, k))
            shutil.rmtree(work / folder, ignore_errors=True)
        except (GitError, OSError) as ex:
            note = (f"Attempt {k}'s branch could not be saved ({str(ex)[:150]}). Its copy is "
                    f"left in ~/agent-work/{folder}.")
        rec = {"k": k, "folder": folder, "branch": attempt_branch(jid, k), "status": status_of(out),
               "tests_passed": tests_passed(out),
               "test_files_changed": extra.get("test_files_changed", 0),
               "diff_lines": extra.get("diff_lines", 0), "files": extra.get("files", []),
               "commit": extra.get("commit", ""),
               "seconds": round(float(out.get("finished") or 0) - float(out.get("started") or 0), 1),
               "issues": (out.get("verify") or {}).get("issues") or [],
               "session": out.get("session"), "watchdog": len(out.get("watchdog") or []),
               "checkpoints": len(((out.get("checkpoints") or {}).get("checkpoints") or [])),
               "banner": ((out.get("verify") or {}).get("banner") or {}).get("text") or "",
               "verify": trimmed_verify(out), "note": note}
        log.append(rec)
        update(name, attempt_log=log, attempts_done=len(log))
        dashboard.tl_add(jd, "attempt",
                         f"Attempt {k} of {n}: {STATUS_WORDS.get(rec['status'], rec['status'])}"
                         + (f" - {rec['tests_passed']} tests passing" if rec["tests_passed"] else ""))
        if rec["status"] == "safety_stop":
            how = "safety_stop"
            break
        if (load(name) or {}).get("status") in ("stopping", "cancelled"):
            stopped, how = True, "stopped"
            break
        if not bestofn.should_continue([attempt_of(r) for r in log], n, hard, stop_early):
            dashboard.tl_add(jd, "attempt", f"No more attempts: attempt {k} passed cleanly")
            break

    winner, ranked = bestofn.pick_winner([attempt_of(r) for r in log])
    by_k = {a["k"]: a for a in log}
    chosen = winner or ({"k": ranked[0].k} if ranked else None)
    final = {"winner": (winner.k if winner else None), "why": bestofn.why(winner, ranked),
             "ranked": [a.k for a in ranked]}

    if stopped or not log:
        cur = update(name, attempts_final=final, phase=None,
                     fail_note="Stopped before all attempts finished.") or {}
        dashboard.tl_add(jd, "failed", "Stopped: " + final["why"])
        return cur

    note = None
    if chosen:
        err = apply_winner(m, work, attempt_branch(jid, chosen["k"]))
        if err:
            note = err
    best = by_k.get((ranked[0].k if ranked else None) or -1) or {}
    if winner:
        status, fail = "done", None
    elif how == "safety_stop":
        status, fail = "safety_stop", best.get("banner") or "Safety stop"
    else:
        status, fail = "failed", final["why"]
    v = dict(best.get("verify") or {})
    if status == "failed" and v.get("banner") and (v["banner"] or {}).get("colour") == "green":
        v["banner"] = {"colour": "red", "text": final["why"]}
    cur = update(name, status=status, finished=time.time(), phase=None, attempt_log=log,
                 attempts_final=final, attempts_current=0, attempts_note=note,
                 verify=v or None, fail_note=fail,
                 session=(by_k.get(winner.k) or {}).get("session") if winner else None,
                 checkpoints=None, checkpoints_note=None) or {}
    dashboard.tl_add(jd, "finished" if status == "done" else "failed", dashboard.finished_text(cur))
    return cur


# ---------------------------------------------------------------- the buttons

def use_attempt(m, k, *, work, update, job_dir):
    """"Use this one instead": the job branch becomes that attempt's branch. Returns an error
    text, or "" when it worked."""
    try:
        k = int(k)
    except (TypeError, ValueError):
        return "No attempt given."
    rec = next((r for r in (m.get("attempt_log") or []) if int(r.get("k") or 0) == k), None)
    if not rec or not rec.get("branch"):
        return "That attempt is not in this job's list."
    g = _jail_git(Path(work) / m["folder"], m["folder"], m["id"])
    if not g.ok("rev-parse", "--verify", "-q", f"refs/heads/{rec['branch']}"):
        return (f"Branch {rec['branch']} is gone - attempts are kept for {KEEP_DAYS} days.")
    err = apply_winner(m, work, rec["branch"])
    if err:
        return err
    update(m["id"], attempts_winner=k, attempts_note=f"Using attempt {k} (branch {rec['branch']}).")
    dashboard.tl_add(job_dir(m["id"]), "attempt", f"Using attempt {k} instead: {rec['branch']}")
    return ""


def retry_extra(m, cfg):
    """Fields for a fresh best-of-N run of a job that failed after its fix rounds."""
    n = int(m.get("attempts") or (cfg or {}).get("attempts") or DEFAULT_N)
    n = min(MAX_N, max(MIN_N, n))
    carry = {k: m[k] for k in ("checks", "checks_source", "verify_off", "fix_rounds") if k in m}
    carry.update({"attempts": n, "hard": False})
    return carry


# ---------------------------------------------------------------- the page

WORD = {"passed": "passed", "failed": "checks failed", "needs_review": "needs review",
        "stuck": "stuck", "unverified": "not verified", "safety_stop": "safety stop",
        "stopped": "stopped"}
TONE = {"passed": "ok", "failed": "bad", "needs_review": "warn2", "stuck": "warn2",
        "unverified": "warn", "safety_stop": "bad", "stopped": "mut"}


def _esc(x):
    return html.escape(str(x if x is not None else ""), quote=True)


def row_html(m, a, can_act, winner_k):
    tone = TONE.get(a.get("status"), "")
    btn = ""
    if can_act and a.get("branch") and a.get("commit") and a["k"] != winner_k:
        btn = (f'<form method="post" action="/use-attempt/{_esc(m["id"])}" style="display:inline" '
               f'onsubmit="return confirm(\'Use attempt {int(a["k"])} instead? The folder will '
               f'switch to its changes.\')"><input type="hidden" name="k" value="{int(a["k"])}">'
               f'<button class="ghost2">Use this one instead</button></form>')
    files = "".join(f'<li>{_esc(f[0])} <span class="mut">+{f[1]} -{f[2]}</span></li>'
                    for f in (a.get("files") or [])[:12])
    more = len(a.get("files") or []) - 12
    change = (f'<details><summary>{len(a.get("files") or [])} files, {_esc(a.get("diff_lines", 0))} lines'
              + (f' ({more} more)' if more > 0 else '') + '</summary><ul>' + files + '</ul></details>'
              if a.get("files") else '<span class="mut">nothing changed</span>')
    tests = f'{a.get("tests_passed", 0)} passing' + (f', {a.get("test_files_changed", 0)} test files'
                                                    if a.get("test_files_changed") else "")
    secs = float(a.get("seconds") or 0)
    return (f'<tr><td>{int(a["k"])}</td><td class="t-{tone}">{_esc(WORD.get(a.get("status"), a.get("status")))}'
            + (' <span class="mut">(kept)</span>' if a["k"] == winner_k else '') + '</td>'
            f'<td>{_esc(tests)}</td><td>{change}</td><td>{secs / 60:.1f} min</td>'
            f'<td><code>{_esc(a.get("branch"))}</code>{("<br>" + _esc(a["note"])) if a.get("note") else ""}</td>'
            f'<td>{btn}</td></tr>')


def card_html(m, jid, can_act, cfg=None):
    log = m.get("attempt_log") or []
    fin = m.get("attempts_final") or {}
    note = m.get("attempts_note")
    running = m.get("status") in ("queued", "running", "stopping")
    if not log and not note and not (m.get("attempts") and running):
        return ""
    n = m.get("attempts_total") or m.get("attempts") or 0
    parts = []
    if running and n:
        now = m.get("attempts_current") or 0
        parts.append(f'<p><b>Attempt {max(1, now)} of {n}</b> <span class="hint">'
                     f'(a hard job is tried {n} times, one after another: about {n}&times; longer)</span></p>')
    if fin.get("why"):
        parts.append(f'<p class="vline">{_esc(fin["why"])}'
                     + (f' <span class="hint">best of {len(log)}</span>' if len(log) > 1 else '') + '</p>')
    if log:
        winner_k = m.get("attempts_winner")
        if winner_k is None:
            winner_k = fin.get("winner")
        rows = "".join(row_html(m, a, can_act, winner_k) for a in log)
        parts.append('<div class="tbl"><table><thead><tr><th>Try</th><th>Result</th><th>Tests</th>'
                     f'<th>What it changed</th><th>Time</th><th>Branch</th><th></th></tr></thead>'
                     f'<tbody>{rows}</tbody></table></div>')
        parts.append(f'<p class="hint">Every try started from the same commit, in a copy of the project '
                     f'only it could see, with the usual checks, fix rounds and watchdog. The tries stay '
                     f'on their branch for {KEEP_DAYS} days.</p>')
    if note:
        parts.append(f'<p class="mut">{_esc(note)}</p>')
    if can_act and not m.get("attempts") and not log \
            and (m.get("verify") or {}).get("status") == "failed":
        k = min(MAX_N, max(MIN_N, int((cfg or {}).get("attempts") or DEFAULT_N)))
        parts.append(f'<form method="post" action="/try-hard/{_esc(jid)}" style="margin-top:10px">'
                     f'<button class="ghost2">Try {k} fresh attempts</button> '
                     f'<span class="hint">each try in its own copy of the project, from the same commit '
                     f'(about {k}&times; longer)</span></form>')
    return f'<div class="card"><h2>Attempts</h2>{"".join(parts)}</div>'
