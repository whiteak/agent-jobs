#!/usr/bin/env python3
"""jobstatus: work out at a glance what a job board job is actually doing.

Pure helpers (standard library only, no side effects at import). Given a job's
meta dict, its events.jsonl path, its project folder and "now", job_status()
returns a small dict the web UI can render:

  state             WORKING | QUEUED | FINISHED | FAILED | STUCK | STOPPED
  label             friendly words for the state
  elapsed           seconds the job has been running
  last_activity_ago seconds since the log file last grew
  turns / actions   assistant tool_use count (or num_turns from the result)
  current_action    "Running tests", "Editing foo.kt", "Thinking", ...
  result_summary    first ~200 chars of the final answer (FINISHED)
  fail_reason       why it failed (FAILED)
  steps             [{text, state: done|active|todo}, ...] plus steps_done /
                    steps_total / steps_source / fallback

Step checklist sources, in order: the latest todo state seen in the log
(TodoWrite / todo_write / TaskCreate / TaskUpdate), else checkboxes in the
project's PLAN.md, else AGENT_REPORT.md, else nothing.

Polling is cheap: the parsed log state is cached per events file keyed on its
size/mtime; each call reads only the bytes after the last consumed line
(logs can be tens of MB and grow every second).
"""
import json
import os
import re
import time

STUCK_SECONDS = 180  # no new log output for this long while running => looks stuck

STATE_LABEL = {
    "WORKING": "Working",
    "QUEUED": "Waiting in queue",
    "FINISHED": "Finished",
    "FAILED": "Failed",
    "STUCK": "Looks stuck",
    "STOPPED": "Stopped",
}

# ---------------------------------------------------------------- log scanning

# events file path -> cached parse state
_parse_cache = {}


def reset_cache():
    """Drop all cached parse state (used by tests; not needed in normal use)."""
    _parse_cache.clear()


def _new_state():
    return {"offset": 0, "size": -1, "actions": 0, "last_ev": None, "result": None,
            "todos": None, "tasks": {}, "task_order": []}


def scan_events(events_path):
    """Return the accumulated parse state for events_path, reading only new bytes."""
    events_path = str(events_path)
    st = _parse_cache.get(events_path)
    if st is None:
        st = _new_state()
        _parse_cache[events_path] = st
    try:
        size = os.path.getsize(events_path)
    except OSError:
        size = 0
    if size == st["size"]:
        return st  # nothing new: no re-parse at all
    if size < st["offset"]:  # file replaced/truncated: start over
        st.clear()
        st.update(_new_state())
    if size == 0:
        st["size"] = 0
        return st
    with open(events_path, "rb") as f:
        f.seek(st["offset"])
        data = f.read()
    cut = data.rfind(b"\n") + 1
    if cut == 0:  # only a partial line so far
        st["size"] = size
        return st
    st["offset"] += cut
    st["size"] = size
    for raw in data[:cut].decode("utf-8", "replace").splitlines():
        _consume(st, raw)
    return st


def _consume(st, raw):
    raw = raw.strip()
    if not raw or not raw.startswith("{"):
        return  # blank / non-JSON lines ("Running local-claude unattended. Log: ...")
    try:
        e = json.loads(raw)
    except ValueError:
        return
    t = e.get("type")
    if t == "assistant":
        for c in (e.get("message") or {}).get("content") or []:
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "tool_use":
                name = c.get("name") or "?"
                inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                st["actions"] += 1
                st["last_ev"] = ("tool", name, inp)
                _track_todo(st, name, inp)
            elif ct == "thinking":
                st["last_ev"] = ("think",)
            elif ct == "text" and str(c.get("text") or "").strip():
                st["last_ev"] = ("text",)
    elif t == "system" and e.get("subtype") == "thinking_tokens":
        st["last_ev"] = ("think",)
    elif t == "result":
        st["result"] = {"text": str(e.get("result") or ""), "is_error": bool(e.get("is_error")),
                        "num_turns": e.get("num_turns"), "duration_ms": e.get("duration_ms") or 0}
        st["last_ev"] = ("result",)


# ---------------------------------------------------------------- todo tools

TODO_TOOLS = {"TodoWrite", "todo_write", "WriteTodos"}
TASK_TOOLS = {"TaskCreate", "TaskUpdate", "TaskComplete"}

_DONE_WORDS = {"completed", "complete", "done", "checked", "finished"}
_ACTIVE_WORDS = {"in_progress", "in progress", "active", "doing", "started"}


def _todo_text(src):
    if isinstance(src, str):
        return src.strip()[:200]
    if isinstance(src, dict):
        for k in ("content", "text", "description", "subject", "title", "prompt"):
            v = src.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:200]
    return ""


def _todo_state(src):
    s = str((src or {}).get("status") or (src or {}).get("state") or "").lower() if isinstance(src, dict) else ""
    if s in _DONE_WORDS:
        return "done"
    if s in _ACTIVE_WORDS:
        return "active"
    return "todo"


