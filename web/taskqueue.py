#!/usr/bin/env python3
"""Upgrade 4b: an approved plan runs as a queue of jobs (added 2026-11-11 by Claude;
jobboard.py keeps small "# U04b" hooks).

Jason approves a plan (upgrade 4a) and the tasks become a queue. The board runs them one at a
time in the project folder - the GPU serves one stream, and every task starts from where the
task before it left the folder, so each one builds on the previous one's final commit. Each
task is an ordinary job: the watchdog, the checkpoints, the checks and the fix rounds all apply
(`jobboard.process_job` is not bypassed anywhere here), and a big task - or one that failed
before - is tried several times when the project has that switched on (upgrade 5's best-of-N).
The queue itself is `agentjobs_upgrades/plan.py`'s `Queue` (not rewritten here): dependency
order, "stop the queue" or "skip the tasks that depend on it" when a task fails, the
"don't start new tasks after" time, and recovery after a restart.

  * Queue state: `state/queues/<folder>.json` (env `JOBBOARD_QUEUES`) - outside everything the
    sandbox mounts, like the job and project records, so an agent cannot mark its own task done,
    skip a task or move the queue onto another folder. The tasks come from the APPROVED copy in
    the project record, not from the (agent-writable) `tasks.json` in the folder.
  * The job text for a task is `plan.job_prompt`: a 10-line summary of the outline, the
    milestone as ROADMAP.md describes it, the task, its acceptance criteria, and "read
    AGENT_NOTES.md first, add 2-5 lines of lessons when you finish".
  * After every task the board checks AGENT_NOTES.md really was updated and says so in the job
    record (and on the job's timeline) when it was not.
  * Nothing runs just because a plan was approved: `create` writes the queue and leaves it
    "ready". Pressing Start the tasks is what sets it going - the same rule 4a promised.
  * git for the queue goes through `safegit.SafeGit` in that folder's own jail
    (`upgrades.make_wrap` -> `jail/run-local.sh sandbox-run`); the project branch is kept, and a
    folder with anything uncommitted is never touched.

Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

import attempts
import dashboard
import planning
import upgrades
from agentjobs_upgrades import plan as pl
from agentjobs_upgrades.safegit import SafeGit

JOB_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")       # the same shape jobboard generates

esc = dashboard.esc
NOTES_FILE = "AGENT_NOTES.md"
MINUTES = {"S": 20, "M": 45}                    # what the planner prompt says S and M mean
OUTLINE_LINES = 10
OUTLINE_MAX = 1200
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
CUTOFF_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
OPEN_STATES = ("queued", "interrupted", "running")
DONE_LIKE = ("done", "failed", "skipped")

# the queue file is written by the worker thread and by the page buttons at the same time.
# Re-entrant: action() holds it while it calls start_next(), and job_finished() holds it while
# it queues the next task's job.
_lock = threading.RLock()

STATUS = {"idle": "not started", "ready": "ready to start", "running": "running",
          "paused": "paused", "stopped": "stopped after a failed task",
          "cancelled": "cancelled", "finished": "finished"}
STATE_WORD = {"queued": "waiting", "interrupted": "interrupted", "running": "running now",
              "done": "done", "failed": "failed", "skipped": "skipped"}
STATE_CLS = {"queued": "mut", "interrupted": "warn2", "running": "ok", "done": "ok",
             "failed": "bad", "skipped": "warn"}
NOTES = {"started": "The tasks are queued - they run one at a time.",
         "paused": "Paused. The task that is running finishes; nothing new starts.",
         "resumed": "Carrying on with the queue.",
         "cancelled": "Queue cancelled. A task that was already running was stopped.",
         "saved": "Queue settings saved.",
         "retried": "That task is queued again - this time it is tried several times.",
         "badtime": "Use a time like 06:30.",
         "started-nothing": "Nothing could start - every task is done, skipped or blocked."}


# ------------------------------------------------------------------ the queue file

def q_path(queues, folder):
    """The queue file for a project (same folder-name rules as the project records)."""
    return planning.rec_path(queues, folder)


def load(queues, folder):
    """The project's `plan.Queue`, or None (no queue, or a file that is not a queue)."""
    p = q_path(queues, folder)
    if not p or not p.is_file():
        return None
    try:
        q = pl.Queue(p)
    except Exception:
        return None
    return q if isinstance(getattr(q, "state", None), dict) and isinstance(q.state.get("tasks"), list) else None


