#!/usr/bin/env python3
"""Upgrade 4a: requirements -> plan (added 2026-11-11 by Claude; jobboard.py keeps small
"# U04a" hooks).

Jason pastes an outline of an app. "Make a plan" starts a planning job - a normal job whose
prompt says do not write app code - and the planner writes `ROADMAP.md` (milestones in plain
English) and `tasks.json` in the project folder. The board checks that plan with
`agentjobs_upgrades/plan.py` (not rewritten here): fields, unique ids, no missing or circular
dependencies. An unusable plan goes straight back to the planner once, by itself, with the
errors in its prompt. Jason then checks and edits the plan on the plan page and approves it.
Nothing is run by this upgrade: approving only records that the plan is approved (4b starts
the queue), and the checks gate is off for a planning job - a plan is judged by its schema and
by Jason, not by the project's build.

  * Project records: `state/plans/<folder>.json` (env `JOBBOARD_PLANS`) - OUTSIDE everything
    the sandbox mounts, like the job records, so a planner can never forge its own approval.
    The draft is the project's own `tasks.json` (the planner writes it); the APPROVED copy is
    kept in the record, so what runs later is what Jason approved.
  * git for a brand-new folder runs inside that folder's own jail (`upgrades.make_wrap` ->
    `jail/run-local.sh sandbox-run`) through `safegit.SafeGit`, so the starting branch is
    clean whatever the agent does later.
  * The planner prompt lives in `prompts/planner.md` so it can be improved without a code
    change; `docs/tasks-schema.json` is the task schema (`plan.SCHEMA`) for humans.

Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import dashboard
import upgrades
from agentjobs_upgrades import plan as pl
from agentjobs_upgrades.safegit import SafeGit

esc = dashboard.esc
WEB = Path(__file__).resolve().parent
PROMPT_FILE = WEB.parent / "prompts" / "planner.md"

KINDS = {"android": "Android app", "windows": "Windows app", "web": "Web app", "other": "Other"}
# Same shape as jobboard.FOLDER_RE - importing jobboard here would be circular, and a folder
# name goes into a path and a git branch, so it is checked again.
FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
BAD_NAME = re.compile(r"\.\.|\.$")            # would break "git init -b agent/<folder>"

NAME_MAX = 60
OUTLINE_MAX = 20000
TASKS_FILE = "tasks.json"
ROADMAP_FILE = "ROADMAP.md"
TASKS_MAX_BYTES = 400_000
ROADMAP_MAX = 40_000
MAX_TASKS = 60
MAX_ERRORS = 30
MISSING = "no tasks.json in the project folder yet"

# ------------------------------------------------------------------ project records


def rec_path(plans, folder):
    """The record file for a project, or None for a folder name that is not a folder name."""
    folder = str(folder or "")
    if not FOLDER_RE.fullmatch(folder):
        return None
    return Path(plans) / f"{folder}.json"


def load(plans, folder):
    p = rec_path(plans, folder)
    if not p:
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("folder") == folder else None


def save(plans, rec):
    """Write a project record (atomically, like the job records)."""
    p = rec_path(plans, rec.get("folder"))
    if not p:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    tmp.replace(p)
    return True


def new_record(folder, name, kind, outline):
    return {"folder": folder, "name": name, "kind": kind, "outline": outline,
            "created": time.time(), "plan_job": None, "plan_retries": 0, "plan_status": "none",
            "approved_at": None, "approved_hash": None, "tasks": []}


def projects(plans):
    """Every project record, newest first."""
    out = []
    for p in sorted(Path(plans).glob("*.json"), key=lambda p: p.name):
        rec = load(plans, p.stem)
        if rec:
            out.append(rec)
    return sorted(out, key=lambda r: r.get("created") or 0, reverse=True)


def branch_of(folder):
    return f"agent/{folder}"


def create_folder(work, folder, name, kind):
    """A brand-new project folder: git on branch agent/<folder> with a first commit, so
    checkpoints and the Changes card work from the very first task. Returns an error text
    ("" = fine) - a folder without git still works, just without checkpoints."""
    root = Path(work) / folder
    root.mkdir(parents=True)
    (root / "README.md").write_text(f"# {name}\n\n{KINDS.get(kind, 'Project')}. Planned with "
                                    f"Agent Jobs; the tasks are in tasks.json.\n", encoding="utf-8")
    try:
        git = SafeGit(root, wrap=upgrades.make_wrap(root, folder, "agentgit-plan"), timeout=180)
        git.run("init", "-q", "-b", branch_of(folder))
        git.run("add", "-A")
        git.run("commit", "-qm", f"start {name}")
        return ""
    except Exception as ex:            # a missing jail or image must not lose the project
        return str(ex)[:200]


# ------------------------------------------------------------------ the form

def project_form(form):
    """The new-project form -> (fields, errors). Fields come back as typed so the page can be
    re-shown with what Jason wrote."""
    g = lambda k: (form.get(k, [""])[0]).strip()
    f = {"name": g("name")[:NAME_MAX], "kind": g("kind"), "outline": g("outline")[:OUTLINE_MAX],
         "folder": g("folder"), "new": not g("folder")}
    errs = []
    if not f["name"]:
        errs.append("Give the project a name.")
    if f["kind"] not in KINDS:
        errs.append("Pick what kind of project it is.")
    if not f["outline"]:
        errs.append("Paste the outline - what the app should do, in your own words.")
    if f["folder"]:
        if not FOLDER_RE.fullmatch(f["folder"]):
            errs.append("That project folder is not on the list.")
    else:
        # "Start a new folder": the name becomes the folder name and the git branch, so it
        # has to be a folder name (a project planned inside an old folder keeps its own name)
        if BAD_NAME.search(f["name"]) or not FOLDER_RE.fullmatch(f["name"]):
            errs.append("Use a simple name for a new project: letters, numbers, dash, "
                        "underscore or dot, starting with a letter or number.")
        f["folder"] = f["name"]
    return f, errs


# ------------------------------------------------------------------ the plan in the folder

def read_tasks(root):
    """(tasks, errors) for a project folder: the tasks as written (even when broken) and what
    `plan.validate` says is wrong with them. errors == [MISSING] when there is no file."""
    p = Path(root) / TASKS_FILE
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], [MISSING]
    if len(raw.encode("utf-8", "replace")) > TASKS_MAX_BYTES:
        return [], [f"tasks.json is too big to read ({len(raw) // 1000} KB)"]
    try:
        data = json.loads(raw)
    except ValueError as ex:
        return [], [f"tasks.json is not valid JSON: {str(ex)[:200]}"]
    tasks = [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []
    return tasks, pl.validate(data)


def read_roadmap(root):
    try:
        return (Path(root) / ROADMAP_FILE).read_text(encoding="utf-8", errors="replace")[:ROADMAP_MAX]
    except OSError:
        return ""


def hash_of(tasks):
    return hashlib.sha256(json.dumps(tasks, sort_keys=True).encode("utf-8")).hexdigest()


def milestones(tasks):
    """Milestone groups in the order the tasks are written: [{"milestone", "tasks"}]."""
    out = []
    for t in tasks:
        name = str(t.get("milestone") or "Tasks")
        if not out or out[-1]["milestone"] != name:
            out.append({"milestone": name, "tasks": []})
        out[-1]["tasks"].append(t)
    return out


def plan_state(plans, rec, root):
    """What the plan is right now, read from the folder (the record is only history):
    none | running | invalid | draft | approved, plus the tasks to edit and their problems."""
    tasks, errs = read_tasks(root)
    missing = errs == [MISSING]
    errs = [] if missing else errs
    st = {"tasks": tasks, "errors": errs, "missing": missing, "roadmap": read_roadmap(root),
          "approved_at": rec.get("approved_at"), "state": "none", "count": len(tasks),
          "milestones": [m["milestone"] for m in milestones(tasks)], "changed": False}
    if missing:
        st["state"] = rec.get("plan_status") if rec.get("plan_status") in ("running", "invalid") else "none"
        return st
    if errs:
        st["state"] = "invalid"
        return st
    if rec.get("approved_at") and rec.get("approved_hash") == hash_of(tasks):
        st["state"] = "approved"
        return st
    st["state"] = "draft"
    st["changed"] = bool(rec.get("approved_at"))
    return st


def write_tasks(root, tasks):
    """Save the draft. The caller validated it; a broken plan stays broken on purpose so the
    planner and Jason both see the same file."""
    (Path(root) / TASKS_FILE).write_text(json.dumps(tasks, indent=1) + "\n", encoding="utf-8")


def validate(tasks):
    """What is wrong with a plan (agentjobs_upgrades/plan.py, not rewritten here)."""
    return pl.validate(tasks)


def approve(plans, rec, root, tasks):
    """Approve a plan: the validated tasks are copied into the record outside the sandbox.
    Returns the errors that stopped it, else []."""
    errs = pl.validate(tasks)
    if errs:
        return errs
    rec.update(approved_at=time.time(), approved_hash=hash_of(tasks), tasks=json.loads(json.dumps(tasks)),
               plan_status="approved")
    save(plans, rec)
    write_tasks(root, tasks)
    return []


# ------------------------------------------------------------------ the plan form

CARD_RE = re.compile(r"^(tid|milestone|title|prompt|acc|after|checks|size|screen)([0-9]+)$")


def form_tasks(form):
    """The plan form -> [task, ...] in card order.

    jobboard parses POST bodies with parse_qs WITHOUT keep_blank_values, so an empty field
    would simply vanish and same-named fields could not be lined up. Every field therefore
    carries its card number (title1, prompt1, ...), and the cards are read in that order."""
    cards = {}
    for name, vals in form.items():
        mm = CARD_RE.match(name)
        if mm and vals:
            cards.setdefault(int(mm.group(2)), {})[mm.group(1)] = str(vals[0])
    out = []
    for i in sorted(cards):
        c = cards[i]
        one = lambda k: c.get(k, "").replace("\r\n", "\n").strip()
        many = lambda k: [x.strip() for x in one(k).splitlines() if x.strip()]
        t = {"id": one("tid"), "milestone": one("milestone") or "Tasks", "title": one("title")[:120],
             "prompt": one("prompt"), "acceptance": many("acc")}
        checks = many("checks")
        if checks:
            t["checks"] = checks
        deps = [x for x in re.split(r"[,\s]+", one("after")) if x]
        if deps:
            t["depends_on"] = deps
        if c.get("size", "") in ("S", "M"):
            t["size"] = c["size"]
        if one("screen"):
            t["screen"] = one("screen")
        out.append(t)
    return out


def next_id(tasks):
    used = {str(t.get("id") or "") for t in tasks}
    n = len(tasks) + 1
    while f"t{n}" in used:
        n += 1
    return f"t{n}"


def blank_task(tasks, milestone="Tasks"):
    return {"id": next_id(tasks), "milestone": milestone, "title": "", "prompt": "",
            "acceptance": [], "size": "S"}


def apply_actions(tasks, form):
    """Up / Down / Delete / Add change the list on the page; Save is what keeps it. Returns
    (tasks, note)."""
    tasks = [dict(t) for t in tasks]
    note = ""
    drop = (form.get("drop", [""])[0]).strip()
    if drop:
        left = [t for t in tasks if str(t.get("id")) != drop]
        if len(left) == len(tasks):
            note = f"That task is not in the plan, so nothing was deleted."
        else:
            tasks = left
            note = f"Deleted {drop}. Press Save to keep it deleted."
    move = (form.get("move", [""])[0]).strip()
    tid, _, way = move.partition(":")
    if tid and way in ("up", "down"):
        for i, t in enumerate(tasks):
            if str(t.get("id")) == tid:
                j = i - 1 if way == "up" else i + 1
                if 0 <= j < len(tasks):
                    tasks[i], tasks[j] = tasks[j], tasks[i]
                    note = f"Moved {tid} {way}. Press Save to keep it."
                else:
                    note = f"{tid} is already {way}."
                break
        else:
            note = "That task is not in the plan, so nothing moved."
    if form.get("add"):
        ms = str(tasks[-1].get("milestone")) if tasks else "Tasks"
        new = blank_task(tasks, ms)
        tasks.append(new)
        note = f"Added {new['id']} at the end. Fill it in and press Save."
    return tasks, note


# ------------------------------------------------------------------ the planner job

def planner_prompt(rec, errors=None):
    """The planning job's prompt, from prompts/planner.md ({{NAME}} {{KIND}} {{FOLDER}}
    {{OUTLINE}} {{ERRORS}})."""
    template = PROMPT_FILE.read_text(encoding="utf-8")
    fields = {"NAME": str(rec.get("name") or rec.get("folder") or "the project"),
              "KIND": KINDS.get(rec.get("kind"), "project"),
              "FOLDER": str(rec.get("folder") or ""),
              "OUTLINE": str(rec.get("outline") or "")[:OUTLINE_MAX],
              "ERRORS": error_block(errors)}
    for k, v in fields.items():
        template = template.replace("{{" + k + "}}", v)
    return re.sub(r"\{\{[A-Z]+\}\}\n?", "", template)


def error_block(errors):
    if not errors:
        return ""
    lines = (errors or [])[:MAX_ERRORS]
    more = f"\n(and {len(errors) - len(lines)} more)" if len(errors) > len(lines) else ""
    return ("Your last tasks.json was refused, so nothing was planned. Write tasks.json again "
            "in the same place - same schema, everything else in the folder stays as it is. "
            "What was wrong:\n" + "\n".join(f"- {e}" for e in lines) + more + "\n\n")


def plan_extra(retries=0):
    """Record fields for a planning job: it makes a plan, so the build gate is off (there is
    nothing to build yet), and a retry says so in the record."""
    extra = {"plan_job": 1, "verify_off": True}
    if retries:
        extra["plan_retry"] = int(retries)
    return extra


def plan_prompt(plans, folder, errors=None):
    rec = load(plans, folder) or new_record(folder, folder, "other", "")
    return planner_prompt(rec, errors)


def plan_finished(m, *, plans, work, new_job, session_of=lambda jid: None, job_dir=None,
                  requeue=True):
    """A planning job ended: look at what it wrote. An unusable plan goes back to the planner
    once, automatically, with the errors in its prompt. Returns the retry's job id or None.
    requeue=False is for a job that was refused before it ran (see jobboard): the project still
    has to stop saying the planner is working, but a job that never ran queues nothing."""
    if not m or not m.get("plan_job") or m.get("kind") == "bench":
        return None
    folder, jid = m.get("folder"), m.get("id")
    rec = load(plans, folder)
    if rec is None:
        rec = new_record(folder, folder, "other", "")
    tasks, errs = read_tasks(Path(work) / folder)
    tl = (lambda text: dashboard.tl_add(job_dir(jid), "plan", text)) if job_dir else (lambda t: None)
    rec.update(plan_job=jid, plan_retries=int(m.get("plan_retry") or 0))
    if errs == [MISSING]:
        rec["plan_status"] = "invalid"
        save(plans, rec)
        tl("The planner wrote no tasks.json - press Make a plan again.")
        return None
    errs = [e for e in errs if e != MISSING]
    if not errs:
        rec["plan_status"] = "draft"
        save(plans, rec)
        tl(f"Plan ready: {len(tasks)} tasks in {len(milestones(tasks))} milestones - review it "
           f"before anything runs.")
        return None
    rec["plan_status"] = "invalid"
    save(plans, rec)
    if m.get("plan_retry"):
        tl("The plan is still not valid: " + "; ".join(errs[:5]))
        return None
    if not requeue:
        tl("The plan job never started (the board refused its record), so nothing was planned. "
           "Still to fix: " + "; ".join(errs[:5]))
        return None
    child = new_job(m["mode"], folder, planner_prompt(rec, errs), m.get("offline", False),
                    resume=session_of(jid), parent=jid, extra=plan_extra(retries=1))
    tl(f"The plan was refused ({len(errs)} problems) - the planner got them back "
       f"(job {child['id']}).")
    return child["id"]


# ------------------------------------------------------------------ pages

def state_line(st):
    """One plain sentence about where the plan stands."""
    if st["state"] == "none":
        return ("No plan yet.", "mut")
    if st["state"] == "running":
        return ("The planner is working on it.", "mut")
    if st["state"] == "invalid":
        if st["missing"]:      # no file at all: "0 problems" would be a lie and no use
            return ("The planner wrote no tasks.json - press Make a plan again.", "bad")
        return (f"The plan is not usable yet ({len(st['errors'])} problem"
                f"{'s' if len(st['errors']) != 1 else ''}).", "bad")
    if st["state"] == "approved":
        return (f"Approved - {st['count']} tasks. Nothing runs until you start them.", "ok")
    return (f"A draft to check: {st['count']} tasks in {len(st['milestones'])} milestones. "
            f"Nothing runs until you approve it.", "warn")


def errors_html(errors):
    if not errors:
        return ""
    lis = "".join(f"<li>{esc(e)}</li>" for e in errors[:MAX_ERRORS])
    return f'<div class="card banner FAILED"><h2>The plan was refused</h2><ul>{lis}</ul></div>'


def options(values, chosen):
    return "".join(f'<option value="{esc(v)}"{" selected" if v == chosen else ""}>{esc(label)}</option>'
                   for v, label in values)


def new_project_body(folders, fields=None, errors=()):
    f = fields or {}
    kinds = [(k, label) for k, label in KINDS.items()]
    folder_opts = '<option value="">Start a new folder...</option>' + "".join(
        f'<option value="{esc(n)}"{" selected" if f.get("folder") == n else ""}>{esc(n)}</option>'
        for n in folders)
    return f"""