def _track_todo(st, name, inp):
    """Keep the latest todo state from the log; the most recent call wins."""
    if name in TODO_TOOLS:
        todos = inp.get("todos") or inp.get("items")
        if isinstance(todos, list):
            st["todos"] = [{"text": _todo_text(t), "state": _todo_state(t)}
                           for t in todos if isinstance(t, (dict, str)) and _todo_text(t)]
    elif name == "TaskCreate":
        tid = str(inp.get("id") or inp.get("task_id") or f"t{len(st['task_order']) + 1}")
        if tid not in st["tasks"]:
            st["task_order"].append(tid)
        st["tasks"][tid] = {"text": _todo_text(inp), "state": "done" if inp.get("status") in _DONE_WORDS else _todo_state(inp)}
    elif name in ("TaskUpdate", "TaskComplete"):
        tid = str(inp.get("taskId") or inp.get("task_id") or inp.get("id") or "")
        if tid in st["tasks"]:
            st["tasks"][tid]["state"] = "done" if name == "TaskComplete" else _todo_state(inp)


# ---------------------------------------------------------------- current action

_TEST_RE = re.compile(r"\bpytest\b|\bunittest\b|\bctest\b|\b(?:npm|yarn|pnpm)\s+(?:run\s+)?test\b|\bgo\s+test\b|\bmake\s+test\b|\bgradlew?\b[^\n]*\b(?:test|check)\b", re.I)
_BUILD_RE = re.compile(r"\bgradlew?\b[^\n]*\b(?:assemble|build|compile|bundle)\w*|\bmake\b|\bcmake\b|\b(?:npm|yarn|pnpm)\s+(?:run\s+)?build\b", re.I)
_SHELL_TOOLS = {"Bash", "run_shell_command"}
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "edit", "write_file", "notebook_edit"}
_READ_TOOLS = {"Read", "read_file"}
_SEARCH_TOOLS = {"Grep", "Glob", "grep_search", "glob", "search_files"}
_WEB_TOOLS = {"WebFetch", "WebSearch", "web_fetch", "web_search"}


def _basename(inp):
    for k in ("file_path", "path", "target_file", "notebook_path"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return os.path.basename(v.rstrip("/")) or v
    return ""


def current_action(ev):
    """Plain-English description of the latest meaningful event, or None."""
    if not ev:
        return None
    kind = ev[0]
    if kind == "think":
        return "Thinking"
    if kind == "text":
        return "Writing a reply"
    if kind == "result":
        return "Finished"
    if kind != "tool":
        return None
    name, inp = ev[1], ev[2]
    if name in _SHELL_TOOLS:
        cmd = str(inp.get("command") or "").strip()
        if not cmd:
            return "Running a command"
        if _TEST_RE.search(cmd):
            return "Running tests"
        if _BUILD_RE.search(cmd):
            return "Building"
        return "Running: " + cmd[:60]
    if name in _EDIT_TOOLS:
        b = _basename(inp)
        return f"Editing {b}" if b else "Editing a file"
    if name in _READ_TOOLS:
        b = _basename(inp)
        return f"Reading {b}" if b else "Reading a file"
    if name in _SEARCH_TOOLS:
        return "Searching the code"
    if name in _WEB_TOOLS:
        return "Looking something up online"
    if name in TODO_TOOLS | TASK_TOOLS:
        return "Updating the step list"
    return f"Using {name}"


# ---------------------------------------------------------------- steps from files

_CHECK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*\[( |x|X)\]\s*(.*)$")


def md_checkboxes(path):
    """Checkbox items in a markdown file, in order: '- [ ]', '- [x]', '12. [x] ...'."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for ln in lines:
        m = _CHECK_RE.match(ln)
        if m:
            out.append({"text": m.group(2).strip()[:200],
                        "state": "done" if m.group(1).lower() == "x" else "todo"})
    return out


def window(meta):
    """(lo, hi) mtime bounds of this job's own window, or None when the window
    never applies (queued job, no start time). hi is None while the job is still
    open-ended (running/stopping). Same rule plan_in_window and reviewbundle use
    so a folder's file from an earlier job is not credited to a new one."""
    status = (meta or {}).get("status")
    started = (meta or {}).get("started") or (meta or {}).get("created")
    if status == "queued" or started is None:
        return None
    finished = (meta or {}).get("finished")
    if status in ("running", "stopping") or finished is None:
        return (started - 5, None)
    return (started - 5, finished + 120)


def mtime_in_window(meta, mt):
    """True when an mtime falls inside this job's own window (see window())."""
    w = window(meta)
    if w is None or mt is None:
        return False
    if mt < w[0]:
        return False
    return True if w[1] is None else mt <= w[1]


def plan_in_window(meta, path):
    """True when PLAN.md / AGENT_REPORT.md was modified inside this job's own
    window, so a folder's plan from an earlier job is not shown for a new one.
    started-5 .. (finished+120 for ended jobs; open-ended while queued/running/
    stopping). A queued job (no started yet) never uses the files."""
    try:
        mt = os.path.getmtime(str(path))
    except OSError:
        return False
    return mtime_in_window(meta, mt)


def _steps_for(meta, st, project_dir, state):
    if st["todos"]:
        steps, src = [dict(t) for t in st["todos"]], "todo-log"
    elif st["task_order"]:
        steps, src = [dict(st["tasks"][t]) for t in st["task_order"]], "todo-log"
    else:
        steps, src = [], ""
        project_dir = str(project_dir or "")
        if project_dir:
            for name in ("PLAN.md", "AGENT_REPORT.md"):
                p = os.path.join(project_dir, name)
                if os.path.isfile(p) and plan_in_window(meta, p):
                    found = md_checkboxes(p)
                    if found:  # steps_source is only reported when checkboxes exist
                        steps, src = found, name
                        break
    if state in ("WORKING", "STUCK") and steps and not any(s["state"] == "active" for s in steps):
        for s in steps:  # first unticked step is the active one while working
            if s["state"] == "todo":
                s["state"] = "active"
                break
    return steps, src


def _one_line(text):
    return " ".join(str(text).split())


_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((?:[^)]*|\[[^\]]*\])\)")
_MD_EMPH_RE = re.compile(r"\*\*|__|~~|\*|(?<!\w)_(?=\w)|(?<=\w)_(?!\w)")
_MD_MARK_RE = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)+", re.M)