def create(queues, rec, on_fail="stop"):
    """Approving a plan turns its approved tasks into a queue, in dependency order. Nothing
    runs yet: the queue waits "ready" until Start the tasks is pressed. A queue that is already
    under way is left alone; one that was never started is replaced (approve again after an
    edit). Returns the Queue, or None."""
    p = q_path(queues, rec.get("folder"))
    if not p:
        return None
    old = load(queues, rec.get("folder"))
    if old and old.state.get("status") in ("running", "paused", "stopped"):
        return old
    try:
        q = pl.Queue(p)
        q.load_plan(json.loads(json.dumps(rec.get("tasks") or [])),
                    on_fail="skip" if on_fail == "skip" else "stop")
    except (ValueError, TypeError, OSError):
        return None
    q.state["status"] = "ready"
    q.state["approved_at"] = rec.get("approved_at")
    q.save()
    return q


def entry(q, task_id):
    for t in q.state["tasks"]:
        if str(t.get("id")) == str(task_id):
            return t
    return None


def queues_of(queues):
    """Every queue on disk: [folder, Queue]."""
    out = []
    try:
        names = sorted(p.stem for p in Path(queues).glob("*.json"))
    except OSError:
        return out
    for name in names:
        q = load(queues, name)
        if q is not None:
            out.append([name, q])
    return out


# ------------------------------------------------------------------ the task's prompt

def outline_summary(rec):
    """The first ten lines of Jason's outline - enough to tell a task what the app is for."""
    lines = [ln.strip() for ln in str(rec.get("outline") or "").splitlines() if ln.strip()]
    return "\n".join(lines[:OUTLINE_LINES])[:OUTLINE_MAX]


def roadmap_sections(text):
    """ROADMAP.md -> [(heading, body), ...], one entry per milestone heading."""
    out, head, body = [], "", []
    for ln in str(text or "").splitlines():
        if ln.startswith("#"):
            if head:
                out.append((head, "\n".join(body).strip()))
            head, body = ln.lstrip("#").strip(), []
        elif head:
            body.append(ln)
    if head:
        out.append((head, "\n".join(body).strip()))
    return out