<div class="card"><h2>New project</h2>
<p class="hint">Tell the tool what you want in your own words. It makes a plan - milestones and
small tasks - and shows it to you to check before anything runs.</p>
{('<div class="card banner FAILED"><ul>' + "".join(f"<li>{esc(e)}</li>" for e in errors) + "</ul></div>") if errors else ""}
<form method="post" action="/project/new">
<label>Project name</label>
<input type="text" name="name" required maxlength="{NAME_MAX}" value="{esc(f.get('name', ''))}"
 placeholder="my-notes-app">
<p class="hint">This is also the folder name in ~/agent-work (letters, numbers, dash, underscore, dot).</p>
<div class="row"><div><label>What is it?</label><select name="kind">
{options(kinds, f.get('kind') or 'android')}</select></div>
<div><label>Project folder</label><select name="folder">{folder_opts}</select>
<p class="hint">Pick an existing folder to plan inside it, or leave it on "Start a new folder".</p></div></div>
<label>The outline: what should it do?</label>
<textarea name="outline" required maxlength="{OUTLINE_MAX}"
 placeholder="A notes app for my phone. I want to type a note and save it, see the list of notes, search them, and have it work without internet. Notes should stay on the phone.">{esc(f.get('outline', ''))}</textarea>
<button>Create project</button></form></div>"""


def projects_body(recs, states):
    rows = "".join(
        f'<tr><td class="now"><a href="/project/{esc(r["folder"])}">{esc(r["name"])}</a></td>'
        f'<td>{esc(KINDS.get(r["kind"], r["kind"]))}</td><td>{esc(r["folder"])}</td>'
        f'<td><span class="{states[r["folder"]][1]}">{esc(states[r["folder"]][0])}</span></td>'
        f'<td class="now"><a href="/plan/{esc(r["folder"])}">Plan</a></td></tr>'
        for r in recs)
    return f"""