def plain_text(s):
    """Strip markdown clutter (**, `, leading #s and list markers, [text](url)
    -> text) and flatten whitespace, so summaries read as plain English."""
    s = _MD_LINK_RE.sub(r"\1", str(s or ""))
    s = s.replace("```", "").replace("`", "")
    s = _MD_EMPH_RE.sub("", s)
    s = _MD_MARK_RE.sub("", s)
    return " ".join(s.split())


# ---------------------------------------------------------------- main entry

def job_status(meta, events_path, project_dir, now=None):
    """Return the status dict for one job. Pure apart from reading the log/files."""
    now = time.time() if now is None else float(now)
    meta = meta or {}
    st = scan_events(events_path)
    try:
        stat = os.stat(str(events_path))
        mtime, size = stat.st_mtime, stat.st_size
    except OSError:
        mtime, size = None, 0

    status = meta.get("status")
    started = meta.get("started") or meta.get("created")
    finished = meta.get("finished")

    if status == "queued":
        state = "QUEUED"
    elif status in ("running", "stopping"):
        idle_ref = mtime if mtime is not None else started
        state = "STUCK" if (idle_ref is not None and now - idle_ref > STUCK_SECONDS) else "WORKING"
    elif status == "done":
        state = "FINISHED"
    elif status in ("failed", "interrupted"):
        state = "FAILED"
    elif status in ("stopped", "cancelled"):
        state = "STOPPED"
    else:
        state = "WORKING"

    elapsed = None
    if started:
        end = finished if (finished and status not in ("queued", "running", "stopping")) else now
        elapsed = max(0, int(end - started))
    if mtime is not None:
        last_activity_ago = max(0, int(now - mtime))
    elif started:
        last_activity_ago = max(0, int((finished or now) - started))
    else:
        last_activity_ago = None

    res = st["result"]
    turns = res["num_turns"] if (res and res.get("num_turns") is not None) else st["actions"]

    if state == "FINISHED":
        ca = None
    elif state == "QUEUED":
        ca = "Waiting in the queue"
    elif state == "STOPPED":
        ca = "Stopped by you"
    elif state == "FAILED":
        ca = None
    else:
        ca = current_action(st["last_ev"]) or ("Thinking" if size else "Starting up")

    result_summary = plain_text(res["text"])[:200] if (res and state == "FINISHED" and res["text"]) else None
    fail_reason = None
    if state == "FAILED":
        if meta.get("fail_note"):          # the worker's own reason (refused record,
            fail_reason = str(meta["fail_note"])   # bench server down) wins
        elif status == "interrupted":
            fail_reason = "Interrupted: the job board restarted while it ran"
        elif res and res["is_error"] and res["text"]:
            fail_reason = plain_text(res["text"])[:200]
        elif res and res["is_error"]:
            fail_reason = "The agent reported an error"
        elif meta.get("exit") not in (None, 0):
            fail_reason = f"Exit code {meta['exit']}"
        else:
            fail_reason = "The job stopped unexpectedly"

    steps, src = _steps_for(meta, st, project_dir, state)
    steps_done = sum(1 for s in steps if s["state"] == "done")
    fallback = None
    if not steps:
        n = st["actions"]
        fallback = f"No plan yet; {n} action{'s' if n != 1 else ''} so far"

    return {
        "state": state,
        "label": STATE_LABEL[state],
        "meta_status": status,
        "elapsed": elapsed,
        "last_activity_ago": last_activity_ago,
        "turns": turns,
        "actions": st["actions"],
        "current_action": ca,
        "result_summary": result_summary,
        "fail_reason": fail_reason,
        "steps": steps,
        "steps_done": steps_done,
        "steps_total": len(steps),
        "steps_source": src,
        "fallback": fallback,
        "short": _one_line(meta.get("job") or meta.get("id") or "job")[:50],
    }
