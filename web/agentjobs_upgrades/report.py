"""Upgrade 10: morning report built only from job/bench records."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Callable, Optional

DONE = {"passed"}
FAILED = {"failed", "stuck", "safety_stop", "stopped"}


def _reason(job: dict) -> str:
    if job.get("status") == "safety_stop":
        return "Safety stop: " + (job.get("safety_command") or job.get("reason") or "blocked command")
    if job.get("watchdog_reason"):
        return job["watchdog_reason"]
    fr = job.get("first_failure") or ""
    if fr:
        return fr.strip().splitlines()[0][:160]
    return job.get("banner", "") or job.get("status", "")


def _dur(s: float) -> str:
    m = int(s // 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def build(jobs: list[dict], bench_sets: list[dict], since: float, until: Optional[float] = None,
          base_url: str = "", qwen_summary: Optional[Callable[[str], Optional[str]]] = None) -> str:
    """jobs: dicts with id, title, project, status, started, finished, rounds, rollbacks,
    attempts, approval ('waiting'|'merged'|...), first_failure, watchdog_reason, safety_command,
    queue_blocked (bool). bench_sets: {label, finished, verdict, summary}."""
    until = until or time.time()
    js = [j for j in jobs if since <= (j.get("finished") or j.get("started") or 0) < until]
    bs = [b for b in bench_sets if since <= (b.get("finished") or 0) < until]
    link = lambda j: f"[{j.get('title') or j['id']}]({base_url.rstrip('/')}/jobs/{j['id']})" if base_url else (j.get("title") or j["id"])
    day = datetime.fromtimestamp(until).strftime("%A %d %B %Y")
    out = [f"# Morning report – {day}", "",
           f"Covers {datetime.fromtimestamp(since):%d %b %H:%M} to {datetime.fromtimestamp(until):%d %b %H:%M}.", ""]
    needs = []
    for j in js:
        if j.get("status") == "safety_stop":
            needs.append(f"- ⛔ {link(j)}: {_reason(j)}")
        elif j.get("approval") == "waiting":
            needs.append(f"- 👀 {link(j)} is waiting for your approval")
        elif j.get("status") in FAILED and j.get("queue_blocked"):
            needs.append(f"- ✗ {link(j)} failed and is blocking its queue: {_reason(j)}")
        elif j.get("status") == "needs_review":
            needs.append(f"- ⚠ {link(j)} needs review: tests were changed")
    out += ["## Needs you", *(needs or ["Nothing. 🎉"]), ""]
    if not js and not bs:
        out += ["Nothing ran in this period.", ""]
    projects: dict[str, list] = {}
    for j in js:
        projects.setdefault(j.get("project") or "Other jobs", []).append(j)
    for proj, items in projects.items():
        done = [j for j in items if j.get("status") in DONE]
        bad = [j for j in items if j.get("status") in FAILED]
        other = [j for j in items if j not in done and j not in bad]
        secs = sum((j.get("finished") or until) - (j.get("started") or until) for j in items)
        out += [f"## {proj}",
                f"{len(done)} done, {len(bad)} failed, {len(other)} other · {_dur(secs)} · "
                f"{sum(j.get('rounds', 0) for j in items)} fix rounds · "
                f"{sum(j.get('rollbacks', 0) for j in items)} rollbacks · "
                f"{sum(max(0, j.get('attempts', 1) - 1) for j in items)} extra attempts", ""]
        for j in done:
            tests = f" ({j['tests_passed']} tests)" if j.get("tests_passed") else ""
            appr = {"waiting": " – waiting for approval", "merged": " – merged"}.get(j.get("approval"), "")
            out.append(f"- ✓ {link(j)}{tests}{appr}")
        for j in bad:
            out.append(f"- ✗ {link(j)}: {_reason(j)} – export for review: /api/jobs/{j['id']}/bundle")
        for j in other:
            out.append(f"- {j.get('status', '?')}: {link(j)}")
        out.append("")
    if bs:
        out.append("## Bench")
        for b in bs:
            out.append(f"- {b.get('label')}: {b.get('verdict') or b.get('summary') or 'finished'}")
        out.append("")
    facts = "\n".join(out)
    if qwen_summary is not None:
        s = qwen_summary(facts)
        if s:
            out += ["## Qwen's summary", "_Written by the local model; the facts above come from the job records._", "", s.strip(), ""]
    return "\n".join(out)