<div class="card"><h2>Projects</h2>
<p class="hint">A plan is only a plan until you approve it, and approving does not run anything.</p>
<div class="tbl"><table><thead><tr><th class="now">Project</th><th>Kind</th><th>Folder</th>
<th>Plan</th><th class="now"></th></tr></thead><tbody>{rows or '<tr><td colspan=5 class=mut>No projects yet. <a href="/project/new">New project</a></td></tr>'}</tbody></table></div>
<p><a class="btn" href="/project/new">New project</a></p></div>"""


def project_body(rec, st, mode_opts, note="", errors=()):
    line, cls = state_line(st)
    plan_job = rec.get("plan_job")
    job_link = (f' &middot; last plan job: <a href="/job/{esc(plan_job)}">{esc(plan_job)}</a>'
                if plan_job else "")
    review = (f'<a class="btn" href="/plan/{esc(rec["folder"])}">Review the plan</a>'
              if st["state"] not in ("none", "running") else "")
    roadmap = (f'<details class="card"><summary><b>Roadmap</b> '
               f'<span class="hint">what ROADMAP.md says</span></summary>'
               f'<pre>{esc(st["roadmap"])}</pre></details>' if st["roadmap"] else "")
    return f"""
<div class="card banner"><h2>{esc(rec['name'])} <span class="mut">&middot; {esc(KINDS.get(rec['kind'], rec['kind']))}</span></h2>
<p class="mut">folder ~/agent-work/{esc(rec['folder'])} &middot; started {time.strftime('%d %b %H:%M', time.localtime(rec.get('created') or time.time()))}{job_link}</p>
{f'<p class="bad">{esc(note)}</p>' if note else ''}
<p class="{cls}"><b>{esc(line)}</b></p>
{errors_html(st['errors'])}
{f'<p class="hint">You approved an earlier version of this plan. Review it and approve again if the new one is what you want.</p>' if st['changed'] else ''}
{f'<div class="card banner FAILED"><ul>' + ''.join(f'<li>{esc(e)}</li>' for e in errors) + '</ul></div>' if errors else ''}
<form method="post" action="/project/edit/{esc(rec['folder'])}">
<label>The outline: what should it do?</label>
<textarea name="outline" maxlength="{OUTLINE_MAX}">{esc(rec.get('outline', ''))}</textarea>
<div class="row"><div><label>Which agent plans it</label><select name="mode">{mode_opts}</select></div></div>
<div class="brow" style="margin-top:8px"><button name="plan" value="1">Make a plan</button>
<button class="ghost2" name="save" value="1">Save the outline</button>{review}</div>
<p class="hint">Making a plan does not write any app code - the planner only writes ROADMAP.md and
tasks.json. Every task is about 45 minutes and says how you can tell it is done.</p></form></div>
{roadmap}
{('<div class="card"><h2>Tasks</h2><ul>' + ''.join(f'<li>{esc(m)}</li>' for m in st['milestones']) + '</ul></div>' if st['milestones'] else '')}"""


def task_card(t, n):
    """One editable task. Every field name carries the card number (see form_tasks)."""
    def txt(k):
        return esc(t.get(k) if isinstance(t.get(k), str) else "")

    def lines(k):
        v = t.get(k)
        return esc("\n".join(str(x) for x in v if str(x).strip())) if isinstance(v, list) else ""

    deps = t.get("depends_on")
    deps = ", ".join(str(x) for x in deps if str(x).strip()) if isinstance(deps, list) else (deps or "")
    after = ", ".join(str(x) for x in (t.get("depends_on") or [])) if isinstance(t.get("depends_on"), list) else ""
    tid = txt("id")
    size = t.get("size") if t.get("size") in ("S", "M") else ""
    return f"""<div class="task">
