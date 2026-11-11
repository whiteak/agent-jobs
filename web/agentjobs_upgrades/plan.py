"""Upgrade 4a/4b: tasks.json validation, ordering and the persistent queue."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

TASK_FIELDS = {"id": str, "milestone": str, "title": str, "prompt": str, "acceptance": list,
               "checks": list, "depends_on": list, "size": str, "screen": str}
REQUIRED = ("id", "milestone", "title", "prompt", "acceptance")

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Agent Jobs project tasks",
    "type": "array",
    "items": {
        "type": "object",
        "required": list(REQUIRED),
        "properties": {
            "id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,40}$"},
            "milestone": {"type": "string"}, "title": {"type": "string", "maxLength": 120},
            "prompt": {"type": "string"},
            "acceptance": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "checks": {"type": "array", "items": {"type": "string"}},
            "depends_on": {"type": "array", "items": {"type": "string"}},
            "size": {"enum": ["S", "M"]}, "screen": {"type": "string"},
        },
        "additionalProperties": False,
    },
}


def validate(tasks) -> list[str]:
    errs = []
    if not isinstance(tasks, list) or not tasks:
        return ["tasks.json must be a non-empty list of tasks"]
    ids = []
    for i, t in enumerate(tasks):
        where = f"task {i + 1}"
        if not isinstance(t, dict):
            errs.append(f"{where}: must be an object"); continue
        where = f"task {t.get('id', i + 1)}"
        for k in REQUIRED:
            if not t.get(k):
                errs.append(f"{where}: missing '{k}'")
        for k, v in t.items():
            if k not in TASK_FIELDS:
                errs.append(f"{where}: unknown field '{k}'")
            elif not isinstance(v, TASK_FIELDS[k]):
                errs.append(f"{where}: '{k}' must be a {TASK_FIELDS[k].__name__}")
        if isinstance(t.get("id"), str) and not re.match(r"^[A-Za-z0-9_.-]{1,40}$", t["id"]):
            errs.append(f"{where}: id may only use letters, digits, . _ -")
        if t.get("size") not in (None, "S", "M"):
            errs.append(f"{where}: size must be S or M")
        if isinstance(t.get("acceptance"), list) and not all(isinstance(a, str) and a.strip() for a in t["acceptance"]):
            errs.append(f"{where}: acceptance criteria must be non-empty text")
        ids.append(t.get("id"))
    dup = sorted({x for x in ids if ids.count(x) > 1 and x})
    for d in dup:
        errs.append(f"duplicate id '{d}'")
    known = set(ids)
    for t in tasks:
        if isinstance(t, dict):
            for d in t.get("depends_on", []) or []:
                if d not in known:
                    errs.append(f"task {t.get('id')}: depends on unknown task '{d}'")
                if d == t.get("id"):
                    errs.append(f"task {t.get('id')}: depends on itself")
    if not errs:
        try:
            order(tasks)
        except ValueError as e:
            errs.append(str(e))
    return errs


def order(tasks: list[dict]) -> list[dict]:
    """Dependency order, keeping the planner's order where possible (stable Kahn)."""
    pos = {t["id"]: i for i, t in enumerate(tasks)}
    deps = {t["id"]: set(t.get("depends_on") or []) for t in tasks}
    done, out = set(), []
    while len(out) < len(tasks):
        ready = [t for t in tasks if t["id"] not in done and deps[t["id"]] <= done]
        if not ready:
            left = sorted((t["id"] for t in tasks if t["id"] not in done), key=pos.get)
            raise ValueError("circular dependency between: " + ", ".join(left))
        nxt = min(ready, key=lambda t: pos[t["id"]])
        out.append(nxt)
        done.add(nxt["id"])
    return out


