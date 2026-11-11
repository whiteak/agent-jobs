#!/usr/bin/env python3
"""Upgrade 9b: push notifications to Jason's phone (ntfy).

The tested logic lives in `agentjobs_upgrades/notify.py` (settings, validation, quiet
hours, message cleaning, 3 tries). This module is the glue jobboard.py calls:

  * settings(path)          state/notify-settings.json -> NotifySettings. An empty server
                            means OFF: nothing is ever sent. NTFY_SERVER / NTFY_TOPIC seed
                            the defaults the first time; the topic starts random.
  * save_form(path, form)   the /notify settings form (validated before anything is saved)
  * job_done(m, path)       which notification a finished job/bench record gets, and its
                            text: job finished ✓/✗ with the checks counts, waiting for you,
                            safety stop, bench verdict
  * report_ready(...)       the morning report is ready (report.py is upgrade 10)
  * model_check(path, ping) edge-triggered "the model server is down"
  * push/send_test          sends through a background queue, so a job never waits on the
                            network; every outcome lands in state/notify-log.jsonl

Nothing here runs in the agent's sandbox (it is the board's own thread), nothing but
titles, status and counts ever reaches the phone - notify.clean drops code, paths and log
text - and no call in this module can raise into the worker. Standard library only.
"""
from __future__ import annotations

import dataclasses
import html
import json
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

from agentjobs_upgrades import notify

# What Jason can tick, in the order the settings page shows it.
EVENTS = (("job_finished", "A job finished", "a tick or a cross plus the checks counts"),
          ("needs_approval", "A job is waiting for you", "the guard flagged the tests, or it needs your OK"),
          ("safety_stop", "Safety stop", "louder, and it arrives in quiet hours too"),
          ("bench_finished", "A bench set finished", "with its overall verdict"),
          ("morning_report", "The morning report is ready", ""),
          ("model_down", "The model server is down", "jobs are waiting for it"))

LOG_NAME = "notify-log.jsonl"
LOG_ROWS = 40                      # how many send outcomes the page keeps
QUEUE_MAX = 200                    # a phone that never answers must not grow forever
MODEL_GAP_S = 60.0                 # how often the worker may ask the model server
NEEDS_YOU = ("needs_review", "stuck")
DONE_STATES = ("stopped", "cancelled", "interrupted")
ACTIVE_STATES = ("queued", "running", "stopping")


# ------------------------------------------------------------------ settings

_pending_topic = None


def default_topic():
    """One random topic per process until it is saved, so the settings page does not
    invent a new one on every reload. It must not be guessable."""
    global _pending_topic
    if _pending_topic is None:
        _pending_topic = notify.random_topic()
    return _pending_topic


def settings(path, write_defaults=False):
    """NotifySettings from the settings file. Missing or broken file = defaults (off).
    The file is only created when write_defaults is set, so reading never litters."""
    path = Path(path)
    raw = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded
    except (OSError, ValueError):
        raw = {}
    s = notify.NotifySettings(topic=_pending_topic or default_topic(),
                              server=os.environ.get("NTFY_SERVER", "").strip())
    if not raw:
        topic = os.environ.get("NTFY_TOPIC", "").strip()
        if topic:
            s.topic = topic
    else:
        s.server = str(raw.get("server", "") or "").strip()
        s.topic = str(raw.get("topic", "") or s.topic).strip()
        s.token = str(raw.get("token", "") or "")
        s.events = {e for e in (raw.get("events") or []) if e in notify.EVENTS}
        s.quiet_start = str(raw.get("quiet_start") or s.quiet_start)
        s.quiet_end = str(raw.get("quiet_end") or s.quiet_end)
        s.base_url = str(raw.get("base_url") or "").strip()
    if write_defaults:
        save(path, s)
    return s


def save(path, s):
    """Write the settings file (0600: it holds the optional access token)."""
    path = Path(path)
    data = {"server": s.server.strip().rstrip("/"), "topic": s.topic, "token": s.token,
            "events": sorted(s.events), "quiet_start": s.quiet_start, "quiet_end": s.quiet_end,
            "base_url": s.base_url.strip().rstrip("/")}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    return data


def form_to_settings(form, cur):
    """The /notify form -> NotifySettings. A blank token or topic keeps the saved one."""
    get = lambda k: (form.get(k, [""])[0]).strip()
    return notify.NotifySettings(server=get("server"),
                                 topic=get("topic") or cur.topic,
                                 token=get("token") or cur.token,
                                 events={k for k in notify.EVENTS if form.get(k)},
                                 quiet_start=get("quiet_start") or cur.quiet_start,
                                 quiet_end=get("quiet_end") or cur.quiet_end,
                                 base_url=get("base_url"))