<input type="hidden" name="tid{n}" value="{tid}">
<label>Title</label><input type="text" name="title{n}" maxlength="120" value="{txt('title')}">
<div class="row"><div><label>Milestone</label><input type="text" name="milestone{n}" value="{txt('milestone')}"></div>
<div><label>How big</label><select name="size{n}">
<option value="">not said</option><option value="S"{' selected' if size == 'S' else ''}>about 20 minutes</option>
<option value="M"{' selected' if size == 'M' else ''}>about 45 minutes</option></select></div></div>
<label>What to do</label><textarea name="prompt{n}">{txt('prompt')}</textarea>
<label>Done when these are true (one per line)</label><textarea name="acc{n}">{lines('acceptance')}</textarea>
<label>Checks - commands that prove it (optional, one per line)</label><textarea name="checks{n}">{lines('checks')}</textarea>
<div class="row"><div><label>After these tasks (ids, comma separated)</label>
<input type="text" name="after{n}" value="{esc(deps)}"></div>
<div><label>What the screen looks like (UI tasks only)</label>
<input type="text" name="screen{n}" value="{txt('screen')}"></div></div>
<div class="brow tfoot"><span class="hint">id {tid}{' &middot; after: ' + esc(after) if after else ''}</span>
<span style="flex:1"></span>
<button class="ghost2" name="move" value="{tid}:up">Up</button>
<button class="ghost2" name="move" value="{tid}:down">Down</button>
<button class="ghost" name="drop" value="{tid}">Delete</button></div></div>"""


def plan_body(rec, st, tasks, note=""):
    """The plan review page: milestones as collapsible sections, one card per task."""
    groups = milestones(tasks)
    n = 0
    secs = ""
    for g in groups:
        cards = ""
        for t in g["tasks"]:
            n += 1
            cards += task_card(t, n)
        open_ = " open" if st["state"] != "approved" else ""
        secs += (f'<details class="ms"{open_}><summary><b>{esc(g["milestone"])}</b> '
                 f'<span class="hint">{len(g["tasks"])} task{"s" if len(g["tasks"]) != 1 else ""}</span>'
                 f'</summary>{cards}</details>')
    line, cls = state_line(st)
    bar = (f'<div class="brow pbar"><button name="save" value="1">Save</button>'
           f'<button name="approve" value="1">Approve plan</button>'
           f'<button class="ghost2" name="add" value="1">Add task</button>'
           f'<span class="hint" style="flex:1">Up, Down, Delete and Add change the list on this '
           f'page; <b>Save</b> keeps them.</span></div>')
    return f"""