def job_prompt(outline_summary: str, milestone_text: str, task: dict) -> str:
    acc = "\n".join(f"- {a}" for a in task["acceptance"])
    return (f"Project outline (summary):\n{outline_summary.strip()}\n\n"
            f"Current milestone:\n{milestone_text.strip()}\n\n"
            f"Your task: {task['title']}\n{task['prompt'].strip()}\n\n"
            f"Done when all of these are true (and checks pass):\n{acc}\n\n"
            "Read AGENT_NOTES.md first. When you finish, add 2-5 lines of lessons to AGENT_NOTES.md "
            "(what you learned, gotchas). Keep PLAN.md as a ticked checklist.")


class Queue:
    """Persistent project queue. State file is rewritten atomically after every change."""

    def __init__(self, path: Path, now=time.time):
        self.path = Path(path)
        self.now = now
        self.state = {"tasks": [], "status": "idle", "on_fail": "stop", "stop_starting_at": None,
                      "created": now()}
        if self.path.exists():
            self.state = json.loads(self.path.read_text())

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.path)

    def load_plan(self, tasks: list[dict], on_fail: str = "stop"):
        errs = validate(tasks)
        if errs:
            raise ValueError("; ".join(errs))
        self.state["tasks"] = [dict(t, state="queued", job_id=None) for t in order(tasks)]
        self.state["on_fail"] = on_fail
        self.state["status"] = "running"
        self.save()

    def _by_id(self):
        return {t["id"]: t for t in self.state["tasks"]}

    def recover(self) -> list[str]:
        """Call on start-up: running tasks become interrupted and will re-run."""
        hit = []
        for t in self.state["tasks"]:
            if t["state"] == "running":
                t["state"] = "interrupted"
                hit.append(t["id"])
        if hit:
            self.save()
        return hit

    def set_cutoff(self, hm: Optional[str], now_dt=None):
        """'Don't start new tasks after' HH:MM: stored as the next time that clock time occurs."""
        from datetime import datetime, timedelta
        if not hm:
            self.state["stop_starting_at"] = None
        else:
            now_dt = now_dt or datetime.fromtimestamp(self.now())
            h, m = map(int, hm.split(":"))
            t = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
            if t <= now_dt:
                t += timedelta(days=1)
            self.state["stop_starting_at"] = t.timestamp()
        self.save()

    def next_task(self) -> Optional[dict]:
        if self.state["status"] != "running":
            return None
        cut = self.state.get("stop_starting_at")
        if cut and self.now() >= cut:
            return None
        by = self._by_id()
        for t in self.state["tasks"]:
            if t["state"] in ("queued", "interrupted"):
                dep_states = [by[d]["state"] for d in t.get("depends_on") or []]
                if all(s == "done" for s in dep_states):
                    return t
                if any(s in ("failed", "skipped") for s in dep_states):
                    continue
                return None        # waiting on something still to run
        return None

    def mark(self, task_id: str, state: str, job_id: Optional[str] = None):
        by = self._by_id()
        t = by[task_id]
        t["state"] = state
        if job_id:
            t["job_id"] = job_id
        if state == "failed":
            if self.state["on_fail"] == "stop":
                self.state["status"] = "stopped"
            else:
                self._skip_dependents(task_id)
        if all(x["state"] in ("done", "failed", "skipped") for x in self.state["tasks"]):
            self.state["status"] = "finished"
        self.save()

    def _skip_dependents(self, failed_id: str):
        changed = True
        bad = {failed_id}
        while changed:
            changed = False
            for t in self.state["tasks"]:
                if t["state"] in ("queued", "interrupted") and set(t.get("depends_on") or []) & bad:
                    t["state"] = "skipped"
                    bad.add(t["id"])
                    changed = True

    def progress(self) -> list[dict]:
        ms: dict[str, dict] = {}
        for t in self.state["tasks"]:
            m = ms.setdefault(t["milestone"], {"milestone": t["milestone"], "done": 0, "total": 0})
            m["total"] += 1
            m["done"] += t["state"] == "done"
        return list(ms.values())