def save_form(path, form, *, post=None, background=True):
    """POST /notify -> (errors, test-result-or-None). Nothing is saved when the input
    is wrong, so a typo cannot switch the phone off by accident."""
    cur = settings(path)
    s = form_to_settings(form, cur)
    errs = notify.validate(s)
    if errs:
        return errs, None
    save(path, s)
    test = None
    if (form.get("test") or [""])[0]:
        ok, why = send_test(path, (form.get("testevent") or ["test"])[0], post=post,
                            settings_obj=s, background=background)
        test = "sent" if ok else ("not sent: " + (why or "failed"))
    return [], test


# ------------------------------------------------------------------ send outcomes

def log_send(path, event, ok, note=""):
    """One line per send attempt set, newest last. Never raises."""
    try:
        p = Path(path).with_name(LOG_NAME)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.time(), "event": str(event)[:30],
                                "ok": bool(ok), "note": " ".join(str(note).split())[:200]}) + "\n")
    except OSError:
        pass


def recent(path, n=15):
    """The last send outcomes, newest first (for the settings page)."""
    try:
        lines = Path(path).with_name(LOG_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-60:]:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return list(reversed(out))[:max(0, min(n, LOG_ROWS))]


# ------------------------------------------------------------------ sending

_q = queue.Queue(maxsize=QUEUE_MAX)
_start_lock = threading.Lock()
_worker_on = False


def _worker():
    while True:
        path, event, msg, s, post, now, retries, sleep = _q.get()
        try:
            _send(path, event, msg, s, post=post, now=now, retries=retries, sleep=sleep)
        except Exception:
            pass


def _enqueue(item):
    global _worker_on
    try:
        _q.put_nowait(item)
    except queue.Full:
        return False
    if not _worker_on:
        with _start_lock:
            if not _worker_on:
                _worker_on = True
                threading.Thread(target=_worker, daemon=True).start()
    return True


def _send(path, event, msg, s, *, post=None, now=None, retries=3, sleep=time.sleep):
    """notify.send (3 tries, quiet hours) + one line in the log. Never raises."""
    errs = []
    try:
        ok = notify.send(s, event, msg, now=now, post=post or notify._post, retries=retries,
                         sleep=sleep, log=lambda m: errs.append(str(m)))
    except Exception as ex:
        errs.append(str(ex))
        ok = False
    log_send(path, event, ok, errs[-1] if errs else "")
    return ok


def push(path, event, title, lines, jid="", *, post=None, now=None, background=True,
         retries=3, sleep=time.sleep, settings_obj=None):
    """Queue (default) or send one notification. True = sent or queued. Never raises,
    and by default never waits: a phone that is unreachable holds up nothing."""
    try:
        s = settings_obj if settings_obj is not None else settings(path)
        if not s.server or event not in s.events:
            return False
        msg = notify.build(event, title, list(lines)[:2], jid, s)
        if background:
            return _enqueue((path, event, msg, s, post, now, retries, sleep))
        return _send(path, event, msg, s, post=post, now=now, retries=retries, sleep=sleep)
    except Exception:
        return False


# ------------------------------------------------------------------ what the phone shows

def plain(text, limit=90):
    """Only titles, status and counts: notify.clean drops code, paths and log text."""
    return notify.clean(str(text).replace("\u00b1", "+/-"), limit)


def job_short(m):
    return plain(" ".join(str(m.get("job") or m.get("id") or "job").split()), 70)


def checks_line(m):
    """The checks counts of a finished record - never a word of its output."""
    v = m.get("verify") or {}
    summ = (m.get("recheck") or {}).get("summary") or v.get("final") or {}
    res = summ.get("results") or []
    st = summ.get("status") or v.get("status")
    if m.get("verify_off"):
        return "Checks were skipped for this job."
    if st in (None, "", "unverified"):
        return "No checks were found."
    passed = sum(int(r.get("passed") or 0) for r in res)
    failed = sum(int(r.get("failed") or 0) for r in res)
    bad = sum(1 for r in res if not r.get("ok"))
    if st == "needs_review":
        return "Checks passed, but the tests changed in a suspicious way."
    if st == "stuck":
        return "Too many rollbacks - it needs a look."
    if st == "passed":
        return "Checks passed" + (f" - {passed} tests" if passed else "")
    return f"{bad} of {len(res)} checks failed" + (f" - {failed} tests failing" if failed else "")