<div class="card banner"><h2>Plan for {esc(rec['name'])}</h2>
<p class="mut">~/agent-work/{esc(rec['folder'])}/{TASKS_FILE}
 &middot; <a href="/project/{esc(rec['folder'])}">project</a>
{' &middot; <a href="/job/' + esc(rec['plan_job']) + '">plan job</a>' if rec.get('plan_job') else ''}
{' &middot; approved ' + time.strftime('%d %b %H:%M', time.localtime(rec['approved_at'])) if rec.get('approved_at') else ''}</p>
<p class="{cls}"><b>{esc(line)}</b></p>
{f'<p class="warn2">{esc(note)}</p>' if note else ''}
{'<p class="hint">You approved an earlier version of this plan - approve again if this one is what you want.</p>' if st['changed'] else ''}</div>
{errors_html(st['errors']) if st['errors'] else ''}
<form method="post" action="/plan/edit/{esc(rec['folder'])}">
{bar}{secs or '<p class="mut">No tasks to show.</p>'}
<p class="hint">Every task needs a title, what to do and at least one thing you can test.
Nothing runs until you approve this plan.</p>{bar}</form>"""


def job_card_html(m, plans):
    """On a planning job's page: where its plan is."""
    if not m.get("plan_job"):
        return ""
    rec = load(plans, m.get("folder"))
    if not rec:
        return ('<div class="card"><h2>Plan</h2><p class="mut">This job made a plan for '
                f'{esc(m.get("folder"))}.</p></div>')
    return (f'<div class="card"><h2>Plan</h2><p>This job plans <a href="/project/{esc(rec["folder"])}">'
            f'{esc(rec["name"])}</a>. When it finishes, check the tasks here: '
            f'<a class="btn" href="/plan/{esc(rec["folder"])}">Review the plan</a></p></div>')


CSS = """<style>
details.ms{border:1px solid var(--line);border-radius:10px;background:var(--card);margin:0 0 12px;padding:0 14px}
details.ms>summary{padding:11px 2px;list-style:none;cursor:pointer}
details.ms>summary::-webkit-details-marker{display:none}
details.ms>summary b{font-size:15px}details.ms[open]>summary{border-bottom:1px solid var(--line)}
.task{border:1px solid var(--line);border-radius:9px;padding:12px;margin:12px 0;background:var(--bg)}
.task label{margin:8px 0 3px;font-size:13px}
.task textarea{min-height:70px}
.task .tfoot{margin-top:10px;padding-top:9px;border-top:1px dashed var(--line)}
.pbar{position:sticky;top:0;z-index:2;background:var(--card);padding:10px 0;
 border-bottom:1px solid var(--line);margin-bottom:12px;align-items:center}
.pbar button{margin-right:2px}
</style>"""
