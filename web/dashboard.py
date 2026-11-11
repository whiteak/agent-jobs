"""Upgrade 7a: live dashboard - timeline, Server-Sent Events, readable live log, badges.
(added 2026-11-11 by Claude; jobboard.py only has small "# U07" hooks)

  * timeline_for(dir)   JOBS/<id>/timeline.jsonl via agentjobs_upgrades.events.JobEvents:
                        {id, time, type, text, data}; queued/started/step/checks/fix rounds/
                        checkpoints/rollbacks/watchdog/safety stop/finished
  * job_stream()        one SSE stream per job page: "job" events (timeline, with ids, so
                        Last-Event-ID resumes), "log" batches (byte offsets) and "status"
  * badge / checks_cell the jobs list
  * HTML/CSS/JS for the timeline card and the live log (search, collapse, auto-scroll)
Standard library only, no CDN.
"""
import html, json, time
from pathlib import Path

from agentjobs_upgrades import events as evm

ACTIVE = ("queued", "running", "stopping")

# board event kind (upgrades.EventLog) -> timeline type; None = not on the timeline
TL_KIND = {"checks": "checks", "fix_round": "fix_round", "checkpoint": "checkpoint",
           "checkpoint_skipped": "checkpoint", "checkpoint_error": "checkpoint", "rollback": "rollback",
           "watchdog": "watchdog", "safety_stop": "safety_stop", "needs_review": "needs_review",
           "guard": "needs_review", "restart": "restart", "checks_start": None}


def timeline_for(job_dir):
    return evm.JobEvents(Path(job_dir) / "timeline.jsonl")


def tl_add(job_dir, type_, text, data=None):
    """Never raises: the timeline is a view, not a reason to break a job."""
    try:
        return timeline_for(job_dir).add(type_, str(text)[:1000], data or {})
    except Exception:
        return None


def finished_text(m):
    st = m.get("status")
    v = (m.get("verify") or {}).get("banner") or {}
    word = {"done": "Finished", "failed": "Failed", "stopped": "Stopped by you", "safety_stop": "Safety stop",
            "interrupted": "Interrupted", "cancelled": "Cancelled"}.get(st, str(st))
    bits = [word]
    if v.get("text"):
        bits.append(v["text"])
    elif m.get("fail_note"):
        bits.append(str(m["fail_note"])[:200])
    return " - ".join(bits)


# ------------------------------------------------------------------ badges

def badge(m, s=None):
    """(text, colour) for the jobs list."""
    st = m.get("status")
    if st == "safety_stop":
        return evm.badge("safety_stop")
    if st in ACTIVE:
        if st == "queued":
            return evm.badge("queued")
        if s and s.get("state") == "STUCK":
            return evm.badge("stuck")
        return evm.badge("running")
    v = (m.get("verify") or {}).get("status")
    if st == "done":
        if v in ("passed", "unverified", "needs_review", "stuck"):
            return evm.badge(v)
        return ("Done", "green")
    if st == "failed":
        return evm.badge("failed")
    if st in ("stopped", "cancelled"):
        return evm.badge("stopped")
    if st == "interrupted":
        return evm.badge("interrupted")
    return evm.badge(str(st or "?"))


def checks_cell(m):
    """Short checks result for the jobs list."""
    if m.get("kind") == "bench":
        return "-"
    v = m.get("verify") or {}
    rech = (m.get("recheck") or {}).get("summary")
    summ = rech or v.get("final")
    if m.get("verify_off"):
        return "not checked"
    if not summ:
        return "-" if m.get("status") not in ACTIVE else "..."
    res = summ.get("results") or []
    passed = sum(r.get("passed") or 0 for r in res)
    failed = sum(r.get("failed") or 0 for r in res)
    st = summ.get("status")
    if v.get("status") == "needs_review":
        return "needs review"
    if st == "passed":
        return f"{passed} passed" if passed else "passed"
    if st == "failed":
        return f"{failed} failed" if failed else "failed"
    return "none found"


# ------------------------------------------------------------------ SSE