def job_message(m, extra=""):
    """(event, title, lines) for a finished job/bench record, or None if it is not worth
    a buzz (Stopped by you, cancelled, interrupted - Jason pressed that himself)."""
    st = m.get("status")
    if st in ACTIVE_STATES or st in DONE_STATES:
        return None
    if st == "safety_stop":
        return ("safety_stop", "Safety stop",
                [job_short(m), "The safety guard stopped this job."])
    if m.get("kind") == "bench":
        ok = st == "done"
        lines = [plain(m.get("job") or m.get("id"), 60)]
        if extra:
            lines.append(plain(extra, 80))
        return ("bench_finished", "Bench run " + ("finished \u2713" if ok else "failed \u2717"), lines)
    if st == "done" and ((m.get("verify") or {}).get("status") in NEEDS_YOU
                         or m.get("approval") == "waiting"):
        return ("needs_approval", "Waiting for you",
                [job_short(m), "Open the job page - it needs your OK."])
    ok = st == "done"
    return ("job_finished", "Job finished \u2713" if ok else "Job failed \u2717",
            [job_short(m), checks_line(m)])


def job_done(m, path, extra="", **kw):
    """The notification for a job that just ended. Off unless a server is set."""
    msg = job_message(m, extra)
    if not msg:
        return False
    event, title, lines = msg
    return push(path, event, title, lines, str(m.get("id") or ""), **kw)


def report_ready(path, lines=(), **kw):
    """The morning report is ready. report.py (upgrade 10) builds the report itself;
    this is the call it makes once the report exists."""
    return push(path, "morning_report", "Morning report ready",
                list(lines) or ["The report is on the board."], **kw)


MODEL_STATE = {"last_ping": 0.0, "down": None}


def model_check(path, ping, *, state=None, min_gap_s=MODEL_GAP_S, now=time.time, **kw):
    """Tell the phone once when the model server stops answering (not every minute while
    it stays down). Returns True if a notification went out, None if nothing was checked."""
    s = kw.get("settings_obj") if kw.get("settings_obj") is not None else settings(path)
    if not s.server or "model_down" not in s.events:
        return None
    st = MODEL_STATE if state is None else state
    t = now()
    if t - float(st.get("last_ping") or 0.0) < min_gap_s:
        return None
    st["last_ping"] = t
    try:
        up = bool(ping())
    except Exception:
        up = False
    was = st.get("down")
    st["down"] = not up
    if up or was is False:
        return None
    return push(path, "model_down", "Model server down",
                ["The local model is not answering.", "Jobs wait until it is back."],
                settings_obj=s, **kw)


# ------------------------------------------------------------------ the test button

SAMPLES = {"job_finished": ("Job finished \u2713", ["Test from the job board", "Checks passed - 42 tests"]),
           "needs_approval": ("Waiting for you", ["Test from the job board", "Open the job page - it needs your OK."]),
           "safety_stop": ("Safety stop", ["Test from the job board", "The safety guard stopped this job."]),
           "bench_finished": ("Bench run finished \u2713", ["Test run", "overall 3.4 +/- 0.5"]),
           "morning_report": ("Morning report ready", ["Test: 3 jobs since yesterday"]),
           "model_down": ("Model server down", ["Test: the local model is not answering."]),
           "test": ("Test from Agent Jobs", ["If you can read this, your phone is set up."])}


def sample(event):
    return SAMPLES.get(event, SAMPLES["test"])


def outside_quiet(s, now=None):
    """A test Jason pressed on purpose must not be swallowed by quiet hours, so it is
    sent with a clock reading outside the window (same clock notify.send uses)."""
    now = now or datetime.now()
    for h in range(24):
        t = now.replace(hour=h, minute=0)
        if not notify.in_quiet_hours(s, t):
            return t
    return now


def send_test(path, event="test", *, post=None, background=True, settings_obj=None):
    """The "Send test notification" button and --test-phone. Returns (sent, why)."""
    s = settings_obj if settings_obj is not None else settings(path)
    errs = notify.validate(s)
    if errs:
        return False, "; ".join(errs)
    if not s.server:
        return False, "no server set, so notifications are off"
    ev = event if event in notify.EVENTS else "test"
    s2 = dataclasses.replace(s, events=set(notify.EVENTS) | {"test"})
    title, lines = sample(ev)
    ok = push(path, ev, title, lines, background=background, post=post,
              now=outside_quiet(s2), settings_obj=s2)
    return bool(ok), ("" if ok else "the send failed (see Recent sends)")


# ------------------------------------------------------------------ the settings page

def status_line(path, now=None):
    s = settings(path)
    if not s.server:
        return "Off - no ntfy server is set, so nothing is ever sent."
    if notify.in_quiet_hours(s, now or datetime.now()):
        return f"On, but quiet until {s.quiet_end} (only a safety stop gets through)."
    return "On."