def flat(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def milestone_text(root, milestone, tasks):
    """What ROADMAP.md says about this task's milestone (its heading and its sentences); falls
    back to the milestone name and the tasks in it when the planner wrote no roadmap."""
    want = flat(milestone)
    for head, body in roadmap_sections(planning.read_roadmap(root)):
        got = flat(head)
        if want and got and (got == want or got in want or want in got):
            return f"{head}\n{body}".strip()
    titles = "; ".join(str(t.get("title") or "") for t in tasks if t.get("milestone") == milestone)
    return f"{milestone}\n{titles}".strip()


def task_prompt(rec, root, task, pos=None, total=None):
    """The job text for one task: plan.job_prompt (outline summary + milestone + task +
    acceptance + AGENT_NOTES) plus where this task sits in the plan."""
    text = pl.job_prompt(outline_summary(rec),
                         milestone_text(root, task.get("milestone"), rec.get("tasks") or []), task)
    extra = []
    if pos and total:
        extra.append(f"You are task {pos} of {total} in a plan Jason already approved. The tasks "
                     f"before this one are done and committed, so start from what is in the "
                     f"folder now; what they learned is in {NOTES_FILE}.")
    if str(task.get("screen") or "").strip():
        extra.append(f"The screen for this task should look like: {str(task['screen']).strip()}")
    return "\n\n".join([text] + extra)


# ------------------------------------------------------------------ best-of-N, the notes, the time

def best_of_n(task, rec, cfg, tries=0):
    """Upgrade 5 for a queue task: a task the planner marked M, or one that already failed, is
    tried several times - but only when this project has it switched on."""
    if not rec.get("best_of_n"):
        return None
    if str(task.get("size")) != "M" and not tries:
        return None
    try:
        n = int((cfg or {}).get("attempts") or attempts.DEFAULT_N)
    except (TypeError, ValueError):
        n = attempts.DEFAULT_N
    return min(attempts.MAX_N, max(attempts.MIN_N, n))


def notes_hash(root):
    """AGENT_NOTES.md as it is now ("" when the file is not there)."""
    try:
        raw = (Path(root) / NOTES_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def notes_note(root, before):
    """What to write in the job record when this task left no lessons ("" = it did)."""
    after = notes_hash(root)
    if not after:
        return f"No {NOTES_FILE} in the project folder - this task wrote down no lessons."
    if after == (before or ""):
        return f"{NOTES_FILE} was not changed by this task - no lessons for the tasks after it."
    return ""


def git_for(root, folder, tag="queue"):
    return SafeGit(root, wrap=upgrades.make_wrap(root, folder, f"agentgit-{tag}"), timeout=180)


def head_sha(root, folder, tag="queue"):
    if not upgrades.git_repo(root):
        return ""
    try:
        return git_for(root, folder, tag).head() or ""
    except Exception:
        return ""


def keep_branch(root, folder, tag="queue"):
    """Keep the queue's work on the project's own branch. After best-of-N the folder can be
    sitting on agent/<job id>; when nothing is uncommitted the project branch simply moves to
    the same commit - no file in the folder changes. Returns a note ("" = nothing to say)."""
    if not upgrades.git_repo(root):
        return ""
    branch = planning.branch_of(folder)
    try:
        g = git_for(root, folder, tag)
        if g.run("status", "--porcelain").strip():
            return "left the folder on its own branch (something is still uncommitted there)"
        head = g.head()
        if not head or g.branch() == branch:
            return ""
        g.run("checkout", "-q", "-B", branch, head)
        return ""
    except Exception as ex:
        return f"could not move the project branch ({str(ex)[:120]})"


def estimate(q, rec, cfg):
    """(tasks left, minutes left): the planner's own sizes, times the number of tries for a task
    that will be tried several times, corrected by what the finished tasks actually took."""
    left, total = 0, 0.0
    actual = {}
    for size in ("S", "M"):
        vals = [float(t.get("seconds") or 0) for t in q.state["tasks"]
                if t.get("state") == "done" and str(t.get("size")) == size and t.get("seconds")]
        if vals:
            actual[size] = sum(vals) / len(vals) / 60.0
    for t in q.state["tasks"]:
        if t.get("state") not in OPEN_STATES:
            continue
        left += 1
        size = str(t.get("size") or "M")
        mins = actual.get(size) or MINUTES.get(size, 45)
        total += mins * (best_of_n(t, rec, cfg, int(t.get("tries") or 0)) or 1)
    return left, total


def fmt_mins(mins):
    if mins <= 0:
        return "nothing left"
    h, m = divmod(int(round(mins)), 60)
    return f"about {h}h {m:02d}m" if h else f"about {m} min"


def progress_lines(q):
    return [f'{p["milestone"]}: {p["done"]} of {p["total"]} task'
            f'{"s" if p["total"] != 1 else ""}' for p in q.progress()]


def idle_reason(q):
    """Why a running queue has nothing to start right now, in plain words."""
    cut = q.state.get("stop_starting_at")
    if cut and q.now() >= cut:
        return ("No new task starts after "
                f"{time.strftime('%H:%M', time.localtime(cut))} - press Resume to carry on.")
    open_ = [t for t in q.state["tasks"] if t.get("state") in ("queued", "interrupted")]
    bad = [t for t in q.state["tasks"] if t.get("state") in ("failed", "skipped")]
    if open_ and bad:
        return "Waiting: a task it comes after is not finished."
    if open_:
        return "Waiting for the task before it."
    return ""


# ------------------------------------------------------------------ starting the next task

def start_next(folder, ctx):
    """Start the next task of this project's queue, if one may start. One job at a time is not
    this function's doing - it queues ONE job and the board's worker runs jobs one at a
    time. Returns the new job's record, or None."""
    q = load(ctx["queues"], folder)
    rec = planning.load(ctx["plans"], folder)
    if q is None or rec is None:
        return None
    with _lock:
        t = q.next_task()
        if t is None:
            return None
        if ctx.get("busy") and ctx["busy"](folder):
            return None
        cfg = ctx["settings"]()
        root = Path(ctx["work"]) / folder
        tasks = q.state["tasks"]
        pos = tasks.index(t) + 1
        tries = int(t.get("tries") or 0)
        extra = {"task_id": str(t["id"])[:40], "task_try": tries + 1,
                 "notes_before": notes_hash(root)}
        checks = upgrades.form_checks("\n".join(str(c) for c in (t.get("checks") or [])))
        if checks and upgrades.checks_ok(checks):
            extra.update(checks=checks, checks_source="the approved plan")
        n = best_of_n(t, rec, cfg, tries)
        if n:
            extra.update(attempts=n, hard=False)
        # the job of the task before this one in the plan: the new job is its follow-up, and
        # "starts from the previous job's final commit" is that job's last commit.
        prev = None
        for x in tasks[:tasks.index(t)][::-1]:
            jid = str(x.get("job_id") or "")
            if linked_job(jid, ctx):
                prev = jid
                break
        own = str(t.get("job_id") or "")
        own = own if linked_job(own, ctx) else None
        resume = ctx["session_of"](own) if t.get("state") == "interrupted" else None
        note = keep_branch(root, folder)
        base = head_sha(root, folder)
        mode = rec.get("mode") if rec.get("mode") in ctx["modes"] else ctx["default_mode"]
        m = ctx["new_job"](mode, folder, task_prompt(rec, root, t, pos, len(tasks)), False,
                           resume=resume, parent=prev or own, extra=extra)
        t["tries"] = tries + 1
        if base:
            t["base"] = base
        if note:
            t["branch_note"] = note
        q.mark(t["id"], "running", m["id"])
        what = f"{t.get('title') or t['id']}"
        if n:
            what += f" - tried {n} times"
        dashboard.tl_add(ctx["job_dir"](m["id"]), "queue",
                         f"Task {pos} of {len(tasks)} ({t.get('milestone')}): {what}")
        if note:
            dashboard.tl_add(ctx["job_dir"](m["id"]), "queue", note)
        return m


def linked_job(jid, ctx):
    """A queue may only link to a job record that is really there (check_record refuses a
    parent whose record is gone)."""
    return bool(jid) and bool(JOB_ID_RE.fullmatch(jid)) and bool(ctx["load"](jid))


def job_finished(m, ctx):
    """A job that came from a plan queue has ended. What happens here, in order:

      * AGENT_NOTES.md: if the task did not add its lessons, the job record says so (and the
        job's timeline does).
      * the task gets its seconds and its end commit, and is marked done / failed / interrupted.
        A failure follows the project's choice: stop the queue, or skip the tasks that come
        after it and carry on. A task you stopped goes back to interrupted and the queue pauses.
      * the next task is queued when the queue is still running.

    Returns the next job's record, or None."""
    if not m or not m.get("task_id") or m.get("kind") == "bench":
        return None
    folder = m.get("folder")
    q = load(ctx["queues"], folder)
    if q is None:
        return None
    t = entry(q, m["task_id"])
    if t is None:
        return None
    root = Path(ctx["work"]) / folder
    status = m.get("status")
    jd = ctx["job_dir"](m["id"]) if ctx.get("job_dir") else None
    if status in ("done", "failed", "safety_stop"):
        note = notes_note(root, m.get("notes_before"))
        if note:
            ctx["update"](m["id"], notes_note=note)
            if jd:
                dashboard.tl_add(jd, "queue", note)
    try:
        if m.get("started") and m.get("finished"):
            t["seconds"] = round(float(m["finished"]) - float(m["started"]), 1)
    except (TypeError, ValueError):
        pass
    end = head_sha(root, folder)
    if end:
        t["end"] = end
    with _lock:
        skipped = sum(1 for x in q.state["tasks"] if x.get("state") == "skipped")
        if status == "done":
            q.mark(t["id"], "done", m["id"])
        elif status in ("stopped", "cancelled", "interrupted"):
            q.mark(t["id"], "interrupted", m["id"])
            if q.state.get("status") == "running":
                q.state["status"] = "paused"
                q.save()
                if jd:
                    dashboard.tl_add(jd, "queue", "The queue is paused - press Resume when you "
                                                 "want the next task to start.")
        else:
            q.mark(t["id"], "failed", m["id"])
            if q.state.get("status") == "stopped":
                if jd:
                    dashboard.tl_add(jd, "queue", f"Task {t['id']} failed, so the queue stopped. "
                                                  "Press Run it again for that task, or choose to "
                                                  "skip the tasks that depend on it.")
            else:
                new = sum(1 for x in q.state["tasks"] if x.get("state") == "skipped") - skipped
                if new > 0 and jd:
                    dashboard.tl_add(jd, "queue", f"Task {t['id']} failed: {new} task"
                                                  f"{'s' if new != 1 else ''} that depend on "
                                                  f"{'it were' if new != 1 else 'it was'} skipped.")
    if q.state.get("status") == "running":
        return start_next(folder, ctx)
    return None


def startup(queues, ctx):
    """Board start-up (after the job records were marked interrupted): a task that was running
    becomes interrupted, and a queue that was running starts it again from where its job left
    the folder - that is where its last checkpoint is."""
    out = []
    for folder, q in queues_of(queues):
        hit = q.recover()
        for tid in hit:
            t = entry(q, tid)
            prev = str((t or {}).get("job_id") or "")
            if not t or not prev or not ctx["load"](prev):
                continue
            ck = ((ctx["load"](prev) or {}).get("checkpoints") or {}).get("checkpoints") or []
            if ck:
                t["resume_from"] = str(ck[-1].get("commit") or "")[:40]
                q.save()
                dashboard.tl_add(ctx["job_dir"](prev), "queue",
                                 f"Interrupted by a restart - {t['id']} starts again from "
                                 f"checkpoint {ck[-1].get('n')} of that job.")
            else:
                dashboard.tl_add(ctx["job_dir"](prev), "queue",
                                 f"Interrupted by a restart - {t['id']} starts again in the "
                                 f"folder as it stands.")
        if q.state.get("status") == "running":
            m = start_next(folder, ctx)
            if m:
                out.append(m["id"])
    return out


# ------------------------------------------------------------------ the project page's buttons

def action(folder, form, ctx):
    """The queue card's one endpoint. Returns a NOTES key for the project page."""
    q = load(ctx["queues"], folder)
    if q is None:
        return "badtime"          # no card without a queue; the page says what happened
    rec = planning.load(ctx["plans"], folder)
    act = (form.get("act", [""])[0]).strip()
    get = lambda k: (form.get(k, [""])[0]).strip()
    with _lock:
        if act == "setup":
            if rec is not None:
                if get("mode") in ctx["modes"]:
                    rec["mode"] = get("mode")
                    planning.save(ctx["plans"], rec)
                rec["best_of_n"] = bool(form.get("best"))
                planning.save(ctx["plans"], rec)
            if get("onfail") in ("stop", "skip"):
                q.state["on_fail"] = get("onfail")
            if "after" in form:
                hm = get("after")
                if hm and not CUTOFF_RE.match(hm):
                    q.save()
                    return "badtime"
                q.set_cutoff(hm or None)
            else:
                q.save()
            return "saved"
        if act in ("start", "resume"):
            if act == "resume" and q.state.get("stop_starting_at") \
                    and q.now() >= q.state["stop_starting_at"]:
                q.set_cutoff(None)      # "press Resume to carry on" has to mean something
            if q.state.get("status") in ("cancelled", "ready", "paused", "stopped", "finished", "idle"):
                q.state["status"] = "running"
                q.save()
            if start_next(folder, ctx) is None and not any(
                    t.get("state") == "running" for t in q.state["tasks"]):
                return "started-nothing"
            return "started" if act == "start" else "resumed"
        if act == "pause":
            q.state["status"] = "paused"
            q.save()
            return "paused"
        if act == "cancel":
            q.state["status"] = "cancelled"
            q.save()
            for t in q.state["tasks"]:
                if t.get("state") == "running" and t.get("job_id") and ctx.get("stop"):
                    ctx["stop"](t["job_id"])
            return "cancelled"
        if act == "retry":
            t = entry(q, get("task"))
            if t is None:
                return "started-nothing"
            if t.get("state") not in ("failed", "interrupted", "skipped"):
                return "started-nothing"
            t["state"] = "queued"
            q.state["status"] = "running"
            q.save()
            start_next(folder, ctx)
            return "retried"
    return "badtime"


def task_mode(rec, ctx):
    return rec.get("mode") if rec.get("mode") in ctx["modes"] else ctx["default_mode"]


# ------------------------------------------------------------------ the card on the project page

def row_html(folder, t, can_act):
    st = str(t.get("state") or "queued")
    job = (f'<a href="/job/{esc(t["job_id"])}">{esc(t["job_id"])}</a>' if t.get("job_id")
           else '<span class="mut">-</span>')
    again = ""
    if can_act and st in ("failed", "skipped", "interrupted"):
        again = (f'<form method="post" action="/queue/{esc(folder)}" style="display:inline">'
                 f'<input type="hidden" name="act" value="retry">'
                 f'<input type="hidden" name="task" value="{esc(t["id"])}">'
                 f'<button class="ghost2">Run it again</button></form>')
    tries = f' <span class="hint">try {t["tries"]}</span>' if int(t.get("tries") or 0) > 1 else ""
    note = ""
    if t.get("notes_note"):
        note = f'<div class="hint warn2">{esc(t["notes_note"])}</div>'
    return (f'<tr><td class="now"><code>{esc(t["id"])}</code></td>'
            f'<td>{esc(t.get("title") or "")}<div class="hint">{esc(t.get("milestone") or "")}</div></td>'
            f'<td class="now"><span class="{STATE_CLS.get(st, "mut")}">{esc(STATE_WORD.get(st, st))}</span>'
            f'{tries}{note}</td><td class="now">{job}</td><td class="now">{again}</td></tr>')


def card_html(rec, ctx, mode_html="", note=""):
    """The task-queue card: progress per milestone, what is running now, a rough time left,
    Pause / Resume / Cancel, and "don't start new tasks after" a time."""
    q = load(ctx["queues"], rec["folder"])
    if q is None:
        return ""
    cfg = ctx["settings"]()
    st = q.state
    tasks = st["tasks"]
    done = sum(1 for t in tasks if t.get("state") == "done")
    bad = sum(1 for t in tasks if t.get("state") in ("failed", "skipped"))
    now = next((t for t in tasks if t.get("state") == "running"), None)
    left, mins = estimate(q, rec, cfg)
    status = STATUS.get(st.get("status"), str(st.get("status")))
    tone = {"running": "ok", "paused": "warn", "stopped": "bad", "cancelled": "mut",
            "finished": "ok"}.get(st.get("status"), "mut")
    pct = round(100 * done / len(tasks)) if tasks else 0
    cut = st.get("stop_starting_at")
    bits = [f'<b class="{tone}">{esc(status)}</b>', f"{done} of {len(tasks)} tasks done"]
    if bad:
        bits.append(f'<span class="bad">{bad} failed or skipped</span>')
    if left:
        bits.append(f"{left} left - {esc(fmt_mins(mins))}")
    ms = "".join(f"<li>{esc(x)}</li>" for x in progress_lines(q))
    cur = (f'<p class="ok"><b>Now:</b> <a href="/job/{esc(now["job_id"])}">{esc(now.get("title") or now["id"])}</a>'
           f' <span class="hint">{esc(now.get("milestone") or "")}</span></p>' if now else
           (f'<p class="mut">{esc(idle_reason(q))}</p>' if st.get("status") == "running" else ""))
    running = bool(now)
    if st.get("status") == "running" and not running and left:
        cur += (f'<p class="hint">{esc(idle_reason(q))}</p>' if idle_reason(q) else "")
    btns = ""
    if st.get("status") in ("ready", "idle"):
        btns += f'<button name="act" value="start">Start the tasks</button>'
    if running or st.get("status") == "running":
        btns += f'<button class="ghost2" name="act" value="pause">Pause</button>'
    if st.get("status") in ("paused", "stopped", "cancelled"):
        btns += f'<button name="act" value="resume">Resume</button>'
    if st.get("status") in ("running", "paused", "stopped", "ready"):
        btns += f'<button class="ghost" name="act" value="cancel">Cancel</button>'
    rows = "".join(row_html(rec["folder"], t, True) for t in tasks)
    cut_val = time.strftime("%H:%M", time.localtime(cut)) if cut else ""
    n = best_of_n({"size": "M"}, rec, cfg, 0) or attempts.DEFAULT_N
    setup = f"""<form method="post" action="/queue/{esc(rec['folder'])}">
<div class="row"><div><label>Which agent runs the tasks</label>
 <select name="mode">{mode_html}</select></div>
<div><label>If a task fails</label><select name="onfail">
<option value="stop"{" selected" if st.get("on_fail") != "skip" else ""}>Stop the queue</option>
<option value="skip"{" selected" if st.get("on_fail") == "skip" else ""}>Skip the tasks that depend on it</option>
</select></div>
<div><label>Don't start new tasks after</label><input type="text" name="after" value="{cut_val}"
 placeholder="06:30" maxlength="5">
<p class="hint">Leave it empty for no time limit. A task already running always finishes.</p></div></div>
<label style="font-weight:400"><input type="checkbox" name="best" value="1"
{"checked" if rec.get("best_of_n") else ""}> Try big tasks (and ones that failed before) {n} times
and keep the best</label>
<div class="brow" style="margin-top:8px"><button class="ghost2" name="act" value="setup">Save these settings</button></div>
</form>"""
    return f"""<div class="card"><h2>Tasks <span class="mut">&middot; {esc(rec['name'])}</span></h2>
{f'<p class="warn2">{esc(note)}</p>' if note else ''}
<p class="mut">{" &middot; ".join(bits)}</p>
<div class="bar s-WORKING"><i style="width:{pct}%"></i></div>
{cur}
<ul class="msprog">{ms}</ul>
<div class="brow">{btns or '<span class="hint">Nothing to do here right now.</span>'}</div>
<div class="tbl"><table><thead><tr><th class="now">Task</th><th>Title</th><th class="now">State</th>
<th class="now">Job</th><th class="now"></th></tr></thead><tbody>{rows}</tbody></table></div>
{setup}
<p class="hint">Tasks run one at a time in the project folder, each starting from the commit the
task before it left. Checks, fix rounds and checkpoints apply to every task.</p></div>"""


CSS = """<style>
ul.msprog{list-style:none;margin:8px 0 0;padding:0;font-size:14px}
ul.msprog li{padding:1px 0}
</style>"""