def sse(event, data, id_=None):
    head = f"id: {id_}\n" if id_ is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def job_stream(jid, last_id, log_off, *, get_meta, get_status, get_log, job_dir,
               poll_s=1.0, keepalive_s=15.0, linger_s=3.0, max_s=6 * 3600, now=time.time, sleep=time.sleep):
    """SSE chunks for one job page until the job has finished (then "end").
    last_id = Last-Event-ID (timeline id); log_off = byte offset of the log already shown."""
    yield "retry: 3000\n\n"
    tl = timeline_for(job_dir)
    prev = None
    done_at = None
    start = last_ping = now()
    while now() - start < max_s:
        m = get_meta(jid)
        if not m:
            yield sse("end", {"reason": "gone"})
            return
        for ev in tl.list(after=last_id):
            last_id = ev["id"]
            yield sse("job", ev, ev["id"])
        items, nxt = get_log(jid, log_off)
        if items or nxt != log_off:
            yield sse("log", {"from": log_off, "next": nxt, "items": items})
            log_off = nxt
        s = get_status(m)
        sj = json.dumps(s, sort_keys=True, default=str)
        if sj != prev:
            prev = sj
            yield f"event: status\ndata: {sj}\n\n"
        if m.get("status") not in ACTIVE:
            done_at = done_at or now()
            if now() - done_at >= linger_s:
                yield sse("end", {"status": m.get("status")})
                return
        if now() - last_ping >= keepalive_s:
            last_ping = now()
            yield ": keepalive\n\n"
        sleep(poll_s)
    yield sse("end", {"reason": "time"})


# ------------------------------------------------------------------ HTML

def esc(x):
    return html.escape(str(x if x is not None else ""), quote=True)


def tl_row(ev):
    icon = evm.ICONS.get(ev.get("type"), "•")
    when = time.strftime("%H:%M:%S", time.localtime(ev.get("time", 0)))
    cls = {"safety_stop": "bad", "failed": "bad", "rollback": "warn2", "needs_review": "warn2",
           "watchdog": "warn2", "restart": "warn2"}.get(ev.get("type"), "")
    if ev.get("type") == "checks":
        cls = {"green": "ok", "red": "bad", "yellow": "warn", "orange": "warn2"}.get(
            (ev.get("data") or {}).get("colour"), "")
    return (f'<li class="{cls}"><span class="tli">{icon}</span><span class="tlt">{when}</span>'
            f'<span>{esc(ev.get("text"))}</span></li>')


def timeline_card(job_dir):
    evs = timeline_for(job_dir).list()
    rows = "".join(tl_row(e) for e in evs[-300:])
    empty = '' if rows else '<li class="mut tlempty">Nothing yet.</li>'
    last = evs[-1]["id"] if evs else 0
    return (f'<div class="card"><h2>Timeline</h2><ul class="tl" id="tl" data-last="{last}">{rows}{empty}</ul></div>')


def log_card(jid):
    return (f'<div class="card"><h2>Live log</h2>'
            f'<div class="brow logbar"><input type="text" id="logq" placeholder="Search the log" '
            f'style="flex:1;min-width:160px"><span class="hint" id="follow">Following</span>'
            f'<a href="/api/raw/{esc(jid)}" target="_blank">Show raw log</a></div>'
            f'<div class="feed logbox" id="feed"><span class="mut">Waiting for the agent...</span></div></div>')


CSS = """<style>
ul.tl{list-style:none;margin:0;padding:0 0 0 4px;border-left:2px solid var(--line)}
ul.tl li{display:flex;gap:8px;padding:4px 0 4px 8px;align-items:baseline}
ul.tl .tli{width:1.4em;text-align:center}ul.tl .tlt{color:var(--mut);font:12px ui-monospace,monospace;white-space:nowrap}
ul.tl li.ok{color:var(--ok)}ul.tl li.bad{color:var(--bad);font-weight:600}ul.tl li.warn{color:var(--warn)}ul.tl li.warn2{color:#d9631e}
.logbox{max-height:70vh;overflow-y:auto;margin-top:8px}
.feed .cmd code,.feed .cmd{font-family:ui-monospace,monospace}.feed .it.cmd b{color:var(--ok)}
.feed .it.edit b{color:var(--warn)}.feed .it.read{color:var(--mut)}
.feed .it.text{white-space:pre-wrap}
button.more{background:none;color:var(--acc);border:0;padding:0;font-size:12px;cursor:pointer}
</style>"""