def page_body(path, errors=(), note="", form=None, readonly=False):
    """The /notify card: server, topic, token, which events, quiet hours, a test button."""
    s = settings(path)

    def val(key, saved=""):
        return ((form.get(key, [""])[0]).strip() if form else "") or saved

    def ticked(event):
        return bool(form.get(event)) if form else (event in s.events)

    errs = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
    err_html = f'<div class="card bad"><b>Not saved:</b><ul>{errs}</ul></div>' if errs else ""
    boxes = ""
    for key, label, hint in EVENTS:
        boxes += (f'<label style="font-weight:400;margin:4px 0"><input type="checkbox" name="{key}" '
                  f'{"checked" if ticked(key) else ""}> {html.escape(label)}'
                  + (f' <span class="hint">{html.escape(hint)}</span>' if hint else "") + "</label>")
    opts = "".join(f'<option value="{k}"{" selected" if ticked(k) else ""}>{html.escape(l)}</option>'
                   for k, l, _ in EVENTS)
    rows = ""
    for rec in recent(path):
        when = time.strftime("%d %b %H:%M", time.localtime(rec.get("time", 0)))
        ok = bool(rec.get("ok"))
        rows += (f'<tr><td>{when}</td><td>{html.escape(str(rec.get("event", "")))}</td>'
                 f'<td class="{"ok" if ok else "bad"}">{"sent" if ok else "failed"}</td>'
                 f'<td class="mut">{html.escape(str(rec.get("note", ""))[:120])}</td></tr>')
    sends = (f'<div class="tbl"><table><thead><tr><th>When</th><th>What</th><th></th>'
             f'<th>Why</th></tr></thead><tbody>{rows}</tbody></table></div>' if rows
             else '<p class="mut">Nothing sent yet.</p>')
    save_btn = "" if readonly else (
        '<div class="brow" style="margin-top:12px"><button>Save</button> '
        '<button name="test" value="1" class="ghost2">Send test notification</button> '
        '<label style="font-weight:400;margin:0 0 0 8px">Test which one '
        '<select name="testevent" style="width:auto;display:inline-block;padding:4px">'
        f'{opts}</select></label></div>')
    ro = ('<p class="hint">This copy runs read-only, so the settings cannot be changed '
          'here.</p>') if readonly else ""
    note_html = f'<p class="ok">{html.escape(note)}</p>' if note else ""
    quiet_now = notify.in_quiet_hours(s, datetime.now())
    return f"""<div class="card"><h2>Phone alerts</h2>
<p><b>{html.escape(status_line(path))}</b> {"<span class=\"hint\">Quiet hours are running now.</span>" if quiet_now else ""}</p>
{note_html}{ro}
<p class="hint">The job board posts to <code>&lt;server&gt;/&lt;topic&gt;</code> with ntfy, and the ntfy app on
your phone shows it. Only titles, status and counts are ever sent - never code, diffs or log text.</p>
<form method="post" action="/notify">
<div class="row"><div><label>ntfy server</label>
<input type="text" name="server" value="{html.escape(val("server", s.server))}" placeholder="http://192.168.1.20:2586">
<span class="hint">Empty switches the phone alerts off.</span></div>
<div><label>Topic (keep it secret)</label>
<input type="text" name="topic" value="{html.escape(val("topic", s.topic))}">
<span class="hint">Anyone who knows this topic reads your alerts.</span></div></div>
<div class="row"><div><label>Access token <span class="hint" style="font-weight:400">(optional)</span></label>
<input type="password" name="token" placeholder="{('saved - leave empty to keep it' if (s.token and not form) else '')}">
<span class="hint">Sent as Bearer. Never shown back to you.</span></div>
<div><label>Quiet hours (no buzzes, except a safety stop)</label>
<div class="brow"><input type="text" name="quiet_start" value="{html.escape(val("quiet_start", s.quiet_start))}" style="width:90px">
<span style="align-self:center">to</span>
<input type="text" name="quiet_end" value="{html.escape(val("quiet_end", s.quiet_end))}" style="width:90px"></div>
<span class="hint">Local time, like 22:00 to 07:00.</span></div></div>
<div><label>Link to the board <span class="hint" style="font-weight:400">(only if your phone can reach this PC - upgrade 9a)</span></label>
<input type="text" name="base_url" value="{html.escape(val("base_url", s.base_url))}" placeholder="http://192.168.1.20:8090"></div>
<label style="margin-top:12px">Which events</label>{boxes}
{save_btn}</form></div>
<div class="card"><h2>Recent sends</h2>{sends}</div>"""
